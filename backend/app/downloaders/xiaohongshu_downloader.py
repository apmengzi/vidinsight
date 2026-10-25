"""小红书下载器。

基于已批量验证的抓取原型（20+ 条笔记实测）产品化：
页面 __INITIAL_STATE__ → 笔记元信息 + 视频直链 → mp4 下载 → ffmpeg 转 mp3。

支持链接形态：
  - xhslink.cn 短链（App 分享）
  - xiaohongshu.com/explore/{id}?xsec_token=...（网页分享）
  - xiaohongshu.com/discovery/item/{id}?...
  - xiaohongshu.com/collection/item/...（合集：用 expand_collection 展开成逐条任务）

登录态：设置页「下载配置 → 小红书」粘贴网页版 Cookie（web_session）后：
  - 页面抓取携带登录 Cookie，显著降低 xsec_token 时效问题
  - 合集展开返回全部笔记；详情页在登录态下尝试免 token 抓取

边界（主动声明）：
  - 图文笔记无音轨：正文+图片说明合成为「转写材料」走 LLM 总结（不下载音频）。
  - 视频笔记：音频转写 + 长正文一并作为总结材料（正文由管线前置注入）。
"""
import hashlib
import json
import logging
import os
import re
import subprocess
import urllib.parse
from typing import Optional, Union

from app.downloaders.base import Downloader
from app.enmus.note_enums import DownloadQuality
from app.exceptions.biz_exception import BizException
from app.exceptions.codes import ErrorCode
from app.models.audio_model import AudioDownloadResult
from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.services.cookie_manager import CookieConfigManager
from app.utils.log_standard import log_event
from app.utils.logger import get_logger

logger = get_logger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

NOTE_ID_RE = re.compile(
    r"/(?:explore|discovery/item|board/\w+|collection/item)/([0-9a-f]{24})",
)
# 2026-10-08 实测真 bug：未登录时 xhslink 短链展开会跳到
#   https://www.xiaohongshu.com/login?redirectPath=http%3A%2F%2F...%2Fdiscovery%2Fitem%2F<24位ID>%3F...
# 真实笔记 ID 被 URL 编码后埋在 redirectPath 里，标准路径正则认不出来 →
# 预览报"无法从链接解析笔记 ID"，而链接本身完全合法（用户从 App 分享复制的）。
# 先解出这层，再退回常规匹配。
_REDIRECT_NOTE_ID_RE = re.compile(
    r"(?:redirectPath|redirect|redirect_url|url)=([^&]+)", re.I,
)
MIN_TEXT_MATERIAL_LEN = 30   # 正文合成转写的最小长度（短于此直接报错）


def _bin(name: str) -> str:
    """定位 ffmpeg/ffprobe：优先 FFMPEG_BIN_PATH（便携版启动器注入）。"""
    custom_dir = os.getenv("FFMPEG_BIN_PATH", "")
    if custom_dir:
        candidate = os.path.join(custom_dir, f"{name}.exe" if os.name == "nt" else name)
        if os.path.exists(candidate):
            return candidate
    return name


def _sanitize(name: str, maxlen: int = 60) -> str:
    return re.sub(r'[\\/:*?"<>|\n\r\t#]+', "_", name or "").strip("_ ")[:maxlen] or "xhs_note"


# 分享链接自带的来源追踪参数（2026-10-13 用户实测提供）。
# 用户从 App「分享 → 复制链接」拿到的链接形如：
#   .../board/<id>?xhsshare=&appuid=...&apptime=...&share_id=...&share_channel=copy_link
# 这些参数**不影响解析**（我们只取路径里的 board_id / note id），但会：
#   1) 让链接长得吓人，用户以为要清理才敢贴
#   2) 混进日志与缓存键，造成同一条内容多个不同 key
# 所以统一剥离——用户直接贴分享链接即可，不用手动删。
_SHARE_PARAMS = {
    "xhsshare", "appuid", "apptime", "share_id", "share_channel",
    "share_from_user_hidden",
}
# 注意：xsec_token / xsec_source 是 note 详情页的**必需访问凭证**，
# 不在通用剥离集里；只有 board/collection 这类不需要 token 的场景
# 才通过 keep_xsec=False 额外剥离（见 strip_share_params）。


def strip_share_params(url: str, *, keep_xsec: bool = True) -> str:
    """剥离分享来源参数，返回干净链接（解析用）。

    keep_xsec=True（默认）时保留 xsec_token——note 详情页缺它必失败。
    只有 board/collection 这类不需要 token 的场景才传 keep_xsec=False。
    """
    if not url or "?" not in url:
        return url or ""
    try:
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

        parts = urlsplit(url)
        pairs = parse_qsl(parts.query, keep_blank_values=False)
        drop = set(_SHARE_PARAMS)
        if not keep_xsec:
            drop |= {"xsec_token", "xsec_source"}
        kept = [(k, v) for k, v in pairs if k not in drop]
        return urlunsplit((parts.scheme, parts.netloc, parts.path,
                           urlencode(kept), parts.fragment))
    except Exception:
        return url


class XiaoHongShuDownloader(Downloader):

    def __init__(self):
        super().__init__()
        self._cookie_mgr = CookieConfigManager()
        self._note_cache: dict[str, dict] = {}   # url -> note（并发任务共享单例，按 URL 键避免串数据）

    # ── 对外接口 ─────────────────────────────────

    def download(self, video_url: str, output_dir: str = None,
                 quality: DownloadQuality = "fast", need_video: Optional[bool] = False,
                 skip_download: bool = False) -> AudioDownloadResult:
        output_dir = output_dir or self.cache_data or "data"
        os.makedirs(output_dir, exist_ok=True)

        url = self._resolve_url(video_url)
        note_id = self._extract_note_id(url)
        if not note_id:
            raise Exception("无法从小红书链接解析笔记 ID，请用 App 内「复制链接」获取分享链接")

        note = self._get_note(url, note_id)
        title = (note.get("title") or "").strip() or "小红书笔记"
        desc = note.get("desc") or ""
        user = (note.get("user") or {}).get("nickname") or (note.get("user") or {}).get("nickName") or ""
        tags = " ".join(t.get("name", "") for t in note.get("tagList") or [])
        video_url_media = self._extract_video_url(note)

        cover_url = ""
        image_list = note.get("imageList") or []
        if image_list:
            cover_url = image_list[0].get("urlDefault") or image_list[0].get("url") or ""

        raw_info = {
            "title": title,
            "description": desc,
            "uploader": user,
            "tags": tags,
            "webpage_url": url,
            "extractor": "xiaohongshu",
            "image_urls": [
                im.get("urlDefault") or im.get("url") or ""
                for im in image_list if (im.get("urlDefault") or im.get("url"))
            ][:9],
        }

        mp4_path = os.path.join(output_dir, f"{note_id}.mp4")
        if not video_url_media:
            # 图文笔记：无音轨，正文已由 download_subtitles 合成为转写材料
            logger.info(f"图文笔记（纯正文总结）: {title[:30]}")
            raw_info["text_only"] = True
            return AudioDownloadResult(
                file_path="",
                title=title,
                duration=0.0,
                cover_url=cover_url,
                platform="xiaohongshu",
                video_id=note_id,
                raw_info=raw_info,
                video_path=None,
            )

        if skip_download:
            # 管线已有转写材料（正文合成），仅提取元信息
            raw_info["text_only"] = False
            return AudioDownloadResult(
                file_path="",
                title=title,
                duration=self._probe_duration(mp4_path) if os.path.exists(mp4_path) else 0.0,
                cover_url=cover_url,
                platform="xiaohongshu",
                video_id=note_id,
                raw_info=raw_info,
                video_path=None,
            )

        def _fetch_video():
            if not (os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 3000):
                logger.info(f"下载小红书视频: {title[:30]}")
                size = self._download_file(video_url_media, mp4_path)
                if size < 3000:
                    raise Exception(f"小红书视频下载失败（{size} bytes）: {title[:30]}")

        mp3_path = os.path.join(output_dir, f"{note_id}.mp3")
        duration = 0.0
        if not os.path.exists(mp3_path):
            bitrate = {"fast": "32k", "medium": "64k", "slow": "128k"}.get(
                quality.value if hasattr(quality, "value") else quality, "64k")
            # 2026-09-15 实测：视频 CDN 偶发下到损坏文件（断流截断/HTML 错误页），
            # ffmpeg 提取直接 exit -22。策略：probe 时长为 0 视为损坏 → 删文件
            # 重下一次再提取；仍失败才报错（单条失败不再拖垮整批合集任务）。
            last_err: Optional[Exception] = None
            for attempt in (1, 2):
                try:
                    _fetch_video()
                    duration = self._probe_duration(mp4_path)
                    if duration <= 0 and attempt == 1:
                        raise ValueError("probe duration=0（下载文件疑似损坏）")
                    subprocess.run(
                        [_bin("ffmpeg"), "-y", "-i", mp4_path, "-vn",
                         "-acodec", "libmp3lame", "-b:a", bitrate, mp3_path],
                        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    last_err = None
                    break
                except (subprocess.CalledProcessError, ValueError) as e:
                    last_err = e
                    logger.warning(
                        f"小红书视频提取音频失败（第 {attempt}/2 次）: {e}；"
                        "删除已下载文件后重下重试")
                    for p in (mp4_path, mp3_path):
                        if os.path.exists(p):
                            try:
                                os.remove(p)
                            except OSError:
                                pass
            if last_err is not None:
                raise Exception(
                    "小红书视频提取音频失败（已自动重下重试一次）：视频源可能损坏，"
                    "可稍后在对话里重发该条链接重试")

        return AudioDownloadResult(
            file_path=mp3_path,
            title=title,
            duration=duration,
            cover_url=cover_url,
            platform="xiaohongshu",
            video_id=note_id,
            raw_info=raw_info,
            video_path=mp4_path if need_video else None,
        )

    def download_direct(
        self,
        direct_url: str,
        video_url: str,
        output_dir: Union[str, None] = None,
        title: Optional[str] = None,
        cover_url: Optional[str] = None,
    ) -> AudioDownloadResult:
        """用现成直链取片（hellotik 兜底通道专用）。

        小红书网页版对无 xsec_token 的链接一律码墙（登录态也一样），yt-dlp 与
        自研 SSR 都可能拿不到；此时第三方解析出的 xhscdn 直链是最后一条路。
        与 B 站不同，小红书直链**不带签名、不会过期**，但仍应尽快落盘。
        """
        output_dir = output_dir or self.cache_data or "data"
        os.makedirs(output_dir, exist_ok=True)

        # 短链可能解析跳转到登录页（未登录时 xhslink 的解析结果），此时拿不到
        # 真实 note_id；退而求其次从直链路径里取，保证同一条笔记缓存键稳定
        note_id = self._extract_note_id(self._resolve_url(video_url))
        if not note_id:
            note_id = self._extract_note_id(direct_url)
        if not note_id:
            note_id = hashlib.md5(direct_url.encode("utf-8")).hexdigest()[:12]
        mp4_path = os.path.join(output_dir, f"{note_id}.mp4")
        mp3_path = os.path.join(output_dir, f"{note_id}.mp3")

        if not (os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 3000):
            size = self._download_file(direct_url, mp4_path)
            if size < 3000:
                if os.path.exists(mp4_path):
                    os.remove(mp4_path)
                raise Exception(
                    f"直链下载失败或文件过小（{size} bytes），请重新点「第三方网页兜底解析」"
                )

        duration = 0.0
        if not os.path.exists(mp3_path):
            subprocess.run(
                [_bin("ffmpeg"), "-y", "-i", mp4_path, "-vn",
                 "-acodec", "libmp3lame", "-b:a", "64k", mp3_path],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        duration = self._probe_duration(mp4_path)

        return AudioDownloadResult(
            file_path=mp3_path,
            title=title or "小红书笔记",
            duration=duration,
            cover_url=cover_url or "",
            platform="xiaohongshu",
            video_id=note_id,
            raw_info={"engine": "hellotik_direct", "video_path": mp4_path},
            video_path=mp4_path,
        )

    def download_subtitles(self, video_url: str, output_dir: str = None,
                           langs: list = None) -> Optional[TranscriptResult]:
        """图文笔记：把「正文 + 图片序号」合成为转写材料，让管线跳过音频下载。

        视频笔记返回 None（音频照常转写，长正文由管线在总结前注入）。
        """
        try:
            url = self._resolve_url(video_url)
            note_id = self._extract_note_id(url)
            if not note_id or "/collection/item/" in url:
                return None
            note = self._get_note(url, note_id)
            if self._extract_video_url(note):
                return None  # 视频笔记：走音频转写

            desc = (note.get("desc") or "").strip()
            if len(desc) < MIN_TEXT_MATERIAL_LEN:
                raise Exception(
                    "该图文笔记正文太短，无法生成有价值的洞察总结"
                )

            image_list = note.get("imageList") or []
            segs = [TranscriptSegment(
                start=0.0, end=0.0, text=f"【小红书笔记正文】\n{desc}")]
            if image_list:
                segs.append(TranscriptSegment(
                    start=0.0, end=0.0,
                    text=f"【配图】共 {len(image_list)} 张图片"))
            user = (note.get("user") or {}).get("nickname") or ""
            if user:
                segs.append(TranscriptSegment(
                    start=0.0, end=0.0, text=f"【作者】{user}"))

            full_text = "\n".join(s.text for s in segs)
            logger.info(f"图文笔记正文已合成为总结材料（{len(desc)} 字）")
            return TranscriptResult(
                language="zh", full_text=full_text, segments=segs)
        except Exception as e:
            # 图文/解析失败时把错误抛给管线（视频笔记的解析失败交给 download 再试）
            if "图文" in str(e) or "正文太短" in str(e) or "合集" in str(e):
                raise
            logger.warning(f"小红书字幕合成跳过: {e}")
            return None

    def expand_collection(self, url: str) -> dict:
        """合集/专辑展开：返回全部笔记清单（批量总结的输入）。

        登录态 Cookie 强烈建议配置：匿名状态下合集页拿不到笔记的
        xsec_token，逐条详情需要登录 Cookie 才能免 token 抓取。
        """
        resolved = self._resolve_url(url)
        html = self._http_get(resolved)
        if len(html) < 5000 or "__INITIAL_STATE__" not in html:
            spec = ErrorCode.XHS_PAGE_FETCH_FAILED
            raise BizException(spec.code, spec.message, err=spec.err,
                               detail={"url": resolved[:120], "html_len": len(html)})
        state = self._parse_state(html)
        col = ((state or {}).get("noteData") or {}).get("collectionData") or {}
        note_list = col.get("noteList") or []
        if not note_list:
            spec = ErrorCode.XHS_COLLECTION_EMPTY
            raise BizException(spec.code, spec.message, err=spec.err,
                               detail={"url": resolved[:120]})

        has_cookie = bool(self._cookie_mgr.get("xiaohongshu"))
        # 兜底 1：WebView 从合集/主页 SPA 页面收割的 token 链接（可信度最高——
        # href 来自 XHS 自己渲染的卡片，token 与 xsec_source 配套）
        webview_tokens: dict[str, str] = {}
        try:
            from app.services.xhs_note_cache import XHSTokenStore
            for nid in (n.get("id") or "" for n in note_list):
                if nid:
                    url = XHSTokenStore.get(nid)
                    if url:
                        webview_tokens[nid] = url
        except Exception:
            pass
        # 兜底 2：登录态页面的内嵌 JSON 可能在 noteList 之外携带 id→token 对，
        # 全文正则收割（两种字段顺序都扫）
        raw_tokens: dict[str, str] = {}
        id_then_token = re.compile(
            r'"(?:note_?[iI]d|id)":"([0-9a-f]{24})"[^{}]{0,240}?"xsec_token":"([^"\\]+)"')
        token_then_id = re.compile(
            r'"xsec_token":"([^"\\]+)"[^{}]{0,240}?"(?:note_?[iI]d|id)":"([0-9a-f]{24})"')
        for m in id_then_token.finditer(html):
            nid, tok = m.group(1), m.group(2)
            if nid and tok:
                raw_tokens.setdefault(nid, tok)
        for m in token_then_id.finditer(html):
            tok, nid = m.group(1), m.group(2)
            if nid and tok:
                raw_tokens.setdefault(nid, tok)
        if raw_tokens:
            logger.info(f"从合集页 HTML 收割到 {len(raw_tokens)} 条 xsec_token")

        notes = []
        for n in note_list:
            nid = n.get("id") or ""
            try:
                from app.services.xhs_note_cache import XHSNoteCache
                cached = XHSNoteCache.get(nid) is not None
            except Exception:
                cached = False
            if nid in webview_tokens:
                detail = webview_tokens[nid]      # WebView 收割的完整链接（token+source 配套，原样用）
                token = "webview"
            else:
                token = n.get("xsec_token") or raw_tokens.get(nid, "")
                if token:
                    detail = f"https://www.xiaohongshu.com/explore/{nid}?xsec_token={token}&xsec_source=app_share"
                elif has_cookie:
                    detail = f"https://www.xiaohongshu.com/explore/{nid}"
                else:
                    detail = f"https://www.xiaohongshu.com/explore/{nid}"
            notes.append({
                "note_id": nid,
                "title": n.get("title") or n.get("desc") or "未命名",
                "type": n.get("type") or "",
                "url": detail,
                "has_token": bool(token),
                # 被动捕获模式：笔记已被用户浏览时由 WebView 记录过，
                # 无 token 也能从 XHSNoteCache 直接取详情
                "cached": cached,
            })
        return {
            "name": col.get("name") or "小红书合集",
            "total": col.get("noteNum") or len(notes),
            "logged_in": has_cookie,
            "notes": notes,
        }

    def expand_board(self, url: str) -> dict:
        """收藏专辑壳解析：匿名 SSR 拿专辑名/总数，合并被动捕获缓存。

        2026-09-21 实测：专辑页匿名 SSR 只有壳（boardDetails：名/total/owner），
        笔记列表走登录态签名 API（boardFeedsMap.notes 匿名为空）——列表数据
        由被动捕获窗口（用户自己翻收藏）经 /xhs/ingest_board_notes 提供，
        这里只负责解析链接里的 board_id + 给出壳信息。
        """
        m = re.search(r"/board/([0-9a-f]{24})", url)
        if not m:
            spec = ErrorCode.XHS_PAGE_FETCH_FAILED
            raise BizException(spec.code, spec.message, err=spec.err,
                               detail={"url": url[:120], "reason": "not a board link"})
        board_id = m.group(1)
        # 剥掉分享来源参数（xhsshare/appuid/share_id/share_channel…）：用户从 App
        # 分享复制的链接带一长串追踪参数，不影响解析但会让请求 URL 变脏。
        # board 场景不需要 xsec_token，可一并剥离（keep_xsec=False）。
        clean_url = strip_share_params(url, keep_xsec=False) or url
        name, total = "我的收藏专辑", 0
        # 匿名 SSR 偶发返回风控壳页（实测同一链接首次 total=0、立刻重试即 270），
        # 重试 2 次可显著提高专辑名/总数的命中率。拿不到也只是壳信息降级，
        # 不影响后续"被动捕获"这条主路径。
        for attempt in range(3):
            try:
                html = self._http_get(clean_url, use_cookie=False)  # 匿名壳即可
                if len(html) >= 5000:
                    state = self._parse_state(html) or {}
                    bd = ((state.get("board") or {}).get("boardDetails") or {})
                    if bd.get("name"):
                        name = bd["name"]
                    total = int(bd.get("total") or 0)
                if total > 0:
                    break  # 拿到真实总数即停，不必再试
            except Exception as e:
                logger.warning(f"专辑壳解析降级（第 {attempt + 1}/3 次，不影响功能）: {e}")
            if attempt < 2:
                import time as _t
                _t.sleep(0.8)
        from app.services.board_note_store import BoardNoteStore
        from app.services.xhs_note_cache import XHSNoteCache
        notes = BoardNoteStore.list_notes(board_id)
        for n in notes:
            n["detail_cached"] = XHSNoteCache.get(n["note_id"]) is not None
        return {
            "board_id": board_id,
            "name": name,
            "total": total,
            "captured": len(notes),
            "notes": notes,
        }

    # ── 内部工具 ─────────────────────────────────

    def _get_note(self, url: str, note_id: str) -> dict:
        """取笔记节点：下载器实例缓存优先 → WebView 抓取缓存 → curl 抓页面。

        WebView 缓存（XHSNoteCache）来自桌面端隐藏窗口由页面自身 JS 签名取数，
        是无 xsec_token 裸链接（合集展开产物）唯一能拿到详情的通道。

        2026-09-15 实测：**过期 cookie 会毒化请求**——带失效 web_session 访问
        带 xsec_token 的详情页，XHS 顶回登录墙壳页（39KB 无笔记数据）；完全
        不带 cookie 反而能拿到全量数据（token 本身匿名可用）。所以首试失败
        时摘掉 cookie 重试一次。
        """
        if url in self._note_cache:
            return self._note_cache[url]
        try:
            from app.services.xhs_note_cache import XHSNoteCache
            cached = XHSNoteCache.get(note_id)
        except Exception:
            cached = None
        if cached:
            logger.info(f"命中 WebView 抓取缓存: {note_id}")
            self._note_cache[url] = cached
            return cached

        def _attempt(use_cookie: bool) -> tuple[str, Optional[dict]]:
            html = self._http_get(url, use_cookie=use_cookie)
            if len(html) < 5000 or "__INITIAL_STATE__" not in html:
                return html, None
            note = self._find_note(self._parse_state(html), note_id)
            return html, note

        html, note = _attempt(True)
        if note is None:
            if self._cookie_mgr.get("xiaohongshu"):
                log_event(logger, "xhs.detail.retry_anonymous", level=logging.INFO,
                          note_id=note_id,
                          reason="带 cookie 未取到笔记（疑似过期 cookie 毒化），摘 cookie 重试")
            html, note = _attempt(False)
        if len(html) < 5000 or "__INITIAL_STATE__" not in html:
            # 2026-09-21 实测：账号被「设备登录超限」踢下线后，页面返回的是
            # 登录墙壳页——笼统的「页面获取失败」会让用户以为产品坏了。
            # 识别登录墙特征给明确指引。
            if "登录超限" in html or "扫码登录" in html or "请重新登录" in html:
                spec = ErrorCode.XHS_LOGIN_REQUIRED
                raise BizException(spec.code, spec.message, err=spec.err,
                                   detail={"url": url[:120], "html_len": len(html)})
            spec = ErrorCode.XHS_PAGE_FETCH_FAILED
            raise BizException(spec.code, spec.message, err=spec.err,
                               detail={"url": url[:120], "html_len": len(html)})
        if not isinstance(note, dict):
            # 2026-09-27 实测：页面带合法 INITIAL_STATE 但目标笔记不在状态树里
            # （token 失效/风控壳页的另一种形态）——此前这里返回 None，
            # 让调用方炸出 NoneType；现在给结构化错误。
            spec = ErrorCode.XHS_PAGE_FETCH_FAILED
            raise BizException(
                spec.code, "页面已返回但未找到该笔记数据（链接可能已失效）",
                err=spec.err,
                detail={"url": url[:120], "note_id": note_id,
                        "reason": "note_not_in_initial_state"})
        self._note_cache[url] = note
        return note

    def _resolve_url(self, url: str) -> str:
        """xhslink 短链解析为真实笔记链接（保留 xsec_token 等参数）。

        小红书同时维护 xhslink.com 与 xhslink.cn 两个短链域（2026-09-27 实测
        两域均存活），App 分享两种形态都可能出现——都要走解析。
        """
        if "xhslink.com" not in url and "xhslink.cn" not in url:
            return url
        try:
            result = subprocess.run(
                ["curl", "-s", "-o", os.devnull, "-w", "%{url_effective}",
                 "-L", "--max-time", "30", "-A", UA, url],
                capture_output=True, text=True, timeout=40,
            )
            resolved = (result.stdout or "").strip()
            if resolved.startswith("http"):
                logger.info(f"小红书短链已解析: {resolved[:80]}")
                return resolved
        except Exception as e:
            logger.warning(f"小红书短链解析失败: {e}")
        raise Exception("小红书短链解析失败，请检查网络后重试")

    @staticmethod
    def _extract_note_id(url: str) -> Optional[str]:
        m = NOTE_ID_RE.search(url)
        if m:
            return m.group(1)
        # 登录页形态：真实 ID 藏在 redirectPath 的 URL 编码里，先解码再找
        if "login" in (url or "").lower():
            for rm in _REDIRECT_NOTE_ID_RE.finditer(url):
                try:
                    inner = urllib.parse.unquote(rm.group(1))
                except Exception:
                    continue
                m2 = NOTE_ID_RE.search(inner)
                if m2:
                    return m2.group(1)
        return None

    def _http_get(self, url: str, use_cookie: bool = True) -> str:
        cmd = ["curl", "-s", "-L", "--max-time", "30", "-A", UA]
        if use_cookie:
            cookie = self._cookie_mgr.get("xiaohongshu")   # 每次现读（热更新）
            if cookie:
                cmd += ["-H", f"Cookie: {cookie.strip()}"]
        cmd.append(url)
        result = subprocess.run(cmd, capture_output=True, timeout=40)
        return result.stdout.decode("utf-8", "ignore")

    @staticmethod
    @staticmethod
    def _parse_state(html: str) -> Optional[dict]:
        raw = html.replace("\\u002F", "/")
        m = re.search(r"window\.__INITIAL_STATE__\s*=\s*", raw)
        if not m:
            return None
        end = raw.find("</script>", m.end())
        blob = raw[m.end():end].strip().rstrip(";")
        blob = re.sub(r"\bundefined\b", "null", blob)
        # 2026-09-15 实测：XHS 详情页 state 里嵌着 JS 表达式 new Map([]) /
        # new Set([])（AiNoteDetailStore.noteDetailMap 等），不是合法 JSON——
        # json.loads/JSON.parse 都必炸（之前所有 400102 皆因此）。消毒成 JSON。
        blob = re.sub(r"new\s+Map\(\s*\[\s*\]\s*\)", "{}", blob)
        blob = re.sub(r"new\s+Set\(\s*\[\s*\]\s*\)", "[]", blob)
        blob = re.sub(r"new\s+Map\((\[\s*.*?\s*\])\)", r"\1", blob)
        try:
            return json.loads(blob)
        except Exception as e:
            logger.warning(f"小红书 INITIAL_STATE 解析失败: {e}")
            return None

    @staticmethod
    def _find_note(state: Optional[dict], note_id: str) -> Optional[dict]:
        """在状态树中找笔记节点：优先 noteId 精确匹配，退化为 id 字段。"""
        if not state:
            return None
        stack = [state]
        fallback = None
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if node.get("noteId") == note_id and node.get("desc") is not None:
                    return node
                if (fallback is None and node.get("id") == note_id
                        and node.get("desc") is not None):
                    fallback = node
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
        return fallback

    @staticmethod
    def parse_token_links(links: list) -> dict:
        """从 WebView 收割的卡片 href 中解析 note_id → 带 token 的详情链接。

        支持四种卡片 href 形态：
        - /explore/{id}?xsec_token=...
        - /discovery/item/{id}?xsec_token=...
        - /search_result/{id}?xsec_token=...&xsec_source=pc_search
        - /user/profile/{uid}/{id}?xsec_token=...（作者主页卡片——v4.1 诊断发现
          主页收割 0 token 的根因：主页卡片全是这种形态，旧路径白名单整批丢弃）
        统一重建为 /explore/{id}（SSR 详情通道已验证的路径），查询串原样保留；
        xsec_source 缺失/为空时按来源补默认值（token 与 source 配套校验，
        空/错 source 会 401：搜索卡片 pc_search、主页卡片 pc_user）。
        """
        out: dict[str, str] = {}
        for raw in links or []:
            link = str(raw)
            m = (re.search(r"/(?:explore|discovery/item|search_result)/([0-9a-f]{24})", link)
                 or re.search(r"/user/profile/[0-9a-f]{24}/([0-9a-f]{24})", link))
            t = re.search(r"[?&]xsec_token=([^&]+)", link)
            if not m or not t:
                continue
            nid = m.group(1)
            if nid in out:
                continue
            is_profile = "/user/profile/" in link
            query = link.split("?", 1)[1] if "?" in link else ""
            abs_link = f"https://www.xiaohongshu.com/explore/{nid}"
            if query:
                abs_link += "?" + query
            default_src = "pc_user" if is_profile else "pc_search"
            if abs_link.endswith("xsec_source="):
                abs_link += default_src
            elif "xsec_source=" not in abs_link:
                abs_link += f"&xsec_source={default_src}" if "?" in abs_link else f"?xsec_source={default_src}"
            out[nid] = abs_link
        return out

    def get_note_structured(self, url: str) -> dict:
        """贴链接即解析：单条笔记的结构化元数据（视频/图文通吃，秒级，不下载）。

        「快速预览」通道——完整生成（下载→转写→LLM）走 download()/download_subtitles()，
        本方法只做元数据抽取，供 /api/xhs/parse 与 harness 工具 parse_xhs_note 使用。
        红线边界：只取用户主动粘贴分享的这一条链接，不登录、不翻页、不批量。

        遵循 harness 失败契约：{success, reason, hint...}，绝不裸抛。
        """
        raw = (url or "").strip()
        if not raw:
            return {"success": False, "reason": "链接为空"}

        if "xiaohongshu.com" not in raw and "xhslink." not in raw:
            return {"success": False, "reason": "不是小红书链接", "url": raw[:120]}

        # 合集/专辑是批量通道，单条解析不收——给明确指引而不是笼统报错
        if "/collection/item/" in raw or "/board/" in raw:
            return {"success": False, "reason": "这是合集/专辑链接",
                    "url": raw[:120],
                    "hint": "合集请走批量总结（会逐条展开），单条解析只收笔记详情链接"}

        nid = self._extract_note_id(raw)
        if not nid and "xhslink." in raw:
            # 短链：先解析出真实链接再取 ID（解析失败不静默）
            try:
                raw = self._resolve_url(raw)
            except Exception as e:  # noqa: BLE001
                return {"success": False, "reason": f"短链解析失败: {str(e)[:120]}",
                        "url": raw[:120]}
            nid = self._extract_note_id(raw)
        if not nid:
            return {"success": False, "reason": "无法从链接解析笔记 ID",
                    "url": raw[:120],
                    "hint": "请从 App 分享面板复制链接（形如 xiaohongshu.com/explore/{id}?xsec_token=… 或 xhslink 短链）"}

        # 裸链接（无 token）只有被动捕获缓存通道可用；没有缓存就教学，不硬试
        if "xsec_token=" not in raw:
            cached = None
            try:
                from app.services.xhs_note_cache import XHSNoteCache
                cached = XHSNoteCache.get(nid)
            except Exception:
                cached = None
            if cached is None:
                return {"success": False, "reason": "链接缺少访问凭证（xsec_token）",
                        "note_id": nid, "url": raw[:120],
                        "hint": ("请从 App 分享面板复制链接（URL 会带 xsec_token= 参数）；"
                                 "或在桌面端浏览过该笔记后再贴裸链接（走被动捕获缓存）")}

        try:
            resolved = self._resolve_url(raw)
            note = self._get_note(resolved, nid)
        except BizException as e:
            # 主通道（SSR）被风控或登录态失效时，hellotik 第三方通道兜底：
            # 它的服务器替我们取内容，不消耗本机 cookie/风控额度（2026-10-24 协议逆向通道）
            fallback = self._hellotik_fallback(raw, nid, reason=str(e.message)[:120])
            if fallback is not None:
                return fallback
            return {"success": False, "reason": e.message[:200], "note_id": nid,
                    "err": e.err, "detail": e.detail or {},
                    "hint": "登录态失效时请在桌面端重新登录小红书（工具不会自动登录）"}
        except Exception as e:  # noqa: BLE001 —— 网络层任何异常都收敛为结构化失败
            fallback = self._hellotik_fallback(raw, nid, reason=str(e)[:120])
            if fallback is not None:
                return fallback
            return {"success": False, "reason": f"解析失败: {str(e)[:180]}", "note_id": nid}
        if not isinstance(note, dict):  # 双保险：_get_note 契约外的任何返回都挡住
            return {"success": False, "reason": "未取得笔记数据", "note_id": nid}

        video_url = self._extract_video_url(note)
        images = []
        for im in (note.get("imageList") or []):
            u = im.get("urlDefault") or im.get("url") or ""
            if u:
                images.append({"url": u,
                               "width": im.get("width"),
                               "height": im.get("height")})
        desc = note.get("desc") or ""
        title = (note.get("title") or (desc.split("\n")[0][:60] if desc else "") or "(无标题)")
        user = note.get("user") or {}
        interact = note.get("interactInfo") or {}
        return {
            "success": True,
            "note_id": nid,
            "note_type": "video" if video_url else ("normal" if images else "unknown"),
            "title": title[:120],
            "desc": desc[:2000],
            "tags": [t.get("name") for t in (note.get("tagList") or []) if t.get("name")],
            "author": {"nickname": user.get("nickName") or user.get("nickname") or "",
                       "user_id": user.get("userId", "")},
            "video_url": video_url,
            "images": images[:18],
            "likes": interact.get("likedCount"),
            "collected": interact.get("collectedCount"),
            "comments": interact.get("commentCount"),
            "ip_location": note.get("ipLocation", ""),
            "source_url": resolved,
            "next_step": ("视频笔记：走生成管线（下载→转写→洞察）"
                          if video_url else "图文笔记：正文+配图合成材料直接生成洞察"),
        }

    def _hellotik_fallback(self, url: str, nid: str, reason: str = "") -> "dict | None":
        """hellotik 第三方解析兜底（主通道失败时调用）。

        Playwright 驱动 hellotik 页面（真实 Chrome headed，窗口屏外），
        依赖本机 playwright；环境不可用时静默返回 None 走原失败路径。
        """
        try:
            from app.services.hellotik_api import parse_note_via_hellotik
            note = parse_note_via_hellotik(url)
        except Exception as e:  # noqa: BLE001 — 兜底失败不上抛
            logger.warning("hellotik 兜底异常: %s", str(e)[:150])
            return None
        if not note or not note.get("title"):
            return None
        logger.info("hellotik 兜底成功 note_id=%s（主通道原因: %s）", nid, reason[:80])
        return {
            "success": True,
            "note_id": nid,
            "note_type": note.get("note_type") or "unknown",
            "title": note.get("title", "")[:120],
            "desc": (note.get("desc") or "")[:2000],
            "tags": [],
            "author": {"nickname": "", "user_id": ""},
            "video_url": note.get("video_url") or "",
            "images": [{"url": u} for u in (note.get("images") or [])[:18]],
            "likes": None, "collected": None, "comments": None,
            "ip_location": "",
            "source_url": url,
            "via": "hellotik",
            "next_step": ("视频笔记：走生成管线（下载→转写→洞察）"
                          if note.get("video_url") else "图文笔记：正文+配图合成材料直接生成洞察"),
        }

    @staticmethod
    def _extract_video_url(note: dict) -> str:
        video = note.get("video") or {}
        streams = ((video.get("media") or {}).get("stream")) or {}
        for codec in ("h264", "h265", "av1"):
            for s in streams.get(codec) or []:
                master = s.get("masterUrl")
                if master:
                    return master
        raw = json.dumps(note, ensure_ascii=False)
        candidates = re.findall(
            r"https?://(?:sns-video[a-z0-9-]*|fe-video-qc)\.xhscdn\.com/[^\"\\<>\s]+", raw)
        return candidates[0] if candidates else ""

    @staticmethod
    def _download_file(url: str, dest: str) -> int:
        subprocess.run(
            ["curl", "-s", "-L", "--max-time", "600", "-A", UA, "-o", dest, url],
            check=False, timeout=620,
        )
        return os.path.getsize(dest) if os.path.exists(dest) else 0

    @staticmethod
    def _probe_duration(path: str) -> float:
        try:
            result = subprocess.run(
                [_bin("ffprobe"), "-v", "error", "-show_entries",
                 "format=duration", "-of", "csv=p=0", path],
                capture_output=True, text=True, timeout=30,
            )
            return float(result.stdout.strip())
        except Exception:
            return 0.0
