"""封面本地持久化 + 登录态预检测试（2026-10-19）。

背景：board_notes.json 里的 cover 是 xhscdn 签名 URL，路径带时效段
（202610180136 这类），过期后带/不带 Referer 一律 403（本次实测 3/3）。
所以必须在捕获那一刻落盘，读取接口优先给 cover_local。

这里用 stub 替换网络下载（`cover_persist._download`），验证：
  1. 落盘路径 /static/covers/{boardId}/{noteId}.{ext}
  2. BoardNoteStore 双字段（cover 原始 URL + cover_local）
  3. 下载失败保留原 URL 作 fallback，并计数
  4. 幂等：重复 ingest 不重复下载
  5. 读取接口：cover_local 悬空时自动回退原 URL
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.routers.note import router as note_router  # noqa: E402
from app.services import cover_persist  # noqa: E402
from app.services.board_note_store import BoardNoteStore  # noqa: E402

BID = "a" * 24
N1 = "b" * 24
N2 = "c" * 24
N3 = "d" * 24
URL = "https://sns-webpic-qc.xhscdn.com/202610180136/abc/cover!nc_n_webp_prv_1"

# 1x1 PNG（真实可解码字节，避免 stub 之外还有格式校验时踩空）
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)


@pytest.fixture
def cover_root(tmp_path, monkeypatch):
    """把封面根目录指到 tmp，避免污染 backend/static/covers。"""
    root = tmp_path / "covers"
    root.mkdir()
    monkeypatch.setattr(cover_persist, "_cover_root", lambda: root)
    return root


@pytest.fixture
def client(monkeypatch):
    """ingest 走真实路由，但网络下载被 stub（不联网、可控制成败）。"""
    app = FastAPI()
    app.include_router(note_router, prefix="/api")
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_store():
    BoardNoteStore.clear(BID)
    yield
    BoardNoteStore.clear(BID)


def _stub_ok(monkeypatch, calls: list | None = None):
    def fake_download(url):
        if calls is not None:
            calls.append(url)
        return PNG, ".png"

    monkeypatch.setattr(cover_persist, "_download", fake_download)


def _stub_fail(monkeypatch):
    def fake_download(url):
        return None, ""

    monkeypatch.setattr(cover_persist, "_download", fake_download)


def test_download_writes_file_and_returns_relative_path(cover_root, monkeypatch):
    _stub_ok(monkeypatch)
    stats = cover_persist.persist_covers(BID, [(N1, URL)])

    assert stats["downloaded"] == 1
    assert stats["failed"] == 0
    rel = stats["items"][N1]
    assert rel == f"/static/covers/{BID}/{N1}.png"
    written = cover_root / BID / f"{N1}.png"
    assert written.is_file()
    assert written.read_bytes() == PNG
    # 相对路径 → 磁盘存在性校验
    assert cover_persist.cover_file_exists(rel) is True


def test_ingest_stores_both_cover_and_cover_local(cover_root, client, monkeypatch):
    _stub_ok(monkeypatch)
    r = client.post("/api/xhs/ingest_board_notes", json={
        "board_id": BID,
        "notes": [{"note_id": N1, "title": "视频笔记", "type": "video", "cover": URL}],
    })
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["accepted"] == 1
    assert body["covers"]["downloaded"] == 1

    note = BoardNoteStore.get(BID, N1)
    assert note["cover"] == URL                                   # 原始 URL 保留
    assert note["cover_local"] == f"/static/covers/{BID}/{N1}.png"  # 本地路径

    r2 = client.get(f"/api/xhs/board/{BID}/notes")
    notes = r2.json()["data"]["notes"]
    assert notes[0]["cover"] == URL
    assert notes[0]["cover_local"] == f"/static/covers/{BID}/{N1}.png"


def test_failed_download_keeps_original_url_as_fallback(cover_root, client, monkeypatch):
    _stub_fail(monkeypatch)
    r = client.post("/api/xhs/ingest_board_notes", json={
        "board_id": BID,
        "notes": [{"note_id": N1, "title": "x", "cover": URL}],
    })
    body = r.json()["data"]
    assert body["covers"]["failed"] == 1
    assert body["covers"]["downloaded"] == 0

    note = BoardNoteStore.get(BID, N1)
    assert note["cover"] == URL
    assert "cover_local" not in note
    assert not (cover_root / BID / f"{N1}.png").exists()


def test_reingest_is_idempotent_no_redownload(cover_root, client, monkeypatch):
    calls: list[str] = []
    _stub_ok(monkeypatch, calls)

    payload = {"board_id": BID, "notes": [{"note_id": N1, "title": "x", "cover": URL}]}
    client.post("/api/xhs/ingest_board_notes", json=payload)
    assert len(calls) == 1

    # 第二次 ingest（重捕获）：文件已存在 → 跳过，不再发起下载
    r2 = client.post("/api/xhs/ingest_board_notes", json=payload)
    assert len(calls) == 1
    assert r2.json()["data"]["covers"]["skipped"] == 1
    assert r2.json()["data"]["covers"]["downloaded"] == 0


def test_empty_cover_field_does_not_erase_previous_cover(cover_root, client, monkeypatch):
    """分页 API 那条上报路径不带封面；后到的空 cover 不能擦掉先到的。"""
    _stub_ok(monkeypatch)
    client.post("/api/xhs/ingest_board_notes", json={
        "board_id": BID,
        "notes": [{"note_id": N1, "title": "有封面", "cover": URL}],
    })
    client.post("/api/xhs/ingest_board_notes", json={
        "board_id": BID,
        "notes": [{"note_id": N1, "title": "有封面", "cover": ""}],
    })
    note = BoardNoteStore.get(BID, N1)
    assert note["cover"] == URL
    assert note["cover_local"] == f"/static/covers/{BID}/{N1}.png"


def test_dangling_cover_local_falls_back_to_url(cover_root, client, monkeypatch):
    _stub_ok(monkeypatch)
    client.post("/api/xhs/ingest_board_notes", json={
        "board_id": BID,
        "notes": [{"note_id": N1, "title": "x", "cover": URL}],
    })
    # 模拟用户清缓存：本地文件被删
    (cover_root / BID / f"{N1}.png").unlink()

    notes = client.get(f"/api/xhs/board/{BID}/notes").json()["data"]["notes"]
    assert "cover_local" not in notes[0]
    assert notes[0]["cover"] == URL
    # 自愈落盘：悬空字段也从 store 里抹掉
    assert "cover_local" not in BoardNoteStore.get(BID, N1)


def test_invalid_urls_counted_as_failed_not_crash(cover_root, monkeypatch):
    _stub_ok(monkeypatch)
    stats = cover_persist.persist_covers(BID, [(N2, ""), (N3, "ftp://x/y.jpg")])
    assert stats["failed"] == 2
    assert stats["downloaded"] == 0


def test_concurrent_batch_downloads_all(cover_root, monkeypatch):
    """线程池 4 并发下 10 张全部落盘（不打网络，只验调度/落盘不丢）。"""
    _stub_ok(monkeypatch)
    ids = [f"{i:024x}" for i in range(1, 11)]
    stats = cover_persist.persist_covers(BID, [(i, URL) for i in ids])
    assert stats["downloaded"] == 10
    assert stats["failed"] == 0
    for nid in ids:
        assert (cover_root / BID / f"{nid}.png").is_file()


# ── 登录态预检 ────────────────────────────────────


def test_cookie_status_missing_cookie(monkeypatch):
    from app.services import platform_guard

    monkeypatch.setattr(platform_guard.CookieConfigManager, "get", lambda self, p: None)
    r = platform_guard.check_xhs_cookie()
    assert r["status"] == "missing"
    assert "捕获窗口" in r["action"]


def test_cookie_status_expired_without_web_session(monkeypatch):
    from app.services import platform_guard

    monkeypatch.setattr(platform_guard.CookieConfigManager, "get",
                        lambda self, p: "a1=xx; gid=yy")
    r = platform_guard.check_xhs_cookie()
    assert r["status"] == "expired"


def test_cookie_status_logged_in_from_ssr(monkeypatch):
    from app.services import platform_guard

    monkeypatch.setattr(platform_guard.CookieConfigManager, "get",
                        lambda self, p: "web_session=abc; a1=xx")

    class Resp:
        status_code = 200
        text = 'window.__INITIAL_STATE__={"user":{"loggedIn":true}}'

    monkeypatch.setattr(platform_guard.requests, "get", lambda *a, **k: Resp())
    r = platform_guard.check_xhs_cookie()
    assert r["status"] == "logged_in"


def test_cookie_status_expired_from_ssr(monkeypatch):
    from app.services import platform_guard

    monkeypatch.setattr(platform_guard.CookieConfigManager, "get",
                        lambda self, p: "web_session=abc; a1=xx")

    class Resp:
        status_code = 200
        text = 'window.__INITIAL_STATE__={"user":{"loggedIn":false}}'

    monkeypatch.setattr(platform_guard.requests, "get", lambda *a, **k: Resp())
    r = platform_guard.check_xhs_cookie()
    assert r["status"] == "expired"
    assert "重新登录" in r["action"]


def test_cookie_status_endpoint_shape(client, monkeypatch):
    from app.services import platform_guard

    monkeypatch.setattr(platform_guard, "check_platform_cookie", lambda p: {
        "platform": p, "status": "expired", "detail": "d", "action": "请重新登录"})
    data = client.get("/api/xhs/cookie_status").json()["data"]
    assert data["xhs"]["valid"] is False
    assert data["xhs"]["hint"] == "请重新登录"
    assert data["bilibili"]["valid"] is False


def test_cookie_status_unknown_is_non_blocking(client, monkeypatch):
    """探测本身失败（网络/风控）不能挡住用户。"""
    from app.services import platform_guard

    monkeypatch.setattr(platform_guard, "check_platform_cookie", lambda p: {
        "platform": p, "status": "unknown", "detail": "timeout", "action": ""})
    data = client.get("/api/xhs/cookie_status").json()["data"]
    assert data["xhs"]["valid"] is True
