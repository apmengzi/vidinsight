@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=%~dp0..\.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
  echo Project Python environment not found:
  echo   %PYTHON_EXE%
  echo.
  echo Please run from project root first:
  echo   setup_python_env.bat
  exit /b 1
)

"%PYTHON_EXE%" main.py
endlocal
