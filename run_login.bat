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

echo 카카오 로그인을 시작합니다.
echo 열린 브라우저 창에서 직접 로그인한 뒤, 이 검은 창에서 Enter 를 누르세요.
echo.
%PY% main.py --login
echo.
pause
