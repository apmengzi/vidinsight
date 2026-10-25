import os
import json
import hashlib
import logging
import tempfile
import time
from abc import ABC
from typing import Union, Optional, List

import requests
import yt_dlp

from app.services.platform_guard import mark_cooldown, clear_cooldown

from app.downloaders.base import Downloader, DownloadQuality, QUALITY_MAP
from app.downloaders.bilibili_subtitle import BilibiliSubtitleFetcher
from app.downloaders.dl_opts import apply_audio_opts, apply_download_opts
from app.models.notes_model import AudioDownloadResult
from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.utils.path_helper import get_data_dir
from app.utils.url_parser import extract_video_id
from app.services.cookie_manager import CookieConfigManager
# ffmpeg_helper 在 backend/ 根（与 main.py 同级），不是 app 包内的模块
from ffmpeg_helper import ensure_ffmpeg_or_raise

logger = logging.getLogger(__name__)


class BilibiliDownloader(Downloader, ABC):
    def __init__(self):
        super().__init__()
        self._cookie_mgr = CookieConfigManager()
        self._cookie = self._cookie_mgr.get('bilibili')
        self._cookiefile = self._write_netscape_cookie_file()

    def _refresh_cookie(self):
        """每次下载前重读 Cookie 配置——下载器是启动时创建的单例，
        若只在 __init__ 读一次，用户保存新 Cookie 后必须重启才能生效。"""
        cookie = self._cookie_mgr.get('bilibili')
        if cookie != self._cookie:
            logger.info("检测到 B站 Cookie 配置已更新，刷新 Cookie 文件")
            self._cookie = cookie
            self._cookiefile = self._write_netscape_cookie_file()

    def _write_netscape_cookie_file(self) -> Optional[str]:
        """将 Cookie 写入 Netscape 格式临时文件，返回文件路径（供 yt-dlp cookiefile 使用）"""
        if not self._cookie:
            logger.warning("B站 Cookie 未配置，下载可能失败")
            return None
        lines = ["# Netscape HTTP Cookie File\n"]
        for pair in self._cookie.split("; "):
            if "=" in pair:
                key, value = pair.split("=", 1)
                lines.append(f".bilibili.com\tTRUE\t/\tFALSE\t0\t{key}\t{value}\n")
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8')
        tmp.writelines(lines)
        tmp.close()
        logger.info("已生成 B站 Netscape Cookie 文件: %s (条目: %d)", tmp.name, len(lines) - 1)
        return tmp.name

    def download(
        self,
        video_url: str,
        output_dir: Union[str, None] = None,
        quality: DownloadQuality = "fast",
        need_video:Optional[bool]=False
    ) -> AudioDownloadResult:
        self._refresh_cookie()
        if output_dir is None:
            output_dir = get_data_dir()
        if not output_dir:
            output_dir=self.cache_data
        os.makedirs(output_dir, exist_ok=True)

        output_path = os.path.join(output_dir, "%(id)s.%(ext)s")

        ydl_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
            'outtmpl': output_path,
            'http_headers': {'Referer': 'https://www.bilibili.com'},
            'noplaylist': True,
            'quiet': False,
        }
        # mp3 抽取 + 16kHz 单声道降采样（转码 3.5s → 1s，转写引擎无损）
        apply_audio_opts(ydl_opts)
        # 传输层参数（分块/并发/下载代理）：默认全关，实测单连接最优；
        # BILI_DL_CHUNK_MB / BILI_DL_CONC_FRAG / DOWNLOAD_PROXY_URL 可覆盖
        apply_download_opts(ydl_opts)
        if self._cookiefile:
            ydl_opts['cookiefile'] = self._cookiefile
        ydl_opts = self.attach_progress(ydl_opts)

        # B站风控翻译 + 412 自动重试：yt-dlp 被风控拦截时报的
        # "deleted or geo-restricted" 具有误导性（视频可能明明存在），
        # 必须结合状态码与 cookie 状态分类，不能原样透传给用户
        max_attempts = 2
        info = None
        for attempt in range(1, max_attempts + 1):
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(video_url, download=True)
                clear_cooldown("bilibili")  # 成功即证明风控缓解，解除冷却
                break
            except yt_dlp.utils.DownloadError as e:
                msg = str(e)
                if "412" in msg or "Precondition" in msg:
                    mark_cooldown("bilibili")  # 平台级冷却：防止连环请求加重限流
                    if attempt < max_attempts:
                        logger.warning(f"B站风控限流(412)，30 秒后自动重试（{attempt}/{max_attempts}）: {msg[:120]}")
                        time.sleep(30)
                        continue
                    raise Exception(
                        "B站风控限流（请求过于频繁或 Cookie 失效）。已自动重试仍失败："
                        "请稍等 1-2 分钟再提交；若反复出现，请到「设置 → Cookie」更新 B 站 Cookie"
                    ) from e
                if "deleted or geo-restricted" in msg:
                    if not self._cookiefile:
                        raise Exception(
                            "无法确认视频状态：未配置 B 站 Cookie，请求可能被平台风控拦截。"
                            "请到「设置 → Cookie」配置后再试"
                        ) from e
                    raise Exception(
                        "视频不存在或已下架（已带 Cookie 验证）。请检查链接："
                        "BV 号应为 BV+10 位字符，从浏览器地址栏完整复制"
                    ) from e
                if "404" in msg or "Not Found" in msg:
                    raise Exception(
                        "视频不存在：链接可能不完整（BV 号应为 BV+10 位）或视频已删除"
                    ) from e
                raise

        video_id = info.get("id")
        title = info.get("title")
        duration = info.get("duration", 0)
        cover_url = info.get("thumbnail")
        audio_path = os.path.join(output_dir, f"{video_id}.mp3")

        return AudioDownloadResult(
            file_path=audio_path,
            title=title,
            duration=duration,
            cover_url=cover_url,
            platform="bilibili",
            video_id=video_id,
            raw_info=info,
            video_path=None  # ❗音频下载不包含视频路径
        )

    def download_direct(
        self,
        direct_url: str,
        video_url: str,
        output_dir: Union[str, None] = None,
        title: Optional[str] = None,
        cover_url: Optional[str] = None,
    ) -> AudioDownloadResult:
        """用现成的无水印直链取片（hellotik 兜底通道专用）。

        为什么存在：B 站 412 风控会把 yt-dlp 拦成"视频不存在"，此时唯一还能
        拿到源片的路子就是别人已经解析好的 CDN 直链。直链带签名有有效期，
        必须**立刻**下载落盘，不能存起来慢慢用。

        只做两件事：HTTP 流式落盘 → ffmpeg 抽音频。没有任何爬取行为，
        等同于浏览器点"下载视频"。
        """
        if output_dir is None:
            output_dir = get_data_dir()
        os.makedirs(output_dir, exist_ok=True)

        video_id = extract_video_id(video_url, "bilibili") or hashlib.md5(
            direct_url.encode("utf-8")
        ).hexdigest()[:12]
        video_path = os.path.join(output_dir, f"{video_id}.mp4")
        audio_path = os.path.join(output_dir, f"{video_id}.mp3")

        # 直链带 deadline 签名，可能已过期 → 失效就明确告诉用户重新解析
        if not os.path.exists(video_path):
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.bilibili.com",
            }
            try:
                with requests.get(direct_url, headers=headers, stream=True, timeout=60) as resp:
                    if resp.status_code in (403, 404):
                        raise Exception(
                            "直链已失效（签名过期）。请重新点「第三方网页兜底解析」再提交"
                        )
                    resp.raise_for_status()
                    with open(video_path, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=1024 * 256):
                            if chunk:
                                f.write(chunk)
            except requests.RequestException as e:
                if os.path.exists(video_path):
                    os.remove(video_path)
                raise Exception(f"直链下载失败：{e}") from e

        if not os.path.exists(audio_path):
            import subprocess

            ensure_ffmpeg_or_raise()  # 会把 ffmpeg 所在目录写进 PATH，之后直接按名字调用
            subprocess.run(
                ["ffmpeg", "-y", "-i", video_path, "-vn",
                 "-ar", "16000", "-ac", "1", "-b:a", "64k", audio_path],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        duration = self._probe_duration(video_path)
        return AudioDownloadResult(
            file_path=audio_path,
            title=title or "",
            duration=duration,
            cover_url=cover_url,
            platform="bilibili",
            video_id=video_id,
            raw_info={"engine": "hellotik_direct", "video_path": video_path},
            video_path=video_path,
        )

    @staticmethod
    def _probe_duration(video_path: str) -> float:
        """用 ffprobe 读时长；读不到就返回 0，绝不因为元数据拿不到而失败。"""
        try:
            import subprocess

            ensure_ffmpeg_or_raise()
            out = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_format", video_path],
                capture_output=True, text=True, timeout=30,
            ).stdout
            return float(json.loads(out)["format"]["duration"])
        except Exception:
            return 0.0

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
        os.makedirs(output_dir, exist_ok=True)
        print("video_url",video_url)
        video_id=extract_video_id(video_url, "bilibili")
        video_path = os.path.join(output_dir, f"{video_id}.mp4")
        if os.path.exists(video_path):
            return video_path

        # 检查是否已经存在


        output_path = os.path.join(output_dir, "%(id)s.%(ext)s")

        ydl_opts = {
            'format': 'bv*[ext=mp4]/bestvideo+bestaudio/best',
            'outtmpl': output_path,
            'http_headers': {'Referer': 'https://www.bilibili.com'},
            'noplaylist': True,
            'quiet': False,
            'merge_output_format': 'mp4',  # 确保合并成 mp4
        }
        # 视频是 DASH 多分段（视频轨 + 音频轨），并发分段在这里才真正生效
        apply_download_opts(ydl_opts)
        if self._cookiefile:
            ydl_opts['cookiefile'] = self._cookiefile
        ydl_opts = self.attach_progress(ydl_opts)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
            video_id = info.get("id")
            video_path = os.path.join(output_dir, f"{video_id}.mp4")

        if not os.path.exists(video_path):
            raise FileNotFoundError(f"视频文件未找到: {video_path}")

        return video_path

    def delete_video(self, video_path: str) -> str:
        """
        删除视频文件
        """
        if os.path.exists(video_path):
            os.remove(video_path)
            return f"视频文件已删除: {video_path}"
        else:
            return f"视频文件未找到: {video_path}"

    def download_subtitles(self, video_url: str, output_dir: str = None,
                           langs: List[str] = None) -> Optional[TranscriptResult]:
        """
        尝试获取B站视频字幕

        :param video_url: 视频链接
        :param output_dir: 输出路径
        :param langs: 优先语言列表
        :return: TranscriptResult 或 None
        """
        # 1) 优先走 B 站官方 player API（直拉，无需下视频；AI 字幕需 SESSDATA cookie）
        try:
            fetcher = BilibiliSubtitleFetcher()
            result = fetcher.fetch_subtitles(video_url)
            if result and result.segments:
                return result
            # 取不到时记录原因：need_login 与 no_subtitle 对用户的含义完全不同
            # （前者配个 Cookie 就能秒出字幕，后者只能老老实实转写）
            self.last_subtitle_status = fetcher.last_status
            if fetcher.last_status == "need_login":
                logger.info("字幕需登录态：已记录 hint，将在状态里提示用户配置 B站 Cookie")
        except Exception as e:
            logger.warning(f"player API 直拉字幕异常，回退到 yt-dlp: {e}")

        # 2) Fallback：原 yt-dlp 路径（更脆弱，遇到签名/Cookie 问题失败概率较高）
        if output_dir is None:
            output_dir = get_data_dir()
        if not output_dir:
            output_dir = self.cache_data
        os.makedirs(output_dir, exist_ok=True)

        if langs is None:
            langs = ['zh-Hans', 'zh', 'zh-CN', 'ai-zh', 'en', 'en-US']

        video_id = extract_video_id(video_url, "bilibili")

        ydl_opts = {
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': langs,
            'subtitlesformat': 'srt/json3/best',  # 支持多种格式
            'skip_download': True,
            'outtmpl': os.path.join(output_dir, f'{video_id}.%(ext)s'),
            'quiet': True,
        }

        # 通过 CookieConfigManager 注入 B站 Cookie（Netscape cookiefile）
        if self._cookiefile:
            ydl_opts['cookiefile'] = self._cookiefile
            ydl_opts['http_headers'] = {'Referer': 'https://www.bilibili.com'}

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_url, download=True)

                # 查找下载的字幕文件
                subtitles = info.get('requested_subtitles') or {}
                if not subtitles:
                    logger.info(f"B站视频 {video_id} 没有可用字幕")
                    return None

                # 按优先级查找字幕
                detected_lang = None
                sub_info = None
                for lang in langs:
                    if lang in subtitles:
                        detected_lang = lang
                        sub_info = subtitles[lang]
                        break

                # 如果按优先级没找到，取第一个可用的（排除弹幕）
                if not detected_lang:
                    for lang, info_item in subtitles.items():
                        if lang != 'danmaku':  # 排除弹幕
                            detected_lang = lang
                            sub_info = info_item
                            break

                if not sub_info:
                    logger.info(f"B站视频 {video_id} 没有可用字幕（排除弹幕）")
                    return None

                # 检查是否有内嵌数据（yt-dlp 有时直接返回字幕内容）
                if 'data' in sub_info and sub_info['data']:
                    logger.info(f"直接从返回数据解析字幕: {detected_lang}")
                    return self._parse_srt_content(sub_info['data'], detected_lang)

                # 查找字幕文件
                ext = sub_info.get('ext', 'srt')
                subtitle_file = os.path.join(output_dir, f"{video_id}.{detected_lang}.{ext}")

                if not os.path.exists(subtitle_file):
                    logger.info(f"字幕文件不存在: {subtitle_file}")
                    return None

                # 根据格式解析字幕文件
                if ext == 'json3':
                    return self._parse_json3_subtitle(subtitle_file, detected_lang)
                else:
                    with open(subtitle_file, 'r', encoding='utf-8') as f:
                        return self._parse_srt_content(f.read(), detected_lang)

        except Exception as e:
            logger.warning(f"获取B站字幕失败: {e}")
            return None

    def _parse_srt_content(self, srt_content: str, language: str) -> Optional[TranscriptResult]:
        """
        解析 SRT 格式字幕内容

        :param srt_content: SRT 字幕文本内容
        :param language: 语言代码
        :return: TranscriptResult
        """
        import re
        try:
            segments = []
            # SRT 格式: 序号\n时间戳\n文本\n\n
            pattern = r'(\d+)\n(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\n(.*?)(?=\n\n|\n\d+\n|$)'
            matches = re.findall(pattern, srt_content, re.DOTALL)

            for match in matches:
                idx, start_time, end_time, text = match
                text = text.strip()
                if not text:
                    continue

                # 转换时间格式 00:00:00,000 -> 秒
                def time_to_seconds(t):
                    parts = t.replace(',', '.').split(':')
                    return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])

                segments.append(TranscriptSegment(
                    start=time_to_seconds(start_time),
                    end=time_to_seconds(end_time),
                    text=text
                ))

            if not segments:
                return None

            full_text = ' '.join(seg.text for seg in segments)
            logger.info(f"成功解析B站SRT字幕，共 {len(segments)} 段")
            return TranscriptResult(
                language=language,
                full_text=full_text,
                segments=segments,
                raw={'source': 'bilibili_subtitle', 'format': 'srt'}
            )

        except Exception as e:
            logger.warning(f"解析SRT字幕失败: {e}")
            return None

    def _parse_json3_subtitle(self, subtitle_file: str, language: str) -> Optional[TranscriptResult]:
        """
        解析 json3 格式字幕文件

        :param subtitle_file: 字幕文件路径
        :param language: 语言代码
        :return: TranscriptResult
        """
        try:
            with open(subtitle_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            segments = []
            events = data.get('events', [])

            for event in events:
                # json3 格式中时间单位是毫秒
                start_ms = event.get('tStartMs', 0)
                duration_ms = event.get('dDurationMs', 0)

                # 提取文本
                segs = event.get('segs', [])
                text = ''.join(seg.get('utf8', '') for seg in segs).strip()

                if text:  # 只添加非空文本
                    segments.append(TranscriptSegment(
                        start=start_ms / 1000.0,
                        end=(start_ms + duration_ms) / 1000.0,
                        text=text
                    ))

            if not segments:
                return None

            full_text = ' '.join(seg.text for seg in segments)

            logger.info(f"成功解析B站字幕，共 {len(segments)} 段")
            return TranscriptResult(
                language=language,
                full_text=full_text,
                segments=segments,
                raw={'source': 'bilibili_subtitle', 'file': subtitle_file}
            )

        except Exception as e:
            logger.warning(f"解析字幕文件失败: {e}")
            return None