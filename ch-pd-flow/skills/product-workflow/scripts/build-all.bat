@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "POWERSHELL_EXE=powershell.exe"
set "BUILD_SCRIPT=%SCRIPT_DIR%build-all.ps1"

if not exist "%BUILD_SCRIPT%" (
  echo [ERROR] Missing build script:
  echo %BUILD_SCRIPT%
  echo.
  pause
  exit /b 1
)

echo Rebuilding product-workflow SKILL.md files...
echo.
"%POWERSHELL_EXE%" -ExecutionPolicy Bypass -File "%BUILD_SCRIPT%"
set "EXIT_CODE=%ERRORLEVEL%"
echo.

if not "%EXIT_CODE%"=="0" (
  echo [ERROR] Build failed with exit code %EXIT_CODE%.
  echo.
  pause
  exit /b %EXIT_CODE%
)

echo [OK] Build completed successfully.
echo.
pause
exit /b 0
