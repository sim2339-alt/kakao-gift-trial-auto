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
echo   1) 노션에 내 체크가 안 된 것만 신청  ^(노션 DB 확인 -> 이미 체크된 건 제외^)
echo   2) 최근·인기순에서 몇 개 신청       ^(기본 30개^)
echo   3) 노션 연동 설정 만들기 ^(notion_secret.json^)
echo.
set MODE=
set /p MODE=번호를 고르세요 [1/2/3] (그냥 Enter = 2): 
if "%MODE%"=="" set MODE=2
if "%MODE%"=="1" goto MODE1
if "%MODE%"=="2" goto MODE2
if "%MODE%"=="3" goto SETUP
echo.
echo [안내] 1, 2, 3 중에서 골라 주세요.
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

:DONE
echo.
pause
