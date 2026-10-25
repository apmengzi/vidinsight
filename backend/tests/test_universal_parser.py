"""全平台统一预览层（universal_parser + /api/parse + 工具 parse_video_link）测试。

不碰真网络：yt-dlp 全部 mock。真实链路验证在进度文件记录（2026-09-28 实测矩阵）。
"""
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import produce_tools, universal_parser as up


# ── 替身 ────────────────────────────────────────────

def _fake_info(**over) -> dict:
    info = {
        "title": "测试视频标题",
        "duration": 212.4,
        "uploader": "测试UP主",
        "description": "这是描述 #话题",
        "thumbnail": "https://img.example.com/cover.jpg",
        "webpage_url": "https://www.bilibili.com/video/BV1GJ411x7h7",
        "tags": ["tag1", "tag2"],
        "formats": [
            {"format_id": "low", "ext": "mp4", "height": 360, "url": "https://cdn/low.m4s", "filesize": 1000},
            {"format_id": "high", "ext": "mp4", "height": 1080, "url": "https://cdn/high.m4s", "filesize": 9000},
            {"format_id": "audio", "ext": "m4a", "height": None, "url": "https://cdn/audio.m4s", "filesize": 500},
        ],
    }
    info.update(over)
    return info


class _FakeYDL:
    """yt_dlp.YoutubeDL 替身：按脚本返回 info 或抛异常。"""
    script: list = []

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        item = _FakeYDL.script.pop(0) if _FakeYDL.script else RuntimeError("脚本耗尽")
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def fake_ytdlp(monkeypatch):
    _FakeYDL.script = []
    monkeypatch.setattr(up, "yt_dlp", SimpleNamespace(YoutubeDL=_FakeYDL))
    return _FakeYDL


# ── 1. yt-dlp 路径（bilibili/youtube/douyin）────────

def test_parse_bilibili_unified_schema(fake_ytdlp):
    _FakeYDL.script = [_fake_info()]
    out = up.parse_link("https://www.bilibili.com/video/BV1GJ411x7h7")
    assert out["success"] is True
    assert out["platform"] == "bilibili"
    assert out["engine"] == "yt-dlp"
    assert out["title"] == "测试视频标题"
    assert out["is_video"] is True
    # formats 按清晰度降序（音频 height=None 视作 0 沉底），且只留有 url 的
    heights = [f["height"] or 0 for f in out["formats"]]
    assert heights == sorted(heights, reverse=True) and heights[0] == 1080
    assert out["formats"][0]["url"] == "https://cdn/high.m4s"


def test_ydl_opts_bilibili_uses_cookiefile(monkeypatch):
    """产品同款：B站配了 cookie 就写 netscape 临时文件。"""
    monkeypatch.setattr(up.CookieConfigManager, "get",
                        lambda self, p: "SESSDATA=abc; buvid3=xyz" if p == "bilibili" else "")
    opts = up._ydl_opts_for("bilibili")
    assert opts.get("cookiefile")
    content = open(opts["cookiefile"], encoding="utf-8").read()
    assert "SESSDATA\tabc" in content
    import os as _os
    _os.unlink(opts["cookiefile"])


def test_ydl_opts_youtube_uses_proxy(monkeypatch):
    monkeypatch.setattr(up.ProxyConfigManager, "get_proxy_url", lambda self: "http://127.0.0.1:7890")
    opts = up._ydl_opts_for("youtube")
    assert opts.get("proxy") == "http://127.0.0.1:7890"


def test_douyin_without_cookie_gives_actionable_hint(fake_ytdlp):
    """2026-09-28 实测：抖音无 cookie 报 Fresh cookies —— 必须转成可行动提示。"""
    _FakeYDL.script = [RuntimeError("ERROR: [Douyin] 123: Fresh cookies (not necessarily logged in) are needed")]
    out = up.parse_link("https://www.douyin.com/video/7345492945006595379")
    assert out["success"] is False
    assert "Cookie" in out["hint"] and "无需登录" in out["hint"]


def test_ytdlp_failure_contract(fake_ytdlp):
    _FakeYDL.script = [RuntimeError("HTTP Error 412: Precondition Failed")]
    out = up.parse_link("https://www.bilibili.com/video/BV1GJ411x7h7")
    assert out["success"] is False
    assert out["engine"] == "yt-dlp"
    assert "反爬" in out["hint"]


# ── 2. 小红书路由（归一化）──────────────────────────

def test_xhs_video_normalized(monkeypatch):
    monkeypatch.setattr(
        up.XiaoHongShuDownloader, "get_note_structured",
        lambda self, url: {
            "success": True, "note_id": "abc", "note_type": "video",
            "title": "xhs标题", "desc": "正文", "tags": ["t"],
            "author": {"nickname": "作者"}, "video_url": "https://v/x.mp4",
            "images": [{"url": "https://img/1.jpg", "width": 1080, "height": 1440}],
            "likes": "1", "collected": "2", "comments": "3",
            "ip_location": "上海", "source_url": url,
        })
    out = up.parse_link("https://www.xiaohongshu.com/explore/"
                        "6aa0e8f70000000012024fdd?xsec_token=T=&xsec_source=pc_user")
    assert out["success"] is True
    assert out["platform"] == "xiaohongshu"
    assert out["engine"] == "ssr"
    assert out["is_video"] is True
    assert out["formats"][0]["url"] == "https://v/x.mp4"
    assert out["note_type"] == "video"          # 富字段保留
    assert out["likes"] == "1"


def test_xhs_failure_passthrough(monkeypatch, fake_ytdlp):
    """SSR 与 yt-dlp 双失败 → 返回 SSR 的教学式失败（hint 更可行动）。"""
    monkeypatch.setattr(
        up.XiaoHongShuDownloader, "get_note_structured",
        lambda self, url: {"success": False, "reason": "链接缺少访问凭证（xsec_token）"})
    _FakeYDL.script = [RuntimeError("ERROR: no video")]
    out = up.parse_link("https://www.xiaohongshu.com/explore/6aa0e8f70000000012024fdd")
    assert out["success"] is False
    assert "xsec_token" in out["reason"]
    assert out["platform"] == "xiaohongshu"


def test_xhs_ssr_fail_falls_back_to_ytdlp(monkeypatch, fake_ytdlp):
    """2026-09-27 实测：小红书视频 yt-dlp 匿名可通 → SSR 失败时必须兜底。"""
    monkeypatch.setattr(
        up.XiaoHongShuDownloader, "get_note_structured",
        lambda self, url: {"success": False, "reason": "链接缺少访问凭证（xsec_token）"})
    _FakeYDL.script = [_fake_info(title="xhs 视频兜底")]
    out = up.parse_link("https://www.xiaohongshu.com/explore/"
                        "6aa0e8f70000000012024fdd?xsec_token=T=")
    assert out["success"] is True
    assert out["engine"] == "yt-dlp"
    assert out["is_video"] is True
    assert "兜底" in out["note"]


# ── 3. 快手 limited 与未知平台 ──────────────────────

def test_kuaishou_honest_limited():
    out = up.parse_link("https://v.kuaishou.com/2vBqX74")
    assert out["success"] is False
    assert out["limited"] is True
    assert "生成" in out["hint"]     # 指向生成管线


def test_unknown_platform_generic_fallback(fake_ytdlp):
    _FakeYDL.script = [_fake_info(title="Vimeo 视频")]
    out = up.parse_link("https://vimeo.com/123456")
    assert out["success"] is True
    assert out["platform"] == "other"
    assert "不在产品支持列表" in out["hint"]


def test_empty_and_invalid_urls():
    assert up.parse_link("")["success"] is False
    assert up.parse_link("随便一句话")["success"] is False


# ── 4. harness 工具注册 ─────────────────────────────

def test_tool_registered_and_restricted():
    names = {t["function"]["name"] for t in produce_tools.PRODUCE_TOOLS}
    assert "parse_video_link" in names
    assert produce_tools.PRODUCE_HANDLERS["parse_video_link"] is produce_tools.parse_video_link
    assert "parse_video_link" in produce_tools.RESTRICTED_TOOLS


def test_tool_handler_success(monkeypatch):
    monkeypatch.setattr(up, "parse_link",
                        lambda url: {"success": True, "platform": "bilibili", "title": "t"})
    out = json.loads(produce_tools.parse_video_link({"url": "https://www.bilibili.com/video/BV1"}))
    assert out["success"] is True


def test_tool_handler_failure_has_suggestion(monkeypatch):
    monkeypatch.setattr(up, "parse_link",
                        lambda url: {"success": False, "reason": "解析失败: x"})
    out = json.loads(produce_tools.parse_video_link({"url": "https://x/y"}))
    assert out["success"] is False
    assert out["suggestion"]


def test_tool_handler_never_raises(monkeypatch):
    def _boom(url):
        raise RuntimeError("炸了")
    monkeypatch.setattr(up, "parse_link", _boom)
    out = json.loads(produce_tools.parse_video_link({"url": "https://x/y"}))
    assert out["success"] is False
    assert "解析异常" in out["reason"]


# ── 5. 路由契约 ─────────────────────────────────────

def _client() -> TestClient:
    from app.routers.note import router
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


def test_route_parse_success(monkeypatch):
    monkeypatch.setattr(up, "parse_link",
                        lambda url: {"success": True, "platform": "bilibili", "title": "t"})
    body = _client().post("/api/parse", json={"video_url": "https://www.bilibili.com/video/BV1"}).json()
    assert body["code"] == 0
    assert body["data"]["success"] is True
    assert body["data"]["platform"] == "bilibili"


def test_route_structured_failure_is_200(monkeypatch):
    monkeypatch.setattr(up, "parse_link",
                        lambda url: {"success": False, "reason": "快手暂无秒级预览通道",
                                     "limited": True})
    body = _client().post("/api/parse", json={"video_url": "https://v.kuaishou.com/x"}).json()
    assert body["code"] == 0
    assert body["data"]["success"] is False
    assert body["data"]["limited"] is True
