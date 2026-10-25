@echo off
setlocal
cd /d "%~dp0"

set "VENV_DIR=%CD%\.venv"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"

if exist "%PYTHON_EXE%" (
  echo Project Python environment already exists:
  echo   %PYTHON_EXE%
) else (
  echo Creating project Python environment in:
  echo   %VENV_DIR%
  py -3.11 -m venv "%VENV_DIR%" 2>nul
  if errorlevel 1 (
    echo py -3.11 failed, falling back to python -m venv...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
      echo Failed to create .venv. Please install Python 3.10 or 3.11 first.
      exit /b 1
    )
  )
)

echo Upgrading pip...
"%PYTHON_EXE%" -m pip install --upgrade pip
if errorlevel 1 exit /b 1

echo Installing backend dependencies into project .venv...
"%PYTHON_EXE%" -m pip install -r backend\requirements.txt
if errorlevel 1 exit /b 1

echo.
echo Project Python environment is ready.
echo Python:
"%PYTHON_EXE%" --version
echo.
echo Use this interpreter:
echo   %PYTHON_EXE%

endlocal
