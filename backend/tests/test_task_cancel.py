# -*- coding: utf-8 -*-
"""T2 长任务取消：注册表语义与状态写入单元测试。

pytest 运行: pytest tests/test_task_cancel.py
独立运行:   python tests/test_task_cancel.py
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.enmus.task_status_enums import TaskStatus
from app.services import note as note_mod
from app.services.task_cancel import (
    TaskCancelled,
    cancel_task,
    check_cancelled,
    clear_task,
    is_cancelled,
)


def _bare_generator():
    """绕过重型 __init__（转写器加载），只测 _update_status 语义。"""
    return object.__new__(note_mod.NoteGenerator)


def _expect_raises(exc_type, fn):
    """pytest 缺席时的 raises 替代。"""
    try:
        fn()
    except exc_type:
        return
    except Exception as e:  # noqa: BLE001
        raise AssertionError(f"抛出了 {type(e).__name__} 而非 {exc_type.__name__}: {e}")
    raise AssertionError(f"未抛出预期的 {exc_type.__name__}")


def test_registry_roundtrip():
    assert not is_cancelled("t1")
    cancel_task("t1")
    assert is_cancelled("t1")
    clear_task("t1")
    assert not is_cancelled("t1")


def test_check_cancelled_raises():
    cancel_task("t2")
    _expect_raises(TaskCancelled, lambda: check_cancelled("t2"))
    clear_task("t2")
    check_cancelled("t2")  # 清除后不再抛


def test_update_status_raises_when_cancelled(tmp_path):
    """被取消后，推进到任何非终态状态都会在阶段边界被打断。"""
    note_mod.NOTE_OUTPUT_DIR = Path(tmp_path)
    cancel_task("t3")
    try:
        _expect_raises(
            TaskCancelled,
            lambda: _bare_generator()._update_status("t3", TaskStatus.PARSING),
        )
    finally:
        clear_task("t3")


def test_cancelled_terminal_write_allowed(tmp_path):
    """写 CANCELLED 终态必须放行，否则状态文件永远标不上。"""
    note_mod.NOTE_OUTPUT_DIR = Path(tmp_path)
    cancel_task("t4")
    try:
        _bare_generator()._update_status(
            "t4", TaskStatus.CANCELLED, message="用户已取消")
        with open(Path(tmp_path) / "t4.status.json", encoding="utf-8") as f:
            data = json.load(f)
        assert data["status"] == TaskStatus.CANCELLED.value
        assert data["message"] == "用户已取消"
    finally:
        clear_task("t4")


def test_normal_status_write_unaffected(tmp_path):
    """未取消任务的状态推进不受影响。"""
    note_mod.NOTE_OUTPUT_DIR = Path(tmp_path)
    _bare_generator()._update_status("t5", TaskStatus.DOWNLOADING)
    with open(Path(tmp_path) / "t5.status.json", encoding="utf-8") as f:
        assert json.load(f)["status"] == TaskStatus.DOWNLOADING.value


# 允许无 pytest 环境直接运行: python tests/test_task_cancel.py
if __name__ == "__main__":
    import inspect
    import tempfile
    import traceback

    failed = 0
    for name, fn in list(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        try:
            params = list(inspect.signature(fn).parameters)
            if params:
                with tempfile.TemporaryDirectory() as td:
                    fn(td)
            else:
                fn()
            print(f"PASS {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    raise SystemExit(1 if failed else 0)


# ── 状态可见性计时（2026-10-18 Must Fix）──
# 锁住三点：①阶段切换累计耗时 ②PARSING 重置计时（重试任务复用 task_id，
# 不重置会把两次运行的时长加在一起——审查抓到的真 bug）③终态不再计时

def test_status_timing_accumulates_and_resets_on_retry(tmp_path, monkeypatch):
    from app.services import note as note_mod
    from app.enmus.task_status_enums import TaskStatus

    monkeypatch.setattr(note_mod, "NOTE_OUTPUT_DIR", tmp_path)
    gen = note_mod.NoteGenerator.__new__(note_mod.NoteGenerator)

    # 第一次运行：PARSING → TRANSCRIBING → FAILED
    gen._update_status("t1", TaskStatus.PARSING)
    gen._update_status("t1", TaskStatus.TRANSCRIBING, progress=10)
    import time as _t
    _t.sleep(0.05)
    gen._update_status("t1", TaskStatus.FAILED, message="x")
    p1 = json.loads((tmp_path / "t1.status.json").read_text(encoding="utf-8"))
    assert "PARSING" in p1.get("stage_times", {}), "阶段切换应累计耗时"
    assert p1["elapsed_s"] >= 0

    # 重试（同 task_id，从 PARSING 重新开始）：计时必须归零
    _t.sleep(0.05)
    gen._update_status("t1", TaskStatus.PARSING)
    p2 = json.loads((tmp_path / "t1.status.json").read_text(encoding="utf-8"))
    assert p2["elapsed_s"] < 0.5, f"重试应重置计时，实际 elapsed_s={p2['elapsed_s']}"
    assert p2.get("stage_times", {}) == {}, "重试应清空阶段耗时"
