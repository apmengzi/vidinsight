"""Agent 轨迹记录（T4 可观测）：每个 ReAct 请求一份 JSONL，一行一个事件。

事件类型：
  start  {question, intent, mode}
  llm    {round, ms, prompt_tokens, completion_tokens, note}   # 意图分类记 note="intent"
  tool   {round, tool, args, result, ms}                        # 含预检索 pre_retrieval
  final  {status, answer_len, sources, rounds, total_ms, prompt_tokens, completion_tokens}

四列 AgentOps 的数据源：延迟/成本两列直接来自这里（T1 评测消费）。
设计约束：**记录失败绝不影响对话主流程**——所有写文件操作吞异常只打 warning。
文件位置：{runtime}/traces/{时间戳}_{session8}.jsonl，只追加、每行即落盘。
"""
import json
import time
import uuid
from pathlib import Path
from typing import Optional

from app.utils.logger import get_logger
from app.utils.path_helper import get_runtime_path

logger = get_logger(__name__)

try:
    TRACE_DIR = Path(get_runtime_path("traces"))
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
except Exception:  # 运行目录不可写等极端情况：降级为内存目录，绝不炸主流程
    import tempfile
    TRACE_DIR = Path(tempfile.gettempdir()) / "vidinsight_traces"
    TRACE_DIR.mkdir(parents=True, exist_ok=True)


def _usage_of(resp) -> tuple[int, int]:
    """从 OpenAI 兼容响应取 token 用量；供应商不给 usage 时安全归零。"""
    try:
        u = getattr(resp, "usage", None)
        if u is None:
            return 0, 0
        return int(getattr(u, "prompt_tokens", 0) or 0), int(getattr(u, "completion_tokens", 0) or 0)
    except Exception:
        return 0, 0


class AgentTraceRecorder:
    """一次 chat 请求的轨迹记录器。用法：start → llm_round/tool_round×N → final。"""

    def __init__(self, session_id: str = ""):
        self.session8 = (session_id or uuid.uuid4().hex)[:8]
        self.path = TRACE_DIR / f"{time.strftime('%Y%m%d_%H%M%S')}_{self.session8}.jsonl"
        self._t0 = time.perf_counter()
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.rounds = 0

    def write(self, event: str, **fields) -> None:
        try:
            rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event,
                   "session": self.session8, **fields}
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except Exception as e:
            logger.warning(f"trace 写入失败（忽略）: {e}")

    def start(self, question: str, intent: str, mode: str) -> None:
        self.write("start", question=question[:200], intent=intent, mode=mode)

    def llm_round(self, round_i: int, ms: float, resp=None, note: str = "") -> None:
        pt, ct = _usage_of(resp)
        self.prompt_tokens += pt
        self.completion_tokens += ct
        self.rounds = max(self.rounds, round_i)
        self.write("llm", round=round_i, ms=round(ms, 1), prompt_tokens=pt,
                   completion_tokens=ct, note=note)

    def tool_round(self, round_i: int, tool: str, args: Optional[dict],
                   result_summary: str, ms: float) -> None:
        self.write("tool", round=round_i, tool=tool, args=args,
                   result=str(result_summary)[:300], ms=round(ms, 1))

    def final(self, status: str, answer_len: int, sources: int) -> None:
        self.write("final", status=status, answer_len=answer_len, sources=sources,
                   rounds=self.rounds, total_ms=round((time.perf_counter() - self._t0) * 1000, 1),
                   prompt_tokens=self.prompt_tokens, completion_tokens=self.completion_tokens)
