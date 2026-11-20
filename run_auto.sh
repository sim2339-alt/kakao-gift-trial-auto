#!/usr/bin/env bash
# 실제 신청: 실행 모드(1=노션 미체크만 / 2=최근·인기순 / 3=노션 설정)와 개수를 입력받아 실행한다.
cd "$(dirname "$0")"

echo "============================================================"
echo " 카카오 체험단 자동 신청"
echo "============================================================"
echo "  1) 노션에 내 체크가 안 된 것만 신청 (노션 DB 확인 → 이미 체크된 건 제외)"
echo "  2) 최근·인기순에서 몇 개 신청 (기본 30개)"
echo "  3) 노션 연동 설정 만들기 (notion_secret.json)"
echo
read -r -p "번호를 고르세요 [1/2/3] (그냥 Enter = 2): " MODE
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
  *)
    read -r -p "몇 개 신청할까요? (그냥 Enter 시 기본 30개): " LIMIT
    [ -z "$LIMIT" ] && LIMIT=30
    python3 main.py --limit "$LIMIT"
    ;;
esac
