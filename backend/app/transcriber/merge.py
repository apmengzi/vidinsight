"""双引擎融合转写：whisper（时间轴）+ FunASR（中文准确度）→ 按时间轴对齐。

## 关键事实（2026-11-04 实测推翻先前假设）

`AutoModel.generate(..., return_raw_text=True)` 会返回 **`timestamp` 字段**：
逐 token 的毫秒区间 `[[50,170], [170,270], ...]`，与 `raw_text`（空格分隔的字）
一一对应。所以 **FunASR 是有时间轴的**，只是默认不带标点分段。

这意味着融合可以做到**精确对齐**，不需要任何字符比例估算。

## 融合策略

```
whisper 段 (start, end)
   ↓ 按时间区间从 FunASR 的 token 流里取对应字
FunASR 同区间文本
   ↓ 两份都规整后比较
相似度低 → 送 LLM 做字词级选择（保留时间轴，只换文字）
```

三条铁律：
1. **时间轴永远来自 whisper**，FunASR 的字只用来改文字
2. 只把「真差异」送 LLM（短语气词、分段粒度差异一律跳过）
3. 每次替换都记审计，可回溯原文
"""
from __future__ import annotations

import difflib
import json
import re
from typing import List, Optional, Tuple

from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ── 文本规整（用于比较，不改变输出）────────────────────

_PUNCT = re.compile(r"[\s，。！？、；：""''（）()《》【】…—·,.!?;:\"'\[\]{}<>/\\|~`@#$%^&*+=_-]")
_CN_NUM = str.maketrans("０１２３４５６７８９", "0123456789")


def _norm(text: str) -> str:
    """去掉标点空白、统一全角数字，用于「两份文本是否一致」的判断。

    注意：只用于比较，**输出永远用原始文本**——用户看到的标点要保留。
    """
    return _PUNCT.sub("", (text or "").translate(_CN_NUM))


def _sim(a: str, b: str) -> float:
    """两段文本的相似度（0-1）。"""
    na, nb = _norm(a), _norm(b)
    if not na and not nb:
        return 1.0
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


# ── FunASR token 时间轴索引 ────────────────────────────

class FunASRIndex:
    """把 FunASR 的 (raw_text, timestamp) 建成可按时间区间取文本的索引。

    raw_text 是空格分隔的逐字序列，timestamp 是等长的毫秒区间列表。
    取区间文本时按「token 中点是否落在区间内」判断——比按边界切更稳，
    避免一个字的区间横跨两段时被重复计算。
    """

    def __init__(self, raw_text: str, timestamps: list):
        self.tokens: List[str] = [t for t in (raw_text or "").split() if t]
        self.times: List[Tuple[int, int]] = []
        for ts in (timestamps or []):
            try:
                self.times.append((int(ts[0]), int(ts[1])))
            except (TypeError, IndexError, ValueError):
                continue
        # 长度不一致时以短的一边为准（宁可少取，不要错位）
        n = min(len(self.tokens), len(self.times))
        self.tokens = self.tokens[:n]
        self.times = self.times[:n]

    @property
    def usable(self) -> bool:
        return len(self.tokens) >= 10

    def text_between(self, start_s: float, end_s: float,
                     pad_ms: int = 120) -> str:
        """取 [start, end] 秒区间内的字，拼成文本。"""
        if not self.usable:
            return ""
        lo = int(start_s * 1000) - pad_ms
        hi = int(end_s * 1000) + pad_ms
        out = []
        for tok, (t0, t1) in zip(self.tokens, self.times):
            mid = (t0 + t1) / 2
            if lo <= mid <= hi:
                out.append(tok)
        return "".join(out)


# ── LLM 校对 ───────────────────────────────────────────

MERGE_PROMPT = """你是语音转写校对员。同一段音频被两个引擎转写，结果有出入。

【主转写】（时间轴准确，中文同音字易错）
{primary}

【参考转写】（中文用词较准）
{reference}

请判断：这段内容应该用哪个版本？规则：
1. **只做字词级修正**，不要改写句式、不要增删内容
2. 如果参考版明显更准（专有名词、术语、人名），输出修正后的版本
3. 如果两份只是同音字差异但主转写更通顺，**原样输出主转写**
4. 如果参考版缺内容或多内容，**以主转写为准**
5. 拿不准就输出主转写原文（宁可不改）

只输出最终的这一段文字，不要解释、不要加引号、不要标注来源。
"""


def _llm_merge(primary: str, reference: str, gpt) -> Optional[str]:
    """让 LLM 在两份文本间做字词级选择。失败返回 None（调用方保留原文）。"""
    if gpt is None:
        return None
    try:
        resp = gpt._do_create([{
            "role": "user",
            "content": MERGE_PROMPT.format(primary=primary, reference=reference),
        }])
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            msg = resp.choices[0].message
            text = (getattr(msg, "reasoning_content", "") or "").strip()
        text = re.sub(r'^[""「『]|[""」』]$', "", text).strip()
        text = re.sub(r"^(修正后|最终版|答案)[:：]\s*", "", text).strip()
        return text or None
    except Exception as e:
        logger.warning(f"[merge] LLM 校对失败，保留主转写: {str(e)[:100]}")
        return None


# ── 主流程 ─────────────────────────────────────────────

def merge_transcripts(
    primary: TranscriptResult,
    funasr_raw: dict,
    gpt=None,
    *,
    sim_threshold: float = 0.75,
    max_llm_calls: int = 40,
    min_chars: int = 10,
) -> Tuple[TranscriptResult, List[dict]]:
    """融合两份转写结果。

    :param primary: whisper 结果（**时间轴唯一来源**）
    :param funasr_raw: FunASR 的原始返回 dict（含 text / raw_text / timestamp）
    :param gpt: 用于校对的 LLM 实例；None 则只做规则对比不调模型
    :param sim_threshold: 低于此相似度的段才送去 LLM 校对
    :param max_llm_calls: LLM 调用上限，防止长视频烧穿预算
    :param min_chars: 少于这么多字的段不送 LLM。
        实测：whisper 会把「对对对」「然后」「好」单独切成段，
        而 FunASR 把它们吸收进长句——这不是识别差异，是分段粒度不同。
    :return: (融合后的 TranscriptResult, 审计记录列表)
    """
    segs = list(primary.segments or [])
    if not segs or not funasr_raw:
        return primary, []

    index = FunASRIndex(funasr_raw.get("raw_text", ""),
                        funasr_raw.get("timestamp", []))
    if not index.usable:
        logger.warning("[merge] FunASR 未返回可用时间戳，跳过融合")
        return primary, []

    merged: List[TranscriptSegment] = []
    audit: List[dict] = []
    llm_calls = 0
    changed = 0

    for seg in segs:
        # 短段（语气词/单字）跳过：分段粒度差异，不是识别错误
        if len(_norm(seg.text)) < min_chars:
            merged.append(seg)
            continue

        ref_win = index.text_between(seg.start, seg.end)
        sim = _sim(seg.text, ref_win)

        final_text = seg.text
        action = "keep"

        if sim < sim_threshold and _norm(ref_win):
            if gpt is not None and llm_calls < max_llm_calls:
                llm_calls += 1
                picked = _llm_merge(seg.text, ref_win, gpt)
                if picked and _sim(picked, seg.text) < 0.999:
                    # 保险：只接受长度合理的替换，防止模型吞句/扩写
                    ratio = len(picked) / max(1, len(seg.text))
                    if 0.6 <= ratio <= 1.6:
                        final_text = picked
                        action = "llm_merge"
                        changed += 1
                    else:
                        action = "reject_length"
            elif gpt is None:
                action = "diff_no_llm"

        merged.append(TranscriptSegment(
            start=seg.start, end=seg.end, text=final_text,
        ))
        if action != "keep":
            audit.append({
                "start": round(seg.start, 2), "end": round(seg.end, 2),
                "sim": round(sim, 3), "action": action,
                "primary": seg.text[:120],
                "reference": ref_win[:120],
                "final": final_text[:120],
            })

    full_text = "".join(s.text for s in merged).strip()
    logger.info(
        f"[merge] 融合完成: {len(segs)} 段, {changed} 段被修正, "
        f"LLM 调用 {llm_calls} 次"
    )

    return TranscriptResult(
        language=primary.language,
        full_text=full_text,
        segments=merged,
        raw=primary.raw,
    ), audit
