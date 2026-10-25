import os
import sys
from pathlib import Path

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
APP_NAME = "VidInsight"


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def get_runtime_root() -> str:
    """Return the writable runtime directory for generated desktop data."""
    if is_frozen():
        base_dir = os.getenv("APPDATA") or os.getenv("LOCALAPPDATA") or str(Path.home())
        root = os.path.join(base_dir, APP_NAME)
    else:
        root = PROJECT_ROOT
    os.makedirs(root, exist_ok=True)
    return root


def get_runtime_path(*parts: str) -> str:
    path = os.path.join(get_runtime_root(), *parts)
    parent = path if not parts else os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return path


def get_data_dir():
    data_path = get_runtime_path("data")
    os.makedirs(data_path, exist_ok=True)
    return data_path


def get_model_dir(subdir: str = "whisper") -> str:
    path = get_runtime_path("models", subdir)
    os.makedirs(path, exist_ok=True)
    return path


def get_app_dir(subdir: str = "") -> str:
    """
    返回一个稳定的可写目录：
    - 开发时：使用 backend 目录
    - 打包后：使用用户可写的 APPDATA/VidInsight 目录
    """
    full_path = get_runtime_path(subdir) if subdir else get_runtime_root()
    os.makedirs(full_path, exist_ok=True)
    return full_path
