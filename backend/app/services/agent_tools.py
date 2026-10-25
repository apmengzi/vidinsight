"""
Agent 工具集合。
提供 search_kb、get_source、list_notes、clarify 等工具，
供 ReAct Agent 在推理循环中调用。
"""
import json
import os
from typing import Optional

from app.services.knowledge_base import UnifiedKnowledgeBase
from app.utils.logger import get_logger
from app.utils.path_helper import get_runtime_path

logger = get_logger(__name__)

NOTE_OUTPUT_DIR = os.getenv("NOTE_OUTPUT_DIR", get_runtime_path("note_results"))

# ═══════════════════════════════════════════════════
# 工具定义（OpenAI function calling 格式）
# ═══════════════════════════════════════════════════

AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_kb",
            "description": (
                "搜索全局洞察库，在所有已索引的竞品视频分析记录中跨视频查找相关内容。"
                "支持按来源类型过滤：meta(视频信息)、transcript(转录原文)、markdown(内容洞察报告)。"
                "适用场景：查找竞品卖点、比较用户痛点、搜索高频话术和选题方向。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索查询，用自然语言描述要查找的内容",
                    },
                    "source_type": {
                        "type": "string",
                        "enum": ["all", "meta", "transcript", "markdown"],
                        "description": "来源类型过滤，默认 all",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回结果数量，默认 5，最大 10",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_source",
            "description": (
                "获取特定分析记录的完整内容。适用场景：深入理解某个竞品视频、"
                "获取完整转录或完整内容洞察报告。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "分析记录的任务 ID（从 search_kb 结果中获取）",
                    },
                    "content_type": {
                        "type": "string",
                        "enum": ["full", "transcript", "markdown", "meta"],
                        "description": "内容类型：full=全部, transcript=转录, markdown=洞察报告, meta=元信息",
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_notes",
            "description": (
                "列出洞察库中所有已索引的内容分析记录摘要。"
                "适用场景：用户问「有哪些分析记录」、了解洞察库覆盖范围。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clarify",
            "description": (
                "当用户的问题不够明确、有多种理解方式、或缺少关键信息时，"
                "向用户提出澄清问题。不要猜测用户意图，直接询问。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "向用户提出的澄清问题",
                    },
                },
                "required": ["question"],
            },
        },
    },
]


# ═══════════════════════════════════════════════════
# 工具执行器
# ═══════════════════════════════════════════════════

class ToolExecutor:
    """Agent 工具执行器，负责实际执行工具调用并返回结果。"""

    def __init__(self, kb: UnifiedKnowledgeBase):
        self.kb = kb

    def execute(self, tool_name: str, arguments: dict) -> str:
        """执行工具调用，返回 JSON 字符串。"""
        try:
            if tool_name == "search_kb":
                return self._search_kb(arguments)
            elif tool_name == "get_source":
                return self._get_source(arguments)
            elif tool_name == "list_notes":
                return self._list_notes()
            elif tool_name == "clarify":
                return self._clarify(arguments)
            else:
                return json.dumps(
                    {"error": f"未知工具: {tool_name}"}, ensure_ascii=False,
                )
        except Exception as e:
            logger.error(f"工具执行失败 [{tool_name}]: {e}")
            return json.dumps(
                {"error": f"工具执行失败: {str(e)}"}, ensure_ascii=False,
            )

    # ── search_kb ──────────────────────────────

    def _search_kb(self, args: dict) -> str:
        query = args.get("query", "")
        source_type = args.get("source_type", "all")
        top_k = min(args.get("top_k", 5), 10)

        if not query.strip():
            return json.dumps({"error": "查询不能为空"}, ensure_ascii=False)

        results = self.kb.search(
            query=query,
            top_k=max(top_k, 8),
            rerank_top_k=top_k,
            source_filter=None if source_type == "all" else source_type,
        )

        formatted = []
        for i, doc in enumerate(results):
            formatted.append({
                "index": i,
                "citation": doc.get("citation", ""),
                "text": doc["text"][:600],
                "metadata": {
                    "task_id": doc.get("metadata", {}).get("task_id", ""),
                    "video_title": doc.get("metadata", {}).get("video_title", ""),
                    "video_url": doc.get("metadata", {}).get("video_url", ""),
                    "source_type": doc.get("metadata", {}).get("source_type", ""),
                    "section_title": doc.get("metadata", {}).get("section_title"),
                    "start_time": doc.get("metadata", {}).get("start_time"),
                    "end_time": doc.get("metadata", {}).get("end_time"),
                },
            })

        return json.dumps({
            "query": query,
            "total": len(formatted),
            "results": formatted,
        }, ensure_ascii=False)

    # ── get_source ─────────────────────────────

    def _get_source(self, args: dict) -> str:
        task_id = args.get("task_id", "")
        content_type = args.get("content_type", "full")

        note_path = os.path.join(NOTE_OUTPUT_DIR, f"{task_id}.json")
        if not os.path.exists(note_path):
            return json.dumps(
                {"error": f"分析记录不存在: {task_id}"}, ensure_ascii=False,
            )

        with open(note_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        result: dict = {"task_id": task_id}

        if content_type in ("full", "meta"):
            am = data.get("audio_meta", {})
            raw = am.get("raw_info", {}) or {}
            result["meta"] = {
                "title": am.get("title") or raw.get("title", ""),
                "uploader": raw.get("uploader", ""),
                "description": (raw.get("description", "") or "")[:500],
                "tags": (raw.get("tags", []) or [])[:10],
                "duration_seconds": am.get("duration", 0),
                "platform": am.get("platform", ""),
                "url": raw.get("webpage_url", ""),
            }

        if content_type in ("full", "markdown"):
            md = data.get("markdown", "")
            if isinstance(md, list):
                md = md[-1].get("content", "") if md else ""
            result["markdown"] = md[:5000]

        if content_type in ("full", "transcript"):
            segments = data.get("transcript", {}).get("segments", [])
            result["transcript"] = {
                "total_segments": len(segments),
                "preview": [
                    {
                        "start": round(s.get("start", 0), 1),
                        "end": round(s.get("end", 0), 1),
                        "text": s.get("text", ""),
                    }
                    for s in segments[:50]
                ],
            }

        return json.dumps(result, ensure_ascii=False)

    # ── list_notes ─────────────────────────────

    def _list_notes(self) -> str:
        notes = self.kb.get_all_notes()
        return json.dumps({
            "total": len(notes),
            "notes": notes,
        }, ensure_ascii=False)

    # ── clarify ────────────────────────────────

    def _clarify(self, args: dict) -> str:
        return json.dumps({
            "action": "clarify",
            "question": args.get("question", ""),
        }, ensure_ascii=False)
