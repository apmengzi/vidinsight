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
    - **分段下载进度合并**（2026-11-03 修复）：B 站 DASH 是「视频轨 + 音频轨」
      两段下载，每段都会走一遍 0→100。旧实现每段重置 last，用户看到
      79% 突然跳回 0%（实测）。现在按段数累计：第 1 段映射到 0-50%，
      第 2 段映射到 50-100%，整体单调递增。
    - 回调内任何异常都不能冒泡进 yt-dlp（会把下载本身搞挂），
      唯一例外是协作式取消信号 TaskCancelled。
    """
    # 段数优先从 info_dict.requested_formats 读（DASH 合并下载时 yt-dlp 会带
    # 视频/音频两条格式）；读不到时先按单段处理，等真出现段内回退再修正。
    # 旧实现硬编码「2 段、第 1 段映射 0-50%」，单段下载会被无谓压缩到 0-50%。
    # 关键：**任何情况下都不允许进度回退**——重新映射后若比已报值低，
    # 就停在已报值上，直到真实进度追上来（用户看到的是"卡住"而非"倒退"）。
    state = {"seg": 0, "done": 0, "n_seg": None, "last": 0.0, "seg_last": 0.0}

    def _expected_segments(d) -> Optional[int]:
        info = d.get("info_dict") or {}
        rf = info.get("requested_formats")
        if isinstance(rf, (list, tuple)) and len(rf) > 0:
            return len(rf)
        return None

    def hook(d) -> None:
        try:
            status = d.get("status") if isinstance(d, dict) else None
            if state["n_seg"] is None:
                n = _expected_segments(d)
                if n:
                    state["n_seg"] = n

            if status == "finished":
                state["done"] += 1
                state["seg_last"] = 100.0
                n = state["n_seg"]
                # 段数未知：这段结束了但可能还有下一段，先不给 100
                overall = (100.0 * state["done"] / n) if n else 99.0
            elif status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes")
                if not total or downloaded is None:
                    return
                pct = clamp_pct(float(downloaded) / float(total) * 100.0)
                if pct is None:
                    return

                # 段内进度回退 = 进入了新的一段（yt-dlp 每段从 0 重新计）
                if pct + 1.0 < state["seg_last"]:
                    state["seg"] += 1
                    state["done"] = max(state["done"], state["seg"])
                    state["seg_last"] = 0.0
                    if state["n_seg"] is None:
                        # 实际是多段：按「已完成段 + 当前段」重新映射
                        state["n_seg"] = state["done"] + 1
                else:
                    state["seg_last"] = pct

                n = state["n_seg"] or 1
                overall = (state["done"] + pct / 100.0) * 100.0 / n
            else:
                return

            overall = clamp_pct(overall)
            if overall is None:
                return

            # 全局单调不回退：重新映射可能算出更低的值，停在已报值上
            if overall < state["last"]:
                overall = state["last"]
            else:
                state["last"] = overall

            if throttle is not None and not throttle.allow(key):
                return
            on_progress(overall)
        except TaskCancelled:
            # 协作式取消必须穿透 yt-dlp 冒泡到管线，不能被"进度失败"吞掉
            raise
        except Exception:  # noqa: BLE001 - 进度上报失败不能影响下载
            return

    return hook
