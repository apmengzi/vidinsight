"""双引擎融合测试。

锁住四件事：
  1. **时间轴只来自 whisper**——FunASR 的字只改文字，绝不改 start/end
  2. 短语气词段（「对对对」「然后」）不送 LLM（分段粒度差异，不是识别错误）
  3. LLM 输出长度异常（吞句/扩写）时拒绝替换，保留原文
  4. 任何异常都不影响主流程——融合是增强项，失败就返回原 transcript
"""
import pytest

from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.transcriber.merge import (
    FunASRIndex,
    _norm,
    _sim,
    merge_transcripts,
)


def _seg(start, end, text):
    return TranscriptSegment(start=start, end=end, text=text)


def _primary(segs):
    return TranscriptResult(
        language="zh",
        full_text="".join(s.text for s in segs),
        segments=segs,
    )


def _funasr(tokens, times, text=""):
    """构造 FunASR 原始返回（raw_text 空格分隔，timestamp 毫秒区间）。"""
    return {
        "text": text or "".join(tokens),
        "raw_text": " ".join(tokens),
        "timestamp": times,
    }


# ── 文本规整 ─────────────────────────────────────────

def test_norm_strips_punct_and_space():
    assert _norm("你好，世界！") == "你好世界"
    assert _norm("a b\tc") == "abc"


def test_norm_unifies_fullwidth_digits():
    assert _norm("１２３") == "123"


def test_sim_identical():
    assert _sim("硬件开发测试", "硬件开发测试") == 1.0


def test_sim_ignores_punct():
    assert _sim("硬件，开发。", "硬件开发") == 1.0


def test_sim_empty():
    assert _sim("", "") == 1.0
    assert _sim("abc", "") == 0.0


# ── FunASR 时间轴索引 ─────────────────────────────────

def test_index_usable():
    idx = FunASRIndex("a b c d e f g h i j k l", [[i * 100, i * 100 + 100] for i in range(12)])
    assert idx.usable


def test_index_not_usable_when_too_few_tokens():
    idx = FunASRIndex("a b", [[0, 100], [100, 200]])
    assert not idx.usable


def test_index_text_between_picks_right_tokens():
    # 每字 100ms：t=0.0-0.3s 应命中前 3 个字
    idx = FunASRIndex(
        "一 二 三 四 五 六 七 八 九 十 甲 乙",
        [[i * 100, i * 100 + 100] for i in range(12)],
    )
    got = idx.text_between(0.0, 0.3, pad_ms=0)
    assert got == "一二三"


def test_index_text_between_pad():
    idx = FunASRIndex(
        "一 二 三 四 五 六 七 八 九 十 甲 乙",
        [[i * 100, i * 100 + 100] for i in range(12)],
    )
    # 加 200ms padding 会多取到后面几个字
    got = idx.text_between(0.0, 0.3, pad_ms=200)
    assert len(got) > 3


def test_index_handles_length_mismatch():
    """tokens 与 timestamps 长度不一致时以短的为准，不能错位。"""
    idx = FunASRIndex("一 二 三", [[0, 100], [100, 200]])
    assert len(idx.tokens) == 2


def test_index_empty():
    idx = FunASRIndex("", [])
    assert not idx.usable
    assert idx.text_between(0, 10) == ""


# ── 融合主流程 ───────────────────────────────────────

def test_merge_preserves_timeline():
    """核心约束：时间轴必须与 whisper 完全一致，一个字都不许变。"""
    segs = [_seg(0.0, 3.0, "这是第一段比较长的内容用于测试"),
            _seg(3.0, 6.0, "这是第二段比较长的内容用于测试")]
    raw = _funasr(
        list("这是第一段比较长的内容用于测试这是第二段比较长的内容用于测试"),
        [[i * 60, i * 60 + 60] for i in range(44)],
    )
    merged, _ = merge_transcripts(_primary(segs), raw, gpt=None)
    assert [(s.start, s.end) for s in merged.segments] == [(0.0, 3.0), (3.0, 6.0)]


def test_merge_skips_short_segments():
    """短语气词段不送校对（分段粒度差异，不是识别错误）。"""
    segs = [_seg(0.0, 1.0, "对对对"),
            _seg(1.0, 4.0, "这是一段足够长的正式内容用于测试")]
    raw = _funasr(list("这是一段足够长的正式内容用于测试"),
                  [[i * 100, i * 100 + 100] for i in range(17)])
    _, audit = merge_transcripts(_primary(segs), raw, gpt=None)
    # 「对对对」不应出现在审计里
    assert all(a["start"] != 0.0 for a in audit)


def test_merge_returns_original_when_no_timestamp():
    """FunASR 没给时间戳时直接放弃融合，返回原结果。"""
    segs = [_seg(0.0, 3.0, "这是一段足够长的内容")]
    raw = {"text": "x", "raw_text": "", "timestamp": []}
    merged, audit = merge_transcripts(_primary(segs), raw, gpt=None)
    assert merged is not None
    assert audit == []


def test_merge_returns_original_when_empty_input():
    pr = _primary([_seg(0.0, 3.0, "内容")])
    merged, audit = merge_transcripts(pr, {}, gpt=None)
    assert audit == []


class _FakeGPT:
    def __init__(self, output):
        self.output = output
        self.calls = 0

    def _do_create(self, messages):
        self.calls += 1

        class M:
            content = self.output
            reasoning_content = ""

        class C:
            message = M()

        class R:
            choices = [C()]

        return R()


def test_merge_llm_replaces_when_length_reasonable():
    """LLM 给出长度合理的修正 → 采纳。

    注意：这里必须让两份文本**差异够大**（相似度低于阈值）才会触发 LLM，
    否则会被 sim_threshold 提前跳过——这正是设计意图（省调用）。
    """
    segs = [_seg(0.0, 4.0, "他去负责签入式的开发测试")]
    raw = _funasr(
        list("他负责嵌入式开发与测试工作"),
        [[i * 100, i * 100 + 100] for i in range(12)],
    )
    gpt = _FakeGPT("他去负责嵌入式的开发测试")
    # 显式提高阈值到 0.99：确保这份构造数据一定走到 LLM 分支。
    # 生产默认 0.75 是「只在明显不一致时才校对」，测试要验证的是校对逻辑本身。
    merged, audit = merge_transcripts(
        _primary(segs), raw, gpt=gpt, sim_threshold=0.99)
    assert gpt.calls >= 1, f"相似度未低于阈值，LLM 未被调用；audit={audit}"
    assert "嵌入式" in merged.segments[0].text
    # 时间轴不变
    assert (merged.segments[0].start, merged.segments[0].end) == (0.0, 4.0)


def test_merge_rejects_llm_output_with_bad_length():
    """LLM 吞句（输出过短）→ 拒绝替换，保留原文。"""
    segs = [_seg(0.0, 4.0, "这是一段完整的长内容不应该被吞掉任何部分")]
    # 参考文本要足够不同，才能把相似度压到阈值以下、真正走到 LLM
    raw = _funasr(
        list("完全不同的参考文本内容用来压低相似度"),
        [[i * 100, i * 100 + 100] for i in range(16)],
    )
    gpt = _FakeGPT("短")   # 明显过短
    merged, audit = merge_transcripts(_primary(segs), raw, gpt=gpt)
    assert gpt.calls >= 1, f"未走到 LLM；audit={audit}"
    assert merged.segments[0].text == segs[0].text
    assert any(a["action"] == "reject_length" for a in audit)


def test_merge_llm_exception_keeps_original():
    """LLM 抛异常 → 保留原文，不崩。"""
    class Boom:
        def _do_create(self, messages):
            raise RuntimeError("provider down")

    segs = [_seg(0.0, 4.0, "这是一段足够长的内容用于测试异常路径")]
    raw = _funasr(
        list("这是完全不同的一段参考文本内容用于触发差异"),
        [[i * 100, i * 100 + 100] for i in range(19)],
    )
    merged, _ = merge_transcripts(_primary(segs), raw, gpt=Boom())
    assert merged.segments[0].text == segs[0].text


def test_merge_respects_llm_call_limit():
    """max_llm_calls 限制生效，防止长视频烧穿预算。"""
    segs = [_seg(i * 4.0, (i + 1) * 4.0, f"第{i}段足够长的内容用于测试调用上限")
            for i in range(10)]
    tokens = []
    times = []
    for i in range(10):
        for j, ch in enumerate(f"第{i}段完全不同的参考文本用于制造差异"):
            tokens.append(ch)
            times.append([(i * 4000) + j * 100, (i * 4000) + j * 100 + 100])
    raw = _funasr(tokens, times)
    gpt = _FakeGPT("替换内容")
    _, _ = merge_transcripts(_primary(segs), raw, gpt=gpt, max_llm_calls=3)
    assert gpt.calls <= 3
