# -*- coding: utf-8 -*-
"""GPU 模式启动包装：把打包 App 自带的 CUDA 运行库目录挂进 DLL 搜索路径后启动 main。

CPU 模式下 fast-whisper 转写慢 ~44 倍（实测同一条视频 CPU ~10 分钟 vs GPU ~15 秒）。
PyInstaller 打包目录 _internal/nvidia/ 里有完整 cudnn9 + cublas12 + nvrtc，
源码直起的后端进程默认搜不到它们（whisper.py 的 PATH 检测在 Git Bash 下传递失效，
os.add_dll_directory 是 Windows 标准做法且实测有效）。
"""
import os
import sys

_NV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "..", "vidinsight-frontend", "src-tauri", "bin",
                   "VidInsightBackend", "_internal", "nvidia")
_NV = os.path.normpath(_NV)
for sub in ("cudnn/bin", "cublas/bin", "cuda_nvrtc/bin"):
    d = os.path.join(_NV, sub)
    if os.path.isdir(d):
        os.add_dll_directory(d)
        os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]

import main  # noqa: E402,F401  —— App/lifespan 注册都在 main 模块级

# main.py 的服务器启动在 __main__ 保护里，import 不会拉起 uvicorn，这里补上
import os  # noqa: E402
import uvicorn  # noqa: E402

if __name__ == "__main__":
    port = int(os.getenv("BACKEND_PORT", 8483))
    host = os.getenv("BACKEND_HOST", "0.0.0.0")
    # 源码直起 = 本机单人使用，关掉 owner 隔离（2026-11-03）。
    # 开着会：脚本/curl 提交的任务归 legacy → 桌面端历史看不见；
    # 老笔记 owner=legacy 而浏览器是随机 UUID → 播放原片被判越权 404。
    # 公开部署走 Docker，不经过这个入口，隔离仍默认开启。
    os.environ.setdefault("OWNER_ISOLATION", "0")
    uvicorn.run(main.app, host=host, port=port, reload=False)
