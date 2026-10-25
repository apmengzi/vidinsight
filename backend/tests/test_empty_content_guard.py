"""空正文防护测试（独立文件）。

为什么不与 test_universal_gpt_content_format 合并：那个文件用 types.ModuleType
伪造 app 包并注入 sys.modules 且不恢复，会把本文件的 import 一起污染。
"""

# 背景：deepseek-v4.1-flash 在开放式分析题上思考链会失控膨胀（实测 20892 字
# 思考、正文 0 字，finish_reason=length）。若把空正文直接存进 partials，
# 会生成残缺/空白报告而任务仍显示成功——答辩现场的真实风险。
# 以下用例锁住"空正文必须重试、仍空必须报错"的契约。

class _Msg:
    def __init__(self, content):
        self.content = content
        self.reasoning_content = "很长很长的思考链" if not content else ""


class _Choice:
    def __init__(self, content, finish):
        self.message = _Msg(content)
        self.finish_reason = finish


class _Resp:
    def __init__(self, content, finish="stop"):
        self.choices = [_Choice(content, finish)]


def _mk_gpt():
    """构造一个不依赖真实网络的最小 UniversalGPT 实例。"""
    from app.gpt.universal_gpt import UniversalGPT
    g = UniversalGPT.__new__(UniversalGPT)
    g.model = "deepseek-v4.1-flash"
    g.temperature = 0.7
    return g


def test_extract_content_returns_empty_for_none():
    g = _mk_gpt()
    assert g._extract_content(_Resp(None, "length")) == ""
    assert g._extract_content(_Resp("   ", "length")) == ""
    assert g._extract_content(_Resp("正常内容")) == "正常内容"


def test_finish_reason_readable():
    g = _mk_gpt()
    assert g._finish_reason(_Resp("", "length")) == "length"
    assert g._finish_reason(_Resp("x", "stop")) == "stop"


def test_extract_content_never_raises_on_broken_response():
    """响应结构异常也不能抛——防护逻辑本身不该成为新的故障点。"""
    g = _mk_gpt()
    assert g._extract_content(object()) == ""
    assert g._finish_reason(object()) == ""


# ── 思考强度控制（2026-10-12 用户要求 + 实测驱动）────────
# 总结任务不需要深度思考，而思考链会消耗输出预算（实测 deepseek 思考
# 10794 字导致正文 0 字）。策略：能关的关（thinking disabled），
# 只有档位的设 low（reasoning_effort），都不认则回退普通调用。

def test_thinking_kwargs_disables_thinking_by_default():
    g = _mk_gpt()
    kw = g._thinking_kwargs()
    assert kw == {"extra_body": {"thinking": {"type": "disabled"}}}


def test_thinking_kwargs_respects_env_off(monkeypatch):
    """VIDINSIGHT_THINKING_OFF=0 时不应注入任何思考参数（留逃生门）。"""
    monkeypatch.setenv("VIDINSIGHT_THINKING_OFF", "0")
    g = _mk_gpt()
    assert g._thinking_kwargs() == {}


def test_unsupported_param_error_detection():
    g = _mk_gpt()
    assert g._is_unsupported_param_error(Exception("Unknown parameter: thinking")) is True
    assert g._is_unsupported_param_error(Exception("unsupported field extra_body")) is True
    assert g._is_unsupported_param_error(Exception("rate limit exceeded")) is False


def test_do_create_cascades_on_unsupported_param(monkeypatch):
    """第一组参数被拒 → 自动降级到 reasoning_effort → 再拒则普通调用。"""
    g = _mk_gpt()
    g.temperature = 0.7
    calls = []

    class _FakeCompletions:
        def create(self, **kw):
            calls.append(kw)
            if len(calls) == 1:
                raise Exception("Unknown parameter: thinking")
            if len(calls) == 2:
                raise Exception("unsupported field: reasoning_effort")
            return _Resp("最终答案")

    class _FakeChat:
        completions = _FakeCompletions()

    g.client = type("C", (), {"chat": _FakeChat()})()
    out = g._do_create([{"role": "user", "content": "hi"}])
    assert g._extract_content(out) == "最终答案"
    assert len(calls) == 3, "应依次尝试三组参数"
    # 第三组必须不含任何思考参数（最保守回退）
    assert "extra_body" not in calls[2]
