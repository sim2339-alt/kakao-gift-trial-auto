# -*- coding: utf-8 -*-
"""
자가 점검 스크립트 (개발자용)

실제 카카오 사이트에 로그인하지 않고도 필터링·화면 조작 로직이 깨지지 않았는지 확인합니다.
    python tests/selftest.py

- 1부: 필터/정렬 로직을 저장된 샘플 응답으로 검증
- 2부: Playwright 페이지를 가짜 객체로 바꿔 신청 플로우(성공/이미신청/버튼없음/로그인필요) 검증
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
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
    print("[1/4] 필터 검증")
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
    print("[2/4] 신청 플로우 검증 (가짜 브라우저)")
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
    print("[4/4] 노션 기록·설정·실행 모드 검증 (가짜 노션 API)")
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
    check("노션: 내 열에 '11/20 O'",
          props.get("메인.엽", {}).get("rich_text", [{}])[0].get("text", {}).get("content") == "11/20 O",
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
    stub = NotionStub(rows=[notion_row("p1", "abc123", "t", mark="11/20 O")])
    rec = main.notion_record(CFG, item, transport=stub, now=NOW)
    check("노션: 오늘 날짜 기록 있음 → unchanged", rec["status"] == "unchanged", f"{rec}")
    check("노션: unchanged 면 PATCH 하지 않음", stub.patches == {}, str(stub.patches))

    # 4) 다른 날 기록만 있음 → 기존 내용 유지 + 줄바꿈으로 덧붙임
    stub = NotionStub(rows=[notion_row("p1", "abc123", "t", mark="11/19")])
    rec = main.notion_record(CFG, item, transport=stub, now=NOW)
    new = "".join(c["text"]["content"] for c in stub.patches["p1"]["메인.엽"]["rich_text"])
    check("노션: 기존 기록 유지 + 덧붙임", rec["status"] == "updated" and new == "11/19\n11/20 O", f"{rec} / {new!r}")

    # 5) 다른 계정 열을 쓰면 그 열에만 기록
    cfg2 = dict(CFG, column="서브.동")
    stub = NotionStub(rows=[notion_row("p2", "abc123", "t", mark="11/20", column="서브.동")])
    rec = main.notion_record(cfg2, item, transport=stub, now=NOW)
    check("노션: --notion-column 열 기준으로 중복 판정", rec["status"] == "unchanged", f"{rec}")

    # 5-1) 노션 검색 지연 대비 — 이번 배치 색인으로도 중복을 막는다
    rec = main.notion_record(CFG, item, transport=NotionStub(), now=NOW,
                             index={"abc123": {"page_id": "p9", "mark": "11/20 O"}})
    check("노션: 색인에 오늘 기록이 있으면 unchanged", rec["status"] == "unchanged", f"{rec}")
    stub = NotionStub()
    rec = main.notion_record(CFG, item, transport=stub, now=NOW,
                             index={"abc123": {"page_id": "p9", "mark": ""}})
    check("노션: 색인 행이 미체크면 그 행을 갱신", rec["status"] == "updated" and "p9" in stub.patches, f"{rec}")
    idx = {}
    stub = NotionStub()
    rec = main.notion_record(CFG, item, transport=stub, now=NOW, index=idx)
    check("노션: 생성 후 배치 색인에 등록", rec["status"] == "created"
          and idx.get("abc123", {}).get("mark") == "11/20 O", str(idx))
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

    # 8) DB 전체 읽기 + 미체크 필터(모드 1)
    stub = NotionStub(rows=[
        notion_row("p1", "aaa", "체크된 상품", mark="11/20"),
        notion_row("p2", "bbb", "미체크 상품"),
        notion_row("p3", "ccc", "다른 열만 체크", mark="11/20", column="서브.동"),
    ])
    scan = main.notion_scan(CFG, transport=stub)
    check("노션: DB 스캔으로 campaignId→행 매핑", set(scan) == {"aaa", "bbb", "ccc"}, str(scan.keys()))
    cands = [{"campaignId": "bbb"}, {"campaignId": "aaa"}, {"campaignId": "ddd"}, {"campaignId": "ccc"}]
    ordered, stat = main.order_by_notion(cands, scan)
    check("모드1: 내 열 체크된 캠페인 제외", [c["campaignId"] for c in ordered] == ["bbb", "ccc", "ddd"],
          str([c["campaignId"] for c in ordered]))
    check("모드1: 미체크(DB 등록) 먼저 → 신규 다음",
          stat["checked"] == 1 and stat["pending"] == 2 and stat["fresh"] == 1, str(stat))

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


def cleanup():
    for path in (ROOT / "logs").glob("fail_*.html"):
        path.unlink(missing_ok=True)


def test_login_logic():
    print("[3/4] 로그인 로직 검증 (네트워크 없이)")
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
    cleanup()
    print()
    if FAILS:
        print(f"실패 {len(FAILS)}건: {FAILS}")
        sys.exit(1)
    print("모든 점검 통과")
