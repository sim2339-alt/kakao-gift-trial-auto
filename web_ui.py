# -*- coding: utf-8 -*-
"""
카카오 선물하기 체험단 — 내 PC용 웹 조종판 (표준 라이브러리만 사용, 추가 설치 없음)

    python web_ui.py                 # http://127.0.0.1:8765 를 띄우고 브라우저를 연다
    python web_ui.py --port 9000     # 다른 포트
    python web_ui.py --no-browser    # 브라우저를 자동으로 열지 않음

안전
  - 127.0.0.1(이 PC)에만 열립니다. 같은 와이파이의 다른 기기에서도 접속되지 않습니다.
  - 노션 토큰·카카오 비밀번호는 화면으로 내보내지 않습니다(열 이름만 읽고 씁니다).
  - 실제 신청은 main.py 의 같은 함수를 그대로 불러 실행합니다(터미널 실행과 동일한 동작).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import main

APP_VERSION = "2.2"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
LOG_MAX = 4000          # 로그 창에 보관할 최대 줄 수
ROOT = Path(__file__).resolve().parent


def now_text() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# 0. 상태 (실행 로그·작업 상태) — 한 프로세스 안에서만 산다
# ---------------------------------------------------------------------------
STATE = {
    "lock": threading.Lock(),
    "job": None,             # {"kind": "apply", "thread": Thread, "argv": [...]}
    "console": [],           # 작업 로그 줄
    "phase": "idle",         # idle | running
    "started_at": None,
    "finished_at": None,
    "summary": None,         # {"rc": 0, "ok": 3, "skip": 0, "fail": 0, "notion": (2, 0)}
    "proc": None,            # 로그인 창 서브프로세스
    "login": None,           # 마지막으로 확인한 로그인 상태
    "login_checked_at": None,
    "columns": None,         # 노션 참가자 열 목록(한 번 읽어 캐시)
}


def reset_state():
    """테스트·재실행용 상태 초기화."""
    with STATE["lock"]:
        STATE.update({"job": None, "console": [], "phase": "idle", "started_at": None,
                      "finished_at": None, "summary": None, "proc": None,
                      "login": None, "login_checked_at": None, "columns": None})


def add_line(line: str):
    text = (line or "").rstrip()
    if not text:
        return
    with STATE["lock"]:
        STATE["console"].append(text)
        if len(STATE["console"]) > LOG_MAX:
            del STATE["console"][: len(STATE["console"]) - LOG_MAX]


class LineWriter(io.TextIOBase):
    """print() 출력을 줄 단위로 STATE['console'] 에 옮긴다(여러 스레드가 써도 줄이 깨지지 않게 잡금)."""

    def __init__(self, sink):
        self._sink = sink
        self._buf = ""
        self._lock = threading.Lock()

    def write(self, s):
        with self._lock:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self._sink(line)
        return len(s)

    def flush(self):
        with self._lock:
            if self._buf:
                self._sink(self._buf)
                self._buf = ""

    def isatty(self):
        return False


def log_payload(since: int = 0) -> dict:
    with STATE["lock"]:
        lines = list(STATE["console"])
        job = STATE["job"]
        running = bool(job and job.get("thread") and job["thread"].is_alive()) \
            or STATE["phase"] == "running"
        payload = {
            "lines": lines[max(0, int(since)):],
            "total": len(lines),
            "running": running,
            "phase": STATE["phase"],
            "started_at": STATE["started_at"],
            "finished_at": STATE["finished_at"],
            "summary": STATE["summary"],
        }
    return payload


# ---------------------------------------------------------------------------
# 1. 노션 설정 · 상태
# ---------------------------------------------------------------------------
def secret_path() -> Path:
    return Path(main.NOTION_SECRET_FILE)


def read_secret() -> dict:
    """notion_secret.json 을 그대로 읽는다(토큰은 화면으로 내보내지 않는다)."""
    p = secret_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_column(column: str) -> dict:
    """참가자 열 이름만 notion_secret.json 에 저장한다(토큰·DB ID 는 그대로 둔다)."""
    column = (column or "").strip()
    if not column:
        return {"ok": False, "error": "열 이름이 비어 있습니다."}
    p = secret_path()
    if not p.is_file():
        return {"ok": False, "error": "notion_secret.json 이 없습니다. PC에서 python main.py --setup-notion 으로 먼저 설정해 주세요."}
    data = read_secret()
    if not data:
        return {"ok": False, "error": "notion_secret.json 을 읽지 못했습니다(형식 확인)."}
    data["column"] = column
    try:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        return {"ok": False, "error": f"저장 실패({type(exc).__name__})"}
    add_line(f"[설정] 참가자 열을 '{column}' 로 저장했습니다.")
    return {"ok": True, "column": column}


def notion_columns(force: bool = False) -> list:
    """노션 DB 스키마에서 참가자 열 후보를 읽는다(한 번 읽어 캐시)."""
    if STATE["columns"] and not force:
        return STATE["columns"]
    cfg = main.load_notion_config()
    if not cfg:
        return []
    cols = main.notion_list_columns(cfg["db_id"], cfg["token"])
    if cols:
        STATE["columns"] = cols
    return cols or []


def check_login() -> dict:
    state = main.login_state()
    with STATE["lock"]:
        STATE["login"] = state
        STATE["login_checked_at"] = now_text()
    return state


def api_status() -> dict:
    raw = read_secret()
    cfg = main.load_notion_config()
    notion = {
        "enabled": bool(cfg),
        "column": (cfg or {}).get("column") or raw.get("column"),
        "columns": notion_columns() if cfg else [],
        "db_tail": (str(raw.get("db_id") or "")[-6:] or None),
        "file": bool(secret_path().is_file()),
    }
    with STATE["lock"]:
        login = STATE["login"]
        checked_at = STATE["login_checked_at"]
    if login is None:
        login = {"state": "unknown", "detail": "아직 확인하지 않았습니다."}
    job = log_payload(0)
    job.pop("lines", None)
    return {
        "version": APP_VERSION,
        "login": {**login, "checked_at": checked_at},
        "notion": notion,
        "job": job,
    }


# ---------------------------------------------------------------------------
# 2. 미리보기 (신청 없이 후보만 선정)
# ---------------------------------------------------------------------------
def plan_summary(mode: str, stat: dict, total: int, applied_checked: bool) -> str:
    if mode == "notion":
        parts = [f"노션 DB {stat.get('rows', 0)}행",
                 f"내 열 체크 완료 {stat.get('checked', 0)}행 제외",
                 f"미체크 {stat.get('pending', 0)}건",
                 f"지금 모집 중 {stat.get('live', 0)}건",
                 f"모집 종료·삭제 {stat.get('ended', 0)}건 제외"]
        if stat.get("deny"):
            parts.append(f"카테고리 기준 제외 {stat['deny']}건")
        if stat.get("hold"):
            parts.append(f"카테고리 미확인 보류 {stat['hold']}건")
        if stat.get("applied"):
            parts.append(f"이미 신청 {stat['applied']}건 제외")
        if not applied_checked:
            parts.append("신청 이력 확인 불가(로그인 필요)")
        return " · ".join(parts) + f" → 신청 대상 {total}건"
    return (f"카카오 목록 허용 {total}건 (보류 {stat.get('held', 0)} · "
            f"제외 {stat.get('denied', 0)}) → 신청 대상 {total}건")


def preview(mode: str = "notion", limit: int = 30, column: str | None = None,
            sort: str = "popular", include_unknown: bool = False,
            notion_all: bool = False, max_pages: int = 30) -> dict:
    """후보만 읽어 온다(실제 신청 없음). 오류는 dict 로 돌려준다."""
    mode = "notion" if mode == "notion" else "list"
    limit = max(1, int(limit or 30))
    notion_cfg = None
    if mode == "notion":
        notion_cfg = main.load_notion_config(column=column)
        if not notion_cfg:
            return {"ok": False, "error": "노션 연동 설정(notion_secret.json)이 없습니다. "
                                          "PC에서 python main.py --setup-notion 으로 먼저 설정해 주세요."}
    items = main.collect_campaigns(max_pages)
    if not items:
        return {"ok": False, "error": "진행 중인 체험단을 가져오지 못했습니다. 인터넷 연결을 확인해 주세요."}
    applied = main.collect_applied_ids()
    flt = main.load_filter()
    if mode == "notion":
        rows = main.notion_rows(notion_cfg)
        if rows is None:
            return {"ok": False, "error": "노션 DB 를 읽지 못했습니다(토큰·DB ID·연결 권한 확인)."}
        picked, held, stat = main.select_notion_targets(
            rows, items, applied=applied, flt=flt, sort_mode=sort,
            include_unknown=include_unknown, ignore_categories=notion_all)
    else:
        picked, held, denied = main.build_candidates(
            items, flt, sort, applied, include_unknown=include_unknown)
        stat = {"passed": len(picked), "held": len(held), "denied": denied}
    return {
        "ok": True, "mode": mode, "limit": limit, "total": len(picked),
        "applied_checked": applied is not None,
        "summary": plan_summary(mode, stat, len(picked), applied is not None),
        "stat": stat,
        "items": [main.candidate_row(i) for i in picked[:limit]],
        "held": [main.candidate_row(i) for i in held[:20]],
    }


# ---------------------------------------------------------------------------
# 3. 실행 · 중단 · 로그인 창
# ---------------------------------------------------------------------------
def build_argv(payload: dict) -> list:
    mode = "notion" if (payload.get("mode") or "notion") == "notion" else "list"
    argv = []
    if mode == "notion":
        argv.append("--notion-pending")
        if payload.get("notion_all"):
            argv.append("--notion-all")
    argv += ["--limit", str(max(1, int(payload.get("limit") or 30)))]
    if payload.get("column"):
        argv += ["--notion-column", str(payload["column"]).strip()]
    if str(payload.get("sort") or "popular") == "deadline":
        argv += ["--sort", "deadline"]
    if payload.get("include_unknown"):
        argv.append("--include-unknown")
    if payload.get("skip_wishlist"):
        argv.append("--skip-wishlist")
    for key, opt in (("delay_min", "--delay-min"), ("delay_max", "--delay-max")):
        if payload.get(key) is not None and str(payload.get(key)) != "":
            try:
                argv += [opt, str(float(payload[key]))]
            except (TypeError, ValueError):
                pass
    return argv


def _parse_summary(lines: list) -> dict:
    out = {}
    for line in lines:
        m = re.search(r"결과 요약: 성공 (\d+)건 / 스킵 (\d+)건 / 실패·불확실 (\d+)건", line)
        if m:
            out.update(ok=int(m.group(1)), skip=int(m.group(2)), fail=int(m.group(3)))
        m = re.search(r"노션 기록: (\d+)건 성공 / (\d+)건 실패", line)
        if m:
            out["notion"] = [int(m.group(1)), int(m.group(2))]
    return out


def _run_main(argv: list):
    """main.py 를 같은 프로세스에서 실행하고 출력을 로그 창으로 옮긴다."""
    writer = LineWriter(add_line)
    rc = -1
    old_stdin = sys.stdin
    try:
        sys.stdin = io.StringIO("")     # 웹에서는 묻지 않고 인자대로만 실행한다
        with contextlib.redirect_stdout(writer):
            rc = main.main(argv)
    except SystemExit as exc:
        rc = int(exc.code or 0)
    except Exception as exc:
        add_line(f"[오류] {type(exc).__name__}: {str(exc)[:160]}")
    finally:
        sys.stdin = old_stdin
        writer.flush()
    with STATE["lock"]:
        lines = list(STATE["console"])
        STATE["phase"] = "idle"
        STATE["finished_at"] = now_text()
        STATE["summary"] = {"rc": rc, **_parse_summary(lines)}
    add_line(f"[종료] 코드 {rc}")


def job_running() -> bool:
    job = STATE["job"]
    return bool(job and job.get("thread") and job["thread"].is_alive())


def start_job(payload: dict) -> dict:
    with STATE["lock"]:
        if job_running():
            return {"ok": False, "error": "이미 실행 중입니다. [중단]을 누르거나 끝날 때까지 기다려 주세요."}
        argv = build_argv(payload)
        STATE["console"] = []
        STATE["summary"] = None
        STATE["finished_at"] = None
        STATE["phase"] = "running"
        STATE["started_at"] = now_text()
        main.reset_stop()
        thread = threading.Thread(target=_run_main, args=(argv,), daemon=True, name="apply-job")
        STATE["job"] = {"kind": "apply", "thread": thread, "argv": argv}
    thread.start()
    add_line(f"[시작] {now_text()} · python main.py {' '.join(argv)}")
    return {"ok": True, "argv": argv}


def stop_job() -> dict:
    main.request_stop()
    proc = STATE.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass
    add_line("[안내] 중단 요청 — 지금 신청 중인 상품까지만 마치고 멈춥니다.")
    return {"ok": True}


def start_login() -> dict:
    proc = STATE.get("proc")
    if proc is not None and proc.poll() is None:
        return {"ok": False, "error": "이미 로그인 창이 열려 있습니다."}
    script = str(ROOT / "main.py")

    def run():
        add_line("[로그인] 데스크톱 로그인 창을 엽니다. 창에서 카카오 로그인을 끝까지 마쳐 주세요.")
        try:
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
            p = subprocess.Popen([sys.executable or "python", script, "--login"], cwd=str(ROOT),
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 encoding="utf-8", errors="replace", bufsize=1, env=env)
        except Exception as exc:
            add_line(f"[로그인] 창을 열지 못했습니다: {type(exc).__name__}: {str(exc)[:120]}")
            return
        STATE["proc"] = p
        try:
            for line in p.stdout or []:
                add_line(line.rstrip())
            p.wait()
        finally:
            STATE["proc"] = None
        state = check_login()
        add_line(f"[로그인] 창이 닫혔습니다. 로그인 상태: {state['state']} ({state['detail']})")

    threading.Thread(target=run, daemon=True, name="login-window").start()
    return {"ok": True}


# ---------------------------------------------------------------------------
# 4. HTTP (127.0.0.1 전용)
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "KakaoTrialPanel/" + APP_VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):    # 콘솔을 조용하게
        return

    # ── 응답 도우미
    def _send(self, code: int, body, ctype: str = "application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _origin_ok(self) -> bool:
        """다른 웹사이트가 이 패널을 조종하지 못하게 한다(CSRF 방지)."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return origin.startswith("http://127.0.0.1") or origin.startswith("http://localhost")

    def do_GET(self):
        if not self._host_ok():
            self._json({"ok": False, "error": "127.0.0.1 에서만 열 수 있습니다."}, 403)
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._send(200, PAGE_HTML, "text/html; charset=utf-8")
        elif path == "/api/status":
            self._json(api_status())
        elif path == "/api/log":
            q = urllib.parse.parse_qs(parsed.query)
            try:
                since = int((q.get("since") or ["0"])[0])
            except ValueError:
                since = 0
            self._json(log_payload(since))
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._json({"ok": False, "error": "없는 주소입니다."}, 404)

    def do_POST(self):
        if not self._host_ok() or not self._origin_ok():
            self._json({"ok": False, "error": "이 PC(127.0.0.1)에서만 조종할 수 있습니다."}, 403)
            return
        parsed = urllib.parse.urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(min(length, 1_000_000)) if length > 0 else b""
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}

        path = parsed.path
        if path == "/api/preview":
            self._json(preview(mode=payload.get("mode") or "notion",
                               limit=payload.get("limit") or 30,
                               column=payload.get("column") or None,
                               sort=payload.get("sort") or "popular",
                               include_unknown=bool(payload.get("include_unknown")),
                               notion_all=bool(payload.get("notion_all"))))
        elif path == "/api/start":
            self._json(start_job(payload))
        elif path == "/api/stop":
            self._json(stop_job())
        elif path == "/api/login":
            self._json(start_login())
        elif path == "/api/check-login":
            self._json({"ok": True, **check_login()})
        elif path == "/api/column":
            self._json(save_column(payload.get("column") or ""))
        else:
            self._json({"ok": False, "error": "없는 주소입니다."}, 404)


def make_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def serve(port: int = DEFAULT_PORT, open_browser: bool = True, host: str = DEFAULT_HOST):
    httpd = make_server(host, port)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print("=" * 60)
    print(" 카카오 체험단 자동 신청 — 웹 조종판")
    print("=" * 60)
    print(f" 주소: {url}")
    print(" 이 PC 에서만 열립니다(외부 접속 불가). 끄려면 이 창에서 Ctrl+C.")
    print("=" * 60)
    add_line(f"[웹 조종판] {url} 에서 시작")
    threading.Thread(target=check_login, daemon=True).start()
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[웹 조종판] 종료합니다.")
    finally:
        httpd.server_close()


# ---------------------------------------------------------------------------
# 5. 화면 (한 장짜리 HTML — 외부 라이브러리 없음)
# ---------------------------------------------------------------------------
PAGE_HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>카카오 체험단 자동 신청 — 웹 조종판</title>
<style>
  :root{
    --bg:#f4f5f7; --card:#ffffff; --line:#e4e6eb; --text:#1b1d21; --muted:#6b7280;
    --accent:#f9d90b; --accent-ink:#2b2a12; --ok:#12805c; --ok-bg:#e6f6ef;
    --warn:#9a5b00; --warn-bg:#fff4e0; --bad:#b3261e; --bad-bg:#fdecea;
    --log-bg:#111418; --log-text:#d6e2ea; --shadow:0 1px 2px rgba(16,24,40,.06),0 8px 24px rgba(16,24,40,.06);
  }
  @media (prefers-color-scheme: dark){
    :root{
      --bg:#0f1115; --card:#171a20; --line:#272b33; --text:#e8eaed; --muted:#9aa1ac;
      --accent:#f9d90b; --accent-ink:#26240f; --ok:#54d6a4; --ok-bg:#12322a;
      --warn:#f0b95b; --warn-bg:#33280f; --bad:#ff8d85; --bad-bg:#3a1b19;
      --log-bg:#0b0d10; --log-text:#cfd8e3; --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 30px rgba(0,0,0,.35);
    }
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","Apple SD Gothic Neo","Malgun Gothic",sans-serif}
  .wrap{max-width:1040px;margin:0 auto;padding:24px 18px 64px}
  header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;margin-bottom:18px}
  h1{margin:0;font-size:20px;letter-spacing:-.2px}
  h1 small{display:block;font-weight:400;font-size:12px;color:var(--muted);letter-spacing:0}
  .pills{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  .pill{display:inline-flex;align-items:center;gap:6px;padding:6px 11px;border-radius:999px;
        border:1px solid var(--line);background:var(--card);font-size:12.5px;white-space:nowrap}
  .pill.ok{background:var(--ok-bg);border-color:transparent;color:var(--ok)}
  .pill.warn{background:var(--warn-bg);border-color:transparent;color:var(--warn)}
  .pill.bad{background:var(--bad-bg);border-color:transparent;color:var(--bad)}
  .dot{width:7px;height:7px;border-radius:50%;background:currentColor;display:inline-block}
  .card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;
        box-shadow:var(--shadow);margin-bottom:14px}
  .card h2{margin:0 0 12px;font-size:14px;font-weight:700;color:var(--muted);letter-spacing:.02em;text-transform:none}
  .row{display:flex;flex-wrap:wrap;gap:12px;align-items:center}
  .row + .row{margin-top:12px}
  label.field{display:flex;flex-direction:column;gap:5px;font-size:12.5px;color:var(--muted)}
  input[type=number],input[type=text],select{font:inherit;font-size:14px;color:var(--text);background:var(--bg);
        border:1px solid var(--line);border-radius:10px;padding:9px 11px;min-width:96px}
  input[type=number]{width:110px}
  select{min-width:150px}
  .seg{display:inline-flex;background:var(--bg);border:1px solid var(--line);border-radius:11px;padding:3px;gap:3px}
  .seg button{border:0;background:transparent;color:var(--muted);font:inherit;font-size:13.5px;
        padding:8px 14px;border-radius:8px;cursor:pointer}
  .seg button.on{background:var(--accent);color:var(--accent-ink);font-weight:700}
  button.btn{font:inherit;font-size:14px;font-weight:600;border-radius:11px;padding:10px 16px;cursor:pointer;
        border:1px solid var(--line);background:var(--card);color:var(--text);transition:.15s}
  button.btn:hover:not(:disabled){border-color:var(--muted)}
  button.btn.primary{background:var(--accent);color:var(--accent-ink);border-color:transparent}
  button.btn.danger{background:var(--bad-bg);color:var(--bad);border-color:transparent}
  button.btn:disabled{opacity:.45;cursor:not-allowed}
  .check{display:flex;align-items:center;gap:8px;font-size:13.5px;color:var(--text)}
  .check input{width:16px;height:16px;accent-color:#12805c}
  .hint{font-size:12.5px;color:var(--muted);margin:8px 0 0}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{padding:9px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
  th{font-size:12px;color:var(--muted);font-weight:600;white-space:nowrap}
  td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
  tbody tr:hover{background:rgba(127,127,127,.06)}
  .tbl-wrap{max-height:420px;overflow:auto;border:1px solid var(--line);border-radius:12px}
  .tbl-wrap.empty{display:none}
  .muted{color:var(--muted)}
  .summary{font-size:13.5px;background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:11px 13px;margin:10px 0 0}
  .summary b{font-weight:700}
  pre#log{margin:10px 0 0;background:var(--log-bg);color:var(--log-text);border-radius:12px;padding:14px;
        height:320px;overflow:auto;font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
        white-space:pre-wrap;word-break:break-all}
  pre#log:empty::before{content:"아직 로그가 없습니다. [자동 신청 시작]을 누르면 여기에 진행 상황이 흐릅니다.";color:#7c8794}
  .stat{display:grid;grid-template-columns:repeat(auto-fit,minmax(112px,1fr));gap:10px;margin-top:10px}
  .stat div{background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:10px 12px}
  .stat span{display:block;font-size:11.5px;color:var(--muted)}
  .stat b{font-size:19px;font-variant-numeric:tabular-nums}
  .banner{display:none;border-radius:12px;padding:11px 13px;font-size:13px;margin-top:10px}
  .banner.show{display:block}
  .banner.err{background:var(--bad-bg);color:var(--bad)}
  .banner.info{background:var(--ok-bg);color:var(--ok)}
  footer{color:var(--muted);font-size:12px;text-align:center;margin-top:22px}
  @media (max-width:640px){.wrap{padding:16px 12px 48px}h1{font-size:18px}.card{padding:14px}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>카카오 체험단 자동 신청<small>내 PC용 조종판 · 127.0.0.1 전용 · v<span id="ver">-</span></small></h1>
    <div class="pills">
      <span id="loginPill" class="pill"><i class="dot"></i>로그인 확인 중…</span>
      <button class="btn" id="btnCheckLogin">로그인 확인</button>
      <button class="btn" id="btnLogin">로그인 창 열기</button>
    </div>
  </header>

  <section class="card">
    <h2>노션 연동</h2>
    <div class="row">
      <span id="notionPill" class="pill"><i class="dot"></i>확인 중…</span>
      <label class="field">참가자 열
        <select id="column"><option value="">(불러오는 중)</option></select>
      </label>
      <button class="btn" id="btnSaveColumn">열 저장</button>
    </div>
    <p class="hint" id="columnHint">1번 모드는 이 열이 비어 있는 노션 행만 신청합니다(노션에 없는 신규 상품은 제외).</p>
    <div class="banner" id="columnBanner"></div>
  </section>

  <section class="card">
    <h2>실행 설정</h2>
    <div class="row">
      <div class="seg" id="modeSeg">
        <button data-mode="notion" class="on">노션 미체크 목록만</button>
        <button data-mode="list">인기순 N개</button>
      </div>
      <label class="field">신청 개수
        <input type="number" id="limit" value="30" min="1" max="500">
      </label>
      <label class="field">정렬
        <select id="sort">
          <option value="popular">인기순</option>
          <option value="deadline">마감임박순</option>
        </select>
      </label>
    </div>
    <div class="row">
      <label class="field">[신청 완료하기] 직전 대기(초)
        <input type="number" id="delayMin" value="5" min="0" max="60" step="0.5">
      </label>
      <label class="field">~ 최대
        <input type="number" id="delayMax" value="7" min="0" max="60" step="0.5">
      </label>
      <label class="check"><input type="checkbox" id="wish" checked>친구공개 위시 담기</label>
      <label class="check"><input type="checkbox" id="unknown">카테고리 미확인도 포함</label>
      <label class="check" id="notionAllWrap"><input type="checkbox" id="notionAll">제외 카테고리까지 포함</label>
    </div>
    <p class="hint">개수는 그때그때 바꾸면 됩니다. 실제 신청은 [자동 신청 시작]을 눌러야 시작됩니다.</p>
  </section>

  <section class="card">
    <h2>미리보기</h2>
    <div class="row">
      <button class="btn primary" id="btnPreview">후보 목록 불러오기</button>
      <span class="muted" id="previewState">아직 불러오지 않았습니다.</span>
    </div>
    <div class="summary" id="previewSummary" style="display:none"></div>
    <div class="tbl-wrap empty" id="tblWrap">
      <table>
        <thead><tr>
          <th class="num">#</th><th>상품명</th><th>브랜드</th>
          <th class="num">신청/모집</th><th class="num">마감</th><th>카테고리</th>
        </tr></thead>
        <tbody id="tblBody"></tbody>
      </table>
    </div>
  </section>

  <section class="card">
    <h2>실행</h2>
    <div class="row">
      <button class="btn primary" id="btnStart">자동 신청 시작</button>
      <button class="btn danger" id="btnStop" disabled>중단</button>
      <span id="runPill" class="pill"><i class="dot"></i>대기 중</span>
    </div>
    <div class="banner" id="runBanner"></div>
    <pre id="log"></pre>
    <div class="stat" id="runStat" style="display:none">
      <div><span>성공</span><b id="sOk">0</b></div>
      <div><span>스킵</span><b id="sSkip">0</b></div>
      <div><span>실패</span><b id="sFail">0</b></div>
      <div><span>노션 기록</span><b id="sNotion">0</b></div>
    </div>
  </section>

  <footer>이 화면은 이 PC 안에서만 열립니다 · 외부 전송 없음 · <span id="foot">표준 라이브러리만 사용</span></footer>
</div>

<script>
const $ = (id) => document.getElementById(id);
let mode = "notion";
let logSince = 0;
let busy = false;

function esc(s){ return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }

async function api(path, body){
  const opt = body === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)};
  const res = await fetch(path, opt);
  return await res.json();
}

function setPill(el, cls, text){
  el.className = "pill " + (cls || "");
  el.innerHTML = '<i class="dot"></i>' + esc(text);
}

function banner(el, text, kind){
  if(!text){ el.className = "banner"; el.textContent = ""; return; }
  el.className = "banner show " + (kind || "info");
  el.textContent = text;
}

/* ── 상태 ───────────────────────────────────────────── */
async function refreshStatus(){
  let st;
  try { st = await api("/api/status"); } catch(e) { return; }
  $("ver").textContent = st.version || "-";
  const login = st.login || {};
  if(login.state === "ok") setPill($("loginPill"), "ok", "카카오 로그인 유지 중");
  else if(login.state === "none") setPill($("loginPill"), "warn", login.detail || "로그인 필요");
  else setPill($("loginPill"), "", login.detail || "로그인 확인 필요");
  $("loginPill").title = (login.detail || "") + (login.checked_at ? " · 확인 " + login.checked_at : "");

  const n = st.notion || {};
  if(n.enabled) setPill($("notionPill"), "ok", "노션 연결됨 · " + (n.column || "열 없음"));
  else setPill($("notionPill"), "bad", "노션 설정 없음(python main.py --setup-notion)");

  const sel = $("column");
  const cols = n.columns || [];
  const want = n.column || "";
  if(cols.length){
    const names = cols.slice();
    if(want && !names.includes(want)) names.unshift(want);
    const sig = names.join("|");
    if(sel.dataset.sig !== sig){
      sel.dataset.sig = sig;
      sel.innerHTML = names.map(x => `<option value="${esc(x)}">${esc(x)}</option>`).join("");
    }
    if(want) sel.value = want;
  } else if(want && sel.dataset.sig !== want){
    sel.dataset.sig = want;
    sel.innerHTML = `<option value="${esc(want)}">${esc(want)}</option>`;
    sel.value = want;
  }
}

/* ── 미리보기 ────────────────────────────────────────── */
async function loadPreview(){
  if(busy) return;
  busy = true;
  $("btnPreview").disabled = true;
  $("previewState").textContent = "불러오는 중…";
  try{
    const r = await api("/api/preview", {
      mode, limit: Number($("limit").value) || 30, column: $("column").value || null,
      sort: $("sort").value, include_unknown: $("unknown").checked, notion_all: $("notionAll").checked
    });
    if(!r.ok){ $("previewState").textContent = "불러오지 못했습니다."; banner($("runBanner"), r.error, "err"); return; }
    banner($("runBanner"), "");
    $("previewState").textContent = `대상 ${r.total}건 중 ${r.items.length}건 표시`;
    const sum = $("previewSummary");
    sum.style.display = "block";
    sum.innerHTML = esc(r.summary);
    const body = $("tblBody");
    body.innerHTML = r.items.map((it, i) => `<tr>
      <td class="num">${i+1}</td>
      <td><a href="${esc(it.url)}" target="_blank" rel="noreferrer">${esc(it.title)}</a></td>
      <td>${esc(it.brand)}</td>
      <td class="num">${Number(it.applicationCount).toLocaleString()} / ${it.recruitmentCount}</td>
      <td class="num">${it.remainingDays == null ? "-" : it.remainingDays + "일"}</td>
      <td class="muted">${esc(it.category)}</td></tr>`).join("");
    $("tblWrap").className = "tbl-wrap";
  } finally {
    busy = false;
    $("btnPreview").disabled = false;
  }
}

/* ── 실행 ───────────────────────────────────────────── */
async function startRun(){
  const r = await api("/api/start", {
    mode, limit: Number($("limit").value) || 30, column: $("column").value || null,
    sort: $("sort").value, include_unknown: $("unknown").checked, notion_all: $("notionAll").checked,
    skip_wishlist: !$("wish").checked,
    delay_min: Number($("delayMin").value || 5), delay_max: Number($("delayMax").value || 7)
  });
  if(!r.ok){ banner($("runBanner"), r.error, "err"); return; }
  banner($("runBanner"), "");
  logSince = 0; $("log").textContent = "";
  $("runStat").style.display = "none";
  refreshLog();
}

async function stopRun(){
  const r = await api("/api/stop", {});
  banner($("runBanner"), r.ok ? "중단 요청을 보냈습니다. 지금 신청 중인 상품까지만 마칩니다." : (r.error||"중단 실패"), "info");
}

function showSummary(s){
  if(!s) return;
  $("runStat").style.display = "grid";
  $("sOk").textContent = s.ok ?? 0;
  $("sSkip").textContent = s.skip ?? 0;
  $("sFail").textContent = s.fail ?? 0;
  $("sNotion").textContent = Array.isArray(s.notion) ? s.notion[0] : 0;
}

async function refreshLog(){
  let r;
  try { r = await api("/api/log?since=" + logSince); } catch(e) { return; }
  if(r.lines && r.lines.length){
    const box = $("log");
    const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
    box.textContent += (box.textContent ? "\n" : "") + r.lines.join("\n");
    logSince = r.total;
    if(atBottom) box.scrollTop = box.scrollHeight;
  }
  const running = !!r.running;
  $("btnStart").disabled = running;
  $("btnStop").disabled = !running;
  $("btnPreview").disabled = running;
  setPill($("runPill"), running ? "warn" : "ok", running ? "실행 중" : "대기 중");
  if(!running && r.summary) showSummary(r.summary);
}

/* ── 이벤트 ─────────────────────────────────────────── */
$("modeSeg").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-mode]");
  if(!b) return;
  mode = b.dataset.mode;
  [...$("modeSeg").children].forEach(x => x.classList.toggle("on", x === b));
  $("notionAllWrap").style.display = mode === "notion" ? "flex" : "none";
});
$("btnPreview").onclick = loadPreview;
$("btnStart").onclick = startRun;
$("btnStop").onclick = stopRun;
$("btnLogin").onclick = async () => {
  const r = await api("/api/login", {});
  banner($("runBanner"), r.ok ? "로그인 창을 열었습니다. PC 화면의 크롬 창에서 로그인을 끝까지 마쳐 주세요." : r.error, r.ok ? "info" : "err");
};
$("btnCheckLogin").onclick = async () => {
  const r = await api("/api/check-login", {});
  banner($("runBanner"), "로그인 상태: " + (r.detail || r.state), r.state === "ok" ? "info" : "err");
  refreshStatus();
};
$("btnSaveColumn").onclick = async () => {
  const r = await api("/api/column", {column: $("column").value});
  banner($("columnBanner"), r.ok ? `참가자 열을 '${r.column}' 로 저장했습니다.` : r.error, r.ok ? "info" : "err");
  refreshStatus();
};
$("notionAllWrap").style.display = "flex";

refreshStatus();
refreshLog();
setInterval(refreshStatus, 5000);
setInterval(refreshLog, 1200);
</script>
</body>
</html>
"""


def main_cli(argv=None) -> int:
    ap = argparse.ArgumentParser(description="카카오 체험단 자동 신청 — 내 PC용 웹 조종판")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"포트 (기본 {DEFAULT_PORT})")
    ap.add_argument("--no-browser", action="store_true", help="브라우저를 자동으로 열지 않음")
    args = ap.parse_args(argv)
    serve(port=args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main_cli())
