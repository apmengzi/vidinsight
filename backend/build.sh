#!/usr/bin/env bash
set -e
# uncomment this for debugging
# set -x

# 切到项目根（假设脚本放在 script/ 目录）
cd "$(dirname "$0")/.."

echo "当前工作目录：$(pwd)"

PYTHON_EXE="$(pwd)/.venv/bin/python"
if [ ! -x "$PYTHON_EXE" ]; then
  echo "Project Python environment not found: $PYTHON_EXE"
  echo "Please run setup_python_env.bat on Windows or create .venv manually first."
  exit 1
fi

# 清理旧的构建
echo "清理旧的构建..."
rm -rf backend/dist backend/build ./vidinsight-frontend/src-tauri/bin/*
echo "清理完成。"

TARGET_TRIPLE=$(rustc -Vv | grep host | cut -f2 -d' ')
echo "Detected target triple: $TARGET_TRIPLE"

echo "开始 PyInstaller 打包..."
"$PYTHON_EXE" -m PyInstaller \
  -y \
  --name VidInsightBackend \
  --paths backend \
  --distpath ./vidinsight-frontend/src-tauri/bin \
  --workpath backend/build \
  --specpath backend \
  --hidden-import uvicorn \
  --hidden-import fastapi \
  --hidden-import starlette \
  --collect-submodules chromadb \
  --add-data "$(pwd)/backend/app/db/builtin_providers.json:." \
  "$(pwd)/backend/main.py"


# 重命名主执行文件以包含目标平台信息
mv \
 ./vidinsight-frontend/src-tauri/bin/VidInsightBackend/VidInsightBackend\
 ./vidinsight-frontend/src-tauri/bin/VidInsightBackend/VidInsightBackend-$TARGET_TRIPLE

echo "PyInstaller 打包完成。"
echo "打包后的目录内容："
ls -l ./vidinsight-frontend/src-tauri/bin/VidInsightBackend

