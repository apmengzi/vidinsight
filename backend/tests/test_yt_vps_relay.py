# -*- coding: utf-8 -*-
"""yt_vps_relay + VPS 字幕兜底路径的离线单测（不依赖 VPS 在线）。

网络相关的 extract_via_vps/fetch_url_via_vps 在 CI/离线环境返回 None 即可
（ssh 不可达静默降级是契约的一部分）。
"""
import pytest

from app.downloaders.youtube_downloader import YoutubeDownloader
from app.services.yt_vps_relay import _ALLOWED, extract_via_vps, fetch_url_via_vps


def test_url_whitelist():
    assert _ALLOWED.match("https://www.youtube.com/watch?v=abc")
    assert _ALLOWED.match("https://youtu.be/abc")
    assert _ALLOWED.match("https://m.youtube.com/watch?v=abc")
    # 命令注入/非 YouTube 链接必须被拒
    assert not _ALLOWED.match("https://evil.com/watch?a=1; rm -rf /")
    assert not _ALLOWED.match("https://www.youtube.com.evil.com/watch?v=x")
    assert not _ALLOWED.match('"; rm -rf /')


def test_fetch_url_whitelist(monkeypatch):
    monkeypatch.delenv("YT_VPS_ENABLED", raising=False)
    # 非 youtube/googlevideo 域直接 None，不发起 ssh
    assert fetch_url_via_vps("http://127.0.0.1:8483/api/x") is None
    assert fetch_url_via_vps("https://bilibili.com/x") is None


def test_extract_disabled(monkeypatch):
    monkeypatch.setenv("YT_VPS_ENABLED", "0")
    assert extract_via_vps("https://www.youtube.com/watch?v=abc") is None


def test_vtt_to_segments():
    vtt = (
        "WEBVTT\nKind: captions\nLanguage: en\n\n"
        "00:00:01.000 --> 00:00:03.000\nhello world\n\n"
        "00:00:03.000 --> 00:00:05.000\nhello world\n\n"  # 滚动重复应被合并
        "00:00:05.000 --> 00:00:07.500\nsecond line\n"
    )
    segs = YoutubeDownloader._vtt_to_segments(vtt)
    assert len(segs) == 2
    assert segs[0].text == "hello world"
    assert segs[0].start == 1.0 and segs[0].end == 5.0  # 合并后跨到 5s
    assert segs[1].text == "second line"
    assert abs(segs[1].end - 7.5) < 0.01
