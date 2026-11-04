"""FunASR 转写器（阿里达摩院 Paraformer）。

## 实测对比（2026-11-04，本机 RTX 5060，同一段 217 秒中文访谈）

| 指标 | fast-whisper small | FunASR paraformer-zh |
|---|---|---|
| GPU 推理 | **9.6s**（RTF 0.044） | 13.9s（CPU，RTF 0.073） |
| 时间戳 | **逐句 start/end** | 无（整段连排） |
| 标点 | 无 | **自带 ct-punc** |
| 中文术语 | 一般（"嵌入式"→"签入式"） | 略好 |
| 英文术语 | 较好（"AI infra"） | 差（"AI刷"） |

## 结论：不替换 whisper，做成可选 + 可融合

- **单独用**：适合纯文字场景（不需要跳转、不需要章节对齐），快且带标点
- **融合用**（推荐）：whisper 出时间轴骨架，FunASR 出校对文本，
  LLM 只在有分歧的段上做字词级选择 —— 见 merge.py

## 依赖

`funasr` + `kaldi-native-fbank`（fbank 后端，约 3MB，比装 torchaudio 轻）。
模型首次运行从 ModelScope 下载约 900MB（paraformer + vad + punc），
缓存目录由 MODELSCOPE_CACHE 控制。
"""
from __future__ import annotations

import os
import time
from typing import Optional

from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.transcriber.base import Transcriber
from app.utils.logger import get_logger

logger = get_logger(__name__)

# 默认模型组合：Paraformer 中文 + FSMN-VAD 切句 + CT-Punc 标点
DEFAULT_MODEL = os.getenv("FUNASR_MODEL", "paraformer-zh")
DEFAULT_VAD = os.getenv("FUNASR_VAD", "fsmn-vad")
DEFAULT_PUNC = os.getenv("FUNASR_PUNC", "ct-punc")


class FunASRTranscriber(Transcriber):
    """FunASR 转写器。懒加载模型，首次调用约 25 秒。"""

    def __init__(self, device: Optional[str] = None, **_ignored):
        self._model = None
        self._device = device or os.getenv("FUNASR_DEVICE", "cpu")

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        try:
            from funasr import AutoModel
        except ImportError as e:
            raise RuntimeError(
                "FunASR 未安装。请执行：pip install funasr kaldi-native-fbank"
            ) from e

        t0 = time.time()
        kwargs = {
            "model": DEFAULT_MODEL,
            "vad_model": DEFAULT_VAD,
            "punc_model": DEFAULT_PUNC,
            "disable_update": True,
        }
        # GPU 需要 torch 带 CUDA；本机 torch 是 CPU 版，自动落到 cpu
        if self._device == "cuda":
            try:
                import torch
                if not torch.cuda.is_available():
                    logger.warning("[funasr] 请求 cuda 但 torch 无 CUDA，改用 cpu")
                    self._device = "cpu"
            except Exception:
                self._device = "cpu"
        kwargs["device"] = self._device

        self._model = AutoModel(**kwargs)
        logger.info(f"[funasr] 模型加载完成 {round(time.time() - t0, 1)}s "
                    f"(device={self._device})")
        return self._model

    def transcript(self, file_path: str, on_progress=None) -> TranscriptResult:
        model = self._ensure_model()

        if on_progress:
            try:
                on_progress(0.1)
            except Exception:
                pass

        t0 = time.time()
        # return_raw_text=True 才会返回 timestamp（逐 token 毫秒区间）。
        # 实测（2026-11-04）：不带这个参数时只有整段 text，无法对齐。
        result = model.generate(
            input=file_path, batch_size_s=60, return_raw_text=True)
        elapsed = round(time.time() - t0, 1)

        text = ""
        raw_text = ""
        timestamps = []
        if result and isinstance(result, list):
            item = result[0]
            text = (item.get("text") or "").strip()
            raw_text = item.get("raw_text") or ""
            timestamps = item.get("timestamp") or []

        if on_progress:
            try:
                on_progress(1.0)
            except Exception:
                pass

        logger.info(f"[funasr] 转写完成 {elapsed}s, {len(text)} 字, "
                    f"{len(timestamps)} 个 token 时间戳")

        # FunASR 不返回「句级」时间戳（只有 token 级），所以 segments 里
        # 只放一个覆盖全片的伪段承载文本。需要句级时间轴时走融合路径
        # （以 whisper 为骨架，见 merge.py）。
        segs = []
        if text:
            dur = (timestamps[-1][1] / 1000.0) if timestamps else 0.0
            segs.append(TranscriptSegment(start=0.0, end=dur, text=text))

        return TranscriptResult(
            language="zh",
            full_text=text,
            segments=segs,
            # 保留完整原始返回，merge 阶段要用 raw_text + timestamp
            raw=result[0] if (result and isinstance(result, list)) else result,
        )


def is_available() -> bool:
    """FunASR 是否可用（装了 funasr 且能 import fbank 后端）。"""
    try:
        import funasr  # noqa: F401
    except ImportError:
        return False
    try:
        import kaldi_native_fbank  # noqa: F401
        return True
    except ImportError:
        pass
    try:
        import torchaudio  # noqa: F401
        return True
    except ImportError:
        return False
