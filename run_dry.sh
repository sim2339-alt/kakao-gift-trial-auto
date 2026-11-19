#!/usr/bin/env bash
# 맥·리눅스용: 후보 목록만 확인
cd "$(dirname "$0")"
python3 main.py --dry-run
