"""高阶意图路由器（T7）单元测试。

只测纯逻辑：JSON 稳健提取、结果归一化校验、LRU 缓存行为。
不发起任何真实 LLM 调用。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.intent_router import (  # noqa: E402
    extract_json,
    normalize_result,
    _cache_get,
    _cache_put,
    _CACHE_TTL,
)


# ====== extract_json ======

def test_extract_json_plain():
    assert extract_json('{"intent": "parse", "links": [], "reason": "ok"}')["intent"] == "parse"


def test_extract_json_fenced():
    text = '```json\n{"intent": "ask", "links": [], "reason": "提问"}\n```'
    assert extract_json(text)["intent"] == "ask"


def test_extract_json_with_surrounding_text():
    text = '好的，我的判断如下：{"intent": "chitchat", "links": [], "reason": "闲聊"} 希望有帮助'
    assert extract_json(text)["intent"] == "chitchat"


def test_extract_json_single_quotes_fallback():
    text = "{'intent': 'parse', 'links': [], 'reason': 'r'}"
    assert extract_json(text)["intent"] == "parse"


def test_extract_json_invalid_returns_none():
    assert extract_json("完全不是 JSON 的回复") is None
    assert extract_json("") is None
    assert extract_json(None) is None


def test_extract_json_multiline():
    text = '一些前缀 {\n  "intent": "compare",\n  "links": ["https://x"],\n  "reason": "对比"\n} 后缀'
    result = extract_json(text)
    assert result["intent"] == "compare"
    assert result["links"] == ["https://x"]


# ====== normalize_result ======

ALLOWED = ["https://a.com/1", "https://b.com/2"]


def test_normalize_valid():
    result = normalize_result(
        {"intent": "parse", "links": ALLOWED, "reason": "r"}, ALLOWED)
    assert result["intent"] == "parse"
    assert result["links"] == ALLOWED


def test_normalize_rejects_unknown_intent():
    result = normalize_result({"intent": "hack", "links": [], "reason": ""}, ALLOWED)
    assert result["intent"] == "auto_fallback"


def test_normalize_filters_fabricated_links():
    # LLM 编造了不在 allowed 集合里的链接 → 必须被剔除
    result = normalize_result(
        {"intent": "parse", "links": ["https://a.com/1", "https://evil.com/x"], "reason": ""},
        ALLOWED)
    assert result["links"] == ["https://a.com/1"]


def test_normalize_none():
    assert normalize_result(None, ALLOWED)["intent"] == "auto_fallback"
    assert normalize_result("not a dict", ALLOWED)["intent"] == "auto_fallback"


def test_normalize_non_list_links():
    result = normalize_result({"intent": "ask", "links": "oops"}, ALLOWED)
    assert result["intent"] == "ask"
    assert result["links"] == []


def test_normalize_reason_clamped():
    result = normalize_result({"intent": "ask", "links": [], "reason": "x" * 500}, ALLOWED)
    assert len(result["reason"]) == 120


# ====== LRU 缓存 ======

def test_cache_roundtrip():
    _cache_put("k1", {"intent": "parse"})
    assert _cache_get("k1") == {"intent": "parse"}


def test_cache_miss():
    assert _cache_get("never-put") is None


def test_cache_ttl_expiry():
    _cache_put("k-expired", {"intent": "ask"})
    # 人为把时间戳拨回 TTL 之前
    import app.services.intent_router as ir
    ts, value = ir._cache["k-expired"]
    ir._cache["k-expired"] = (time.time() - _CACHE_TTL - 1, value)
    assert _cache_get("k-expired") is None
    assert "k-expired" not in ir._cache


def test_cache_lru_eviction():
    import app.services.intent_router as ir
    old = ir._cache.copy()
    try:
        ir._cache.clear()
        for i in range(100):
            _cache_put(f"key-{i}", {"i": i})
        # 容量 100：第 101 条进队，最老的 key-0 出队
        _cache_put("key-100", {"i": 100})
        assert len(ir._cache) == 100
        assert "key-0" not in ir._cache
        assert "key-100" in ir._cache
    finally:
        ir._cache.clear()
        ir._cache.update(old)


def test_cache_move_to_end_on_hit():
    import app.services.intent_router as ir
    _cache_put("k-old", {"v": 1})
    _cache_put("k-new", {"v": 2})
    _cache_get("k-old")  # 命中 → k-old 移到队尾（最新）
    assert list(ir._cache.keys())[-1] == "k-old"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"passed={passed} failed={failed}")
    sys.exit(1 if failed else 0)
