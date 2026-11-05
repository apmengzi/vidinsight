"""
联网搜索服务。
为 ReAct Agent 提供 web_search 能力，支持 Exa 与 You.com 两个后端。

- Exa（默认）：api.exa.ai 国内可直连，语义搜索质量高，返回 highlights。
- You.com：需经本机代理（HTTPS_PROXY 或显式 proxy），返回 SSE 流式 JSON-RPC。

配置来源（按优先级）：
1. 环境变量 WEB_SEARCH_PROVIDER=exa|you
2. 环境变量 EXA_API_KEY / YDC_API_KEY
3. config/web_search.json（与 downloader.json 同目录）
"""
from __future__ import annotations

import json
import os
from typing import Any

from app.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_TIMEOUT = 25.0


def _load_config() -> dict:
    """读取 config/web_search.json，不存在则返回空 dict。"""
    try:
        from app.utils.path_helper import get_runtime_path

        path = get_runtime_path("config", "web_search.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception as e:  # 配置缺失不应阻断搜索能力
        logger.warning(f"[web_search] 读取配置失败: {e}")
    return {}


def _resolve_settings() -> dict:
    """合并配置与环境变量，确定 provider / key / proxy。"""
    cfg = _load_config()
    provider = (
        os.getenv("WEB_SEARCH_PROVIDER")
        or cfg.get("provider")
        or "exa"
    ).strip().lower()

    exa_key = os.getenv("EXA_API_KEY") or cfg.get("exa_api_key") or ""
    ydc_key = os.getenv("YDC_API_KEY") or cfg.get("ydc_api_key") or ""
    proxy = (
        os.getenv("WEB_SEARCH_PROXY")
        or cfg.get("proxy")
        or ""
    ).strip()

    return {
        "provider": provider,
        "exa_api_key": exa_key,
        "ydc_api_key": ydc_key,
        "proxy": proxy,
        "num_results": int(cfg.get("num_results", 6)),
    }


def is_configured() -> bool:
    """是否有可用凭据（供路由层判断是否暴露 web_search 工具）。"""
    s = _resolve_settings()
    if s["provider"] == "you":
        return bool(s["ydc_api_key"])
    return bool(s["exa_api_key"])


def _post(url: str, headers: dict, payload: dict, proxy: str | None, timeout: float) -> str:
    """POST 并返回文本。proxy 为空时直连且忽略环境代理变量。"""
    import httpx

    kwargs: dict[str, Any] = {
        "headers": headers,
        "timeout": timeout,
        "trust_env": False,  # 不让系统代理变量干扰显式配置
    }
    if proxy:
        kwargs["proxy"] = proxy

    with httpx.Client(**kwargs) as client:
        resp = client.post(url, json=payload)
        resp.raise_for_status()
        return resp.text


def _search_exa(query: str, num_results: int, settings: dict) -> dict:
    payload = {
        "query": query,
        "type": "auto",
        "numResults": num_results,
        "contents": {"highlights": True},
    }
    text = _post(
        "https://api.exa.ai/search",
        {"x-api-key": settings["exa_api_key"], "Content-Type": "application/json"},
        payload,
        settings["proxy"] or None,
        DEFAULT_TIMEOUT,
    )
    data = json.loads(text)
    results = []
    for item in data.get("results", []):
        highlights = item.get("highlights") or []
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "published": item.get("publishedDate", ""),
            "snippet": "\n".join(highlights)[:1200],
            "author": item.get("author", ""),
        })
    return {"provider": "exa", "results": results}


def _search_you(query: str, num_results: int, settings: dict) -> dict:
    """调用 You.com MCP 的 you-search（JSON-RPC over SSE）。"""
    proxy = settings["proxy"] or os.getenv("HTTPS_PROXY") or None
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "you-search", "arguments": {"query": query}},
    }
    text = _post(
        "https://api.you.com/mcp",
        {
            "Authorization": f"Bearer {settings['ydc_api_key']}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        payload,
        proxy,
        DEFAULT_TIMEOUT + 10,
    )

    # SSE: 取最后一条带 result 的 data 行
    raw = ""
    for line in text.splitlines():
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if '"result"' in chunk:
                raw = chunk
    if not raw:
        raise RuntimeError("You.com 未返回结果")

    data = json.loads(raw)
    content = data.get("result", {}).get("content", [])
    inner = ""
    for c in content:
        if c.get("type") == "text":
            inner = c.get("text", "")
    if not inner:
        return {"provider": "you", "results": []}

    parsed = json.loads(inner)
    web = (parsed.get("results") or {}).get("web", [])
    results = []
    for item in web[:num_results]:
        highlights = ((item.get("contents") or {}).get("highlights") or [])
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "published": item.get("page_age", ""),
            "snippet": "\n".join(highlights)[:1200] or (item.get("description") or "")[:1200],
        })
    return {"provider": "you", "results": results}


def web_search(query: str, num_results: int | None = None) -> dict:
    """执行联网搜索，返回统一结构。失败时抛出异常由调用方兜底。"""
    settings = _resolve_settings()
    n = num_results or settings["num_results"]
    n = max(1, min(int(n), 10))

    if settings["provider"] == "you":
        if not settings["ydc_api_key"]:
            raise RuntimeError("未配置 YDC_API_KEY")
        return _search_you(query, n, settings)

    if not settings["exa_api_key"]:
        raise RuntimeError("未配置 EXA_API_KEY")
    return _search_exa(query, n, settings)
