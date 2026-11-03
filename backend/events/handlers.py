"""转写完成后的临时文件清理。

2026-11-03 改动：视频文件**不再删除**，改为归档到笔记产物目录。

为什么：用户希望在 VidInsight 里直接回看原片（离线也能看），
像刷小红书一样点开笔记就能播放。原逻辑在转写完成后把
`{video_id}.mp4` 一起删掉，导致笔记里永远没有视频可播。

现在的策略：
- 视频 → 归档到 `note_results/{task_id}/video.mp4`（跟着笔记走，
  删笔记时才删视频），并用 faststart 重封装支持边下边播
- 音频、字幕、缩略图等临时文件 → 照旧删除
- 磁盘保护：归档前检查总占用，超过上限时按最旧优先淘汰
"""
import os
import shutil
import subprocess
from pathlib import Path

from app.utils.logger import get_logger

logger = get_logger(__name__)

# 视频归档总容量上限（默认 20GB）。超了按「最久未访问」淘汰旧视频，
# 但**保留笔记本身**——只删视频文件，笔记和关键帧图还在。
MAX_ARCHIVE_BYTES = int(os.getenv("VIDEO_ARCHIVE_MAX_BYTES", str(20 * 1024**3)))

_VIDEO_EXTS = (".mp4", ".webm", ".mkv", ".flv", ".m4v")


def _archive_dir_for(task_id: str) -> Path:
    """笔记产物目录下的视频位置。与 note_results/{task_id}.json 同级。"""
    from app.utils.path_helper import get_runtime_path
    root = Path(get_runtime_path("note_results"))
    d = root / task_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _faststart(src: Path, dst: Path) -> bool:
    """重封装为 faststart（moov 前置），浏览器/WebView 才能边下边播。

    直接 copy 不重新编码，几秒完成；失败则退化为普通复制。
    """
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-c", "copy",
             "-movflags", "+faststart", str(dst)],
            check=True, capture_output=True, timeout=300,
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"faststart 重封装失败，改用直接复制: {str(e)[:80]}")
        try:
            shutil.copy2(src, dst)
            return True
        except Exception:
            return False


def _enforce_quota(keep_task_id: str) -> None:
    """归档总量超限时，从最旧开始删视频（只删 video.mp4，保留笔记与关键帧图）。"""
    try:
        from app.utils.path_helper import get_runtime_path
        root = Path(get_runtime_path("note_results"))
        vids = []
        total = 0
        for p in root.glob("*/video.mp4"):
            try:
                sz = p.stat().st_size
                total += sz
                vids.append((p.stat().st_mtime, sz, p))
            except OSError:
                continue
        if total <= MAX_ARCHIVE_BYTES:
            return
        vids.sort()  # 最旧优先
        for mtime, sz, p in vids:
            if total <= MAX_ARCHIVE_BYTES:
                break
            if p.parent.name == keep_task_id:
                continue  # 当前任务刚归档的不删
            try:
                p.unlink()
                total -= sz
                logger.info(f"[video-archive] 配额淘汰旧视频: {p.parent.name}")
            except OSError:
                pass
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[video-archive] 配额检查失败: {str(e)[:80]}")


def archive_video(src_path: str, task_id: str) -> str | None:
    """把已下载的视频归档到笔记目录，返回归档后的路径（失败返回 None）。"""
    try:
        src = Path(src_path)
        if not src.exists() or src.suffix.lower() not in _VIDEO_EXTS:
            return None
        dst = _archive_dir_for(task_id) / "video.mp4"
        if dst.exists() and dst.stat().st_size > 100_000:
            return str(dst)  # 幂等
        if not _faststart(src, dst):
            return None
        _enforce_quota(task_id)
        logger.info(f"[video-archive] 视频已归档: {dst} "
                    f"({dst.stat().st_size / 1024**2:.0f}MB)")
        return str(dst)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[video-archive] 归档失败: {str(e)[:120]}")
        return None


def cleanup_temp_files(data):
    """转写完成后清理临时文件。视频走归档，其余删除。"""
    logger.info(f"starting cleanup temp files ：{data['file_path']}")
    file_path = data.get("file_path") or ""
    if not file_path or not os.path.exists(file_path):
        logger.warning(f"路径不存在：{file_path}")
        return

    dir_path = os.path.dirname(file_path)
    base_name = os.path.basename(file_path)
    video_id, _ = os.path.splitext(base_name)

    # 先尝试归档视频（如果有 task_id 且目录里有视频文件）
    task_id = data.get("task_id") or ""
    if task_id:
        for ext in _VIDEO_EXTS:
            cand = os.path.join(dir_path, f"{video_id}{ext}")
            if os.path.exists(cand):
                archived = archive_video(cand, task_id)
                if archived:
                    # 归档成功后删原文件（省一份磁盘）
                    try:
                        os.remove(cand)
                        logger.info(f"原视频已归档并删除源文件：{cand}")
                    except OSError:
                        pass
                break

    logger.info(f"开始清理 video_id={video_id} 的临时文件")
    for file in os.listdir(dir_path):
        if not file.startswith(video_id):
            continue
        # 视频已处理过，跳过
        if Path(file).suffix.lower() in _VIDEO_EXTS:
            continue
        full_path = os.path.join(dir_path, file)
        try:
            os.remove(full_path)
            logger.info(f"删除文件：{full_path}")
        except Exception as e:
            logger.error(f"删除失败：{full_path}，原因：{e}")
