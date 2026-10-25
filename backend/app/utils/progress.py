"""阶段内进度上报工具（2026-10-19 进度可见性改造）。

三个高频调用点共用同一套节流与取值约定：
- 下载：yt-dlp 的 ``progress_hooks`` → :func:`make_ytdlp_progress_hook`
- 转写：faster-whisper 的 ``on_progress``（whisper.py 内部已按 1s 节流）
- LLM：分块循环的 i/N

约定：进度一律是 **当前阶段内** 的 0-100 百分比；拿不到总量时返回 None，
由 ``NoteGenerator._update_status`` 写成 ``stage_progress: null``，绝不猜假值。
"""
import threading
import time
from typing import Callable, Optional

from app.services.task_cancel import TaskCancelled

DEFAULT_MIN_INTERVAL_S = 1.0


def clamp_pct(value) -> Optional[float]:
    """把任意输入折成 0-100 的 float；无法解释为有限数值时返回 None。"""
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return None
    if pct != pct or pct in (float("inf"), float("-inf")):
        return None
    return round(max(0.0, min(100.0, pct)), 1)


class ProgressThrottle:
    """按 key 节流的放行器（线程安全，每个 key 在窗口内最多放行一次）。

    yt-dlp 的回调频率可达每秒数十次，直接落盘会把状态文件写成 IO 瓶颈；
    这里只做"是否放行"判断，真正的写盘仍由调用方完成。
    """

    def __init__(self, min_interval: float = DEFAULT_MIN_INTERVAL_S,
                 clock: Optional[Callable[[], float]] = None):
        self._min_interval = max(0.0, float(min_interval))
        self._clock = clock or time.monotonic
        self._last: dict = {}
        self._lock = threading.Lock()

    def allow(self, key: str = "default") -> bool:
        now = self._clock()
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < self._min_interval:
                return False
            self._last[key] = now
            return True

    def reset(self) -> None:
        with self._lock:
            self._last.clear()


def build_ytdlp_opts_with_progress(ydl_opts: dict, reporter: Optional[Callable[[float], None]],
                                   throttle: Optional[ProgressThrottle] = None,
                                   key: str = "download") -> dict:
    """返回带 progress_hooks 的 yt-dlp opts 副本（不修改调用方字典）。"""
    if reporter is None:
        return ydl_opts
    opts = dict(ydl_opts)
    existing = list(opts.get("progress_hooks") or [])
    existing.append(make_ytdlp_progress_hook(reporter, throttle=throttle, key=key))
    opts["progress_hooks"] = existing
    return opts


def make_ytdlp_progress_hook(on_progress: Callable[[float], None],
                             throttle: Optional[ProgressThrottle] = None,
                             key: str = "download") -> Callable[[dict], None]:
    """构造 yt-dlp ``progress_hooks`` 回调：downloaded/total → 0-100。

    - ``total_bytes`` 缺失时退回 ``total_bytes_estimate``；两者都没有则不上报
      （分段/直播类下载没有可靠总量，宁可写 null 也不编一个百分比）。
    - 视频+音频分段下载会让百分比回退，这里按"单调不回退"处理。
    - 回调内任何异常都不能冒泡进 yt-dlp（会把下载本身搞挂），
      唯一例外是协作式取消信号 TaskCancelled。
    """
    state = {"last": 0.0}

    def hook(d) -> None:
        try:
            status = d.get("status") if isinstance(d, dict) else None
            if status == "finished":
                pct = 100.0
            elif status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes")
                if not total or downloaded is None:
                    return
                pct = float(downloaded) / float(total) * 100.0
            else:
                return

            pct = clamp_pct(pct)
            if pct is None:
                return
            if pct < state["last"]:
                pct = state["last"]
            else:
                state["last"] = pct

            if throttle is not None and not throttle.allow(key):
                return
            on_progress(pct)
        except TaskCancelled:
            # 协作式取消必须穿透 yt-dlp 冒泡到管线，不能被"进度失败"吞掉
            raise
        except Exception:  # noqa: BLE001 - 进度上报失败不能影响下载
            return

    return hook
