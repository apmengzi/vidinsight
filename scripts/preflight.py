"""发布前自检（release gate）：一条命令跑完所有门禁，任何一步失败即退出码 1。

用法（仓库根目录）：
    python scripts/preflight.py                 # 全量：pytest + tsc + cargo check + 环境检查
    python scripts/preflight.py --skip-cargo    # 跳过 Rust 检查（约省 30-60s）
    python scripts/preflight.py --skip-frontend # 跳过 tsc
    python scripts/preflight.py --skip-env      # 跳过环境检查

约定（doc/ENGINEERING_STANDARD.md）：改动合入 main 前必须全绿；出包部署前必须全量跑。
"""
import argparse
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
FRONTEND = REPO / "vidinsight-frontend"
TAURI = FRONTEND / "src-tauri"

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    return ok


def run_step(name: str, cmd: list[str], cwd: Path, timeout: int = 600) -> bool:
    print(f"\n▶ {name}: {' '.join(cmd)}  (cwd={cwd.name})")
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError as e:
        return record(name, False, f"命令不存在: {e}")
    except subprocess.TimeoutExpired:
        return record(name, False, f"超时（>{timeout}s）")
    dur = time.perf_counter() - start
    ok = proc.returncode == 0
    tail = "\n".join((proc.stdout or "").strip().splitlines()[-6:])
    err_tail = "\n".join((proc.stderr or "").strip().splitlines()[-6:])
    detail = tail if ok else (tail + ("\n[stderr] " + err_tail if err_tail else ""))
    return record(name, ok, f"({dur:.1f}s) {detail}".strip())


def check_env() -> None:
    ffmpeg = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    record("env.ffmpeg", ffmpeg is not None,
           f"PATH 中找到: {ffmpeg}" if ffmpeg else
           "PATH 无 ffmpeg——打包环境需 FFMPEG_BIN_PATH 指向可用二进制")
    try:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 8483))
        record("env.port_8483_free", True, "8483 空闲，可正常启动")
    except OSError:
        record("env.port_8483_free", False,
               "8483 被占用——存在孤儿后端进程，启动会报『后端进程已退出』")
    has_dotenv = (REPO / ".env").exists()
    # .env 仅 Docker/Web 部署需要；桌面版（Tauri+sidecar）配置走 UI + AppData，缺失不算失败
    record("env.dotenv", True,
           ".env 存在（Docker/Web 可用）" if has_dotenv
           else ".env 不存在——桌面部署可接受；Docker/Web 部署前需从 .env.example 复制")


def main() -> int:
    parser = argparse.ArgumentParser(description="VidInsight 发布前自检")
    parser.add_argument("--skip-cargo", action="store_true", help="跳过 cargo check")
    parser.add_argument("--skip-frontend", action="store_true", help="跳过 tsc")
    parser.add_argument("--skip-env", action="store_true", help="跳过环境检查")
    args = parser.parse_args()

    print("=" * 64)
    print("VidInsight 发布前自检 preflight")
    print("=" * 64)

    run_step("backend.pytest", [sys.executable, "-m", "pytest", "-q"], BACKEND, timeout=600)
    if not args.skip_frontend:
        pnpm = shutil.which("pnpm") or shutil.which("pnpm.cmd")
        if pnpm:
            run_step("frontend.tsc", [pnpm, "exec", "tsc", "--noEmit"], FRONTEND, timeout=300)
        else:
            record("frontend.tsc", False, "未找到 pnpm，无法运行 TypeScript 检查")
    if not args.skip_cargo:
        if (TAURI / "Cargo.toml").exists():
            run_step("tauri.cargo_check", ["cargo", "check", "-q"], TAURI, timeout=600)
        else:
            record("tauri.cargo_check", False, f"缺少 {TAURI}")
    if not args.skip_env:
        check_env()

    print("\n" + "=" * 64)
    print(f"{'✅ 通过' if all(ok for _, ok, _ in results) else '❌ 有失败项'}  自检结果")
    print("=" * 64)
    for name, ok, detail in results:
        print(f"  {'✓' if ok else '✗'} {name:<22} {detail.splitlines()[0] if detail else ''}")
    failed = [n for n, ok, _ in results if not ok]
    if failed:
        print(f"\n失败项: {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
