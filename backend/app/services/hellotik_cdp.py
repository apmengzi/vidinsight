# -*- coding: utf-8 -*-
"""hellotik 直链解析（Web 版）——通过 CDP 驱动宿主机 Chrome 访问 hellotik.app。

为什么这样设计：
    桌面版走 Tauri 的 fetch_hellotik_direct（内嵌 WebView 操作 hellotik 页面）。
    Web 部署没有 Tauri，但 B 站 412/503 风控必须靠 hellotik 绕开，否则整条
    生成管线在服务器 IP 上不可用。这里用 CDP 连接宿主机上**已登录、可正常
    访问 B 站**的 Chrome，把同样的真人操作搬过去。

前置条件（部署时一次性配置）：
    1. 宿主机 Chrome 以 --remote-debugging-port=9333 启动
    2. 该 Chrome 能正常访问 www.hellotik.app 与 www.bilibili.com
    3. 环境变量 HELLOTIK_CDP_PORT（默认 9333）、HELLOTIK_CDP_HOST（默认 127.0.0.1）

失败契约：任何异常都返回 {"success": false, "reason": ...}，不抛给上层，
调用方按「无兜底」处理，行为与以前一致。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

CDP_HOST = os.getenv("HELLOTIK_CDP_HOST", "127.0.0.1")
CDP_PORT = os.getenv("HELLOTIK_CDP_PORT", "9333")
HELLOTIK_PAGE = "https://www.hellotik.app/zh"

# 与 hellotik_api.py 保持同一套解密参数
RESP_CBC_KEY = b"93838338562359368888868323563256"
_CUSTOM = "ZYXABCDEFGHIJKLMNOPQRSTUVWzyxabcdefghijklmnopqrstuvw9876543210-_"
_STD = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_B64_TABLE = str.maketrans(_CUSTOM, _STD)

CDN_ALLOW = ("bilivideo.com", "hdslb.com", "biliapi.com", "akamaized.net")


def _deobfuscate(b64s: str) -> bytes:
    raw = base64.b64decode(b64s).decode("latin1")
    raw = "".join(chr(ord(c) ^ 0x5A) for c in raw)
    raw = "".join(raw[i:i + 8][::-1] for i in range(0, len(raw), 8))
    raw = raw.translate(_B64_TABLE)
    raw += "=" * (-len(raw) % 4)
    return base64.b64decode(raw)


def _decrypt(resp: dict) -> dict:
    """解密 /api/parse 响应；被截断时用正则抢救关键字段。"""
    if resp.get("status") != 0:
        raise ValueError(f"status={resp.get('status')}")
    if not resp.get("encrypt"):
        return resp.get("data") or {}
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    ct = _deobfuscate(resp["data"])
    iv = _deobfuscate(resp["key"])
    dec = Cipher(algorithms.AES(RESP_CBC_KEY), modes.CBC(iv)).decryptor()
    pt = dec.update(ct) + dec.finalize()
    pt = pt[:-pt[-1]]
    txt = pt.decode("utf-8", errors="ignore")
    try:
        return json.loads(txt)
    except Exception:
        import re
        def g(pat):
            m = re.search(pat, txt)
            return m.group(1) if m else ""
        return {
            "title": g(r'"title"\s*:\s*"([^"]*)"'),
            "type": g(r'"type"\s*:\s*"([^"]*)"'),
            "url": g(r'"url"\s*:\s*"(https:[^"]*)"'),
            "cover": g(r'"cover"\s*:\s*"(https:[^"]*)"'),
            "_truncated": True,
        }


def _cdp_ws_url() -> Optional[str]:
    """找一个可用的 page target 的 WebSocket 调试地址。"""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{CDP_HOST}:{CDP_PORT}/json/list", timeout=8) as r:
            targets = json.load(r)
        for t in targets:
            if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                return t["webSocketDebuggerUrl"]
        return None
    except Exception as e:
        logger.warning("hellotik cdp list failed: %s", str(e)[:120])
        return None


def parse_direct_url(video_url: str, timeout_s: float = 45.0) -> dict:
    """B 站链接 → hellotik 无水印直链。

    返回 {"success": bool, "video_url", "title", "cover", "reason"}。
    直链带签名有时效，调用方必须立即下载。
    """
    ws_url = _cdp_ws_url()
    if not ws_url:
        return {"success": False, "reason": "cdp_unavailable"}

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return {"success": False, "reason": "playwright_missing"}

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://{CDP_HOST}:{CDP_PORT}")
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            try:
                page.goto(HELLOTIK_PAGE, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3500)

                # connect_over_cdp 下没有 wait_for_response，改用事件监听
                captured = {}
                def _on_resp(r):
                    try:
                        if r.url.rstrip("/").endswith("/api/parse") and "parse-entitlement" not in r.url:
                            captured["resp"] = r
                    except Exception:
                        pass
                page.on("response", _on_resp)

                fill = page.evaluate(
                    """(u) => {
                        const i = document.querySelector('textarea,input[type=text],input:not([type])');
                        if (!i) return 'NO_INPUT';
                        const proto = i.tagName === 'TEXTAREA'
                          ? Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,'value').set
                          : Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
                        proto.call(i, u);
                        i.dispatchEvent(new Event('input',{bubbles:true}));
                        const btn = document.querySelector('button[type=submit]') ||
                          [...document.querySelectorAll('button')].filter(x=>/解析|parse/i.test(x.textContent||''))[0];
                        if (!btn) return 'NO_BUTTON';
                        btn.click();
                        return 'CLICKED';
                    }""", video_url)
                if fill != "CLICKED":
                    return {"success": False, "reason": f"fill_{fill}"}

                deadline = time.time() + timeout_s
                while time.time() < deadline and "resp" not in captured:
                    page.wait_for_timeout(500)
                resp = captured.get("resp")
                if resp is None:
                    return {"success": False, "reason": "no_parse_response"}
                if resp.status != 200:
                    return {"success": False, "reason": f"http_{resp.status}"}
                body = resp.json()
                data = _decrypt(body)

                direct = (data.get("url") or "").strip()
                if not direct or not any(d in direct for d in CDN_ALLOW):
                    return {"success": False, "reason": "no_cdn_url"}

                title = (data.get("title") or "").split("|||")[0].strip()
                logger.info("hellotik ok: %s -> %s", title[:40], direct[:80])
                return {
                    "success": True,
                    "video_url": direct,
                    "title": title,
                    "cover": data.get("cover") or "",
                    "platform": "bilibili",
                }
            finally:
                try:
                    page.close()
                except Exception:
                    pass
    except Exception as e:  # noqa: BLE001
        logger.warning("hellotik parse failed: %s", str(e)[:180])
        return {"success": False, "reason": str(e)[:180]}
