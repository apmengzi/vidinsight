"""
Chat API 路由。

端点：
- POST /chat/index      — 索引笔记到统一知识库
- GET  /chat/status     — 查询索引状态
- POST /chat/ask        — Agent 问答（统一知识库 + ReAct，task_id 可选）
- POST /chat/ask/legacy — 旧版单笔记 RAG 问答（向后兼容）
- GET  /chat/kb/stats   — 知识库统计
- POST /chat/kb/rebuild — 重建 BM25 索引
"""
from typing import Optional

import json as _json
import time

from fastapi import APIRouter, BackgroundTasks
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.services.chat_service import chat_legacy, chat_agent, chat_harness, chat_harness_stream
from app.services.vector_store import VectorStoreManager
from app.services.knowledge_base import UnifiedKnowledgeBase
from app.utils.logger import get_logger
from app.utils.response import ResponseWrapper as R
from app.services.task_cancel import TaskCancelled, cancel_task, clear_task

logger = get_logger(__name__)

router = APIRouter()

# 索引状态追踪
_index_status: dict[str, str] = {}


def clear_index_status(task_ids: list[str]) -> None:
    """清理已删除任务的内存索引状态。"""
    for task_id in task_ids:
        _index_status.pop(task_id, None)


# ── Pydantic 模型 ──────────────────────────────

class IndexRequest(BaseModel):
    task_id: str


class ChatMessage(BaseModel):
    role: str
    content: str


class AskRequest(BaseModel):
    question: str
    history: list[ChatMessage] = []
    provider_id: str
    model_name: str
    task_id: Optional[str] = None  # 可选，限定到特定笔记


# ── 索引 ───────────────────────────────────────

def _do_index(task_id: str):
    """后台索引任务：同时索引到新旧两个系统。"""
    try:
        _index_status[task_id] = "indexing"

        # 旧版按任务索引（保持兼容）
        try:
            VectorStoreManager().index_task(task_id)
        except Exception as e:
            logger.warning(f"旧版索引失败（不影响）: {e}")

        # 新版统一知识库索引
        kb = UnifiedKnowledgeBase()
        count = kb.index_note(task_id)
        if count == 0:
            raise RuntimeError("无可索引内容")

        _index_status[task_id] = "indexed"
        logger.info(f"索引完成: {task_id}, chunks={count}")
    except Exception as e:
        _index_status[task_id] = "failed"
        logger.error(f"索引失败: {task_id}, {e}")


@router.post("/chat/index")
def index_task(data: IndexRequest, background_tasks: BackgroundTasks):
    """触发后台索引任务。幂等：已索引的笔记会被重新索引。"""
    if _index_status.get(data.task_id) == "indexing":
        return R.success(msg="正在索引中，请稍候")

    _index_status[data.task_id] = "indexing"
    background_tasks.add_task(_do_index, data.task_id)
    return R.success(msg="开始索引")


@router.get("/chat/status")
def chat_status(task_id: str):
    """
    查询索引状态。

    Returns:
        status: idle | indexing | indexed | failed
        indexed: bool
    """
    try:
        # 优先查内存状态
        status = _index_status.get(task_id)
        if status:
            return R.success(data={
                "status": status,
                "indexed": status == "indexed",
            })

        # 回退到持久化检查
        kb = UnifiedKnowledgeBase()
        indexed = kb.is_indexed(task_id)
        if indexed:
            _index_status[task_id] = "indexed"

        return R.success(data={
            "status": "indexed" if indexed else "idle",
            "indexed": indexed,
        })
    except Exception as e:
        logger.error(f"查询索引状态失败: {e}")
        return R.success(data={"status": "idle", "indexed": False})


# ── Agent 问答（新版，主要使用）────────────────

@router.post("/chat/ask")
def ask_agent(data: AskRequest):
    """
    Agent 问答 — 使用统一知识库 + ReAct 推理。

    支持全局搜索（不传 task_id）和单笔记限定（传 task_id）。
    Response:
        {
            answer: str,        # 含 [n] 引用标记的回答
            sources: [{...}],   # 来源引用列表
            intent: str,        # search | task | chitchat
            tool_calls: [...],  # Agent 工具调用追踪
            kb_stats: {...},    # 知识库统计
        }
    """
    logger.info(f"[Router] 收到 chat/ask 请求 provider={data.provider_id} model={data.model_name} question={data.question[:50]}")
    try:
        history = [
            {"role": m.role, "content": m.content}
            for m in data.history
        ]

        result = chat_agent(
            question=data.question,
            history=history,
            provider_id=data.provider_id,
            model_name=data.model_name,
            task_id=data.task_id,
        )
        return R.success(data=result)

    except TaskCancelled:
        clear_task(data.task_id)
        logger.info(f"Agent 问答被用户取消 (task_id={data.task_id})")
        return R.success(data={
            "answer": "已按你的要求停止本轮回答。",
            "sources": [], "intent": "search", "tool_calls": [],
            "cancelled": True,
        })
    except ValueError as e:
        return R.error(msg=str(e))
    except Exception as e:
        logger.error(f"Agent 问答失败: {e}", exc_info=True)
        return R.error(msg=f"问答失败: {str(e)}")


class HarnessAskRequest(AskRequest):
    """统一 harness 问答请求。

    allowed_tool_kinds: 场景隔离白名单——
      ["consume"]            只开检索类（只读问答面板，不触发生产）
      ["produce"]            只开生产类（导入面板）
      不传 / ["consume","produce"]  全开（主对话）
    """

    allowed_tool_kinds: list[str] = ["consume", "produce"]


@router.post("/chat/ask/harness")
def ask_harness(data: HarnessAskRequest):
    """
    统一 Harness 问答 —— produce（生产笔记）与 consume（检索问答）同环编排。

    与 /chat/ask 的区别：
    - 新增 produce 工具（校验链接 / 生成笔记 / 摄入收藏专辑 / 平台列表）
    - 每个工具返回结构化结果（success + reason），环据此决策重试或换路
    - 受限工具（generate_note / ingest_board）显式标记，受产品红线约束：
      只处理用户自己的内容，绝不自动登录、绝不爬取他人内容

    Response 同 /chat/ask，另附 rounds / degraded / elapsed_s。
    """
    logger.info(
        f"[Router] chat/ask/harness provider={data.provider_id} "
        f"model={data.model_name} kinds={data.allowed_tool_kinds} "
        f"question={data.question[:50]}"
    )
    try:
        history = [{"role": m.role, "content": m.content} for m in data.history]

        result = chat_harness(
            question=data.question,
            history=history,
            provider_id=data.provider_id,
            model_name=data.model_name,
            task_id=data.task_id,
            allowed_tool_kinds=data.allowed_tool_kinds,
        )
        return R.success(data=result)

    except TaskCancelled:
        clear_task(data.task_id)
        logger.info(f"Harness 问答被用户取消 (task_id={data.task_id})")
        return R.success(data={
            "answer": "已按你的要求停止本轮回答。",
            "sources": [], "intent": "search", "tool_calls": [],
            "cancelled": True,
        })
    except ValueError as e:
        return R.error(msg=str(e))
    except Exception as e:
        logger.error(f"Harness 问答失败: {e}", exc_info=True)
        return R.error(msg=f"问答失败: {str(e)}")


@router.post("/chat/ask/harness/stream")
def ask_harness_stream(data: HarnessAskRequest):
    """流式 Harness 问答（SSE）：思考链 / 工具事件 / 正文增量实时推送。

    事件流（每行 `data: {json}`）：
      status   — 阶段标记（routing / thinking / …）
      thinking — 思考链增量（模型 reasoning_content，支持 GLM / DeepSeek 等）
      tool     — 工具调用完成事件（名称 / 成败 / 受限标记 / 耗时）
      delta    — 正文增量
      final    — 完整结果（与非流式 /chat/ask/harness 的 data 同构）
      error    — 上游报错透传（如 AutoClaw 的「高峰限流 810002」「免费额度用尽」）

    连接错误或生成器内部异常也以 error 事件收尾——前端永远能拿到可解释的失败。
    """
    history = [{"role": m.role, "content": m.content} for m in data.history]
    t0 = time.time()

    def _gen():
        try:
            for event in chat_harness_stream(
                question=data.question,
                history=history,
                provider_id=data.provider_id,
                model_name=data.model_name,
                task_id=data.task_id,
                allowed_tool_kinds=data.allowed_tool_kinds,
            ):
                yield f"data: {_json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001 —— 断流前把原因告诉前端
            logger.error(f"SSE 生成器异常: {e}", exc_info=True)
            yield f"data: {_json.dumps({'type': 'error', 'message': f'连接中断: {str(e)[:150]}'}, ensure_ascii=False)}\n\n"
        finally:
            logger.info(f"[chat] ask_harness_stream 耗时 {time.time() - t0:.1f}s（流结束）")

    logger.info(f"[chat] ask_harness_stream 耗时 {time.time() - t0:.1f}s")
    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx 反代场景禁缓冲
        },
    )


class RouteIntentRequest(BaseModel):
    message: str
    links: list[str] = []
    provider_id: str
    model_name: str


@router.post("/chat/route_intent")
def route_intent_api(data: RouteIntentRequest):
    """高阶意图路由（Premium）：LLM 判断消息意图与待解析链接。

    任何失败返回 intent=auto_fallback，前端回退机械规则路径。
    """
    t0 = time.time()
    try:
        from app.services.intent_router import route_intent
        result = route_intent(
            message=data.message,
            links=data.links,
            provider_id=data.provider_id,
            model_name=data.model_name,
        )
        logger.info(f"[chat] route_intent 耗时 {time.time() - t0:.1f}s")
        return R.success(data=result)
    except Exception as e:
        logger.info(f"[chat] route_intent 耗时 {time.time() - t0:.1f}s")
        return R.success(data={"intent": "auto_fallback", "links": [],
                               "reason": str(e)[:120]})


@router.post("/chat/cancel")
def cancel_agent(data: AskRequest):
    """取消进行中的 Agent 问答（按 task_id 匹配单笔记问答会话）。"""
    if not data.task_id:
        return R.error(msg="该问答未绑定任务，无法取消")
    cancel_task(data.task_id)
    return R.success({"task_id": data.task_id, "status": "CANCELLING"})


# ── 旧版问答（向后兼容）───────────────────────

@router.post("/chat/ask/legacy")
def ask_legacy(data: AskRequest):
    """
    旧版单笔记 RAG 问答（保持向后兼容）。
    需要提供 task_id。
    """
    if not data.task_id:
        return R.error(msg="旧版问答需要提供 task_id")

    try:
        history = [
            {"role": m.role, "content": m.content}
            for m in data.history
        ]

        result = chat_legacy(
            task_id=data.task_id,
            question=data.question,
            history=history,
            provider_id=data.provider_id,
            model_name=data.model_name,
        )
        return R.success(data=result)

    except ValueError as e:
        return R.error(msg=str(e))
    except Exception as e:
        logger.error(f"旧版问答失败: {e}", exc_info=True)
        return R.error(msg=f"问答失败: {str(e)}")


# ── 知识库管理 ────────────────────────────────

@router.get("/chat/kb/stats")
def kb_stats():
    """获取统一知识库统计信息。"""
    try:
        kb = UnifiedKnowledgeBase()
        return R.success(data=kb.get_stats())
    except Exception as e:
        logger.error(f"获取知识库统计失败: {e}")
        return R.error(msg=str(e))


@router.post("/chat/kb/rebuild")
def kb_rebuild():
    """从 ChromaDB 重建 BM25 索引（修复不一致时使用）。"""
    try:
        kb = UnifiedKnowledgeBase()
        count = kb.rebuild_bm25()
        return R.success(data={"rebuilt": count}, msg=f"BM25 索引已重建: {count} 文档")
    except Exception as e:
        logger.error(f"重建 BM25 失败: {e}")
        return R.error(msg=str(e))
