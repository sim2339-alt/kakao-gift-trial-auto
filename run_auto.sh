#!/usr/bin/env bash
# 맥·리눅스용: 실제 TOP 30 신청
cd "$(dirname "$0")"
python3 main.py --limit 30
