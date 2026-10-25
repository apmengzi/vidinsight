import os
import shutil
import sys
import time
from pathlib import Path

try:
    # faster-whisper >= 1.1 才有批处理流水线；缺失时仅走顺序解码
    from faster_whisper import BatchedInferencePipeline, WhisperModel
except ImportError:  # pragma: no cover - 老版本兼容
    from faster_whisper import WhisperModel  # type: ignore[no-redef]

    BatchedInferencePipeline = None  # type: ignore[assignment]

from app.decorators.timeit import timeit
from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.transcriber.base import Transcriber
from app.transcriber.whisper_models import (
    hf_cache_dirname,
    is_local_target,
    resolve_whisper_model,
)
from app.utils.logger import get_logger
from app.utils.path_helper import get_model_dir
from events import transcription_finished

logger = get_logger(__name__)


def _env_int(name: str, default: int) -> int:
    """读取整数环境变量；非法值回退默认并告警，避免静默改变解码行为。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"环境变量 {name}={raw!r} 不是整数，回退默认值 {default}")
        return default


def _venv_nvidia_bin_dirs() -> list[str]:
    """nvidia-* wheel 提供的 DLL bin 目录（免装 CUDA Toolkit 的运行库来源）。

    两种布局：
    - venv 源码运行：<venv>/Lib/site-packages/nvidia/*/bin
    - PyInstaller 打包：nvidia DLL 经 build.bat --add-data 收进 _internal/nvidia/*/bin
      （frozen 下 sys.prefix 即 _internal），布局为 <sys.prefix>/nvidia/*/bin
    """
    candidates = [
        Path(sys.prefix) / "Lib" / "site-packages" / "nvidia",
        Path(sys.prefix) / "nvidia",
    ]
    # PyInstaller frozen：_MEIPASS 指向 _internal，是 add-data 数据的可靠落点
    if hasattr(sys, "_MEIPASS"):
        candidates.append(Path(sys._MEIPASS) / "nvidia")
    dirs: list[str] = []
    for nvidia_dir in candidates:
        if nvidia_dir.is_dir():
            dirs.extend(str(p) for p in sorted(nvidia_dir.glob("*/bin")) if p.is_dir())
    return dirs


# add_dll_directory 返回的句柄一旦被 GC，目录注册即失效；必须模块级持有
_NVIDIA_DLL_HANDLES: list = []


def _register_nvidia_dll_dirs() -> None:
    # shutil.which 找到 ≠ Windows 加载器能加载：必须注册
    # 1) PATH 前置：ctranslate2 内部用裸 LoadLibrary，只认 PATH
    # 2) add_dll_directory：供 cudnn 各 DLL 之间的依赖解析
    if _NVIDIA_DLL_HANDLES:
        return
    bin_dirs = _venv_nvidia_bin_dirs()
    for bin_dir in bin_dirs:
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
        _NVIDIA_DLL_HANDLES.append(os.add_dll_directory(bin_dir))


def _find_windows_dll(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found

    cuda_path = os.getenv("CUDA_PATH")
    if cuda_path:
        candidate = Path(cuda_path) / "bin" / name
        if candidate.exists():
            return str(candidate)

    for bin_dir in _venv_nvidia_bin_dirs():
        candidate = Path(bin_dir) / name
        if candidate.exists():
            return str(candidate)

    # shutil.which 在 Windows 按 PATHEXT 匹配，.dll 不在其中——PATH 上的
    # DLL 目录（如 start_gpu.py 前置的打包版 CUDA 运行库）永远搜不到。
    # 逐目录直查文件名兜底，与上面 venv 扫描同款逻辑。
    for bin_dir in os.environ.get("PATH", "").split(os.pathsep):
        if not bin_dir:
            continue
        candidate = Path(bin_dir) / name
        if candidate.exists():
            return str(candidate)

    return None


def _cuda_runtime_ready() -> tuple[bool, str]:
    if os.name != "nt":
        return True, ""

    _register_nvidia_dll_dirs()
    required = ["cudnn_ops64_9.dll", "cublas64_12.dll"]
    missing = [name for name in required if not _find_windows_dll(name)]
    if missing:
        return (
            False,
            "缺少 CUDA 运行库: "
            + ", ".join(missing)
            + "。请安装 cuDNN 9，并确认 CUDA/cuDNN 的 bin 目录在 PATH 中。",
        )

    return True, ""


class WhisperTranscriber(Transcriber):
    def __init__(
        self,
        model_size: str = "base",
        device: str = "cpu",
        compute_type: str | None = None,
        cpu_threads: int | None = None,
    ):
        requested_device = (device or os.getenv("WHISPER_DEVICE", "cuda")).lower()
        self.requested_device = requested_device
        self.device = "cuda" if requested_device == "cuda" else "cpu"
        if self.device == "cuda":
            cuda_ready, reason = _cuda_runtime_ready()
            if not cuda_ready:
                logger.warning(f"fast-whisper CUDA 运行库不可用，降级到 CPU。{reason}")
                self.device = "cpu"

        self.compute_type = compute_type or os.getenv(
            "WHISPER_COMPUTE_TYPE",
            "float16" if self.device == "cuda" else "int8",
        )
        self.model_size = model_size
        # CPU 转写默认吃 4 线程：写死 1 会让多核机器在长音频上白白慢数倍
        self.cpu_threads = cpu_threads or int(os.getenv("WHISPER_CPU_THREADS", "4"))

        model_dir = get_model_dir("whisper")
        try:
            self.model = self._build_model(model_size, model_dir)
        except Exception as e:
            if self.device == "cuda" and requested_device == "cuda":
                logger.warning(
                    "fast-whisper CUDA 加载失败，降级到 CPU。"
                    f"原因: {e}。如果要使用 GPU，请确认安装的是 CUDA 版 torch，"
                    "并且 ctranslate2/faster-whisper 所需 CUDA/cuDNN 运行库可用。"
                )
                self.device = "cpu"
                self.compute_type = compute_type or os.getenv("WHISPER_COMPUTE_TYPE", "int8")
                self.model = self._build_model(model_size, model_dir)
            else:
                logger.warning(f"加载 whisper-{model_size} 失败: {e}; 清理 cache 后重试")
                self._purge_cache(model_dir, model_size)
                self.model = self._build_model(model_size, model_dir)

        logger.info(
            f"fast-whisper 已加载: model={model_size}, device={self.device}, "
            f"compute_type={self.compute_type}"
        )

        # GPU 走批处理流水线（BatchedInferencePipeline）：按停顿切成句子级窗口后
        # 一次解码 batch_size 个窗口，把顺序解码下的 GPU 空闲填满。
        # CPU 保持原顺序路径：ctranslate2 的批处理在 CPU 上收益小且更吃内存。
        self.batched_pipeline = self._build_batched_pipeline()

    def _build_batched_pipeline(self):
        if self.device != "cuda":
            return None
        if BatchedInferencePipeline is None:
            logger.warning(
                "当前 faster-whisper 版本没有 BatchedInferencePipeline，回退顺序解码"
            )
            return None
        try:
            pipeline = BatchedInferencePipeline(model=self.model)
            logger.info("fast-whisper 批处理流水线已启用（BatchedInferencePipeline）")
            return pipeline
        except Exception as e:
            logger.warning(f"BatchedInferencePipeline 初始化失败，回退顺序解码: {e}")
            return None

    def _build_model(self, model_size: str, model_dir: str) -> WhisperModel:
        target = resolve_whisper_model(model_size)
        return WhisperModel(
            model_size_or_path=target,
            device=self.device,
            compute_type=self.compute_type,
            download_root=model_dir,
            cpu_threads=self.cpu_threads,
        )

    @staticmethod
    def _purge_cache(model_dir: str, model_size: str) -> None:
        try:
            target = resolve_whisper_model(model_size)
        except Exception:
            target = model_size
        if is_local_target(target):
            logger.warning(
                f"模型 {model_size} 指向本地路径 {target}，加载失败时不清理用户文件。"
                "请检查该目录是否完整。"
            )
            return

        candidates = [
            Path(model_dir) / hf_cache_dirname(target),
            Path(model_dir) / f"whisper-{model_size}",
        ]
        for path in candidates:
            if path.exists():
                logger.info(f"清理损坏 cache: {path}")
                shutil.rmtree(path, ignore_errors=True)

    def _transcribe_sequential(self, file_path: str, beam_size: int):
        return self.model.transcribe(
            file_path,
            beam_size=beam_size,
            vad_filter=True,
        )

    def _transcribe_batched(self, file_path: str, beam_size: int, batch_size: int):
        # 坑 1：BatchedInferencePipeline.transcribe 的 without_timestamps 默认是 True
        # （见 faster_whisper/transcribe.py:284），不显式关掉会让所有 segment 丢失
        # 时间戳，下游时间轴全变 0。必须显式传 False。
        # 坑 2：批处理对每个 VAD chunk 只 generate 一次，不做 chunk 内的 seek 迭代
        # （transcribe.py:536-558），而默认 VAD（min_silence=2000ms）会把 29s 内的
        # 多句话并成一块 → 除第一句外全被丢弃（实测 12 句只出 6 句）。
        # 因此这里自己按停顿切出句子级 clip_timestamps，保证每块都能被单次解码覆盖。
        from faster_whisper.audio import decode_audio
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        audio = decode_audio(file_path, sampling_rate=self.model.feature_extractor.sampling_rate)
        vad_options = VadOptions(
            min_silence_duration_ms=max(0, _env_int("WHISPER_VAD_MIN_SILENCE_MS", 500)),
            # 模型单窗口 30s：超长连续语音必须切开，否则同样会被截断
            max_speech_duration_s=30,
        )
        # 注意：不能用 merge_segments()，它会把相邻语音段重新并回 30s 大块
        # （vad.py:330-360），又会踩回 chunk 内漏句的坑。
        clips = get_speech_timestamps(audio, vad_options)
        if not clips:
            # 纯静音/无语音：交给上游 vad_filter 走空结果路径，避免 RuntimeError
            return self.batched_pipeline.transcribe(
                audio,
                beam_size=beam_size,
                batch_size=batch_size,
                vad_filter=True,
                without_timestamps=False,
                word_timestamps=False,
            )
        return self.batched_pipeline.transcribe(
            audio,
            beam_size=beam_size,
            batch_size=batch_size,
            clip_timestamps=clips,
            without_timestamps=False,
            word_timestamps=False,
        )

    @staticmethod
    def _consume_segments(segments_raw, info, on_progress=None, state: dict | None = None):
        """把 faster-whisper 的 Segment 流转成 TranscriptSegment。

        顺序路径与批处理路径产出的 Segment 字段一致（id/seek/start/end/text/
        tokens/avg_logprob/...，见 faster_whisper/transcribe.py:48-60 与 563-579），
        下游只用 start/end/text，这里统一做一次时间戳消毒，两条路径共用。
        state["consumed"] 记录已产出条数：生成器惰性求值，中途抛错时据此判断
        能否安全回退（已产出内容不能丢，否则会重复/截断）。
        """
        duration = float(getattr(info, "duration", 0.0) or 0.0)
        last_report = 0.0

        segments = []
        full_text = ""
        prev_end = 0.0
        for seg in segments_raw:
            if state is not None:
                state["consumed"] = state.get("consumed", 0) + 1
            text = seg.text.strip()
            # 消毒：VAD/贪心解码下偶发 inf/nan 时间戳（会让 JSON 序列化直接 500），
            # 用前段末尾或音频总时长兜底 clamp 成有限值
            start = seg.start if seg.start == seg.start and seg.start not in (float("inf"), float("-inf")) else prev_end
            end = seg.end if seg.end == seg.end and seg.end not in (float("inf"), float("-inf")) else max(start, prev_end, duration if duration > 0 else prev_end)
            if not (end >= start):
                end = start
            prev_end = end
            full_text += text + " "
            segments.append(
                TranscriptSegment(
                    start=start,
                    end=end,
                    text=text,
                )
            )
            progress_ref = end
            if on_progress is not None and duration > 0:
                now = time.time()
                if now - last_report >= 1.0:  # 节流：1 秒上报一次
                    last_report = now
                    on_progress(min(progress_ref / duration, 0.99))
        return segments, full_text

    @timeit
    def transcript(self, file_path: str, on_progress=None) -> TranscriptResult:
        # 默认值：beam=1（沿用本文件原默认，未静默改变行为）；
        # batch=16 为 RTX 5060 8GB 上的实测拐点（16→32 无进一步收益）。
        beam_size = max(1, _env_int("WHISPER_BEAM_SIZE", 1))
        batch_size = max(1, _env_int("WHISPER_BATCH_SIZE", 16))
        try:
            if self.batched_pipeline is not None:
                mode = f"batched(batch_size={batch_size}, beam_size={beam_size})"
                state = {"consumed": 0}
                try:
                    segments_raw, info = self._transcribe_batched(file_path, beam_size, batch_size)
                    segments, full_text = self._consume_segments(
                        segments_raw, info, on_progress, state
                    )
                except Exception as e:
                    # 生成器惰性求值：已产出内容不能丢（重跑会重复/截断），
                    # 只在一条都没产出时安全回退顺序路径。
                    if state["consumed"] > 0:
                        raise
                    logger.warning(f"批处理转写失败，回退顺序解码: {e}")
                    mode = f"sequential-fallback(beam_size={beam_size})"
                    segments_raw, info = self._transcribe_sequential(file_path, beam_size)
                    segments, full_text = self._consume_segments(segments_raw, info, on_progress)
            else:
                mode = f"sequential(beam_size={beam_size})"
                segments_raw, info = self._transcribe_sequential(file_path, beam_size)
                segments, full_text = self._consume_segments(segments_raw, info, on_progress)

            logger.info(
                f"fast-whisper 转写完成: mode={mode}, segments={len(segments)}, "
                f"duration={float(getattr(info, 'duration', 0.0) or 0.0):.1f}s"
            )
            return TranscriptResult(
                language=info.language,
                full_text=full_text.strip(),
                segments=segments,
                raw=info,
            )
        except Exception as e:
            logger.exception(f"转写失败: {e}")
            raise

    def on_finish(self, video_path: str, result: TranscriptResult) -> None:
        logger.info("转写完成")
        transcription_finished.send({
            "file_path": video_path,
        })
