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

APP_VERSION = "2.5"
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
    "columns_error": None,   # 열 목록을 못 읽은 사유(화면에 그대로 보여 준다)
}


def reset_state():
    """테스트·재실행용 상태 초기화."""
    with STATE["lock"]:
        STATE.update({"job": None, "console": [], "phase": "idle", "started_at": None,
                      "finished_at": None, "summary": None, "proc": None,
                      "login": None, "login_checked_at": None, "columns": None,
                      "columns_error": None})


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
    """담당자 열 이름만 notion_secret.json 에 저장한다(토큰·DB ID 는 그대로 둔다).

    실행할 때 고른 열을 기억해 두는 데도 쓴다(다음에 열면 그 열이 기본값).
    """
    column = (column or "").strip()
    if not column:
        return {"ok": False, "error": "열 이름이 비어 있습니다."}
    p = secret_path()
    if not p.is_file():
        return {"ok": False, "error": "notion_secret.json 이 없습니다. PC에서 python main.py --setup-notion 으로 먼저 설정해 주세요."}
    data = read_secret()
    if not data:
        return {"ok": False, "error": "notion_secret.json 을 읽지 못했습니다(형식 확인)."}
    if str(data.get("column") or "").strip() == column:
        return {"ok": True, "column": column, "unchanged": True}
    data["column"] = column
    try:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        return {"ok": False, "error": f"저장 실패({type(exc).__name__})"}
    add_line(f"[설정] 참가자 열을 '{column}' 로 저장했습니다.")
    return {"ok": True, "column": column}


def remember_column(column: str | None) -> None:
    """실행·미리보기에 쓴 열을 파일에 기억해 둔다(다음 실행의 기본값 = 직전에 고른 열)."""
    column = (column or "").strip()
    if column:
        save_column(column)


def _public_databases(entries, current_id: str = "") -> list:
    """저장해 둔 DB 목록 → 화면에 보낼 모양(토큰 없음)."""
    out = []
    current = main.normalize_notion_db_id(current_id)
    for db in entries or []:
        if not isinstance(db, dict):
            continue
        db_id = main.normalize_notion_db_id(db.get("id"))
        if not db_id:
            continue
        cols = [str(c).strip() for c in (db.get("columns") or []) if str(c).strip()]
        out.append({"id": db_id, "title": str(db.get("title") or "(제목 없는 DB)"),
                    "columns": len(cols), "current": db_id == current})
    return out


def _write_secret(data: dict) -> str | None:
    """notion_secret.json 저장(토큰이 든 파일이라 권한 600). 실패 사유를 돌려준다."""
    p = secret_path()
    try:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except Exception:
            pass
    except Exception as exc:
        return f"저장 실패({type(exc).__name__})"
    return None


def save_notion_settings(token: str, db_id: str = "", column: str = "") -> dict:
    """⚙ 설정 변경(접힘 메뉴): 토큰만 넣어도 되고 DB ID 는 비워 둬도 된다.

    DB ID 가 비어 있으면 토큰으로 연결된 노션 DB 를 자동으로 찾아(POST /v1/search)
    담당자 열이 있는 DB 를 골라 저장한다(1개면 그 DB, 여러 개면 가장 맞는 DB를 기본으로).
    DB ID 는 페이지/DB 전체 URL·하이픈 없는 32자로 붙여넣어도 ID 만 뽑아 저장한다.
    토큰은 화면·로그로 돌려주지 않는다.
    """
    token = (token or "").strip()
    raw_db = (db_id or "").strip()
    db_id = main.normalize_notion_db_id(raw_db)
    if not token:
        return {"ok": False, "error": "노션 API 토큰을 넣어 주세요. (DB ID 는 비워 두면 자동으로 찾습니다)"}
    old = read_secret()
    old_token = str(old.get("token") or "").strip()
    old_db = main.normalize_notion_db_id(old.get("db_id"))
    col = (column or "").strip() or str(old.get("column") or main.NOTION_DEFAULT_COLUMN).strip()
    databases = [d for d in (old.get("_databases") or []) if isinstance(d, dict) and d.get("id")]
    note = ""
    found_now = []
    if not db_id:
        # 토큰만으로 연결된 DB 를 찾는다
        add_line("[설정] DB ID 없이 진행 — 토큰으로 연결된 노션 DB 를 찾습니다…")
        found_now = main.notion_discover_databases(token) or []
        if found_now:
            databases = found_now
        usable = [d for d in databases if d.get("columns")]
        pick = main.pick_notion_database(databases, prefer_id=old_db)
        if pick:
            db_id = pick["id"]
            note = f"DB ID 없이 저장 — 토큰으로 연결된 DB에서 '{pick.get('title')}'를 골랐습니다."
            if len(usable) > 1:
                note += (f" 연결된 DB가 {len(usable)}개라 가장 맞는 DB를 기본으로 골랐습니다"
                         " — 다른 DB로 바꾸려면 ⚙ 설정 변경의 [DB]에서 고르세요.")
        elif old_db:
            db_id = old_db
            note = "노션에서 DB를 자동으로 찾지 못해 저장된 DB ID로 저장했습니다."
        else:
            # DB 를 못 찾아도 토큰은 저장해 둔다 — 노션에서 DB에 통합을 추가한 뒤 [↻ DB 목록]으로 이어갈 수 있게
            data = dict(old)
            data.update({"enabled": True, "token": token, "db_id": "", "column": col})
            data.pop("_단계", None)
            write_err = _write_secret(data)
            if write_err:
                return {"ok": False, "error": write_err}
            STATE["columns"] = []
            STATE["columns_error"] = ("노션에서 이 API 연결(Integration)을 DB 페이지에 추가했는지 확인해 주세요."
                                      " 토큰은 저장해 두었으니, DB에 연결한 뒤 [↻ DB 목록]을 누르면 이어집니다.")
            add_line(f"[경고] {STATE['columns_error']}")
            return {"ok": False, "token_saved": True, "error": STATE["columns_error"],
                    "columns": [], "columns_error": STATE["columns_error"]}

    changed = not (token == old_token and db_id == old_db)
    cols = main.notion_list_columns(db_id, token) or []
    if cols:
        if col not in cols:
            col = cols[0]
    elif changed:
        note = (note + " " if note else "") + "저장은 했지만 노션에서 열 목록을 확인하지 못했습니다(토큰·DB 연결 권한 확인)."
        # 직접 넣은 DB에서 못 읽었어도, 연결된 다른 DB가 있으면 알려 준다
        other = [d for d in databases if d.get("columns") and d.get("id") != db_id]
        if not other and not found_now:
            other = [d for d in (main.notion_discover_databases(token) or [])
                     if d.get("columns") and d.get("id") != db_id]
        if other:
            names = ", ".join(f"'{d.get('title')}'" for d in other[:3])
            note += f" 이 토큰으로 열을 읽을 수 있는 DB가 있습니다: {names}."
            databases = databases or other
    data = dict(old)
    data.update({"enabled": True, "token": token, "db_id": db_id, "column": col})
    data.pop("_단계", None)
    if databases:
        data["_databases"] = [{"id": d.get("id"), "title": d.get("title"), "columns": list(d.get("columns") or [])}
                              for d in databases]
    if cols:
        data["_columns"] = list(cols)
    elif changed:
        data.pop("_columns", None)   # 다른 DB·토큰인데 못 읽었으면 이전 DB 의 열 목록은 버린다
    err = _write_secret(data)
    if err:
        return {"ok": False, "error": err}
    # 저장 직후 화면이 바로 새 열 목록을 쓰도록 캐시를 맞춘다(토큰/DB 가 바뀌었는데 못 읽었으면 비운다).
    if cols:
        STATE["columns"] = list(cols)
        STATE["columns_error"] = None
    else:
        STATE["columns"] = [] if changed else \
            [str(c).strip() for c in (data.get("_columns") or []) if str(c).strip()]
        STATE["columns_error"] = main.notion_last_error() or \
            "노션에서 열 목록을 읽지 못했습니다(토큰·DB ID·DB 연결 권한 확인)."
    add_line(f"[설정] 노션 설정을 저장했습니다. (담당자 열: {col}) — 다음부터 이 설정을 그대로 씁니다.")
    if cols:
        add_line(f"[설정] 노션에서 담당자 열 {len(cols)}개를 읽었습니다: {', '.join(cols)}")
    elif STATE["columns_error"]:
        add_line(f"[경고] {STATE['columns_error']}")
    if raw_db and raw_db != db_id:
        add_line(f"[설정] 붙여넣은 주소에서 DB ID 만 추출했습니다: {db_id}")
    return {"ok": True, "column": col, "columns": list(STATE["columns"] or []),
            "columns_error": STATE["columns_error"],
            "databases": _public_databases(data.get("_databases"), db_id),
            "normalized_db": db_id if raw_db and raw_db != db_id else None,
            "note": note}


def switch_notion_db(db_id: str) -> dict:
    """[DB] 선택 변경: 저장된 토큰으로 그 DB 의 담당자 열을 다시 읽어 설정을 바꾼다."""
    data = read_secret()
    token = str(data.get("token") or "").strip()
    if not token:
        return {"ok": False, "error": "저장된 노션 토큰이 없습니다. ⚙ 설정 변경에서 토큰을 먼저 저장해 주세요."}
    new_id = main.normalize_notion_db_id(db_id)
    if not new_id:
        return {"ok": False, "error": "DB ID 를 확인해 주세요."}
    cols = main.notion_list_columns(new_id, token) or []
    err = None if cols else (main.notion_last_error() or "노션에서 열 목록을 읽지 못했습니다.")
    col = str(data.get("column") or main.NOTION_DEFAULT_COLUMN).strip()
    if cols and col not in cols:
        col = cols[0]
    data.update({"db_id": new_id, "column": col, "enabled": True})
    data.pop("_단계", None)
    if cols:
        data["_columns"] = list(cols)
    else:
        data.pop("_columns", None)
    write_err = _write_secret(data)
    if write_err:
        return {"ok": False, "error": write_err}
    STATE["columns"] = list(cols)
    STATE["columns_error"] = err
    title = next((str(d.get("title")) for d in (data.get("_databases") or [])
                  if main.normalize_notion_db_id((d or {}).get("id")) == new_id), "")
    add_line(f"[설정] 노션 DB 를 바꿨습니다: {title or new_id[-6:]} · 담당자 열 {len(cols)}개")
    return {"ok": True, "db_id": new_id, "column": col, "columns": list(cols),
            "columns_error": err, "databases": _public_databases(data.get("_databases"), new_id)}


def refresh_databases() -> dict:
    """[↻ DB 목록]: 저장된 토큰으로 연결된 노션 DB 목록을 다시 읽는다(DB ID 가 비어 있으면 자동 선택)."""
    data = read_secret()
    token = str(data.get("token") or "").strip()
    if not token:
        return {"ok": False, "error": "저장된 노션 토큰이 없습니다. ⚙ 설정 변경에서 토큰을 먼저 저장해 주세요."}
    found = main.notion_discover_databases(token)
    if found is None:
        err = main.notion_last_search_error() or "노션 DB 목록을 읽지 못했습니다(토큰 확인)."
        return {"ok": False, "error": err, "databases": _public_databases(data.get("_databases"),
                                                                          data.get("db_id"))}
    if found:
        data["_databases"] = [{"id": d["id"], "title": d.get("title"), "columns": list(d.get("columns") or [])}
                              for d in found]
    cur = main.normalize_notion_db_id(data.get("db_id"))
    pick = main.pick_notion_database(found, prefer_id=cur) if found else None
    note = ""
    if pick and not cur:
        # DB ID 가 비어 있던 설정 — 찾은 DB 중 기본 DB 로 바로 이어 준다
        data.update({"db_id": pick["id"], "enabled": True})
        cur = pick["id"]
        cols = list(pick.get("columns") or [])
        col = str(data.get("column") or main.NOTION_DEFAULT_COLUMN).strip()
        data["column"] = col if col in cols else (cols[0] if cols else col)
        if cols:
            data["_columns"] = cols
        note = f"DB ID 없이 '{pick.get('title')}' DB 를 골라 저장했습니다."
    write_err = _write_secret(data)
    if write_err:
        return {"ok": False, "error": write_err}
    databases = _public_databases(data.get("_databases"), cur)
    if not found:
        return {"ok": False, "databases": databases,
                "error": "이 API 연결(Integration)이 추가된 DB 를 찾지 못했습니다. "
                         "노션 DB 화면 ⋯ → 연결 에서 통합을 추가했는지 확인해 주세요."}
    if cur:
        cols = [str(c).strip() for c in (data.get("_columns") or []) if str(c).strip()]
        if cols:
            STATE["columns"] = cols
            STATE["columns_error"] = None
    add_line(f"[설정] 노션 DB 목록 새로고침 — {len(databases)}개" + (f" · {note}" if note else ""))
    return {"ok": True, "databases": databases, "count": len(databases), "note": note,
            "columns": list(STATE["columns"] or []), "column": data.get("column"),
            "columns_error": STATE.get("columns_error")}


def disable_notion() -> dict:
    """노션 기록을 끈다(파일은 지우지 않고 enabled=false — 토큰·DB ID 는 남겨 둔다)."""
    data = read_secret()
    if not data:
        return {"ok": False, "error": "저장된 노션 설정이 없습니다."}
    data["enabled"] = False
    p = secret_path()
    try:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        return {"ok": False, "error": f"저장 실패({type(exc).__name__})"}
    add_line("[설정] 노션 기록을 껐습니다(CSV 저장은 그대로). 다시 켜려면 ⚙ 설정 변경에서 저장하세요.")
    return {"ok": True}


def notion_columns(force: bool = False) -> list:
    """노션 DB 스키마에서 참가자 열 후보를 읽는다(한 번 읽어 캐시).

    실시간 읽기가 실패하면 notion_secret.json 에 저장해 둔 `_columns` 로 폴백한다.
    이때는 STATE["columns_error"] 에 사유를 남겨 화면이 '열 목록을 못 읽었음'을 보여 준다.
    """
    if STATE["columns"] and not force:
        return list(STATE["columns"])
    cfg = main.load_notion_config()
    if not cfg:
        STATE["columns"] = []
        STATE["columns_error"] = None
        return []
    cols = main.notion_list_columns(cfg["db_id"], cfg["token"])
    if cols:
        STATE["columns"] = list(cols)
        STATE["columns_error"] = None
        return list(cols)
    reason = main.notion_last_error() or "노션에서 열 목록을 읽지 못했습니다(토큰·DB ID·DB 연결 권한 확인)."
    cached = [str(c).strip() for c in (cfg.get("columns") or []) if str(c).strip()]
    if cached:
        STATE["columns"] = list(cached)
        STATE["columns_error"] = f"{reason} 저장해 둔 열 {len(cached)}개로 표시 중입니다."
        return list(cached)
    STATE["columns"] = []
    STATE["columns_error"] = reason
    return []


def reload_columns() -> dict:
    """[↻ 새로고침]: 노션에서 담당자 열 목록을 지금 다시 읽어 온다(캐시 무시)."""
    cols = notion_columns(force=True)
    err = STATE.get("columns_error")
    add_line(f"[설정] 담당자 열 새로고침 — {len(cols)}개" + (f" ({err})" if err else ""))
    return {"ok": not err, "columns": list(cols),
            "column": (main.load_notion_config() or {}).get("column"),
            "error": err}


def check_login() -> dict:
    """로그인 상태 + 지금 로그인된 계정 닉네임을 읽어 화면 상태에 넣는다."""
    state = main.login_state()
    with STATE["lock"]:
        STATE["login"] = state
        STATE["login_checked_at"] = now_text()
    return state


def logout_kakao() -> dict:
    """카카오 로그아웃: 저장된 세션(쿠키 백업 + 브라우저 프로필)을 지운다.

    다음에 [카카오 로그인 창 열기]로 다른 계정에도 로그인할 수 있다.
    신청이 도는 중에는 세션이 필요하므로 거부한다.
    """
    if job_running():
        return {"ok": False, "error": "자동 신청이 도는 중입니다. [중단]으로 멈춘 뒤에 로그아웃해 주세요."}
    proc = STATE.get("proc")
    if proc is not None and proc.poll() is None:
        return {"ok": False, "error": "로그인 창이 열려 있습니다. 그 창을 닫은 뒤에 다시 눌러 주세요."}
    res = main.logout(quiet=True)
    state = {"state": "none",
             "detail": "로그아웃됨 — [카카오 로그인 창 열기]로 다시 로그인해 주세요."}
    with STATE["lock"]:
        STATE["login"] = state
        STATE["login_checked_at"] = now_text()
    if res.get("ok"):
        add_line(f"[로그아웃] 저장된 카카오 세션을 지웠습니다(쿠키 {res.get('cookies', 0)}개)"
                 + (f" · 지운 항목: {', '.join(res.get('removed') or [])}" if res.get("removed") else ""))
    else:
        add_line(f"[로그아웃] 일부 실패 — {res.get('detail')}")
    return {"ok": bool(res.get("ok")), "state": state, **{k: res.get(k) for k in ("removed", "failed", "cookies")},
            "error": None if res.get("ok") else res.get("detail")}


def api_status() -> dict:
    raw = read_secret()
    cfg = main.load_notion_config()
    state = main.notion_secret_state()
    notion_state = "saved" if cfg else (state if state in ("missing", "disabled", "incomplete") else "missing")
    token_saved = bool(str(raw.get("token") or "").strip())
    columns = notion_columns() if cfg else []
    columns_error = STATE.get("columns_error") if cfg else None
    if not cfg and token_saved and notion_state != "disabled":
        # 토큰만 저장된 상태 — DB ID 를 못 정한 이유를 화면에서 바로 알 수 있게
        columns_error = ("DB ID 가 아직 비어 있습니다 — ⚙ 설정 변경의 [↻ DB 목록]을 누르면 "
                         "이 토큰에 연결된 DB 를 찾아 저장합니다.")
    databases = _public_databases(raw.get("_databases"), raw.get("db_id"))
    if not databases and cfg:
        # 목록을 아직 못 읽었어도 지금 쓰는 DB 는 보여 준다([↻ DB 목록]을 누르면 전체를 읽어 온다)
        databases = [{"id": cfg["db_id"], "title": f"저장된 DB (…{cfg['db_id'][-6:]})",
                      "columns": len(columns), "current": True}]
    notion = {
        "enabled": bool(cfg),
        "state": notion_state,
        "column": (cfg or {}).get("column") or raw.get("column"),
        "columns": columns,
        "columns_error": columns_error,
        "databases": databases,
        "db_tail": (main.normalize_notion_db_id(raw.get("db_id"))[-6:] or None),
        "file": bool(secret_path().is_file()),
        "token_saved": token_saved,
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
def plan_summary(mode: str, stat: dict, total: int, applied_checked: bool,
                 applied_count: int | None = None) -> str:
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
    parts = [f"카카오 목록 허용 {total}건",
             f"보류 {stat.get('held', 0)}"]
    if applied_count:
        parts.append(f"제외 {stat.get('denied', 0)}건(이미 신청 {applied_count}건 포함)")
    else:
        parts.append(f"제외 {stat.get('denied', 0)}건")
    if not applied_checked:
        parts.append("신청 이력 확인 불가(로그인 필요)")
    return " · ".join(parts) + f" → 신청 대상 {total}건"


def normalize_limit(limit, default: int | None = None) -> int:
    """개수 값을 정리한다. 0/'all'/'전체' = 제한 없음(0), 빈 값 = 기본값."""
    if default is None:
        default = main.LIMIT_DEFAULT
    if limit is None:
        return default
    if isinstance(limit, str):
        text = limit.strip()
        if text == "":
            return default
        if text.lower() in ("all", "전체", "전부", "모두"):
            return 0
        try:
            limit = int(float(text))
        except ValueError:
            return default
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return default
    return 0 if value <= 0 else value


def preview(mode: str = "notion", limit=main.LIMIT_DEFAULT, column: str | None = None,
            sort: str = "popular", include_unknown: bool = False,
            notion_all: bool = False, max_pages: int = 30) -> dict:
    """후보만 읽어 온다(실제 신청 없음). 오류는 dict 로 돌려준다. limit 0 = 전체."""
    mode = "notion" if mode == "notion" else "list"
    limit = normalize_limit(limit, main.LIMIT_DEFAULT)
    notion_cfg = None
    if mode == "notion":
        remember_column(column)          # 이번에 고른 열을 다음 실행의 기본값으로 기억
        notion_cfg = main.load_notion_config(column=column)
        if not notion_cfg:
            raw = read_secret()
            if str(raw.get("token") or "").strip():
                return {"ok": False, "error": "노션 DB 가 아직 정해지지 않았습니다 — ⚙ 설정 변경의 [↻ DB 목록]을 "
                                              "누르면 이 토큰에 연결된 DB 를 찾아 저장합니다."}
            return {"ok": False, "error": "노션 연동 설정(notion_secret.json)이 없습니다. "
                                          "PC에서 python main.py --setup-notion 으로 먼저 설정해 주세요."}
    items = main.collect_campaigns(max_pages)
    if not items:
        return {"ok": False, "error": "진행 중인 체험단을 가져오지 못했습니다. 인터넷 연결을 확인해 주세요."}
    applied = main.collect_applied_ids()
    # 신청 이력에서 걸러 낸 건수 — 로그인 전(이력 확인 불가)이면 None
    applied_count = len(applied) if applied is not None else None
    applied_in_list = 0
    if applied:
        live_ids = {it.get("campaignId") for it in items if it.get("campaignId")}
        applied_in_list = len(live_ids & applied)
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
        "applied_count": applied_count,
        "applied_in_list": applied_in_list,
        "summary": plan_summary(mode, stat, len(picked), applied is not None, applied_in_list),
        "stat": stat,
        "items": [main.candidate_row(i) for i in (picked[:limit] if limit > 0 else picked)],
        "held": [main.candidate_row(i) for i in held[:20]],
    }


# ---------------------------------------------------------------------------
# 3. 실행 · 중단 · 로그인 창
# ---------------------------------------------------------------------------
def build_argv(payload: dict) -> list:
    mode = "notion" if (payload.get("mode") or "notion") == "notion" else "list"
    remember_column(payload.get("column"))   # 고른 열을 기억(다음 실행 기본값)
    argv = []
    if mode == "notion":
        argv.append("--notion-pending")
        if payload.get("notion_all"):
            argv.append("--notion-all")
    argv += ["--limit", str(normalize_limit(payload.get("limit"), main.LIMIT_DEFAULT))]
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
                               limit=payload.get("limit"),
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
        elif path == "/api/logout":
            r = logout_kakao()
            self._json({"ok": r["ok"], "state": r.get("state"), "detail": r.get("state", {}).get("detail"),
                        "removed": r.get("removed"), "cookies": r.get("cookies"),
                        "error": r.get("error")})
        elif path == "/api/column":
            self._json(save_column(payload.get("column") or ""))
        elif path == "/api/notion-setup":
            self._json(save_notion_settings(payload.get("token") or "",
                                            payload.get("db_id") or "",
                                            payload.get("column") or ""))
        elif path == "/api/notion-columns":
            self._json(reload_columns())
        elif path == "/api/notion-db":
            self._json(switch_notion_db(payload.get("db_id") or ""))
        elif path == "/api/notion-dbs":
            self._json(refresh_databases())
        elif path == "/api/notion-disable":
            self._json(disable_notion())
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
  input[type=number],input[type=text],input[type=password],select{font:inherit;font-size:14px;color:var(--text);background:var(--bg);
        border:1px solid var(--line);border-radius:10px;padding:9px 11px;min-width:96px}
  input[type=number]{width:110px}
  input[type=password]{min-width:240px}
  select{min-width:150px}
  details.admin{margin-top:12px;border-top:1px dashed var(--line);padding-top:10px}
  details.admin summary{cursor:pointer;font-size:12.5px;color:var(--muted);list-style:none}
  details.admin summary::-webkit-details-marker{display:none}
  details.admin summary::before{content:"▸ ";display:inline-block;transition:.15s}
  details.admin[open] summary::before{transform:rotate(90deg)}
  details.admin[open] summary{color:var(--text);margin-bottom:10px}
  .seg{display:inline-flex;background:var(--bg);border:1px solid var(--line);border-radius:11px;padding:3px;gap:3px}
  .seg button{border:0;background:transparent;color:var(--muted);font:inherit;font-size:13.5px;
        padding:8px 14px;border-radius:8px;cursor:pointer}
  .seg button.on{background:var(--accent);color:var(--accent-ink);font-weight:700}
  .seg.chips button{font-size:12.5px;padding:7px 11px}
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
      <button class="btn" id="btnLogin">카카오 로그인 창 열기</button>
      <button class="btn" id="btnLogout">카카오 로그아웃</button>
    </div>
  </header>
  <p class="hint" style="margin:-8px 0 16px">한 화면에서 끝납니다 — ① [카카오 로그인 창 열기](처음 한 번) → ② 기록할 담당자 열 선택 → ③ [후보 목록 불러오기] → ④ [자동 신청 시작]</p>

  <section class="card">
    <h2>노션 연동 <span class="muted" id="notionSavedNote" style="font-weight:400;text-transform:none;letter-spacing:0"></span></h2>
    <div class="row">
      <span id="notionPill" class="pill"><i class="dot"></i>확인 중…</span>
      <label class="field">이번에 기록할 담당자 열
        <span style="display:flex;gap:6px;align-items:center">
          <select id="column"><option value="">(불러오는 중)</option></select>
          <button class="btn" id="btnReloadCols" type="button" title="노션에서 열 목록을 다시 읽어 옵니다">↻ 새로고침</button>
        </span>
      </label>
    </div>
    <p class="hint" id="columnHint">노션 토큰·DB ID 는 <b>한 번 저장하면 다시 묻지 않습니다.</b> 이번 실행에서 <b>어느 담당자 열에 기록할지</b>만 고르면 됩니다. 고른 열은 다음 실행의 기본값으로 기억됩니다.</p>
    <div class="banner" id="columnBanner"></div>
    <details class="admin" id="notionAdmin">
      <summary>⚙ 설정 변경 (토큰·DB ID — 처음 한 번만 / 거의 쓸 일 없음)</summary>
      <div class="row">
        <label class="field">노션 API 토큰 <span class="muted">(바꿀 때만)</span>
          <input type="password" id="tokenNew" autocomplete="off" spellcheck="false" placeholder="ntn_…">
        </label>
        <label class="field">DB ID <span class="muted">(안 넣어도 됨 · 토큰으로 자동 찾기)</span>
          <input type="text" id="dbNew" autocomplete="off" spellcheck="false" placeholder="비워 두면 자동 · 노션 주소도 됨">
        </label>
        <button class="btn" id="btnSaveNotion">저장</button>
        <button class="btn danger" id="btnNotionOff">노션 기록 끄기</button>
      </div>
      <div class="row">
        <label class="field">연결된 DB <span class="muted">(여러 개면 여기서 바꿈)</span>
          <select id="dbSelect"><option value="">(아직 없음)</option></select>
        </label>
        <button class="btn" id="btnReloadDbs" type="button">↻ DB 목록</button>
      </div>
      <p class="hint">토큰만 넣고 저장해도 됩니다 — DB ID 칸을 비우면 이 토큰(API 연결)에 연결된 노션 DB 를 찾아 담당자 열이 있는 DB 를 자동으로 고릅니다. 여러 개면 [연결된 DB]에서 바꿀 수 있습니다. 주소를 붙여넣어도 ID 만 자동 추출합니다. 토큰은 이 PC 의 <code>notion_secret.json</code> 에만 저장되고 화면·로그로는 나오지 않습니다.</p>
      <div class="banner" id="adminBanner"></div>
    </details>
    <p class="hint" id="columnModeHint">1번 모드는 이 열이 비어 있는 노션 행만 신청합니다(노션에 없는 신규 상품은 제외).</p>
  </section>

  <section class="card">
    <h2>실행 설정</h2>
    <div class="row">
      <div class="seg" id="modeSeg">
        <button data-mode="notion" class="on">노션 미체크 목록 (신청 안 한 것만)</button>
        <button data-mode="list">카카오 전체 중 신청 안 한 것 (인기순 등)</button>
      </div>
      <label class="field">신청 개수 <span class="muted">(0 = 있는 것 전부)</span>
        <input type="number" id="limit" value="100" min="0" max="9999">
      </label>
      <div class="seg chips" id="limitChips">
        <button data-limit="50">50개</button>
        <button data-limit="100" class="on">100개</button>
        <button data-limit="200">200개</button>
        <button data-limit="0">전체</button>
      </div>
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
    <p class="hint">개수는 그때그때 바꾸면 됩니다(0 또는 [전체] = 지금 대상 전부). 실제 신청은 [자동 신청 시작]을 눌러야 시작됩니다.</p>
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

/* 개수 입력값: 빈칸 = 기본 100, 0 이하 = 전체(0) */
function limitVal(){
  const raw = String($("limit").value ?? "").trim();
  if(raw === "") return 100;
  const n = Number(raw);
  if(!Number.isFinite(n)) return 100;
  return n > 0 ? Math.floor(n) : 0;
}

function syncLimitChips(){
  const v = limitVal();
  [...$("limitChips").children].forEach(b => b.classList.toggle("on", Number(b.dataset.limit) === v));
}

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
  if(!text){ el.className = "banner"; el.textContent = ""; el.dataset.kind = ""; return; }
  el.className = "banner show " + (kind || "info");
  el.textContent = text;
  el.dataset.kind = kind || "info";
}

/* ── 상태 ───────────────────────────────────────────── */
async function refreshStatus(){
  let st;
  try { st = await api("/api/status"); } catch(e) { return; }
  $("ver").textContent = st.version || "-";
  const login = st.login || {};
  /* 지금 로그인된 카카오 계정 — 서브.연 / 서브.오 를 바꿨는지 배지에서 바로 본다 */
  const nick = login.nickname || "";
  const mask = (login.account && login.account.masked_key) || "";
  const acct = nick || mask;
  const acctText = acct ? (nick && mask ? acct + " (" + mask + ")" : acct) : "";
  if(login.state === "ok") setPill($("loginPill"), "ok", acctText ? "로그인됨 · " + acctText : "로그인됨 · 계정 이름 확인 불가");
  else if(login.state === "none") setPill($("loginPill"), "warn", login.detail || "로그인 필요");
  else setPill($("loginPill"), "", login.detail || "로그인 확인 필요");
  $("loginPill").title = (login.detail || "") + (acctText ? " · 계정: " + acctText : "")
    + (login.checked_at ? " · 확인 " + login.checked_at : "");

  const n = st.notion || {};
  if(n.state === "saved") setPill($("notionPill"), "ok", "노션 설정 저장됨 · " + (n.column || "열 없음"));
  else if(n.state === "disabled") setPill($("notionPill"), "warn", "노션 기록 꺼짐 · ⚙ 설정 변경에서 켜기");
  else if(n.state === "incomplete") setPill($("notionPill"), "warn", "노션 설정이 비어 있음 · ⚙ 설정 변경");
  else setPill($("notionPill"), "bad", "노션 설정 없음 · ⚙ 설정 변경에서 한 번만");
  $("notionSavedNote").textContent = n.state === "saved" ? "· 저장됨 · 토큰·DB ID 다시 묻지 않음" : "";

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
    sel.title = `담당자 열 ${cols.length}개`;
  } else if(want && sel.dataset.sig !== want){
    sel.dataset.sig = want;
    sel.innerHTML = `<option value="${esc(want)}">${esc(want)}</option>`;
    sel.value = want;
    sel.title = "노션에서 열 목록을 읽지 못해 저장된 열 1개만 표시 중";
  }
  /* 열 목록을 못 읽었으면 이유를 숨기지 않고 그대로 보여 준다(1개만 뜨는 원인) */
  const colErr = n.columns_error || "";
  if(colErr) banner($("columnBanner"), colErr + " — [↻ 새로고침]으로 다시 읽어 보세요.", "err");
  else if($("columnBanner").dataset.kind === "err") banner($("columnBanner"), "");

  /* 연결된 DB 목록(여러 개면 여기서 바꿈) */
  const dbs = n.databases || [];
  const dsel = $("dbSelect");
  const dsig = dbs.map(d => `${d.id}:${d.title}:${d.columns}:${d.current ? 1 : 0}`).join("|");
  if(dsel.dataset.sig !== dsig){
    dsel.dataset.sig = dsig;
    dsel.innerHTML = dbs.length
      ? dbs.map(d => `<option value="${esc(d.id)}"${d.current ? " selected" : ""}>${esc(d.title)} · 담당자 열 ${d.columns}개</option>`).join("")
      : `<option value="">(아직 없음 — [↻ DB 목록])</option>`;
  }
  if(dbs.length){ const cur = dbs.find(d => d.current); if(cur) dsel.value = cur.id; }
}

/* ── 미리보기 ────────────────────────────────────────── */
async function loadPreview(){
  if(busy) return;
  busy = true;
  $("btnPreview").disabled = true;
  $("previewState").textContent = "불러오는 중…";
  try{
    const r = await api("/api/preview", {
      mode, limit: limitVal(), column: $("column").value || null,
      sort: $("sort").value, include_unknown: $("unknown").checked, notion_all: $("notionAll").checked
    });
    if(!r.ok){ $("previewState").textContent = "불러오지 못했습니다."; banner($("runBanner"), r.error, "err"); return; }
    banner($("runBanner"), "");
    $("previewState").textContent = limitVal() === 0
      ? `대상 ${r.total}건 전체 표시`
      : `대상 ${r.total}건 중 ${r.items.length}건 표시`;
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
    mode, limit: limitVal(), column: $("column").value || null,
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
$("limitChips").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-limit]");
  if(!b) return;
  $("limit").value = b.dataset.limit;
  syncLimitChips();
  $("previewSummary").style.display = "none";
});
$("limit").addEventListener("input", syncLimitChips);
$("btnPreview").onclick = loadPreview;
$("btnStart").onclick = startRun;
$("btnStop").onclick = stopRun;
$("btnLogin").onclick = async () => {
  const r = await api("/api/login", {});
  banner($("runBanner"), r.ok ? "로그인 창을 열었습니다. PC 화면의 크롬 창에서 로그인을 끝까지 마쳐 주세요." : r.error, r.ok ? "info" : "err");
};
$("btnLogout").onclick = async () => {
  if(!confirm("카카오 로그아웃할까요?\n저장된 로그인 세션을 지웁니다(다른 계정으로 다시 로그인할 수 있습니다).")) return;
  $("btnLogout").disabled = true;
  try{
    const r = await api("/api/logout", {});
    banner($("runBanner"), r.ok ? "로그아웃했습니다. [카카오 로그인 창 열기]로 다시 로그인할 수 있습니다." : (r.error || "로그아웃하지 못했습니다."), r.ok ? "info" : "err");
    await refreshStatus();
  } finally {
    $("btnLogout").disabled = false;
  }
};
$("btnCheckLogin").onclick = async () => {
  const r = await api("/api/check-login", {});
  banner($("runBanner"), "로그인 상태: " + (r.detail || r.state), r.state === "ok" ? "info" : "err");
  refreshStatus();
};
$("btnSaveNotion").onclick = async () => {
  const token = $("tokenNew").value.trim();
  const db = $("dbNew").value.trim();
  if(!token){ banner($("adminBanner"), "노션 API 토큰을 넣어 주세요. (DB ID 는 비워 두면 자동으로 찾습니다)", "err"); return; }
  $("btnSaveNotion").disabled = true;
  try{
    const r = await api("/api/notion-setup", {token, db_id: db, column: $("column").value || ""});
    const extra = r.normalized_db ? ` 주소에서 DB ID 만 추출했습니다(${r.normalized_db}).` : "";
    if(!r.ok){ banner($("adminBanner"), (r.error || "저장하지 못했습니다.") + extra, "err"); await refreshStatus(); return; }
    $("tokenNew").value = "";
    $("dbNew").value = "";
    if(r.columns_error) banner($("adminBanner"), r.columns_error + extra, "err");
    else banner($("adminBanner"), (r.note || `저장했습니다. 담당자 열 '${r.column}' · 이제 토큰·DB ID 는 다시 묻지 않습니다.`) + extra, "info");
    await refreshStatus();
  } finally {
    $("btnSaveNotion").disabled = false;
  }
};
$("dbSelect").addEventListener("change", async () => {
  const v = $("dbSelect").value;
  if(!v) return;
  const r = await api("/api/notion-db", {db_id: v});
  if(r.ok && !r.columns_error)
    banner($("adminBanner"), `DB 를 바꿨습니다 · 담당자 열 ${(r.columns || []).length}개`, "info");
  else banner($("adminBanner"), r.error || r.columns_error || "DB 를 바꾸지 못했습니다.", "err");
  await refreshStatus();
});
$("btnReloadDbs").onclick = async () => {
  const btn = $("btnReloadDbs");
  btn.disabled = true;
  btn.textContent = "찾는 중…";
  try{
    const r = await api("/api/notion-dbs", {});
    if(r.ok) banner($("adminBanner"), `${r.count}개 DB 를 찾았습니다.` + (r.note ? ` ${r.note}` : ""), "info");
    else banner($("adminBanner"), r.error || "DB 를 찾지 못했습니다.", "err");
    await refreshStatus();
  } finally {
    btn.disabled = false;
    btn.textContent = "↻ DB 목록";
  }
};
$("btnNotionOff").onclick = async () => {
  if(!confirm("노션 기록을 끌까요? (저장된 토큰은 지우지 않고 끄기만 합니다)")) return;
  const r = await api("/api/notion-disable", {});
  banner($("adminBanner"), r.ok ? "노션 기록을 껐습니다. 다시 켜려면 위에서 토큰·DB ID 를 저장하세요." : r.error, r.ok ? "info" : "err");
  refreshStatus();
};
$("column").addEventListener("change", async () => {
  const v = $("column").value;
  if(!v) return;
  const r = await api("/api/column", {column: v});
  if(r.ok && $("columnBanner").dataset.kind !== "err")
    banner($("columnBanner"), `'${r.column}' 열로 기억했습니다. 다음 실행에도 이 열이 기본입니다.`, "info");
});
$("btnReloadCols").onclick = async () => {
  const btn = $("btnReloadCols");
  btn.disabled = true;
  btn.textContent = "불러오는 중…";
  try{
    const r = await api("/api/notion-columns", {});
    if(r.ok) banner($("columnBanner"), `담당자 열 ${(r.columns || []).length}개를 노션에서 다시 읽었습니다.`, "info");
    else banner($("columnBanner"), (r.error || "열 목록을 읽지 못했습니다.") + " — 토큰·DB ID·DB 연결(⋯ → 연결)을 확인해 주세요.", "err");
    await refreshStatus();
  } finally {
    btn.disabled = false;
    btn.textContent = "↻ 새로고침";
  }
};
$("column").addEventListener("change", () => { $("previewSummary").style.display = "none"; });
$("notionAllWrap").style.display = "flex";
syncLimitChips();

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
