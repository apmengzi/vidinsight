"""WorkBuddy 免费模型池接入测试。

两层：
- 单元测试：注入 fake inner client，验证并发闸门 ≤2、429/503 指数退避与
  5 次重试预算、非重试状态快速失败、流名额归还、失败计数与日志（零真网）
- 集成测试：真打本地端点 127.0.0.1:7864（端点不在则 skip），
  非流式与流式各一发；该端点同时供生产流水线使用，测试用最小 prompt
"""
import gc
import threading
import time
from types import SimpleNamespace

import httpx
import openai
import pytest

from app.gpt.workbuddy_provider import (
    GLOBAL_CONCURRENCY,
    MAX_RETRIES,
    _backoff_delay,
    is_workbuddy_provider,
    WorkBuddyClient,
    WorkBuddyPoolBusy,
    WorkBuddyPoolExhausted,
    get_workbuddy_pool_stats,
)

WB_BASE = "http://127.0.0.1:7864/v1"
WB_KEY = "wb2api-local"
WB_MODEL = "deepseek-v4.1-flash"


# ── 工具 ──────────────────────────────────────

def _status_err(code: int) -> openai.APIStatusError:
    req = httpx.Request("POST", f"{WB_BASE}/chat/completions")
    resp = httpx.Response(code, request=req)
    cls = openai.RateLimitError if code == 429 else openai.InternalServerError
    return cls(f"error code: {code}", response=resp, body=None)


class _FakeInner:
    """按脚本回放的假上游：'ok' / int(状态码) / 异常实例 / 自定义返回值。"""

    def __init__(self, script=None, hook=None):
        self.script = list(script or [])
        self.hook = hook
        self.calls = 0
        self.models = SimpleNamespace(list=lambda: [])
        chat = SimpleNamespace(completions=self)
        self.chat = chat

    def create(self, *, model, messages, **kwargs):
        self.calls += 1
        if self.hook:
            self.hook()
        action = self.script.pop(0) if self.script else "ok"
        if action == "ok":
            return object()  # 哨兵响应
        if isinstance(action, int):
            raise _status_err(action)
        if isinstance(action, Exception):
            raise action
        return action


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """单测零等待：退避 sleep 直接跳过（真网络调用不经过这里）。"""
    monkeypatch.setattr(time, "sleep", lambda s: None)


def _client_with(script=None, hook=None) -> WorkBuddyClient:
    return WorkBuddyClient(inner=_FakeInner(script, hook=hook))


# ── 退避策略 ──────────────────────────────────

def test_backoff_delay_bounds():
    for attempt in range(8):
        d = _backoff_delay(attempt)
        base = min(5 * (2 ** attempt), 120)
        assert base * 0.9 <= d <= base * 1.1


def test_retries_429_then_succeeds():
    c = _client_with([429, 429, "ok"])
    resp = c.chat.completions.create(model=WB_MODEL, messages=[{"role": "user", "content": "hi"}])
    assert resp is not None
    snap = get_workbuddy_pool_stats()
    assert snap["retries"] == 2 and snap["e429"] == 2
    assert snap["consecutive_failures"] == 0  # 成功后清零


def test_gives_up_after_retry_budget():
    c = _client_with([503] * (MAX_RETRIES + 2))
    with pytest.raises(WorkBuddyPoolExhausted) as ei:
        c.chat.completions.create(model=WB_MODEL, messages=[{"role": "user", "content": "hi"}])
    # 初始 1 次 + 重试 5 次 = 6 次上游调用，绝不多打
    assert c._inner.calls == MAX_RETRIES + 1
    assert ei.value.attempts == MAX_RETRIES + 1
    assert get_workbuddy_pool_stats()["gave_up"] == 1


def test_exhausted_error_not_outer_retryable():
    """str 不含 UniversalGPT 可重试 token，避免外层再套重试放大流量。"""
    e = WorkBuddyPoolExhausted(6, 155.0)
    raw = str(e).lower()
    for token in ("error code: 429", "error code: 503", "rate limit",
                  "service unavailable", "timeout", "timed out"):
        assert token not in raw
    assert getattr(e, "status_code", None) is None


def test_no_retry_on_500_and_auth():
    c = _client_with([500])
    with pytest.raises(openai.APIStatusError):
        c.chat.completions.create(model=WB_MODEL, messages=[])
    assert c._inner.calls == 1  # 500 不在白名单，原样抛出

    c2 = _client_with([_status_err(401)])
    with pytest.raises(openai.APIStatusError):
        c2.chat.completions.create(model=WB_MODEL, messages=[])
    assert c2._inner.calls == 1  # 认证错快速失败


# ── 并发闸门 ──────────────────────────────────

def test_global_concurrency_cap():
    """确定性验证：恰好 2 个线程能同时进入上游（≤2 且能到 2），其余排队。"""
    entered = threading.Semaphore(0)
    go = threading.Event()
    lock = threading.Lock()
    state = {"cur": 0, "peak": 0}

    def hook():
        with lock:
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
        entered.release()
        go.wait(10)
        with lock:
            state["cur"] -= 1

    c = _client_with(["ok"] * 8, hook=hook)
    done = []

    def worker():
        c.chat.completions.create(model=WB_MODEL, messages=[])
        done.append(True)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    try:
        for _ in range(2):
            assert entered.acquire(timeout=10), "上游并发不足 2，闸门过紧"
        with lock:
            assert state["cur"] == 2, "并发闸门应放行恰好 2 个"
    finally:
        go.set()
    for t in threads:
        t.join(timeout=30)
    assert len(done) == 6       # 全部完成（排队生效，没有饿死）
    assert state["peak"] <= GLOBAL_CONCURRENCY


def test_stream_holds_then_releases_slot():
    c = _client_with([])
    c._inner.create = lambda **kw: iter(["a", "b"])
    stream = c.chat.completions.create(model=WB_MODEL, messages=[], stream=True)
    assert list(stream) == ["a", "b"]
    # 耗尽后并发名额归还
    assert c._sem._value == GLOBAL_CONCURRENCY


def test_stream_abandoned_releases_slot_on_gc():
    c = _client_with([])
    c._inner.create = lambda **kw: iter(["x", "y"])
    stream = c.chat.completions.create(model=WB_MODEL, messages=[], stream=True)
    next(iter(stream))  # 只取一个就抛弃（模拟调用方 break）
    del stream
    gc.collect()
    assert c._sem._value == GLOBAL_CONCURRENCY


def test_queue_timeout_raises_busy():
    c = _client_with(["ok"])
    monkey_busy = SimpleNamespace(acquire=lambda timeout=None: False)
    c._sem = monkey_busy
    with pytest.raises(WorkBuddyPoolBusy):
        c.chat.completions.create(model=WB_MODEL, messages=[])


def test_failure_counters_and_log(caplog):
    c = _client_with([503, "ok"])
    with caplog.at_level("ERROR", logger="app.gpt.workbuddy_provider"):
        c.chat.completions.create(model=WB_MODEL, messages=[])
    snap = get_workbuddy_pool_stats()
    assert snap["e503"] == 1 and snap["retries"] == 1
    assert any("[WorkBuddy]" in r.getMessage() and "退避重试" in r.getMessage()
               for r in caplog.records)


def test_provider_detection():
    assert is_workbuddy_provider("workbuddy", "")
    assert is_workbuddy_provider("built-in", "http://127.0.0.1:7864/v1")
    assert not is_workbuddy_provider("built-in", "http://127.0.0.1:18766/v1")


# ── 集成测试（真端点；端点不在则跳过） ────────────

def _endpoint_up() -> bool:
    """真实可用性探测：/models 通只代表路由活着；号池可能 pool_empty。
    发一次 max_tokens=1 的最小补全（20s 超时），失败即视为池子当前不可用，
    集成测试跳过而非误报失败（上游资源问题不是被测代码的回归）。"""
    try:
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=3.0), trust_env=False) as hc:
            r = hc.post(
                f"{WB_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {WB_KEY}"},
                json={"model": WB_MODEL, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1},
            )
            return r.status_code == 200
    except Exception:
        return False


@pytest.mark.integration
def test_integration_nonstream_roundtrip(monkeypatch):
    if not _endpoint_up():
        pytest.skip("WorkBuddy 本地端点未启动（127.0.0.1:7864）")
    # 思考型模型夜间首字常超 60s（池子间歇慢），测试对齐演示配置的 150s 容差
    monkeypatch.setenv("WORKBUDDY_FIRST_BYTE_TIMEOUT", "150")
    c = WorkBuddyClient()
    resp = c.chat.completions.create(
        model=WB_MODEL,
        messages=[{"role": "user", "content": "只回复两个字母：pong"}],
        max_tokens=256,
    )
    msg = resp.choices[0].message
    text = (getattr(msg, "content", None) or getattr(msg, "reasoning_content", "") or "").strip()
    assert text, "非流式返回为空"


@pytest.mark.integration
def test_integration_stream_roundtrip(monkeypatch):
    if not _endpoint_up():
        pytest.skip("WorkBuddy 本地端点未启动（127.0.0.1:7864）")
    monkeypatch.setenv("WORKBUDDY_FIRST_BYTE_TIMEOUT", "150")
    c = WorkBuddyClient()
    stream = c.chat.completions.create(
        model=WB_MODEL,
        messages=[{"role": "user", "content": "只回复两个字母：pong"}],
        max_tokens=256,
        stream=True,
    )
    parts = []
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        text = getattr(delta, "content", None) or getattr(delta, "reasoning_content", None)
        if text:
            parts.append(text)
    assert parts, "流式未收到任何增量"
    assert "".join(parts).strip(), "流式拼接内容为空"
    # 流耗尽后并发名额已归还
    assert c._sem._value == GLOBAL_CONCURRENCY
