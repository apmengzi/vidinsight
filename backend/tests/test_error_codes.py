"""T11 标准化测试：错误码注册表 + BizException + 异常处理器 + 请求中间件。"""
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.exceptions.biz_exception import BizException
from app.exceptions.codes import ErrorCode, get_error_spec, validate_registry
from app.exceptions.exception_handlers import register_exception_handlers
from app.utils.log_standard import (
    JsonFormatter, log_event, register_request_middleware, request_id_var,
)


# ── 错误码注册表 ─────────────────────────────────────────────

def test_registry_unique_and_complete():
    """整数码无冲突、err 无重复、文案非空——注册表自身约束。"""
    assert validate_registry() == []


def test_error_specs_are_distinct():
    specs = [s for s in vars(ErrorCode).values()
             if isinstance(s, type(ErrorCode.INTERNAL))]
    assert len(specs) >= 6  # 注册表实际有内容，别忘配
    assert len({s.err for s in specs}) == len(specs)
    assert len({s.code for s in specs}) == len(specs)


def test_get_error_spec_lookup():
    assert get_error_spec("XHS_NOTE_NOT_FOUND") is ErrorCode.XHS_NOTE_NOT_FOUND
    assert get_error_spec("NOT_REGISTERED") is None


def test_biz_exception_carries_err_and_detail():
    spec = ErrorCode.XHS_COLLECTION_EMPTY
    exc = BizException(spec.code, "自定义文案", err=spec.err, detail={"note_id": "x"})
    assert exc.code == spec.code
    assert exc.message == "自定义文案"
    assert exc.err == "XHS_COLLECTION_EMPTY"
    assert exc.detail == {"note_id": "x"}
    # 旧式两参构造仍然兼容（err/detail 为 None）
    legacy = BizException(300101, "老调用")
    assert legacy.err is None and legacy.detail is None


# ── 异常处理器：err/detail 透传到响应 ────────────────────────

def _make_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/biz")
    def biz():
        spec = ErrorCode.XHS_PAGE_FETCH_FAILED
        raise BizException(spec.code, spec.message, err=spec.err,
                           detail={"url": "https://xhs.test/explore/1"})

    @app.get("/boom")
    def boom():
        raise RuntimeError("炸了")

    return app


def test_biz_exception_response_includes_err_and_detail():
    client = TestClient(_make_app(), raise_server_exceptions=False)
    resp = client.get("/biz")
    body = resp.json()
    spec = ErrorCode.XHS_PAGE_FETCH_FAILED
    assert body["code"] == spec.code
    assert body["msg"] == spec.message
    assert body["err"] == "XHS_PAGE_FETCH_FAILED"
    assert body["detail"]["url"].startswith("https://xhs.test")


def test_unhandled_exception_maps_to_internal():
    client = TestClient(_make_app(), raise_server_exceptions=False)
    body = client.get("/boom").json()
    assert body["code"] == ErrorCode.INTERNAL.code
    assert body["err"] == "INTERNAL"


# ── 结构化日志 ───────────────────────────────────────────────

def test_json_formatter_emits_event_fields_and_request_id():
    logger = logging.getLogger("test.jsonfmt")
    logger.setLevel(logging.INFO)
    record = logger.makeRecord(
        "test.jsonfmt", logging.INFO, __file__, 1,
        "collection.expand.done", (), None, extra={
            "event": "collection.expand.done", "total": 8, "platform": "xiaohongshu"})
    line = JsonFormatter().format(record)
    import json
    payload = json.loads(line)
    assert payload["event"] == "collection.expand.done"
    assert payload["total"] == 8
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.jsonfmt"


def test_log_event_writes_through_logger():
    logger = logging.getLogger("test.log_event")
    logger.setLevel(logging.INFO)
    seen = {}

    class Capture(logging.Handler):
        def emit(self, record):
            seen.update(record.__dict__)

    logger.addHandler(Capture())
    log_event(logger, "xhs.token.harvest", tokens=3, raw=5)
    assert seen["event"] == "xhs.token.harvest"
    assert seen["tokens"] == 3


# ── 请求中间件：request_id 注入 + 访问日志 ───────────────────

def _make_middleware_app() -> FastAPI:
    app = FastAPI()
    register_request_middleware(app)

    @app.get("/ping")
    def ping():
        return {"rid": request_id_var.get()}

    @app.get("/crash")
    def crash():
        raise RuntimeError("mid-crash")

    return app


def test_middleware_assigns_request_id():
    client = TestClient(_make_middleware_app(), raise_server_exceptions=False)
    resp = client.get("/ping")
    rid = resp.headers.get("X-Request-Id")
    assert rid, "响应缺少 X-Request-Id 头"
    assert resp.json()["rid"] == rid


def test_middleware_honors_incoming_request_id():
    client = TestClient(_make_middleware_app(), raise_server_exceptions=False)
    resp = client.get("/ping", headers={"X-Request-Id": "abc123def456"})
    assert resp.headers["X-Request-Id"] == "abc123def456"
    assert resp.json()["rid"] == "abc123def456"


def test_middleware_does_not_swallow_crash():
    """中间件只记日志不吞异常：RuntimeError 必须继续向上抛（交由异常处理器/服务器）。"""
    client = TestClient(_make_middleware_app())  # 默认 raise_server_exceptions=True
    with pytest.raises(RuntimeError):
        client.get("/crash")


def test_log_event_renames_reserved_field_names():
    """业务字段撞 LogRecord 标准属性时自动加 f_ 前缀，而不是让整条日志 KeyError。"""
    from app.utils.log_standard import _sanitize_fields
    safe = _sanitize_fields({"message": "x", "msg": "y", "exc_info": "z", "ok": 1})
    assert safe == {"f_message": "x", "f_msg": "y", "f_exc_info": "z", "ok": 1}


# ── LLM 供应商错误归类 ───────────────────────────────────────

def test_classify_llm_error_maps_gateway_failures():
    """504 整页 HTML / 超时文本都应翻译为 LLM_PROVIDER_TIMEOUT，而不是透传 HTML。"""
    from app.utils.llm_error import classify_llm_error
    html_504 = RuntimeError('<html><title>504 Gateway Time-out</title></html>')
    assert classify_llm_error(html_504) is ErrorCode.LLM_PROVIDER_TIMEOUT
    assert classify_llm_error(RuntimeError("Request timed out")) is ErrorCode.LLM_PROVIDER_TIMEOUT
    assert classify_llm_error(RuntimeError("502 Bad Gateway")) is ErrorCode.LLM_PROVIDER_TIMEOUT


def test_classify_llm_error_passes_through_unknown():
    from app.utils.llm_error import classify_llm_error
    assert classify_llm_error(RuntimeError("API key 无效")) is None
    assert classify_llm_error(ValueError("x")) is None


# ── 收割窗口信号中转 ─────────────────────────────────────────

def _signal_app():
    from app.routers.note import router
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


def test_harvest_signal_post_and_get_roundtrip():
    client = _signal_app()
    r = client.post("/api/xhs/harvest_signal", content=b'{"label":"win1","event":"done","data":{"count":8}}',
                    headers={"Content-Type": "text/plain"})
    assert r.status_code == 200
    got = client.get("/api/xhs/harvest_signal", params={"label": "win1"}).json()
    assert got["data"]["event"] == "done"
    assert got["data"]["data"]["count"] == 8


def test_harvest_signal_rejects_bad_label():
    client = _signal_app()
    r = client.post("/api/xhs/harvest_signal", content=b'{"label":"bad label;","event":"done"}',
                    headers={"Content-Type": "text/plain"})
    body = r.json()
    assert body["err"] == "HARVEST_SIGNAL_INVALID"


def test_harvest_signal_get_unknown_label_returns_null():
    client = _signal_app()
    got = client.get("/api/xhs/harvest_signal", params={"label": "ghost"}).json()
    assert got["data"] is None
