# -*- coding: utf-8 -*-
"""YouTube VPS 中继：ssh 远程 yt-dlp 按需解析（无常驻服务）。

为什么：hellotik 上游取不到 YouTube（UPSTREAM_PARSE_FAILED 实测定性，
客户端出口无法影响）；本地 yt-dlp 依赖 Clash 代理。美区 VPS（instabox，
ssh 免密已配）上的 yt-dlp 是第三条独立通道——Clash 挂了/被限流时兜底，
零部署（ssh 即鉴权，无常驻进程，沙盒环境友好）。

用法（youtube_downloader 主通道失败时调用）：
    from app.services.yt_vps_relay import extract_via_vps
    meta = extract_via_vps(url)  # None=中继不可用
    # {"title","duration","thumbnail","audio_url","video_url","subtitles":{lang:[{url,...}]}}
环境变量：YT_VPS_HOST（默认 instabox1，~/.ssh/config 已配）
          YT_VPS_ENABLED=0 关闭
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

_VPS_HOST = os.getenv("YT_VPS_HOST", "instabox1")
_YTDLP_PATH = "~/ytrelay/yt-dlp"
_ALLOWED = re.compile(r"^https?://(www\.youtube\.com/watch\?|youtu\.be/|m\.youtube\.com/watch\?)", re.I)


def extract_via_vps(url: str, timeout: int = 120) -> Optional[dict]:
    """经 VPS 远程执行 yt-dlp -J，返回规整元数据；不可用/失败返回 None。"""
    if os.getenv("YT_VPS_ENABLED", "1") == "0":
        return None
    if not _ALLOWED.match((url or "").strip()):
        return None
    try:
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
             _VPS_HOST,
             f'{_YTDLP_PATH} -J --socket-timeout 20 --no-warnings "{url}"'],
            capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            logger.warning("yt vps relay failed: %s", (r.stderr or "")[-200:])
            return None
        meta = json.loads(r.stdout)
    except Exception as e:  # noqa: BLE001 — 兜底通道失败不上抛
        logger.warning("yt vps relay error: %s", str(e)[:150])
        return None

    audio = video = None
    for f in meta.get("formats") or []:
        if not f.get("url"):
            continue
        if f.get("acodec") != "none" and f.get("vcodec") == "none":
            # 转写只需要音轨：同质量取最小码率省带宽
            if audio is None or (f.get("abr") or 0) < (audio.get("abr") or 99):
                audio = f
        elif f.get("vcodec") != "none":
            if video is None or (f.get("height") or 0) > (video.get("height") or 0):
                video = f
    return {
        "title": meta.get("title") or "",
        "duration": meta.get("duration") or 0,
        "thumbnail": meta.get("thumbnail") or "",
        "uploader": meta.get("uploader") or "",
        "audio_url": (audio or {}).get("url") or "",
        "video_url": (video or {}).get("url") or "",
        "subtitles": {k: v for k, v in (meta.get("subtitles") or {}).items()
                      if k in ("zh-Hans", "zh", "zh-Hant", "en")},
    }


def fetch_url_via_vps(url: str, timeout: int = 30) -> Optional[str]:
    """经 VPS 取任意 youtube.com 资源（字幕 vtt 等）——本机无法直连 YouTube 时的
    唯一路径。仅限 youtube 域，返回文本内容。"""
    if os.getenv("YT_VPS_ENABLED", "1") == "0":
        return None
    if "youtube.com" not in url and "youtu.be" not in url and "googlevideo.com" not in url:
        return None
    try:
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", _VPS_HOST,
             f'curl -s -m {timeout} "{url}"'],
            capture_output=True, timeout=timeout + 15, encoding="utf-8", errors="replace")
        return r.stdout if r.returncode == 0 and r.stdout else None
    except Exception as e:  # noqa: BLE001
        logger.warning("vps fetch url error: %s", str(e)[:120])
        return None
