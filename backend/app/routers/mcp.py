# -*- coding: utf-8 -*-
"""MCP server：把 VidInsight 暴露成 Agent 客户端可调用的工具集。

对标同类项目 video-to-note 的 MCP 能力（2026-10-24 用户指定学习），并放大
我们的多平台/批量优势：Cherry Studio、Claude Desktop、Codex 等 MCP 客户端
配置 SSE 端点 http://<host>:8483/mcp/sse 后，Agent 对话里贴视频链接即可
直接生成笔记、查进度、解析元数据、保存模型凭证。

设计约束：
- 增量挂载（main.py app.mount("/mcp", ...)），不触碰任何现有路由
- 工具实现走本机 loopback HTTP 调 /api/*，零逻辑重复、零状态分叉
- 凭证保存只接受用户在对话中主动提供的 key（工具入参），绝不自动登录
"""
from __future__ import annotations

import logging
import os

import httpx
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

_mcp = FastMCP(
    "VidInsight",
    instructions=(
        "VidInsight——AI 视频笔记智能体。把 B 站/小红书/抖音/快手/YouTube/本地视频"
        "链接变成结构化 Markdown 笔记。典型用法：用户贴链接 → generate_note 提交 →"
        " get_task_status 轮询（SUCCESS 后 result 里有完整笔记）；想先看一眼内容用"
        " parse_link；需要换模型先 save_provider_key。"
    ),
)

_BASE = f"http://127.0.0.1:{os.getenv('BACKEND_PORT', '8483')}"
# 默认生成参数与 BoardPage 批量同款（免费通道，稳定）
_DEFAULT_MODEL = {"model_name": "deepseek-v4.1-flash", "provider_id": "workbuddy"}


@_mcp.tool()
async def generate_note(video_url: str, model_name: str = "", provider_id: str = "",
                        style: str = "adaptive") -> str:
    """提交视频/笔记链接，生成 Markdown 洞察笔记。

    支持 B站、小红书、抖音、快手、YouTube 链接。返回 task_id，用 get_task_status 查进度。
    model_name/provider_id 留空则用默认免费模型；style 可选 adaptive（自适应结构，推荐）。
    """
    body = {
        "video_url": video_url,
        "platform": _detect_platform(video_url),
        "quality": "fast",
        "model_name": model_name or _DEFAULT_MODEL["model_name"],
        "provider_id": provider_id or _DEFAULT_MODEL["provider_id"],
        "style": style,
        "format": ["link", "summary"],
    }
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{_BASE}/api/generate_note", json=body)
        d = r.json()
    if d.get("code") != 0:
        return f"提交失败：{d.get('msg', r.status_code)}"
    tid = d["data"]["task_id"]
    return f"已提交，task_id={tid}。请用 get_task_status 查询（每 5 秒一次，视频转写约 1-5 分钟）。"


@_mcp.tool()
async def get_task_status(task_id: str) -> str:
    """查询笔记生成进度。status 可能是 PENDING/PARSING/DOWNLOADING/TRANSCRIBING/
    SUMMARIZING/SUCCESS/FAILED；SUCCESS 时直接返回笔记正文。"""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{_BASE}/api/task_status/{task_id}")
        d = r.json().get("data") or {}
    status = d.get("status", "UNKNOWN")
    if status == "SUCCESS":
        result = d.get("result") or {}
        md = result.get("markdown") or ""
        title = (result.get("audio_meta") or {}).get("title", "")
        return f"SUCCESS《{title}》\n\n{md[:12000]}"
    msg = d.get("message") or ""
    stage = d.get("stage_progress")
    return f"status={status}" + (f" 阶段进度 {stage}%" if stage else "") + (f"（{msg}）" if msg else "")


@_mcp.tool()
async def parse_link(url: str) -> str:
    """贴链接即解析：秒级返回标题/正文/作者/互动数等元数据（不下载不生成）。
    小红书视频/图文通吃；其他平台返回基础元数据。生成完整笔记请用 generate_note。"""
    platform = _detect_platform(url)
    path = "/api/xhs/parse" if platform == "xiaohongshu" else "/api/parse"
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post(f"{_BASE}{path}", json={"video_url": url})
        d = (r.json().get("data") or {})
    if platform == "xiaohongshu":
        if not d.get("success"):
            return f"解析失败：{d.get('reason', '未知原因')}"
        return (
            f"类型：{d.get('note_type')}｜标题：{d.get('title')}\n"
            f"正文：{(d.get('desc') or '')[:800]}\n"
            f"作者：{(d.get('author') or {}).get('nickname', '')}｜赞 {d.get('likes')}｜收藏 {d.get('collected')}"
        )
    preview = d.get("data") or d
    return f"标题：{preview.get('title', '')}\n简介：{(preview.get('description') or '')[:400]}"


@_mcp.tool()
async def save_provider_key(provider_id: str, api_key: str, base_url: str = "") -> str:
    """保存/更新模型供应商的 API Key（用户在对话中主动提供时调用）。
    provider_id 如 deepseek/zhipu/qwen/gemini/groq；base_url 仅自建中转时需要。"""
    async with httpx.AsyncClient(timeout=30) as c:
        r1 = await c.get(f"{_BASE}/api/get_provider_by_id/{provider_id}")
        cur = (r1.json().get("data") or {})
        if not cur:
            return f"未知供应商 {provider_id}，可用值见 /api/get_all_providers"
        body = {**cur, "api_key": api_key}
        if base_url:
            body["base_url"] = base_url
        r2 = await c.post(f"{_BASE}/api/update_provider", json=body)
    return f"已保存 {provider_id} 的 API Key" if r2.json().get("code") == 0 else "保存失败"


def _detect_platform(url: str) -> str:
    u = (url or "").lower()
    if "bilibili.com" in u or "b23.tv" in u:
        return "bilibili"
    if "xiaohongshu.com" in u or "xhslink." in u:
        return "xiaohongshu"
    if "douyin.com" in u:
        return "douyin"
    if "kuaishou.com" in u:
        return "kuaishou"
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    return "local"


def mcp_asgi_app():
    """供 main.py 挂载的 ASGI app（SSE 传输）。"""
    return _mcp.sse_app()
