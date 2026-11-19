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
    로그인 세션은 ./kakao_profile 폴더(브라우저 프로필)에만 남습니다.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
import urllib.error
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

# 모바일 UA 필수: 데스크톱 UA로 접근하면 PC 제한(/pc-restricted) 화면으로 막힐 수 있음
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
DEVICE_CANDIDATES = ["iPhone 14", "iPhone 13", "iPhone 12"]
PROFILE_DIR = "kakao_profile"
LOG_DIR = Path("logs")

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
def fetch_json(url: str, timeout: int = 20):
    """(status, data, error) 반환. 로그인 관련 401 은 예외 대신 상태코드로 돌려준다."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": MOBILE_UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": BASE + "/giftbox/trial-campaign",
        },
    )
    try:
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
        status, data, err = fetch_json(f"{HISTORY_API}?page={page}&size=50")
        if status == 401:
            print("[안내] 신청 이력 조회는 로그인이 필요합니다(세션 없음/만료).")
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


def _launch(p, headless: bool):
    """모바일 에뮬레이션 + 영구 프로필(로그인 세션 유지)."""
    from playwright.sync_api import sync_playwright  # noqa: F401  (문서화용)

    device = dict(_pick_device(p))
    device.pop("default_browser_type", None)
    ctx = p.chromium.launch_persistent_context(
        str(Path(PROFILE_DIR).resolve()),
        headless=headless,
        locale="ko-KR",
        timezone_id="Asia/Seoul",
        args=["--disable-blink-features=AutomationControlled"],
        **device,
    )
    ctx.set_default_timeout(8000)
    return ctx


def _logged_in(ctx) -> bool:
    try:
        resp = ctx.request.get(
            f"{HISTORY_API}?page=0&size=1",
            headers={"User-Agent": MOBILE_UA, "Referer": BASE + "/giftbox/trial-campaign"},
        )
        return resp.status == 200
    except Exception:
        return False


def _browser_hint(exc) -> str:
    """playwright 가 내는 불친절한 오류를 사용자용 문장으로 바꾼다."""
    msg = str(exc)
    if "Executable doesn't exist" in msg or "playwright install" in msg:
        return "브라우저(크로미움)가 설치되지 않았습니다. install.bat 을 먼저 실행해 주세요."
    if "Host system is missing dependencies" in msg:
        return "브라우저 실행에 필요한 시스템 파일이 없습니다(리눅스: playwright install-deps)."
    return ""


def run_login() -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = _launch(p, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(BASE + "/giftbox/trial-campaign", wait_until="domcontentloaded")
        print()
        print("=" * 64)
        print(" 1) 열린 브라우저 창에서 [카카오 로그인]을 직접 진행해 주세요.")
        print(" 2) 로그인이 끝나면 이 검은 창으로 돌아와 Enter 키를 누르세요.")
        print("    (브라우저 창은 Enter를 누르면 자동으로 닫힙니다)")
        print("=" * 64)
        try:
            input("로그인 완료 후 Enter >>> ")
        except (EOFError, KeyboardInterrupt):
            pass

        ok = _logged_in(ctx)
        if not ok:
            # 쿠키가 늦게 저장되는 경우가 있어 한 번 더 확인
            page.wait_for_timeout(1500)
            ok = _logged_in(ctx)
        ctx.close()

    if ok:
        print("[완료] 로그인 세션이 저장되었습니다. 이제 run_dry.bat / run_auto.bat 을 실행하세요.")
        return 0
    print("[확인 필요] 로그인이 확인되지 않았습니다. 카카오 로그인을 끝까지 마쳤는지 확인한 뒤")
    print("           run_login.bat 을 다시 실행해 주세요.")
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


def _ensure_default_address(page) -> str:
    """기본 배송지가 선택되어 있는지 확인하고, 선택 창이 뜨면 기본 배송지를 고른다."""
    try:
        body = page.inner_text("body", timeout=3000)
    except Exception:
        body = ""
    if "배송지" not in body:
        return "배송지 영역 없음(무시)"

    # 이미 주소가 표시되어 있으면 그대로 사용
    if re.search(r"(로|길|동|읍|면)\s*\d", body):
        return "기본 배송지 표시됨"

    clicked = _click_first(page, [r"배송지\s*선택", r"배송지\s*변경", r"주소\s*선택"])
    if not clicked:
        return "배송지 선택 버튼 없음"
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


def _ensure_wishlist(page, enabled: bool = True) -> str:
    """
    선물함(위시) 담기 단계가 있는 화면이면 처리한다.
    실제 앱 사용 순서가 [위시에 담기] → [체험단 신청하기] 인 경우가 있어 best-effort 로 시도한다.
    버튼이 없으면 아무 것도 하지 않는다.
    """
    if not enabled:
        return "위시 단계 생략"
    clicked = _click_first(page, [r"위시에\s*담기", r"위시\s*담기", r"찜하기"])
    if not clicked:
        return "위시 버튼 없음"
    page.wait_for_timeout(800)
    _click_first(page, [r"친구\s*공개", r"전체\s*공개", r"공개로\s*담기"])
    _click_first(page, [r"^확인$", r"^담기$", r"^저장$"])
    page.wait_for_timeout(500)
    return f"위시 담기 시도({clicked})"


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
        page.goto(APPLY_PAGE.format(cid=cid), wait_until="domcontentloaded")
        page.wait_for_timeout(int(random.uniform(1200, 2000)))

        try:
            body_text = page.inner_text("body", timeout=4000)
        except Exception:
            body_text = ""

        # 로그인이 플렸거나 세션이 만료된 경우
        if "accounts.kakao.com" in page.url or "/login" in page.url \
                or "카카오계정으로 로그인" in body_text or "로그인이 필요" in body_text:
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
        wish = _ensure_wishlist(page, enabled=not skip_wishlist)
        agree = _check_agreements(page)

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
            if re.search(r"신청\s*(이)?\s*완료", tail):
                success = True
            elif "이미 신청" in tail:
                result.update(status="스킵", reason="이미 신청한 체험단")
                return result

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
                result.update(status="성공", reason=f"POST {last['status']} / 배송지:{addr} / {wish} / 동의{agree}건")
            else:
                result.update(status="실패", reason=f"신청 응답 {last['status']} {str(last.get('body'))[:120]}")
            return result

        if success:
            result.update(status="성공", reason=f"신청 완료 화면 확인 / 배송지:{addr} / {wish} / 동의{agree}건")
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
        ctx = _launch(p, headless=args.headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        if not _logged_in(ctx):
            ctx.close()
            print("[중단] 로그인 세션이 없습니다. 먼저 run_login.bat(python main.py --login)을 실행하세요.")
            return 2

        for idx, item in enumerate(targets, 1):
            title = (item.get("campaignTitle") or "")[:36]
            print(f"\n({idx}/{len(targets)}) {title} … ", end="", flush=True)
            try:
                res = apply_one(page, item, skip_wishlist=args.skip_wishlist)
            except LoginRequired:
                print("로그인 필요 → 중단")
                print("[중단] 로그인이 풀렸습니다. run_login.bat 으로 다시 로그인한 뒤 재실행하세요.")
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
    ap.add_argument("--dry-run", action="store_true", help="실제 신청 없이 후보 목록만 출력")
    ap.add_argument("--limit", type=int, default=30, help="실제 신청할 최대 개수 (기본 30)")
    ap.add_argument("--sort", choices=["popular", "deadline"], default="popular",
                    help="정렬 방식: popular(신청자 많은 순, 기본) / deadline(마감 임박 순)")
    ap.add_argument("--max-pages", type=int, default=30, help="목록 API 최대 페이지 수 (기본 30)")
    ap.add_argument("--delay-min", type=float, default=1.5, help="건별 최소 대기 초 (기본 1.5)")
    ap.add_argument("--delay-max", type=float, default=3.0, help="건별 최대 대기 초 (기본 3.0)")
    ap.add_argument("--headless", action="store_true", help="브라우저 창 없이 실행(디버깅 어려움)")
    ap.add_argument("--skip-wishlist", action="store_true",
                    help="위시(선물함)에 담는 단계를 건너뜀")
    ap.add_argument("--include-unknown", action="store_true",
                    help="카테고리 정보가 없는 상품까지 신청 대상에 포함(기본은 보류)")
    args = ap.parse_args(argv)

    if args.delay_min > args.delay_max:
        args.delay_min, args.delay_max = args.delay_max, args.delay_min

    if args.login:
        try:
            return run_login()
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
