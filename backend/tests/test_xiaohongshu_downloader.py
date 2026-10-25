# -*- coding: utf-8 -*-
"""小红书下载器机制级测试（不依赖网络）。

真实背景：xsec_token 有时效，E2E 需要用户现分享的链接；
本测试用合成笔记节点 + 真实页面 HTML 验证解析/查找/直链提取逻辑。
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.downloaders.xiaohongshu_downloader import (  # noqa: E402
    XiaoHongShuDownloader,
    _sanitize,
)
from app.services.xhs_note_cache import XHSNoteCache, XHSTokenStore  # noqa: E402


def _fake_note() -> dict:
    return {
        "noteId": "6aa0e8f70000000012024fdd",
        "title": "超爱的芝士培根虾滑卷教程‼️",
        "desc": "一口一个不吱声[色色R] #芝士就是力量#",
        "user": {"nickname": "测试UP"},
        "tagList": [{"name": "虾滑"}, {"name": "芝士培根"}],
        "imageList": [{"urlDefault": "https://sns-webpic-qc.xhscdn.com/cover.jpg"}],
        "video": {
            "media": {
                "stream": {
                    "h264": [{"masterUrl": "https://sns-video-qc.xhscdn.com/video_1.mp4"}],
                    "h265": [{"masterUrl": "https://sns-video-hw.xhscdn.com/video_h265.mp4"}],
                },
            },
        },
    }


def test_find_note_by_note_id():
    note = _fake_note()
    state = {"noteData": {"noteDetailMap": {note["noteId"]: {"note": note}}}}
    # 嵌套结构里按 noteId 精确查找
    wrapped = {"a": [state]}
    found = XiaoHongShuDownloader._find_note(wrapped, note["noteId"])
    assert found is not None
    assert found["title"].startswith("超爱的芝士培根虾滑卷")


def test_find_note_fallback_by_id():
    note = _fake_note()
    note.pop("noteId")
    # 合集 noteList 等结构里主键是 id（实测合集页返回的字段名）
    note["id"] = "6aa0e8f70000000012024fdd"
    state = {"noteData": {"data": [note]}}
    found = XiaoHongShuDownloader._find_note(state, "6aa0e8f70000000012024fdd")
    assert found is not None


def test_extract_video_url_prefers_h264():
    url = XiaoHongShuDownloader._extract_video_url(_fake_note())
    assert url.startswith("https://sns-video-qc.xhscdn.com/video_1.mp4")

    empty = XiaoHongShuDownloader._extract_video_url({"desc": "图文笔记"})
    assert empty == ""


def test_parse_state_on_real_collection_page():
    """用真实抓取的合集页 HTML 验证 INITIAL_STATE 解析（结构级）。

    注意：本用例**依赖真实网络与平台当时的返回**。2026-10-13 实测小红书对
    匿名请求返回风控壳页（36KB、含 login 字样、无 noteData/noteList），此时
    "拿不到数据"是平台行为、不是代码回归——故做优雅降级（跳过），而不是把
    平台状态写成硬断言（否则平台一改版测试就红，属假失败）。
    解析逻辑本身由本文件其它不依赖网络的用例覆盖。
    """
    try:
        req = urllib.request.Request(
            "https://www.xiaohongshu.com/collection/item/6a3a9c8a0ab4000000000001",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
    except Exception:
        print("SKIP 网络不可用")
        return
    state = XiaoHongShuDownloader._parse_state(html)
    if state is None:
        print("SKIP 页面未返回可解析状态（可能被风控）")
        return
    note_list = (state.get("noteData") or {}).get("collectionData", {}).get("noteList") or []
    if not note_list:
        print("SKIP 未取到 noteList（匿名风控或平台改版）")
        return
    assert len(note_list) >= 5  # 正常返回时应至少 5 条


def test_sanitize_filename():
    assert _sanitize('超爱/的:虾滑卷"教程') == "超爱_的_虾滑卷_教程"
    assert _sanitize("") == "xhs_note"


def _fake_collection_html() -> str:
    col = {
        "name": "27届校招", "noteNum": 2, "totalEpisodes": 0,
        "noteList": [
            {"id": "a" * 24, "title": "秋招速通指南", "type": "video",
             "desc": "x", "cover": {}, "user": {}, "formatedLikes": "1"},
            {"id": "b" * 24, "title": "产品岗拆解", "type": "normal",
             "desc": "y", "cover": {}, "user": {}, "formatedLikes": "2"},
        ],
    }
    state = {"noteData": {"collectionData": col}}
    blob = json.dumps(state, ensure_ascii=False).replace("/", r"/")
    html = f"<html><script>window.__INITIAL_STATE__={blob};</script></html>"
    return html + "<!-- " + "pad" * 2000 + " -->"  # 越过 5000 字节页面有效性保护


def _make_downloader() -> "XiaoHongShuDownloader":
    d = XiaoHongShuDownloader()
    d._resolve_url = lambda u: u  # 短链解析跳过（无网络）
    return d


def test_expand_collection_parses_note_list():
    d = _make_downloader()
    d._http_get = lambda url: _fake_collection_html()  # type: ignore
    out = d.expand_collection("https://www.xiaohongshu.com/collection/item/" + "c" * 24)
    assert out["name"] == "27届校招"
    assert out["total"] == 2
    assert len(out["notes"]) == 2
    assert out["notes"][0]["note_id"] == "a" * 24
    assert "/explore/" in out["notes"][0]["url"]


def test_text_note_subtitles_synthesized():
    d = _make_downloader()
    note = _fake_note()
    note.pop("video")  # 图文笔记：无视频字段
    note["desc"] = "这是一段足够长的图文笔记正文，" * 10
    d._get_note = lambda url, nid: note  # type: ignore
    tr = d.download_subtitles("https://www.xiaohongshu.com/explore/" + "1" * 24)
    assert tr is not None
    assert "【小红书笔记正文】" in tr.segments[0].text
    assert tr.full_text and "图文笔记正文" in tr.full_text


def test_video_note_subtitles_returns_none():
    d = _make_downloader()
    d._get_note = lambda url, nid: _fake_note()  # type: ignore（带 video 字段）
    tr = d.download_subtitles("https://www.xiaohongshu.com/explore/" + "1" * 24)
    assert tr is None


def test_text_only_download_returns_meta():
    d = _make_downloader()
    note = _fake_note()
    note.pop("video")
    note["desc"] = "正文内容"
    url = "https://www.xiaohongshu.com/explore/" + "1" * 24
    d._note_cache[url] = note  # 预置实例缓存，免 curl 抓取
    from app.enmus.note_enums import DownloadQuality
    result = d.download(
        url,
        output_dir=os.path.join(os.environ.get("TEMP", "/tmp"), "xhs_test_meta"),
        quality=DownloadQuality.fast,
        skip_download=True,
    )
    assert result.raw_info.get("text_only") is True
    assert result.file_path == ""


# ====== XHSNoteCache（WebView 抓取回传缓存）======

def test_xhs_note_cache_roundtrip_and_ttl():
    nid = "cache" + "0" * 19
    try:
        XHSNoteCache.put(nid, {"desc": "hello"})
        assert XHSNoteCache.get(nid) == {"desc": "hello"}
        # 人为拨过期（TTL 现为 6 小时，用模块常量推导而不是写死秒数）
        import app.services.xhs_note_cache as m
        ts, note = m.XHSNoteCache._data[nid]
        m.XHSNoteCache._data[nid] = (time.time() - m._TTL - 1, note)
        assert XHSNoteCache.get(nid) is None
    finally:
        XHSNoteCache._data.pop(nid, None)


def test_get_note_prefers_webview_cache_over_http():
    """XHSNoteCache 有数据时 _get_note 不发 HTTP，直接返回缓存节点。"""
    nid = "web" + "0" * 21
    url = "https://www.xiaohongshu.com/explore/" + nid
    try:
        XHSNoteCache.put(nid, {"desc": "来自 WebView", "title": "t"})
        d = _make_downloader()
        def _boom(u):
            raise AssertionError("不应发起 HTTP 抓取")
        d._http_get = _boom  # type: ignore
        note = d._get_note(url, nid)
        assert note["desc"] == "来自 WebView"
    finally:
        XHSNoteCache._data.pop(nid, None)


def test_video_download_retries_on_corrupt_file(tmp_path):
    """2026-09-15 实战回归：CDN 偶发下到损坏 mp4（ffmpeg exit -22）。修复后
    自动删除坏文件、重下一次并成功提取。"""
    import app.downloaders.xiaohongshu_downloader as mod

    d = _make_downloader()
    note = _fake_note()
    nid = note["noteId"]
    url = "https://www.xiaohongshu.com/explore/" + nid
    d._note_cache[url] = note  # 免 curl 抓详情

    calls = {"n": 0}

    def fake_download_file(dl_url, path):
        calls["n"] += 1
        with open(path, "wb") as f:
            f.write(b"corrupt-html-page" * 300)  # >3000B 但不是视频
        return os.path.getsize(path)

    d._download_file = fake_download_file  # type: ignore

    def fake_probe(p):
        return 3.5 if calls["n"] >= 2 else 0.0  # 损坏文件 probe=0，重下后正常

    d._probe_duration = fake_probe  # type: ignore

    orig_run = mod.subprocess.run

    def fake_run(cmd, **kw):
        with open(cmd[-1], "wb") as f:
            f.write(b"mp3-bytes")
        class _R:
            returncode = 0
        return _R()

    mod.subprocess.run = fake_run  # type: ignore
    try:
        from app.enmus.note_enums import DownloadQuality
        result = d.download(url, output_dir=str(tmp_path),
                            quality=DownloadQuality.fast, skip_download=False)
        assert result.file_path.endswith(".mp3")
        assert calls["n"] == 2  # 损坏重下了一次
        assert os.path.exists(result.file_path)
    finally:
        mod.subprocess.run = orig_run


def test_parse_state_sanitizes_new_map_and_finds_note():
    """2026-09-15 实战回归：XHS 详情页 state 含 JS 表达式 `new Map([])` 与裸
    `undefined`（json.loads/JSON.parse 必炸，此前合集任务全部 400102 的真根因），
    且笔记可能位于顶层 note.noteDetailMap 而非 noteData.data。合成同构页面验证。"""
    nid = "6" * 24
    note = {"noteId": nid, "title": "被开除的产品经理", "desc": "正文" * 40, "user": {}}
    blob = (
        '{"global":{"pwaAddDesktopPrompt":undefined,"serverTime":1789421145545},'
        '"note":{"noteDetailMap":{"' + nid + '":{"note":'
        + json.dumps(note, ensure_ascii=False) + '}}},'
        '"AiNoteDetailStore":{"noteDetailMap":new Map([])}}'
    )
    html = ("<html><script>window.__INITIAL_STATE__=" + blob + ";</script></html>"
            + "<!-- " + "pad" * 2600 + " -->")
    st = XiaoHongShuDownloader._parse_state(html)
    assert st is not None, "new Map([]) 消毒后必须可解析"
    found = XiaoHongShuDownloader._find_note(st, nid)
    assert found is not None
    assert found["title"].startswith("被开除")


def test_get_note_retries_without_cookie_when_stale_cookie_poisons():
    """2026-09-15 实测回归：过期 cookie 会把匿名可看的 token 详情页毒化成
    登录墙壳页（带 cookie 无笔记数据），摘掉 cookie 重试即命中。"""
    nid = "f" * 24
    url = "https://www.xiaohongshu.com/explore/" + nid

    def _state_html(with_note: bool) -> str:
        note = _fake_note()
        note["noteId"] = nid
        inner = {"note": note} if with_note else {}
        state = {"noteData": {"noteDetailMap": {nid: inner}}}
        blob = json.dumps(state, ensure_ascii=False)
        return ("<html><script>window.__INITIAL_STATE__=" + blob + ";</script></html>"
                + "<!-- " + "pad" * 2600 + " -->")

    calls: list = []

    def fake_get(url, use_cookie=True):
        calls.append(use_cookie)
        return _state_html(with_note=not use_cookie)  # 带 cookie 毒化；不带才有数据

    d = _make_downloader()
    d._http_get = fake_get  # type: ignore
    XHSNoteCache._data.pop(nid, None)
    try:
        note = d._get_note(url, nid)
        assert note["noteId"] == nid
        assert calls == [True, False]  # 首试带 cookie，失败后摘 cookie 重试
    finally:
        XHSNoteCache._data.pop(nid, None)


def test_get_note_login_wall_raises_login_required():
    """2026-09-21 风控升级回归：账号被「设备登录超限」踢下线后页面是登录墙
    壳页——必须抛 XHS_LOGIN_REQUIRED（带明确指引），不是笼统的页面获取失败。"""
    from app.exceptions.codes import ErrorCode
    nid = "e" * 24
    url = "https://www.xiaohongshu.com/explore/" + nid
    wall = ("<html><body>电脑设备登录超限，请重新登录 扫码登录</body></html>"
            "<!-- " + "pad" * 2600 + " -->")  # 有登录墙词、无 INITIAL_STATE
    d = _make_downloader()
    d._http_get = lambda u, use_cookie=True: wall  # type: ignore
    d._cookie_mgr = type("M", (), {"get": staticmethod(lambda p: None)})()  # 无 cookie，不触发匿名重试
    XHSNoteCache._data.pop(nid, None)
    try:
        try:
            d._get_note(url, nid)
            raise AssertionError("登录墙壳页必须抛 XHS_LOGIN_REQUIRED")
        except Exception as e:
            assert getattr(e, "err", "") == ErrorCode.XHS_LOGIN_REQUIRED.err, \
                f"应抛 XHS_LOGIN_REQUIRED，实际: {getattr(e, 'err', e)}"
    finally:
        XHSNoteCache._data.pop(nid, None)


# ====== XHSTokenStore + parse_token_links（合集 token 收割链路）======

def test_parse_token_links_extracts_and_dedupes():
    links = [
        "https://www.xiaohongshu.com/explore/aaaaaaaaaaaaaaaaaaaaaaaa?xsec_token=TOK1&xsec_source=pc_user",
        "https://www.xiaohongshu.com/explore/aaaaaaaaaaaaaaaaaaaaaaaa?xsec_token=TOK1&xsec_source=pc_user",  # 重复
        "/search_result/bbbbbbbbbbbbbbbbbbbbbbbb?xsec_token=TOK2&xsec_source=pc_search",
        "https://www.xiaohongshu.com/explore/cccccccccccccccccccccccc",  # 无 token → 丢弃
        "https://www.xiaohongshu.com/search_result/dddddddddddddddddddddddd?xsec_token=TOK3&xsec_source=",  # 空 source
    ]
    out = XiaoHongShuDownloader.parse_token_links(links)
    assert set(out.keys()) == {"a" * 24, "b" * 24, "d" * 24}
    assert out["a" * 24].startswith("https://www.xiaohongshu.com/explore/")
    assert "xsec_token=TOK1" in out["a" * 24]
    # 相对路径同样规范化：search_result → explore（SSR 详情通道）
    assert out["b" * 24].startswith("https://www.xiaohongshu.com/explore/")
    assert "xsec_source=pc_search" in out["b" * 24]
    # search_result 路径规范化为 explore（SSR 详情通道），空 source 补 pc_search
    assert "/explore/" in out["d" * 24]
    assert out["d" * 24].endswith("xsec_source=pc_search")


def test_parse_token_links_profile_card_format():
    """作者主页卡片 href（/user/profile/{uid}/{id}?xsec_token=...）必须被收录。

    v4.1 诊断实锤：主页卡片全是这种形态，旧路径白名单整批丢弃 → 收割 0 token。
    顶栏个人主页链接（/user/profile/{uid}?...，无第二段 id、token 为空）不能收。
    """
    nid = "e" * 24
    links = [
        f"/user/profile/601f8c81000000000100062d/{nid}?xsec_token=TOKPROF&xsec_source=pc_user",
        f"https://www.xiaohongshu.com/user/profile/601f8c81000000000100062d/{nid}?xsec_token=TOKPROF2",
        # 顶栏链接：uid 后无笔记 id 段；带空 token 的也不能收
        "/user/profile/601f8c81000000000100062d?channel_type=web_profile_page&xsec_token=",
        f"/user/profile/601f8c81000000000100062d/{nid}?xsec_token=",  # 空 token → 丢弃
    ]
    out = XiaoHongShuDownloader.parse_token_links(links)
    assert set(out.keys()) == {nid}
    url = out[nid]
    assert url.startswith(f"https://www.xiaohongshu.com/explore/{nid}")
    assert "xsec_token=TOKPROF" in url  # 先到先得，第一条的 token 胜出
    assert "xsec_source=pc_user" in url  # 主页来源默认 pc_user（token/source 配套）


def test_expand_collection_merges_webview_tokens():
    """XHSTokenStore 里有的笔记，展开结果用收割的完整 URL 原样替换。"""
    nid = "a" * 24
    harvested = f"https://www.xiaohongshu.com/explore/{nid}?xsec_token=TOKWEB&xsec_source=pc_user"
    try:
        XHSTokenStore.put(nid, harvested)
        d = _make_downloader()
        d._http_get = lambda url: _fake_collection_html()  # type: ignore
        out = d.expand_collection("https://www.xiaohongshu.com/collection/item/" + "c" * 24)
        target = [n for n in out["notes"] if n["note_id"] == nid][0]
        assert target["url"] == harvested
        assert target["has_token"] is True
    finally:
        XHSTokenStore._data.pop(nid, None)


def test_expand_collection_marks_cached_notes():
    """被动捕获模式：已被用户浏览记录（XHSNoteCache 命中）的笔记打 cached 标记，
    前端据此放行无 token 链接（_get_note 直接命中缓存，不需要 xsec_token）。"""
    nid = "a" * 24
    try:
        XHSNoteCache.put(nid, {"desc": "被动记录", "title": "t"})
        d = _make_downloader()
        d._http_get = lambda url: _fake_collection_html()  # type: ignore
        out = d.expand_collection("https://www.xiaohongshu.com/collection/item/" + "c" * 24)
        by_id = {n["note_id"]: n for n in out["notes"]}
        assert by_id[nid]["cached"] is True
        assert by_id["b" * 24]["cached"] is False
    finally:
        XHSNoteCache._data.pop(nid, None)


if __name__ == "__main__":
    import traceback

    failed = 0
    for name, fn in list(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        try:
            fn()
            print(f"PASS {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    raise SystemExit(1 if failed else 0)
