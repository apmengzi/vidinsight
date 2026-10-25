@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0.."
echo Current working directory: %CD%

if exist "%USERPROFILE%\.cargo\bin\rustc.exe" (
  set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"
)

set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" goto missing_venv
"%PYTHON_EXE%" --version >nul 2>nul
if errorlevel 1 goto broken_venv

where rustc >nul 2>nul
if errorlevel 1 goto missing_rust

echo Cleaning old build outputs...
if exist backend\dist rmdir /s /q backend\dist
if exist backend\build rmdir /s /q backend\build
if exist vidinsight-frontend\src-tauri\bin rmdir /s /q vidinsight-frontend\src-tauri\bin

mkdir vidinsight-frontend\src-tauri\bin

rem 2026-10-20 根治「rmdir bin 连带删 ffmpeg」坑：bin 清空后自动从仓库根
rem ffmpeg_build_backup/ 恢复 ffmpeg 目录，否则 pnpm tauri build 必炸
rem （resource path `bin\ffmpeg` doesn't exist）。
if exist ffmpeg_build_backup\ffmpeg.exe (
  mkdir vidinsight-frontend\src-tauri\bin\ffmpeg
  copy /y ffmpeg_build_backup\ffmpeg.exe vidinsight-frontend\src-tauri\bin\ffmpeg\ >nul
  copy /y ffmpeg_build_backup\ffprobe.exe vidinsight-frontend\src-tauri\bin\ffmpeg\ >nul
  echo Restored ffmpeg from ffmpeg_build_backup into src-tauri\bin\ffmpeg.
) else (
  echo [WARN] ffmpeg_build_backup not found - run pnpm tauri build will fail on missing bin\ffmpeg!
)

for /f "tokens=2 delims=:" %%A in ('rustc -Vv ^| findstr "host"') do (
  set "TARGET_TRIPLE=%%A"
)
set "TARGET_TRIPLE=%TARGET_TRIPLE: =%"
if "%TARGET_TRIPLE%"=="" goto missing_rust
echo Detected target triple: %TARGET_TRIPLE%

echo Building VidInsightBackend with project .venv...
"%PYTHON_EXE%" -m PyInstaller ^
  -y ^
  --name VidInsightBackend ^
  --paths backend ^
  --distpath vidinsight-frontend\src-tauri\bin ^
  --workpath backend\build ^
  --specpath backend ^
  --hidden-import uvicorn ^
  --hidden-import fastapi ^
  --hidden-import starlette ^
  --collect-submodules chromadb ^
  --add-data "%CD%\backend\app\db\builtin_providers.json;." ^
  --add-data "%CD%\.venv\Lib\site-packages\nvidia\cublas\bin;nvidia\cublas\bin" ^
  --add-data "%CD%\.venv\Lib\site-packages\nvidia\cudnn\bin;nvidia\cudnn\bin" ^
  --add-data "%CD%\.venv\Lib\site-packages\nvidia\cuda_nvrtc\bin;nvidia\cuda_nvrtc\bin" ^
  --add-data "%CD%\.venv\Lib\site-packages\faster_whisper\assets;faster_whisper\assets" ^
  backend\main.py
if errorlevel 1 goto build_failed

move /Y vidinsight-frontend\src-tauri\bin\VidInsightBackend\VidInsightBackend.exe vidinsight-frontend\src-tauri\bin\VidInsightBackend\VidInsightBackend-%TARGET_TRIPLE%.exe
if errorlevel 1 goto build_failed

echo PyInstaller build complete:
dir vidinsight-frontend\src-tauri\bin\VidInsightBackend
endlocal
exit /b 0

:missing_venv
echo Project Python environment not found:
echo   %PYTHON_EXE%
echo Please run setup_python_env.bat from project root first.
endlocal
exit /b 1

:broken_venv
echo Project Python environment exists but cannot run:
echo   %PYTHON_EXE%
echo Please recreate it with setup_python_env.bat.
endlocal
exit /b 1

:missing_rust
echo Rust toolchain not found. Please install Rust first:
echo   winget install Rustlang.Rustup
echo Then reopen the terminal and run:
echo   rustc -V
echo   cargo -V
endlocal
exit /b 1

:build_failed
echo Backend sidecar build failed.
endlocal
exit /b 1
