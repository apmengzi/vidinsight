import os
import logging
from abc import ABC
from typing import Union, Optional, List

import yt_dlp

from app.downloaders.base import Downloader, DownloadQuality
from app.downloaders.dl_opts import apply_download_opts, get_download_proxy
from app.downloaders.youtube_subtitle import YouTubeSubtitleFetcher
from app.models.notes_model import AudioDownloadResult
from app.models.transcriber_model import TranscriptResult
from app.services.proxy_config_manager import ProxyConfigManager
from app.utils.path_helper import get_data_dir
from app.utils.url_parser import extract_video_id

logger = logging.getLogger(__name__)


def _apply_proxy(ydl_opts: dict) -> dict:
    """YouTube 在国内需要代理。配置了全局代理就塞进 yt-dlp opts。

    DOWNLOAD_PROXY_URL 优先于全局代理配置——它专供"只让下载流量走 VPS 中继"
    的场景（LLM/转写仍走原链路）。两者都没配才不加 proxy。
    """
    proxy = get_download_proxy() or ProxyConfigManager().get_proxy_url()
    if proxy:
        ydl_opts['proxy'] = proxy
        logger.info(f"yt-dlp 走代理: {proxy}")
    return ydl_opts


class YoutubeDownloader(Downloader, ABC):
    def __init__(self):

        super().__init__()

    def download(
        self,
        video_url: str,
        output_dir: Union[str, None] = None,
        quality: DownloadQuality = "fast",
        need_video: Optional[bool] = False,
        skip_download: bool = False,
    ) -> AudioDownloadResult:
        if output_dir is None:
            output_dir = get_data_dir()
        if not output_dir:
            output_dir = self.cache_data
        os.makedirs(output_dir, exist_ok=True)

        output_path = os.path.join(output_dir, "%(id)s.%(ext)s")

        ydl_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
            'outtmpl': output_path,
            'noplaylist': True,
            'quiet': False,
        }

        # 分块/并发/下载代理（默认关；BILI_DL_* / DOWNLOAD_PROXY_URL 可覆盖）
        apply_download_opts(ydl_opts)

        if skip_download:
            ydl_opts['skip_download'] = True

        _apply_proxy(ydl_opts)
        ydl_opts = self.attach_progress(ydl_opts)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=not skip_download)
            video_id = info.get("id")
            title = info.get("title")
            duration = info.get("duration", 0)
            cover_url = info.get("thumbnail")
            ext = info.get("ext", "m4a")
            audio_path = os.path.join(output_dir, f"{video_id}.{ext}")

        return AudioDownloadResult(
            file_path=audio_path,
            title=title,
            duration=duration,
            cover_url=cover_url,
            platform="youtube",
            video_id=video_id,
            raw_info={'tags': info.get('tags')},
            video_path=None,
        )

    def download_video(
        self,
        video_url: str,
        output_dir: Union[str, None] = None,
    ) -> str:
        """
        下载视频，返回视频文件路径
        """
        if output_dir is None:
            output_dir = get_data_dir()
        video_id = extract_video_id(video_url, "youtube")
        video_path = os.path.join(output_dir, f"{video_id}.mp4")
        if os.path.exists(video_path):
            return video_path
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, "%(id)s.%(ext)s")

        ydl_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]',
            'outtmpl': output_path,
            'noplaylist': True,
            'quiet': False,
            'merge_output_format': 'mp4',  # 确保合并成 mp4
        }

        # YouTube 是 DASH 多分段，并发分段对视频轨+音频轨都有意义
        apply_download_opts(ydl_opts)
        _apply_proxy(ydl_opts)
        ydl_opts = self.attach_progress(ydl_opts)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
            video_id = info.get("id")
            video_path = os.path.join(output_dir, f"{video_id}.mp4")

        if not os.path.exists(video_path):
            raise FileNotFoundError(f"视频文件未找到: {video_path}")

        return video_path

    def download_subtitles(self, video_url: str, output_dir: str = None,
                           langs: List[str] = None) -> Optional[TranscriptResult]:
        """
        通过 YouTube InnerTube API 直接获取字幕（优先人工字幕，其次自动生成）。
        比 yt_dlp 方式更轻量，无需写临时文件到磁盘。

        :param video_url: 视频链接
        :param output_dir: 未使用（保留接口兼容）
        :param langs: 优先语言列表
        :return: TranscriptResult 或 None
        """
        if langs is None:
            langs = ['zh-Hans', 'zh', 'zh-CN', 'zh-TW', 'en', 'en-US', 'ja']

        video_id = extract_video_id(video_url, "youtube")
        fetcher = YouTubeSubtitleFetcher()
        print(
            f"尝试获取字幕，video_id={video_id}, langs={langs}"
        )
        result = fetcher.fetch_subtitles(video_id, langs)
        if result is not None:
            return result

        # 本机网络拿不到字幕（Clash 掉线/被限流）时，经美区 VPS 远程 yt-dlp
        # 兜底取字幕（ssh 按需执行，无常驻服务；YT_VPS_ENABLED=0 可关）
        try:
            from app.services.yt_vps_relay import extract_via_vps
            meta = extract_via_vps(video_url)
        except Exception:  # noqa: BLE001 — 兜底通道异常不上抛
            meta = None
        if not meta:
            return None
        import requests as _requests
        from app.models.transcriber_model import TranscriptSegment  # noqa: F401 — 结构见下方
        for lang in langs:
            track = (meta.get("subtitles") or {}).get(lang)
            if not track:
                continue
            try:
                # yt-dlp 字幕轨道里[0]是 vtt；同语言自动字幕排在手动后，优先手动
                chosen = track[-1] if isinstance(track, list) else track
                url = chosen.get("url") if isinstance(chosen, dict) else chosen
                # vtt 在 youtube.com 域上，本机（无代理时）取不到——同样经 VPS 拉
                from app.services.yt_vps_relay import fetch_url_via_vps
                vtt = fetch_url_via_vps(url)
                segments = self._vtt_to_segments(vtt)
                if segments:
                    logger.info("VPS 中继取到 YouTube 字幕（%s，%d 段）", lang, len(segments))
                    return TranscriptResult(language=lang,
                                            full_text=" ".join(s.text for s in segments),
                                            segments=segments)
            except Exception as e:  # noqa: BLE001
                logger.warning("VPS 字幕轨道 %s 解析失败: %s", lang, str(e)[:120])
        return None

    @staticmethod
    def _vtt_to_segments(vtt: str) -> list:
        """WebVTT → TranscriptSegment 列表（简单解析：时间轴行 + 文本行）。"""
        from app.models.transcriber_model import TranscriptSegment
        import re as _re
        segs = []
        blocks = _re.split(r"\n\s*\n", vtt)
        for b in blocks:
            lines = [l for l in b.splitlines() if l.strip() and not l.startswith(("WEBVTT", "Kind:", "Language:"))]
            if not lines:
                continue
            m = None
            for l in lines:
                m = _re.match(r"(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})\.(\d{3})", l)
                if m:
                    break
            if not m:
                continue
            g = [int(x) for x in m.groups()]
            start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
            end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
            texts = [l for l in lines if "-->" not in l]
            text = " ".join(texts).strip()
            if text and not text.startswith("<"):
                segs.append(TranscriptSegment(start=start, end=end, text=text))
        # 合并相邻同文（vtt 滚动重复）
        merged = []
        for s in segs:
            if merged and merged[-1].text == s.text:
                merged[-1].end = s.end
            else:
                merged.append(s)
        return merged
