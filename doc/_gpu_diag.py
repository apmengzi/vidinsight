import ctypes
import os
import sys
from pathlib import Path

nvidia = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
bins = [str(p) for p in sorted(nvidia.glob("*/bin")) if p.is_dir()]
print("BIN DIRS:", bins)

def try_load(tag):
    try:
        h = ctypes.WinDLL("cudnn_ops64_9.dll")
        print(f"[{tag}] cudnn_ops64_9.dll OK handle={h._handle}")
        return True
    except OSError as e:
        print(f"[{tag}] FAIL: {e}")
        return False

try_load("baseline")

handles = [os.add_dll_directory(b) for b in bins]
try_load("after add_dll_directory")

for b in bins:
    os.environ["PATH"] = b + os.pathsep + os.environ.get("PATH", "")
try_load("after PATH prepend")

print("PATH head:", os.environ["PATH"][:300])
