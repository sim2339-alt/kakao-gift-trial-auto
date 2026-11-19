@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo  카카오 선물하기 체험단 자동 신청 - 최초 설치
echo ============================================================
echo.

set PY=
py -3 --version >nul 2>&1 && set PY=py -3
if "%PY%"=="" (python --version >nul 2>&1 && set PY=python)
if "%PY%"=="" (
  echo [오류] 파이썬을 찾지 못했습니다.
  echo        https://www.python.org/downloads/ 에서 설치할 때
  echo        "Add python.exe to PATH" 를 반드시 체크해 주세요.
  echo.
  pause
  exit /b 1
)

echo [1/3] 파이썬 확인: %PY%
%PY% --version
echo.
echo [2/3] 필요한 패키지 설치 (playwright)
%PY% -m pip install --upgrade pip
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo [오류] 패키지 설치에 실패했습니다. 위 빨간 글씨를 알려 주세요.
  pause
  exit /b 1
)
echo.
echo [3/3] 브라우저(크로미움) 내려받기 - 수 분 걸립니다
%PY% -m playwright install chromium
if errorlevel 1 (
  echo.
  echo [오류] 브라우저 설치에 실패했습니다. 인터넷 연결을 확인해 주세요.
  pause
  exit /b 1
)

echo.
echo ============================================================
echo  설치가 끝났습니다. 이제 run_login.bat 을 더블클릭하세요.
echo ============================================================
pause
