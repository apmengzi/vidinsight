"""高阶意图路由器（T7 / Premium）。

机械规则的升级版：由 LLM 在管线最前端判断「用户到底想干什么」，
替代纯正则的链接/提问二分。机械规则仍保留为保底路径——

  高阶开关关闭（默认）: 链接 → 解析；其余 → 知识库问答（零延迟、零成本）
  高阶开关打开:         输入先过一次轻量 LLM 路由（~1s），按意图分发；
                        路由失败/超时自动回退机械路径

意图集合：
  parse     解析/总结链接（单条、批量、合集；可夹杂文字要求）
  compare   把新链接与知识库已有内容对比（先解析，后对比提问）
  ask       基于知识库的提问/查询（不带解析意图）
  chitchat  寒暄、与产品无关

关键设计：链接的存在性永远由正则机械判定（事实，不由 LLM 编造），
LLM 只决定「拿这些链接做什么」。
"""
import json
import re
import time
from collections import OrderedDict
from typing import Optional

from app.gpt.gpt_factory import GPTFactory
from app.models.model_config import ModelConfig
from app.services.provider import ProviderService
from app.utils.logger import get_logger

logger = get_logger(__name__)

VALID_INTENTS = {"parse", "compare", "ask", "chitchat"}

ROUTER_SYSTEM_PROMPT = """你是 VidInsight（灵悉）——AI 短视频/笔记内容洞察工作台的意图路由器。

## 产品能力（你只能路由到这些能力）
1. parse 解析总结：下载并转写/读取视频或图文笔记，AI 生成结构化洞察报告。
   支持 B站、YouTube、抖音、快手、小红书（视频+图文）、小红书合集（自动展开全部笔记批量解析）。
2. compare 对比分析：把新解析的内容与知识库中已有的历史分析记录做对比（卖点、人群、话术等）。
   流程是先解析新链接，完成后用户可直接追问对比。
3. ask 知识库问答：基于已有分析记录提问，回答带来源编号引用。
4. chitchat 闲聊：与以上能力无关的寒暄或闲聊。

## 你的任务
判断用户这条消息的主要意图，并挑出消息中需要解析的链接。

## 判定规则
- 消息含链接且（明确要求解析/总结/分析，或除链接外只有少量辅助文字）→ parse
- 消息含链接且明确要与已有内容对比/对标/找差异 → compare
- 消息不含解析意图、在问已有记录的内容或产品怎么用 → ask
- 纯寒暄/与产品无关 → chitchat
- 只有链接、没有任何文字：默认 parse
- 严禁编造消息里不存在的链接；links 只能来自用户提供的链接列表

## 输出格式
只输出一个 JSON 对象，不要任何其他文字或代码块标记：
{"intent": "parse|compare|ask|chitchat", "links": ["选出的待解析链接"], "reason": "一句话理由"}"""

# LRU 缓存：同一条输入（含链接）短时间内不重复烧 LLM
_CACHE_MAX = 100
_cache: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()
_CACHE_TTL = 600.0  # 10 分钟


def _cache_key(message: str, links: tuple) -> str:
    return f"{message.strip()}|{'|'.join(sorted(links))}"


def _cache_get(key: str) -> Optional[dict]:
    item = _cache.get(key)
    if not item:
        return None
    ts, value = item
    if time.time() - ts > _CACHE_TTL:
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)
    return value


def _cache_put(key: str, value: dict) -> None:
    _cache[key] = (time.time(), value)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


def extract_json(text: str) -> Optional[dict]:
    """从 LLM 回复中稳健地提取路由 JSON（容忍 ``` 围栏与前后杂文）。"""
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        try:
            return json.loads(m.group(0).replace("'", '"'))
        except Exception:
            return None


def normalize_result(raw: Optional[dict], allowed_links: list[str]) -> dict:
    """校验并收敛路由结果；任何不合法都回落 auto_fallback。"""
    if not raw or not isinstance(raw, dict):
        return {"intent": "auto_fallback", "links": [], "reason": "路由结果为空"}
    intent = str(raw.get("intent", "")).strip().lower()
    if intent not in VALID_INTENTS:
        return {"intent": "auto_fallback", "links": [], "reason": f"未知意图: {intent}"}
    links = raw.get("links") or []
    if not isinstance(links, list):
        links = []
    allowed = set(allowed_links)
    safe_links = [str(l) for l in links if l in allowed]
    return {"intent": intent, "links": safe_links,
            "reason": str(raw.get("reason", ""))[:120]}


def route_intent(
    message: str,
    links: list[str],
    provider_id: str,
    model_name: str,
) -> dict:
    """主入口：LLM 意图路由。任何失败返回 auto_fallback（前端走机械路径）。"""
    key = _cache_key(message, tuple(links))
    cached = _cache_get(key)
    if cached is not None:
        return {**cached, "cached": True}

    try:
        provider = ProviderService.get_provider_by_id(provider_id)
        if not provider:
            raise ValueError(f"未找到模型供应商: {provider_id}")
        config = ModelConfig(
            api_key=provider["api_key"],
            base_url=provider["base_url"],
            model_name=model_name,
            provider=provider["type"],
            name=provider["name"],
        )
        client = GPTFactory.from_config(config).client

        user_prompt = (
            f"用户提供的链接列表（只能从中选择）：\n"
            f"{json.dumps(links, ensure_ascii=False) if links else '（无）'}\n\n"
            f"用户消息：\n{message[:2000]}"
        )
        resp = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
            max_tokens=300,
        )
        raw = extract_json(resp.choices[0].message.content or "")
        result = normalize_result(raw, links)
        if result["intent"] == "auto_fallback":
            return result  # 不缓存失败结果
        _cache_put(key, result)
        logger.info(f"意图路由: intent={result['intent']} links={len(result['links'])} "
                    f"reason={result['reason'][:60]}")
        return result
    except Exception as e:
        logger.warning(f"意图路由失败，回退机械路径: {e}")
        return {"intent": "auto_fallback", "links": [], "reason": str(e)[:120]}
