"""长任务取消注册表。

设计要点（面试常问）：
- 取消是「协作式」的：设置标志后，任务只能在阶段边界 / LLM 轮次边界响应，
  无法打断已经在飞行中的单次 LLM 调用（HTTP 请求本身不可中断）。
- 标志保存在进程内存（set + 锁）。单机桌面部署没有跨进程取消需求，
  若上分布式需要换成 Redis 等共享存储。
- clear 由任务终态清理时调用，防止集合无限增长。
"""
import threading


class TaskCancelled(Exception):
    """任务被用户取消，在阶段边界抛出。"""

    def __init__(self, task_id: str | None = None):
        self.task_id = task_id
        super().__init__(f"task cancelled: {task_id}")


_lock = threading.Lock()
_cancelled: set[str] = set()


def cancel_task(task_id: str) -> None:
    """标记任务为已取消（API 层调用）。"""
    if not task_id:
        return
    with _lock:
        _cancelled.add(task_id)


def is_cancelled(task_id: str | None) -> bool:
    if not task_id:
        return False
    with _lock:
        return task_id in _cancelled


def clear_task(task_id: str | None) -> None:
    """任务进入终态后清除标志，防止集合无限增长。"""
    if not task_id:
        return
    with _lock:
        _cancelled.discard(task_id)


def check_cancelled(task_id: str | None) -> None:
    """阶段边界检查：已取消则抛 TaskCancelled。"""
    if is_cancelled(task_id):
        raise TaskCancelled(task_id)
