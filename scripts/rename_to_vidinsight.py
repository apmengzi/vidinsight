#!python
"""
VidInsight 统一改名脚本（阶段 1：内部标识符 + 产物名对齐）

设计要点:
1. 长串优先有序替换, 避免子串互相污染
   (CyberStomachBackend -> VidInsightBackend 必须排在 CyberStomach -> VidInsight 之前)
2. 跳过二进制 / 依赖目录, 只处理文本文件
3. 支持 dry-run, 先看规模再落盘
4. 改名后不触碰"可能合法的第三方引用"(如 upstream 说明文档)时可用 --exclude 指定

用法:
  python scripts/rename_to_vidinsight.py --dry-run          # 预览
  python scripts/rename_to_vidinsight.py --apply            # 落盘
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ── 有序替换表: (旧串, 新串), 长串必须靠前 ───────────────────────────
ORDERED_REPLACEMENTS: list[tuple[str, str]] = [
    # 1) 复合产物名优先
    ("CyberStomachBackend", "VidInsightBackend"),
    ("BiliNoteBackend", "VidInsightBackend"),
    ("BillNote_Backend", "VidInsightBackend"),
    # 2) 目录/仓名相关复合词
    ("BillNote_frontend", "vidinsight-frontend"),
    ("BillNote_extension", "vidinsight-extension"),
    ("bili_note.db", "vidinsight.db"),
    ("bill_note.db", "vidinsight.db"),
    # 3) 单品牌词
    ("CyberStomach", "VidInsight"),
    ("Cyber Stomach", "VidInsight"),
    ("cyberstomach", "vidinsight"),
    ("BiliNote", "VidInsight"),
    ("bilinote", "vidinsight"),
    ("BillNote", "VidInsight"),
    ("billnote", "vidinsight"),
]

# ── 需要额外修正的特殊目标(不走通用替换) ──────────────────────────
SPECIAL_TARGETS: list[tuple[str, str, str]] = [
    # (文件路径后缀, 旧串, 新串)
    ("src-tauri/tauri.conf.json", "com.jefferyhuang.bilinote", "com.vidinsight.app"),
    ("src-tauri/Cargo.toml", "com.jefferyhuang.bilinote", "com.vidinsight.app"),
]

SKIP_DIRS = {
    "node_modules", ".git", "venv", ".venv", "__pycache__", "target",
    "dist", "build", ".next", ".idea", ".vscode", "release",
    ".playwright-mcp", ".v2c", ".pytest_cache", "egg-info",
}

TEXT_EXT = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".vue", ".json", ".rs", ".toml",
    ".md", ".txt", ".yml", ".yaml", ".bat", ".sh", ".html", ".css", ".scss",
    ".spec", ".gitignore", ".env", ".cfg", ".ini", ".sql", ".svg", ".xml",
}

# 明确跳过: 历史 / 上游溯源文档不应被改写 (保留证据链)
PROTECTED_PATH_MARKERS = [
    "02_完整源代码",
    "综合设计3_最终交付包",
    "doc/UPSTREAM_VS_OURS.md",
    # 本脚本自身: 替换表里含旧串, 改了会把自己改坏
    "scripts/rename_to_vidinsight.py",
    # 运行时产出 / 测试数据: 里面是笔记内容, 不是代码标识符, 不能动
    "note_results",
    "eval_data",
    "backend/static",
    "static/screenshots",
    ".db",
]


def is_protected(path: Path) -> bool:
    s = str(path).replace("\\", "/")
    return any(marker in s for marker in PROTECTED_PATH_MARKERS)


def should_process(path: Path) -> bool:
    if is_protected(path):
        return False
    parts = set(path.parts)
    if parts & SKIP_DIRS:
        return False
    # 有扩展名的按白名单; 无扩展名的(如 Dockerfile/.gitignore)按大小粗判
    if path.suffix and path.suffix not in TEXT_EXT:
        return False
    try:
        if path.stat().st_size > 3 * 1024 * 1024:  # >3MB 跳过
            return False
    except OSError:
        return False
    return True


def transform(text: str, rel_posix: str) -> tuple[str, int]:
    """返回 (新文本, 替换次数)"""
    hits = 0
    out = text
    for old, new in ORDERED_REPLACEMENTS:
        if old in out:
            hits += out.count(old)
            out = out.replace(old, new)

    for suffix, old, new in SPECIAL_TARGETS:
        if rel_posix.endswith(suffix) and old in out:
            hits += out.count(old)
            out = out.replace(old, new)
    return out, hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写盘; 不加则为 dry-run")
    args = ap.parse_args()

    total_files = 0
    total_hits = 0
    touched: list[tuple[str, int]] = []

    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if not should_process(path):
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        rel = path.relative_to(ROOT).as_posix()
        new_text, hits = transform(raw, rel)
        if hits and new_text != raw:
            total_files += 1
            total_hits += hits
            touched.append((rel, hits))
            if args.apply:
                path.write_text(new_text, encoding="utf-8")

    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"===== {mode} =====")
    print(f"files touched : {total_files}")
    print(f"total replaces: {total_hits}")
    print("---- per-file (top 40) ----")
    for rel, hits in sorted(touched, key=lambda x: -x[1])[:40]:
        print(f"  {hits:5d}  {rel}")
    if not args.apply:
        print("\n(dry-run: no file changed. use --apply to write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
