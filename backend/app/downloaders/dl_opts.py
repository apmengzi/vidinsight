"""yt-dlp 下载参数统一拼装（B站 / YouTube 共用）。

实测结论（2026-10-19，BV1GJ411x7h7 3:32 + BV1hN411L7V9 13:16，见
bench 记录）：B站 dash 音频是**单文件**（format 里没有 fragments），
所以 ``concurrent_fragment_downloads`` 对它完全无效；``http_chunk_size``
虽然能切出 Range 请求，但 CDN 单连接本就跑满（5.16MB / 0.24s），
切块反而因为每块都要重新等首包而更慢：

    1MB 分块 → 0.90~1.48s     2MB → 0.82~0.95s
    5MB → 0.45~0.57s          不分块 → 0.17~0.37s

因此默认**不开**分块/并发（保持单连接，这才是实测最优），只提供
环境变量口子供特殊网络（高 RTT、跨洋中继）下自行打开：

    BILI_DL_CHUNK_MB      >0 时设置 http_chunk_size（MB）
    BILI_DL_CONC_FRAG     >0 时设置 concurrent_fragment_downloads
    DOWNLOAD_PROXY_URL    下载专用代理，如 http://vps.example.com:3128

真正拖慢 B站音频链路的是**解析**（WBI 签名 + playurl 往返 1.5~4.5s）
和 **ffmpeg 转 mp3**（3.5s，默认 44.1kHz 立体声重采样），不是下载。
后者见 :func:`build_audio_postprocessors`。
"""
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# 实测最优默认值：不分块、不并发（单连接已跑满 CDN）
DEFAULT_CHUNK_MB = 0
DEFAULT_CONC_FRAG = 0

# whisper / groq / bcut 都是 16kHz 单声道就够，先降采样再编码 mp3
# 比默认 44.1kHz 立体声快约 3.5 倍（3.54s → 1.01s，同一 m4a 实测）
TRANSCRIBE_SAMPLE_RATE = "16000"
TRANSCRIBE_CHANNELS = "1"


def _int_env(name: str, default: int) -> int:
    """读正整数环境变量；非法值（空、非数字、负数）回退默认值并告警。"""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("%s=%r 不是整数，使用默认值 %s", name, raw, default)
        return default
    if val < 0:
        logger.warning("%s=%r 不能为负，使用默认值 %s", name, raw, default)
        return default
    return val


def get_chunk_mb() -> int:
    return _int_env("BILI_DL_CHUNK_MB", DEFAULT_CHUNK_MB)


def get_conc_frag() -> int:
    return _int_env("BILI_DL_CONC_FRAG", DEFAULT_CONC_FRAG)


def get_download_proxy() -> Optional[str]:
    """下载专用代理。

    优先 ``DOWNLOAD_PROXY_URL``（VPS 中继/自建 socks-http 网关），
    没有就返回 None——**不**自动继承全局 HTTP_PROXY，避免把国内 CDN
    流量误送进出国代理反而更慢。YouTube 那侧另有 ProxyConfigManager
    的全局代理逻辑，两者互不覆盖。
    """
    val = os.environ.get("DOWNLOAD_PROXY_URL")
    if val and val.strip():
        return val.strip()
    return None


def build_transfer_opts(chunk_mb: Optional[int] = None,
                        conc_frag: Optional[int] = None) -> dict:
    """返回只管"传输层"的 yt-dlp opts 片段（不含 format/outtmpl）。"""
    chunk = get_chunk_mb() if chunk_mb is None else chunk_mb
    conc = get_conc_frag() if conc_frag is None else conc_frag
    opts: dict = {}
    if chunk > 0:
        opts["http_chunk_size"] = chunk * 1024 * 1024
    if conc > 0:
        opts["concurrent_fragment_downloads"] = conc
    proxy = get_download_proxy()
    if proxy:
        opts["proxy"] = proxy
        logger.info("yt-dlp 走下载代理(DOWNLOAD_PROXY_URL): %s", proxy)
    return opts


def build_audio_postprocessors(preferred_quality: str = "64") -> list:
    """抽音频为 mp3 的后处理器。

    ⚠️ 降采样参数**不能**写在 postprocessor 字典里——``FFmpegExtractAudioPP``
    的构造函数只认 preferredcodec / preferredquality / nopostoverwrites，
    多余键会直接抛 ``unexpected keyword argument``。yt-dlp 的正确入口是
    opts 顶层的 ``postprocessor_args``，键名 ``extractaudio+ffmpeg``
    （``pp_key()`` 返回 "ExtractAudio"），见 :func:`build_postprocessor_args`。
    """
    return [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": "mp3",
        "preferredquality": preferred_quality,
    }]


def build_postprocessor_args() -> dict:
    """顶层 ``postprocessor_args``：先降到 16kHz 单声道再编码 mp3。

    对转写引擎（faster-whisper / groq / bcut）无损，但转码时间从
    3.5s 降到约 1s（同一 m4a 实测）。"""
    return {
        "extractaudio+ffmpeg": [
            "-ar", TRANSCRIBE_SAMPLE_RATE,
            "-ac", TRANSCRIBE_CHANNELS,
        ],
    }


def apply_audio_opts(ydl_opts: dict, preferred_quality: str = "64") -> dict:
    """一次性挂上 mp3 后处理器 + 降采样参数（原地更新并返回）。"""
    ydl_opts["postprocessors"] = build_audio_postprocessors(preferred_quality)
    ydl_opts["postprocessor_args"] = build_postprocessor_args()
    return ydl_opts


def apply_download_opts(ydl_opts: dict) -> dict:
    """把传输层参数合并进已有 opts（原地更新并返回，方便链式调用）。"""
    ydl_opts.update(build_transfer_opts())
    return ydl_opts
