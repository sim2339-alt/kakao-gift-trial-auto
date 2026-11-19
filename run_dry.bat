@echo off
chcp 65001 >nul
cd /d "%~dp0"

set PY=
py -3 --version >nul 2>&1 && set PY=py -3
if "%PY%"=="" (python --version >nul 2>&1 && set PY=python)
if "%PY%"=="" (
  echo [오류] 파이썬을 찾지 못했습니다. install.bat 을 먼저 실행해 주세요.
  pause
  exit /b 1
)

echo 오늘의 후보 목록을 조회합니다. (실제 신청은 하지 않습니다)
echo.
%PY% main.py --dry-run
echo.
pause
