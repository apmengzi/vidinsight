"""收藏专辑封面本地持久化（2026-10-19）。

背景（实测）：board_notes.json 里捕获时拿到的 cover 是
sns-webpic-qc.xhscdn.com 的签名 URL，路径里带时效段（如 202610180136）。
过了时效段后，带/不带 Referer 一律 403（本次实测 3/3 全 403，Content-Length: 0），
白板页 242 张封面全部裂图。

唯一可靠的时机是「捕获那一刻」：页面刚渲染出来的卡片 URL 是新鲜的，
带 `Referer: https://www.xiaohongshu.com/` + 浏览器 UA 实测 200。
所以 ingest 到后端时立刻下载到
    {runtime}/static/covers/{boardId}/{noteId}.{ext}
并只把**相对路径** /static/covers/... 存进 BoardNoteStore；原始 URL 继续保留，
下载失败时前端仍可回退到原 URL（虽然大概率 403，但旧数据/瞬时故障下有意义）。

并发克制：模块级线程池 4 线程，单张超时 10s（连接 5s），单张上限 8MB。
幂等：目标文件已存在（任意允许扩展名）即跳过，不重复下载。
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, Optional

import requests

from app.utils.logger import get_logger

logger = get_logger(__name__)

COVER_URL_PREFIX = "/static/covers"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_REFERER = "https://www.xiaohongshu.com/"
# (connect, read)：单张封面 10s 内必须收完，避免个别慢图拖住整个线程池
_TIMEOUT = (5, 10)
_MAX_BYTES = 8 * 1024 * 1024

_EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/png": ".png",
    "image/gif": ".gif",
}
_ALLOWED_EXTS = (".jpg", ".webp", ".png", ".gif")

# 全局线程池：242 张全量捕获时也不会把后端打满（最多 4 个并发下载）
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cover-dl")
_executor_lock = threading.Lock()


def _cover_root() -> Path:
    """封面落盘根目录（dev=backend/static/covers，打包后=APPDATA/VidInsight/static/covers）。"""
    from app.utils.path_helper import get_runtime_path
    return Path(get_runtime_path("static", "covers"))


def _safe_id(value: str) -> bool:
    import re
    return bool(re.fullmatch(r"[0-9a-f]{24}", value or ""))


def find_local_cover(board_id: str, note_id: str) -> Optional[str]:
    """返回已存在的本地封面相对 URL（/static/covers/...），没有则 None。"""
    if not _safe_id(board_id) or not _safe_id(note_id):
        return None
    base = _cover_root() / board_id
    for ext in _ALLOWED_EXTS:
        if (base / f"{note_id}{ext}").is_file():
            return f"{COVER_URL_PREFIX}/{board_id}/{note_id}{ext}"
    return None


def cover_file_exists(rel_path: str) -> bool:
    """相对 URL → 磁盘存在性（读取接口自愈用）。"""
    if not rel_path or not rel_path.startswith(COVER_URL_PREFIX + "/"):
        return False
    rel = rel_path[len(COVER_URL_PREFIX) + 1:]
    try:
        return (_cover_root() / rel).is_file()
    except Exception:
        return False


def _download(url: str) -> tuple[Optional[bytes], str]:
    """下载一张封面，返回 (bytes, ext)；失败返回 (None, "")。

    ext 由 Content-Type 决定（xhscdn 的 URL 后缀是 `!nc_n_webp_prv_1` 这类
    处理指令，不能当扩展名用）。
    """
    try:
        with requests.get(
            url,
            headers={
                "User-Agent": _UA,
                "Referer": _REFERER,
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
            timeout=_TIMEOUT,
            stream=True,
        ) as resp:
            if resp.status_code != 200:
                logger.warning(f"[cover] 下载失败 status={resp.status_code} url={url[:120]}")
                return None, ""
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            ext = _EXT_BY_TYPE.get(ctype, "")
            if not ext:
                logger.warning(f"[cover] 非图片内容 type={ctype or '-'} url={url[:120]}")
                return None, ""
            buf = bytearray()
            for chunk in resp.iter_content(64 * 1024):
                if not chunk:
                    continue
                buf.extend(chunk)
                if len(buf) > _MAX_BYTES:
                    logger.warning(f"[cover] 图片超过 {_MAX_BYTES} 字节，放弃 url={url[:120]}")
                    return None, ""
            if not buf:
                return None, ""
            return bytes(buf), ext
    except Exception as e:  # noqa: BLE001 —— 单张失败不能影响捕获主流程
        logger.warning(f"[cover] 下载异常 {type(e).__name__}: {str(e)[:120]} url={url[:120]}")
        return None, ""


def _download_and_write(board_id: str, note_id: str, url: str) -> Optional[str]:
    data, ext = _download(url)
    if not data:
        return None
    try:
        dest_dir = _cover_root() / board_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{note_id}{ext}"
        # 临时名带线程 id：同一 note 被两个 ingest 请求并发下载时不会互相踩 tmp
        tmp = dest.with_suffix(f"{dest.suffix}.{threading.get_ident()}.tmp")
        tmp.write_bytes(data)
        # 2026-10-20：rename 偶发 WinError 5（拒绝访问）——Defender/索引器
        # 短暂锁住新建文件（实测 2026-10-20 05:31/05:50 两发）。小退避重试，
        # 重试间隙若并发兄弟已写好同名文件则直接采用。
        for attempt in range(3):
            try:
                tmp.replace(dest)
                return f"{COVER_URL_PREFIX}/{board_id}/{note_id}{ext}"
            except PermissionError:
                if attempt < 2:
                    time.sleep(0.05 * (attempt + 1))
                    done = find_local_cover(board_id, note_id)
                    if done:
                        try:
                            tmp.unlink(missing_ok=True)
                        except OSError:
                            pass
                        return done
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        logger.warning(f"[cover] 落盘重试仍被拒 note={note_id}（杀软/索引器占用）")
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[cover] 落盘失败 note={note_id}: {e}")
        return None


def persist_covers(board_id: str, entries: Iterable[tuple[str, str]]) -> dict:
    """把一批 (note_id, cover_url) 下载到本地，返回统计。

    返回值：
        {"downloaded": n, "skipped": n, "failed": n,
         "items": {note_id: "/static/covers/..."}}
    - 已存在本地文件 → skipped（幂等，重捕获不重复下）
    - 空/非 http URL 或下载失败 → failed（原 URL 保留作 fallback）
    """
    stats = {"downloaded": 0, "skipped": 0, "failed": 0, "items": {}}
    if not _safe_id(board_id):
        return stats

    pending: list[tuple[str, str]] = []
    seen: set[str] = set()
    for note_id, url in entries:
        if not _safe_id(note_id) or note_id in seen:
            continue
        seen.add(note_id)
        existing = find_local_cover(board_id, note_id)
        if existing:
            stats["skipped"] += 1
            stats["items"][note_id] = existing
            continue
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            stats["failed"] += 1
            continue
        pending.append((note_id, url))

    if not pending:
        return stats

    with _executor_lock:
        futures = {
            _executor.submit(_download_and_write, board_id, nid, url): nid
            for nid, url in pending
        }
    for fut in as_completed(futures):
        nid = futures[fut]
        try:
            rel = fut.result()
        except Exception:  # noqa: BLE001
            rel = None
        if rel:
            stats["downloaded"] += 1
            stats["items"][nid] = rel
        else:
            stats["failed"] += 1
    return stats
