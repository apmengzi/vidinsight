# -*- coding: utf-8 -*-
"""miku_image 渠道客户端离线测试（不发网络请求）。

覆盖：配置读取（env 优先）、JWT 过期兜底、generate 完整任务流、
401 自动重登重试、失败语义（任务 failed → MikuError）。
"""
import json
import time


def _mk_resp(status, payload):
    class R:
        status_code = status
        text = json.dumps(payload, ensure_ascii=False)
        def json(self):
            return payload
    return R()


def _patch_http(monkeypatch, m, post=None, get=None):
    """统一打桩 m._direct_session。miku 的全部 HTTP 走直连 Session（trust_env=False，
    实测 proxies={None} 会被 requests 2.32 的 env 代理回填，不可靠），
    因此不能再打桩 requests.post/get。"""
    class FakeSession:
        def post(self, url, **kw):
            return post(url, **kw) if post else _mk_resp(200, {})
        def get(self, url, **kw):
            return get(url, **kw) if get else _mk_resp(200, {})
    monkeypatch.setattr(m, "_direct_session", FakeSession)


def test_load_config_env(monkeypatch, tmp_path):
    from app.services import miku_image as m
    monkeypatch.setenv("MIKU_EMAIL", "a@b.c")
    monkeypatch.setenv("MIKU_PASSWORD", "pw")
    monkeypatch.delenv("MIKU_BASE", raising=False)
    monkeypatch.delenv("MIKU_MODEL", raising=False)
    cfg = m.load_config()
    assert cfg["email"] == "a@b.c" and cfg["password"] == "pw"
    assert cfg["base"] == "https://miku.app.itstudio.club"
    assert cfg["model"] == m.DEFAULT_MODEL


def test_load_config_absent(monkeypatch, tmp_path):
    from app.services import miku_image as m
    for k in ("MIKU_EMAIL", "MIKU_PASSWORD", "MIKU_BASE", "MIKU_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(m, "_config_dir", lambda: str(tmp_path))  # 无 miku.json
    assert m.load_config() is None
    assert m.is_configured() is False


def test_jwt_exp_fallback_on_garbage():
    from app.services import miku_image as m
    # 解不出 exp 时按 20 分钟后过期，宁可提前重登也不带过期 token 打接口
    assert 0 < m._jwt_exp("not.a.jwt") - time.time() < 1300


def test_generate_task_flow(monkeypatch, tmp_path):
    from app.services import miku_image as m

    monkeypatch.setenv("MIKU_EMAIL", "a@b.c")
    monkeypatch.setenv("MIKU_PASSWORD", "pw")
    monkeypatch.setattr(m, "_config_dir", lambda: str(tmp_path))
    m._token_state.update(token="t", exp_ts=time.time() + 7200, device_id="dev_x")

    calls = {"post": 0, "get": 0}

    def fake_post(url, **kw):
        calls["post"] += 1
        body = json.loads(kw["data"].decode("utf-8"))
        assert body["model"] == m.DEFAULT_MODEL, "默认必须用最省积分模型"
        assert body["parameters"]["size"] == m.PORTRAIT_SIZE  # 4:5 竖版直出
        assert "charset" not in kw["headers"].get("Content-Type", "")  # 不加 charset 也合法
        return _mk_resp(200, {"code": 0, "data": {"task_id": "tk1", "estimated_cost": 0.35}})

    def fake_get(url, **kw):
        calls["get"] += 1
        if calls["get"] == 1:
            return _mk_resp(200, {"data": {"status": "processing"}})
        return _mk_resp(200, {"data": {"status": "completed",
                                       "result_url": "/api/v1/files/tasks/tk1/1.png?sig=x"}})

    _patch_http(monkeypatch, m, fake_post, fake_get)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)

    out = m.generate("测试 prompt")
    assert out["url"] == "https://miku.app.itstudio.club/api/v1/files/tasks/tk1/1.png?sig=x"
    assert out["cost"] == 0.35 and out["task_id"] == "tk1"
    assert calls["post"] == 1 and calls["get"] == 2


def test_generate_401_relogin_retry(monkeypatch, tmp_path):
    from app.services import miku_image as m

    monkeypatch.setenv("MIKU_EMAIL", "a@b.c")
    monkeypatch.setenv("MIKU_PASSWORD", "pw")
    monkeypatch.setattr(m, "_config_dir", lambda: str(tmp_path))
    m._token_state.update(token="expired", exp_ts=time.time() + 7200, device_id="dev_x")

    state = {"posts": 0, "logins": 0}

    def fake_login(cfg):
        state["logins"] += 1
        m._token_state.update(token="fresh", exp_ts=time.time() + 7200)
        return "fresh"

    def fake_post(url, **kw):
        state["posts"] += 1
        if state["posts"] == 1:
            return _mk_resp(401, {"error": "invalid or expired token"})
        return _mk_resp(200, {"code": 0, "data": {"task_id": "tk2", "estimated_cost": 0.3}})

    def fake_get(url, **kw):
        return _mk_resp(200, {"data": {"status": "completed",
                                       "result_url": "/api/v1/files/tasks/tk2/1.png"}})

    monkeypatch.setattr(m, "_login", fake_login)
    _patch_http(monkeypatch, m, fake_post, fake_get)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)

    out = m.generate("x")
    assert state["logins"] == 1 and state["posts"] == 2
    assert out["task_id"] == "tk2"


def test_verify_cap_sig_uses_newline_join(monkeypatch, tmp_path):
    """锁死根因：sig 的 msg 是 join("\\n")（源码模板字面量含真实换行），
    曾误读为空格导致所有纯 HTTP 签到 400。"""
    from app.services import miku_image as m

    monkeypatch.setenv("MIKU_EMAIL", "a@b.c")
    monkeypatch.setenv("MIKU_PASSWORD", "pw")
    monkeypatch.setattr(m, "_config_dir", lambda: str(tmp_path))
    cfg = {"base": "https://miku.app.itstudio.club"}
    headers = {"User-Agent": "UA/1.0"}
    cap = {"id": "c1", "nonce": "n1", "issued_at": 1700000000000, "sign_key": "k" * 32,
           "background_image": "", "template_image": "",
           "background_image_width": 600, "background_image_height": 360,
           "template_image_width": 110, "template_image_height": 360}

    body = {}

    def fake_post(url, **kw):
        body.update(__import__("json").loads(kw["data"]))
        return _mk_resp(200, {"data": {"captcha_token": "tok"}})

    _patch_http(monkeypatch, m, post=fake_post)
    tok = m._verify_cap(cfg, headers, cap, 150)
    assert tok == "tok"
    sig = body["sig"]
    expect_msg = "\n".join(["miku-ci-v1", "c1", "n1", "1700000000000", sig["ph"], "UA/1.0"])
    assert sig["mac"] == m._hmac_sha256_hex(cap["sign_key"], expect_msg)
    # 服务端容差内语义：offset 即提交值；帧字段与真实 UI 一致
    assert body["slider_max"] == 310 and body["pt"] == "mouse"


def test_generate_task_failed_raises(monkeypatch, tmp_path):
    from app.services import miku_image as m

    monkeypatch.setenv("MIKU_EMAIL", "a@b.c")
    monkeypatch.setenv("MIKU_PASSWORD", "pw")
    monkeypatch.setattr(m, "_config_dir", lambda: str(tmp_path))
    m._token_state.update(token="t", exp_ts=time.time() + 7200, device_id="dev_x")

    _patch_http(monkeypatch, m,
                post=lambda url, **kw: _mk_resp(200, {"code": 0, "data": {"task_id": "tk3"}}),
                get=lambda url, **kw: _mk_resp(
                    200, {"data": {"status": "failed", "error": "insufficient credits"}}))
    monkeypatch.setattr(m.time, "sleep", lambda s: None)

    try:
        m.generate("x")
        assert False, "任务失败应抛 MikuError"
    except m.MikuError as e:
        assert "insufficient credits" in str(e)
