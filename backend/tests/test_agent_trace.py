"""T4 轨迹记录与只读接口测试。"""
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.agent_trace import AgentTraceRecorder, TRACE_DIR


def _recorder() -> AgentTraceRecorder:
    return AgentTraceRecorder(session_id="testsess0001")


def test_recorder_writes_parseable_event_stream():
    rec = _recorder()
    rec.start("哪个博主讲了定价？", intent="search", mode="search")
    rec.tool_round(0, "pre_retrieval", {"query": "定价"},
                   "5 条", 123.4)
    rec.llm_round(0, 88.8, None, note="intent")  # usage=None 必须安全归零
    rec.llm_round(1, 1500.2, None)
    rec.tool_round(1, "search_kb", {"query": "定价策略"}, "[1] 定价相关片段…", 42.0)
    rec.final("answered", 356, 5)

    lines = rec.path.read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(l) for l in lines]
    assert [e["event"] for e in events] == [
        "start", "tool", "llm", "llm", "tool", "final"]
    final = events[-1]
    assert final["status"] == "answered"
    assert final["rounds"] == 1
    assert final["total_ms"] >= 0
    assert final["prompt_tokens"] == 0  # usage=None 归零，不炸
    rec.path.unlink(missing_ok=True)


def test_recorder_never_raises_on_broken_path():
    """写文件失败只打 warning，绝不上抛（对话主流程保护）。"""
    rec = _recorder()
    rec.path = "Z:/definitely/not/a/real/dir/trace.jsonl"  # 非法路径
    rec.start("q", "search", "search")  # 不应抛
    rec.final("answered", 1, 0)          # 不应抛


def test_trace_router_list_and_read():
    from app.routers.trace import router
    rec = _recorder()
    rec.start("测试问题", "search", "search")
    rec.final("answered", 10, 1)

    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)

    listing = client.get("/api/agent/traces").json()
    assert listing["code"] == 0
    names = [t["name"] for t in listing["data"]["traces"]]
    assert rec.path.name in names

    body = client.get(f"/api/agent/trace/{rec.path.name}").json()
    assert body["code"] == 0
    events = body["data"]["events"]
    assert events[0]["event"] == "start"
    assert events[-1]["event"] == "final"
    rec.path.unlink(missing_ok=True)


def test_trace_router_rejects_bad_name():
    from app.routers.trace import router
    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)
    # 路由可匹配但文件名正则拒绝
    body = client.get("/api/agent/trace/$$evil.jsonl").json()
    assert body["err"] == "TRACE_NAME_INVALID"
    # 路径穿越（编码斜杠）要么 404 要么被拒，绝不能 500 或读文件
    r = client.get("/api/agent/trace/..%2F..%2Fevil.jsonl")
    assert r.status_code in (404, 400)
    assert TRACE_DIR.exists()
