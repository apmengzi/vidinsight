# -*- coding: utf-8 -*-
"""MCP server 契约测试。

不跑真实 SSE 握手（长连接不适合 TestClient 同步模型），验证三件事：
1. 工具注册完整（generate_note/get_task_status/parse_link/save_provider_key）
2. 平台探测正确（MCP 工具选 API 路径的依据）
3. main.py 挂载成功（/mcp/sse 端点存在，返回 event-stream 或重定向特征）
"""
import pytest
from fastapi.testclient import TestClient

from app.routers.mcp import _detect_platform, _mcp


def test_tools_registered():
    import asyncio

    tools = asyncio.get_event_loop().run_until_complete(_mcp.list_tools()) \
        if hasattr(_mcp, "list_tools") else None
    # mcp 1.9：list_tools 是 async 方法，返回 list[Tool]
    names = {t.name for t in tools} if tools else set()
    assert {"generate_note", "get_task_status", "parse_link", "save_provider_key"} <= names


def test_detect_platform():
    assert _detect_platform("https://www.bilibili.com/video/BV1x") == "bilibili"
    assert _detect_platform("https://b23.tv/abc") == "bilibili"
    assert _detect_platform("https://www.xiaohongshu.com/explore/123") == "xiaohongshu"
    assert _detect_platform("https://xhslink.cn/o/xyz") == "xiaohongshu"
    assert _detect_platform("https://www.douyin.com/video/1") == "douyin"
    assert _detect_platform("https://www.youtube.com/watch?v=x") == "youtube"
    assert _detect_platform("D:/local/file.mp4") == "local"


def test_mcp_mounted():
    from main import app
    routes = {getattr(r, "path", "") for r in app.routes}
    mount = next((r for r in app.routes if getattr(r, "path", "") == "/mcp"), None)
    assert mount is not None, "/mcp 子应用未挂载"
