"""对照实验：A=仅 PATH prepend；B=add_dll_directory+预加载全部 DLL"""
import ctypes
import logging
import os
import sys
import time
from pathlib import Path

mode = sys.argv[1]
logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(levelname)s %(name)s %(message)s")

nvidia = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
bins = [str(p) for p in sorted(nvidia.glob("*/bin")) if p.is_dir()]

if mode == "A":
    for b in bins:
        os.environ["PATH"] = b + os.pathsep + os.environ["PATH"]
    print("MODE A: PATH prepend only")
elif mode == "B":
    handles = [os.add_dll_directory(b) for b in bins]
    loaded = 0
    for b in bins:
        for dll in sorted(Path(b).glob("*.dll")):
            try:
                ctypes.WinDLL(str(dll))
                loaded += 1
            except OSError as e:
                print(f"PRELOAD FAIL {dll.name}: {e}")
    print(f"MODE B: add_dll_directory + preloaded {loaded} DLLs, handles={len(handles)}")

from app.transcriber.whisper import WhisperTranscriber

t = WhisperTranscriber(model_size="small", device="cuda")
print(f"device={t.device}")
t0 = time.perf_counter()
try:
    res = t.transcript("D:/CyberStomach/VidInsight/doc/_gpu_short.wav")
    print(f"RESULT[{mode}]: OK {time.perf_counter()-t0:.2f}s len={len(res.full_text)}")
except Exception as e:
    print(f"RESULT[{mode}]: FAIL {type(e).__name__}: {e}")
