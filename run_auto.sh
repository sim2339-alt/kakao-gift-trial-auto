#!/usr/bin/env bash
# 실제 신청: 실행 모드(1=노션 미체크만 / 2=최근·인기순 / 3=노션 설정)와 개수를 입력받아 실행한다.
cd "$(dirname "$0")"

echo "============================================================"
echo " 카카오 체험단 자동 신청"
echo "============================================================"
echo "  1) 노션 DB 미체크 행만 신청 (노션에 없는 신규 상품은 제외)"
echo "  2) 최근·인기순에서 몇 개 신청 (기본 30개)"
echo "  3) 노션 연동 설정 만들기 (notion_secret.json)"
echo "  4) 웹 조종판 열기 (브라우저에서 버튼으로 조작)"
echo
read -r -p "번호를 고르세요 [1/2/3/4] (그냥 Enter = 2): " MODE
[ -z "$MODE" ] && MODE=2

case "$MODE" in
  1)
    read -r -p "몇 개 신청할까요? (그냥 Enter 시 최대 50개): " LIMIT
    [ -z "$LIMIT" ] && LIMIT=50
    python3 main.py --notion-pending --limit "$LIMIT"
    ;;
  3)
    python3 main.py --setup-notion
    ;;
  4)
    python3 web_ui.py
    ;;
  *)
    read -r -p "몇 개 신청할까요? (그냥 Enter 시 기본 30개): " LIMIT
    [ -z "$LIMIT" ] && LIMIT=30
    python3 main.py --limit "$LIMIT"
    ;;
esac
