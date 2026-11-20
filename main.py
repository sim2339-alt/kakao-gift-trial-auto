# -*- coding: utf-8 -*-
"""
카카오톡 선물하기 '선물 체험단' 자동 신청 도우미 (모바일 에뮬레이션)

사용법
  python main.py --login            최초 1회: 브라우저에서 카카오 로그인 후 세션 저장
  python main.py --dry-run          실제 신청 없이 오늘 후보 목록만 확인
  python main.py --limit 30         후보 상위 30개 실제 신청 (기본값 30)

주의
  - 본인 계정으로만 사용하세요. 계정 공유·대량 자동화는 이용약관 위반이 될 수 있습니다.
  - 로그인 정보(비밀번호)는 이 스크립트 어디에도 저장되지 않습니다.
    로그인 세션은 ./kakao_profile 폴더(브라우저 프로필)와 kakao_state.json(세션 백업)에만 남습니다.
  - 로그인은 반드시 '데스크톱 웹 로그인' 경로로 합니다(아래 _login_url 설명 참고).
    모바일 화면에서 로그인 버튼을 누르면 카카오톡 앱 안내로 빠져 PC에서는 로그인이 완결되지 않습니다.

신청 순서(2026-11-20 실측, gift 번들 chunk-TXTX3FXH / chunk-2BRB6BUS 기준)
  1) 상세 페이지: 상품을 위시(선물함)에 담기  ← 이 단계를 건너뛰면 상세 페이지가 신청을 막습니다.
  2) 상세 페이지: [선물 체험단 신청하기] → 옵션이 있는 캠페인은 옵션 선택 레이어에서 하나 고름
  3) 신청 페이지(/apply?optionId=…): 신청 완료하기 → POST /a/partner-trial/api/v1/trial-campaigns/apply
  · 신청 페이지는 옵션을 화면에서 고르지 않고 URL query 의 optionId 만 읽습니다.
    optionId 없이 들어가면 옵션 칸이 빈 채로 신청되어 서버가 거부하고
    '체험단에 응모하지 못했어요 / 고객센터로 문의해 주세요' 팝업이 뜨게 됩니다.
"""

from __future__ import annotations

import argparse
import csv
import http.cookiejar
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 0. 상수 (2026-11-20 실측 기준)
# ---------------------------------------------------------------------------
BASE = "https://gift.kakao.com"
LIST_API = BASE + "/a/partner-trial/api/v1/trial-campaigns"
HISTORY_API = BASE + "/a/partner-trial/api/v1/trial-campaigns/apply"
APPLY_API_PATH = "/a/partner-trial/api/v1/trial-campaigns/apply"
DETAIL_PAGE = BASE + "/giftbox/trial-campaign/{cid}"
APPLY_PAGE = BASE + "/giftbox/trial-campaign/{cid}/apply"
# 신청 페이지가 실제로 쓰는 캠페인 상세(옵션 목록·위시 필요 여부·신청 여부).
DETAIL_API = BASE + "/a/partner-trial/api/v1/trial-campaigns/{cid}"
# 상품 위시(선물함) 담김 여부. 응답: {"wished": bool, "visibleTo": "others"|"me"|null}
WISH_STATE_API = BASE + "/a/product-detail/v1/items/{pid}/wishes/wish"
# 신청이 거부될 때 화면에 뜨는 catch-all 팝업 문구(번들 chunk-2BRB6BUS 실측)
APPLY_DENIED_TEXT = "체험단에 응모하지 못했어요"

# 모바일 UA 필수: 데스크톱 UA로 접근하면 PC 제한(/pc-restricted) 화면으로 막힐 수 있음
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
DEVICE_CANDIDATES = ["iPhone 14", "iPhone 13", "iPhone 12"]
PROFILE_DIR = "kakao_profile"
LOG_DIR = Path("logs")

# 로그인 전용(데스크톱) 설정 — 자세한 이유는 _login_url() 주석 참고
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DESKTOP_VIEWPORT = {"width": 1280, "height": 900}
# 카카오 '웹 로그인(OAuth 코드)' 입구 — gift 세션 쿠키를 실제로 발급하는 유일한 경로
AUTH_WEB = "https://shopping-userauth-web.kakao.com"
GIFT_WEB_LOGIN = BASE + "/a/v1/web/login"
LOGIN_TARGET = BASE + "/giftbox/trial-campaign"
# 로그인 세션 백업(storage_state) — .gitignore 에 등록되어 있음
STATE_FILE = "kakao_state.json"
LOGIN_WAIT_SECONDS = 180
LOGIN_POLL_SECONDS = 2

# 목록 API 정렬은 CREATED_AT만 허용된다.
# sortProperty=APPLY_COUNT 는 서버가 HTTP 400 을 돌려주므로, 인기순은 받아온 뒤 로컬에서 정렬한다.
LIST_SORT_PROPERTY = "CREATED_AT"
PAGE_SIZE = 50

# ---------------------------------------------------------------------------
# 1. 사용자 맞춤 필터 기본값 (config.json 이 있으면 그 값으로 덮어쓴다)
#    카테고리 ID와 이름은 gift.kakao.com 실제 응답에서 확인한 값.
# ---------------------------------------------------------------------------
DEFAULT_FILTER = {
    # 허용 대분류: 식품(93, 250) / 리빙(94) / 패션(96) / 디지털·가전(97)
    "allow_l1_ids": [93, 94, 96, 97, 250],
    # 제외 대분류: 뷰티(95, 243) / 향수·바디(244) / 레저·스포츠·골프(99)
    "deny_l1_ids": [95, 243, 244, 99],
    # 유아동·반려(98)는 아래 하위 카테고리만 허용 (키즈·아동용품)
    "allow_l2_ids_under_98": [138, 139, 141],
    # 제외 하위 카테고리: 신생아선물세트(136), 임신·출산·육아(144), 반려동물(314), 뷰티가전(161)
    "deny_l2_ids": [136, 144, 314, 161],
    # 상품명에 아래 단어가 있으면 제외
    "deny_keywords": [
        "신생아", "배냇저고리", "딸랑이", "치발기", "젖병", "분유", "아기띠",
        "임산부", "산모", "수유", "유축", "이유식", "기저귀",
        "반려동물", "반려", "강아지", "고양이", "사료", "캣타워", "펫",
        "탈모", "염색", "립스틱", "틴트", "파운데이션", "아이섀도",
        "선크림", "세럼", "앰플", "마스크팩", "아이라이너", "쿠션팩트",
        "골프", "스포츠", "등산", "헬스",
    ],
}


def load_filter() -> dict:
    """config.json 이 있으면 기본값 위에 덮어쓴다(비개발자가 기준을 바꿀 때 사용)."""
    cfg = json.loads(json.dumps(DEFAULT_FILTER, ensure_ascii=False))
    path = Path("config.json")
    if path.exists():
        try:
            user_cfg = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(user_cfg, dict):
                cfg.update(user_cfg)
                print(f"[설정] config.json 적용: {path.resolve()}")
        except Exception as exc:  # 설정 파일이 깨져도 기본값으로 계속 진행
            print(f"[경고] config.json 을 읽지 못했습니다({exc}). 기본 기준으로 진행합니다.")
    return cfg


# ---------------------------------------------------------------------------
# 2. HTTP (표준 라이브러리만 사용 → 설치 실패 요인 최소화)
# ---------------------------------------------------------------------------
_SESSION_OPENER = None  # 저장된 세션 쿠키를 붙인 요청기(1회만 생성)


def _load_state_cookies(path: str = STATE_FILE) -> list:
    """kakao_state.json(storage_state)에서 쿠키 목록만 꺼낸다. 값은 절대 출력하지 않는다."""
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    cookies = data.get("cookies") or []
    return [c for c in cookies if c.get("name") and c.get("domain")]


def _cookie_jar(cookies: list | None = None) -> http.cookiejar.CookieJar:
    """storage_state 쿠키 → urllib 용 CookieJar (--dry-run 신청이력 조회에 사용)."""
    jar = http.cookiejar.CookieJar()
    for c in (_load_state_cookies() if cookies is None else cookies):
        try:
            expires = c.get("expires")
            expires = int(expires) if expires and float(expires) > 0 else None
            jar.set_cookie(http.cookiejar.Cookie(
                version=0, name=c["name"], value=c.get("value", ""),
                port=None, port_specified=False,
                domain=c["domain"], domain_specified=True,
                domain_initial_dot=str(c["domain"]).startswith("."),
                path=c.get("path") or "/", path_specified=True,
                secure=bool(c.get("secure")), expires=expires, discard=False,
                comment=None, comment_url=None, rfc2109=False,
                rest={"HttpOnly": None} if c.get("httpOnly") else {},
            ))
        except Exception:
            continue
    return jar


def _session_opener():
    global _SESSION_OPENER
    if _SESSION_OPENER is None:
        jar = _cookie_jar()
        _SESSION_OPENER = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)) if len(jar) else False
    return _SESSION_OPENER or None


def fetch_json(url: str, timeout: int = 20, use_session: bool = False):
    """(status, data, error) 반환. 로그인 관련 401 은 예외 대신 상태코드로 돌려준다."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": MOBILE_UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": BASE + "/giftbox/trial-campaign",
        },
    )
    opener = _session_opener() if use_session else None
    try:
        if opener is not None:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return resp.status, json.loads(raw), None
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw), None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except Exception:
            data = None
        return exc.code, data, raw[:300]
    except Exception as exc:
        return None, None, str(exc)


def collect_campaigns(max_pages: int) -> list:
    """진행 중(IN_PROGRESS) 체험단 전체를 페이지 순회로 수집."""
    items, page, total = [], 0, None
    while page < max_pages:
        url = (
            f"{LIST_API}?status=IN_PROGRESS&page={page}&size={PAGE_SIZE}"
            f"&sortProperty={LIST_SORT_PROPERTY}&sortDir=DESC"
        )
        status, data, err = fetch_json(url)
        if status != 200 or not isinstance(data, dict):
            print(f"[오류] 목록 API 실패 (page={page}, status={status}, {err})")
            break
        total = data.get("totalCount", total)
        batch = data.get("contents") or []
        items.extend(batch)
        if data.get("isLast") or not batch:
            break
        page += 1
        time.sleep(random.uniform(0.4, 0.9))
    if total is not None:
        print(f"[수집] 진행 중 체험단 {len(items)}개 (서버 totalCount={total})")
    return items


def collect_applied_ids() -> set | None:
    """이미 신청한 캠페인 ID 집합. 로그인 세션이 없으면 401 → None."""
    applied, page = set(), 0
    while page < 30:
        status, data, err = fetch_json(f"{HISTORY_API}?page={page}&size=50", use_session=True)
        if status == 401:
            print("[안내] 신청 이력 조회는 로그인이 필요합니다(저장된 세션 없음/만료).")
            print("       run_login.bat 을 한 번 실행하면 다음부터 신청 이력까지 자동으로 걸러집니다.")
            return None
        if status != 200 or not isinstance(data, dict):
            print(f"[경고] 신청 이력 조회 실패 (status={status}, {err})")
            return None
        batch = data.get("contents") or []
        for row in batch:
            cid = row.get("campaignId")
            if cid:
                applied.add(cid)
        if data.get("isLast") or not batch:
            break
        page += 1
    print(f"[수집] 이미 신청한 체험단 {len(applied)}개")
    return applied


# ---------------------------------------------------------------------------
# 3. 필터링 / 정렬
# ---------------------------------------------------------------------------
def classify(item: dict, flt: dict):
    """(판정, 사유) 반환. 판정: allow(신청 대상) / hold(카테고리 미확인 → 보류) / deny(제외)."""
    views = item.get("displayCategoryViews") or []
    title = item.get("campaignTitle") or ""

    # 카테고리 정보가 없는 상품은 함부로 신청하지 않고 '보류'로 분리한다.
    if not views:
        if category_blank_keyword_ok(title, flt):
            return "hold", "카테고리 미확인(보류)"
        return "deny", "카테고리 미확인 + 제외 키워드"

    for v in views:
        lid = v.get("ldisplayCategoryId")
        mid = v.get("mdisplayCategoryId")
        lname = v.get("ldisplayCategoryName") or ""
        mname = v.get("mdisplayCategoryName") or ""
        if lid in flt["deny_l1_ids"]:
            return "deny", f"제외 카테고리({lname}>{mname})"
        if mid in flt["deny_l2_ids"]:
            return "deny", f"제외 하위카테고리({mname})"
        if "뷰티" in lname or "골프" in mname or "스포츠" in mname \
                or "반려" in mname or "동물" in mname:
            return "deny", f"제외 카테고리({lname}>{mname})"

    allowed = False
    for v in views:
        lid = v.get("ldisplayCategoryId")
        mid = v.get("mdisplayCategoryId")
        if lid in flt["allow_l1_ids"]:
            allowed = True
        elif lid == 98 and mid in flt["allow_l2_ids_under_98"]:
            allowed = True
    if not allowed:
        v = views[0]
        return "deny", f"허용 범위 밖({v.get('ldisplayCategoryName')}>{v.get('mdisplayCategoryName')})"

    for kw in flt["deny_keywords"]:
        if kw and kw in title:
            return "deny", f"제외 키워드({kw})"
    return "allow", "허용"


def category_blank_keyword_ok(title: str, flt: dict) -> bool:
    """카테고리가 비어 있는 상품을 키워드만으로 거를 때 쓰는 검사."""
    for kw in flt["deny_keywords"]:
        if kw and kw in title:
            return False
    return True


def category_label(item: dict) -> str:
    views = item.get("displayCategoryViews") or []
    if not views:
        return "-"
    v = views[0]
    lname = v.get("ldisplayCategoryName") or "?"
    mname = v.get("mdisplayCategoryName") or ""
    return f"{lname}>{mname}" if mname else lname


def build_candidates(items: list, flt: dict, sort_mode: str, exclude_ids: set | None,
                     include_unknown: bool = False) -> tuple:
    """(신청 후보, 보류 후보, 제외 건수) 반환."""
    passed, held, denied = [], [], 0
    for item in items:
        decision, reason = classify(item, flt)
        if decision == "deny":
            denied += 1
            continue
        if exclude_ids and item.get("campaignId") in exclude_ids:
            denied += 1
            continue
        if decision == "hold":
            if include_unknown:
                passed.append(item)
            else:
                held.append(item)
            continue
        passed.append(item)

    if sort_mode == "deadline":
        passed.sort(key=lambda x: (x.get("remainingDays", 999), -(x.get("applicationCount") or 0)))
        held.sort(key=lambda x: (x.get("remainingDays", 999), -(x.get("applicationCount") or 0)))
    else:  # popular
        passed.sort(key=lambda x: -(x.get("applicationCount") or 0))
        held.sort(key=lambda x: -(x.get("applicationCount") or 0))

    print(f"[필터] 허용 {len(passed)}개 / 보류 {len(held)}개 / 제외 {denied}개")
    return passed, held, denied


# ---------------------------------------------------------------------------
# 4. 화면 출력 · CSV 저장
# ---------------------------------------------------------------------------
def print_table(items: list, limit: int, header: str | None = None):
    if not items:
        print("후보가 없습니다.")
        return
    rows = items[:limit]
    line = "-" * 96
    if header:
        print(header)
    print(line)
    print(f"{'#':>3}  {'신청자':>7}  {'모집':>4}  {'마감':>4}  {'카테고리':<18}  상품명")
    print(line)
    for idx, item in enumerate(rows, 1):
        title = (item.get("campaignTitle") or "").strip()
        if len(title) > 34:
            title = title[:33] + "…"
        brand = (item.get("brand") or {}).get("name", "")
        print(
            f"{idx:>3}  {(item.get('applicationCount') or 0):>7,}  "
            f"{(item.get('recruitmentCount') or 0):>4}  "
            f"{(str(item.get('remainingDays', '-')) + '일'):>4}  "
            f"{category_label(item):<18}  {title}  [{brand}]"
        )
    print(line)
    print(f"표시 {len(rows)}개 (후보 전체 {len(items)}개)")


def save_candidates_csv(items: list, limit: int, held: list | None = None) -> Path:
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / f"candidates_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as fp:
        wr = csv.writer(fp)
        wr.writerow(["순위", "상태", "campaignId", "상품명", "브랜드", "카테고리",
                     "신청자수", "모집인원", "남은일수", "상세URL"])
        rank = 0
        for item in items[:limit]:
            rank += 1
            wr.writerow([
                rank,
                "신청대상",
                item.get("campaignId", ""),
                item.get("campaignTitle", ""),
                (item.get("brand") or {}).get("name", ""),
                category_label(item),
                item.get("applicationCount") or 0,
                item.get("recruitmentCount") or 0,
                item.get("remainingDays", ""),
                DETAIL_PAGE.format(cid=item.get("campaignId", "")),
            ])
        for item in (held or []):
            wr.writerow([
                "-",
                "보류(카테고리 미확인)",
                item.get("campaignId", ""),
                item.get("campaignTitle", ""),
                (item.get("brand") or {}).get("name", ""),
                category_label(item),
                item.get("applicationCount") or 0,
                item.get("recruitmentCount") or 0,
                item.get("remainingDays", ""),
                DETAIL_PAGE.format(cid=item.get("campaignId", "")),
            ])
    return path


def save_results_csv(results: list, path: Path | None = None) -> Path:
    LOG_DIR.mkdir(exist_ok=True)
    path = path or LOG_DIR / f"results_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as fp:
        wr = csv.writer(fp)
        wr.writerow(["시각", "campaignId", "상품명", "브랜드", "카테고리",
                     "신청자수", "모집인원", "결과", "사유"])
        for row in results:
            wr.writerow([
                row.get("time", ""), row.get("campaignId", ""), row.get("title", ""),
                row.get("brand", ""), row.get("category", ""),
                row.get("applicationCount", 0), row.get("recruitmentCount", 0),
                row.get("status", ""), row.get("reason", ""),
            ])
    return path


# ---------------------------------------------------------------------------
# 5. Playwright (로그인 / 신청) — 무거우므로 필요할 때만 import
# ---------------------------------------------------------------------------
class LoginRequired(Exception):
    pass


def _pick_device(p):
    for name in DEVICE_CANDIDATES:
        if name in p.devices:
            return p.devices[name]
    print("[경고] 등록된 iPhone 기기 프로필이 없어 기본 모바일 설정으로 실행합니다.")
    return {"user_agent": MOBILE_UA, "viewport": {"width": 390, "height": 844},
            "is_mobile": True, "has_touch": True, "device_scale_factor": 3}


def _browser_hint(exc) -> str:
    """playwright 가 내는 불친절한 오류를 사용자용 문장으로 바꾼다."""
    msg = str(exc)
    if "Executable doesn't exist" in msg or "playwright install" in msg:
        return "브라우저(크로미움)가 설치되지 않았습니다. install.bat 을 먼저 실행해 주세요."
    if "Host system is missing dependencies" in msg:
        return "브라우저 실행에 필요한 시스템 파일이 없습니다(리눅스: playwright install-deps)."
    return ""


def _launch(p, headless: bool, mode: str = "mobile"):
    """영구 프로필(kakao_profile)로 브라우저 실행.

    mode="mobile" : 체험단 신청용(iPhone 14 에뮬레이션). 데스크톱 UA 로 열면 /pc-restricted 로 막힌다.
    mode="desktop": 로그인용. gift 의 로그인 분기가 모바일 UA 에서는 '카카오톡 앱으로 열기'로 빠지므로
                    로그인만은 데스크톱 UA 로 진행한다(쿠키는 UA 와 무관하게 같은 프로필에 저장된다).
    """
    from playwright.sync_api import sync_playwright  # noqa: F401  (문서화용)

    if mode == "desktop":
        opts = {"user_agent": DESKTOP_UA, "viewport": dict(DESKTOP_VIEWPORT)}
    else:
        opts = dict(_pick_device(p))
        opts.pop("default_browser_type", None)

    ctx = p.chromium.launch_persistent_context(
        str(Path(PROFILE_DIR).resolve()),
        headless=headless,
        locale="ko-KR",
        timezone_id="Asia/Seoul",
        args=["--disable-blink-features=AutomationControlled"],
        **opts,
    )
    ctx.set_default_timeout(8000)
    return ctx


def _restore_state(ctx) -> int:
    """kakao_state.json 이 있으면 쿠키를 프로필에 복원한다(프로필이 날아간 경우 대비)."""
    cookies = _load_state_cookies()
    if not cookies:
        return 0
    try:
        ctx.add_cookies(cookies)
        print(f"[세션] kakao_state.json 쿠키 {len(cookies)}개를 불러왔습니다.")
        return len(cookies)
    except Exception as exc:
        print(f"[경고] kakao_state.json 쿠키 복원 실패: {type(exc).__name__}")
        return 0


def _ensure_session(ctx) -> bool:
    """프로필 세션을 먼저 확인하고, 없을 때만 kakao_state.json 백업을 복원한다.

    (새 프로필에 이미 있는 세션을 오래된 백업 쿠키로 덮어써 로그아웃시키지 않기 위한 순서)
    """
    if _logged_in(ctx):
        return True
    if _restore_state(ctx) and _logged_in(ctx):
        print("[세션] kakao_state.json 백업 세션으로 로그인 상태를 확인했습니다.")
        return True
    return False


def _login_probe(ctx) -> tuple:
    """(status, error) — HISTORY_API 를 한 번 호출해 로그인 여부를 본다. 200 이면 로그인 상태."""
    try:
        resp = ctx.request.get(
            f"{HISTORY_API}?page=0&size=1",
            headers={"User-Agent": MOBILE_UA, "Referer": LOGIN_TARGET},
        )
        return resp.status, ""
    except Exception as exc:
        return None, str(exc)[:120]


def _logged_in(ctx) -> bool:
    return _login_probe(ctx)[0] == 200


def _login_url(target: str = LOGIN_TARGET) -> str:
    """gift 세션을 발급하는 '데스크톱 웹 로그인' 입구 주소(2026-11-20 실측).

    gift 번들(chunk-ZOOQ4WOZ.js)의 인증 서비스는
        login() → isKakaotalk || isMobileOrTablet ? loginApp() : getWebAuthUrl()
    로 갈라진다. 즉 모바일 UA 에서는 로그인 버튼이 /giftbox-redirect(카카오톡 앱 안내)로 빠지고,
    그 화면의 '카카오 계정으로 로그인하기'는 accounts.kakao.com → /unsup/giftbox 로 돌아와
    gift 세션을 만들지 못한다. 그래서 모바일 창에서 손으로 로그인해도 체험단 페이지는 계속 로그아웃 상태다.

    데스크톱 분기의 getWebAuthUrl() 이 만드는 주소는 UA 와 무관하게 동작하며,
    shopping-userauth-web → kauth.kakao.com/oauth/authorize → accounts.kakao.com/login
    → gift.kakao.com/a/v1/web/login 순서로 돌아와 gift 쿠키를 실제로 발급한다.
    """
    inner = f"{GIFT_WEB_LOGIN}?redirectUrl={urllib.parse.quote(target, safe='')}"
    return f"{AUTH_WEB}/login?channel=GIFT&redirect_url={urllib.parse.quote(inner, safe='')}"


def _cookie_summary(cookies: list) -> dict:
    """도메인별 쿠키 개수만 센다(쿠키 값은 절대 출력하지 않는다)."""
    out: dict = {}
    for c in cookies or []:
        domain = str(c.get("domain") or "").lstrip(".")
        if domain:
            out[domain] = out.get(domain, 0) + 1
    return out


def _domain_count(summary: dict, host: str) -> int:
    """해당 호스트에 직접 설정된 쿠키만 센다('kakao.com' 이 '*.kakao.com' 까지 세지 않게)."""
    return int(summary.get(host, 0)) + int(summary.get("." + host, 0))


def _enter_pressed() -> bool:
    """검은 창에서 Enter 가 눌렸는지 논블로킹으로 확인(터미널이 아니면 무시)."""
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return False
        if os.name == "nt":
            import msvcrt
            if msvcrt.kbhit():
                msvcrt.getwch()
                return True
            return False
        import select
        readable, _, _ = select.select([sys.stdin], [], [], 0)
        if readable:
            sys.stdin.readline()
            return True
    except Exception:
        pass
    return False


def _wait_for_login(page, ctx, wait_seconds: int = LOGIN_WAIT_SECONDS) -> tuple:
    """2초 간격으로 로그인 여부를 폴링한다. Enter 를 누르면 즉시 한 번 더 확인한다.

    (2초에 1회만 조회한다. 상대 서비스에 부담을 주지 않도록 0.1초 간격으로 두드리지 않는다.)
    반환: (로그인여부, 확인횟수)
    """
    deadline = time.time() + wait_seconds
    checks = 0
    while True:
        checks += 1
        if _login_probe(ctx)[0] == 200:
            return True, checks
        if time.time() >= deadline:
            return False, checks

        waited = 0.0
        while waited < LOGIN_POLL_SECONDS:
            try:
                page.wait_for_timeout(100)
            except Exception:  # 사용자가 창을 닫은 경우
                return _login_probe(ctx)[0] == 200, checks
            waited += 0.1
            if _enter_pressed():
                break
        left = int(max(0, deadline - time.time()))
        print(f"\r  [대기] 로그인 감지 중… {wait_seconds - left}s / {wait_seconds}s "
              f"(Enter 로 즉시 확인)      ", end="", flush=True)


def _verify_mobile_session(p) -> tuple:
    """로그인 후 실제 신청에 쓰는 모바일(iPhone 14) 화면에서도 세션이 유지되는지 확인한다.

    반환: (HISTORY_API 상태코드 또는 None, 오류문자열)
    """
    try:
        ctx = _launch(p, headless=True, mode="mobile")
    except Exception as exc:
        return "launch_error", f"{type(exc).__name__}: {str(exc)[:80]}"
    try:
        return _login_probe(ctx)
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def run_login(mode: str = "desktop") -> int:
    from playwright.sync_api import sync_playwright

    login_url = _login_url()
    where = "데스크톱 크기" if mode == "desktop" else "모바일(iPhone) 크기"
    with sync_playwright() as p:
        ctx = _launch(p, headless=False, mode=mode)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        if _ensure_session(ctx):
            try:
                page.wait_for_timeout(300)
                ctx.storage_state(path=STATE_FILE)
            except Exception:
                pass
            ctx.close()
            print("[완료] 이미 로그인 세션이 있습니다. 세션을 kakao_state.json 에 다시 저장했습니다.")
            status, err = _verify_mobile_session(p)
            if status == 200:
                print("  - [검증] 신청 화면(모바일 모드)에서도 세션 유지 확인 (HISTORY_API 200)")
            elif status == "launch_error":
                print(f"  - [안내] 신청 화면 검증은 건너뜀(창 실행 실패: {err}). run_auto.bat 에서 확인됩니다.")
            else:
                print(f"  - [경고] 신청 화면(모바일 모드)에서 세션이 인식되지 않습니다"
                      f"(HTTP {status if status is not None else '없음'}{(' ' + err) if err else ''}).")
                print("           run_login.bat 을 실행해 로그인을 다시 마쳐 주세요.")
                return 1
            print("       이어서 run_dry.bat / run_auto.bat 을 실행하세요.")
            return 0

        print()
        print("=" * 68)
        print(f" 1) 열린 브라우저 창은 [{where}] 입니다. (로그인 전용 창)")
        print("    카카오 로그인 화면이 자동으로 뜹니다. 카카오톡 QR / 아이디 로그인 모두 가능합니다.")
        print(" 2) 로그인을 마치면 자동으로 감지됩니다. (2초마다 확인, 최대 3분)")
        print("    기다리지 않으려면 이 검은 창에서 Enter 를 누르세요.")
        print(" 3) 창이 저절로 닫히고 [완료] 로그인 세션이 감지·저장되었습니다! 가 나오면 끝입니다.")
        print("=" * 68)
        print(f"  로그인 주소: {login_url}")
        print()
        try:
            page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            print(f"[경고] 로그인 페이지를 여는 중 지연이 있었습니다({type(exc).__name__}). 창을 직접 확인해 주세요.")

        ok, checks = _wait_for_login(page, ctx, LOGIN_WAIT_SECONDS)

        if ok:
            print("\r" + " " * 70 + "\r", end="")
            try:
                page.wait_for_timeout(1500)  # 쿠키 안정화 대기
            except Exception:
                pass
            saved = ""
            try:
                ctx.storage_state(path=STATE_FILE)
                saved = str(Path(STATE_FILE).resolve())
            except Exception as exc:
                saved = f"저장 실패({type(exc).__name__})"
            cookies = []
            try:
                cookies = ctx.cookies()
            except Exception:
                pass
            summary = _cookie_summary(cookies)
            ctx.close()
            print("[완료] 로그인 세션이 감지·저장되었습니다!")
            print(f"  - 프로필 폴더: {Path(PROFILE_DIR).resolve()}")
            print(f"  - 세션 백업: {saved}")
            print(f"  - 확인 횟수: {checks}회")
            print(f"  - 쿠키 도메인: {', '.join(f'{d}({n})' for d, n in sorted(summary.items())) or '없음'}")
            if mode == "desktop":
                # 데스크톱에서 만든 세션이 실제 신청용 모바일 화면에서도 통하는지 바로 확인
                status, err = _verify_mobile_session(p)
                if status == 200:
                    print("  - [검증] 신청 화면(모바일 모드)에서도 세션 유지 확인 (HISTORY_API 200)")
                elif status == "launch_error":
                    print(f"  - [안내] 신청 화면 검증은 건너뜀(창 실행 실패: {err}). run_auto.bat 에서 확인됩니다.")
                else:
                    print(f"  - [경고] 신청 화면(모바일 모드)에서 세션이 인식되지 않습니다"
                          f"(HTTP {status if status is not None else '없음'}{(' ' + err) if err else ''}).")
                    print("           이 경우 아래 명령으로 모바일 화면에서 직접 로그인해 주세요:")
                    print("             python main.py --login-mobile")
                    return 3
            print("  이제 run_dry.bat / run_auto.bat 을 실행하세요.")
            return 0

        # ---- 실패 진단 ----
        status, err = _login_probe(ctx)
        try:
            current_url = page.url
        except Exception:
            current_url = "(알 수 없음)"
        try:
            cookies = ctx.cookies()
        except Exception:
            cookies = []
        summary = _cookie_summary(cookies)
        ctx.close()

        print("\r" + " " * 70 + "\r", end="")
        print("[확인 필요] 로그인이 확인되지 않았습니다.")
        print(f"  - HISTORY_API 응답: HTTP {status if status is not None else '없음'}"
              f"{(' ' + err) if err else ''}")
        print(f"  - 현재 브라우저 주소: {current_url}")
        print(f"  - 저장된 쿠키: {', '.join(f'{d}({n})' for d, n in sorted(summary.items())) or '없음'}")
        print(f"    (핵심: gift.kakao.com {_domain_count(summary, 'gift.kakao.com')}개 / "
              f"kakao.com {_domain_count(summary, 'kakao.com')}개 / "
              f"shopping-userauth-web.kakao.com {_domain_count(summary, 'shopping-userauth-web.kakao.com')}개)")
        print("  - gift 세션은 로그인 후 gift.kakao.com/a/v1/web/login 를 거쳐야 만들어집니다.")
        print("    계정 로그인만 하고 그 화면에 머물렀다면 세션이 만들어지지 않습니다.")
        print("    위 주소가 accounts.kakao.com 이면 로그인이 끝나지 않은 상태입니다.")
        print("    run_login.bat 을 다시 실행해 로그인을 끝까지 마쳐 주세요.")
        return 1


# ---- 신청 화면 조작 헬퍼 ---------------------------------------------------
def _visible(loc) -> bool:
    try:
        return loc.count() > 0 and loc.first.is_visible()
    except Exception:
        return False


def _click_first(page, patterns, scope=None, timeout: int = 3000) -> str | None:
    """보이는 버튼/링크 중 패턴에 맞는 첫 요소를 클릭. 성공 시 사용한 텍스트 반환."""
    root = scope or page
    for pat in patterns:
        rx = re.compile(pat)
        for loc in (
            root.get_by_role("button", name=rx),
            root.locator("a", has_text=rx),
            root.locator("button", has_text=rx),
            root.locator("[role=button]", has_text=rx),
        ):
            try:
                if loc.count() == 0:
                    continue
                item = loc.first
                if not item.is_visible():
                    continue
                item.click(timeout=timeout)
                return pat
            except Exception:
                continue
    return None


def _json_get(page, url: str, referer: str = "") -> tuple:
    """브라우저 세션 쿠키를 그대로 쓴 JSON GET. 반환: (data, 오류문자열). 401 은 LoginRequired."""
    headers = {"User-Agent": MOBILE_UA, "Accept": "application/json, text/plain, */*"}
    if referer:
        headers["Referer"] = referer
    try:
        resp = page.context.request.get(url, headers=headers, timeout=20000)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {str(exc)[:80]}"
    if resp.status == 401:
        raise LoginRequired()
    if resp.status != 200:
        return None, f"HTTP {resp.status}"
    try:
        return resp.json(), ""
    except Exception:
        return None, "JSON 아님"


def fetch_campaign_detail(page, cid: str) -> tuple:
    """캠페인 상세를 읽어 옵션 목록·위시 필요 여부·신청 여부를 확보한다.

    신청 페이지(pw-trial-campaign-apply)와 상세 페이지가 쓰는 것과 같은 API 다.
    반환: (detail dict 또는 None, 오류문자열)
    """
    if not cid:
        return None, "campaignId 없음"
    data, err = _json_get(page, DETAIL_API.format(cid=cid), APPLY_PAGE.format(cid=cid))
    if not isinstance(data, dict) or not data.get("campaignId"):
        return None, err or "상세 응답 형식 아님"
    return data, ""


def option_choices(detail: dict | None) -> list:
    """상세 응답의 옵션 목록 → [(id, 이름)]"""
    out = []
    for opt in (detail or {}).get("options") or []:
        oid = opt.get("id")
        if oid is not None:
            out.append((oid, opt.get("value") or ""))
    return out


def apply_url(cid: str, detail: dict | None) -> tuple:
    """신청 페이지 주소. 옵션이 있는 캠페인은 optionId 를 query 로 넘겨야 신청이 성립한다.

    (번들 chunk-2BRB6BUS: apply 페이지는 옵션을 화면에서 고르지 않고 activatedRoute.queryParams
     의 optionId 만 읽는다. 없으면 selectedOption=null → POST body 에 optionId 가 빠져 서버가 거부한다.)
    반환: (url, 옵션 이름)
    """
    opts = option_choices(detail)
    base = APPLY_PAGE.format(cid=cid)
    if opts:
        return f"{base}?optionId={opts[0][0]}", str(opts[0][1])
    return base, ""


def _wish_state(page, product_id) -> bool | None:
    """위시 담김 여부(친구 공개 기준). 확인 못 하면 None."""
    if not product_id:
        return None
    data, _ = _json_get(page, WISH_STATE_API.format(pid=product_id), DETAIL_PAGE.format(cid=""))
    if not isinstance(data, dict):
        return None
    return data.get("wished") is True and (data.get("visibleTo") or "") == "others"


def _ensure_wish(page, detail: dict | None, enabled: bool = True) -> str:
    """체험단 신청 STEP 1 — 상품을 위시(선물함)에 담는다.

    신청 페이지(/apply)에는 위시 버튼이 없다. 위시는 상세 페이지에서만 담을 수 있고,
    상세 페이지는 위시하지 않은 상품의 신청 버튼을 레이어로 막는다.
    그래서 실제 사용자 순서와 같이 상세 페이지에서 담는다.
    """
    if not enabled:
        return "위시 단계 생략"
    if not detail:
        return "위시 확인 불가(상세 정보 없음)"
    if detail.get("wishable") is False:
        return "위시 대상 아님"

    cid = detail.get("campaignId") or ""
    pid = detail.get("productId")
    try:
        page.goto(DETAIL_PAGE.format(cid=cid), wait_until="domcontentloaded")
        page.wait_for_timeout(int(random.uniform(1000, 1600)))
    except Exception as exc:
        return f"위시 페이지 열기 실패({type(exc).__name__})"
    try:
        body = page.inner_text("body", timeout=4000)
    except Exception:
        body = ""

    if "위시 했어요" in body:
        return "이미 위시함"
    clicked = _click_first(page, [r"위시에\s*담기", r"위시\s*담기", r"찜하기"])
    if not clicked:
        if _wish_state(page, pid) is True:
            return "이미 위시함"
        return "위시 버튼 없음(위시 못 함)"

    page.wait_for_timeout(int(random.uniform(800, 1300)))
    _click_first(page, [r"친구\s*공개로\s*담기", r"^확인$", r"^담기$", r"^위시$"])
    page.wait_for_timeout(400)
    state = _wish_state(page, pid)
    if state is True:
        return "위시 담기 완료"
    if state is False:
        return "위시 담기 실패(서버 미반영)"
    return f"위시 담기 시도({clicked})"


def _ensure_default_address(page) -> str:
    """배송지는 사이트가 계정 기본 배송지로 자동 채운다.

    이미 채워져 있으면 아무 것도 건드리지 않는다. '배송지 변경하기'를 눌러 레이어를 열면
    신청 버튼이 가려져 오히려 실패한다. 비어 있을 때만 '배송지 선택하기'로 기본 배송지를 고른다.
    """
    try:
        body = page.inner_text("body", timeout=3000)
    except Exception:
        body = ""
    if "배송지" not in body:
        return "배송지 영역 없음(무시)"

    need_select = "입력된 배송지 정보가 없어요" in body or "배송지 선택하기" in body
    if not need_select:
        return "기본 배송지 자동 선택됨"

    clicked = _click_first(page, [r"배송지\s*선택하기", r"배송지\s*선택", r"주소\s*선택"])
    if not clicked:
        return "배송지 비어 있음(선택 버튼 못 찾음)"
    page.wait_for_timeout(800)

    picked = _click_first(page, [r"기본\s*배송지", r"^선택$", r"^배송지$"])
    if not picked:
        # 라디오 버튼 첫 항목이라도 선택
        try:
            radio = page.locator("input[type=radio]").first
            if radio.count() > 0:
                radio.check(force=True)
                picked = "라디오 첫 항목"
        except Exception:
            pass
    _click_first(page, [r"^확인$", r"^저장$", r"^적용$"])
    page.wait_for_timeout(500)
    return f"배송지 선택: {picked or '실패'}"


def apply_failure_hint(text: str) -> str:
    """신청 실패 화면에서 원인을 사람이 읽을 수 있는 문장으로 바꾼다."""
    if not text:
        return ""
    if APPLY_DENIED_TEXT in text:
        return "화면 팝업: 체험단에 응모하지 못했어요 / 고객센터로 문의(서버가 신청을 거부)"
    if "배송지 정보를 입력해주세요" in text:
        return "화면 팝업: 배송지 정보를 입력해주세요"
    if "옵션" in text and "optionId" in text:
        return "화면 오류: 옵션(optionId) 누락"
    return ""


def _check_agreements(page) -> int:
    """필수 동의 체크박스가 있으면 체크."""
    count = 0
    try:
        boxes = page.locator("input[type=checkbox]")
        for i in range(min(boxes.count(), 10)):
            box = boxes.nth(i)
            try:
                if box.is_visible() and not box.is_checked():
                    box.check(force=True)
                    count += 1
            except Exception:
                continue
    except Exception:
        pass
    return count


def _dump_failure(page, cid: str, tag: str = "fail"):
    LOG_DIR.mkdir(exist_ok=True)
    try:
        html = page.content()
        path = LOG_DIR / f"{tag}_{cid}_{datetime.now():%Y%m%d_%H%M%S}.html"
        path.write_text(html, encoding="utf-8")
        return path
    except Exception:
        return None


def apply_one(page, campaign: dict, skip_wishlist: bool = False) -> dict:
    """캠페인 1건 신청. 결과 dict 반환(status: 성공/스킵/실패/불확실)."""
    cid = campaign.get("campaignId") or ""
    result = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "campaignId": cid,
        "title": campaign.get("campaignTitle", ""),
        "brand": (campaign.get("brand") or {}).get("name", ""),
        "category": category_label(campaign),
        "applicationCount": campaign.get("applicationCount") or 0,
        "recruitmentCount": campaign.get("recruitmentCount") or 0,
        "status": "실패",
        "reason": "",
    }

    api_calls = []

    def on_response(resp):
        try:
            if APPLY_API_PATH in resp.url and resp.request.method == "POST":
                body = None
                try:
                    body = resp.json()
                except Exception:
                    try:
                        body = resp.text()[:300]
                    except Exception:
                        body = None
                payload = None
                try:
                    payload = resp.request.post_data_json
                except Exception:
                    pass
                api_calls.append({"status": resp.status, "body": body, "payload": payload})
        except Exception:
            pass

    page.on("response", on_response)
    try:
        # 0) 상세 정보: 신청 페이지가 쓰는 것과 같은 API 로 옵션·위시·신청 여부를 먼저 확인한다.
        detail, detail_err = fetch_campaign_detail(page, cid)
        if detail:
            if detail.get("applied") is True:
                result.update(status="스킵", reason="이미 신청한 체험단")
                return result
            if detail.get("campaignStatus") not in (None, "IN_PROGRESS"):
                result.update(status="스킵", reason=f"모집 중 아님({detail.get('campaignStatus')})")
                return result

        # 1) STEP 1: 위시 담기 — 신청 페이지에는 버튼이 없어 상세 페이지에서 해야 한다.
        wish = _ensure_wish(page, detail, enabled=not skip_wishlist)

        # 2) 신청 페이지: 옵션이 있는 캠페인은 optionId 를 query 로 넘겨야 신청이 성립한다.
        url, opt_name = apply_url(cid, detail)
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(int(random.uniform(1200, 2000)))

        try:
            body_text = page.inner_text("body", timeout=4000)
        except Exception:
            body_text = ""

        # 로그인이 플렸거나 세션이 만료된 경우
        if "accounts.kakao.com" in page.url or "/login" in page.url \
                or "giftbox-redirect" in page.url or "/unsup/" in page.url \
                or "카카오계정으로 로그인" in body_text or "로그인이 필요" in body_text \
                or "선물함 로그인 안내" in body_text or "카카오 계정으로 로그인하기" in body_text:
            raise LoginRequired()
        if "pc-restricted" in page.url:
            result.update(status="실패", reason="PC 제한 화면으로 이동(모바일 UA 확인 필요)")
            return result

        if "이미 신청" in body_text:
            result.update(status="스킵", reason="이미 신청한 체험단")
            return result
        if "모집이 마감" in body_text or "모집 마감" in body_text:
            result.update(status="스킵", reason="모집 마감")
            return result

        addr = _ensure_default_address(page)
        agree = _check_agreements(page)
        option_note = f"옵션:{opt_name[:18]}" if opt_name else "옵션 없음"
        if detail is None:
            option_note += f"(상세 조회 실패: {detail_err or '알 수 없음'})"

        clicked = _click_first(page, [
            r"신청\s*완료하기", r"체험단\s*신청하기", r"신청\s*완료", r"신청하기", r"^신청$",
        ])
        if not clicked:
            path = _dump_failure(page, cid)
            result.update(status="실패", reason=f"신청 버튼을 찾지 못함(화면 저장: {path})")
            return result

        page.wait_for_timeout(900)

        # 확인 모달 처리
        modal = None
        for sel in ("[role=dialog]", "div[class*=modal]", "div[class*=Modal]", "div[class*=dialog]"):
            loc = page.locator(sel)
            if _visible(loc):
                modal = loc.first
                break
        if modal is not None:
            _click_first(page, [r"^확인$", r"^동의하고 신청$", r"^신청$", r"^예$", r"^네$"], scope=modal)

        # 결과 확인: 신청 완료 페이지 이동 / 성공 응답 / 완료 문구
        deadline = time.time() + 12
        success = False
        while time.time() < deadline:
            if "/applied/" in page.url:
                success = True
                break
            if api_calls and api_calls[-1].get("status") in (200, 201):
                success = True
                break
            if api_calls and api_calls[-1].get("status") not in (200, 201) and api_calls[-1].get("status"):
                break
            page.wait_for_timeout(500)

        if not success:
            try:
                tail = page.inner_text("body", timeout=2500)
            except Exception:
                tail = ""
            if re.search(r"신청\s*(이)?\s*완료", tail) and APPLY_DENIED_TEXT not in tail:
                success = True
            elif "이미 신청" in tail:
                result.update(status="스킵", reason="이미 신청한 체험단")
                return result
        else:
            tail = ""

        if api_calls:
            last = api_calls[-1]
            (LOG_DIR).mkdir(exist_ok=True)
            try:
                (LOG_DIR / "last_apply_request.json").write_text(
                    json.dumps(api_calls[-3:], ensure_ascii=False, indent=1), encoding="utf-8"
                )
            except Exception:
                pass
            if success:
                result.update(status="성공",
                              reason=f"POST {last['status']} / 배송지:{addr} / {wish} / 동의{agree}건 / {option_note}")
            else:
                body = last.get("body")
                server_msg = ""
                if isinstance(body, dict):
                    server_msg = str(body.get("errorMessage") or body.get("message") or "")[:60]
                    if body.get("errorCode") is not None:
                        server_msg += f"(code {body.get('errorCode')})"
                hint = apply_failure_hint(tail) or "서버 응답에 표준 에러 봉투 없음(500·게이트웨이·차단 응답)"
                result.update(status="실패",
                              reason=f"신청 응답 {last['status']} {server_msg or str(body)[:80]} / {hint} / {option_note}")
            return result

        if success:
            result.update(status="성공",
                          reason=f"신청 완료 화면 확인 / 배송지:{addr} / {wish} / 동의{agree}건 / {option_note}")
        else:
            path = _dump_failure(page, cid)
            result.update(status="불확실", reason=f"결과 확인 실패(화면 저장: {path})")
        return result

    except LoginRequired:
        raise
    except Exception as exc:
        path = _dump_failure(page, cid)
        result.update(status="실패", reason=f"{type(exc).__name__}: {str(exc)[:100]}")
        return result
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass


def run_apply(candidates: list, args) -> int:
    from playwright.sync_api import sync_playwright

    if not candidates:
        print("신청할 후보가 없습니다.")
        return 0

    targets = candidates[: args.limit]
    print(f"\n[신청 시작] 후보 {len(candidates)}개 중 상위 {len(targets)}개 "
          f"(정렬: {'마감임박' if args.sort == 'deadline' else '인기순'})")

    results = []
    with sync_playwright() as p:
        ctx = _launch(p, headless=args.headless, mode="mobile")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        if not _ensure_session(ctx):
            status, err = _login_probe(ctx)
            try:
                cookies = ctx.cookies()
            except Exception:
                cookies = []
            summary = _cookie_summary(cookies)
            ctx.close()
            print(f"[중단] 로그인 세션이 없습니다 (HISTORY_API HTTP {status}{(' ' + err) if err else ''}).")
            print(f"       쿠키: {', '.join(f'{d}({n})' for d, n in sorted(summary.items())) or '없음'}")
            print(f"       (핵심: gift.kakao.com {_domain_count(summary, 'gift.kakao.com')}개 / "
                  f"kakao.com {_domain_count(summary, 'kakao.com')}개 / "
                  f"shopping-userauth-web.kakao.com {_domain_count(summary, 'shopping-userauth-web.kakao.com')}개)")
            print("       먼저 run_login.bat(데스크톱 로그인 창)을 실행해 로그인을 끝까지 마쳤 주세요.")
            print("       (체험단 화면에서 뜨는 '카카오 계정으로 로그인하기'로는 gift 세션이 만들어지지 않습니다.)")
            return 2

        for idx, item in enumerate(targets, 1):
            title = (item.get("campaignTitle") or "")[:36]
            print(f"\n({idx}/{len(targets)}) {title} … ", end="", flush=True)
            try:
                res = apply_one(page, item, skip_wishlist=args.skip_wishlist)
            except LoginRequired:
                print("로그인 필요 → 중단")
                print("[중단] 로그인이 풀렸습니다. run_login.bat(데스크톱 로그인 창)으로 다시 로그인한 뒤 재실행하세요.")
                break
            results.append(res)
            print(f"{res['status']} ({res['reason'][:80]})")
            if idx < len(targets):
                delay = random.uniform(args.delay_min, args.delay_max)
                time.sleep(delay)

        ctx.close()

    ok = sum(1 for r in results if r["status"] == "성공")
    skip = sum(1 for r in results if r["status"] == "스킵")
    fail = sum(1 for r in results if r["status"] in ("실패", "불확실"))
    print("\n" + "=" * 60)
    print(f"결과 요약: 성공 {ok}건 / 스킵 {skip}건 / 실패·불확실 {fail}건")
    if any("고객센터로 문의" in (r.get("reason") or "") for r in results):
        print("\n[안내] '체험단에 응모하지 못했어요 / 고객센터로 문의' 팝업이 뜬 건이 있습니다.")
        print("       이 문구는 신청 POST 가 서버에서 거부됐는데 표준 에러 응답이 아닐 때 뜼니다.")
        print("       사유 란의 '신청 응답 <status>' 와 logs/last_apply_request.json 을 확인해 주세요.")
    if results:
        path = save_results_csv(results)
        print(f"결과 파일: {path.resolve()}")
        print("\n[성공 목록]")
        for r in results:
            if r["status"] == "성공":
                print(f"  - {r['title']} [{r['brand']}]")
    print("=" * 60)
    return 0 if fail == 0 else 3


# ---------------------------------------------------------------------------
# 6. 진입점
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="카카오 선물하기 체험단 자동 신청 (모바일 에뮬레이션)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--login", action="store_true", help="최초 1회 카카오 로그인 후 세션 저장")
    ap.add_argument("--login-mobile", action="store_true",
                    help="--login 과 함께: 모바일(iPhone) 화면에서 로그인 (데스크톱 로그인으로 세션이 안 잡힐 때)")
    ap.add_argument("--dry-run", action="store_true", help="실제 신청 없이 후보 목록만 출력")
    ap.add_argument("--limit", type=int, default=30, help="실제 신청할 최대 개수 (기본 30)")
    ap.add_argument("--sort", choices=["popular", "deadline"], default="popular",
                    help="정렬 방식: popular(신청자 많은 순, 기본) / deadline(마감 임박 순)")
    ap.add_argument("--max-pages", type=int, default=30, help="목록 API 최대 페이지 수 (기본 30)")
    ap.add_argument("--delay-min", type=float, default=5.0, help="건별 최소 대기 초 (기본 5.0)")
    ap.add_argument("--delay-max", type=float, default=7.0, help="건별 최대 대기 초 (기본 7.0)")
    ap.add_argument("--headless", action="store_true", help="브라우저 창 없이 실행(디버깅 어려움)")
    ap.add_argument("--skip-wishlist", action="store_true",
                    help="위시(선물함)에 담는 단계를 건너뜀")
    ap.add_argument("--include-unknown", action="store_true",
                    help="카테고리 정보가 없는 상품까지 신청 대상에 포함(기본은 보류)")
    args = ap.parse_args(argv)

    if args.delay_min > args.delay_max:
        args.delay_min, args.delay_max = args.delay_max, args.delay_min

    if args.login or args.login_mobile:
        try:
            return run_login("mobile" if args.login_mobile else "desktop")
        except ImportError:
            print("[오류] playwright 가 설치되지 않았습니다. install.bat 을 먼저 실행하세요.")
            return 1
        except Exception as exc:
            hint = _browser_hint(exc)
            if hint:
                print(f"[오류] {hint}")
            else:
                print(f"[오류] 로그인 실행 실패: {type(exc).__name__}: {exc}")
            return 1

    flt = load_filter()
    items = collect_campaigns(args.max_pages)
    if not items:
        print("[안내] 진행 중인 체험단을 가져오지 못했습니다. 네트워크 또는 사이트 변경 여부를 확인하세요.")
        return 1

    applied = collect_applied_ids()
    candidates, held, denied = build_candidates(
        items, flt, args.sort, applied, include_unknown=args.include_unknown
    )

    if args.dry_run:
        print()
        print_table(candidates, args.limit, header="■ 신청 후보 (실제 신청 대상)")
        if held:
            print("\n※ 아래 상품은 카카오 목록에 카테고리 정보가 없어 자동 신청에서 빠졌습니다.")
            print("  (필요하면 --include-unknown 옵션으로 함께 신청할 수 있습니다.)")
            print_table(held, len(held), header="■ 보류 목록 (참고용)")
        if candidates:
            path = save_candidates_csv(candidates, args.limit, held)
            print(f"후보 목록 저장: {path.resolve()}")
        print("\n※ dry-run 모드입니다. 실제 신청은 하나도 하지 않았습니다.")
        print("※ 실제 신청: run_auto.bat (또는 python main.py --limit 30)")
        return 0

    if held and not args.include_unknown:
        print(f"※ 카테고리 미확인 {len(held)}개는 자동 신청에서 제외했습니다.")

    try:
        return run_apply(candidates, args)
    except ImportError:
        print("[오류] playwright 가 설치되지 않았습니다. install.bat 을 먼저 실행하세요.")
        return 1
    except Exception as exc:
        hint = _browser_hint(exc)
        print(f"[오류] {hint or f'{type(exc).__name__}: {exc}'}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
