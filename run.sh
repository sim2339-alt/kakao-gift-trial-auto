#!/usr/bin/env bash
# 원스톱 실행기 (맥·리눅스) — 이것만 실행하면 됩니다.
#   ① 파이썬 확인  ② 없는 프로그램(playwright·크로미움) 자동 설치  ③ 웹 조종판 열기
set -u
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[오류] 파이썬3를 찾지 못했습니다. https://www.python.org/downloads/ 에서 설치해 주세요."
  exit 1
fi

echo "============================================================"
echo " 카카오 체험단 자동 신청"
echo "============================================================"

if ! python3 -c "import playwright" >/dev/null 2>&1; then
  echo "[설치] 처음 한 번만 필요한 프로그램을 내려받습니다 (인터넷 필요, 몇 분)."
  python3 -m pip install --upgrade pip
  python3 -m pip install -r requirements.txt || { echo "[오류] 패키지 설치에 실패했습니다."; exit 1; }
fi

if ! python3 -c "import os,sys;from playwright.sync_api import sync_playwright;p=sync_playwright().start();ok=os.path.exists(p.chromium.executable_path);p.stop();sys.exit(0 if ok else 1)" >/dev/null 2>&1; then
  echo "[설치] 브라우저(크로미움)를 내려받습니다 (몇 분 걸립니다)."
  python3 -m playwright install chromium || { echo "[오류] 브라우저 설치에 실패했습니다."; exit 1; }
fi

echo " 잠시 뒤 브라우저에 조종판이 열립니다.  주소: http://127.0.0.1:8765"
echo "  · 처음이면 [카카오 로그인 창 열기] 로 로그인만 해 두면 됩니다(한 번만)."
echo "  · 노션은 처음 한 번만 설정하면 다시 묻지 않습니다. 담당자 열만 고르면 됩니다."
echo "  · 끄려면 Ctrl+C"
echo "============================================================"
exec python3 web_ui.py "$@"
