import ctypes
import logging
import sys
import time
import wave

logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(levelname)s %(name)s %(message)s")

from app.transcriber.whisper import WhisperTranscriber, _venv_nvidia_bin_dirs, _NVIDIA_DLL_HANDLES

print("HANDLES after import:", len(_NVIDIA_DLL_HANDLES))

t = WhisperTranscriber(model_size="small", device="cuda")
print("HANDLES after model:", len(_NVIDIA_DLL_HANDLES), "device:", t.device)
print("BIN DIRS:", _venv_nvidia_bin_dirs())

# 模型创建后，ctranslate2 视角下裸名加载是否可见？
for name in ["cublas64_12.dll", "cudnn_ops64_9.dll", "cudnn64_9.dll"]:
    try:
        h = ctypes.WinDLL(name)
        print(f"BARE LOAD OK: {name} handle={h._handle}")
    except OSError as e:
        print(f"BARE LOAD FAIL: {name} -> {e}")

# 用 12 秒切片转写，快速验证推理路径
SRC = "D:/CyberStomach/VidInsight/doc/_gpu_test.wav"
SHORT = "D:/CyberStomach/VidInsight/doc/_gpu_short.wav"
with wave.open(SRC, "rb") as r:
    params = r.getparams()
    frames = r.readframes(int(params.framerate * 12))
with wave.open(SHORT, "wb") as w:
    w.setparams(params)
    w.writeframes(frames)

t0 = time.perf_counter()
try:
    res = t.transcript(SHORT)
    print(f"TRANSCRIBE OK in {time.perf_counter()-t0:.2f}s len={len(res.full_text)}")
    print("TEXT:", res.full_text[:120])
except Exception as e:
    print(f"TRANSCRIBE FAIL: {type(e).__name__}: {e}")
