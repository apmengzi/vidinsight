"""Agent 轨迹只读接口（T4 可观测）。

GET /api/agent/traces          最近 N 份轨迹文件（新→旧）
GET /api/agent/trace/{name}    单份轨迹全文（JSONL → 事件数组）

轻依赖：不 import 任何重服务，测试可以直接挂 TestClient。
"""
import json
import re
from pathlib import Path

from fastapi import APIRouter

from app.utils.response import ResponseWrapper as R
from app.services.agent_trace import TRACE_DIR

router = APIRouter()

_NAME_RE = re.compile(r"^[0-9A-Za-z_\-]+\.jsonl$")


@router.get("/agent/traces")
def list_traces(limit: int = 20):
    """最近轨迹列表（新→旧），limit 1-100。"""
    limit = max(1, min(limit, 100))
    try:
        files = sorted(
            TRACE_DIR.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )[:limit]
        items = [{
            "name": p.name,
            "size_bytes": p.stat().st_size,
            "mtime": int(p.stat().st_mtime),
        } for p in files]
        return R.success({"traces": items, "dir": str(TRACE_DIR)})
    except Exception as e:
        return R.error(msg=str(e), err="TRACE_LIST_FAILED")


@router.get("/agent/trace/{name}")
def read_trace(name: str):
    """读取单份轨迹：JSONL 逐行解析为事件数组。"""
    if not _NAME_RE.match(name):
        return R.error(msg="非法轨迹文件名", err="TRACE_NAME_INVALID")
    path = TRACE_DIR / name
    if not path.exists():
        return R.error(msg="轨迹不存在", err="TRACE_NOT_FOUND")
    events = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    events.append({"event": "corrupt_line", "raw": line[:200]})
        return R.success({"name": name, "events": events})
    except Exception as e:
        return R.error(msg=str(e), err="TRACE_READ_FAILED")
