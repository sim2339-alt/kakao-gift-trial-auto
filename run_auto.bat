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

echo ============================================================
echo  카카오 체험단 자동 신청
echo ============================================================
echo   1) 노션 DB 미체크 행만 신청  ^(노션에 없는 신규 상품은 제외^)
echo   2) 최근·인기순에서 몇 개 신청       ^(기본 30개^)
echo   3) 노션 연동 설정 만들기 ^(notion_secret.json^)
echo   4) 웹 조종판 열기  ^(브라우저에서 버튼으로 조작^)
echo.
set MODE=
set /p MODE=번호를 고르세요 [1/2/3/4] (그냥 Enter = 2): 
if "%MODE%"=="" set MODE=2
if "%MODE%"=="1" goto MODE1
if "%MODE%"=="2" goto MODE2
if "%MODE%"=="3" goto SETUP
if "%MODE%"=="4" goto WEB
echo.
echo [안내] 1, 2, 3, 4 중에서 골라 주세요.
pause
exit /b 1

:MODE1
echo.
echo ※ 처음 한 번은 노션 설정이 필요합니다. 없으면 지금 물어봅니다.
set LIMIT=
set /p LIMIT=몇 개 신청할까요? (그냥 Enter 시 최대 50개): 
if "%LIMIT%"=="" set LIMIT=50
echo.
%PY% main.py --notion-pending --limit %LIMIT%
goto DONE

:MODE2
set LIMIT=
set /p LIMIT=몇 개 신청할까요? (그냥 Enter 시 기본 30개): 
if "%LIMIT%"=="" set LIMIT=30
echo.
%PY% main.py --limit %LIMIT%
goto DONE

:SETUP
echo.
%PY% main.py --setup-notion
goto DONE

:WEB
echo.
%PY% web_ui.py
goto DONE

:DONE
echo.
pause
