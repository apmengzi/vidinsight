"""贴链接即解析（/api/xhs/parse + 工具 parse_xhs_note）测试。

覆盖 hellotik 式「秒级预览」通道的产品契约：
1. 视频笔记 / 图文笔记 的结构化元数据抽取
2. 裸链接（无 xsec_token）→ 教学式失败（不硬试）
3. 裸链接 + 被动捕获缓存 → 走缓存通道成功
4. 登录墙壳页 → 明确登录指引
5. 合集链接 → 指路批量通道
6. 短链双域（xhslink.com / .cn）统一解析
7. harness 工具注册（schema/handler/restricted 一致性）
8. 路由端点契约（结构化失败也 200 + success 字段）
"""
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.downloaders import xiaohongshu_downloader as xhs_mod
from app.downloaders.xiaohongshu_downloader import XiaoHongShuDownloader
from app.services import produce_tools
from app.services.xhs_note_cache import XHSNoteCache

TOKEN_URL = ("https://www.xiaohongshu.com/explore/"
             "6aa0e8f70000000012024fdd"
             "?xsec_token=ABTestToken=&xsec_source=pc_user")
BARE_URL = "https://www.xiaohongshu.com/explore/6aa0e8f70000000012024fdd"


def _video_note() -> dict:
    return {
        "noteId": "6aa0e8f70000000012024fdd",
        "title": "超爱的芝士培根虾滑卷教程‼️",
        "desc": "一口一个不吱声[色色R] #芝士就是力量",
        "user": {"userId": "u123", "nickName": "测试UP"},
        "tagList": [{"name": "虾滑"}, {"name": "芝士培根"}],
        "imageList": [{"urlDefault": "https://sns-webpic-qc.xhscdn.com/cover.jpg",
                       "width": 1080, "height": 1440}],
        "video": {"media": {"stream": {"h264": [
            {"masterUrl": "https://sns-video-qc.xhscdn.com/video_1.mp4"}]}}},
        "interactInfo": {"likedCount": "1234", "collectedCount": "567",
                         "commentCount": "89"},
        "time": 1727400000000,
        "ipLocation": "上海",
    }


def _image_note() -> dict:
    note = _video_note()
    note.pop("video")
    note["imageList"] = [
        {"urlDefault": f"https://sns-webpic-qc.xhscdn.com/img{i}.jpg"} for i in range(3)
    ]
    return note


def _detail_html(note: dict) -> str:
    state = {"note": {"noteDetailMap": {note["noteId"]: {"note": note}}}}
    blob = json.dumps(state, ensure_ascii=False)
    return ("<html><script>window.__INITIAL_STATE__=" + blob + ";</script>"
            "<!-- " + "x" * 6000 + " --></html>")


@pytest.fixture(autouse=True)
def _clean_cache():
    XHSNoteCache._data.clear()
    yield
    XHSNoteCache._data.clear()


def _downloader_with(html: str) -> XiaoHongShuDownloader:
    d = XiaoHongShuDownloader()
    d._http_get = lambda url, use_cookie=True: html  # type: ignore[method-assign]
    return d


# ── 1. 结构化抽取 ────────────────────────────────────

def test_parse_video_note_structured():
    d = _downloader_with(_detail_html(_video_note()))
    out = d.get_note_structured(TOKEN_URL)
    assert out["success"] is True, out
    assert out["note_type"] == "video"
    assert out["note_id"] == "6aa0e8f70000000012024fdd"
    assert "芝士培根虾滑卷" in out["title"]
    assert out["video_url"] == "https://sns-video-qc.xhscdn.com/video_1.mp4"
    assert {"虾滑", "芝士培根"} <= set(out["tags"])
    assert out["author"]["nickname"] == "测试UP"
    assert out["likes"] == "1234"
    assert out["ip_location"] == "上海"
    assert out["source_url"].startswith("https://www.xiaohongshu.com/explore/")


def test_parse_image_note_structured():
    d = _downloader_with(_detail_html(_image_note()))
    out = d.get_note_structured(TOKEN_URL)
    assert out["success"] is True, out
    assert out["note_type"] == "normal"
    assert out["video_url"] == ""
    assert len(out["images"]) == 3
    assert "图文" in out["next_step"]


# ── 2. 裸链接：教学式失败 / 缓存通道 ─────────────────

def test_bare_link_without_token_fails_with_hint():
    d = _downloader_with(_detail_html(_video_note()))
    out = d.get_note_structured(BARE_URL)
    assert out["success"] is False
    assert "xsec_token" in out["reason"]
    assert "App 分享" in out["hint"]
    # 绝不能真的发请求（mock 里真发了也拿不到 token 页——但这里根本不该走到 _get_note）
    assert out.get("note_id") == "6aa0e8f70000000012024fdd"


def test_bare_link_with_webview_cache_succeeds():
    XHSNoteCache.put("6aa0e8f70000000012024fdd", _video_note())
    d = _downloader_with("不该被访问")  # 若走了网络会拿到垃圾，命中缓存则不碰
    out = d.get_note_structured(BARE_URL)
    assert out["success"] is True, out
    assert out["note_type"] == "video"


# ── 3. 异常页面的明确指引 ────────────────────────────

def test_login_wall_gives_actionable_error():
    wall = "<!-- 登录超限，请扫码登录 -->" + "x" * 6000
    d = _downloader_with(wall)
    out = d.get_note_structured(TOKEN_URL)
    assert out["success"] is False
    assert out["err"] == "XHS_LOGIN_REQUIRED"
    assert "重新登录" in out["hint"]


def test_state_without_target_note_is_structured_failure():
    """2026-09-27 实测根因：页面带合法 INITIAL_STATE 但目标笔记不在树里
    （token 失效壳页）。此前 _get_note 返回 None → 调用方炸 NoneType；
    现在必须收敛为结构化失败。"""
    other = _video_note()
    other["noteId"] = "bbbbbbbbbbbbbbbbbbbbbbbb"
    d = _downloader_with(_detail_html(other))  # state 里只有别人的笔记
    out = d.get_note_structured(TOKEN_URL)
    assert out["success"] is False, out
    assert "未找到该笔记数据" in out["reason"]
    assert out["err"] == "XHS_PAGE_FETCH_FAILED"


def test_collection_link_rejected_with_routing_hint():
    d = _downloader_with(_detail_html(_video_note()))
    out = d.get_note_structured(
        "https://www.xiaohongshu.com/collection/item/6aa0e8f70000000012024fdd")
    assert out["success"] is False
    assert "合集" in out["reason"]
    assert "批量" in out["hint"]


def test_non_xhs_link_rejected():
    out = XiaoHongShuDownloader().get_note_structured("https://example.com/abc")
    assert out["success"] is False
    assert "不是小红书" in out["reason"]


def test_empty_url():
    out = XiaoHongShuDownloader().get_note_structured("  ")
    assert out["success"] is False
    assert out["reason"] == "链接为空"


# ── 4. 短链双域统一 ──────────────────────────────────

def test_short_link_both_domains_resolved(monkeypatch):
    """xhslink.com 与 xhslink.cn 都必须走 curl 解析（不再只认 .cn）。"""
    calls = []

    def _fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(stdout="https://www.xiaohongshu.com/explore/"
                                       "6aa0e8f70000000012024fdd?xsec_token=ABT=&xsec_source=pc_user")

    monkeypatch.setattr(xhs_mod.subprocess, "run", _fake_run)
    d = XiaoHongShuDownloader()
    for host in ("xhslink.com", "xhslink.cn"):
        resolved = d._resolve_url(f"https://{host}/aBc123")
        assert resolved.startswith("https://www.xiaohongshu.com/explore/"), host
    assert len(calls) == 2


def test_structured_parse_accepts_short_link(monkeypatch):
    """get_note_structured 收到短链时：先解析短链再取详情。"""
    monkeypatch.setattr(
        xhs_mod.XiaoHongShuDownloader, "_resolve_url",
        lambda self, url: TOKEN_URL,
    )
    d = _downloader_with(_detail_html(_video_note()))
    out = d.get_note_structured("https://xhslink.com/aBc123")
    assert out["success"] is True, out
    assert out["note_type"] == "video"


# ── 5. harness 工具注册 ─────────────────────────────

def test_tool_registered_and_restricted():
    names = {t["function"]["name"] for t in produce_tools.PRODUCE_TOOLS}
    assert "parse_xhs_note" in names
    assert produce_tools.PRODUCE_HANDLERS["parse_xhs_note"] is produce_tools.parse_xhs_note
    assert "parse_xhs_note" in produce_tools.RESTRICTED_TOOLS


def test_tool_handler_success(monkeypatch):
    monkeypatch.setattr(
        xhs_mod.XiaoHongShuDownloader, "get_note_structured",
        lambda self, url: {"success": True, "note_type": "video",
                           "title": "t", "video_url": "v", "images": []},
    )
    out = json.loads(produce_tools.parse_xhs_note({"url": TOKEN_URL}))
    assert out["success"] is True
    assert out["note_type"] == "video"


def test_tool_handler_structured_failure_passthrough(monkeypatch):
    monkeypatch.setattr(
        xhs_mod.XiaoHongShuDownloader, "get_note_structured",
        lambda self, url: {"success": False, "reason": "链接缺少访问凭证（xsec_token）",
                           "hint": "从 App 分享复制"},
    )
    out = json.loads(produce_tools.parse_xhs_note({"url": BARE_URL}))
    assert out["success"] is False
    assert out["suggestion"], "失败必须带可行动建议"


def test_tool_handler_never_raises(monkeypatch):
    def _boom(self, url):
        raise RuntimeError("网络炸了")
    monkeypatch.setattr(xhs_mod.XiaoHongShuDownloader, "get_note_structured", _boom)
    out = json.loads(produce_tools.parse_xhs_note({"url": TOKEN_URL}))
    assert out["success"] is False
    assert "解析异常" in out["reason"]


# ── 6. 路由契约 ─────────────────────────────────────

def _client() -> TestClient:
    from app.routers.note import router
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


def test_route_parse_success(monkeypatch):
    monkeypatch.setattr(
        xhs_mod.XiaoHongShuDownloader, "get_note_structured",
        lambda self, url: {"success": True, "note_type": "video", "title": "t"},
    )
    body = _client().post("/api/xhs/parse", json={"video_url": TOKEN_URL}).json()
    assert body["code"] == 0
    assert body["data"]["success"] is True


def test_route_structured_failure_is_200_with_success_false(monkeypatch):
    """失败契约：结构化失败走 200 + success=false，前端按字段分支而不是按状态码。"""
    monkeypatch.setattr(
        xhs_mod.XiaoHongShuDownloader, "get_note_structured",
        lambda self, url: {"success": False, "reason": "链接缺少访问凭证（xsec_token）",
                           "hint": "从 App 分享复制"},
    )
    body = _client().post("/api/xhs/parse", json={"video_url": BARE_URL}).json()
    assert body["code"] == 0
    assert body["data"]["success"] is False
    assert "xsec_token" in body["data"]["reason"]


def test_route_unexpected_error_is_handled(monkeypatch):
    def _boom(self, url):
        raise RuntimeError("curl 不存在")
    monkeypatch.setattr(xhs_mod.XiaoHongShuDownloader, "get_note_structured", _boom)
    body = _client().post("/api/xhs/parse", json={"video_url": TOKEN_URL}).json()
    assert body["code"] != 0
    assert "解析失败" in body["msg"]


# ── 笔记 ID 解析：短链跳登录页的形态 ────────────────────────
# 2026-10-08 实测真 bug：未登录时 xhslink 短链展开会跳到
#   /login?redirectPath=<URL编码的 discovery/item/<24位ID>?xsec_token=…>
# 真实 ID 被编码埋在参数里，标准路径正则认不出 → 预览报"无法从链接解析笔记 ID"，
# 但链接本身完全合法（用户正是从 App 分享复制的）。以下用例锁住该修复。

def test_extract_note_id_from_login_redirect():
    """登录页 redirectPath 里 URL 编码的真实笔记 ID 要能解出来。"""
    url = ("https://www.xiaohongshu.com/login?redirectPath=http%3A%2F%2F"
           "www.xiaohongshu.com%2Fdiscovery%2Fitem%2F6ab63ecd000000000a01a5b5"
           "%3Fxsec_token%3DABC")
    assert xhs_mod.XiaoHongShuDownloader._extract_note_id(url) == "6ab63ecd000000000a01a5b5"


def test_extract_note_id_normal_forms_still_work():
    """常规形态不能因为新增分支而回归。"""
    D = xhs_mod.XiaoHongShuDownloader
    assert D._extract_note_id("https://www.xiaohongshu.com/explore/6ab63ecd000000000a01a5b5?xsec_token=x") == "6ab63ecd000000000a01a5b5"
    assert D._extract_note_id("https://www.xiaohongshu.com/discovery/item/6ab63ecd000000000a01a5b5") == "6ab63ecd000000000a01a5b5"


def test_extract_note_id_returns_none_when_absent():
    """没有 ID 时返回 None（调用方给教学提示），不能瞎猜。"""
    D = xhs_mod.XiaoHongShuDownloader
    assert D._extract_note_id("https://www.xiaohongshu.com/") is None
    assert D._extract_note_id("https://www.xiaohongshu.com/login") is None


# ── 分享参数剥离（2026-10-13 用户实测驱动）────────────────
# 用户从 App「分享→复制链接」拿到的链接带一长串追踪参数
# （xhsshare/appuid/apptime/share_id/share_channel）。不影响解析，但链接
# 长得吓人、且会混进日志与缓存键。统一剥离，让用户直接贴分享链接即可。

def test_strip_share_params_on_board_link():
    """board 场景：分享参数全剥，且不需要 xsec_token。"""
    url = ("https://www.xiaohongshu.com/board/66e64e4e000000001c01b8ed"
           "?xhsshare=&appuid=601f8c81000000000100062d&apptime=1791980228"
           "&share_id=df2469c3fdec414dae22ea7acb1ff352&share_channel=copy_link")
    out = xhs_mod.strip_share_params(url, keep_xsec=False)
    assert out == "https://www.xiaohongshu.com/board/66e64e4e000000001c01b8ed"


def test_strip_share_params_keeps_xsec_token_by_default():
    """note 详情页的 xsec_token 是必需访问凭证，绝不能剥（剥了必失败）。"""
    url = ("https://www.xiaohongshu.com/explore/6ab63ecd000000000a01a5b5"
           "?xsec_token=ABC123&xsec_source=pc_feed&xhsshare=")
    out = xhs_mod.strip_share_params(url, keep_xsec=True)
    assert "xsec_token=ABC123" in out
    assert "xsec_source=pc_feed" in out
    assert "xhsshare" not in out


def test_strip_share_params_handles_clean_and_empty():
    clean = "https://www.xiaohongshu.com/board/66e64e4e000000001c01b8ed"
    assert xhs_mod.strip_share_params(clean) == clean
    assert xhs_mod.strip_share_params("") == ""
    assert xhs_mod.strip_share_params(None) == ""


def test_strip_share_params_wechat_source():
    """微信分享来源（share_channel=wechat）同样要剥。"""
    url = ("https://www.xiaohongshu.com/board/66e64e4e000000001c01b8ed"
           "?xhsshare=&appuid=601f8c81000000000100062d&apptime=1791883698"
           "&share_id=d00f593592bc41ca8f5aaac10e92172d&share_channel=wechat")
    out = xhs_mod.strip_share_params(url, keep_xsec=False)
    assert "?" not in out and "share_channel" not in out
