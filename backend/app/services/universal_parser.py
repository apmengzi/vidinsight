"""
全平台「贴链接即预览」统一解析层 —— VidInsight 的 hellotik 式秒级预览，六平台一个入口。

设计（2026-09-28 全平台实测后定稿）：
每个平台都是「主通道 + 兜底通道」双保险，不把宝押在单一引擎上：

  平台        主通道（生成/下载用）              预览通道（本文件，秒级）
  ─────────────────────────────────────────────────────────────────
  bilibili    yt-dlp + cookiefile（产品在用）    yt-dlp -J + 同款 cookiefile  ✅实测
  youtube     yt-dlp + 代理（产品在用）          yt-dlp -J + 同款代理        （产品已验证）
  xiaohongshu 自研 SSR（视频+图文，登录态）       get_note_structured          ✅实测（视频匿名可通）
  douyin      自研签名 abogus                    yt-dlp -J（需新鲜 cookie）   ⚠️实测：无 cookie 报结构化错误
  kuaishou    自研 requests                      无快速通道 → 诚实返回 limited ❌实测：extractor 不认 short-video 形态
  local       本地文件                           不适用
  其他        ——                                 yt-dlp generic 兜底（1000+ 站点的"全平台"精神）

统一失败契约：{success, reason, hint...}，绝不裸抛（同 harness gate）。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from typing import Any, Optional

import yt_dlp

from app.downloaders.xiaohongshu_downloader import XiaoHongShuDownloader
from app.services.proxy_config_manager import ProxyConfigManager
from app.services.cookie_manager import CookieConfigManager
from app.utils.logger import get_logger

logger = get_logger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")

# 走 yt-dlp 预览的平台（kuaishou 实测 extractor 不认 short-video 形态，不白跑 20s）
_YTDLP_PLATFORMS = {"bilibili", "youtube", "douyin"}


def _fail(reason: str, **kw: Any) -> dict:
    payload = {"success": False, "reason": reason}
    payload.update(kw)
    return payload


def _write_netscape_cookie(cookie: str, domain: str) -> Optional[str]:
    """cookie 字符串 → Netscape 格式临时文件（复用 bilibili_downloader 同款逻辑）。"""
    if not cookie:
        return None
    lines = ["# Netscape HTTP Cookie File\n"]
    for pair in cookie.split("; "):
        if "=" in pair:
            key, value = pair.split("=", 1)
            lines.append(f".{domain}\tTRUE\t/\tFALSE\t0\t{key}\t{value}\n")
    fd, path = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    return path


def _ydl_opts_for(platform: str) -> dict:
    """各平台产品同款 yt-dlp 选项：bilibili/youtube/douyin 各自的 cookie 与代理。"""
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 20,
        "noplaylist": True,
    }
    cookie_mgr = CookieConfigManager()
    if platform == "bilibili":
        path = _write_netscape_cookie(cookie_mgr.get("bilibili"), "bilibili.com")
        if path:
            opts["cookiefile"] = path
    elif platform == "youtube":
        proxy = ProxyConfigManager().get_proxy_url()
        if proxy:
            opts["proxy"] = proxy
    elif platform == "douyin":
        # 2026-09-28 实测：抖音 extractor 明确要「新鲜 cookie（无需登录）」；
        # 用户配了就带上，没配就让它报结构化错误（下方统一捕获）
        path = _write_netscape_cookie(cookie_mgr.get("douyin"), "douyin.com")
        if path:
            opts["cookiefile"] = path
    return opts


def _ytdlp_metadata(url: str, platform: str) -> dict:
    """yt-dlp 取元数据（python API，skip_download），返回统一 schema 或结构化失败。"""
    opts = _ydl_opts_for(platform)
    cookiefile = opts.get("cookiefile")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:  # noqa: BLE001 —— 上游 extractor 的任何报错都收敛为结构化失败
        msg = str(e)
        hint = ""
        if "Fresh cookies" in msg or "cookies" in msg.lower() and platform == "douyin":
            hint = ("抖音需要新鲜 Cookie（浏览器访问一次抖音网页版即可获得，无需登录）；"
                    "或在设置页「下载配置→抖音」配置后重试")
        elif "412" in msg or "Precondition" in msg:
            hint = "触发了平台反爬，请在设置页配置该平台 Cookie 后重试"
        elif "Unsupported URL" in msg:
            hint = "该链接形态暂不支持预览，可直接提交生成（走平台专用下载通道）"
        return _fail(f"解析失败: {msg[:180]}", platform=platform, engine="yt-dlp", hint=hint)

    if not info:
        return _fail("未取得元数据", platform=platform, engine="yt-dlp")

    formats = []
    for f in (info.get("formats") or []):
        if f.get("url"):
            formats.append({
                "format_id": f.get("format_id"),
                "ext": f.get("ext"),
                "height": f.get("height"),
                "vsize": f.get("filesize") or f.get("filesize_approx"),
                "url": f["url"],
            })
    formats.sort(key=lambda x: (x.get("height") or 0), reverse=True)

    return {
        "success": True,
        "platform": platform,
        "engine": "yt-dlp",
        "url": url,
        "webpage_url": info.get("webpage_url") or url,
        "title": (info.get("title") or "")[:200],
        "duration_s": info.get("duration"),
        "uploader": info.get("uploader") or info.get("channel"),
        "description": (info.get("description") or "")[:2000],
        "cover": info.get("thumbnail"),
        "tags": (info.get("tags") or [])[:15],
        "is_video": True,
        "formats": formats[:8],
    }


def _from_xhs_structured(xhs: dict, url: str) -> dict:
    """把 xiaohongshu_downloader.get_note_structured 的结果归一到统一 schema。"""
    if not xhs.get("success"):
        xhs.setdefault("platform", "xiaohongshu")
        xhs.setdefault("engine", "ssr")
        return xhs
    video_url = xhs.get("video_url") or ""
    images = xhs.get("images") or []
    return {
        "success": True,
        "platform": "xiaohongshu",
        "engine": "ssr",
        "url": url,
        "webpage_url": xhs.get("source_url") or url,
        "title": xhs.get("title", ""),
        "duration_s": None,
        "uploader": (xhs.get("author") or {}).get("nickname", ""),
        "description": xhs.get("desc", ""),
        "cover": (images[0]["url"] if images else ""),
        "tags": xhs.get("tags") or [],
        "is_video": bool(video_url),
        "formats": ([{"format_id": "master", "ext": "mp4", "height": None,
                      "vsize": None, "url": video_url}] if video_url else []),
        # xhs 专属富字段（预览卡可直接用）
        "note_type": xhs.get("note_type"),
        "images": images,
        "likes": xhs.get("likes"),
        "collected": xhs.get("collected"),
        "comments": xhs.get("comments"),
        "ip_location": xhs.get("ip_location"),
    }


def parse_link(url: str) -> dict:
    """全平台统一预览入口：任何支持平台的链接 → 秒级结构化元数据。

    路由：小红书 → 自研 SSR（富字段）；bilibili/youtube/douyin → yt-dlp（产品同款
    cookie/代理）；kuaishou → 诚实返回 limited（无快速通道，走生成管线）；
    未知平台 → yt-dlp generic 兜底（1000+ 站点）。
    """
    raw = (url or "").strip()
    if not raw:
        return _fail("链接为空")

    if not re.match(r"https?://", raw):
        return _fail("不是有效的链接", url=raw[:120])

    platform = _detect(raw)

    # B站链接完整性预检：BV 号标准为 BV+10 位。长度异常时（复制截断的典型特征）
    # 提前给出可行动提示，而不是让 yt-dlp 报一条"视频不存在"的天书误导用户
    if platform == "bilibili":
        m = re.search(r"BV([0-9A-Za-z]+)", raw)
        if m and len(m.group(1)) != 10 and "b23.tv" not in raw:
            got = len(m.group(1))
            return _fail(
                f"BV 号长度异常（{got} 位，标准为 10 位）——链接很可能复制时被截断",
                platform="bilibili",
                hint="请回到视频页，从浏览器地址栏重新完整复制（BV 开头共 12 个字符）",
            )

    if platform == "xiaohongshu":
        try:
            xhs = XiaoHongShuDownloader().get_note_structured(raw)
        except Exception as e:  # noqa: BLE001 —— 下载器内部已收敛，这里只兜意外
            xhs = _fail(f"解析失败: {str(e)[:160]}", platform="xiaohongshu")
        if xhs.get("success"):
            return _from_xhs_structured(xhs, raw)
        # SSR 未取到（无登录态/token 失效是常态）：视频笔记用 yt-dlp 兜底——
        # 2026-09-27 实测 yt-dlp 对小红书视频匿名可通，预览不依赖登录态。
        fallback = _ytdlp_metadata(raw, "xiaohongshu")
        if fallback.get("success"):
            fallback["engine"] = "yt-dlp"
            fallback["note"] = "SSR 主通道未取到（建议配置登录态以获取图文富字段），本结果来自 yt-dlp 兜底"
            return fallback
        # 两条通道都失败：返回 SSR 的教学式失败（hint 更可行动），补齐统一字段
        xhs.setdefault("platform", "xiaohongshu")
        xhs.setdefault("engine", "ssr+yt-dlp")
        return xhs

    if platform == "kuaishou":
        return _fail(
            "快手暂无秒级预览通道（平台反爬限制）",
            platform="kuaishou",
            limited=True,
            hint="可直接提交生成 —— 生成管线走快手专用下载通道，功能不受影响",
        )

    # 平台风控冷却预检：冷却期内不再消耗请求（由 412 分类器触发，自动过期）
    from app.services.platform_guard import cooldown_remaining
    remain = cooldown_remaining(platform)
    if remain > 0:
        return _fail(
            f"平台风控冷却中（约 {int(remain)} 秒后自动恢复）",
            platform=platform,
            hint="刚被平台限流，立即重试会加重限制；稍候片刻再提交即可",
        )

    if platform in _YTDLP_PLATFORMS:
        return _ytdlp_metadata(raw, platform)

    # 未知平台：yt-dlp generic 兜底 —— "全平台"的精神：引擎认识的链接都能看一眼
    result = _ytdlp_metadata(raw, "other")
    if result.get("success"):
        result["hint"] = "该平台不在产品支持列表内，可预览但生成笔记需在支持的平台"
    else:
        result.setdefault("hint", "支持的平台：bilibili / youtube / douyin / kuaishou / xiaohongshu")
    return result


def _detect(url: str) -> str:
    """平台识别（与 produce_tools.detect_platform 同源，另认 xhslink 双域）。"""
    u = url.lower()
    if re.search(r"bilibili\.com|b23\.tv", u):
        return "bilibili"
    if re.search(r"youtube\.com|youtu\.be", u):
        return "youtube"
    if re.search(r"xiaohongshu\.com|xhslink\.", u):
        return "xiaohongshu"
    if "douyin.com" in u or "iesdouyin.com" in u:
        return "douyin"
    if re.search(r"kuaishou\.com|chenzhongtech\.com", u):
        return "kuaishou"
    return "other"
