"""一图流 LLM 提炼层测试。

锁住三件事：
  1. 模型输出不规范（带 ```json 包裹、前后有废话）也要能抠出 JSON
  2. 字段缺失/类型错误一律补默认值，**绝不整张图失败**
  3. LLM 不可用（抛异常/返回垃圾）时返回 None，让调用方降级到本地抽取
"""
import pytest

from app.utils.one_pager_distill import (
    _clip,
    _extract_json,
    distill,
    normalize,
)


# ── JSON 抠取 ────────────────────────────────────────

def test_extract_json_plain():
    assert _extract_json('{"title": "x"}') == {"title": "x"}


def test_extract_json_with_code_fence():
    """模型很爱把 JSON 包在 ```json 里。"""
    text = '```json\n{"title": "x", "points": []}\n```'
    assert _extract_json(text) == {"title": "x", "points": []}


def test_extract_json_with_prose_around():
    """模型偶尔会先解释再给 JSON。"""
    text = '好的，这是结果：\n{"title": "y"}\n希望有帮助。'
    assert _extract_json(text) == {"title": "y"}


def test_extract_json_garbage_returns_none():
    assert _extract_json("完全不是 JSON") is None
    assert _extract_json("") is None
    assert _extract_json(None) is None


def test_extract_json_array_returns_none():
    """顶层是数组时视为无效（我们要的是对象）。"""
    assert _extract_json('[1, 2, 3]') is None


# ── 规整 ────────────────────────────────────────────

def test_normalize_full():
    raw = {
        "title": "标题",
        "subtitle": "副标题",
        "points": [{"head": "要点一", "body": "说明一"}],
        "conclusion": "结论",
    }
    r = normalize(raw)
    assert r["title"] == "标题"
    assert r["points"][0] == {"head": "要点一", "body": "说明一"}
    assert r["conclusion"] == "结论"


def test_normalize_missing_fields_get_defaults():
    """只给 title 也要能出图。"""
    r = normalize({"title": "只有标题"})
    assert r["title"] == "只有标题"
    assert r["subtitle"] == ""
    assert r["points"] == []
    assert r["conclusion"] == ""


def test_normalize_points_as_strings():
    """模型有时把 points 写成纯字符串数组。"""
    r = normalize({"title": "t", "points": ["要点甲", "要点乙"]})
    assert [p["body"] for p in r["points"]] == ["要点甲", "要点乙"]
    assert all(p["head"] == "" for p in r["points"])


def test_normalize_drops_empty_points():
    r = normalize({"title": "t", "points": [{}, {"head": "", "body": ""},
                                             {"head": "有效", "body": "x"}]})
    assert len(r["points"]) == 1


def test_normalize_caps_points_at_six():
    raw = {"title": "t", "points": [{"head": f"h{i}", "body": "b"} for i in range(20)]}
    assert len(normalize(raw)["points"]) == 6


def test_normalize_empty_returns_none():
    assert normalize({}) is None
    assert normalize({"title": "", "points": []}) is None
    assert normalize(None) is None


def test_normalize_truncates_long_text():
    """超长文本必须截断，否则渲染时撑爆画布。"""
    r = normalize({"title": "标" * 100, "points": [{"head": "h" * 100, "body": "b" * 300}]})
    assert len(r["title"]) <= 24
    assert len(r["points"][0]["head"]) <= 16
    assert len(r["points"][0]["body"]) <= 48


def test_clip_collapses_whitespace():
    assert _clip("a\n\n  b   c", 20) == "a b c"


# ── distill（LLM 调用）───────────────────────────────

class _Msg:
    def __init__(self, content="", reasoning_content=""):
        self.content = content
        self.reasoning_content = reasoning_content


class _Choice:
    def __init__(self, msg):
        self.message = msg


class _Resp:
    def __init__(self, msg):
        self.choices = [_Choice(msg)]


class _FakeGPT:
    def __init__(self, text="", reasoning=""):
        self._text = text
        self._reasoning = reasoning
        self.calls = 0

    def _do_create(self, messages):
        self.calls += 1
        return _Resp(_Msg(self._text, self._reasoning))


def test_distill_happy_path():
    gpt = _FakeGPT('{"title": "t", "points": [{"head": "h", "body": "b"}]}')
    r = distill("这是一段足够长的笔记内容" * 5, gpt)
    assert r["title"] == "t"
    assert gpt.calls == 1


def test_distill_uses_reasoning_content_when_content_empty():
    """部分模型思考时正文为空、内容全在 reasoning_content（实测 Systalyze）。"""
    gpt = _FakeGPT(text="", reasoning='{"title": "从思考里拿到的"}')
    r = distill("这是一段足够长的笔记内容" * 5, gpt)
    assert r["title"] == "从思考里拿到的"


def test_distill_returns_none_on_exception():
    class Boom:
        def _do_create(self, messages):
            raise RuntimeError("provider down")

    assert distill("这是一段足够长的笔记内容" * 5, Boom()) is None


def test_distill_returns_none_on_garbage_output():
    gpt = _FakeGPT("我不会按格式输出，这里是散文……")
    assert distill("这是一段足够长的笔记内容" * 5, gpt) is None


def test_distill_skips_short_content():
    """内容太短不浪费一次调用。"""
    gpt = _FakeGPT('{"title": "t"}')
    assert distill("短", gpt) is None
    assert gpt.calls == 0


def test_distill_truncates_long_content():
    """超长笔记要截断后再送模型，避免撑爆上下文。"""
    captured = {}

    class Cap:
        def _do_create(self, messages):
            captured["len"] = len(messages[0]["content"])
            return _Resp(_Msg('{"title": "t", "points": [{"head":"h","body":"b"}]}'))

    distill("内容" * 8000, Cap())
    # 6000 字上限 + 提示词模板本身，不应超过 9000
    assert captured["len"] < 9000
