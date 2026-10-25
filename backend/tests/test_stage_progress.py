# -*- coding: utf-8 -*-
"""阶段内进度（stage_progress）单元测试。

覆盖三阶段：下载（yt-dlp progress_hooks）、转写（on_progress）、LLM 分块（i/N）。
全部使用 stub，不真下载、不真调 LLM；唯一真实 IO 是 tmp_path 下的状态文件。

pytest 运行: pytest tests/test_stage_progress.py -q
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.downloaders.base import Downloader
from app.enmus.task_status_enums import TaskStatus
from app.models.audio_model import AudioDownloadResult
from app.models.gpt_model import GPTSource
from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.services import note as note_mod
from app.utils.progress import (
    ProgressThrottle,
    build_ytdlp_opts_with_progress,
    clamp_pct,
    make_ytdlp_progress_hook,
)


# ── 工具 ────────────────────────────────────────────────

def _bare_generator():
    """绕过重型 __init__（会加载 whisper 模型），只测状态与阶段接线。"""
    return object.__new__(note_mod.NoteGenerator)


def _read_status(tmp_path: Path, task_id: str) -> dict:
    return json.loads((tmp_path / f"{task_id}.status.json").read_text(encoding="utf-8"))


class _FakeDownloader(Downloader):
    """只吐进度、不落盘的下载器 stub。"""

    def __init__(self, reports=(12.5, 37.5)):
        super().__init__()
        self.reports = list(reports)
        self.reporter_seen = []

    def download(self, video_url, output_dir=None, quality="fast",
                 need_video=False, skip_download=False):
        reporter = self.progress_reporter
        assert reporter is not None, "NoteGenerator 应通过 progress_scope 注入回调"
        self.reporter_seen.append(reporter)
        for pct in self.reports:
            reporter(pct)
        return AudioDownloadResult(
            file_path="fake.mp3", title="t", duration=1.0, cover_url=None,
            platform="bilibili", video_id="v1", raw_info={}, video_path=None)


class _FakeTranscriber:
    def __init__(self, fractions=(0.25, 0.5)):
        self.fractions = fractions

    def transcript(self, file_path: str, on_progress=None):
        assert on_progress is not None
        for frac in self.fractions:
            on_progress(frac)
        return TranscriptResult(
            language="zh", full_text="hello",
            segments=[TranscriptSegment(start=0.0, end=1.0, text="hello")])


class _FakeGPT:
    """最小 GPT stub：按块数上报进度，验证 note.py 的接线。"""

    def __init__(self, progress_values=(25.0, 100.0)):
        self.progress_values = progress_values
        self.received = []

    def summarize(self, source):
        self.received.append(source)
        for pct in self.progress_values:
            assert source.on_progress is not None
            source.on_progress(pct)
        return "# note"


# ── 1. 阶段内百分比语义（写入 / 读取）────────────────────

def test_stage_progress_null_on_stage_entry_then_value(tmp_path, monkeypatch):
    monkeypatch.setattr(note_mod, "NOTE_OUTPUT_DIR", tmp_path)
    gen = _bare_generator()

    gen._update_status("t1", TaskStatus.PARSING)
    assert _read_status(tmp_path, "t1")["stage_progress"] is None

    # 进入下载阶段但尚无百分比 → 显式 null，不能写 0 假装开始
    gen._update_status("t1", TaskStatus.DOWNLOADING)
    assert _read_status(tmp_path, "t1")["stage_progress"] is None

    gen._update_status("t1", TaskStatus.DOWNLOADING, stage_progress=42.5)
    data = _read_status(tmp_path, "t1")
    assert data["stage_progress"] == 42.5
    assert data["status"] == TaskStatus.DOWNLOADING.value

    # 同阶段不带值再写（如其他状态消息）→ 沿用上次值
    gen._update_status("t1", TaskStatus.DOWNLOADING, message="still downloading")
    assert _read_status(tmp_path, "t1")["stage_progress"] == 42.5

    # 换阶段 → 归 null
    gen._update_status("t1", TaskStatus.TRANSCRIBING)
    assert _read_status(tmp_path, "t1")["stage_progress"] is None

    # 转写阶段内百分比
    gen._update_status("t1", TaskStatus.TRANSCRIBING, stage_progress=80.0)
    assert _read_status(tmp_path, "t1")["stage_progress"] == 80.0

    # 终态没传 → 保留最后已知值（前端可显示"卡在 80% 时失败"）
    gen._update_status("t1", TaskStatus.FAILED, message="boom")
    data = _read_status(tmp_path, "t1")
    assert data["stage_progress"] == 80.0
    assert data["message"] == "boom"


def test_stage_progress_clamped_and_global_progress_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(note_mod, "NOTE_OUTPUT_DIR", tmp_path)
    gen = _bare_generator()

    gen._update_status("t2", TaskStatus.PARSING)
    gen._update_status("t2", TaskStatus.DOWNLOADING, progress=33, stage_progress=140)
    data = _read_status(tmp_path, "t2")
    assert data["stage_progress"] == 100.0, "阶段内百分比必须夹到 0-100"
    assert data["progress"] == 33, "全局 progress 必须保持原语义"

    gen._update_status("t2", TaskStatus.DOWNLOADING, stage_progress=-5)
    assert _read_status(tmp_path, "t2")["stage_progress"] == 0.0


def test_retry_resets_stage_progress(tmp_path, monkeypatch):
    """重试复用 task_id：PARSING 开局必须清掉上一轮残留的阶段百分比。"""
    monkeypatch.setattr(note_mod, "NOTE_OUTPUT_DIR", tmp_path)
    gen = _bare_generator()

    gen._update_status("t3", TaskStatus.PARSING)
    gen._update_status("t3", TaskStatus.TRANSCRIBING, stage_progress=66.0)
    gen._update_status("t3", TaskStatus.PARSING)
    assert _read_status(tmp_path, "t3")["stage_progress"] is None


# ── 2. 下载：yt-dlp progress_hooks ─────────────────────

def test_ytdlp_hook_converts_bytes_to_percent_and_ignores_unknown_total():
    seen = []
    hook = make_ytdlp_progress_hook(seen.append, throttle=None)

    hook({"status": "downloading", "downloaded_bytes": 0, "total_bytes": 1000})
    hook({"status": "downloading", "downloaded_bytes": 250, "total_bytes": 1000})
    hook({"status": "downloading", "downloaded_bytes": 500})  # 无总量 → 不上报
    hook({"status": "finished", "total_bytes": 1000})

    assert seen[0] == 0.0
    assert seen[1] == 25.0
    assert seen[-1] == 100.0
    assert all(isinstance(v, float) for v in seen)
    assert all(0.0 <= v <= 100.0 for v in seen)


def test_ytdlp_hook_uses_estimate_and_is_monotonic():
    seen = []
    hook = make_ytdlp_progress_hook(seen.append, throttle=None)

    hook({"status": "downloading", "downloaded_bytes": 900, "total_bytes_estimate": 1000})
    hook({"status": "downloading", "downloaded_bytes": 100, "total_bytes": 1000})
    assert seen == [90.0, 90.0], "分段下载回退的百分比不能被写回去"


def test_ytdlp_hook_swallows_errors():
    def boom(_pct):
        raise RuntimeError("disk full")

    hook = make_ytdlp_progress_hook(boom, throttle=None)
    hook({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 2})  # 不抛


def test_throttle_limits_to_one_per_second():
    now = [1000.0]
    throttle = ProgressThrottle(min_interval=1.0, clock=lambda: now[0])

    assert throttle.allow("k") is True
    now[0] += 0.2
    assert throttle.allow("k") is False
    now[0] += 0.9
    assert throttle.allow("k") is True
    # 不同 key 互不影响（并发任务/不同阶段）
    assert throttle.allow("other") is True


def test_build_opts_does_not_mutate_input():
    original = {"format": "best"}
    opts = build_ytdlp_opts_with_progress(original, lambda _p: None)
    assert "progress_hooks" not in original
    assert len(opts["progress_hooks"]) == 1
    # 未注入 reporter 时原样返回
    assert build_ytdlp_opts_with_progress(original, None) is original


def test_download_stage_writes_stage_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(note_mod, "NOTE_OUTPUT_DIR", tmp_path)
    gen = _bare_generator()
    gen.video_img_urls = []
    gen.video_path = None

    downloader = _FakeDownloader(reports=(12.5, 37.5))
    # 关掉节流以便两次上报都落盘（节流本身另有单测）
    downloader._progress_local.throttle = ProgressThrottle(min_interval=0.0)

    audio = gen._download_media(
        downloader=downloader,
        video_url="https://example.com/v",
        quality="fast",
        audio_cache_file=tmp_path / "dl1_audio.json",
        status_phase=TaskStatus.DOWNLOADING,
        platform="bilibili",
        output_path=str(tmp_path),
        screenshot=False,
        video_understanding=False,
        video_interval=0,
        grid_size=[],
    )

    assert audio.video_id == "v1"
    data = _read_status(tmp_path, "dl1")
    assert data["status"] == TaskStatus.DOWNLOADING.value
    assert data["stage_progress"] == 37.5


# ── 3. 转写：consumed/total → 阶段内百分比 ───────────────

def test_transcribe_stage_writes_stage_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(note_mod, "NOTE_OUTPUT_DIR", tmp_path)
    gen = _bare_generator()
    gen.transcriber = _FakeTranscriber(fractions=(0.25, 0.5))

    transcript = gen._transcribe_audio(
        audio_file="fake.mp3",
        transcript_cache_file=tmp_path / "tr1_transcript.json",
        status_phase=TaskStatus.TRANSCRIBING,
    )

    assert transcript.full_text == "hello"
    data = _read_status(tmp_path, "tr1")
    assert data["status"] == TaskStatus.TRANSCRIBING.value
    assert data["stage_progress"] == 50.0
    assert data["progress"] == 50, "旧前端读的全局 progress 字段继续写"


# ── 4. LLM：分块 i/N + SUMMARIZING 计时 ─────────────────

def _make_audio_meta() -> AudioDownloadResult:
    return AudioDownloadResult(
        file_path="fake.mp3", title="t", duration=10.0, cover_url=None,
        platform="bilibili", video_id="v1", raw_info={"tags": []}, video_path=None)


def _make_transcript(texts=("aaa", "bbb")) -> TranscriptResult:
    segments = [TranscriptSegment(start=float(i), end=float(i) + 1, text=t)
                for i, t in enumerate(texts)]
    return TranscriptResult(language="zh", full_text=" ".join(texts), segments=segments)


def test_summarize_writes_progress_and_llm_stage_time_into_real_task_file(tmp_path, monkeypatch):
    """LLM 阶段计时必须进 <task_id>.status.json（历史 bug：写进了 *_markdown 孤立文件）。"""
    monkeypatch.setattr(note_mod, "NOTE_OUTPUT_DIR", tmp_path)
    gen = _bare_generator()
    gpt = _FakeGPT(progress_values=(25.0, 100.0))

    markdown = gen._summarize_text(
        audio_meta=_make_audio_meta(),
        transcript=_make_transcript(),
        gpt=gpt,
        markdown_cache_file=tmp_path / "llm1_markdown.md",
        link=False, screenshot=False, formats=[], style=None, extras=None,
        video_img_urls=[], task_id="llm1",
    )
    assert markdown == "# note"
    assert gpt.received[0].on_progress is not None, "GPTSource 必须带上进度回调"

    data = _read_status(tmp_path, "llm1")
    assert data["status"] == TaskStatus.SUMMARIZING.value
    assert data["stage_progress"] == 100.0
    assert not (tmp_path / "llm1_markdown.status.json").exists(), \
        "不应再产生 *_markdown.status.json 孤立状态文件"

    # 进入 SUCCESS 后，SUMMARIZING 的耗时必须出现在 stage_times 里
    gen._update_status("llm1", TaskStatus.SUCCESS, stage_progress=100.0)
    data = _read_status(tmp_path, "llm1")
    assert "SUMMARIZING" in data["stage_times"], data
    assert data["stage_progress"] == 100.0


def test_summarize_task_id_backward_compatible_without_explicit_id(tmp_path, monkeypatch):
    """未显式传 task_id 时，从 *_markdown.md 反推 task_id（去掉 _markdown 后缀）。"""
    monkeypatch.setattr(note_mod, "NOTE_OUTPUT_DIR", tmp_path)
    gen = _bare_generator()

    gen._summarize_text(
        audio_meta=_make_audio_meta(),
        transcript=_make_transcript(),
        gpt=_FakeGPT(progress_values=()),
        markdown_cache_file=tmp_path / "llm2_markdown.md",
        link=False, screenshot=False, formats=[], style=None, extras=None,
        video_img_urls=[],
    )
    assert (tmp_path / "llm2.status.json").exists()
    assert not (tmp_path / "llm2_markdown.status.json").exists()


def test_universal_gpt_reports_chunk_progress(tmp_path, monkeypatch):
    """分块循环按 i/N 上报：2 块 → 45%（90 的一半），合并后 100%。"""
    from app.gpt.universal_gpt import UniversalGPT

    monkeypatch.setenv("NOTE_OUTPUT_DIR", str(tmp_path))

    class _Msg:
        content = "partial"

    class _Choice:
        message = _Msg()
        finish_reason = "stop"

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        def create(self, **_kwargs):
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    gpt = UniversalGPT(client=_Client(), model="fake")
    # 让每个 segment 单独成块：上限 = 空 prompt 大小 + 单段（含时间前缀）的余量
    base_size = gpt._estimate_messages_bytes(
        gpt.create_messages([], title="t", tags=[], _format=[], style=None, extras=None))
    gpt.max_request_bytes = base_size + 120

    # 块级进度按 1s 节流：把单调钟推进 2s，验证每次块完成都能上报
    clock = [0.0]

    def _tick():
        clock[0] += 2.0
        return clock[0]

    monkeypatch.setattr("app.gpt.universal_gpt.time.monotonic", _tick)

    seen = []
    source = GPTSource(
        segment=_make_transcript(("a" * 80, "b" * 80)).segments,
        title="t", tags=[], checkpoint_key=None, on_progress=seen.append)

    result = gpt.summarize(source)

    assert result == "partial"
    assert seen[0] == 0.0
    assert 45.0 in seen, f"第一块完成应上报 45%，实际 {seen}"
    assert seen[-1] == 100.0


def test_universal_gpt_without_on_progress_stays_silent(tmp_path, monkeypatch):
    """未注入回调时，GPT 实现不应产生任何进度开销/异常。"""
    from app.gpt.universal_gpt import UniversalGPT

    monkeypatch.setenv("NOTE_OUTPUT_DIR", str(tmp_path))

    class _Msg:
        content = "ok"

    class _Choice:
        message = _Msg()
        finish_reason = "stop"

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        def create(self, **_kwargs):
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    gpt = UniversalGPT(client=_Client(), model="fake")
    result = gpt.summarize(GPTSource(segment=_make_transcript(("x",)).segments, title="t", tags=[]))
    assert result == "ok"


# ── 5. 路由透出 ─────────────────────────────────────────

def test_task_status_route_exposes_stage_progress(tmp_path, monkeypatch):
    """task_status 的 data 必须带 stage_progress（含 null 语义）。"""
    import json as _json
    from app.routers import note as note_router

    monkeypatch.setattr(note_router, "NOTE_OUTPUT_DIR", str(tmp_path))

    # 阶段内可量化
    (tmp_path / "r1.status.json").write_text(_json.dumps({
        "status": TaskStatus.TRANSCRIBING.value,
        "stage_progress": 61.5,
        "elapsed_s": 3.0,
        "stage_times": {"PARSING": 0.2},
    }), encoding="utf-8")
    resp = note_router.get_task_status("r1")
    payload = _json.loads(resp.body)
    assert payload["data"]["stage_progress"] == 61.5
    assert payload["data"]["progress"] is None  # 全局 progress 缺省保持原样

    # 阶段内不可量化 → null（前端据此隐藏进度条）
    (tmp_path / "r2.status.json").write_text(_json.dumps({
        "status": TaskStatus.SUMMARIZING.value,
        "stage_progress": None,
    }), encoding="utf-8")
    payload = _json.loads(note_router.get_task_status("r2").body)
    assert "stage_progress" in payload["data"]
    assert payload["data"]["stage_progress"] is None

    # 成功终态也透出
    (tmp_path / "r3.status.json").write_text(_json.dumps({
        "status": TaskStatus.SUCCESS.value,
        "stage_progress": 100.0,
        "stage_times": {"PARSING": 0.4, "SUMMARIZING": 12.3},
    }), encoding="utf-8")
    (tmp_path / "r3.json").write_text(_json.dumps({"markdown": "# x"}), encoding="utf-8")
    payload = _json.loads(note_router.get_task_status("r3").body)
    assert payload["data"]["stage_progress"] == 100.0
    assert payload["data"]["stage_times"]["SUMMARIZING"] == 12.3


# ── 6. clamp 语义 ───────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (0, 0.0), (12.345, 12.3), (100, 100.0), (150, 100.0), (-3, 0.0),
    (None, None), ("abc", None), (float("nan"), None), (float("inf"), None),
])
def test_clamp_pct(raw, expected):
    assert clamp_pct(raw) == expected
