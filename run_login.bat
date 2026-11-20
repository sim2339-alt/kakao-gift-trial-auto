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

echo 카카오 로그인을 시작합니다. (로그인 전용 '데스크톱 크기' 창이 열립니다)
echo 1) 창에서 카카오 로그인을 끝까지 진행하세요. (QR·아이디 모두 가능)
echo 2) 로그인은 자동으로 감지됩니다. (최대 3분, Enter 로 즉시 확인)
echo 3) [완료] 로그인 세션이 감지·저장되었습니다! 가 나오면 끝입니다.
echo.
%PY% main.py --login
echo.
pause
