# -*- coding: utf-8 -*-
"""hellotik 直链解析（本地浏览器版）——不依赖外部 Chrome/CDP。

与 hellotik_cdp.py 的区别：
    cdp 版：connect_over_cdp 连宿主机 Chrome，需要人工开着 --remote-debugging-port
    本模块：Playwright 直接启动本地 Chromium，headless 即可通过 hellotik 的
           gate 校验（2026-10-28 实测：headless=True 返回 status=0，无需 headed）

为什么 headless 也能过：
    hellotik 的 gate 只校验"真实浏览器会话指纹"（UA/JS 环境/cookie），
    不检测 headless 特征。加 --disable-blink-features=AutomationControlled
    与真实 UA 即可。

部署要求：
    Ubuntu 宿主 venv: /home/hello/.venv-hellotik
    安装: pip install playwright cryptography && playwright install chromium
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

HELLOTIK_PAGE = "https://www.hellotik.app/zh"
RESP_CBC_KEY = b"93838338562359368888868323563256"
_CUSTOM = "ZYXABCDEFGHIJKLMNOPQRSTUVWzyxabcdefghijklmnopqrstuvw9876543210-_"
_STD = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_B64_TABLE = str.maketrans(_CUSTOM, _STD)

CDN_ALLOW = ("bilivideo.com", "hdslb.com", "biliapi.com", "akamaized.net")

# 真实浏览器指纹（headless 也能过 gate 的关键）
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _deobfuscate(b64s: str) -> bytes:
    raw = base64.b64decode(b64s).decode("latin1")
    raw = "".join(chr(ord(c) ^ 0x5A) for c in raw)
    raw = "".join(raw[i:i + 8][::-1] for i in range(0, len(raw), 8))
    raw = raw.translate(_B64_TABLE)
    raw += "=" * (-len(raw) % 4)
    return base64.b64decode(raw)


def _decrypt(resp: dict) -> dict:
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


def parse_direct_url(video_url: str, timeout_s: float = 60.0) -> dict:
    """B 站链接 → hellotik 无水印直链（本地 headless Chromium）。

    返回 {"success": bool, "video_url", "title", "cover", "reason"}。
    直链带签名有时效，调用方必须立即下载。
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return {"success": False, "reason": "playwright_missing"}

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                      "--disable-dev-shm-usage"],
            )
            ctx = browser.new_context(
                user_agent=_UA,
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
            )
            page = ctx.new_page()
            captured: dict = {}

            def _on_resp(r):
                try:
                    if r.url.rstrip("/").endswith("/api/parse") and "entitlement" not in r.url:
                        captured["resp"] = r
                except Exception:
                    pass

            page.on("response", _on_resp)
            try:
                page.goto(HELLOTIK_PAGE, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(4000)

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

                data = _decrypt(resp.json())
                direct = (data.get("url") or "").strip()
                if not direct or not any(d in direct for d in CDN_ALLOW):
                    return {"success": False, "reason": "no_cdn_url"}

                title = (data.get("title") or "").split("|||")[0].strip()
                logger.info("hellotik(local) ok: %s -> %s", title[:40], direct[:80])
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
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:  # noqa: BLE001
        logger.warning("hellotik(local) failed: %s", str(e)[:180])
        return {"success": False, "reason": str(e)[:180]}
