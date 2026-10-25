import ctypes
import os
import sys
from pathlib import Path

nvidia = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
bins = [str(p) for p in sorted(nvidia.glob("*/bin")) if p.is_dir()]
print("REGISTERED DIRS:", bins)
handles = [os.add_dll_directory(b) for b in bins]

cudnn_bin = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia" / "cudnn" / "bin"
print("\nFILES IN cudnn/bin:")
for f in sorted(cudnn_bin.iterdir()):
    print(f"  {f.name}  {f.stat().st_size}")

print("\nLOAD EACH (cudnn first, then cublas):")
order = sorted(cudnn_bin.glob("*.dll")) + sorted((Path(sys.prefix)/"Lib"/"site-packages"/"nvidia"/"cublas"/"bin").glob("*.dll"))
for dll in order:
    try:
        h = ctypes.WinDLL(str(dll))
        print(f"  OK   {dll.name}")
    except OSError as e:
        print(f"  FAIL {dll.name} -> {e}")
