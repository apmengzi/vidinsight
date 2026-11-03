"""一图流内容提炼：让 LLM 通读整篇笔记，产出一图流专用的结构化数据。

为什么不让 LLM 直接写整张图（HTML/SVG）：
  - 排版由代码控制才能保证不溢出、字号一致、永不出白图
  - LLM 只负责"提炼什么"，代码负责"怎么摆"——职责分离，两边都好验证

为什么不在渲染时现算：
  - 一图流是慢操作（LLM 要读完几千字），结果要能缓存复用
  - 提炼结果落盘，重复生成同一条笔记不再烧 token
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

# 输出结构固定为这几个字段，前端和渲染层都依赖它
ONEPAGER_SCHEMA_KEYS = ("title", "subtitle", "points", "conclusion")

DISTILL_PROMPT = """你是信息设计编辑。下面是一份视频笔记全文，请把它提炼成一张「一图流」信息图需要的内容。

严格要求：
1. **只使用原文里出现过的信息**，绝对不要补充原文没有的事实、数字、结论
2. 要点要具体：优先保留原文里的数字、术语、对比关系、步骤顺序；不要写"讲了很多细节"这种空话
3. 每条要点控制在一句话内（不超过 40 字），能被一眼扫完
4. 如果原文是教程/步骤类，按操作顺序排列；如果是观点/分析类，按重要性排列

输出**严格 JSON**，不要 markdown 代码块，不要任何解释文字：
{{
  "title": "整篇笔记的核心主题，不超过 20 字",
  "subtitle": "一句话说清这篇讲了什么，不超过 35 字",
  "points": [
    {{"head": "要点小标题，4-10 字", "body": "具体说明，不超过 40 字"}}
  ],
  "conclusion": "一句话结论或行动建议，不超过 30 字，原文没有明确结论就留空字符串"
}}

points 数量控制在 3-6 条，挑最有信息量的。

笔记全文：
---
{content}
---
"""


def _extract_json(text: str) -> Optional[dict]:
    """从 LLM 输出里抠出 JSON（容忍代码块包裹、前后多余文字）。"""
    if not text:
        return None
    s = text.strip()
    # 去掉 ```json ... ``` 包裹
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)

    # 直接解析
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass

    # 退化：找第一个 { 到最后一个 }
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(s[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


def _clip(text: str, limit: int) -> str:
    """截断到指定长度，去掉换行和多余空白。"""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t[:limit] if len(t) > limit else t


def normalize(raw: dict) -> Optional[Dict]:
    """把 LLM 输出规整成渲染层能直接用的结构。

    任何字段缺失都补默认值，**绝不因为模型输出不规范就整张图失败**——
    一图流是展示层，宁可降级也不该报错。
    """
    if not isinstance(raw, dict):
        return None

    title = _clip(raw.get("title"), 24)
    subtitle = _clip(raw.get("subtitle"), 42)
    conclusion = _clip(raw.get("conclusion"), 36)

    points: List[Dict[str, str]] = []
    raw_points = raw.get("points")
    if isinstance(raw_points, list):
        for p in raw_points:
            if isinstance(p, dict):
                head = _clip(p.get("head"), 16)
                body = _clip(p.get("body"), 48)
                if head or body:
                    points.append({"head": head, "body": body})
            elif isinstance(p, str):
                body = _clip(p, 48)
                if body:
                    points.append({"head": "", "body": body})
            if len(points) >= 6:
                break

    if not title and not points:
        return None

    return {
        "title": title or "内容洞察",
        "subtitle": subtitle,
        "points": points,
        "conclusion": conclusion,
    }


def distill(markdown: str, gpt) -> Optional[Dict]:
    """调用 LLM 把整篇笔记提炼成一图流结构。失败返回 None（调用方降级到本地抽取）。"""
    content = (markdown or "").strip()
    if len(content) < 40:
        return None

    # 长笔记截断：一图流只需要骨架，塞全文反而让模型抓不住重点。
    # 保留头尾——开头通常是主题，结尾通常是结论。
    if len(content) > 6000:
        head = content[:4000]
        tail = content[-1800:]
        content = f"{head}\n\n……（中间省略）……\n\n{tail}"

    prompt = DISTILL_PROMPT.format(content=content)
    try:
        resp = gpt._do_create([{"role": "user", "content": prompt}])
        text = (resp.choices[0].message.content or "").strip()
        # 某些模型思考时会把内容塞进 reasoning_content，正文为空
        if not text:
            msg = resp.choices[0].message
            text = (getattr(msg, "reasoning_content", "") or "").strip()
        obj = _extract_json(text)
        result = normalize(obj) if obj else None
        if result:
            logger.info(f"[one_pager] LLM 提炼成功：{len(result['points'])} 个要点")
        else:
            logger.warning(f"[one_pager] LLM 输出无法解析，将降级到本地抽取: {text[:120]}")
        return result
    except Exception as e:
        logger.warning(f"[one_pager] LLM 提炼失败，将降级到本地抽取: {str(e)[:120]}")
        return None
