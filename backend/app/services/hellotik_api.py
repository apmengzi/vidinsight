# -*- coding: utf-8 -*-
"""hellotik 解析通道（协议逆向版）：小红书 + B 站。

2026-10-24 逆向结论（chunks 6189 + 2261 + rednote page，全文见 out/hellotik_*.js）：

请求侧（页面构造，纯 HTTP 复现被 gate 反制——服务端只给真实浏览器会话登记票据，
   非浏览器会话的票据在 parse 阶段一律 INVALID_ENVELOPE）：
   POST /api/parse-entitlement-status → quoteToken(JWT, requestHash=SHA256(requestURL))
   POST /api/gate-e5eea8              → {tk, sd, ex(120s)}
   POST /api/parse                    → 明文 JSON（含 quoteToken + 身份字段）经
      AES-GCM 加密，密钥 = SHA-256(f"{tk}:{sd}")，字段 tk/pl/iv/vr

响应侧（本模块可独立处理，与浏览器无关）：
   {status:0, encrypt:true, data, key} → data/key 各过三层混淆后 AES-CBC 解密：
     混淆 = XOR 0x5A → 每 8 字符组内反转 → 自定义 base64 字母表映射；
     key 字段实为 IV；CBC 密钥 = 前端硬编码 "93838338562359368888868323563256"
   解密后 {"title": "标题 |||正文(含话题)", "type": "video|images", "url": 视频直链,
     "videos":[{url, video_fullinfo:[{url,size,type}]}], "pics":[图片直链], "cover": ...}

配额：每 uwx_id 身份每日 10 次（累计 30 次后降到 6、80 后 5）；gate 另有 IP 级
   频控（429，约 20 分钟自愈）。批量驱动须按身份轮换浏览器 context。
"""
from __future__ import annotations

import base64
import json
import logging
import time
from typing import Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger(__name__)

RESP_CBC_KEY = b"93838338562359368888868323563256"  # chunk 2261 硬编码
_CUSTOM = "ZYXABCDEFGHIJKLMNOPQRSTUVWzyxabcdefghijklmnopqrstuvw9876543210-_"
_STD = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_B64_TABLE = str.maketrans(_CUSTOM, _STD)
PAGE_URL_REDNOTE = "https://www.hellotik.app/zh/rednote"
PAGE_URL_HOME = "https://www.hellotik.app/zh"  # B 站等其余平台走主页（2026-10-24 实测）
# 每身份单轮解析上限（官方 10/天，留余量；批量场景轮换 context 计数重置）
MAX_PER_IDENTITY = 8


def hellotik_page_for(url: str) -> str:
    """按目标链接选 hellotik 解析页：小红书有专属页，其余（B 站）走主页。"""
    u = (url or "").lower()
    if "xiaohongshu.com" in u or "xhslink." in u:
        return PAGE_URL_REDNOTE
    return PAGE_URL_HOME


def deobfuscate(b64s: str) -> bytes:
    """hellotik 响应字段三层去混淆：XOR 0x5A → 8 字符组内反转 → 字母表映射 → b64decode"""
    raw = base64.b64decode(b64s).decode("latin1")
    raw = "".join(chr(ord(c) ^ 0x5A) for c in raw)
    raw = "".join(raw[i:i + 8][::-1] for i in range(0, len(raw), 8))
    raw = raw.translate(_B64_TABLE)
    raw += "=" * (-len(raw) % 4)
    return base64.b64decode(raw)


def decrypt_parse_response(resp: dict) -> dict:
    """解密 /api/parse 的 200 响应体，返回结构化笔记数据。"""
    if resp.get("status") != 0:
        raise ValueError(f"hellotik parse status={resp.get('status')}")
    if not resp.get("encrypt"):
        return resp.get("data") or {}
    ct = deobfuscate(resp["data"])
    iv = deobfuscate(resp["key"])
    dec = Cipher(algorithms.AES(RESP_CBC_KEY), modes.CBC(iv)).decryptor()
    pt = dec.update(ct) + dec.finalize()
    pt = pt[:-pt[-1]]  # PKCS7
    return json.loads(pt.decode("utf-8"))


def split_title(raw_title: str) -> tuple[str, str]:
    """hellotik 的 title = '标题 |||正文(含话题标签)'，拆成 (标题, 正文)。"""
    if "|||" in (raw_title or ""):
        t, desc = raw_title.split("|||", 1)
        return t.strip(), desc.strip()
    return (raw_title or "").strip(), ""


def normalize_note(data: dict) -> dict:
    """规整成与 xiaohongshu_downloader.get_note_structured 兼容的结构。"""
    title, desc = split_title(data.get("title") or "")
    video_url = (data.get("url") or "").strip()
    pics = [u for u in (data.get("pics") or []) if u]
    return {
        "note_id": data.get("note_id") or "",
        "title": title,
        "desc": desc,
        "note_type": "video" if video_url else ("images" if pics else "text"),
        "video_url": video_url,
        "images": pics,
        "cover": data.get("cover") or (pics[0] if pics else ""),
        "video_qualities": [
            {"url": v.get("url") or "", "size": v.get("size") or 0, "type": v.get("type") or ""}
            for entry in (data.get("videos") or [])
            for v in (entry.get("video_fullinfo") or [])
        ],
    }


class HelloTikPlaywrightDriver:
    """Playwright 驱动 hellotik 页面解析（UI 触发，页面自己构造请求绕过会话校验）。

    依赖本机 playwright + 真实 Chrome（channel='chrome'；headless 会被
    auto_script 检测，必须 headed，窗口移到屏外不干扰用户）。
    批量场景用 parse_many；单条用 parse_note。
    """

    def __init__(self, chrome_channel: str = "chrome"):
        self._chrome_channel = chrome_channel
        self._pw = None

    def _ensure_playwright(self):
        if self._pw is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
        return self._pw

    def parse_many(self, urls: list[str], on_result=None, interval_s: float = 4.0):
        """批量解析。on_result(url, normalized_note|None, error) 逐条回调。

        身份配额自动轮换：每 MAX_PER_IDENTITY 条换一个浏览器 context（新身份）。
        页面失败（INVALID_ENVELOPE/429）自动重载并重置计数。
        支持混平台：按每条链接自动选 /zh/rednote 或 /zh 主页（跨页时重载）。
        """
        pw = self._ensure_playwright()
        browser = pw.chromium.launch(
            channel=self._chrome_channel, headless=False,
            args=["--disable-blink-features=AutomationControlled",
                  "--window-position=32000,32000"])
        try:
            ctx = browser.new_context()
            page = ctx.newPage()
            current_page_url = ""
            used = 0
            for i, url in enumerate(urls):
                target = hellotik_page_for(url)
                need_reload = (target != current_page_url) or used >= MAX_PER_IDENTITY
                if need_reload:
                    ctx.close()
                    ctx = browser.new_context()
                    page = ctx.newPage()
                    self._load(page, target)
                    current_page_url = target
                    used = 0
                try:
                    resp = self._parse_one(page, url)
                    note = normalize_note(decrypt_parse_response(resp))
                    used += 1
                    if on_result:
                        on_result(url, note, None)
                except Exception as e:  # noqa: BLE001 — 单条失败不中断批次
                    logger.warning("hellotik parse fail %s: %s", url[:80], str(e)[:120])
                    if "INVALID" in str(e) or "429" in str(e):
                        self._load(page, target)
                        used = 0
                    if on_result:
                        on_result(url, None, str(e)[:200])
                time.sleep(interval_s)
        finally:
            browser.close()

    def parse_note(self, url: str) -> dict:
        """单条解析（同步，独立开浏览器，适合低频兜底）。"""
        pw = self._ensure_playwright()
        browser = pw.chromium.launch(
            channel=self._chrome_channel, headless=False,
            args=["--disable-blink-features=AutomationControlled",
                  "--window-position=32000,32000"])
        try:
            page = browser.newContext().newPage()
            self._load(page, hellotik_page_for(url))
            return normalize_note(decrypt_parse_response(self._parse_one(page, url)))
        finally:
            browser.close()

    @staticmethod
    def _load(page, page_url):
        page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3500)

    @staticmethod
    def _parse_one(page, url: str) -> dict:
        """填框 + 点解析 + 抓 /api/parse 响应（精确路径匹配，别撞 entitlement）。"""
        resp_future = page.wait_for_response(
            lambda r: r.url.rstrip("/").endswith("/api/parse"), timeout=40000)
        fill = page.evaluate(
            """(u) => {
                const i = document.querySelector('textarea,input[type=text],input:not([type])');
                if (!i) return 'NO_INPUT';
                const proto = i.tagName === 'TEXTAREA'
                  ? Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set
                  : Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                proto.call(i, u);
                i.dispatchEvent(new Event('input', { bubbles: true }));
                const btn = document.querySelector('button[type=submit]') ||
                  [].slice.call(document.querySelectorAll('button'))
                      .filter(x => /解析/.test(x.textContent || ''))[0];
                if (btn) { btn.click(); return 'CLICKED'; }
                return 'NO_BUTTON';
            }""", url)
        if fill != "CLICKED":
            raise RuntimeError(f"hellotik fill failed: {fill}")
        resp = resp_future.value if hasattr(resp_future, "value") else resp_future
        if resp.status != 200:
            raise RuntimeError(f"hellotik http {resp.status}")
        body = resp.json()
        if body.get("status") != 0:
            raise RuntimeError(f"hellotik {body.get('code') or body.get('reason')}")
        return body


def parse_note_via_hellotik(video_url: str) -> Optional[dict]:
    """对外入口：小红书/B站链接 → 结构化数据（hellotik 通道）。

    供 xiaohongshu_downloader 在 SSR 通道失败（风控 400101 等）时兜底。
    返回 normalize_note 结构；通道不可用（无 playwright/Chrome）返回 None，
    调用方按无兜底处理。
    """
    try:
        return HelloTikPlaywrightDriver().parse_note(video_url)
    except Exception as e:  # noqa: BLE001 — 兜底通道失败不上抛
        logger.warning("hellotik fallback unavailable: %s", str(e)[:150])
        return None
