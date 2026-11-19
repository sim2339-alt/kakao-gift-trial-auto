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

echo 체험단 TOP 30 자동 신청을 시작합니다. 브라우저 창을 닫지 마세요.
echo (개수를 바꾸려면 이 파일을 메모장으로 열어 --limit 30 의 숫자를 수정)
echo.
%PY% main.py --limit 30
echo.
pause
