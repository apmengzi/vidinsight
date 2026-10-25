"""B站字幕直拉通道测试。

锁住两件事：
  1. 失败必须**可诊断**——"该视频没字幕"和"你没登录"对用户的含义完全不同，
     前者只能转写，后者配个 Cookie 就能秒出。混为一谈会让用户白白等转写。
  2. cid 获取要抗瞬时风控（2026-10-08 实测：同一 bvid 首次 code!=0、重试即 0）。
"""
from app.downloaders.bilibili_subtitle import BilibiliSubtitleFetcher


def test_last_status_initialized_none():
    f = BilibiliSubtitleFetcher()
    assert f.last_status is None


def test_classify_returns_need_login_when_no_cookie():
    """未配置 cookie 时，取不到字幕一律归因于登录态缺失。"""
    f = BilibiliSubtitleFetcher()
    f._cookie = ""
    assert f._classify("BV1GJ411x7h7") == "need_login"


def test_classify_returns_need_login_when_tracks_exist_but_locked(monkeypatch):
    """轨道存在却拿不到 URL → 是身份问题（is_lock），不是视频没字幕。"""
    f = BilibiliSubtitleFetcher()
    f._cookie = "SESSDATA=fake"
    monkeypatch.setattr(f, "_has_locked_tracks", lambda bvid: True)
    assert f._classify("BV1GJ411x7h7") == "need_login"


def test_classify_returns_no_subtitle_when_no_tracks(monkeypatch):
    """view API 都列不出轨道 → 该视频确实没有字幕。"""
    f = BilibiliSubtitleFetcher()
    f._cookie = "SESSDATA=fake"
    monkeypatch.setattr(f, "_has_locked_tracks", lambda bvid: False)
    assert f._classify("BV1GJ411x7h7") == "no_subtitle"


def test_get_cid_retries_on_transient_block(monkeypatch):
    """view API 偶发 code!=0（瞬时风控），重试后成功才算稳。"""
    calls = {"n": 0}

    class Resp:
        def json(self):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"code": -412, "message": "请求被拦截"}
            return {"code": 0, "data": {"cid": 137649199}}

    monkeypatch.setattr("requests.get", lambda *a, **k: Resp())
    f = BilibiliSubtitleFetcher()
    assert f._get_cid("BV1GJ411x7h7") == 137649199
    assert calls["n"] == 2  # 第一次失败、第二次成功


def test_get_cid_returns_none_after_exhausted_retries(monkeypatch):
    """持续失败要返回 None（让调用方回退转写），不能死循环。"""

    class Resp:
        def json(self):
            return {"code": -412, "message": "blocked"}

    monkeypatch.setattr("requests.get", lambda *a, **k: Resp())
    f = BilibiliSubtitleFetcher()
    assert f._get_cid("BV1GJ411x7h7") is None


def test_has_locked_tracks_tolerates_api_error(monkeypatch):
    """view API 异常时不能抛——字幕失败不该拖垮主流程。"""

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr("requests.get", boom)
    f = BilibiliSubtitleFetcher()
    assert f._has_locked_tracks("BV1GJ411x7h7") is False
