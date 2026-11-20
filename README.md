# 🎁 카카오 선물하기 체험단 자동 신청 (모바일 에뮬레이션)

카카오톡 **선물하기 체험단**을 아이폰(모바일)처럼 접속해서, 신엽 님 기준에 맞는 상품 상위 30개를 자동으로 신청해 주는 프로그램입니다.

- 컴퓨터: **윈도우** (맥·리눅스도 동일하게 동작)
- 준비물: 파이썬(Python) 1개, 카카오 계정 1개
- 로그인 비밀번호는 **어디에도 저장되지 않습니다.** 브라우저 프로필(`kakao_profile` 폴더)과 세션 백업(`kakao_state.json`)에 로그인 상태만 남습니다.

---

## 1. 처음 한 번만 하면 되는 설치 (약 5분)

### 1-1. 파이썬 설치
1. https://www.python.org/downloads/ 접속 → **Download Python 3.x** 클릭
2. 설치 파일 실행 → **맨 아래 "Add python.exe to PATH" 체크박스를 반드시 체크** → Install Now

### 1-2. 이 폴더 내려받기
- GitHub 페이지 오른쪽 위 **Code → Download ZIP** → 압축 풀기 (예: `C:\kakao-gift-trial-auto`)
- 압축을 푼 폴더 안에 `install.bat` 이 보이면 정상입니다.

### 1-3. 설치 실행
- **`install.bat` 을 더블클릭** 하세요.
- 검은 창이 뜨고 자동으로 필요한 프로그램을 내려받습니다. (3~10분, 인터넷 필요)
- 마지막에 `설치가 끝났습니다` 가 보이면 완료. 아무 키나 눌러 창을 닫습니다.

> 설치 중 빨간 글씨(에러)가 보이면 그 화면을 캡처해서 알려 주세요.

---

## 2. 최초 1회 로그인 (딱 한 번)

**`run_login.bat` 더블클릭**

1. **데스크톱 크기의 브라우저 창**이 열리고, 카카오 로그인 화면으로 자동 이동합니다. (로그인 전용 창)
2. 창 안에서 **카카오 로그인**을 끝까지 진행합니다. (카카오톡 QR 로그인 / 아이디 로그인 모두 가능)
3. 로그인이 끝나면 **자동으로 감지**됩니다. (2초마다 확인, 최대 3분)
   기다리지 않으려면 검은 창에서 **Enter** 를 누르면 바로 확인합니다.
4. `[완료] 로그인 세션이 감지·저장되었습니다!` 가 나오면 끝입니다. 창은 저절로 닫힙니다.
5. 이어서 **신청 화면(모바일 모드)에서도 세션이 유지되는지 자동으로 한 번 더 확인**합니다.
   `[검증] 신청 화면(모바일 모드)에서도 세션 유지 확인` 이 보이면 정상입니다.
   `[경고] … 세션이 인식되지 않습니다` 가 나오면 아래 2-1 을 실행하세요.

> 로그인 상태는 보통 몇 주간 유지됩니다. 신청이 안 되고 "로그인 세션이 없습니다" 가 나오면 이 단계를 다시 하면 됩니다.

### 2-1. (드물게) 모바일 화면에서 세션이 인식되지 않을 때

```bat
python main.py --login-mobile
```

데스크톱 창 대신 **모바일(iPhone) 화면**에서 같은 로그인을 진행합니다. 로그인 주소는 자동으로 뜨는 것을 그대로 쓰면 됩니다.

> ⚠️ **체험단 화면 안에서 로그인하지 마세요.** 체험단 페이지는 모바일 전용이라 모바일 화면으로 열리는데,
> 그 화면의 로그인 버튼은 **카카오톡 앱으로 열기**로 연결됩니다. PC에서는 앱이 열리지 않아 로그인이 끝나지 않고,
> "카카오 계정으로 로그인하기"로 계정에 들어가도 **선물하기 로그인 상태는 만들어지지 않습니다.**
> 그래서 로그인만은 데스크톱 창에서 합니다. 로그인한 세션은 신청(모바일 화면)에서 그대로 이어집니다.

---

## 3. 매번 사용하는 방법

| 파일 | 하는 일 |
|---|---|
| **`run_dry.bat`** | 실제 신청 없이 **오늘 후보 TOP 30 목록만 표로 보여 줍니다.** (처음엔 이걸로 확인) |
| **`run_auto.bat`** | 후보 상위 **30개를 실제로 신청**합니다. |

### 실행 순서
1. `run_dry.bat` 더블클릭 → 표에 뜬 상품 목록 확인
2. 마음에 들면 `run_auto.bat` 더블클릭 → 실제 신청 진행 (상품당 2~3초 간격, 30개면 약 2~3분)
3. 끝나면 **성공/스킵/실패 요약**과 `logs\results_날짜_시간.csv` 결과 파일이 생성됩니다.

> 상품당 처리 순서: 신청 페이지 열기 → **위시(선물함) 담기**(버튼이 있는 경우) → 기본 배송지 확인 → 동의 체크 → [신청] 클릭 → 확인 창 처리 → 결과 확인

> 신청은 **한 번에 30개까지**가 기본값입니다. 개수를 바꾸려면 `run_auto.bat` 을 메모장으로 열어
> `python main.py --limit 30` 의 숫자를 고치면 됩니다. (예: `--limit 10`)

---

## 4. 어떤 상품을 신청하나요? (필터 기준)

`config.json` 을 메모장으로 열면 직접 바꿀 수 있습니다.

**신청하는 카테고리**
- 식품·디저트 / 디지털·소형가전 / 리빙·생활용품 / 패션·잡화 / 키즈·아동용품 / 캐릭터 굿즈

**신청하지 않는 카테고리**
- 뷰티·화장품(향수·바디 포함), 골프·레저·스포츠
- 신생아·임신·출산 용품, 반려동물(강아지·고양이) 용품, 뷰티가전

**제외 키워드** (상품명에 들어가면 제외)
신생아, 배냇저고리, 딸랑이, 치발기, 젖병, 분유, 아기띠, 임산부, 산모, 수유, 이유식, 기저귀,
반려동물, 강아지, 고양이, 사료, 캣타워, 탈모, 염색, 립스틱, 틴트, 파운데이션, 아이섀도,
선크림, 세럼, 앰플, 마스크팩, 골프, 스포츠, 등산, 헬스 …

**정렬**
- 기본은 **인기순**(신청자 수 많은 순). 마감 임박 순으로 보려면 `--sort deadline` 을 붙입니다.

---

## 5. 명령어로 직접 실행하고 싶을 때

```bat
python main.py --login
python main.py --dry-run
python main.py --limit 30
python main.py --limit 10 --sort deadline
python main.py --dry-run --max-pages 5
```

| 옵션 | 설명 | 기본값 |
|---|---|---|
| `--login` | 카카오 로그인 후 세션 저장 (데스크톱 창) | - |
| `--login-mobile` | `--login` 과 같지만 모바일(iPhone) 창에서 로그인 | - |
| `--dry-run` | 실제 신청 없이 후보 목록만 출력 | - |
| `--limit N` | 신청할 개수 | 30 |
| `--sort popular\|deadline` | 인기순 / 마감임박순 | popular |
| `--max-pages N` | 목록을 몇 페이지까지 읽을지(1페이지=50개) | 30 |
| `--delay-min` / `--delay-max` | 건별 대기 시간(초) | 1.5 / 3.0 |
| `--include-unknown` | 카테고리 정보가 없는 상품까지 신청 (기본은 보류) | 꺼짐 |
| `--skip-wishlist` | 상품을 위시(선물함)에 담는 단계를 건너뜀 | 꺼짐(담기 시도) |
| `--headless` | 창 없이 실행 | 꺼짐 |

---

## 6. 잘 안 될 때

| 증상 | 원인 / 해결 |
|---|---|
| `로그인 세션이 없습니다` | `run_login.bat` 다시 실행 |
| 체험단 화면에서 자꾸 로그아웃 / 로그인 버튼이 "카카오톡 앱에서 보기"로만 뜸 | 모바일 화면의 로그인은 카카오톡 앱 전용입니다. `run_login.bat`(데스크톱 창)으로 로그인하세요. |
| `로그인이 확인되지 않았습니다` + `HTTP 401` | 로그인 화면에서 로그인을 끝까지 마치지 않은 경우입니다. 출력된 `현재 브라우저 주소`가 `accounts.kakao.com` 이면 로그인 미완료입니다. |
| `[경고] 신청 화면(모바일 모드)에서 세션이 인식되지 않습니다` | `python main.py --login-mobile` 로 모바일 화면에서 로그인하세요. |
| `신청 버튼을 찾지 못함` | 카카오가 화면을 바꾼 경우입니다. `logs\fail_*.html` 파일이 생깁니다 → 알려 주시면 바로 고칩니다. |
| 목록이 0개 | 인터넷 연결 확인. 카카오가 API 주소를 바꾼 경우도 있습니다. |
| `PC 제한 화면으로 이동` | 모바일 설정이 빠진 경우입니다. 프로그램을 최신 버전으로 받아 주세요. |
| `브라우저(크로미움)가 설치되지 않았습니다` | `install.bat` 을 다시 실행해 주세요. |
| 신청은 됐는데 `불확실` | 결과 확인만 실패한 경우입니다. 선물하기 앱 → 체험단 → **신청내역**에서 확인하세요. |

---

## 7. 파일 구조

```
main.py            실제 프로그램
config.json        필터 기준(카테고리·키워드) — 메모장으로 수정 가능
requirements.txt   필요한 파이썬 패키지 목록
install.bat        최초 설치 (윈도우)
run_login.bat      최초 1회 로그인 (윈도우)
run_dry.bat        후보 목록 확인 (윈도우)
run_auto.bat       실제 신청 (윈도우)
*.sh               맥·리눅스용 동일 명령
kakao_profile/     로그인 세션 보관 폴더 (자동 생성, 공유 금지)
kakao_state.json   로그인 세션 백업 (자동 생성, 공유 금지)
logs/              결과 CSV · 실패 화면 저장 (자동 생성)
tests/selftest.py  개발자용 자체 점검 (로그인 없이 로직만 확인)
```

---

## 8. 알아 둘 점 (꼭 읽어 주세요)

- 이 프로그램은 **본인 계정 1개**로만 쓰도록 만들어졌습니다. 여러 계정을 돌리거나 짧은 시간에 반복 실행하면 카카오 이용 제한을 받을 수 있습니다.
- 자동 신청은 카카오 서비스 구조가 바뀌면 언제든 동작이 달라질 수 있습니다. 그때는 `logs` 폴더를 보고 고치면 됩니다.
- 당첨되면 배송은 **선물하기에 저장된 기본 배송지**로 갑니다. 실행 전에 앱에서 기본 배송지가 맞는지 한 번 확인하세요.
- 받은 체험 상품은 카카오 안내에 따라 **후기(리뷰) 작성** 의무가 있을 수 있습니다.

---

## 9. API 메모 (개발자용, 2026-11-20 실측)

| 용도 | 요청 | 인증 |
|---|---|---|
| 진행 중 목록 | `GET /a/partner-trial/api/v1/trial-campaigns?status=IN_PROGRESS&page=0&size=20&sortProperty=CREATED_AT&sortDir=DESC` | 불필요 |
| 신청 이력 | `GET /a/partner-trial/api/v1/trial-campaigns/apply?page=0&size=50` | 로그인 쿠키 |
| 신청 | `POST /a/partner-trial/api/v1/trial-campaigns/apply` | 로그인 쿠키 |
| 상세 | `GET /a/partner-trial/api/v1/trial-campaigns/{campaignId}` | 로그인 쿠키 |

- `sortProperty=APPLY_COUNT` 는 **HTTP 400** 을 돌려줍니다 → `CREATED_AT` 으로 전부 받아 로컬 정렬.
- 목록 응답 필드: `campaignId`, `campaignTitle`, `brand.name`, `productId`, `imageUrl`,
  `displayCategoryViews[]`(`ldisplayCategoryId`, `ldisplayCategoryName`, `mdisplayCategoryId`, `mdisplayCategoryName`),
  `remainingDays`, `recruitmentCount`, `applicationCount` + 최상위 `page`, `size`, `totalCount`, `isLast`
- 실제 대분류 ID: 식품 93·250 / 리빙 94 / 뷰티 95·243 / 향수·바디 244 / 패션 96 / 디지털·가전 97 / 유아동·반려 98 / 레저·스포츠 99
- 모바일 UA 필수 (데스크톱 UA → `/giftbox/trial-campaign/pc-restricted` 차단)

**로그인 구조 (2026-11-20 실측)** — `run_login` 이 데스크톱 UA 를 쓰는 이유

- gift 인증 서비스(`chunk-ZOOQ4WOZ.js`): `login()` 은 `isKakaotalk || isMobileOrTablet` 이면 `loginApp()`,
  아니면 `getWebAuthUrl()` 로 갈라집니다. 모바일 UA 에서는 `/giftbox-redirect`(카카오톡 앱 안내)로 빠지고,
  그 화면의 "카카오 계정으로 로그인하기"는 `accounts.kakao.com/login?continue=…/unsup/giftbox` 로 가서
  gift 세션을 만들지 못합니다(→ 체험단 페이지는 계속 로그아웃).
- 웹 로그인(=gift 세션 발급) 경로:
  `https://shopping-userauth-web.kakao.com/login?channel=GIFT&redirect_url=<enc(https://gift.kakao.com/a/v1/web/login?redirectUrl=<enc(대상)>)>`
  → `kauth.kakao.com/oauth/authorize?client_id=98020c3772c56cf7faa6aceb97b0722d&redirect_uri=…/callback&state={redirectUrl,channel:GIFT}`
  → `accounts.kakao.com/login?continue=<kauth>` → 로그인 후 `gift.kakao.com/a/v1/web/login` → 세션 쿠키 발급 → 대상 페이지로 복귀
- 위 체인은 UA 와 무관하게 동작하지만, 화면·안내가 데스크톱 기준인 쪽이 로그인이 확실해 `run_login` 만 데스크톱 UA 로 열고
  쿠키는 같은 `kakao_profile` 에 저장해 모바일 UA 신청 단계에서 그대로 사용합니다(쿠키는 UA 와 무관).
