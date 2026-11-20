@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 카카오 체험단 자동 신청

rem ============================================================================
rem  원스톱 실행기 — 이것만 더블클릭하면 됩니다.
rem   ① 파이썬 확인   ② 없는 프로그램(playwright·크로미움) 자동 설치
rem   ③ 웹 조종판을 열어 로그인 → 담당자 열 선택 → 미리보기 → 자동 신청까지 한 화면에서
rem ============================================================================

set PY=
py -3 --version >nul 2>&1 && set PY=py -3
if "%PY%"=="" (python --version >nul 2>&1 && set PY=python)
if "%PY%"=="" goto NOPY

echo ============================================================
echo  카카오 체험단 자동 신청
echo ============================================================
echo.

rem ── ① 필요한 프로그램(playwright) 확인 → 없으면 자동 설치
%PY% -c "import playwright" >nul 2>&1
if errorlevel 1 goto INSTALL

rem ── ② 브라우저(크로미움) 확인 → 없으면 내려받기
%PY% -c "import os,sys;from playwright.sync_api import sync_playwright;p=sync_playwright().start();ok=os.path.exists(p.chromium.executable_path);p.stop();sys.exit(0 if ok else 1)" >nul 2>&1
if errorlevel 1 goto BROWSER

goto WEB

:INSTALL
echo [설치] 처음 한 번만 필요한 프로그램을 내려받습니다 (인터넷 필요, 몇 분).
echo.
%PY% -m pip install --upgrade pip
%PY% -m pip install -r requirements.txt
if errorlevel 1 goto FAIL
echo.
echo [설치] 브라우저(크로미움)를 내려받습니다.
%PY% -m playwright install chromium
if errorlevel 1 goto FAIL
echo.
echo [설치] 끝났습니다. 이제 웹 조종판을 엽니다.
echo.
goto WEB

:BROWSER
echo [설치] 브라우저(크로미움)를 내려받습니다 (몇 분 걸립니다).
%PY% -m playwright install chromium
if errorlevel 1 goto FAIL
echo.
goto WEB

:WEB
echo  잠시 뒤 브라우저에 조종판이 열립니다.  주소: http://127.0.0.1:8765
echo   · 처음이면 [카카오 로그인 창 열기] 로 로그인만 해 두면 됩니다(한 번만).
echo   · 노션은 처음 한 번만 설정하면 다시 묻지 않습니다. 담당자 열만 고르면 됩니다.
echo   · 이 검은 창을 닫거나 Ctrl+C 를 누르면 조종판이 꺼집니다.
echo ============================================================
echo.
%PY% web_ui.py
if errorlevel 1 goto FAIL
goto DONE

:NOPY
echo ============================================================
echo  [오류] 파이썬(Python)을 찾지 못했습니다.
echo ============================================================
echo  1) https://www.python.org/downloads/ 에서 Python 3.x 를 내려받아 실행
echo  2) 설치 첫 화면의 "Add python.exe to PATH" 를 반드시 체크
echo  3) 그다음 이 run.bat 을 다시 더블클릭
echo.
pause
exit /b 1

:FAIL
echo.
echo [오류] 설치 또는 실행에 실패했습니다. 위에 나온 빨간 글씨를 알려 주세요.
echo        (수동 설치: scripts\install.bat / 웹 조종판만 다시 열기: scripts\run_web.bat)
echo.
pause
exit /b 1

:DONE
echo.
pause
