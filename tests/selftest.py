# -*- coding: utf-8 -*-
"""
자가 점검 스크립트 (개발자용)

실제 카카오 사이트에 로그인하지 않고도 필터링·화면 조작 로직이 깨지지 않았는지 확인합니다.
    python tests/selftest.py

- 1부: 필터/정렬 로직을 저장된 샘플 응답으로 검증
- 2부: Playwright 페이지를 가짜 객체로 바꿔 신청 플로우(성공/이미신청/버튼없음/로그인필요) 검증
- 6부: 웹 조종판(web_ui.py) API·실행/중단·토큰 미노출 검증
- 7부: 노션 API 설정 1회 저장 → 이후 실행은 담당자 열만 선택(토큰·DB ID 재질문 없음) 검증
- 8부: 원스톱 실행기(run.bat)와 루트 정리, 웹 조종판 한 화면 흐름 검증
- 9부: 카카오 로그아웃(세션 파일 삭제 → 다른 계정 로그인) — CLI·웹 검증
- 10부: 담당자 열 목록 — 노션 페이지 주소 붙여넣기 정규화 · `_columns` 폴백 · [↻ 새로고침] 검증
- 11부: 계정 전환 캐시 — 세션 파일이 바뀌면 새 쿠키 · 로그아웃 초기화 · 로그인 계정 닉네임 · 이미 신청 건수 검증
- 12부: 카카오 닉네임 → 노션 담당자 열 자동 선택 — 매칭 점수 · 계정 변경 · 수동 선택 보호 검증
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import contextlib
import io
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import main  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------- 1부: 필터
SAMPLE = [
    # (상품명, [(대분류id, 대분류명, 중분류id, 중분류명)])
    ("수플린 설향딸기 800g", [(93, "식품", 203, "과일·견과")]),
    ("무드웨어 블러 립스틱", [(95, "뷰티", 181, "메이크업")]),
    ("호빵맨 딸랑딸랑 차임벨", [(98, "유아동·반려", 139, "장난감·인형")]),
    ("골프 우산", [(99, "레저·스포츠", 123, "골프")]),
    ("강아지 수제 간식", [(98, "유아동·반려", 314, "고양이 간식·용품")]),
    ("신생아 배냇저고리 세트", [(98, "유아동·반려", 144, "임신·출산·육아")]),
    ("스누피 3단 전기요", [(97, "디지털·가전", 163, "계절가전")]),
    ("유아 칫솔 4개입", [(98, "유아동·반려", 141, "유아교육·도서")]),
]


def build_item(title, cats):
    views = [
        {
            "displaySpace": "BASIC",
            "ldisplayCategoryId": lid,
            "ldisplayCategoryName": lname,
            "mdisplayCategoryId": mid,
            "mdisplayCategoryName": mname,
            "representative": True,
        }
        for lid, lname, mid, mname in cats
    ]
    item = {
        "campaignId": f"id-{title[:6]}",
        "campaignTitle": title,
        "brand": {"name": "테스트브랜드"},
        "applicationCount": 100,
        "recruitmentCount": 5,
        "remainingDays": 3,
        "imageUrl": "",
    }
    if views:
        item["displayCategoryViews"] = views
    return item


def test_filter():
    print("[1/12] 필터 검증")
    flt = main.load_filter()
    cases = {
        "수플린 설향딸기 800g": "allow",
        "무드웨어 블러 립스틱": "deny",
        "호빵맨 딸랑딸랑 차임벨": "deny",       # 제외 키워드(딸랑이 계열 '딸랑딸랑'은 '딸랑이' 미포함 → 카테고리로만 판정)
        "골프 우산": "deny",
        "강아지 수제 간식": "deny",
        "신생아 배냇저고리 세트": "deny",
        "스누피 3단 전기요": "allow",
        "유아 칫솔 4개입": "allow",
    }
    for title, cats in SAMPLE:
        decision, reason = main.classify(build_item(title, cats), flt)
        want = cases[title]
        # '딸랑딸랑' 상품은 장난감 카테고리라 허용되지만, 실제 응답의 '딸랑이' 키워드 상품은 제외된다.
        if title == "호빵맨 딸랑딸랑 차임벨":
            want = "allow"
        check(f"{title} → {want}", decision == want, f"실제 {decision}({reason})")

    # 카테고리 없음 → 보류
    decision, _ = main.classify(build_item("핫팩 10개입", []), flt)
    check("카테고리 없는 상품 → hold(보류)", decision == "hold", decision)
    decision, _ = main.classify(build_item("립스틱 세트", []), flt)
    check("카테고리 없음 + 제외 키워드 → deny", decision == "deny", decision)

    # 정렬
    items = [
        build_item("A", [(93, "식품", 203, "과일·견과")]),
        build_item("B", [(93, "식품", 203, "과일·견과")]),
        build_item("C", [(93, "식품", 203, "과일·견과")]),
    ]
    items[0]["applicationCount"], items[1]["applicationCount"], items[2]["applicationCount"] = 10, 500, 100
    items[0]["remainingDays"], items[1]["remainingDays"], items[2]["remainingDays"] = 1, 5, 2
    passed, held, denied = main.build_candidates(items, flt, "popular", None)
    check("인기순 정렬", [i["campaignTitle"] for i in passed] == ["B", "C", "A"], str([i["campaignTitle"] for i in passed]))
    passed, held, denied = main.build_candidates(items, flt, "deadline", None)
    check("마감임박순 정렬", [i["campaignTitle"] for i in passed] == ["A", "C", "B"], str([i["campaignTitle"] for i in passed]))
    passed, held, denied = main.build_candidates(items, flt, "popular", {items[1]["campaignId"]})
    check("이미 신청한 캠페인 제외", all(i["campaignId"] != items[1]["campaignId"] for i in passed))


# ---------------------------------------------------- 2부: 가짜 브라우저 검증
class FakeRequest:
    method = "POST"
    post_data_json = {"campaignId": "abc", "productId": 111, "optionId": 77}


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status = status
        self.url = main.BASE + main.APPLY_API_PATH
        self.request = FakeRequest()
        self._body = {"result": True} if body is None else body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def text(self):
        return json.dumps(self._body, ensure_ascii=False)


class FakeApiResponse:
    """page.context.request.get 응답 모사."""

    def __init__(self, status=200, data=None):
        self.status = status
        self._data = data

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class FakeApiBridge:
    """세션 쿠키가 붙은 GET (상세 조회 · 위시 상태 조회) 모사."""

    def __init__(self):
        self.detail = None
        self.wish = False
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        if "/wishes/wish" in url:
            return FakeApiResponse(200, {
                "wished": self.wish,
                "visibleTo": "others" if self.wish else None,
            })
        if isinstance(self.detail, Exception):
            raise self.detail
        if isinstance(self.detail, FakeApiResponse):
            return self.detail
        return FakeApiResponse(200, self.detail or {})


class FakeContext:
    def __init__(self):
        self.request = FakeApiBridge()


class FakeElement:
    def __init__(self, page, text):
        self.page = page
        self.text = text

    def is_visible(self):
        return True

    def click(self, timeout=3000):
        self.page.handle_click(self.text)

    def check(self, force=True):
        self.page.checked += 1


class FakeLocator:
    def __init__(self, page, texts):
        self.page = page
        self.texts = list(texts)

    def count(self):
        return len(self.texts)

    @property
    def first(self):
        return FakeElement(self.page, self.texts[0])

    def nth(self, i):
        return FakeElement(self.page, self.texts[i])

    def is_visible(self):
        return bool(self.texts)

    def click(self, timeout=3000):
        self.page.handle_click(self.texts[0])

    def check(self, force=True):
        self.page.checked += 1


DENIED_POPUP = "체험단에 응모하지 못했어요\n고객센터로 문의해주시면 도와드릴게요."


class FakePage:
    def __init__(self, buttons, body, url=None, on_apply_click="success", goto_redirect=None,
                 wishable=False, options=None, applied=False, campaign_status="IN_PROGRESS",
                 detail_api="ok"):
        self.buttons = buttons
        self.apply_body = body
        self.body = body
        self.url = url or main.BASE + "/giftbox/trial-campaign/abc/apply"
        self.on_apply_click = on_apply_click
        self.goto_redirect = goto_redirect  # 로그인 페이지로 튕기는 상황 모사
        self.listeners = {}
        self.checked = 0
        self.clicks = []
        self.goto_urls = []

        # 상세 API 응답(신청 페이지·상세 페이지가 쓰는 것과 같은 정보)
        if detail_api == "error":
            detail = RuntimeError("detail fetch failed")
        elif detail_api == "401":
            detail = FakeApiResponse(401, {})
        else:
            detail = {
                "campaignId": "abc",
                "productId": 111,
                "campaignTitle": "테스트 상품",
                "itemType": "Shipping",
                "campaignStatus": campaign_status,
                "applied": applied,
                "wishable": wishable,
                "options": options or [],
            }
        self.context = FakeContext()
        if isinstance(detail, FakeApiResponse):
            self.context.request.detail = detail
        elif isinstance(detail, Exception):
            self.context.request.detail = detail
        else:
            self.context.request.detail = detail

        # 상세 페이지(=신청 페이지 아님) 본문
        self.detail_body = "친구 공개로 위시에 담기" if wishable else "상품 기본 정보"

    @property
    def wish_added(self):
        return self.context.request.wish

    # --- playwright API 모사
    def goto(self, url, wait_until=None):
        self.goto_urls.append(url)
        if self.goto_redirect:
            self.url = self.goto_redirect
            return
        self.url = url
        if "/apply" not in url:
            self.body = self.detail_body

    def wait_for_timeout(self, ms):
        return None

    def inner_text(self, selector, timeout=None):
        return self.body

    def content(self):
        return "<html><body>fake</body></html>"

    def on(self, event, cb):
        self.listeners.setdefault(event, []).append(cb)

    def remove_listener(self, event, cb):
        if cb in self.listeners.get(event, []):
            self.listeners[event].remove(cb)

    def locator(self, selector, has_text=None):
        if selector == "input[type=checkbox]":
            return FakeLocator(self, [])
        if selector in ("a", "button", "[role=button]"):
            pool = self.buttons if selector != "a" else ["링크"]
            if has_text is not None:
                pat = has_text if isinstance(has_text, str) else has_text
                pool = [t for t in pool if (pat.search(t) if hasattr(pat, "search") else pat in t)]
            return FakeLocator(self, pool)
        return FakeLocator(self, [])

    def get_by_role(self, role, name=None):
        if role != "button":
            return FakeLocator(self, [])
        if name is None:
            return FakeLocator(self, self.buttons)
        return FakeLocator(self, [t for t in self.buttons if name.search(t)])

    # --- 클릭 처리
    def handle_click(self, text):
        self.clicks.append(text)
        if "위시" in text and "담기" in text:
            self.context.request.wish = True
            self.detail_body = "친구 공개로 위시 했어요"
            self.body = self.detail_body
            return
        if "신청" in text and self.on_apply_click == "success":
            self.url = main.BASE + "/giftbox/trial-campaign/applied/abc"
            for cb in self.listeners.get("response", []):
                cb(FakeResponse(200))
        elif "신청" in text and self.on_apply_click == "fail":
            self.body = "신청에 실패했습니다"
            for cb in self.listeners.get("response", []):
                cb(FakeResponse(400, {"errorCategory": "common", "errorCode": -10001,
                                      "errorMessage": "VALIDATION_ERROR", "errorName": "validation_error"}))
        elif "신청" in text and self.on_apply_click == "denied":
            # 서버가 표준 에러 봉투가 아닌 응답(500 기본 에러페이지)을 준 상황
            self.body = self.apply_body + "\n" + DENIED_POPUP
            for cb in self.listeners.get("response", []):
                cb(FakeResponse(500, {"timestamp": "2026-11-20T10:00:00", "status": 500,
                                      "error": "Internal Server Error", "path": "/a/partner-trial/api/v1/trial-campaigns/apply"}))


def test_apply_flow():
    print("[2/12] 신청 플로우 검증 (가짜 브라우저)")
    campaign = {
        "campaignId": "abc",
        "campaignTitle": "테스트 상품",
        "brand": {"name": "테스트"},
        "applicationCount": 10,
        "recruitmentCount": 5,
        "remainingDays": 2,
        "displayCategoryViews": [{
            "ldisplayCategoryId": 93, "ldisplayCategoryName": "식품",
            "mdisplayCategoryId": 203, "mdisplayCategoryName": "과일·견과",
        }],
    }
    ADDR = "배송지 서울시 중구 세종대로 110"
    APPLY = main.BASE + "/giftbox/trial-campaign/abc/apply"
    DETAIL = main.BASE + "/giftbox/trial-campaign/abc"

    # 1) 옵션 없는 캠페인 → /apply 그대로 진입
    page = FakePage(["신청 완료하기", "확인"], ADDR)
    res = main.apply_one(page, campaign)
    check("정상 신청 → 성공", res["status"] == "성공", f"{res['status']} / {res['reason']}")
    check("신청 버튼 클릭 발생", any("신청" in c for c in page.clicks), str(page.clicks))
    check("옵션 없으면 optionId 없이 진입", page.goto_urls[-1] == APPLY, str(page.goto_urls))
    check("사유에 옵션 정보 기록", "옵션 없음" in res["reason"], res["reason"])

    # 2) 옵션 있는 캠페인 → ?optionId=77 (이번 버그의 핵심 회귀 방지)
    page = FakePage(["신청 완료하기", "확인"], ADDR, options=[{"id": 77, "value": "화이트"}])
    res = main.apply_one(page, campaign)
    check("옵션 있으면 ?optionId=77 로 진입", "?optionId=77" in page.goto_urls[-1], str(page.goto_urls))
    check("옵션 캠페인 신청 → 성공", res["status"] == "성공", f"{res['status']} / {res['reason']}")

    # 3) 위시가 필요한 캠페인 → 상세 페이지에서 위시 먼저
    page = FakePage(["친구 공개로 위시에 담기", "신청 완료하기", "확인"], ADDR, wishable=True)
    res = main.apply_one(page, campaign)
    check("위시 담기 단계 처리", any("위시" in c for c in page.clicks), str(page.clicks))
    check("위시 페이지를 신청 페이지보다 먼저 연다", bool(page.goto_urls) and page.goto_urls[0] == DETAIL, str(page.goto_urls))
    check("위시 담김 상태 확인", page.wish_added is True)
    check("위시 + 신청 → 성공", res["status"] == "성공", f"{res['status']} / {res['reason']}")

    # 4) --skip-wishlist
    page = FakePage(["친구 공개로 위시에 담기", "신청 완료하기", "확인"], ADDR, wishable=True)
    res = main.apply_one(page, campaign, skip_wishlist=True)
    check("--skip-wishlist 시 위시 클릭 없음", all("위시" not in c for c in page.clicks), str(page.clicks))
    check("--skip-wishlist 시 상세 페이지를 열지 않음",
          all(APPLY in u for u in page.goto_urls), str(page.goto_urls))
    check("--skip-wishlist 로도 신청 성공", res["status"] == "성공", f"{res['status']} / {res['reason']}")

    # 5) 이미 신청(상세 applied) → 스킵 (페이지를 열지 않는다)
    page = FakePage(["신청 완료하기"], ADDR, applied=True)
    res = main.apply_one(page, campaign)
    check("이미 신청 → 스킵", res["status"] == "스킵", f"{res['status']} / {res['reason']}")
    check("이미 신청이면 신청 페이지를 열지 않음", page.goto_urls == [], str(page.goto_urls))

    # 6) 모집 중이 아닌 캠페인 → 스킵
    page = FakePage(["신청 완료하기"], ADDR, campaign_status="ANNOUNCING")
    res = main.apply_one(page, campaign)
    check("모집 중 아님 → 스킵", res["status"] == "스킵", f"{res['status']} / {res['reason']}")

    # 7) 화면에 '이미 신청' 문구 → 스킵
    page = FakePage(["신청 완료하기"], "이미 신청한 체험단입니다")
    res = main.apply_one(page, campaign)
    check("본문 '이미 신청' → 스킵", res["status"] == "스킵", f"{res['status']} / {res['reason']}")

    # 8) 신청 버튼 없음 → 실패
    page = FakePage([], "배송지 서울시 123")
    res = main.apply_one(page, campaign)
    check("신청 버튼 없음 → 실패", res["status"] == "실패", f"{res['status']} / {res['reason']}")

    # 9) 로그인 페이지로 튕김 → LoginRequired
    page = FakePage(["신청 완료하기"], "로그인",
                   goto_redirect="https://accounts.kakao.com/login?continue=x")
    try:
        main.apply_one(page, campaign)
        check("로그인 페이지로 튕김 → LoginRequired 예외", False, "예외 없음")
    except main.LoginRequired:
        check("로그인 페이지로 튕김 → LoginRequired 예외", True)

    page = FakePage(["신청 완료하기"], "카카오계정으로 로그인하세요")
    try:
        main.apply_one(page, campaign)
        check("본문에 로그인 안내 → LoginRequired 예외", False, "예외 없음")
    except main.LoginRequired:
        check("본문에 로그인 안내 → LoginRequired 예외", True)

    # 10) 상세 API 가 401 → LoginRequired
    page = FakePage(["신청 완료하기"], ADDR, detail_api="401")
    try:
        main.apply_one(page, campaign)
        check("상세 API 401 → LoginRequired 예외", False, "예외 없음")
    except main.LoginRequired:
        check("상세 API 401 → LoginRequired 예외", True)

    # 11) 상세 API 실패 → 상세 없이 계속 진행(경고 기록)
    page = FakePage(["신청 완료하기"], ADDR, detail_api="error")
    res = main.apply_one(page, campaign)
    check("상세 조회 실패해도 신청은 진행", res["status"] == "성공", f"{res['status']} / {res['reason']}")
    check("사유에 상세 조회 실패 기록", "상세 조회 실패" in res["reason"], res["reason"])

    # 12) 표준 에러 봉투(400) → 서버 errorMessage 를 사유에 남긴다
    page = FakePage(["신청 완료하기"], ADDR, on_apply_click="fail")
    res = main.apply_one(page, campaign)
    check("신청 응답 400 → 실패", res["status"] == "실패", f"{res['status']} / {res['reason']}")
    check("400 사유에 서버 errorMessage 포함", "VALIDATION_ERROR" in res["reason"], res["reason"])

    # 13) '체험단에 응모하지 못했어요 / 고객센터' 팝업(비표준 응답) → 사유에 그대로 남긴다
    page = FakePage(["신청 완료하기"], ADDR, on_apply_click="denied")
    res = main.apply_one(page, campaign)
    check("비표준 응답(500) → 실패", res["status"] == "실패", f"{res['status']} / {res['reason']}")
    check("사유에 신청 응답 500 기록", "500" in res["reason"], res["reason"])
    check("사유에 '고객센터로 문의' 팝업 안내 기록", "고객센터로 문의" in res["reason"], res["reason"])

    # 14) 배송지: 이미 채워져 있으면 '배송지 변경'을 누르지 않는다(레이어가 신청 버튼을 가리는 문제 방지)
    page = FakePage(["배송지 변경하기", "신청 완료하기", "확인"], ADDR)
    main.apply_one(page, campaign)
    check("배송지가 있으면 변경 레이어를 열지 않음",
          all("변경" not in c for c in page.clicks), str(page.clicks))

    # 15) 배송지가 비었을 때만 선택 단계 수행
    page = FakePage(["배송지 선택하기", "기본 배송지", "확인", "신청 완료하기"],
                    "배송지 정보\n입력된 배송지 정보가 없어요!")
    main.apply_one(page, campaign)
    check("배송지가 비면 선택 단계 수행", any("선택하기" in c for c in page.clicks), str(page.clicks))

    # 16) 대기 위치: 신청 후가 아니라 [신청 완료하기] 클릭 직전
    events = []
    orig_sleep = time.sleep
    time.sleep = lambda sec: events.append(("sleep", sec))
    try:
        page = FakePage(["신청 완료하기", "확인"], ADDR)
        orig_click = page.handle_click
        page.handle_click = lambda text: (events.append(("click", text)), orig_click(text))[1]
        res = main.apply_one(page, campaign, pre_submit_delay=(3.0, 3.0))
    finally:
        time.sleep = orig_sleep
    sleeps = [e for e in events if e[0] == "sleep"]
    first_submit = next((i for i, e in enumerate(events)
                         if e[0] == "click" and "신청" in e[1]), None)
    first_sleep = next((i for i, e in enumerate(events) if e[0] == "sleep"), None)
    check("[신청 완료하기] 직전에 대기", res["status"] == "성공" and first_sleep is not None
          and first_submit is not None and first_sleep < first_submit, str(events[:6]))
    check("대기 시간이 --delay 값을 따른다", any(abs(v - 3.0) < 1e-9 for _, v in sleeps), str(sleeps))



# ------------------------------------------------- 4부: 노션 기록·설정·모드 검증
def notion_row(page_id, cid, title, mark="", column=None):
    """노션 DB 행 하나(가짜 응답용)."""
    column = column or main.NOTION_DEFAULT_COLUMN
    rt = [{"type": "text", "text": {"content": mark}, "plain_text": mark}] if mark else []
    return {
        "id": page_id,
        "properties": {
            "링크": {"type": "url", "url": main.DETAIL_PAGE.format(cid=cid)},
            "상품명·브랜드": {"type": "title", "title": [
                {"type": "text", "text": {"content": title}, "plain_text": title}]},
            column: {"type": "rich_text", "rich_text": rt},
        },
    }


class NotionStub:
    """노션 API 가짜 서버 — query / pages 생성 / pages 갱신 / DB 스키마 조회."""

    def __init__(self, rows=None, schema=None, fail=None):
        self.rows = list(rows or [])
        self.schema = schema
        self.fail = fail        # (status, data, err) 를 모든 호출에 그대로 돌려준다
        self.calls = []
        self.patches = {}

    def __call__(self, method, path, payload, token, timeout=None):
        self.calls.append((method, path, payload))
        if self.fail is not None:
            return self.fail
        if method == "GET" and path.startswith("/databases/"):
            return 200, {"object": "database", "properties": self.schema or {}}, None
        if method == "POST" and path.endswith("/query"):
            flt = (payload or {}).get("filter") or {}
            rows = self._match(flt) if flt else list(self.rows)
            return 200, {"results": rows, "has_more": False, "next_cursor": None}, None
        if method == "POST" and path == "/pages":
            props = (payload or {}).get("properties") or {}
            page_id = f"new-{len(self.calls)}"
            self.rows.append({"id": page_id, "properties": _props_from_payload(props)})
            return 200, {"id": page_id}, None
        if method == "PATCH" and path.startswith("/pages/"):
            page_id = path.rsplit("/", 1)[-1]
            self.patches[page_id] = (payload or {}).get("properties") or {}
            return 200, {"id": page_id}, None
        return 404, {"message": "not found"}, None

    def _match(self, flt):
        prop = flt.get("property")
        cond = flt.get("url") or flt.get("title") or {}
        needle = cond.get("contains") or ""
        out = []
        for row in self.rows:
            val = main._plain(row["properties"].get(prop))
            if needle and needle in val:
                out.append(row)
        return out

    def posted_props(self):
        for method, path, payload in self.calls:
            if method == "POST" and path == "/pages":
                return (payload or {}).get("properties") or {}
        return {}


def _props_from_payload(props):
    """POST /pages 로 보낸 속성 → 응답 모양(plain_text 포함)."""
    out = {}
    for name, val in props.items():
        for kind in ("title", "rich_text"):
            if kind in val:
                chunks = [{"type": "text", "text": c["text"],
                           "plain_text": c["text"]["content"]} for c in val[kind]]
                out[name] = {"type": kind, kind: chunks}
                break
        if "url" in val:
            out[name] = {"type": "url", "url": val["url"]}
    return out


class FakeInput:
    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


class use_input:
    """input() 을 잠시 바꿔 쓴다."""

    def __init__(self, answers):
        self.fake = FakeInput(answers)

    def __enter__(self):
        import builtins
        self._old = builtins.input
        builtins.input = self.fake
        return self.fake

    def __exit__(self, *exc):
        import builtins
        builtins.input = self._old
        return False


def test_notion():
    print("[4/12] 노션 기록·설정·실행 모드 검증 (가짜 노션 API)")
    NOW = datetime(2026, 11, 20, 11, 0, 0)
    CFG = {"token": "ntn_dummy", "db_id": "db-dummy", "column": "메인.엽", "file": "notion_secret.json"}
    item = {
        "campaignId": "abc123",
        "title": "핫팩 10개입",
        "brand": "테스트브랜드",
        "option": "화이트",
        "skip_wishlist": False,
        "applicationCount": 120,
        "recruitmentCount": 10,
        "remainingDays": 3,
        "status": "성공",
    }

    # 1) 신규 행 생성
    stub = NotionStub()
    rec = main.notion_record(CFG, item, transport=stub, now=NOW)
    check("노션: 새 캠페인 → 행 생성", rec["status"] == "created", f"{rec}")
    props = stub.posted_props()
    sent_title = "".join(c["text"]["content"] for c in props.get("상품명·브랜드", {}).get("title", []))
    check("노션: 상품명·브랜드(title)에 [브랜드] 붙여 기록", sent_title == "[테스트브랜드] 핫팩 10개입",
          sent_title)
    check("노션: 링크(url) 기록", props.get("링크", {}).get("url", "").endswith("/trial-campaign/abc123"),
          str(props.get("링크")))
    check("노션: 옵션 기록",
          props.get("옵션", {}).get("rich_text", [{}])[0].get("text", {}).get("content") == "화이트",
          str(props.get("옵션"))[:120])
    check("노션: 메커니즘 '상세→위시→옵션→신청'",
          props.get("메커니즘", {}).get("rich_text", [{}])[0].get("text", {}).get("content") == "상세→위시→옵션→신청",
          str(props.get("메커니즘"))[:120])
    check("노션: 마감일·모집현황 'D-3 · 120/10명'",
          props.get("마감일·모집현황", {}).get("rich_text", [{}])[0].get("text", {}).get("content") == "D-3 · 120/10명",
          str(props.get("마감일·모집현황"))[:120])
    check("노션: 내 열에 '11/20'(날짜만)",
          props.get("메인.엽", {}).get("rich_text", [{}])[0].get("text", {}).get("content") == "11/20",
          str(props.get("메인.엽"))[:120])

    # 2) 기존 행(내 열 비어 있음) → 새 행 없이 PATCH
    stub = NotionStub(rows=[notion_row("p1", "abc123", "[테스트] 테스트 핫팩 10개입")])
    rec = main.notion_record(CFG, item, transport=stub, now=NOW)
    check("노션: 기존 행 → 갱신(PATCH)", rec["status"] == "updated", f"{rec}")
    check("노션: 기존 행에는 POST /pages 없음",
          not any(m == "POST" and p == "/pages" for m, p, _ in stub.calls), str(stub.calls)[:160])
    check("노션: 갱신은 내 열만 보낸다",
          list(stub.patches.get("p1", {}).keys()) == ["메인.엽"], str(stub.patches.get("p1"))[:160])

    # 3) 같은 날 이미 기록됨 → 그대로 둔다
    stub = NotionStub(rows=[notion_row("p1", "abc123", "t", mark="11/20")])
    rec = main.notion_record(CFG, item, transport=stub, now=NOW)
    check("노션: 오늘 날짜 기록 있음 → unchanged", rec["status"] == "unchanged", f"{rec}")
    check("노션: unchanged 면 PATCH 하지 않음", stub.patches == {}, str(stub.patches))

    # 4) 다른 날 기록만 있음 → 기존 내용 유지 + 줄바꿈으로 덧붙임
    stub = NotionStub(rows=[notion_row("p1", "abc123", "t", mark="11/19")])
    rec = main.notion_record(CFG, item, transport=stub, now=NOW)
    new = "".join(c["text"]["content"] for c in stub.patches["p1"]["메인.엽"]["rich_text"])
    check("노션: 기존 기록 유지 + 덧붙임", rec["status"] == "updated" and new == "11/19\n11/20", f"{rec} / {new!r}")

    # 5) 다른 계정 열을 쓰면 그 열에만 기록
    cfg2 = dict(CFG, column="서브.동")
    stub = NotionStub(rows=[notion_row("p2", "abc123", "t", mark="11/20", column="서브.동")])
    rec = main.notion_record(cfg2, item, transport=stub, now=NOW)
    check("노션: --notion-column 열 기준으로 중복 판정", rec["status"] == "unchanged", f"{rec}")

    # 5-1) 노션 검색 지연 대비 — 이번 배치 색인으로도 중복을 막는다
    rec = main.notion_record(CFG, item, transport=NotionStub(), now=NOW,
                             index={"abc123": {"page_id": "p9", "mark": "11/20"}})
    check("노션: 색인에 오늘 기록이 있으면 unchanged", rec["status"] == "unchanged", f"{rec}")
    stub = NotionStub()
    rec = main.notion_record(CFG, item, transport=stub, now=NOW,
                             index={"abc123": {"page_id": "p9", "mark": ""}})
    check("노션: 색인 행이 미체크면 그 행을 갱신", rec["status"] == "updated" and "p9" in stub.patches, f"{rec}")
    idx = {}
    stub = NotionStub()
    rec = main.notion_record(CFG, item, transport=stub, now=NOW, index=idx)
    check("노션: 생성 후 배치 색인에 등록", rec["status"] == "created"
          and idx.get("abc123", {}).get("mark") == "11/20", str(idx))
    rec = main.notion_record(CFG, item, transport=stub, now=NOW, index=idx)
    check("노션: 같은 배치에서 두 번째 기록 → 중복 생성 없음", rec["status"] == "unchanged", f"{rec}")

    # 6) 노션 오류가 나도 예외 없이 failed
    stub = NotionStub(fail=(500, {"message": "internal error"}, "boom"))
    rec = main.notion_record(CFG, item, transport=stub, now=NOW)
    check("노션: 오류 → 예외 없이 failed", rec["status"] == "failed" and "HTTP 500" in rec["detail"], f"{rec}")
    stub = NotionStub(fail=(None, None, "URLError: timeout"))
    rec = main.notion_record(CFG, item, transport=stub, now=NOW)
    check("노션: 네트워크 오류 → failed", rec["status"] == "failed", f"{rec}")

    # 7) 설정 파일: 없음/꺼짐/열 덮어쓰기
    check("노션: notion_secret.json 없음 → 기록 안 함(None)",
          main.load_notion_config(path=str(ROOT / "logs" / "_no_such_notion.json")) is None)
    tmp = ROOT / "logs" / "_test_notion_secret.json"
    tmp.parent.mkdir(exist_ok=True)
    tmp.write_text(json.dumps({"enabled": False, "token": "x", "db_id": "y"}, ensure_ascii=False),
                   encoding="utf-8")
    check("노션: enabled=false → 기록 안 함(None)", main.load_notion_config(path=str(tmp)) is None)
    tmp.write_text(json.dumps({"token": "ntn_x", "db_id": "d", "column": "메인.엽"}, ensure_ascii=False),
                   encoding="utf-8")
    cfg3 = main.load_notion_config(path=str(tmp))
    check("노션: column 없으면 기본 '메인.엽'", cfg3["column"] == "메인.엽", str(cfg3))
    cfg3 = main.load_notion_config(path=str(tmp), column="서브.택")
    check("노션: CLI 열 이름이 파일보다 우선", cfg3["column"] == "서브.택", str(cfg3))

    # 8) DB 전체 읽기 + 1번 모드 선정(노션 DB 의 미체크 행만 · 신규 상품 제외)
    stub = NotionStub(rows=[
        notion_row("p1", "aaa", "체크된 상품", mark="11/20"),
        notion_row("p2", "bbb", "미체크 상품"),
        notion_row("p3", "ccc", "다른 열만 체크", mark="11/20", column="서브.동"),
    ])
    scan = main.notion_scan(CFG, transport=stub)
    check("노션: DB 스캔으로 campaignId→행 매핑", set(scan) == {"aaa", "bbb", "ccc"}, str(scan.keys()))
    rows = main.notion_rows(CFG, transport=stub)
    check("노션: DB 전체 행을 행 단위로 읽는다(링크 키 추출)",
          len(rows) == 3 and rows[1]["campaignId"] == "bbb" and rows[1]["mark"] == "", str(rows[:2]))

    # 8-1) 모드 1: 노션 DB 에 있는 미체크 행만 → 신규(노션에 없는) 상품은 제외
    def live_item(cid, title, cats):
        it = build_item(title, cats)
        it["campaignId"] = cid
        return it

    flt = main.load_filter()
    live = [
        live_item("bbb", "수플린 설향딸기 800g", [(93, "식품", 203, "과일·견과")]),
        live_item("ccc", "무드웨어 블러 립스틱", [(95, "뷰티", 181, "메이크업")]),
        live_item("eee", "스누피 3단 전기요", [(97, "디지털·가전", 163, "계절가전")]),
        live_item("fff", "카테고리 미확인 상품", []),
        live_item("ggg", "노션에 없는 신규 상품", [(93, "식품", 200, "디저트")]),
    ]
    rows = [
        {"page_id": "p1", "campaignId": "aaa", "title": "체크된 상품", "mark": "11/20"},
        {"page_id": "p2", "campaignId": "bbb", "title": "수플린 설향딸기", "mark": ""},
        {"page_id": "p3", "campaignId": "bbb", "title": "수플린 설향딸기(중복 행)", "mark": ""},
        {"page_id": "p4", "campaignId": "ccc", "title": "립스틱", "mark": ""},
        {"page_id": "p5", "campaignId": "ddd", "title": "모집 끝난 상품", "mark": ""},
        {"page_id": "p6", "campaignId": "eee", "title": "이미 신청한 상품", "mark": ""},
        {"page_id": "p7", "campaignId": "fff", "title": "카테고리 미확인", "mark": ""},
        {"page_id": "p8", "campaignId": "", "title": "링크 없는 행", "mark": ""},
    ]
    picked, held, stat = main.select_notion_targets(rows, live, applied={"eee"}, flt=flt)
    ids = [c["campaignId"] for c in picked]
    check("모드1: 노션 미체크 행만 대상", ids == ["bbb"], str(ids))
    check("모드1: 노션에 없는 신규 상품은 제외", "ggg" not in ids, str(ids))
    check("모드1: 목록에 없는(모집 종료·삭제) 행은 스킵", "ddd" not in ids and stat["ended"] == 1, str(stat))
    check("모드1: 이미 신청(applied)한 행은 스킵", "eee" not in ids and stat["applied"] == 1, str(stat))
    check("모드1: 체크된 행 제외", stat["checked"] == 1 and "aaa" not in ids, str(stat))
    check("모드1: 같은 캠페인 중복 행은 1건으로 합침", stat["dup"] == 1 and ids.count("bbb") == 1, str(stat))
    check("모드1: 링크 없는 행은 건수로 보고", stat["no_link"] == 1, str(stat))
    check("모드1: 미체크 5건(캠페인 기준) · 모집 중 4 · 대상 1",
          stat["pending"] == 5 and stat["live"] == 4 and stat["targets"] == 1, str(stat))
    check("모드1: 제외 카테고리(립스틱)는 기본 제외",
          stat["deny"] == 1 and "ccc" not in ids, str(stat))
    check("모드1: 카테고리 미확인은 보류로 분리",
          stat["hold"] == 1 and [c["campaignId"] for c in held] == ["fff"], str(stat))

    # 8-2) --notion-all: 카테고리 기준까지 무시해도 신규 상품은 여전히 제외
    picked2, _held2, _stat2 = main.select_notion_targets(
        rows, live, applied={"eee"}, flt=flt, ignore_categories=True)
    ids2 = [c["campaignId"] for c in picked2]
    check("모드1: --notion-all 은 제외 카테고리까지 포함", "ccc" in ids2, str(ids2))
    check("모드1: --notion-all 도 노션에 없는 신규 상품은 제외", "ggg" not in ids2, str(ids2))

    # 8-3) 정렬: 인기순(신청자 많은 순) / 마감임박순
    live_b = [dict(live_item("h1", "상품H1", [(93, "식품", 203, "과일·견과")]), applicationCount=10, remainingDays=5),
              dict(live_item("h2", "상품H2", [(93, "식품", 203, "과일·견과")]), applicationCount=90, remainingDays=1)]
    rows_b = [{"page_id": "q1", "campaignId": "h1", "title": "H1", "mark": ""},
              {"page_id": "q2", "campaignId": "h2", "title": "H2", "mark": ""}]
    pop, _h, _s = main.select_notion_targets(rows_b, live_b, flt=flt, sort_mode="popular")
    check("모드1: 인기순 정렬", [c["campaignId"] for c in pop] == ["h2", "h1"], str([c["campaignId"] for c in pop]))
    dl, _h, _s = main.select_notion_targets(rows_b, live_b, flt=flt, sort_mode="deadline")
    check("모드1: 마감임박순 정렬", [c["campaignId"] for c in dl] == ["h2", "h1"], str([c["campaignId"] for c in dl]))

    # 8-4) 신청 이력을 못 읽었을 때(applied=None)도 동작하고 그 사실을 남긴다
    _p, _h, stat_n = main.select_notion_targets(rows, live, applied=None, flt=flt)
    check("모드1: 신청 이력 없이도 선정(확인 불가 표시)", stat_n["applied_checked"] is False, str(stat_n))

    # 9) 스키마에서 참가자 열 후보 추리기
    schema = {
        "상품명·브랜드": {"type": "title"},
        "링크": {"type": "url"},
        "등록일": {"type": "created_time"},
        "메커니즘": {"type": "rich_text"},
        "옵션": {"type": "rich_text"},
        "마감일·모집현황": {"type": "rich_text"},
        "서브.동": {"type": "rich_text"},
        "메인.엽": {"type": "rich_text"},
        "1팀.엽2(2200)": {"type": "rich_text"},
    }
    stub = NotionStub(schema=schema)
    cols = main.notion_list_columns("db-dummy", "ntn_dummy", transport=stub)
    check("설정: 메타 열(옵션·메커니즘·마감일) 제외", not ({"옵션", "메커니즘", "마감일·모집현황"} & set(cols)),
          str(cols))
    check("설정: 기본 열(메인.엽)이 1번", cols[0] == "메인.엽", str(cols))
    check("설정: 참가자 열 후보 3개", len(cols) == 3, str(cols))

    # 10) 설정 마법사: 토큰 → DB ID(Enter 기본) → 열 번호 → 파일 저장
    stub = NotionStub(schema=schema)
    path = ROOT / "logs" / "_test_setup_notion.json"
    path.unlink(missing_ok=True)
    with use_input(["ntn_setup_token", "", "2"]):
        cfg = main.setup_notion(path=str(path), transport=stub)
    saved = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    check("설정: notion_secret.json 자동 생성", path.is_file() and cfg is not None, str(saved))
    check("설정: 토큰 저장", saved.get("token") == "ntn_setup_token", str(saved.get("token")))
    check("설정: DB ID Enter → 기본값",
          saved.get("db_id") == main.NOTION_DEFAULT_DB_ID, str(saved.get("db_id")))
    check("설정: 고른 번호(2번) 열 저장", saved.get("column") == cols[1], str(saved.get("column")))
    check("설정: 저장된 파일을 그대로 다시 읽는다", main.load_notion_config(path=str(path)) is not None)
    path.unlink(missing_ok=True)
    tmp.unlink(missing_ok=True)

    # 11) 개수 입력: Enter = 기본(100), 숫자·0·'전체' 인정
    check("기본값: LIMIT_DEFAULT = 100", main.LIMIT_DEFAULT == 100, str(main.LIMIT_DEFAULT))
    check("기본값: 노션 미체크 기본도 100", main.LIMIT_DEFAULT_UNCHECKED == 100,
          str(main.LIMIT_DEFAULT_UNCHECKED))
    with use_input(["", "10", "abc", "7"]) as fake:
        check("입력: Enter → 기본 100", main.ask_limit() == 100)
        check("입력: 숫자 → 그 값", main.ask_limit() == 10)
        check("입력: 문자는 다시 묻는다", main.ask_limit() == 7, str(fake.prompts[-1:]))
    with use_input(["0", "전체", "all", " 0 "]):
        check("입력: 0 → 전체(0)", main.ask_limit() == 0)
        check("입력: '전체' → 전체(0)", main.ask_limit() == 0)
        check("입력: 'all' → 전체(0)", main.ask_limit() == 0)
        check("입력: 공백+'0' → 전체(0)", main.ask_limit(50) == 0)
    with use_input(["200"]):
        check("입력: 200개 지원", main.ask_limit() == 200)
    with use_input([]):
        check("입력: EOF → 기본값", main.ask_limit(50) == 50)

    # 12) 표시 문자열
    check("노션: 마감 정보 없으면 '마감일 미확인'",
          main.notion_status_text({"applicationCount": 1, "recruitmentCount": 2}).startswith("마감일 미확인"),
          main.notion_status_text({"applicationCount": 1, "recruitmentCount": 2}))
    check("노션: 옵션 없으면 메커니즘에 옵션 없음",
          main.notion_mechanism({"option": "", "skip_wishlist": False}) == "상세→위시→신청",
          main.notion_mechanism({"option": "", "skip_wishlist": False}))
    check("노션: 제출 직전 대기 파라미터가 apply_one 에 있다",
          "pre_submit_delay" in main.apply_one.__code__.co_varnames,
          str(main.apply_one.__code__.co_varnames))


# ------------------------------------------- 5부: main() 실행 모드 통합 점검
class FakeStdin:
    def __init__(self, tty=True):
        self.tty = tty

    def isatty(self):
        return self.tty


def test_main_modes():
    print("[5/12] 실행 모드 통합 검증 (main() — 네트워크·브라우저 없이)")
    items = [
        build_item("스누피 3단 전기요", [(97, "디지털·가전", 163, "계절가전")]),
        build_item("수플린 설향딸기 800g", [(93, "식품", 203, "과일·견과")]),
        build_item("무드웨어 블러 립스틱", [(95, "뷰티", 181, "메이크업")]),   # 제외 대상
        build_item("초콜릿 선물세트", [(93, "식품", 200, "디저트")]),          # 노션에 없는 신규
    ]
    saved = {
        "collect_campaigns": main.collect_campaigns,
        "collect_applied_ids": main.collect_applied_ids,
        "load_notion_config": main.load_notion_config,
        "notion_rows": main.notion_rows,
        "notion_list_columns": main.notion_list_columns,
        "run_apply": main.run_apply,
        "stdin": sys.stdin,
    }
    # 패치는 saved 를 담은 뒤에 한다(먼저 패치하면 복구할 때 가짜 함수가 남는다)
    main.notion_list_columns = lambda db, tok, transport=None: ["메인.엽", "서브.동"]
    seen = []

    def fake_run_apply(candidates, args):
        seen.append({"count": len(candidates),
                     "limit": args.limit,
                     "notion": getattr(args, "notion_cfg", None),
                     "ids": [c["campaignId"] for c in candidates]})
        return 0

    main.collect_campaigns = lambda mp: items
    main.collect_applied_ids = lambda: None
    main.load_notion_config = lambda path=None, column=None: {
        "token": "ntn_dummy", "db_id": "db", "column": column or "메인.엽", "file": "x"}
    main.run_apply = fake_run_apply
    try:
        # (1) 인자 없이 대화형: 모드 2 + 개수 10
        sys.stdin = FakeStdin(True)
        with use_input(["2", "10"]):
            rc = main.main([])
        check("모드: 대화형 2번 → 개수 10", rc == 0 and seen[-1]["limit"] == 10, f"rc={rc} {seen[-1:]}")

        # (2) 인자 없이 대화형: 모드 1 → 노션 미체크(등록된 행)만 + 개수 5
        main.notion_rows = lambda cfg, transport=None: [
            {"page_id": "p1", "campaignId": "id-스누피 3단", "title": "t", "mark": "11/20"},  # 체크됨
            {"page_id": "p2", "campaignId": "id-수플린 설향", "title": "t", "mark": ""},         # 미체크
            {"page_id": "p3", "campaignId": "id-무드웨어 블", "title": "t", "mark": ""},         # 미체크지만 제외 카테고리
        ]
        with use_input(["1", "5"]):
            rc = main.main([])
        got = seen[-1]
        check("모드1: 노션 체크된 상품 제외", "id-스누피 3단" not in got["ids"], str(got["ids"]))
        check("모드1: 미체크 상품은 대상에 포함", "id-수플린 설향" in got["ids"], str(got["ids"]))
        check("모드1: 노션에 없는 신규 상품은 제외", "id-초콜릿 선물" not in got["ids"], str(got["ids"]))
        check("모드1: 노션 미체크 행만으로 대상 구성", got["ids"] == ["id-수플린 설향"], str(got["ids"]))
        check("모드1: 개수 5 로 진행", rc == 0 and got["limit"] == 5, f"rc={rc} {got}")
        check("모드1: 노션 설정이 run_apply 로 전달", got["notion"] is not None, str(got["notion"]))

        # (3) --notion-pending + --limit: 묻지 않고 바로 실행(배치)
        with use_input([]) as fake:
            rc = main.main(["--notion-pending", "--limit", "2"])
        check("배치: --notion-pending --limit 2 → 묻지 않음", rc == 0 and seen[-1]["limit"] == 2
              and fake.prompts == [], f"rc={rc} prompts={fake.prompts}")
        check("배치: 미체크만 신청", "id-스누피 3단" not in seen[-1]["ids"], str(seen[-1]["ids"]))
        check("배치: 신규 상품 제외", "id-초콜릿 선물" not in seen[-1]["ids"], str(seen[-1]["ids"]))

        # (3-1) --notion-all: 제외 카테고리까지 포함(단 신규 상품은 여전히 제외)
        with use_input([]) as fake:
            rc = main.main(["--notion-pending", "--limit", "5", "--notion-all"])
        check("--notion-all: 제외 카테고리 행 포함", "id-무드웨어 블" in seen[-1]["ids"], str(seen[-1]["ids"]))
        check("--notion-all: 신규 상품은 제외 유지", "id-초콜릿 선물" not in seen[-1]["ids"], str(seen[-1]["ids"]))

        # (4) --limit 만 주면 모드 2 로 바로 실행
        with use_input([]) as fake:
            rc = main.main(["--limit", "3"])
        check("배치: --limit 만 → 모드2·개수3", rc == 0 and seen[-1]["limit"] == 3 and fake.prompts == [],
              f"rc={rc} prompts={fake.prompts}")

        # (5) --no-notion 이면 노션 설정을 아예 쓰지 않는다
        with use_input(["2"]):
            rc = main.main(["--limit", "3", "--no-notion"])
        check("--no-notion: 노션 설정 없음", rc == 0 and seen[-1]["notion"] is None, str(seen[-1]))

        # (6) 1번 모드인데 노션 설정이 없으면 중단(중복 신청 방지)
        main.load_notion_config = lambda path=None, column=None: None
        sys.stdin = FakeStdin(False)
        rc = main.main(["--notion-pending", "--limit", "5"])
        check("모드1: 노션 설정 없으면 중단", rc == 1, f"rc={rc}")

        # (7) --limit 0 = 제한 없이 대상 전체
        sys.stdin = FakeStdin(False)
        with use_input([]) as fake:
            rc = main.main(["--limit", "0"])
        got0 = seen[-1]
        check("--limit 0 → 전체(제한 없음)로 실행", rc == 0 and got0["limit"] == 0 and fake.prompts == [],
              f"rc={rc} {got0}")
        check("--limit 0 → 대상 전부가 run_apply 로 전달", got0["count"] == len(got0["ids"]) and got0["count"] >= 2,
              str(got0))
        check("--limit 0 → 제외 카테고리는 여전히 빠짐", "id-무드웨어 블" not in got0["ids"], str(got0["ids"]))

        # (8) 개수 0 = 화면 표시·CSV 도 후보 전부
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main.print_table(items, 0)
        check("표시: limit 0 → 후보 전체 출력", f"표시 {len(items)}개" in buf.getvalue(), buf.getvalue()[-90:])
        csv_path = main.save_candidates_csv(items, 0)
        csv_lines = csv_path.read_text(encoding="utf-8-sig").strip().splitlines()
        check("CSV: limit 0 → 후보 전체 저장", len(csv_lines) - 1 == len(items), str(len(csv_lines)))
        csv_path.unlink(missing_ok=True)
    finally:
        main.collect_campaigns = saved["collect_campaigns"]
        main.collect_applied_ids = saved["collect_applied_ids"]
        main.load_notion_config = saved["load_notion_config"]
        main.notion_rows = saved["notion_rows"]
        main.notion_list_columns = saved["notion_list_columns"]
        main.run_apply = saved["run_apply"]
        sys.stdin = saved["stdin"]


def cleanup():
    for path in (ROOT / "logs").glob("fail_*.html"):
        path.unlink(missing_ok=True)


# --------------------------------------------- 6부: 웹 조종판(web_ui) 검증
def test_web():
    print("[6/12] 웹 조종판 API 검증 (가짜 카카오·노션, 실제 HTTP 서버)")
    import web_ui

    web_ui.reset_state()
    saved = {
        "collect_campaigns": main.collect_campaigns,
        "collect_applied_ids": main.collect_applied_ids,
        "load_notion_config": main.load_notion_config,
        "notion_rows": main.notion_rows,
        "notion_list_columns": main.notion_list_columns,
        "login_state": main.login_state,
        "main": main.main,
        "secret": main.NOTION_SECRET_FILE,
    }
    flt = main.load_filter()

    def live_item(cid, title, cats):
        it = build_item(title, cats)
        it["campaignId"] = cid
        return it

    items = [
        live_item("n1", "수플린 설향딸기 800g", [(93, "식품", 203, "과일·견과")]),
        live_item("n2", "무드웨어 블러 립스틱", [(95, "뷰티", 181, "메이크업")]),
        live_item("n3", "스누피 3단 전기요", [(97, "디지털·가전", 163, "계절가전")]),
        live_item("x9", "노션에 없는 신규 상품", [(93, "식품", 200, "디저트")]),
    ]
    rows = [
        {"page_id": "p1", "campaignId": "n1", "title": "수플린", "mark": ""},
        {"page_id": "p2", "campaignId": "n2", "title": "립스틱", "mark": ""},
        {"page_id": "p3", "campaignId": "n3", "title": "스누피", "mark": "11/20"},
    ]
    main.collect_campaigns = lambda mp: items
    main.collect_applied_ids = lambda: None
    main.notion_rows = lambda cfg, transport=None: rows
    main.load_notion_config = lambda path=None, column=None: {
        "token": "ntn_secret_dummy", "db_id": "db", "column": column or "메인.엽", "file": "x"}
    main.notion_list_columns = lambda db, tok, transport=None: ["메인.엽", "서브.동"]
    main.login_state = lambda: {"state": "ok", "detail": "테스트"}
    try:
        # 1) 상태 응답 — 열 목록은 주고 토큰은 주지 않는다
        web_ui.check_login()          # 로그인 상태는 버튼을 놀렸을 때만 확인한다(캐시)
        st = web_ui.api_status()
        check("웹: 상태에 참가자 열 목록", st["notion"]["columns"] == ["메인.엽", "서브.동"], str(st["notion"]))
        check("웹: 상태에 현재 열", st["notion"]["column"] == "메인.엽", str(st["notion"]))
        check("웹: 상태에 로그인 상태", st["login"]["state"] == "ok", str(st["login"]))
        check("웹: 상태 응답에 토큰 없음", "ntn_secret_dummy" not in json.dumps(st, ensure_ascii=False))

        # 2) 미리보기 — 1번 모드는 노션 미체크 행만(신규 상품 제외)
        pv = web_ui.preview("notion", 10)
        ids = [i["campaignId"] for i in pv["items"]]
        check("웹: 미리보기 성공", pv["ok"] and pv["total"] == 1, str(pv.get("summary")))
        check("웹: 미리보기 1번 모드 = 노션 미체크 행만", ids == ["n1"], str(ids))
        check("웹: 미리보기 신규 상품 제외", "x9" not in ids and "n3" not in ids, str(ids))
        check("웹: 미리보기 요약 문구 포함", "노션 DB" in pv["summary"] and "244" not in pv["summary"], pv["summary"])
        pv2 = web_ui.preview("list", 2)
        check("웹: 2번 모드 미리보기", pv2["ok"] and len(pv2["items"]) == 2, str(pv2.get("summary")))
        pv_all = web_ui.preview("list", 0)
        check("웹: 미리보기 limit 0 = 대상 전부", pv_all["ok"] and pv_all["limit"] == 0
              and len(pv_all["items"]) == pv_all["total"], f"{pv_all.get('total')} / {len(pv_all.get('items') or [])}")
        pv_all2 = web_ui.preview("list", "전체")
        check("웹: 미리보기 '전체' 문자열도 전부", pv_all2["ok"] and pv_all2["limit"] == 0
              and len(pv_all2["items"]) == pv_all2["total"], str(pv_all2.get("limit")))
        pv_def = web_ui.preview("list")
        check("웹: 미리보기 개수 생략 = 기본 100", pv_def["ok"] and pv_def["limit"] == 100, str(pv_def.get("limit")))
        pv3 = web_ui.preview("notion", 5, notion_all=True)
        check("웹: 제외 카테고리까지 포함 옵션", sorted(i["campaignId"] for i in pv3["items"]) == ["n1", "n2"],
              str([i["campaignId"] for i in pv3["items"]]))

        # 3) 참가자 열 저장 — 토큰은 그대로 두고 column 만 바꿈
        tmp_secret = ROOT / "logs" / "_web_notion_secret.json"
        tmp_secret.parent.mkdir(exist_ok=True)
        tmp_secret.write_text(json.dumps({"enabled": True, "token": "ntn_keep_me",
                                          "db_id": "db-x", "column": "메인.엽"}), encoding="utf-8")
        main.NOTION_SECRET_FILE = str(tmp_secret)
        web_ui.STATE["columns"] = None
        saved_col = web_ui.save_column("서브.동")
        written = json.loads(tmp_secret.read_text(encoding="utf-8"))
        check("웹: 열 저장 성공", saved_col["ok"] and written["column"] == "서브.동", str(written))
        check("웹: 열 저장 때 토큰·DB ID 보존", written["token"] == "ntn_keep_me" and written["db_id"] == "db-x",
              str(written))
        check("웹: 빈 열 이름은 거부", web_ui.save_column("  ")["ok"] is False)
        tmp_secret.unlink(missing_ok=True)

        # 4) 인자 조립
        check("웹: 1번 모드 인자", web_ui.build_argv({"mode": "notion", "limit": 5}) ==
              ["--notion-pending", "--limit", "5"], str(web_ui.build_argv({"mode": "notion", "limit": 5})))
        argv = web_ui.build_argv({"mode": "list", "limit": 3, "sort": "deadline",
                                 "skip_wishlist": True, "delay_min": 4, "delay_max": 6})
        check("웹: 2번 모드 인자(정렬·대기·위시)",
              argv == ["--limit", "3", "--sort", "deadline", "--skip-wishlist",
                       "--delay-min", "4.0", "--delay-max", "6.0"], str(argv))
        check("웹: --notion-all 인자", "--notion-all" in web_ui.build_argv({"mode": "notion", "notion_all": True}))
        check("웹: limit 0 인자", web_ui.build_argv({"mode": "list", "limit": 0}) == ["--limit", "0"],
              str(web_ui.build_argv({"mode": "list", "limit": 0})))
        check("웹: '전체' 인자도 0", web_ui.build_argv({"mode": "list", "limit": "전체"}) == ["--limit", "0"],
              str(web_ui.build_argv({"mode": "list", "limit": "전체"})))
        check("웹: 개수 미지정 인자 = 기본 100", web_ui.build_argv({"mode": "list"}) == ["--limit", "100"],
              str(web_ui.build_argv({"mode": "list"})))
        check("CLI: --limit all → 0(전체)", main._limit_arg("all") == 0 and main._limit_arg("전체") == 0,
              str((main._limit_arg("all"), main._limit_arg("전체"))))
        check("CLI: --limit 200 → 200", main._limit_arg("200") == 200)
        try:
            main._limit_arg("많이")
            bad_arg = True
        except Exception:
            bad_arg = False
        check("CLI: --limit 잘못된 값은 거부", bad_arg is False)

        # 5) 실행 → 중단 → 요약(진짜 main 대신 가짜 함수)
        def fake_main(argv):
            print("[가짜] 신청 시작")
            while not main.stop_requested():
                time.sleep(0.02)
            print("결과 요약: 성공 2건 / 스킵 1건 / 실패·불확실 0건")
            print("노션 기록: 2건 성공 / 0건 실패")
            return 0
        main.main = fake_main
        web_ui.reset_state()
        r1 = web_ui.start_job({"mode": "notion", "limit": 2})
        r2 = web_ui.start_job({"mode": "notion", "limit": 2})
        web_ui.stop_job()
        for _ in range(300):
            if not web_ui.job_running():
                break
            time.sleep(0.02)
        time.sleep(0.15)
        lp = web_ui.log_payload(0)
        check("웹: 자동 신청 시작", r1["ok"] is True and r1["argv"][0] == "--notion-pending", str(r1))
        check("웹: 실행 중 두 번째 시작은 거부", r2["ok"] is False and "이미 실행 중" in r2["error"], str(r2))
        check("웹: 중단 요청 → 실행 종료", not web_ui.job_running())
        check("웹: 로그에 진행 출력이 쌓임", any("가짜" in l for l in lp["lines"]), str(lp["lines"][:3]))
        check("웹: 완료 요약 숫자 파싱",
              lp["summary"] and lp["summary"].get("ok") == 2 and lp["summary"].get("notion") == [2, 0],
              str(lp["summary"]) + " / " + str(lp["lines"]))
        check("웹: 완료 후 phase idle", lp["phase"] == "idle" and lp["running"] is False, str(lp["phase"]))

        # 6) 실제 HTTP 서버 — 첫 화면·/api/status·/api/preview·없는 주소
        httpd = web_ui.make_server("127.0.0.1", 0)
        port = httpd.server_address[1]
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        base = f"http://127.0.0.1:{port}"
        try:
            html = urllib.request.urlopen(base + "/", timeout=10).read().decode("utf-8")
            check("웹: 첫 화면 HTML", "카카오 체험단" in html and "<script>" in html and "</html>" in html)
            check("웹: 개수 칩 50·100·200·전체", all(f'data-limit="{v}"' in html for v in ("50", "100", "200", "0"))
                  and ">전체<" in html, "")
            check("웹: 개수 기본 100 · 0=전체 안내", 'id="limit" value="100" min="0"' in html and "0 = 있는 것 전부" in html, "")
            check("웹: 모드 라벨 정리", "노션 미체크 목록 (신청 안 한 것만)" in html
                  and "카카오 전체 중 신청 안 한 것" in html and "인기순 N개" not in html, "")
            st2 = json.loads(urllib.request.urlopen(base + "/api/status", timeout=15).read().decode("utf-8"))
            check("웹: /api/status 200", st2.get("version") == web_ui.APP_VERSION, str(st2)[:120])
            check("웹: /api/status 토큰 미노출", "ntn_" not in json.dumps(st2, ensure_ascii=False))
            req = urllib.request.Request(
                base + "/api/preview", data=json.dumps({"mode": "notion", "limit": 3}).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            pv4 = json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8"))
            check("웹: /api/preview 동작", pv4.get("ok") is True and len(pv4.get("items") or []) <= 3,
                  str(pv4)[:120])
            req0 = urllib.request.Request(
                base + "/api/preview", data=json.dumps({"mode": "list", "limit": 0}).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            pv0 = json.loads(urllib.request.urlopen(req0, timeout=20).read().decode("utf-8"))
            check("웹: /api/preview limit 0 = 전부", pv0.get("ok") is True and pv0.get("limit") == 0
                  and len(pv0.get("items") or []) == pv0.get("total"),
                  f"limit={pv0.get('limit')} items={len(pv0.get('items') or [])} total={pv0.get('total')}")
            try:
                urllib.request.urlopen(base + "/api/nope", timeout=10)
                missing = 200
            except urllib.error.HTTPError as exc:
                missing = exc.code
            check("웹: 없는 API → 404", missing == 404, str(missing))
        finally:
            httpd.shutdown()
            httpd.server_close()
            th.join(timeout=3)
    finally:
        main.collect_campaigns = saved["collect_campaigns"]
        main.collect_applied_ids = saved["collect_applied_ids"]
        main.load_notion_config = saved["load_notion_config"]
        main.notion_rows = saved["notion_rows"]
        main.notion_list_columns = saved["notion_list_columns"]
        main.login_state = saved["login_state"]
        main.main = saved["main"]
        main.NOTION_SECRET_FILE = saved["secret"]
        main.reset_stop()
        web_ui.reset_state()


def test_login_logic():
    print("[3/12] 로그인 로직 검증 (네트워크 없이)")
    url = main._login_url()
    check("로그인 주소가 웹 로그인 입구(shopping-userauth-web)인가",
          url.startswith("https://shopping-userauth-web.kakao.com/login?channel=GIFT&redirect_url="), url[:80])
    check("로그인 주소에 gift /a/v1/web/login 복귀 지점이 포함",
          "gift.kakao.com%2Fa%2Fv1%2Fweb%2Flogin" in url, url)
    check("로그인 주소에 체험단 경로가 이중 인코딩으로 포함",
          "giftbox%252Ftrial-campaign" in url, url)
    check("로그인 주소에 모바일 앱 안내(/giftbox-redirect·gift-talk) 없음",
          "giftbox-redirect" not in url and "gift-talk" not in url)

    # 도메인별 쿠키 개수(값 미출력)
    cookies = [
        {"name": "a", "value": "secret", "domain": ".kakao.com"},
        {"name": "b", "value": "secret", "domain": "gift.kakao.com"},
        {"name": "c", "value": "secret", "domain": ".gift.kakao.com"},
        {"name": "d", "value": "secret", "domain": "shopping-userauth-web.kakao.com"},
    ]
    summary = main._cookie_summary(cookies)
    check("쿠키 도메인 집계", summary.get("kakao.com") == 1 and summary.get("gift.kakao.com") == 2,
          str(summary))

    # storage_state 파일 → CookieJar
    tmp = ROOT / "logs" / "_test_state.json"
    tmp.parent.mkdir(exist_ok=True)
    tmp.write_text(json.dumps({"cookies": [
        {"name": "_kau", "value": "x", "domain": ".kakao.com", "path": "/",
         "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax"},
        {"name": "gift_session", "value": "y", "domain": "gift.kakao.com", "path": "/",
         "expires": 1893456000.0, "httpOnly": True, "secure": True, "sameSite": "Lax"},
    ]}, ensure_ascii=False), encoding="utf-8")
    loaded = main._load_state_cookies(str(tmp))
    check("storage_state 쿠키 읽기", len(loaded) == 2, str(len(loaded)))
    jar = main._cookie_jar(loaded)
    check("쿠키 → CookieJar 변환", len(jar) == 2, str(len(jar)))
    tmp.unlink(missing_ok=True)

    # 폴링: 3번째 확인에서 200 이 나오면 즉시 성공 반환
    class FakeCtx:
        def __init__(self, ok_at):
            self.calls = 0
            self.ok_at = ok_at

        class _Resp:
            def __init__(self, status):
                self.status = status

        class _Req:
            def __init__(self, outer):
                self.outer = outer

            def get(self, url, headers=None):
                self.outer.calls += 1
                return FakeCtx._Resp(200 if self.outer.calls >= self.outer.ok_at else 401)

        @property
        def request(self):
            return FakeCtx._Req(self)

    class FakeWaitPage:
        def wait_for_timeout(self, ms):
            time.sleep(ms / 1000.0)  # 실제 대기처럼 시간을 흘려보낸다

    ok, checks = main._wait_for_login(FakeWaitPage(), FakeCtx(ok_at=2), wait_seconds=30)
    check("로그인 폴링: 2번째 확인에서 감지 후 즉시 종료", ok is True and checks <= 3, f"ok={ok} checks={checks}")
    ok, checks = main._wait_for_login(FakeWaitPage(), FakeCtx(ok_at=10 ** 6), wait_seconds=1)
    check("로그인 폴링: 시간 초과 시 실패 반환", ok is False, f"ok={ok}")


# --------------------- 7부: 노션 1회 저장 · 담당자 열만 선택(다시 묻지 않음)
def test_notion_once():
    print("[7/12] 노션 1회 저장 · 담당자 열만 선택 검증 (가짜 노션 API)")
    tmp = ROOT / "logs" / "_once_notion_secret.json"
    tmp.parent.mkdir(exist_ok=True)
    tmp.write_text(json.dumps({"enabled": True, "token": "ntn_saved_once", "db_id": "db-once",
                               "column": "메인.엽", "note": "keep"}, ensure_ascii=False), encoding="utf-8")
    cols = ["메인.엽", "1팀.엽2(2200)", "서브.동"]
    saved = {k: getattr(main, k) for k in ("notion_list_columns", "collect_campaigns",
                                           "collect_applied_ids", "load_notion_config",
                                           "notion_rows", "notion_secret_state",
                                           "run_apply", "NOTION_SECRET_FILE")}
    saved["stdin"] = sys.stdin
    main.notion_list_columns = lambda db, tok, transport=None: list(cols)
    main.NOTION_SECRET_FILE = str(tmp)
    try:
        # 1) 저장된 설정은 묻지 않고 그대로 읽는다(토큰·DB ID 재질문 없음)
        with use_input([]) as fake:
            cfg = main.load_notion_config()
        check("1회 저장: 토큰·DB ID 를 다시 묻지 않고 읽음", bool(cfg) and cfg["token"] == "ntn_saved_once"
              and fake.prompts == [], str(fake.prompts))
        check("1회 저장: 저장된 열을 기본으로 읽음", cfg["column"] == "메인.엽", str(cfg.get("column")))
        check("1회 저장: 상태 = ok", main.notion_secret_state() == "ok", main.notion_secret_state())

        # 2) 담당자 열만 묻는다 — Enter = 저장된 열
        with use_input([""]) as fake:
            col = main.ask_notion_column(cfg)
        prompts = " ".join(fake.prompts)
        check("열 선택: Enter → 저장된 열 유지", col == "메인.엽", col)
        check("열 선택: 묻는 것은 열 하나뿐(토큰·DB ID 질문 없음)",
              "토큰" not in prompts and "DB ID" not in prompts, prompts)
        check("열 선택: Enter 만 누르면 파일 그대로",
              json.loads(tmp.read_text(encoding="utf-8"))["column"] == "메인.엽")

        # 3) 번호로 다른 열을 고르면 즉시 기억(다음 실행 기본값)
        with use_input(["2"]):
            col2 = main.ask_notion_column(cfg)
        after = json.loads(tmp.read_text(encoding="utf-8"))
        check("열 선택: 2번 → '1팀.엽2(2200)'", col2 == cols[1], col2)
        check("열 선택: 고른 열을 파일에 기억", after["column"] == col2, str(after.get("column")))
        check("열 선택: 기억할 때 토큰·DB ID·다른 값 보존",
              after["token"] == "ntn_saved_once" and after["db_id"] == "db-once"
              and after.get("note") == "keep", str(after))
        check("열 선택: 다음 실행 기본값 = 직전 선택",
              main.load_notion_config()["column"] == col2, main.load_notion_config()["column"])

        # 4) 열 이름을 직접 쳐도 인정, 이상한 값은 저장된 열 유지
        with use_input(["서브.동"]):
            check("열 선택: 열 이름 직접 입력 인정",
                  main.ask_notion_column(main.load_notion_config()) == "서브.동")
        with use_input(["99"]):
            check("열 선택: 목록에 없는 번호 → 저장된 열 유지",
                  main.ask_notion_column(main.load_notion_config()) == "서브.동")

        # 5) 노션 읽기가 실패해도 저장된 열로 진행(질문 없음)
        main.notion_list_columns = lambda db, tok, transport=None: None
        cfg5 = main.load_notion_config()
        with use_input([]) as fake:
            col5 = main.ask_notion_column(cfg5)
        check("열 선택: 노션 읽기 실패 → 저장된 열·질문 없음", col5 == cfg5["column"] and fake.prompts == [],
              f"{col5} {fake.prompts}")

        # 6) 저장해 둔 열 목록(_columns)이 있으면 노션 읽기가 안 돼도 고를 수 있다
        data = json.loads(tmp.read_text(encoding="utf-8"))
        data["_columns"] = list(cols)
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with use_input(["1"]) as fake:
            col6 = main.ask_notion_column(main.load_notion_config())
        check("열 선택: 저장해 둔 열 목록으로도 고를 수 있다",
              bool(fake.prompts) and col6 in cols, f"{col6} {fake.prompts}")

        # 7) 설정 변경(--setup-notion): Enter 만 누르면 기존 값 유지 + 다른 키 보존
        main.notion_list_columns = lambda db, tok, transport=None: list(cols)
        with use_input(["", "", ""]) as fake:
            cfg7 = main.setup_notion()
        saved7 = json.loads(tmp.read_text(encoding="utf-8"))
        check("설정 변경: Enter 만 → 토큰·DB ID 유지",
              saved7["token"] == "ntn_saved_once" and saved7["db_id"] == "db-once", str(saved7.get("db_id")))
        check("설정 변경: 열도 유지", saved7["column"] in cols, str(saved7.get("column")))
        check("설정 변경: 다른 키·열 목록 보존", saved7.get("note") == "keep" and saved7.get("_columns") == cols,
              str(saved7.keys()))
        check("설정 변경: 저장 후 바로 쓸 수 있는 설정 반환", bool(cfg7) and cfg7["column"] == saved7["column"])

        # 8) main(): 배치(--limit)는 토큰·DB ID·열을 묻지 않는다
        tmp.write_text(json.dumps({"enabled": True, "token": "ntn_saved_once", "db_id": "db-once",
                                   "column": "메인.엽", "note": "keep"}, ensure_ascii=False), encoding="utf-8")
        items = [build_item("수플린 설향딸기 800g", [(93, "식품", 203, "과일·견과")]),
                 build_item("스누피 3단 전기요", [(97, "디지털·가전", 163, "계절가전")])]
        main.collect_campaigns = lambda mp: items
        main.collect_applied_ids = lambda: None
        main.notion_rows = lambda cfg, transport=None: [
            {"page_id": "p1", "campaignId": "id-수플린 설향", "title": "t", "mark": ""},
        ]
        seen = {}

        def fake_run_apply(cands, args):
            ncfg = getattr(args, "notion_cfg", None)
            seen.update({"count": len(cands),
                         "column": (ncfg or {}).get("column"),
                         "token": (ncfg or {}).get("token"),
                         "notion": ncfg is not None})
            return 0

        main.run_apply = fake_run_apply
        sys.stdin = FakeStdin(True)
        with use_input([]) as fake:
            rc = main.main(["--limit", "1"])
        check("실행(배치): 아무것도 묻지 않음", rc == 0 and fake.prompts == [], str(fake.prompts))
        check("실행(배치): 저장된 열·토큰 그대로 사용",
              seen.get("column") == "메인.엽" and seen.get("token") == "ntn_saved_once", str(seen))

        # 9) 대화형 마법사는 '어느 담당자 열'만 묻는다
        with use_input(["2", "3", "2"]) as fake:
            rc = main.main([])
        joined = " ".join(fake.prompts)
        check("실행(대화형): 토큰·DB ID 질문 없음", "노션 API 토큰" not in joined and "DB ID" not in joined,
              joined)
        check("실행(대화형): 담당자 열을 묻는다", "담당자 열" in joined, joined)
        check("실행(대화형): 고른 2번 열로 실행", rc == 0 and seen.get("column") == cols[1], str(seen))
        check("실행(대화형): 고른 열을 파일에 기억",
              json.loads(tmp.read_text(encoding="utf-8"))["column"] == cols[1])
        with use_input(["2", "1", ""]) as fake:
            rc = main.main([])
        check("실행(대화형): Enter → 직전 선택 열 유지", rc == 0 and seen.get("column") == cols[1], str(seen))

        # 10) --ask-column: 개수를 줘도 열만 묻는다(run_auto.bat 경로)
        with use_input(["3"]) as fake:
            rc = main.main(["--limit", "1", "--ask-column"])
        check("--ask-column: 열 하나만 묻고 3번 열로 실행",
              rc == 0 and seen.get("column") == cols[2] and len(fake.prompts) == 1, f"{fake.prompts} {seen}")

        # 11) 노션을 끄면 켜라고 다시 묻지 않고 한 줄만 알린다
        data = json.loads(tmp.read_text(encoding="utf-8"))
        data["enabled"] = False
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        check("상태: enabled=false → disabled", main.notion_secret_state() == "disabled")
        with use_input([]) as fake:
            rc = main.main(["--limit", "1"])
        check("실행: 노션 꺼짐 → 묻지 않고 진행", rc == 0 and fake.prompts == [] and seen.get("notion") is False,
              f"{fake.prompts} {seen}")

        # 12) 웹 조종판: 상태 saved · ⚙ 설정 변경(접힘)에만 토큰 입력
        import web_ui
        data["enabled"] = True
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        web_ui.reset_state()
        st = web_ui.api_status()
        check("웹: 상태 = 저장됨", st["notion"]["state"] == "saved" and st["notion"]["token_saved"] is True,
              str(st["notion"]))
        check("웹: 상태에 토큰 본문 없음", "ntn_saved_once" not in json.dumps(st, ensure_ascii=False))
        check("웹 ⚙설정: 빈 토큰·DB ID 거부", web_ui.save_notion_settings("", "")["ok"] is False)
        r = web_ui.save_notion_settings("ntn_new_token", "db-new", "")
        after2 = json.loads(tmp.read_text(encoding="utf-8"))
        check("웹 ⚙설정: 토큰·DB ID 저장(1회)", r["ok"] and after2["token"] == "ntn_new_token"
              and after2["db_id"] == "db-new", str(r)[:120])
        check("웹 ⚙설정: 응답에 토큰 없음", "ntn_new_token" not in json.dumps(r, ensure_ascii=False))
        check("웹 ⚙설정: 담당자 열 자동 선택·다른 키 보존",
              r["column"] in cols and after2.get("note") == "keep", str(r.get("column")))
        web_ui.reset_state()
        check("웹: 저장 직후 상태 saved", web_ui.api_status()["notion"]["state"] == "saved")
        check("웹: 열만 바꿔도 기억", web_ui.save_column(cols[2])["ok"]
              and json.loads(tmp.read_text(encoding="utf-8"))["column"] == cols[2])
        check("웹 ⚙설정: 노션 기록 끄기", web_ui.disable_notion()["ok"]
              and main.notion_secret_state() == "disabled")
        check("웹: 꺼짐 상태 표시", web_ui.api_status()["notion"]["state"] == "disabled")

        # 13) 화면: 토큰 입력은 '설정 변경' 접힘 메뉴 안에만 있다
        html = web_ui.PAGE_HTML
        check("웹 화면: ⚙ 설정 변경(접힘 details)", '<details class="admin" id="notionAdmin">' in html, "")
        check("웹 화면: 토큰 입력은 접힘 메뉴 안", 'id="tokenNew"' in html
              and html.index('id="tokenNew"') > html.index('id="notionAdmin"'), "")
        check("웹 화면: 노션 카드에 토큰 상시 입력 없음", 'id="btnSaveColumn"' not in html, "")
        check("웹 화면: '다시 묻지 않습니다' 안내", "다시 묻지 않습니다" in html, "")
    finally:
        for key, value in saved.items():
            if key == "stdin":
                sys.stdin = value
            else:
                setattr(main, key, value)
        tmp.unlink(missing_ok=True)


# ------------------------------- 8부: 원스톱 실행(run.bat) · 루트 정리 · 한 화면 흐름
def test_onestop():
    print("[8/12] 원스톱 실행기(run.bat) · 루트 정리 · 웹 한 화면 흐름 검증")
    root = ROOT
    run_bat = root / "run.bat"
    run_sh = root / "run.sh"
    legacy = ["install.bat", "install.sh", "run_auto.bat", "run_auto.sh", "run_login.bat",
              "run_login.sh", "run_dry.bat", "run_dry.sh", "run_web.bat", "run_web.sh"]

    # 1) 루트에는 실행기 하나만
    check("원스톱: 루트에 run.bat 이 있다", run_bat.is_file())
    check("원스톱: 맥·리눅스용 run.sh 도 같이 제공", run_sh.is_file())
    check("원스톱: 루트에 예전 실행 파일이 남아 있지 않다",
          not [n for n in legacy if (root / n).exists()],
          str([n for n in legacy if (root / n).exists()]))
    check("원스톱: 예전 실행 파일은 scripts/ 로 이동",
          all((root / "scripts" / n).is_file() for n in legacy),
          str([n for n in legacy if not (root / "scripts" / n).is_file()]))

    # 2) run.bat 이 설치까지 자동으로 처리하고 웹 조종판을 띄우는가
    bat = run_bat.read_text(encoding="utf-8", errors="replace")
    check("run.bat: 웹 조종판을 실행", "web_ui.py" in bat, "")
    check("run.bat: 필요한 패키지 자동 설치", "pip install -r requirements.txt" in bat, "")
    check("run.bat: 브라우저(크로미움) 자동 설치", "playwright install chromium" in bat, "")
    check("run.bat: 이미 설치됐으면 설치를 건너뛴다", bat.count("if errorlevel 1 goto") >= 1
          and "import playwright" in bat, "")
    check("run.bat: 파이썬이 없으면 안내", "python.org/downloads" in bat and ":NOPY" in bat, "")
    check("run.bat: 프로젝트 루트에서 실행", 'cd /d "%~dp0"' in bat and "%~dp0.." not in bat, "")
    check("run.bat: 윈도우용 CRLF 줄바꿈", b"\r\n" in run_bat.read_bytes(),
          str(run_bat.read_bytes()[:20]))
    check("run.bat: 설치·실행 실패 시 안내 후 멈춤", ":FAIL" in bat and "pause" in bat, "")

    # 3) scripts/ 안의 예전 실행 파일도 그대로 동작하게(상위 폴더로 이동)
    for name in [n for n in legacy if n.endswith(".bat")]:
        text = (root / "scripts" / name).read_text(encoding="utf-8", errors="replace")
        check(f"scripts/{name}: 상위 폴더(프로젝트 루트)에서 실행", 'cd /d "%~dp0.."' in text, text[:40])
    for name in [n for n in legacy if n.endswith(".sh")]:
        text = (root / "scripts" / name).read_text(encoding="utf-8", errors="replace")
        check(f"scripts/{name}: 상위 폴더(프로젝트 루트)에서 실행", '$(dirname "$0")/..' in text,
              text.splitlines()[2] if len(text.splitlines()) > 2 else "")

    # 4) 웹 조종판 한 화면 흐름: 로그인·담당자 열·미리보기·자동 신청 시작
    import web_ui
    html = web_ui.PAGE_HTML
    for label in ("카카오 로그인 창 열기", "로그인 확인", "담당자 열", "후보 목록 불러오기",
                  "자동 신청 시작", "중단", "설정 변경"):
        check(f"웹 한 화면: '{label}' 있음", label in html, "")
    check("웹 한 화면: 진행 순서 안내", "한 화면에서 끝납니다" in html, "")
    check("웹 한 화면: 로그인 → 열 → 미리보기 → 신청 순서대로 배치",
          html.index('id="btnLogin"') < html.index('id="column"')
          < html.index('id="btnPreview"') < html.index('id="btnStart"'), "")
    check("웹 한 화면: 노션 토큰·DB ID 는 접힘 메뉴에만",
          html.index('id="notionAdmin"') < html.index('id="tokenNew"'), "")
    check("웹 한 화면: 예전 [열 저장] 버튼 없음", "btnSaveColumn" not in html, "")

    # 5) README 는 run.bat 하나로 안내
    readme = (root / "README.md").read_text(encoding="utf-8")
    check("README: run.bat 을 첫 실행 안내로 사용", "run.bat" in readme and "더블클릭" in readme, "")
    check("README: 예전 실행 파일은 scripts/ 경로로 안내", "scripts\\" in readme or "scripts/" in readme, "")


def test_logout():
    print("[9/12] 카카오 로그아웃 검증 (세션 파일 삭제 · CLI · 웹)")
    import web_ui

    prof = ROOT / "logs" / "_logout_profile"
    statef = ROOT / "logs" / "_logout_state.json"

    def make_session():
        shutil.rmtree(prof, ignore_errors=True)
        (prof / "Default").mkdir(parents=True, exist_ok=True)
        (prof / "Default" / "Cookies").write_bytes(b"dummy")
        statef.write_text(json.dumps({"cookies": [
            {"name": "_kau", "value": "x", "domain": ".kakao.com", "path": "/"},
            {"name": "gift_session", "value": "y", "domain": "gift.kakao.com", "path": "/"},
        ]}, ensure_ascii=False), encoding="utf-8")

    saved = {"PROFILE_DIR": main.PROFILE_DIR, "STATE_FILE": main.STATE_FILE, "stdin": sys.stdin,
             "login_state": main.login_state}
    main.PROFILE_DIR = str(prof)
    main.STATE_FILE = str(statef)
    try:
        # 1) logout(): 쿠키 백업 + 브라우저 프로필 삭제
        make_session()
        res = main.logout(quiet=True)
        check("로그아웃: 프로필 폴더·세션 파일 삭제",
              res["ok"] and not prof.exists() and not statef.exists(), str(res)[:160])
        check("로그아웃: 지운 항목·저장 쿠키 수 보고",
              res["cookies"] == 2 and len(res["removed"]) == 2 and not res["failed"], str(res)[:160])
        res2 = main.logout(quiet=True)
        check("로그아웃: 이미 로그아웃 상태여도 안전", res2["ok"] and res2["removed"] == [], str(res2)[:120])

        # 2) CLI --logout: 묻지 않고 세션만 지운다
        make_session()
        sys.stdin = FakeStdin(True)
        with use_input([]) as fake:
            rc = main.main(["--logout"])
        check("CLI --logout: 코드 0 · 세션 삭제",
              rc == 0 and not prof.exists() and not statef.exists(), f"rc={rc}")
        check("CLI --logout: 아무것도 묻지 않음", fake.prompts == [], str(fake.prompts))

        # 3) 웹: 신청 중에는 거부, 아니면 즉시 로그아웃 + 상태 갱신
        web_ui.reset_state()
        make_session()
        main.login_state = lambda: {"state": "ok", "detail": "테스트"}
        web_ui.STATE["login"] = {"state": "ok", "detail": "테스트"}
        th = threading.Thread(target=lambda: time.sleep(0.8), daemon=True)
        web_ui.STATE["job"] = {"kind": "apply", "thread": th, "argv": []}
        th.start()
        r = web_ui.logout_kakao()
        check("웹 로그아웃: 자동 신청 중이면 거부",
              r["ok"] is False and "자동 신청" in (r["error"] or ""), str(r)[:160])
        check("웹 로그아웃: 거부됐을 때 세션 파일 유지", prof.exists() and statef.exists())
        th.join()
        web_ui.STATE["job"] = None
        r = web_ui.logout_kakao()
        check("웹 로그아웃: 세션 삭제", r["ok"] and not prof.exists() and not statef.exists(), str(r)[:160])
        st = web_ui.api_status()
        check("웹 로그아웃: 화면 상태가 '로그아웃됨(로그인 필요)'",
              st["login"]["state"] == "none" and "로그아웃" in (st["login"].get("detail") or ""),
              str(st["login"]))
        check("웹 로그아웃: 로그 창에 기록", any("로그아웃" in l for l in web_ui.log_payload(0)["lines"]), "")
        check("웹 화면: [카카오 로그아웃] 버튼",
              'id="btnLogout"' in web_ui.PAGE_HTML and "카카오 로그아웃" in web_ui.PAGE_HTML, "")
        check("웹 API: /api/logout 라우트", '"/api/logout"' in (ROOT / "web_ui.py").read_text(encoding="utf-8"), "")

        # 4) HTTP 로 실제로 눌렀을 때
        make_session()
        web_ui.reset_state()
        httpd = web_ui.make_server("127.0.0.1", 0)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/logout",
                                         data=b"{}", headers={"Content-Type": "application/json"},
                                         method="POST")
            out = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
            check("웹 /api/logout: 200 · 세션 삭제",
                  out.get("ok") is True and not prof.exists() and not statef.exists(), str(out)[:160])
            check("웹 /api/logout: 상태 'none' 반환",
                  (out.get("state") or {}).get("state") == "none", str(out)[:160])
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)
    finally:
        main.PROFILE_DIR = saved["PROFILE_DIR"]
        main.STATE_FILE = saved["STATE_FILE"]
        sys.stdin = saved["stdin"]
        shutil.rmtree(prof, ignore_errors=True)
        statef.unlink(missing_ok=True)
        main.login_state = saved["login_state"]
        web_ui.reset_state()


# ------------- 10부: 담당자 열 목록 — 노션 주소 정규화 · 폴백 · [↻ 새로고침]
NOTION_URL = ("https://www.notion.so/sinyeob/41a27dbea66981939138de51fa8dfb1c"
              "?v=1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f&pvs=4")
NOTION_UUID = "41a27dbe-a669-8193-9138-de51fa8dfb1c"
NOTION_SCHEMA_TEN = {
    "상품명·브랜드": {"type": "title"},
    "메인.엽": {"type": "rich_text"},
    "1팀.엽2(2200)": {"type": "rich_text"},
    "서브.동": {"type": "rich_text"},
    "서브.준": {"type": "rich_text"},
    "옵션": {"type": "rich_text"},
    "메커니즘": {"type": "rich_text"},
    "마감일·모집현황": {"type": "rich_text"},
    "링크": {"type": "url"},
    "등록일": {"type": "created_time"},
}


class StrictNotionStub:
    """노션 실제 API 처럼 동작하는 가짜 서버 — DB ID 형식이 틀리면 400('Invalid request URL.').

    실제 노션은 `/databases/https://www.notion.so/....` 같은 주소를 받으면 400 을 돌려준다.
    이 스텁으로 '주소를 그대로 저장했을 때 왜 열 목록이 안 나오는지'를 재현/검증한다.
    """

    ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32}")

    def __init__(self, schema=None, deny=(), databases=None):
        self.schema = schema if schema is not None else NOTION_SCHEMA_TEN
        self.deny = set(deny)     # 이 DB ID 는 403(통합이 연결되지 않은 DB 흉내)
        self.databases = databases   # None → DB 목록 검색은 404(통합이 아무 DB 에도 없음)
        self.calls = []
        self.searches = []
        self.schemas = {str(d["id"]).replace("-", ""): (d.get("properties") or {})
                        for d in (databases or [])}

    def __call__(self, method, path, payload, token, timeout=None):
        self.calls.append((method, path))
        if method == "POST" and path == "/search":
            if self.databases is None:
                return 404, {"message": "not found"}, '{"message":"not found"}'
            self.searches.append(payload or {})
            results = [{
                "object": "database",
                "id": d["id"],
                "title": [{"type": "text", "text": {"content": d["title"]}, "plain_text": d["title"]}],
                "properties": d.get("properties") or {},
            } for d in self.databases]
            return 200, {"object": "list", "results": results, "has_more": False, "next_cursor": None}, None
        if method == "GET" and path.startswith("/databases/"):
            db = path[len("/databases/"):]
            if not self.ID_RE.fullmatch(db):
                return 400, {"message": "Invalid request URL."}, '{"message":"Invalid request URL."}'
            if db.replace("-", "") in {d.replace("-", "") for d in self.deny}:
                return 403, {"message": "Unauthorized"}, '{"message":"Unauthorized"}'
            schema = self.schemas.get(db.replace("-", ""), self.schema)
            return 200, {"object": "database", "properties": schema}, None
        return 404, {"message": "not found"}, None


def test_column_discovery():
    print("[10/12] 담당자 열 목록 — 주소 정규화 · 토큰만으로 DB 자동 찾기 · 폴백 · 새로고침 검증")
    import web_ui

    read = lambda: (ROOT / "web_ui.py").read_text(encoding="utf-8")

    # 1) DB ID 정규화 — 주소·32자·UUID 를 모두 8-4-4-4-12 로
    check("DB ID: 페이지 주소 → ID 추출", main.normalize_notion_db_id(NOTION_URL) == NOTION_UUID,
          main.normalize_notion_db_id(NOTION_URL))
    check("DB ID: 하이픈 없는 32자 → UUID",
          main.normalize_notion_db_id("41a27dbea66981939138de51fa8dfb1c") == NOTION_UUID)
    check("DB ID: 이미 정리된 UUID 그대로", main.normalize_notion_db_id(NOTION_UUID) == NOTION_UUID)
    check("DB ID: 앞뒤 공백·따옴표 정리", main.normalize_notion_db_id('  "' + NOTION_URL + '"  ') == NOTION_UUID)
    check("DB ID: 앵커(#) 붙은 주소도 처리",
          main.normalize_notion_db_id("https://www.notion.so/x/41a27dbea66981939138de51fa8dfb1c#abc") == NOTION_UUID)
    check("DB ID: ID 가 없는 값은 그대로", main.normalize_notion_db_id("db-once") == "db-once")
    check("DB ID: 빈 값", main.normalize_notion_db_id("  ") == "" and main.normalize_notion_db_id(None) == "")

    # 2) 원인 재현 — 주소를 그대로 API 경로에 넣으면 노션은 400 을 준다(예전 증상: 열 0개 → 저장된 1개만 노출)
    strict = StrictNotionStub()
    status, _data, _err = strict("GET", f"/databases/{NOTION_URL}", None, "ntn_x")
    check("원인 재현: DB ID 자리에 주소를 넣으면 400", status == 400, str(status))
    check("원인 재현: 정리된 ID 로는 같은 스텁이 200", strict("GET", f"/databases/{NOTION_UUID}", None, "ntn_x")[0] == 200)

    # 3) notion_list_columns() 는 주소를 받아도 스스로 정리해 읽는다
    main.NOTION_LAST_ERROR.update({"db_id": None, "message": None})
    cols = main.notion_list_columns(NOTION_URL, "ntn_x", transport=StrictNotionStub())
    check("주소 붙여넣기: notion_list_columns 가 열 4개를 읽음",
          cols is not None and len(cols) == 4 and cols[0] == "메인.엽", str(cols))
    check("주소 붙여넣기: 읽었으면 실패 사유 없음", main.notion_last_error() is None, str(main.notion_last_error()))

    # 4) 웹 ⚙설정: 주소를 넣어도 ID 만 저장하고, 열 목록을 즉시 반영
    tmp = ROOT / "logs" / "_cols_secret.json"
    tmp.parent.mkdir(exist_ok=True)
    tmp.write_text(json.dumps({"enabled": True, "token": "ntn_old", "db_id": "db-old",
                               "column": "메인.엽"}), encoding="utf-8")
    saved = {"http": main._notion_http, "secret": main.NOTION_SECRET_FILE,
             "columns": main.notion_list_columns}
    main._notion_http = StrictNotionStub()
    main.NOTION_SECRET_FILE = str(tmp)
    web_ui.reset_state()
    try:
        r = web_ui.save_notion_settings("ntn_url_token", NOTION_URL, "")
        data = json.loads(tmp.read_text(encoding="utf-8"))
        check("웹 ⚙설정: 주소에서 DB ID 만 저장", r["ok"] and data["db_id"] == NOTION_UUID, str(data.get("db_id")))
        check("웹 ⚙설정: 토큰 저장", data["token"] == "ntn_url_token")
        check("웹 ⚙설정: 응답에 정규화된 DB ID 안내", r.get("normalized_db") == NOTION_UUID, str(r.get("normalized_db")))
        check("웹 ⚙설정: 담당자 열 4개를 즉시 반환", len(r.get("columns") or []) == 4, str(r.get("columns")))
        check("웹 ⚙설정: 실패 사유 없음", r.get("columns_error") is None, str(r.get("columns_error")))
        check("웹 ⚙설정: STATE['columns'] 즉시 갱신(새로고침 없이 바로 보임)",
              web_ui.STATE["columns"] == r["columns"], str(web_ui.STATE["columns"]))
        check("웹 ⚙설정: 파일에 _columns 저장", data.get("_columns") == r["columns"], str(data.get("_columns")))
        st = web_ui.api_status()
        check("웹 상태: 열 4개 · 오류 없음",
              len(st["notion"]["columns"]) == 4 and not st["notion"].get("columns_error"), str(st["notion"])[:160])
        check("웹 상태: 응답에 토큰 없음", "ntn_url_token" not in json.dumps(st, ensure_ascii=False))

        # 5) 다른 DB 인데 못 읽으면: 이전 DB 의 열 목록을 보여 주지 않고(1개 오해 방지) 사유를 남긴다
        locked = "41a27dbe-ffff-8193-9138-de51fa8dfb1c"
        main._notion_http = StrictNotionStub(deny=[locked])
        r2 = web_ui.save_notion_settings("ntn_url_token", locked, "")
        data2 = json.loads(tmp.read_text(encoding="utf-8"))
        check("웹 ⚙설정(실패): 저장은 하되 열 목록은 비움", r2["ok"] and r2["columns"] == [], str(r2.get("columns")))
        check("웹 ⚙설정(실패): 사유를 응답에 담는다",
              bool(r2.get("columns_error")) and "403" in (r2.get("columns_error") or ""), str(r2.get("columns_error")))
        check("웹 ⚙설정(실패): 다른 DB 의 _columns 는 버린다", "_columns" not in data2, str(data2.keys()))
        check("웹 ⚙설정(실패): STATE['columns'] = []", web_ui.STATE["columns"] == [], str(web_ui.STATE["columns"]))
        st2 = web_ui.api_status()
        check("웹 상태(실패): columns_error 로 이유를 알 수 있다", bool(st2["notion"].get("columns_error")), str(st2["notion"])[:160])

        # 6) 실시간 읽기가 안 돼도 저장해 둔 _columns 로 고를 수 있다(폴백)
        data2["_columns"] = ["메인.엽", "서브.동", "서브.준"]
        tmp.write_text(json.dumps(data2, ensure_ascii=False), encoding="utf-8")
        main._notion_http = StrictNotionStub(deny=[locked])
        web_ui.reset_state()
        cols_fb = web_ui.notion_columns()
        check("폴백: 저장해 둔 열 목록을 쓴다", cols_fb == ["메인.엽", "서브.동", "서브.준"], str(cols_fb))
        check("폴백: 화면에 '저장해 둔 열' 안내", "저장해 둔 열" in (web_ui.STATE.get("columns_error") or ""),
              str(web_ui.STATE.get("columns_error")))

        # 7) [↻ 새로고침]: 권한이 풀린 뒤 다시 읽으면 5개가 돌아오고 오류가 사라진다
        main._notion_http = StrictNotionStub()
        rr = web_ui.reload_columns()
        check("새로고침: 4개를 다시 읽음", rr["ok"] and len(rr["columns"]) == 4, str(rr)[:160])
        check("새로고침: 오류 표시가 지워짐", web_ui.STATE.get("columns_error") is None,
              str(web_ui.STATE.get("columns_error")))
        check("새로고침: 로그에 남김", any("담당자 열 새로고침" in l for l in web_ui.log_payload(0)["lines"]), "")
        main._notion_http = StrictNotionStub(deny=[locked])
        rr2 = web_ui.reload_columns()
        check("새로고침(실패): ok=false · 사유 반환", rr2["ok"] is False and bool(rr2["error"]), str(rr2)[:160])

        # 8) HTTP 로 실제 [↻ 새로고침] 을 눌렀을 때
        main._notion_http = StrictNotionStub()
        web_ui.reset_state()
        httpd = web_ui.make_server("127.0.0.1", 0)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/notion-columns",
                                         data=b"{}", headers={"Content-Type": "application/json"},
                                         method="POST")
            out = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
            check("웹 /api/notion-columns: 200 · 열 4개",
                  out.get("ok") is True and len(out.get("columns") or []) == 4, str(out)[:160])
            check("웹 /api/notion-columns: 토큰 미노출", "ntn_" not in json.dumps(out, ensure_ascii=False))
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)

        # 9) 화면에 담당자 열 옆 [↻ 새로고침] 버튼과 오류 표시
        html = web_ui.PAGE_HTML
        check("웹 화면: [↻ 새로고침] 버튼", 'id="btnReloadCols"' in html and "새로고침" in html)
        check("웹 화면: 담당자 열 select 바로 옆에 버튼",
              html.index('id="btnReloadCols"') > html.index('id="column"')
              and html.index('id="btnReloadCols"') - html.index('id="column"') < 400, "")
        check("웹 화면: columns_error 를 표시", "columns_error" in html and "columnBanner" in html)
        check("웹 화면: 열 개수 힌트", "담당자 열 ${cols.length}개" in html, "")
        check("웹 API: /api/notion-columns 라우트", '"/api/notion-columns"' in read(), "")

        # 10) CLI 설정 마법사도 주소를 받아 ID 만 저장한다
        tmp.unlink(missing_ok=True)
        main._notion_http = StrictNotionStub()
        buf = io.StringIO()
        with use_input(["ntn_setup_url", NOTION_URL, ""]) as fake:
            with contextlib.redirect_stdout(buf):
                cfg = main.setup_notion(path=str(tmp))
        saved_file = json.loads(tmp.read_text(encoding="utf-8")) if tmp.is_file() else {}
        printed = buf.getvalue()
        check("CLI 설정: 주소 → 정규화된 DB ID 저장", saved_file.get("db_id") == NOTION_UUID,
              str(saved_file.get("db_id")))
        check("CLI 설정: 주소에서 추출했다고 알림", "주소에서 DB ID 만 추출" in printed, printed[:200])
        check("CLI 설정: 열 번호를 물어 고른다", "참가자 열" in printed and bool(cfg))
        check("CLI 설정: 주소로도 열 4개를 읽음", len(saved_file.get("_columns") or []) == 4,
              str(saved_file.get("_columns")))
        check("CLI 설정: 파일에 URL 이 남지 않음", "notion.so" not in json.dumps(saved_file, ensure_ascii=False))

        # 11) 설정 파일에 주소가 직접 적혀 있어도 읽을 때 정리된다
        tmp.write_text(json.dumps({"enabled": True, "token": "ntn_manual", "db_id": NOTION_URL,
                                   "column": "서브.준"}), encoding="utf-8")
        cfg_manual = main.load_notion_config()
        check("수동 편집: 파일의 주소도 ID 로 정리해 읽음",
              cfg_manual and cfg_manual["db_id"] == NOTION_UUID, str((cfg_manual or {}).get("db_id")))
        check("수동 편집: 그 설정으로 열 목록을 읽음",
              len(main.notion_list_columns(cfg_manual["db_id"], cfg_manual["token"],
                                           transport=StrictNotionStub()) or []) == 4)

        # 12) 토큰만 있으면 DB 를 자동으로 찾는다(POST /v1/search) — DB ID 입력은 선택
        db_a = {"id": NOTION_UUID, "title": "🎁 카카오 체험단 응모 현황표",
                "properties": NOTION_SCHEMA_TEN}
        db_b = {"id": "51b27dbe-a669-8193-9138-de51fa8dfb1c", "title": "서브 기록장",
                "properties": {"이름": {"type": "title"}, "서브.동": {"type": "rich_text"}}}
        db_c = {"id": "61c27dbe-a669-8193-9138-de51fa8dfb1c", "title": "할 일",
                "properties": {"이름": {"type": "title"}, "상태": {"type": "select"}}}
        sdb = StrictNotionStub(databases=[db_a, db_b, db_c])
        main._notion_http = sdb
        found = main.notion_discover_databases("ntn_auto")
        check("자동 찾기: 연결된 DB 3개를 찾음", found is not None and len(found) == 3,
              str(found)[:160])
        check("자동 찾기: DB 이름·담당자 열까지 함께 읽음",
              [len(d["columns"]) for d in (found or [])] == [4, 1, 0]
              and (found or [{}])[0]["title"].startswith("🎁"), str([(d["title"], len(d["columns"])) for d in (found or [])]))
        check("자동 찾기: 검색은 database 필터 · page_size 20",
              (sdb.searches or [{}])[0].get("filter") == {"property": "object", "value": "database"}
              and (sdb.searches or [{}])[0].get("page_size") == 20, str(sdb.searches[:1]))
        pick = main.pick_notion_database(found)
        check("자동 선택: 담당자 열·'체험단' 이름 기준으로 현황표 DB", pick and pick["id"] == db_a["id"],
              str((pick or {}).get("title")))
        check("자동 선택: 저장된 DB 가 후보에 있으면 그 DB 유지",
              (main.pick_notion_database(found, prefer_id=db_b["id"]) or {}).get("id") == db_b["id"])
        check("자동 선택: 담당자 열이 있는 DB 가 없으면 None",
              main.pick_notion_database([dict(db_c)]) is None)
        check("자동 찾기: 검색 실패는 스키마 오류와 섞이지 않는다",
              main.notion_discover_databases("ntn_x", transport=StrictNotionStub()) is None
              and "DB 목록 조회 실패" in (main.notion_last_search_error() or ""),
              str(main.notion_last_search_error()))

        # 12-2) DB 목록을 아직 못 읽은 설정에서도 지금 쓰는 DB 는 [DB] 칸에 보인다
        tmp.write_text(json.dumps({"enabled": True, "token": "ntn_bare", "db_id": NOTION_UUID,
                                   "column": "메인.엽"}, ensure_ascii=False), encoding="utf-8")
        web_ui.reset_state()
        st_bare = web_ui.api_status()["notion"]
        check("웹 상태: DB 목록이 없으면 저장된 DB 1개로 표시",
              len(st_bare["databases"]) == 1 and st_bare["databases"][0]["current"] is True,
              str(st_bare["databases"]))
        check("웹 상태: 저장된 DB 표시에 토큰 없음", "ntn_bare" not in json.dumps(st_bare, ensure_ascii=False),
              str(st_bare["databases"]))

        # 13) 웹: 토큰만 넣고 저장 → DB 자동 선택 · 목록 기억
        tmp.write_text(json.dumps({"enabled": True, "token": "ntn_old", "db_id": "db-old",
                                   "column": "메인.엽"}), encoding="utf-8")
        main._notion_http = StrictNotionStub(databases=[db_a, db_b, db_c])
        web_ui.reset_state()
        r = web_ui.save_notion_settings("ntn_token_only", "")
        data = json.loads(tmp.read_text(encoding="utf-8"))
        check("웹 토큰만: DB ID 없이 저장 성공", r["ok"] is True and data["db_id"] == db_a["id"],
              str(data.get("db_id")))
        check("웹 토큰만: 고른 DB 를 안내", "체험단" in (r.get("note") or ""), str(r.get("note")))
        check("웹 토큰만: 담당자 열 4개를 바로 채움", len(r.get("columns") or []) == 4, str(r.get("columns")))
        check("웹 토큰만: 연결된 DB 목록을 기억", len(data.get("_databases") or []) == 3,
              str(len(data.get("_databases") or [])))
        check("웹 토큰만: DB ID 칸 비워도 안내 문구 없음", not r.get("columns_error"), str(r.get("columns_error")))
        st = web_ui.api_status()
        dbs = st["notion"]["databases"]
        check("웹 상태: DB 목록 3개 · 현재 DB 표시",
              len(dbs) == 3 and [d["current"] for d in dbs] == [True, False, False], str(dbs))
        check("웹 상태: DB 이름 전달", dbs[0]["title"].startswith("🎁") and dbs[2]["columns"] == 0, str(dbs))
        check("웹 상태: 토큰 미노출", "ntn_token_only" not in json.dumps(st, ensure_ascii=False))

        # 14) 웹: [DB] 를 바꾸면 그 DB 기준으로 담당자 열을 다시 읽는다
        r_sw = web_ui.switch_notion_db(db_b["id"])
        data = json.loads(tmp.read_text(encoding="utf-8"))
        check("DB 바꾸기: db_id·열 목록 교체", r_sw["ok"] and data["db_id"] == db_b["id"]
              and r_sw["columns"] == ["서브.동"], str(r_sw)[:160])
        check("DB 바꾸기: 저장된 열이 새 DB 에 없으면 첫 열로", data["column"] == "서브.동",
              str(data.get("column")))
        check("DB 바꾸기: api_status 현재 DB 표시",
              [d["current"] for d in web_ui.api_status()["notion"]["databases"]] == [False, True, False])
        r_sw2 = web_ui.switch_notion_db(db_a["id"])
        check("DB 바꾸기: 되돌리기", r_sw2["ok"] and len(r_sw2["columns"]) == 4, str(r_sw2)[:120])

        # 15) 웹: DB 를 못 찾으면 안내만 하고, 토큰은 저장해 두어 [↻ DB 목록]으로 이어 간다
        tmp.write_text(json.dumps({"enabled": False}), encoding="utf-8")
        main._notion_http = StrictNotionStub(databases=None)
        r_no = web_ui.save_notion_settings("ntn_lonely", "")
        data = json.loads(tmp.read_text(encoding="utf-8"))
        check("웹 못 찾음: 저장 실패로 안내", r_no["ok"] is False
              and "API 연결(Integration)" in (r_no.get("error") or ""), str(r_no.get("error")))
        check("웹 못 찾음: 토큰은 저장해 둠", r_no.get("token_saved") is True and data["token"] == "ntn_lonely"
              and data["db_id"] == "", str(data))
        check("웹 못 찾음: 상태에 안내", "DB ID 가 아직 비어 있습니다" in (web_ui.api_status()["notion"]["columns_error"] or ""),
              str(web_ui.api_status()["notion"]["columns_error"]))
        web_ui.reset_state()
        pv_no_db = web_ui.preview("notion", 5)
        check("웹 못 찾음: 미리보기는 [↻ DB 목록] 안내", pv_no_db["ok"] is False
              and "DB 목록" in (pv_no_db.get("error") or ""), str(pv_no_db.get("error")))
        main._notion_http = StrictNotionStub(databases=[db_a])
        rr = web_ui.refresh_databases()
        data = json.loads(tmp.read_text(encoding="utf-8"))
        check("DB 목록 새로고침: 찾은 DB 로 자동 이어감", rr["ok"] and data["db_id"] == db_a["id"]
              and len(data.get("_columns") or []) == 4, str(rr)[:160])
        check("DB 목록 새로고침: 안내 문구", "골라 저장" in (rr.get("note") or ""), str(rr.get("note")))
        main._notion_http = StrictNotionStub(databases=[])
        rr_none = web_ui.refresh_databases()
        check("DB 목록 새로고침(0개): ⋯ → 연결 안내", rr_none["ok"] is False
              and "⋯ → 연결" in (rr_none.get("error") or ""), str(rr_none.get("error")))
        main._notion_http = StrictNotionStub(databases=None)
        rr_err = web_ui.refresh_databases()
        check("DB 목록 새로고침(검색 실패): 사유 전달", rr_err["ok"] is False
              and "DB 목록 조회 실패" in (rr_err.get("error") or ""), str(rr_err.get("error")))

        # 16) 화면: DB ID 는 선택 입력 · [DB] 선택과 [↻ DB 목록] 제공
        html = web_ui.PAGE_HTML
        check("웹 화면: DB ID '안 넣어도 됨' 안내", "안 넣어도 됨" in html and "토큰으로 자동 찾기" in html)
        check("웹 화면: [DB] 선택 · [↻ DB 목록] 버튼", 'id="dbSelect"' in html and 'id="btnReloadDbs"' in html)
        check("웹 화면: DB 목록을 그린다", "n.databases" in html and "d.current" in html)
        py = read()
        check("웹 API: /api/notion-db · /api/notion-dbs 라우트",
              '"/api/notion-db"' in py and '"/api/notion-dbs"' in py)
        js_ids = set(re.findall(r'\$\("([A-Za-z0-9_]+)"\)', html.split("<script>")[1]))
        html_ids = set(re.findall(r'id="([A-Za-z0-9_]+)"', html))
        check("웹 화면: 스크립트가 쓰는 id 가 모두 화면에 있다", not (js_ids - html_ids), str(sorted(js_ids - html_ids)))
        main_src = Path(main.__file__).read_text(encoding="utf-8")
        check("main: /v1/search 로 DB 를 찾는 함수", "def notion_discover_databases" in main_src
              and 'tr("POST", "/search"' in main_src)

        # 17) CLI: DB ID Enter → 토큰으로 자동 찾아 저장
        tmp.unlink(missing_ok=True)
        main._notion_http = StrictNotionStub(databases=[db_a, db_b, db_c])
        buf = io.StringIO()
        with use_input(["ntn_cli_auto", "", ""]):
            with contextlib.redirect_stdout(buf):
                cfg_auto = main.setup_notion(path=str(tmp))
        saved_auto = json.loads(tmp.read_text(encoding="utf-8")) if tmp.is_file() else {}
        printed = buf.getvalue()
        check("CLI 자동: DB ID 없이 저장", saved_auto.get("db_id") == db_a["id"], str(saved_auto.get("db_id")))
        check("CLI 자동: 어떤 DB 를 골랐는지 알림", "[자동 선택]" in printed, printed[-300:])
        check("CLI 자동: 여러 개라 기본 선택했다고 알림", "기본으로 골랐습니다" in printed, printed[-200:])
        check("CLI 자동: 찾은 DB 목록을 기억", len(saved_auto.get("_databases") or []) == 3)
        check("CLI 자동: 열 목록도 저장", len(saved_auto.get("_columns") or []) == 4)
        check("CLI 자동: 설정 반환", bool(cfg_auto) and cfg_auto["db_id"] == db_a["id"])

        # 18) CLI: DB 를 못 찾고 저장된 DB 도 없으면 안내하고 멈춘다
        tmp.unlink(missing_ok=True)
        main._notion_http = StrictNotionStub(databases=None)
        saved_default = main.NOTION_DEFAULT_DB_ID
        main.NOTION_DEFAULT_DB_ID = ""
        try:
            buf = io.StringIO()
            with use_input(["ntn_cli_none", ""]):
                with contextlib.redirect_stdout(buf):
                    cfg_none = main.setup_notion(path=str(tmp))
            check("CLI 못 찾음: 안내하고 멈춤", cfg_none is None and not tmp.exists()
                  and "API 연결(Integration)을 DB 페이지에 추가" in buf.getvalue(), buf.getvalue()[-200:])
        finally:
            main.NOTION_DEFAULT_DB_ID = saved_default
    finally:
        main._notion_http = saved["http"]
        main.NOTION_SECRET_FILE = saved["secret"]
        main.notion_list_columns = saved["columns"]
        main.NOTION_LAST_ERROR.update({"db_id": None, "message": None})
        main.NOTION_LAST_SEARCH_ERROR.update({"message": None})
        tmp.unlink(missing_ok=True)
        web_ui.reset_state()


# ------------- 11부: 계정 전환 캐시 — 세션 파일 변경 · 로그아웃 · 계정 닉네임 · 이미 신청 건수
def test_session_cache():
    print("[11/12] 계정 전환 캐시 — 세션 파일 변경 시 새 쿠키 · 로그아웃 초기화 · 계정 닉네임 · 이미 신청 건수 검증")
    import web_ui

    statef = ROOT / "logs" / "_session_state.json"
    prof = ROOT / "logs" / "_session_profile"
    saved = {k: getattr(main, k) for k in ("STATE_FILE", "PROFILE_DIR", "fetch_json", "login_state",
                                          "collect_campaigns", "collect_applied_ids")}

    def write_state(token, pad=0, domain=None):
        """카카오 세션 백업(kakao_state.json)을 다른 계정 쿠키로 다시 쓴다."""
        dom = domain or ".kakao.com"
        cookies = [
            {"name": "_kau", "value": token, "domain": dom, "path": "/"},
            {"name": "gift_session", "value": token, "domain": domain or "gift.kakao.com", "path": "/"},
        ]
        if pad:
            cookies.append({"name": "pad", "value": "x" * pad, "domain": ".kakao.com", "path": "/"})
        statef.write_text(json.dumps({"cookies": cookies}, ensure_ascii=False), encoding="utf-8")

    def jar_of(opener):
        for h in (getattr(opener, "handlers", []) or []):
            jar = getattr(h, "cookiejar", None)
            if jar is not None:
                return {c.name: c.value for c in jar}
        return {}

    main.STATE_FILE = str(statef)
    main.PROFILE_DIR = str(prof)
    main.reset_session_cache()
    try:
        # 1) 첫 계정 쿠키로 요청기 생성 — 같은 파일이면 그대로 재사용
        write_state("account-A")
        op1 = main._session_opener()
        check("세션 요청기: 저장된 세션으로 생성", op1 is not None and jar_of(op1).get("_kau") == "account-A",
              str(jar_of(op1) if op1 else None))
        check("세션 요청기: 세션 파일이 그대로면 재사용", main._session_opener() is op1)

        # 2) 로그아웃 후 다른 계정 로그인(kakao_state.json 다시 씀) → 새 쿠키여야 한다
        write_state("account-B", pad=7)
        op2 = main._session_opener()
        check("세션 요청기: 세션 파일이 바뀌면 새로 생성", op2 is not op1)
        check("세션 요청기: 새 계정 쿠키를 쓴다(이전 계정 이력이 남지 않음)",
              jar_of(op2).get("_kau") == "account-B", str(jar_of(op2)))

        # 3) 세션 파일 삭제(로그아웃) → 세션 없음
        statef.unlink(missing_ok=True)
        check("세션 요청기: 세션 파일이 지워지면 None", main._session_opener() is None)

        # 4) 파일 재생성 → 새 쿠키
        write_state("account-C")
        op3 = main._session_opener()
        check("세션 요청기: 세션 파일이 다시 생기면 새 쿠키",
              op3 is not None and op3 is not op2 and jar_of(op3).get("_kau") == "account-C",
              str(jar_of(op3) if op3 else None))

        # 5) logout() 이 캐시를 즉시 비운다(파일 삭제 + 요청기 초기화)
        res = main.logout(quiet=True)
        check("로그아웃: 요청기·계정 캐시 즉시 초기화",
              res["ok"] and main._SESSION_OPENER is None and main._SESSION_OPENER_SIG is None
              and main._session_opener() is None, str(res)[:160])

        # 5-2) 실제 버그 재현: 오래 사는 프로세스에서 계정을 바꿔도 이전 계정 이력이 남지 않는다
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        seen_cookies = []

        class HistoryHandler(BaseHTTPRequestHandler):
            """신청 이력 API 흉내 — 쿠키(_kau)에 따라 다른 계정의 이력을 돌려준다."""

            def log_message(self, *a):
                return

            def do_GET(self):
                raw = self.headers.get("Cookie") or ""
                token = ""
                for part in raw.split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == "_kau":
                        token = v
                seen_cookies.append(token)
                if token == "account-A":
                    body = {"contents": [{"campaignId": "A-1"}, {"campaignId": "A-2"}], "isLast": True}
                    code = 200
                elif token == "account-B":
                    body = {"contents": [{"campaignId": "B-1"}], "isLast": True}
                    code = 200
                else:
                    body, code = {"errorMessage": "unauthorized"}, 401
                data = json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), HistoryHandler)
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        saved_history = main.HISTORY_API
        try:
            main.HISTORY_API = (f"http://127.0.0.1:{httpd.server_address[1]}"
                                "/a/partner-trial/api/v1/trial-campaigns/apply")
            write_state("account-A", domain="127.0.0.1")
            main.reset_session_cache()
            ids_a = main.collect_applied_ids()
            write_state("account-B", pad=7, domain="127.0.0.1")   # 로그아웃 → 다른 계정 로그인
            ids_b = main.collect_applied_ids()
            check("실제 버그 재현: 계정 전환 뒤 이전 계정 이력이 남지 않는다",
                  ids_a == {"A-1", "A-2"} and ids_b == {"B-1"}, f"{ids_a} → {ids_b} ({seen_cookies})")
            statef.unlink(missing_ok=True)            # 로그아웃
            ids_none = main.collect_applied_ids()
            check("계정 전환: 로그아웃 상태에서는 이력 없음(401 → None)",
                  ids_none is None and seen_cookies[-1] == "", f"{ids_none} / {seen_cookies}")
        finally:
            main.HISTORY_API = saved_history
            httpd.shutdown()
            httpd.server_close()
            th.join(timeout=3)

        # 6) 계정 정보(닉네임) — gift 헤더 API 응답을 가짜로
        current = {"nick": "서브.연"}
        calls = []

        def fake_fetch(url, timeout=20, use_session=False):
            calls.append((url, use_session))
            if "/a/v1/header" in url:
                if current["nick"] is None:
                    return 200, {}, None
                return 200, {"user": {"nickname": current["nick"], "id": 123456789}, "cartInfo": {"count": 0}}, None
            if "/trial-campaigns/apply" in url:
                return 200, {"contents": [], "isLast": True}, None
            return 200, {}, None

        main.fetch_json = fake_fetch
        write_state("account-A")
        main.reset_session_cache()
        acc = main.fetch_account()
        check("계정 정보: /a/v1/header 로 닉네임을 읽는다",
              bool(acc) and acc.get("nickname") == "서브.연" and acc.get("source") == main.USER_INFO_API, str(acc))
        check("계정 정보: 식별자는 가려서(ID …6789) 보여 준다",
              bool(acc) and acc.get("masked_key") == "ID …6789", str(acc))
        n_before = len(calls)
        again = main.fetch_account()
        check("계정 정보: 같은 세션이면 다시 부르지 않는다(60초 캐시)", len(calls) == n_before and again == acc,
              f"calls={len(calls) - n_before}")

        current["nick"] = "서브.오"
        write_state("account-B", pad=7)      # 다른 계정으로 로그인한 상황
        acc2 = main.fetch_account()
        check("계정 정보: 계정을 바꾸면 새 닉네임으로 다시 읽는다",
              bool(acc2) and acc2.get("nickname") == "서브.오" and len(calls) == n_before + 1, str(acc2))

        # 7) 로그인 상태에 닉네임이 함께 온다
        st = main.login_state()
        check("로그인 상태: 닉네임을 함께 돌려준다",
              st.get("state") == "ok" and st.get("nickname") == "서브.오" and "서브.오" in (st.get("detail") or ""),
              str(st))
        check("로그인 상태: 계정 식별자(ID …6789)도 함께 보여 준다",
              "ID …6789" in (st.get("detail") or ""), str(st.get("detail")))

        # 8) 닉네임을 못 읽어도 로그인 상태는 유지(오탐으로 '로그아웃' 표시하지 않음)
        current["nick"] = None
        main.reset_session_cache()
        st2 = main.login_state()
        check("로그인 상태: 닉네임을 못 읽어도 '로그인됨' 유지",
              st2.get("state") == "ok" and st2.get("nickname") is None, str(st2))

        # 9) 웹 조종판: 상태에 닉네임이 실리고 배지에 '로그인됨 · 닉네임'
        current["nick"] = "서브.오"
        main.reset_session_cache()
        web_ui.reset_state()
        web_ui.STATE["login"] = main.login_state()
        payload = web_ui.api_status()["login"]
        check("웹 상태: 로그인 계정 닉네임을 화면에 전달", payload.get("nickname") == "서브.오", str(payload))
        check("웹 화면: 배지에 '로그인됨 · 닉네임'",
              "로그인됨 · " in web_ui.PAGE_HTML and "login.nickname" in web_ui.PAGE_HTML)
        web_ui.reset_state()

        # 10) 미리보기: 이미 신청 N건을 요약에 명시
        main.login_state = saved["login_state"]
        items = [build_item("수플린 설향딸기 800g", [(93, "식품", 203, "과일·견과")]),
                 build_item("스누피 3단 전기요", [(97, "디지털·가전", 163, "계절가전")]),
                 build_item("무드웨어 블러 립스틱", [(95, "뷰티", 181, "메이크업")])]
        main.collect_campaigns = lambda max_pages: list(items)
        main.collect_applied_ids = lambda: {"id-수플린 설향", "id-지난상품"}
        r = web_ui.preview(mode="list", limit=10)
        check("미리보기: 이미 신청 건수를 따로 돌려준다",
              r.get("ok") and r.get("applied_count") == 2 and r.get("applied_in_list") == 1,
              str({k: r.get(k) for k in ("applied_count", "applied_in_list")}))
        check("미리보기: 요약에 '이미 신청 1건 포함'", "이미 신청 1건 포함" in (r.get("summary") or ""),
              str(r.get("summary")))
        check("미리보기: 이미 신청한 상품은 대상에서 빠진다",
              all(it["campaignId"] != "id-수플린 설향" for it in r.get("items") or []), str(r.get("items"))[:200])

        # 11) 로그인 전(이력 확인 불가)이면 그 사실을 숨기지 않는다
        main.collect_applied_ids = lambda: None
        r2 = web_ui.preview(mode="list", limit=10)
        check("미리보기: 이력 확인 불가를 요약에 표시",
              r2.get("applied_count") is None and "신청 이력 확인 불가" in (r2.get("summary") or ""),
              str(r2.get("summary")))
    finally:
        for k, v in saved.items():
            setattr(main, k, v)
        main.reset_session_cache()
        web_ui.reset_state()
        statef.unlink(missing_ok=True)
        shutil.rmtree(prof, ignore_errors=True)


# ------------- 12부: 카카오 닉네임 → 노션 담당자 열 자동 선택
def test_nickname_columns():
    print("[12/12] 카카오 닉네임 → 노션 담당자 열 자동 선택 (매칭 점수 · 계정 변경 · 수동 선택 보호)")
    import web_ui

    # 현황표 담당자 열의 실제 모양(접두사 + 핵심 이름 + 괄호 설명) — 타인 실명은 쓰지 않고 예시 이름만 쓴다
    cols = ["메인.엽", "1팀.엽2(2200)", "서브.동", "서브.문", "서브.연",
            "서브.오", "서브.완", "서브.주", "서브.준", "서브.택"]

    # 1) 열 이름 분해
    check("열 분해: '메인.엽' → ('메인', '엽')",
          main.notion_column_core("메인.엽") == ("메인", "엽"), str(main.notion_column_core("메인.엽")))
    check("열 분해: '1팀.엽2(2200)' → ('1팀', '엽2') — 괄호 설명은 이름이 아니다",
          main.notion_column_core("1팀.엽2(2200)") == ("1팀", "엽2"),
          str(main.notion_column_core("1팀.엽2(2200)")))
    check("열 분해: 구분자가 없으면 열 이름 전체가 핵심 이름",
          main.notion_column_core("담당자") == ("", "담당자"), str(main.notion_column_core("담당자")))

    # 2) 매칭 규칙 — 닉네임만 보고 고른다
    check("매칭: '이신엽' → '메인.엽'", main.match_notion_column_by_nickname("이신엽", cols) == "메인.엽",
          str(main.match_notion_column_detail("이신엽", cols)))
    check("매칭: '신엽' → '메인.엽'(성 제외 이름도 인정)",
          main.match_notion_column_by_nickname("신엽", cols) == "메인.엽")
    check("매칭: '오정원' → '서브.오'(성씨 일치)",
          main.match_notion_column_by_nickname("오정원", cols) == "서브.오",
          str(main.match_notion_column_detail("오정원", cols)))
    check("매칭: '엽2' → '1팀.엽2(2200)'(괄호 제외한 핵심 이름 일치)",
          main.match_notion_column_by_nickname("엽2", cols) == "1팀.엽2(2200)",
          str(main.match_notion_column_detail("엽2", cols)))
    check("매칭: '엽2'일 때 1글자 '엽'(메인.엽)보다 2글자 일치가 우선",
          main.match_notion_column_detail("엽2", cols).get("score", 0) > 900,
          str(main.match_notion_column_detail("엽2", cols).get("column")))
    check("매칭: 열 이름과 완전 일치(공백·대소문자 무시)를 가장 높게 본다",
          main.match_notion_column_detail(" 메인 . 엽 ", cols).get("column") == "메인.엽"
          and main.match_notion_column_detail(" 메인 . 엽 ", cols).get("score") >= 1000
          and "완전" in (main.match_notion_column_detail(" 메인 . 엽 ", cols).get("reason") or ""),
          str(main.match_notion_column_detail(" 메인 . 엽 ", cols)))

    cols_with_sua = cols + ["서브.수아"]
    check("매칭: '김수아' → '서브.수아'(2글자 핵심 이름이 닉네임에 포함)",
          main.match_notion_column_by_nickname("김수아", cols_with_sua) == "서브.수아",
          str(main.match_notion_column_detail("김수아", cols_with_sua)))
    check("매칭: '수아' → '서브.수아'", main.match_notion_column_by_nickname("수아", cols_with_sua) == "서브.수아")
    check("매칭: 2글자 일치가 1글자 일치보다 높은 점수",
          main.match_notion_column_detail("수아", cols_with_sua).get("score", 0)
          > main.match_notion_column_detail("오정원", cols).get("score", 0))
    check("매칭: '서브.수아'가 없으면 '김수아'는 None(엉뚱한 열을 고르지 않는다)",
          main.match_notion_column_by_nickname("김수아", cols) is None,
          str(main.match_notion_column_detail("김수아", cols)))

    # 3) 확신이 없을 때: None — 부르는 쪽이 저장된 열을 유지한다
    no_match = main.match_notion_column_detail("테스트계정", cols)
    check("매칭 없음 → None", main.match_notion_column_by_nickname("테스트계정", cols) is None)
    check("매칭 없음 사유를 남긴다", no_match.get("score") == 0 and "찾지 못했습니다" in (no_match.get("reason") or ""),
          str(no_match))
    check("닉네임 없음 → None", main.match_notion_column_by_nickname("", cols) is None
          and main.match_notion_column_by_nickname(None, cols) is None)
    check("열 목록 없음 → None", main.match_notion_column_by_nickname("이신엽", []) is None
          and main.match_notion_column_by_nickname("이신엽", None) is None)
    check("account_id 는 점수를 바꾸지 않고 결과에만 담긴다",
          main.match_notion_column_detail("오정원", cols, account_id="ID …1234").get("column") == "서브.오"
          and main.match_notion_column_detail("오정원", cols, account_id="ID …1234").get("account_id") == "ID …1234")

    # 4) 웹 조종판 — 로그인 계정이 바뀌면 열을 자동으로 고른다(파일은 임시 파일)
    tmp = ROOT / "logs" / "_auto_notion_secret.json"
    tmp.parent.mkdir(exist_ok=True)
    tmp.write_text(json.dumps({"enabled": True, "token": "ntn_auto_probe", "db_id": "db-auto",
                               "column": "메인.엽"}, ensure_ascii=False), encoding="utf-8")
    saved = {k: getattr(main, k) for k in ("NOTION_SECRET_FILE", "notion_list_columns", "login_state")}
    main.NOTION_SECRET_FILE = str(tmp)
    main.notion_list_columns = lambda db, tok, transport=None: list(cols)
    web_ui.reset_state()

    def login_of(nick, key="ID …1234"):
        return {"state": "ok", "detail": f"테스트 · {nick}", "nickname": nick,
                "account": {"nickname": nick, "masked_key": key}}

    def secret():
        return json.loads(tmp.read_text(encoding="utf-8"))

    try:
        web_ui.STATE["columns"] = list(cols)

        # 4-1) 로그인 확인 → 닉네임과 맞는 열을 고르고 기록에 남긴다
        web_ui.STATE["login"] = login_of("이신엽")
        first = web_ui.apply_auto_column(trigger="login")
        check("자동 선택: '이신엽' 로그인 → '메인.엽'",
              bool(first) and first.get("column") == "메인.엽", str(first))
        rec = secret().get("_auto_column") or {}
        check("자동 선택: 기록을 파일에 남긴다(자동=True · 점수)",
              rec.get("column") == "메인.엽" and rec.get("auto") is True and rec.get("score", 0) > 0, str(rec))
        check("자동 선택: 기록에 토큰이 들어가지 않는다", "ntn_auto_probe" not in json.dumps(rec, ensure_ascii=False))

        # 4-2) 같은 계정이면 다시 맞추지 않는다(손으로 고른 열 보호)
        again = web_ui.apply_auto_column(trigger="login")
        check("자동 선택: 같은 계정이면 다시 맞추지 않는다", bool(again) and again.get("changed") is False
              and bool(again.get("skipped")), str(again))

        # 4-3) 계정이 바뀌면 그 계정 닉네임의 열로 바뀐다
        web_ui.STATE["login"] = login_of("오정원", "ID …9999")
        second = web_ui.apply_auto_column(trigger="login")
        check("자동 선택: 계정 변경 → '서브.오' 열로 바뀜",
              bool(second) and second.get("changed") is True and secret()["column"] == "서브.오", str(second))
        check("자동 선택: 바뀐 열을 파일에 저장(토큰·DB ID 보존)",
              secret()["token"] == "ntn_auto_probe" and secret()["db_id"] == "db-auto", str(secret()))
        check("자동 선택: 로그 창에 무엇을 골랐는지 남긴다",
              any("[자동 선택]" in line and "서브.오" in line for line in web_ui.STATE["console"]),
              str(web_ui.STATE["console"][-3:]))

        # 4-4) 닉네임이 같아도 계정 ID 가 다르면 다른 계정으로 본다(배지의 ID …끝4자리)
        web_ui.STATE["login"] = login_of("오정원", "ID …1111")
        other_id = web_ui.apply_auto_column(trigger="login")
        check("자동 선택: 닉네임이 같아도 ID 가 다르면 새 계정으로 판단",
              bool(other_id) and other_id.get("skipped") is None, str(other_id))

        # 4-5) 사용자가 직접 고른 열은 같은 계정에서 덮어쓰지 않는다
        web_ui.STATE["login"] = login_of("오정원", "ID …9999")
        web_ui.save_column("서브.동")
        web_ui.mark_manual_column("서브.동")
        kept = web_ui.apply_auto_column(trigger="login")
        check("자동 선택: 손으로 고른 열('서브.동')을 덮어쓰지 않는다",
              bool(kept) and secret()["column"] == "서브.동", str(kept))
        check("자동 선택: 수동 선택은 auto=False 로 기록",
              (secret().get("_auto_column") or {}).get("auto") is False, str(secret().get("_auto_column")))
        check("자동 선택: 수동 선택 뒤 화면은 '자동 선택됨'을 표시하지 않는다",
              web_ui.api_status()["notion"]["auto"]["active"] is False,
              str(web_ui.api_status()["notion"]["auto"]))

        # 4-6) 열 목록을 새로 읽었을 때 — 저장된 열이 사라졌으면 다시 맞춘다
        data = secret()
        data["column"] = "서브.사라진열"
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        web_ui.STATE["columns"] = list(cols)
        reloaded = web_ui.apply_auto_column(columns=cols, trigger="columns")
        check("열 목록 새로고침: 사라진 열이면 닉네임으로 다시 고른다",
              bool(reloaded) and reloaded.get("changed") is True and secret()["column"] == "서브.오", str(reloaded))
        again_cols = web_ui.apply_auto_column(columns=cols, trigger="columns")
        check("열 목록 새로고침: 자동 선택값 그대로면 같은 열을 유지",
              bool(again_cols) and secret()["column"] == "서브.오", str(again_cols))
        web_ui.save_column("서브.준")
        web_ui.mark_manual_column("서브.준")
        manual_kept = web_ui.apply_auto_column(columns=cols, trigger="columns")
        check("열 목록 새로고침: 손으로 고른 열은 그대로 둔다",
              bool(manual_kept) and secret()["column"] == "서브.준" and bool(manual_kept.get("skipped")),
              str(manual_kept))

        # 4-7) 맞는 열이 없는 닉네임 — 기존 열 유지 + 안내 문구
        web_ui.STATE["login"] = login_of("테스트계정", "ID …5555")
        none_found = web_ui.apply_auto_column(trigger="login")
        check("자동 선택: 맞는 열이 없으면 기존 열을 유지",
              bool(none_found) and none_found.get("changed") is False and secret()["column"] == "서브.준",
              str(none_found))
        auto_info = web_ui.api_status()["notion"]["auto"]
        check("자동 선택: 맞는 열이 없었다는 안내를 화면에 전달",
              auto_info.get("active") is False and "찾지 못했습니다" in (auto_info.get("note") or ""),
              str(auto_info))

        # 4-8) api_status — 지금 열이 자동 선택값이면 active(화면이 '(자동 선택됨)' 표시)
        web_ui.STATE["login"] = login_of("오정원", "ID …9999")
        web_ui.save_column("서브.오")
        data = secret()
        data["_auto_column"] = {"account": "오정원|ID …9999", "nickname": "오정원", "column": "서브.오",
                                "auto": True, "score": 480, "at": "2026-11-24 10:00:00"}
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        st = web_ui.api_status()["notion"]
        check("웹 상태: 지금 열이 자동 선택값이면 active",
              st["auto"]["active"] is True and st["auto"]["column"] == "서브.오", str(st["auto"]))
        check("웹 상태: 자동 선택 계정 닉네임을 함께 전달", st["auto"].get("nickname") == "오정원",
              str(st["auto"]))
        check("웹 상태 응답에 토큰 없음", "ntn_auto_probe" not in json.dumps(st, ensure_ascii=False))
        check("웹 화면: '(자동 선택됨)' 표시 코드",
              "(자동 선택됨)" in web_ui.PAGE_HTML and "auto.active" in web_ui.PAGE_HTML)

        # 4-9) check_login() 이 실제 경로 — 로그인 상태를 읽으면 바로 자동 선택까지 한다
        web_ui.reset_state()
        web_ui.STATE["columns"] = list(cols)
        main.login_state = lambda: login_of("이신엽", "ID …1234")
        web_ui.save_column("서브.오")
        state = web_ui.check_login()
        check("check_login: 로그인 계정을 읽고", state.get("nickname") == "이신엽", str(state))
        check("check_login: 담당자 열을 닉네임으로 자동 선택", secret()["column"] == "메인.엽", secret()["column"])
        check("check_login: 상태에 자동 선택 정보가 실린다",
              web_ui.api_status()["notion"]["auto"]["active"] is True, str(web_ui.api_status()["notion"]["auto"]))
    finally:
        for k, v in saved.items():
            setattr(main, k, v)
        web_ui.reset_state()
        tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    test_filter()
    test_apply_flow()
    test_login_logic()
    test_notion()
    test_main_modes()
    test_web()
    test_notion_once()
    test_onestop()
    test_logout()
    test_column_discovery()
    test_session_cache()
    test_nickname_columns()
    cleanup()
    print()
    if FAILS:
        print(f"실패 {len(FAILS)}건: {FAILS}")
        sys.exit(1)
    print("모든 점검 통과")
