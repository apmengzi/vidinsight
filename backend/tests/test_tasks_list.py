# -*- coding: utf-8 -*-
"""GET /api/tasks/list：历史页后端兜底端点。

契约：
- 列出 note_results 下全部任务（含失败任务），按创建时间倒序
- 成功任务带 title/platform/markdown，来源链接提取进 video_url
- limit/offset 分页，limit 上限 500
- _audio/_markdown 中间文件不算任务

只挂 note 路由到独立 FastAPI 实例（不 import main，避开其他测试
对 main 模块导入链的污染），专注端点自身行为。
"""
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.note import router as note_router


@pytest.fixture(scope="module")
def client():
    app = FastAPI()
    app.include_router(note_router, prefix="/api")
    return TestClient(app)


@pytest.fixture()
def seeded_results(tmp_path, monkeypatch):
    """造三个任务：SUCCESS（含完整 meta）、FAILED（只有 status）、中间文件。"""
    from app.routers import note as note_router
    monkeypatch.setattr(note_router, "NOTE_OUTPUT_DIR", str(tmp_path))

    now = time.time()
    # 任务 A：最新、成功、带 markdown 与来源链接
    ta = tmp_path / "aaa-success.status.json"
    ta.write_text(json.dumps({"status": "SUCCESS", "started_at": now}), encoding="utf-8")
    (tmp_path / "aaa-success.json").write_text(json.dumps({
        "audio_meta": {"title": "视频A", "platform": "bilibili", "video_id": "BV1",
                       "cover_url": "http://c/1.png", "duration": 61.5},
        "markdown": "来源链接：https://www.bilibili.com/video/BV1\n\n# 笔记A",
    }, ensure_ascii=False), encoding="utf-8")
    # 任务 B：更早、成功
    tb = tmp_path / "bbb-old.status.json"
    tb.write_text(json.dumps({"status": "SUCCESS", "started_at": now - 100}), encoding="utf-8")
    (tmp_path / "bbb-old.json").write_text(json.dumps({
        "audio_meta": {"title": "视频B", "platform": "xiaohongshu", "video_id": "nx1",
                       "cover_url": "", "duration": 0},
        "markdown": "# 笔记B",
    }, ensure_ascii=False), encoding="utf-8")
    # 任务 C：失败，只有 status 文件
    (tmp_path / "ccc-failed.status.json").write_text(
        json.dumps({"status": "FAILED", "started_at": now - 50}), encoding="utf-8")
    # 中间文件：不算任务
    (tmp_path / "aaa-success_audio.json").write_text("{}", encoding="utf-8")
    return tmp_path


def test_list_tasks_orders_and_fields(seeded_results, client):
    resp = client.get("/api/tasks/list")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 3
    ids = [it["task_id"] for it in data["items"]]
    # 倒序：A(now) > C(now-50) > B(now-100)
    assert ids == ["aaa-success", "ccc-failed", "bbb-old"]

    a = data["items"][0]
    assert a["status"] == "SUCCESS"
    assert a["title"] == "视频A"
    assert a["platform"] == "bilibili"
    assert a["markdown"].startswith("来源链接")
    assert a["video_url"] == "https://www.bilibili.com/video/BV1"
    assert a["duration"] == 61.5

    c = data["items"][1]
    assert c["status"] == "FAILED"
    assert c["title"] == "" and c["markdown"] == ""


def test_list_tasks_pagination(seeded_results, client):
    resp = client.get("/api/tasks/list", params={"limit": 1, "offset": 1})
    data = resp.json()["data"]
    assert data["total"] == 3
    assert len(data["items"]) == 1
    assert data["items"][0]["task_id"] == "ccc-failed"
