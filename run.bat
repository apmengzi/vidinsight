@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Project Python environment not found.
  echo Run setup_python_env.bat first.
  pause
  exit /b 1
)

start "Backend" powershell -NoExit -Command "cd backend; ..\.venv\Scripts\python.exe main.py"
start "Frontend" powershell -NoExit -Command "cd vidinsight-frontend; pnpm.cmd dev"

start http://localhost:3015/
