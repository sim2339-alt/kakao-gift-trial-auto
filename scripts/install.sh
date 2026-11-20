#!/usr/bin/env bash
# 맥·리눅스용: 최초 1회 설치 (보통은 run.sh 가 자동으로 처리합니다)
set -e
cd "$(dirname "$0")/.."
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
echo
echo "설치가 끝났습니다. 다음: ./run_login.sh"
