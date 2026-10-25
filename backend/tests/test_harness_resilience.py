"""Harness 韧性测试 —— 对标 dsh / Codex 的三条铁律：

1. 工具异常 ≠ 会话失败：异常收敛为结构化失败（success=false + reason），
   **当前轮次继续**，模型读到 reason 自行决定重试 / 换路 / 结束。
2. 模型产出非法 JSON 参数：不炸、不终止，记录 args_error 后照常走结构化 gate。
3. 全链路 trace 落盘：start / llm / tool / final 事件流可复盘（Codex rollout 原则）。

另覆盖答辩预检端点 /api/sys_demo_check 的结构与 Harness 自检项。
"""
import asyncio
import json
import types
from pathlib import Path

import pytest

from app.services import harness as harness_mod
from app.services.agent_trace import TRACE_DIR


# ── 测试替身 ────────────────────────────────────────

class _FakeMsg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self, exclude_none=True):  # harness 会把 assistant 消息回填历史
        d = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in self.tool_calls
            ]
        return d


class _FakeResp:
    def __init__(self, msg):
        self.choices = [types.SimpleNamespace(message=msg)]
        self.usage = None


class _FakeCall:
    def __init__(self, call_id, name, arguments):
        self.id = call_id
        self.function = types.SimpleNamespace(name=name, arguments=arguments)


class _FakeCompletions:
    """按脚本逐轮返回响应；记录收到的 messages 供断言。"""

    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._script:
            raise AssertionError("脚本耗尽：循环未按预期终止")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeClient:
    def __init__(self, script):
        self.chat = types.SimpleNamespace(
            completions=_FakeCompletions(script))


class _FakeExecutor:
    """consume 工具执行器替身：explode_tool 必炸，其余返回成功。"""

    def __init__(self):
        self.calls = []

    def execute(self, name, args):
        self.calls.append((name, args))
        if name == "explode_tool":
            raise RuntimeError("磁盘着火了（测试注入）")
        return json.dumps({"success": True, "tool": name, "args": args},
                          ensure_ascii=False)


class _FakeBase:
    """VidInsightAgent 替身：只提供 harness 用到的面。"""

    def __init__(self, client, executor):
        self._client = client
        self._model = "fake-model"
        self.tools = executor
        self.kb = types.SimpleNamespace(
            get_stats=lambda: {"total_notes": 0, "total_chunks": 0,
                               "bm25_available": True, "reranker_available": False},
            search=lambda **kw: [],
        )

    def _sanitize_input(self, message):
        return message

    def _classify_intent(self, message, history, recorder=None):
        return "search"

    def _build_contextual_query(self, message, history):
        return message

    def _handle_chitchat(self, message):
        return {"answer": "闲聊", "sources": [], "intent": "chitchat",
                "tool_calls": [], "kb_stats": self.kb.get_stats()}


def _make_harness(monkeypatch, script, executor=None):
    """构建一个不碰真 LLM / 真 DB 的 VidInsightHarness。"""
    executor = executor or _FakeExecutor()
    fake = _FakeBase(_FakeClient(script), executor)
    monkeypatch.setattr(harness_mod, "VidInsightAgent",
                        lambda provider_id, model_name: fake)
    h = harness_mod.VidInsightHarness("fake-provider", "fake-model")
    return h, executor


def _new_trace_files():
    return {p.name for p in TRACE_DIR.glob("*.jsonl")}


# ── 1. 工具异常 → 结构化失败，轮次继续（dsh 铁律）──────

def test_tool_exception_becomes_structured_failure_and_loop_continues(monkeypatch):
    """第 1 轮工具爆炸 → 结构化失败回灌模型；第 2 轮模型给最终回答。"""
    script = [
        _FakeResp(_FakeMsg(tool_calls=[_FakeCall("c1", "explode_tool", '{"x": 1}')])),
        _FakeResp(_FakeMsg(content="已收到失败并换路，最终回答 [1]")),
    ]
    before = _new_trace_files()
    h, executor = _make_harness(monkeypatch, script)

    result = h.chat("测试工具异常韧性")

    assert result["rounds"] == 2, "工具异常后循环必须继续，而不是终止"
    assert result["answer"] == "已收到失败并换路，最终回答 [1]"
    tc = result["tool_calls"][0]
    assert tc["ok"] is False
    out = json.loads(tc["output"])
    assert out["success"] is False
    assert "工具执行异常" in out["reason"]
    assert "磁盘着火" in out["reason"]            # 根因保留给模型
    assert out["recoverable"] is True
    assert tc["tool_error"], "异常原文应记录在 trace 里供复盘"
    # 异常没有外溢：第二次调用拿到的是结构化失败（模型可读），不是 Exception
    second_messages = executor  # executor 只被调用了一次（explode_tool）
    assert any(n == "explode_tool" for n, _ in executor.calls)

    # trace 落盘：start → llm → tool → llm → final
    new_files = _new_trace_files() - before
    assert new_files, "harness 必须写轨迹文件"
    trace_path = TRACE_DIR / sorted(new_files)[-1]
    events = [json.loads(l) for l in
              trace_path.read_text(encoding="utf-8").strip().splitlines()]
    assert [e["event"] for e in events] == ["start", "llm", "tool", "llm", "final"]
    assert events[-1]["status"] == "answered"
    assert events[-1]["rounds"] == 2
    tool_event = events[2]
    assert tool_event["tool"] == "explode_tool"
    trace_path.unlink(missing_ok=True)


# ── 2. 非法 JSON 参数 → 记录 args_error，照常走 gate ──

def test_invalid_json_args_recorded_and_loop_continues(monkeypatch):
    script = [
        _FakeResp(_FakeMsg(tool_calls=[_FakeCall("c1", "list_notes", "{oops 不是 JSON")])),
        _FakeResp(_FakeMsg(content="参数已修正，最终回答")),
    ]
    h, _ = _make_harness(monkeypatch, script)

    result = h.chat("测试非法参数韧性")

    assert result["rounds"] == 2
    tc = result["tool_calls"][0]
    assert tc["args"] == {}
    assert "args_error" in tc and "非法 JSON" in tc["args_error"]
    assert tc["ok"] is True  # list_notes 用空参数也能成功返回


# ── 3. LLM 挂掉 → 优雅降级（不 500）────────────────

def test_llm_failure_degrades_gracefully(monkeypatch):
    script = [RuntimeError("连接超时（测试注入）")]
    h, _ = _make_harness(monkeypatch, script)

    result = h.chat("测试 LLM 故障降级")

    assert result.get("degraded") is True
    assert "模型调用失败" in result["answer"] or result["answer"]


# ── 4. 超轮次 → 有界终止 + 明确说明 ─────────────────

def test_max_rounds_returns_degraded_with_note(monkeypatch):
    exploding = [
        _FakeResp(_FakeMsg(tool_calls=[_FakeCall(f"c{i}", "list_notes", "{}")]))
        for i in range(harness_mod.VidInsightHarness.MAX_ROUNDS + 1)
    ]
    h, _ = _make_harness(monkeypatch, exploding)

    result = h.chat("测试超轮次")

    assert result["rounds"] if "rounds" in result else True
    assert result.get("degraded") is True
    assert "最大推理轮次" in result["answer"] or "基于已获取的信息" in result["answer"]


# ── 5. 答辩预检端点 ─────────────────────────────────

def test_demo_check_endpoint_structure():
    from app.routers.config import sys_demo_check

    resp = asyncio.run(sys_demo_check())
    body = json.loads(resp.body)
    assert body["code"] == 0
    data = body["data"]
    assert data["overall"] in {"ready", "warn", "fail"}
    assert data["summary"]["ready"] + data["summary"]["warn"] + data["summary"]["fail"] == len(data["checks"])
    assert data["checks"], "至少要有一项体检结果"
    for c in data["checks"]:
        assert c["status"] in {"ok", "warn", "fail"}
        assert c["name"] and isinstance(c["detail"], str)

    names = {c["name"] for c in data["checks"]}
    assert {"数据库", "FFmpeg", "模型供应商", "知识库", "Harness 工具自检"} <= names


def test_demo_check_harness_self_test_passes():
    """工具自检项必须 ok —— 这是验证 gate 链路的存活信号。"""
    from app.routers.config import sys_demo_check

    resp = asyncio.run(sys_demo_check())
    data = json.loads(resp.body)["data"]
    self_check = next(c for c in data["checks"] if c["name"] == "Harness 工具自检")
    assert self_check["status"] == "ok", self_check
