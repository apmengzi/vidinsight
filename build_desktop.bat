@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

if exist "%USERPROFILE%\.cargo\bin\rustc.exe" (
  set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"
)

call check_desktop_env.bat
if errorlevel 1 (
  echo.
  echo Fix the checks above, then run build_desktop.bat again.
  endlocal
  exit /b 1
)

echo.
echo Building backend sidecar...
call backend\build.bat
if errorlevel 1 (
  echo Backend sidecar build failed.
  endlocal
  exit /b 1
)

echo.
echo Building Tauri desktop app...
set "VSWHERE=C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
set "VSDEVCMD="
if exist "%VSWHERE%" (
  for /f "usebackq delims=" %%I in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VS_INSTALL=%%I"
  if defined VS_INSTALL set "VSDEVCMD=!VS_INSTALL!\Common7\Tools\VsDevCmd.bat"
)

cd vidinsight-frontend
if defined VSDEVCMD if exist "%VSDEVCMD%" (
  call "!VSDEVCMD!" -arch=x64
)
call npm run desktop:build
if errorlevel 1 (
  echo Tauri desktop build failed.
  endlocal
  exit /b 1
)

echo.
echo Desktop build complete.
echo Installer bundles are under:
echo   vidinsight-frontend\src-tauri\target\release\bundle
endlocal
