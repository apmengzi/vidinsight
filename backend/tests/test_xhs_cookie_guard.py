"""小红书 Cookie 健康探测测试。

锁住一个真实误报（2026-11-05 用户报障）：
用户明明在捕获窗口里登录着，历史页却弹「登录态已过期」。

根因：旧实现只看 `/explore` 页面的 `"loggedIn":true` 字符串。
实测发现真实有效 cookie 访问该页仍返回 `"loggedIn":false`（SSR 快照
不反映请求 cookie 的登录态），于是必然误报。

新实现的判据（实测三态）：
  - 真实有效 cookie → `/user/profile/me` 返回 **404**
  - 伪造 web_session → 返回 **500**
  - 无 cookie        → **302** 跳转

且「任一信号为真即判有效」，宁可少报错也不误伤用户。
"""
import pytest

from app.services import platform_guard


class _FakeResp:
    def __init__(self, status_code=200, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


def _patch(monkeypatch, cookie, me_resp, explore_resp):
    """注入假 cookie 与假响应。"""
    monkeypatch.setattr(
        platform_guard.CookieConfigManager, "get", lambda self, p: cookie)

    def fake_get(url, **kwargs):
        if "/user/profile/me" in url:
            if isinstance(me_resp, Exception):
                raise me_resp
            return me_resp
        if "/explore" in url:
            if isinstance(explore_resp, Exception):
                raise explore_resp
            return explore_resp
        return _FakeResp(200, "")

    monkeypatch.setattr(platform_guard.requests, "get", fake_get)


def test_no_cookie_reports_missing(monkeypatch):
    _patch(monkeypatch, "", _FakeResp(404), _FakeResp(200, ""))
    r = platform_guard.check_xhs_cookie()
    assert r["status"] == "missing"


def test_cookie_without_web_session_is_expired(monkeypatch):
    _patch(monkeypatch, "a1=xxx; webId=yyy", _FakeResp(404), _FakeResp(200, ""))
    r = platform_guard.check_xhs_cookie()
    assert r["status"] == "expired"
    assert "web_session" in r["detail"]


def test_profile_404_means_logged_in(monkeypatch):
    """核心回归：profile 返回 404 = 已登录，即使 explore 说 loggedIn:false。"""
    _patch(
        monkeypatch,
        "web_session=real_token_value_here",
        _FakeResp(404),
        _FakeResp(200, '"loggedIn":false'),   # 旧实现被这个误判
    )
    r = platform_guard.check_xhs_cookie()
    assert r["status"] == "logged_in", f"误报复发: {r}"


def test_explore_logged_in_true_also_counts(monkeypatch):
    """explore 明确说已登录时，即使 profile 异常也算有效（宁可少报错）。"""
    _patch(
        monkeypatch,
        "web_session=real_token_value_here",
        _FakeResp(500),
        _FakeResp(200, '"loggedIn":true'),
    )
    r = platform_guard.check_xhs_cookie()
    assert r["status"] == "logged_in"


def test_profile_500_means_expired(monkeypatch):
    """伪造会话时服务端解析失败返回 500 → 判失效。"""
    _patch(
        monkeypatch,
        "web_session=fake",
        _FakeResp(500),
        _FakeResp(200, '"loggedIn":false'),
    )
    r = platform_guard.check_xhs_cookie()
    assert r["status"] == "expired"


def test_both_probes_fail_is_unknown_not_expired(monkeypatch):
    """两个探测都网络失败 → 判 unknown，不能诬赖用户登录态失效。"""
    _patch(
        monkeypatch,
        "web_session=real_token_value_here",
        Exception("connection timeout"),
        Exception("connection timeout"),
    )
    r = platform_guard.check_xhs_cookie()
    assert r["status"] == "unknown"
    assert r["status"] != "expired"


def test_profile_302_means_expired(monkeypatch):
    """无 cookie 时会被重定向，判失效。"""
    _patch(
        monkeypatch,
        "web_session=old",
        _FakeResp(302, "", {"Location": "/login"}),
        _FakeResp(200, '"loggedIn":false'),
    )
    r = platform_guard.check_xhs_cookie()
    assert r["status"] == "expired"
