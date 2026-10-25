"""Workflow 门控用 pytest 包装：world.run 固定 cwd 无 shell，
用本脚本切到 backend 目录再跑（与本地 `python -m pytest tests/ -q` 等价）。"""
import os
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
import pytest

sys.exit(pytest.main(["tests", "-q", "-p", "no:cacheprovider"]))
