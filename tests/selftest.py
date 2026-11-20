# -*- coding: utf-8 -*-
"""
자가 점검 스크립트 (개발자용)

실제 카카오 사이트에 로그인하지 않고도 필터링·화면 조작 로직이 깨지지 않았는지 확인합니다.
    python tests/selftest.py

- 1부: 필터/정렬 로직을 저장된 샘플 응답으로 검증
- 2부: Playwright 페이지를 가짜 객체로 바꿔 신청 플로우(성공/이미신청/버튼없음/로그인필요) 검증
- 6부: 웹 조종판(web_ui.py) API·실행/중단·토큰 미노출 검증
"""

from __future__ import annotations

import json
import os
import re
import sys
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
    print("[1/6] 필터 검증")
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
    print("[2/6] 신청 플로우 검증 (가짜 브라우저)")
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
    print("[4/6] 노션 기록·설정·실행 모드 검증 (가짜 노션 API)")
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

    # 11) 개수 입력: Enter = 기본, 숫자만 인정
    with use_input(["", "10", "abc", "7"]) as fake:
        check("입력: Enter → 기본 30", main.ask_limit() == 30)
        check("입력: 숫자 → 그 값", main.ask_limit() == 10)
        check("입력: 문자는 다시 묻는다", main.ask_limit() == 7, str(fake.prompts[-1:]))
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
    print("[5/6] 실행 모드 통합 검증 (main() — 네트워크·브라우저 없이)")
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
        "run_apply": main.run_apply,
        "stdin": sys.stdin,
    }
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

        # (7) --limit 0 은 거부
        sys.stdin = FakeStdin(False)
        rc = main.main(["--limit", "0"])
        check("--limit 0 → 중단", rc == 1, f"rc={rc}")
    finally:
        main.collect_campaigns = saved["collect_campaigns"]
        main.collect_applied_ids = saved["collect_applied_ids"]
        main.load_notion_config = saved["load_notion_config"]
        main.notion_rows = saved["notion_rows"]
        main.run_apply = saved["run_apply"]
        sys.stdin = saved["stdin"]


def cleanup():
    for path in (ROOT / "logs").glob("fail_*.html"):
        path.unlink(missing_ok=True)


# --------------------------------------------- 6부: 웹 조종판(web_ui) 검증
def test_web():
    print("[6/6] 웹 조종판 API 검증 (가짜 카카오·노션, 실제 HTTP 서버)")
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
            st2 = json.loads(urllib.request.urlopen(base + "/api/status", timeout=15).read().decode("utf-8"))
            check("웹: /api/status 200", st2.get("version") == web_ui.APP_VERSION, str(st2)[:120])
            check("웹: /api/status 토큰 미노출", "ntn_" not in json.dumps(st2, ensure_ascii=False))
            req = urllib.request.Request(
                base + "/api/preview", data=json.dumps({"mode": "notion", "limit": 3}).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            pv4 = json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8"))
            check("웹: /api/preview 동작", pv4.get("ok") is True and len(pv4.get("items") or []) <= 3,
                  str(pv4)[:120])
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
    print("[3/6] 로그인 로직 검증 (네트워크 없이)")
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


if __name__ == "__main__":
    test_filter()
    test_apply_flow()
    test_login_logic()
    test_notion()
    test_main_modes()
    test_web()
    cleanup()
    print()
    if FAILS:
        print(f"실패 {len(FAILS)}건: {FAILS}")
        sys.exit(1)
    print("모든 점검 통과")
