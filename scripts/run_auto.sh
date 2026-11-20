#!/usr/bin/env bash
# 실제 신청: 실행 모드(1=노션 미체크만 / 2=카카오 전체 중 신청 안 한 것 / 3=노션 설정 변경 / 4=웹)와 개수를 입력받아 실행한다.
# 노션 토큰·DB ID 는 한 번 저장하면 다시 묻지 않고, 실행할 때는 담당자(열)만 고른다.
cd "$(dirname "$0")/.."

echo "============================================================"
echo " 카카오 체험단 자동 신청"
echo "============================================================"
echo "  1) 노션 미체크 목록만 불러오기 (신청 안 한 것만)"
echo "  2) 카카오 전체 중 신청 안 한 것 (인기순/마감임박순, 기본 100개)"
echo "  3) 노션 설정 변경 (토큰·DB ID · 처음 한 번만 저장하면 됨)"
echo "  4) 웹 조종판 열기 (브라우저에서 버튼으로 조작)"
echo
read -r -p "번호를 고르세요 [1/2/3/4] (그냥 Enter = 2): " MODE
[ -z "$MODE" ] && MODE=2

case "$MODE" in
  1)
    read -r -p "몇 개 신청할까요? (그냥 Enter = 100개, 0 = 있는 것 전부): " LIMIT
    [ -z "$LIMIT" ] && LIMIT=100
    [ "$LIMIT" = "전체" ] && LIMIT=0
    python3 main.py --notion-pending --limit "$LIMIT" --ask-column
    ;;
  3)
    python3 main.py --setup-notion
    ;;
  4)
    python3 web_ui.py
    ;;
  *)
    read -r -p "몇 개 신청할까요? (그냥 Enter = 100개, 0 = 있는 것 전부): " LIMIT
    [ -z "$LIMIT" ] && LIMIT=100
    [ "$LIMIT" = "전체" ] && LIMIT=0
    python3 main.py --limit "$LIMIT" --ask-column
    ;;
esac
