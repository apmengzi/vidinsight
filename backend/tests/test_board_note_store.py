"""收藏专辑 BoardNoteStore + ingest_board_notes 端点测试（2026-09-21 收藏专辑转向）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from fastapi import FastAPI  # noqa: E402

from app.routers.note import router as note_router  # noqa: E402
from app.services.board_note_store import BoardNoteStore  # noqa: E402

BID = "a" * 24
N1 = "b" * 24
N2 = "c" * 24


def setup_function():
    BoardNoteStore.clear(BID)


def teardown_function():
    BoardNoteStore.clear(BID)


def test_store_upsert_and_order():
    BoardNoteStore.put(BID, {"note_id": N1, "title": "第一条", "type": "video"})
    BoardNoteStore.put(BID, {"note_id": N2, "title": "第二条", "type": "normal"})
    notes = BoardNoteStore.list_notes(BID)
    assert [n["note_id"] for n in notes] == [N1, N2]  # 正序：先捕获在前（页面从新到旧，先捕获=最新）
    # upsert：同 id 覆盖
    BoardNoteStore.put(BID, {"note_id": N1, "title": "改名", "type": "video"})
    notes = BoardNoteStore.list_notes(BID)
    assert len(notes) == 2
    assert [n for n in notes if n["note_id"] == N1][0]["title"] == "改名"


def test_store_rejects_invalid_ids():
    BoardNoteStore.put(BID, {"note_id": "bad", "title": "x"})
    BoardNoteStore.put("", {"note_id": N1, "title": "x"})
    assert BoardNoteStore.size(BID) == 0


def test_ingest_endpoint_roundtrip():
    app = FastAPI()
    app.include_router(note_router, prefix="/api")
    client = TestClient(app)
    body = {"board_id": BID, "notes": [
        {"note_id": N1, "title": "视频笔记", "type": "video", "xsec_token": "TOK1", "xsec_source": "pc_board"},
        {"note_id": "zzz", "title": "非法id应被丢弃", "type": ""},
    ]}
    r = client.post("/api/xhs/ingest_board_notes", json=body)
    assert r.status_code == 200
    assert r.json()["data"]["accepted"] == 1
    # 列表可读 + detail_cached 字段存在
    r2 = client.get(f"/api/xhs/board/{BID}/notes")
    data = r2.json()["data"]
    assert data["total"] == 1
    assert data["notes"][0]["note_id"] == N1
    assert "detail_cached" in data["notes"][0]


def test_ingest_endpoint_rejects_bad_board():
    app = FastAPI()
    app.include_router(note_router, prefix="/api")
    client = TestClient(app)
    r = client.post("/api/xhs/ingest_board_notes", json={"board_id": "xx", "notes": []})
    assert r.status_code == 200
    assert r.json()["code"] != 0


def test_expand_board_rejects_non_board_url():
    from app.downloaders.xiaohongshu_downloader import XiaoHongShuDownloader
    d = XiaoHongShuDownloader()
    d._resolve_url = lambda u: u  # type: ignore
    try:
        d.expand_board("https://www.xiaohongshu.com/explore/" + "d" * 24)
        raise AssertionError("非 board 链接必须抛错")
    except Exception as e:
        assert getattr(e, "err", "") == "XHS_PAGE_FETCH_FAILED"
