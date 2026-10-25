"""
Chat 服务层。
兼容旧版单笔记 RAG 问答和新的全局 Agent 问答。
"""
from typing import Any, Optional
import json

from app.services.vector_store import VectorStoreManager
from app.services.chat_tools import TOOLS, execute_tool
from app.services.agent import VidInsightAgent
from app.gpt.gpt_factory import GPTFactory
from app.models.model_config import ModelConfig
from app.services.provider import ProviderService
from app.utils.logger import get_logger

logger = get_logger(__name__)

SYSTEM_PROMPT = """你是一个短视频内容洞察问答助手。你拥有以下能力：

1. 系统已自动检索了一些相关内容作为初始参考（见下方）
2. 你可以调用工具主动查询更多信息：
   - lookup_transcript: 查询视频原始转录文本（支持按时间、关键词、位置筛选）
   - get_video_info: 获取视频元信息（标题、作者、简介、标签等）
   - get_note_content: 获取完整内容洞察报告

--- 初始检索内容 ---
{context}
---

回答要求：
- 如果初始检索内容不足以回答问题，请主动调用工具获取更多信息
- 回答关于视频具体原话、细节时，用 lookup_transcript 查询原文
- 回答关于作者、标题等基本信息时，用 get_video_info 查询
- 请用中文回答，保持简洁准确"""


def _build_context(chunks: list[dict]) -> str:
    parts = []
    for chunk in chunks:
        meta = chunk.get("metadata", {})
        source_type = meta.get("source_type", "unknown")
        if source_type == "meta":
            label = "[视频信息]"
        elif source_type == "markdown":
            label = f"[分析 - {meta.get('section_title', '')}]"
        else:
            start = meta.get("start_time", 0)
            end = meta.get("end_time", 0)
            label = f"[转录 - {start:.0f}s~{end:.0f}s]"
        parts.append(f"{label}\n{chunk['text']}")
    return "\n\n".join(parts)


def _build_sources(chunks: list[dict]) -> list[dict]:
    sources = []
    for chunk in chunks:
        meta = chunk.get("metadata", {})
        source = {
            "text": chunk["text"][:200],
            "source_type": meta.get("source_type", "unknown"),
        }
        if meta.get("section_title"):
            source["section_title"] = meta["section_title"]
        if meta.get("start_time") is not None:
            source["start_time"] = meta["start_time"]
        if meta.get("end_time") is not None:
            source["end_time"] = meta["end_time"]
        sources.append(source)
    return sources


# ═══════════════════════════════════════════════════
# 旧版：单笔记 RAG 问答（保持向后兼容）
# ═══════════════════════════════════════════════════

def chat_legacy(
    task_id: str,
    question: str,
    history: list[dict],
    provider_id: str,
    model_name: str,
) -> dict:
    """基于单个笔记的 RAG + Tool Calling 问答（旧版兼容）。"""
    vector_store = VectorStoreManager()

    chunks = vector_store.query(task_id, question, n_results=6)
    context = _build_context(chunks) if chunks else "（未检索到相关内容，请使用工具查询）"
    sources = _build_sources(chunks) if chunks else []

    system_msg = SYSTEM_PROMPT.format(context=context)
    messages = [{"role": "system", "content": system_msg}]

    for msg in history[-20:]:
        messages.append({"role": msg["role"], "content": msg["content"]})

    messages.append({"role": "user", "content": question})

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
    gpt = GPTFactory.from_config(config)

    max_rounds = 3
    for round_i in range(max_rounds):
        response = gpt.client.chat.completions.create(
            model=gpt.model,
            messages=messages,
            tools=TOOLS,
            temperature=0.7,
        )

        msg = response.choices[0].message

        if not msg.tool_calls:
            return {"answer": msg.content or "", "sources": sources}

        messages.append(msg)

        for tool_call in msg.tool_calls:
            fn_name = tool_call.function.name
            try:
                fn_args = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                fn_args = {}

            logger.info(f"Tool call [{round_i + 1}/{max_rounds}]: {fn_name}")
            result = execute_tool(task_id, fn_name, fn_args)

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            })

    response = gpt.client.chat.completions.create(
        model=gpt.model,
        messages=messages,
        temperature=0.7,
    )

    return {"answer": response.choices[0].message.content or "", "sources": sources}


# ═══════════════════════════════════════════════════
# 新版：全局 Agent 问答（统一知识库 + ReAct）
# ═══════════════════════════════════════════════════

# Agent 实例缓存（按 provider+model 缓存）
_agent_cache: dict[str, VidInsightAgent] = {}


# ═══════════════════════════════════════════════════
# Harness：统一 produce / consume 的 Agent 内核
# ═══════════════════════════════════════════════════

_harness_cache: dict[str, Any] = {}


def chat_harness(
    question: str,
    history: list[dict],
    provider_id: str,
    model_name: str,
    task_id: Optional[str] = None,
    allowed_tool_kinds: Optional[list[str]] = None,
) -> dict:
    """
    统一 harness 问答：produce（生产笔记）与 consume（检索问答）同环编排。

    allowed_tool_kinds: 场景隔离白名单，如 ["consume"] 只开检索类工具，
    用于"只读问答"面板不触发生产（避免误触第三方平台）。
    """
    cache_key = f"{provider_id}:{model_name}"

    if cache_key not in _harness_cache:
        logger.info(f"创建 Harness 实例: {cache_key}")
        try:
            from app.services.harness import VidInsightHarness

            _harness_cache[cache_key] = VidInsightHarness(provider_id, model_name)
        except Exception as e:
            logger.error(f"Harness 初始化失败 [{cache_key}]: {e}", exc_info=True)
            raise ValueError(f"AI 模型初始化失败: {str(e)[:200]}") from e

    harness = _harness_cache[cache_key]
    try:
        return harness.chat(
            message=question,
            history=history,
            task_id=task_id,
            allowed_tool_kinds=allowed_tool_kinds,
        )
    except Exception as e:
        logger.error(f"Harness 调用失败 [{cache_key}]，清除缓存: {e}", exc_info=True)
        _harness_cache.pop(cache_key, None)
        raise


def chat_harness_stream(
    question: str,
    history: list[dict],
    provider_id: str,
    model_name: str,
    task_id: Optional[str] = None,
    allowed_tool_kinds: Optional[list[str]] = None,
):
    """流式 harness：逐事件 yield dict（thinking/tool/delta/final/error）。

    实例缓存与 chat_harness 共用；初始化失败时 yield 一个 error 事件而不是抛——
    SSE 已经开始响应，抛异常只会变成断流，前端收不到任何解释。
    """
    cache_key = f"{provider_id}:{model_name}"
    if cache_key not in _harness_cache:
        logger.info(f"创建 Harness 实例（stream）: {cache_key}")
        try:
            from app.services.harness import VidInsightHarness

            _harness_cache[cache_key] = VidInsightHarness(provider_id, model_name)
        except Exception as e:
            logger.error(f"Harness 初始化失败 [{cache_key}]: {e}", exc_info=True)
            yield {"type": "error", "message": f"AI 模型初始化失败: {str(e)[:200]}",
                   "reason": "model_init_failed"}
            return

    harness = _harness_cache[cache_key]
    try:
        yield from harness.chat_stream(
            message=question,
            history=history,
            task_id=task_id,
            allowed_tool_kinds=allowed_tool_kinds,
        )
    except Exception as e:
        logger.error(f"Harness 流式调用失败 [{cache_key}]: {e}", exc_info=True)
        _harness_cache.pop(cache_key, None)
        yield {"type": "error", "message": f"问答失败: {str(e)[:200]}",
               "reason": "harness_stream_failed"}


def chat_agent(
    question: str,
    history: list[dict],
    provider_id: str,
    model_name: str,
    task_id: Optional[str] = None,
) -> dict:
    """
    全局 Agent 问答：统一知识库 + ReAct 推理 + 来源引用。

    Args:
        question: 用户问题
        history: 对话历史
        provider_id: LLM 供应商 ID
        model_name: 模型名称
        task_id: 可选，限定到特定笔记

    Returns:
        {"answer", "sources", "intent", "tool_calls", "kb_stats"}
    """
    cache_key = f"{provider_id}:{model_name}"

    if cache_key not in _agent_cache:
        logger.info(f"创建 Agent 实例: {cache_key}")
        try:
            _agent_cache[cache_key] = VidInsightAgent(provider_id, model_name)
        except Exception as e:
            logger.error(f"Agent 初始化失败 [{cache_key}]: {e}", exc_info=True)
            raise ValueError(f"AI 模型初始化失败: {str(e)[:200]}") from e

    agent = _agent_cache[cache_key]
    try:
        return agent.chat(message=question, history=history, task_id=task_id)
    except Exception as e:
        # Agent 调用失败时清除缓存，下次请求重新初始化
        logger.error(f"Agent 调用失败 [{cache_key}]，清除缓存: {e}", exc_info=True)
        _agent_cache.pop(cache_key, None)
        raise
