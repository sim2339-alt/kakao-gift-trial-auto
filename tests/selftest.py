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
    print("[1/2] 필터 검증")
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
    post_data_json = {"campaignId": "abc"}


class FakeResponse:
    def __init__(self, status=200):
        self.status = status
        self.url = main.BASE + main.APPLY_API_PATH
        self.request = FakeRequest()

    def json(self):
        return {"result": True}

    def text(self):
        return '{"result": true}'


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


class FakePage:
    def __init__(self, buttons, body, url=None, on_apply_click="success", goto_redirect=None):
        self.buttons = buttons
        self.body = body
        self.tail_body = ""
        self.url = url or main.BASE + "/giftbox/trial-campaign/abc/apply"
        self.on_apply_click = on_apply_click
        self.goto_redirect = goto_redirect  # 로그인 페이지로 튕기는 상황 모사
        self.listeners = {}
        self.checked = 0
        self.clicks = []

    # --- playwright API 모사
    def goto(self, url, wait_until=None):
        self.url = self.goto_redirect or url

    def wait_for_timeout(self, ms):
        return None

    def inner_text(self, selector, timeout=None):
        return self.tail_body or self.body

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
        if "신청" in text and self.on_apply_click == "success":
            self.url = main.BASE + "/giftbox/trial-campaign/applied/abc"
            for cb in self.listeners.get("response", []):
                cb(FakeResponse(200))
        elif "신청" in text and self.on_apply_click == "fail":
            self.tail_body = "신청에 실패했습니다"
            for cb in self.listeners.get("response", []):
                cb(FakeResponse(400))


def test_apply_flow():
    print("[2/2] 신청 플로우 검증 (가짜 브라우저)")
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

    page = FakePage(["배송지 변경", "신청 완료하기", "확인"], "배송지 서울시 중구 세종대로 110")
    res = main.apply_one(page, campaign)
    check("정상 신청 → 성공", res["status"] == "성공", f"{res['status']} / {res['reason']}")
    check("신청 버튼 클릭 발생", any("신청" in c for c in page.clicks), str(page.clicks))

    page = FakePage(["위시에 담기", "신청 완료하기", "확인"], "배송지 서울시 중구 세종대로 110")
    res = main.apply_one(page, campaign)
    check("위시 담기 단계 처리", any("위시" in c for c in page.clicks), str(page.clicks))
    check("위시 + 신청 → 성공", res["status"] == "성공", f"{res['status']} / {res['reason']}")

    page = FakePage(["위시에 담기", "신청 완료하기", "확인"], "배송지 서울시 중구 세종대로 110")
    main.apply_one(page, campaign, skip_wishlist=True)
    check("--skip-wishlist 시 위시 클릭 없음", all("위시" not in c for c in page.clicks), str(page.clicks))

    page = FakePage(["신청 완료하기"], "이미 신청한 체험단입니다")
    res = main.apply_one(page, campaign)
    check("이미 신청 → 스킵", res["status"] == "스킵", f"{res['status']} / {res['reason']}")

    page = FakePage([], "배송지 서울시 123")
    res = main.apply_one(page, campaign)
    check("신청 버튼 없음 → 실패", res["status"] == "실패", f"{res['status']} / {res['reason']}")

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

    page = FakePage(["신청 완료하기"], "배송지 서울시 123", on_apply_click="fail")
    res = main.apply_one(page, campaign)
    check("신청 응답 400 → 실패", res["status"] == "실패", f"{res['status']} / {res['reason']}")


def cleanup():
    for path in (ROOT / "logs").glob("fail_*.html"):
        path.unlink(missing_ok=True)


def test_login_logic():
    print("[3/3] 로그인 로직 검증 (네트워크 없이)")
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
    cleanup()
    print()
    if FAILS:
        print(f"실패 {len(FAILS)}건: {FAILS}")
        sys.exit(1)
    print("모든 점검 통과")
