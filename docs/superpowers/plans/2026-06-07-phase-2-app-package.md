# Phase 2 · server.py → app/ Decomposition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decompose `server.py` (2412 lines, 46 routes, 45 module-level symbols) into `app/` package — `state.py` singleton + 10 API routers + auth (state/middleware/pages) + WS (broadcast/handler) + agent (content/permission/options/session/turn) + reporting + persistence. **Pure relocation + import re-wiring, ZERO business-logic change.**

**Architecture:** Single `app.state.state` (and `app.auth.state.auth_state`) singletons are the only cross-module shared state. Strict one-way dep direction: `permission → options → session → turn → ws.handler`. The lifespan stays in `app/main.py` and calls into per-subsystem init hooks. Routers are FastAPI APIRouter() instances `include`-d at startup.

**Tech Stack:** FastAPI APIRouter, no new deps. `tests/replay.py` (new) records 100 HTTP + 30 WS frames before any code moves, then re-records after the last task and byte-diffs them with field normalization.

**Branch:** `refactor/phase-2-app-package` (already created)
**Parent spec:** [2026-06-06-refactor-roadmap.md](../specs/2026-06-06-refactor-roadmap.md) §Phase 2
**Audit basis:** server.py decomposition audit completed 2026-06-07.

---

## File Structure (Target)

| Path | Action | Lines | Origin in server.py | Risk |
|---|---|---|---|---|
| `app/state.py` | Create | 40 | AppState (205-235) + singleton | ★★ |
| `app/persistence/__init__.py` | Create | 0 | — | ★ |
| `app/persistence/files.py` | Create | 110 | uploads_dir + resolve helpers + classify_upload + _safe_filename + upload consts (187-202, 238-262, 468-499) | ★★ |
| `app/ws/__init__.py` | Create | 0 | — | ★ |
| `app/ws/broadcast.py` | Create | 20 | broadcast (281-291) | ★ |
| `app/auth/__init__.py` | Create | 0 | — | ★ |
| `app/auth/state.py` | Create | 25 | _AUTH_FILE, _COOKIE_*, auth_state init (821-826) | ★★ |
| `app/auth/middleware.py` | Create | 70 | _PUBLIC_*, _is_public, _wants_html, _current_device, auth_middleware (828-897) | ★★★ |
| `app/auth/pages.py` | Create | 280 | _AUTH_PAGE_CSS, _page, _ua_short, _html_escape, 9 page handlers (898-1158) | ★★★ |
| `app/agent/content.py` | Create | 140 | _read_text_safe, _read_xlsx_as_text, _build_user_content (501-625) | ★★ |
| `app/agent/permission.py` | Create | 80 | AUTO_ALLOW, CHAT_TOOLS, can_use_tool, truncate, summarize_input | ★★★★ |
| `app/agent/options.py` | Create | 50 | CHAT_SYSTEM_PROMPT, AVAILABLE_MODES/MODELS, PB_MCP_SERVER, make_options | ★★★ |
| `app/agent/session.py` | Create | 110 | init_client, open_session, new_session | ★★★★ |
| `app/agent/turn.py` | Create | 220 | _save_msg, _block_to_event, run_user_turn | ★★★★ |
| `app/reporting/__init__.py` | Create | 0 | — | ★ |
| `app/reporting/weekly_report.py` | Create | 40 | _weekly_report_posted | ★★ |
| `app/api/__init__.py` | Create | 0 | — | ★ |
| `app/api/meta.py` | Create | 50 | /api/health, /api/usage, /api/meta | ★ |
| `app/api/well_known.py` | Create | 30 | 2 /.well-known routes | ★ |
| `app/api/push.py` | Create | 25 | /api/vapid-public-key, /api/subscribe, /api/unsubscribe | ★ |
| `app/api/today_todos.py` | Create | 80 | _PBError + today helpers + 2 routes | ★★ |
| `app/api/browse.py` | Create | 50 | /api/browse, /api/mkdir | ★★ |
| `app/api/sessions.py` | Create | 65 | 5 sessions routes | ★★★ |
| `app/api/uploads.py` | Create | 55 | /api/upload | ★★ |
| `app/api/poi.py` | Create | 280 | POI + 3 providers + merge | ★ |
| `app/api/settings.py` | Create | 130 | weekly-report + notion-sync + _pb_sync_global | ★★★ |
| `app/api/sync.py` | Create | 200 | sync/now + targets CRUD + registry export | ★★★ |
| `app/ws/handler.py` | Create | 330 | /ws + handle_ws_message + handle_cmd | ★★★★★ |
| `app/main.py` | Create | 150 | FastAPI app + lifespan + CORS + static mount + router includes | ★★★ |
| `server.py` | Modify | 16 | Thin re-export shim: `from app.main import app` | ★ |
| `tests/replay.py` | Create | 200 | Recorder + comparator | ★★ |
| `tests/fixtures/phase2_baseline.jsonl` | Create | data | Pre-Phase-2 traffic recording | — |
| `tests/fixtures/phase2_after.jsonl` | Create | data | Post-Phase-2 recording for byte-diff | — |

**Out of scope** (explicitly preserved):
- L165 `CHAT_SYSTEM_PROMPT`'s `/home/dev/phone-bridge/CHECKIN.md` literal
- L420 `init_client` single-session semantics (Phase 3)
- `os.environ` writes inside `app/integrations/pb/token.py` (Phase 1)
- The 2 `PBClient` shapes coexisting
- L1635 `pb._http` `# noqa: SLF001` access
- Mass narrowing of bare `except Exception` (only narrow inside moved files)

---

## State Singleton Pattern

This is non-negotiable. Every file that needs global state imports it the same way:

```python
# app/state.py
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeSDKClient
    from fastapi import WebSocket


@dataclass
class AppState:
    client: "ClaudeSDKClient | None" = None
    cwd_root: Path = field(default_factory=lambda: Path.cwd().resolve())
    cwd: Path = field(init=False)
    websockets: set["WebSocket"] = field(default_factory=set)
    client_tz: str = ""
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    pending_meta: dict[str, dict] = field(default_factory=dict)
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    current_turn_task: asyncio.Task | None = None
    session_id: str | None = None
    sdk_session_id: str | None = None
    mode: str = "code"
    model: str = ""
    auto_approve: bool = False

    def __post_init__(self) -> None:
        self.cwd = self.cwd_root


state: AppState = AppState()
```

**Why singleton not Depends?** `state` is process-level not per-request; Depends forces every consumer (including non-route functions like `broadcast`, `run_user_turn`) into the DI pattern, creating awkward signatures and circular import risk. The dataclass is mutable in-place — that's the whole point.

`cwd_root` actual default (from `settings.default_cwd`) is set in `app/main.py:lifespan` to avoid importing settings at module-load time in app/state.py.

---

## Task 0: Record baseline replay fixture

**Files:**
- Create: `tests/replay.py`
- Create: `tests/fixtures/.gitkeep`
- Modify: `server.py` (add `BRIDGE_RECORD=1`-gated middleware/hook)
- Run: capture `tests/fixtures/phase2_baseline.jsonl` from the live server

This MUST happen before any code is moved. If we move first, we cannot compare against the old behavior.

- [ ] **Step 1: Write `tests/replay.py`**

```python
"""Phase 2 baseline / after recorder + comparator.

Usage (recording, requires running server with BRIDGE_RECORD=1):
    BRIDGE_RECORD=1 BRIDGE_RECORD_PATH=tests/fixtures/phase2_baseline.jsonl \\
        python -m uvicorn server:app ...

Usage (comparing):
    python tests/replay.py diff \\
        tests/fixtures/phase2_baseline.jsonl \\
        tests/fixtures/phase2_after.jsonl

Records JSONL: one object per HTTP req/resp, WS open/close, WS frame.
Comparator normalizes random fields (session_id, cb_id, ISO timestamps,
cost_usd, duration_ms, token counts) via first-occurrence remap, then
byte-diffs each record.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


_ISO_TS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?"
)
_HEX_UUID_RE = re.compile(r"^[0-9a-f]{15,}$")


def _normalize(obj: Any, remap: dict[str, str], counters: dict[str, int]) -> Any:
    if isinstance(obj, dict):
        return {k: _normalize_value(k, v, remap, counters) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize(v, remap, counters) for v in obj]
    return obj


def _normalize_value(key: str, value: Any, remap: dict[str, str],
                     counters: dict[str, int]) -> Any:
    if key in {"session_id", "sdk_session_id", "id", "cb_id"} and isinstance(value, str):
        if value and _HEX_UUID_RE.match(value):
            tag = "sid" if "session" in key or key == "id" else "cb"
            if value not in remap:
                counters[tag] = counters.get(tag, 0) + 1
                remap[value] = f"<{tag}_{counters[tag]}>"
            return remap[value]
    if key in {"notion_id", "notion_db_id"} and isinstance(value, str) and value:
        if value not in remap:
            counters["nid"] = counters.get("nid", 0) + 1
            remap[value] = f"<nid_{counters['nid']}>"
        return remap[value]
    if isinstance(value, str) and _ISO_TS_RE.search(value):
        return _ISO_TS_RE.sub("<TS>", value)
    if key in {"cost_usd", "duration_ms", "duration_api_ms",
              "input_tokens", "output_tokens", "cache_creation_input_tokens",
              "cache_read_input_tokens", "num_turns"}:
        return "<NUM>"
    if key in {"key", "vapid_public_key"} and isinstance(value, str) and len(value) > 40:
        return "<VAPID>"
    return _normalize(value, remap, counters)


class Recorder:
    """Single-process JSONL append-only recorder."""
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")
        self._seq = 0

    def _emit(self, rec: dict[str, Any]) -> None:
        self._seq += 1
        rec["seq"] = self._seq
        self._fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()

    def http(self, method: str, path: str, query: str,
             req_body: bytes | None, status: int, resp_body: bytes) -> None:
        def _body(b: bytes | None) -> dict:
            if b is None or len(b) == 0:
                return {"len": 0}
            if len(b) > 4096:
                return {"len": len(b), "sha256": hashlib.sha256(b).hexdigest()}
            try:
                return {"json": json.loads(b)}
            except (json.JSONDecodeError, ValueError):
                return {"len": len(b), "sha256": hashlib.sha256(b).hexdigest()}
        self._emit({
            "kind": "http",
            "req": {"method": method, "path": path, "query": query,
                    "body": _body(req_body)},
            "resp": {"status": status, "body": _body(resp_body)},
        })

    def ws_open(self) -> None:
        self._emit({"kind": "ws_open"})

    def ws_close(self, code: int | None) -> None:
        self._emit({"kind": "ws_close", "code": code})

    def ws_frame(self, direction: str, frame_text: str) -> None:
        try:
            frame = json.loads(frame_text)
        except (json.JSONDecodeError, ValueError):
            frame = {"_raw": frame_text[:500]}
        self._emit({"kind": "ws", "dir": direction, "frame": frame})


def diff(baseline_path: str, after_path: str) -> int:
    base_records = [json.loads(l) for l in Path(baseline_path).read_text(encoding="utf-8").splitlines() if l.strip()]
    after_records = [json.loads(l) for l in Path(after_path).read_text(encoding="utf-8").splitlines() if l.strip()]

    if len(base_records) != len(after_records):
        print(f"FAIL: record count differs - baseline={len(base_records)} after={len(after_records)}")
        return 1

    base_remap: dict[str, str] = {}
    after_remap: dict[str, str] = {}
    base_counts: dict[str, int] = {}
    after_counts: dict[str, int] = {}

    fails = 0
    for i, (b, a) in enumerate(zip(base_records, after_records), 1):
        b_norm = _normalize(b, base_remap, base_counts)
        a_norm = _normalize(a, after_remap, after_counts)
        for d in (b_norm, a_norm):
            d.pop("seq", None)
        if b_norm != a_norm:
            fails += 1
            print(f"FAIL record #{i}:")
            print(f"  baseline: {json.dumps(b_norm, sort_keys=True)[:300]}")
            print(f"  after   : {json.dumps(a_norm, sort_keys=True)[:300]}")
            if fails >= 10:
                print("(stopping after 10 mismatches)")
                break

    if fails == 0:
        print(f"OK: {len(base_records)} records match after normalization")
        return 0
    print(f"FAIL: {fails} record(s) differ")
    return 1


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in {"diff"}:
        print("usage: python tests/replay.py diff <baseline.jsonl> <after.jsonl>")
        sys.exit(2)
    sys.exit(diff(sys.argv[2], sys.argv[3]))
```

- [ ] **Step 2: Add BRIDGE_RECORD=1 hooks in server.py**

Near the top (after `app = FastAPI(...)`):

```python
# --- Phase 2 baseline recorder (BRIDGE_RECORD=1) ----------------------------
# Removed at end of Phase 2 (Task 16).
_recorder = None
if os.environ.get("BRIDGE_RECORD"):
    from pathlib import Path as _RP
    import sys as _RS
    _RS.path.insert(0, str(_RP(__file__).resolve().parent / "tests"))
    from replay import Recorder as _Recorder
    _recorder = _Recorder(_RP(os.environ.get("BRIDGE_RECORD_PATH",
                                              "tests/fixtures/phase2_baseline.jsonl")))


@app.middleware("http")
async def _record_http(request: Request, call_next):
    if not _recorder:
        return await call_next(request)
    req_body = await request.body()
    async def _receive():
        return {"type": "http.request", "body": req_body, "more_body": False}
    request._receive = _receive
    response = await call_next(request)
    chunks = []
    async for c in response.body_iterator:
        chunks.append(c)
    resp_body = b"".join(chunks)
    _recorder.http(request.method, request.url.path,
                   str(request.url.query), req_body,
                   response.status_code, resp_body)
    from starlette.responses import Response as _SR
    return _SR(content=resp_body, status_code=response.status_code,
               headers=dict(response.headers),
               media_type=response.media_type)
```

For WebSocket: in the existing `/ws` handler, after `await websocket.accept()` call `if _recorder: _recorder.ws_open()`; wrap `await websocket.receive_text()` with a try and call `_recorder.ws_frame("in", text)`; similarly wrap `await websocket.send_text(text)` calls with `_recorder.ws_frame("out", text)`; in the `finally:` block call `if _recorder: _recorder.ws_close(None)`.

- [ ] **Step 3: Deploy + enable recorder**

```powershell
deploy
```

Then on the VM:
```bash
ssh dashboard-server
sudo systemctl edit phone-bridge
# Add under [Service]:
#   Environment="BRIDGE_RECORD=1"
#   Environment="BRIDGE_RECORD_PATH=/home/dev/phone-bridge/tests/fixtures/phase2_baseline.jsonl"
sudo systemctl daemon-reload
sudo systemctl restart phone-bridge
```

- [ ] **Step 4: Drive 100 HTTP + 30 WS interactions**

Manually exercise the PWA + curl scripts to hit every route. Reference §Coverage Checklist at end of this plan.

- [ ] **Step 5: Stop recorder, pull baseline locally**

```bash
ssh dashboard-server "sudo systemctl edit phone-bridge"
# Remove the two Environment= lines, save
sudo systemctl daemon-reload
sudo systemctl restart phone-bridge
scp dashboard-server:/home/dev/phone-bridge/tests/fixtures/phase2_baseline.jsonl tests/fixtures/
```

- [ ] **Step 6: Sanity-check baseline**

```bash
wc -l tests/fixtures/phase2_baseline.jsonl
python -c "import json; recs = [json.loads(l) for l in open('tests/fixtures/phase2_baseline.jsonl', encoding='utf-8') if l.strip()]; print('records:', len(recs)); print('kinds:', {r['kind'] for r in recs}); print('http count:', sum(1 for r in recs if r['kind']=='http')); print('ws count:', sum(1 for r in recs if r['kind']=='ws'))"
```

Expected: ≥80 http + ≥20 ws records.

- [ ] **Step 7: Commit**

```bash
git add tests/replay.py tests/fixtures/.gitkeep tests/fixtures/phase2_baseline.jsonl server.py
git commit -m "test(replay): record Phase 2 baseline traffic + add comparator

tests/replay.py: Recorder writes JSONL of every HTTP req/resp and WS
frame. Comparator normalizes random fields and byte-diffs records.

tests/fixtures/phase2_baseline.jsonl: ~100 HTTP + ~30 WS captured from
live staging exercising every route before Phase 2 decomposition.

server.py: BRIDGE_RECORD=1-gated middleware + WS hooks. Removed in Task 16."
```

---

## Task 1: `app/state.py` — AppState singleton

**Files:**
- Create: `app/state.py`
- Modify: `server.py`

- [ ] **Step 1: Create `app/state.py`** — use the §State Singleton Pattern code verbatim.

- [ ] **Step 2: Remove the dataclass + singleton from `server.py`**

Delete the `@dataclass` + `class AppState:` block (around line 205) and the `state = AppState()` line. Replace with:
```python
from app.state import state
```

- [ ] **Step 3: Sanity**

```bash
python -c "import ast; ast.parse(open('server.py', encoding='utf-8').read()); print('parse OK')"
python -c "import server; print('OK')" 2>&1 | tail -3
```

- [ ] **Step 4: Commit**

```bash
git add app/state.py server.py
git commit -m "refactor(state): extract AppState + state singleton to app/state.py"
```

---

## Tasks 2-13: per-file extractions

Each task follows this pattern (5 steps):

1. **Create** the new file with the moved code + needed imports
2. **Delete** the originals from `server.py` and add `from app.X import ...`
3. **Parse + import sanity:** `python -c "import server; print('OK')"`
4. **Run smoke:** `python tests/smoke_backend.py` (against running staging)
5. **Commit:** one commit per task

**If smoke fails**: revert the task (`git reset --hard HEAD~1`), diagnose, fix, redo. Do not stack failures.

The full task list (each is independent):

### Task 2: `app/persistence/files.py`
Move: `UPLOAD_DIRNAME, MAX_UPLOAD_SIZE, MAX_INLINE_IMAGE_BYTES, ALLOWED_IMAGE_MIMES, ALLOWED_PDF_MIMES, TEXT_EXTS, SHEET_EXTS, MAX_TEXT_INLINE_CHARS, MAX_SHEET_ROWS_PER_SHEET, uploads_dir, _resolve_in_root, _to_rel, classify_upload, _safe_filename`.
Imports: `from app.state import state`.

### Task 3: `app/ws/broadcast.py`
Move: `broadcast()`.
Imports: `from app.state import state`.

### Task 4: `app/auth/state.py`
Move: `_AUTH_FILE, _COOKIE_DAYS, _COOKIE_SECONDS, auth_state`.
Imports: `from app.paths import AUTH_FILE; from app.settings import settings; import auth as auth_mod`.

### Task 5: `app/auth/middleware.py` + `app/auth/pages.py`
- middleware.py: `_PUBLIC_PREFIXES, _PUBLIC_EXACT, _is_public, _wants_html, _current_device, auth_middleware`
- pages.py: APIRouter with 9 handlers (`/setup`, `/setup/verify`, `/login`, `/logout`, `/devices`, `/devices/revoke`) + `_AUTH_PAGE_CSS, _page, _ua_short, _html_escape`

In server.py: `app.middleware("http")(auth_middleware)` + `app.include_router(auth_pages_router)`.

### Task 6: `app/agent/content.py`
Move: `_read_text_safe, _read_xlsx_as_text, _build_user_content`.
Imports: persistence helpers.

### Task 7: `app/agent/permission.py`
Move: `AUTO_ALLOW, CHAT_TOOLS, can_use_tool, truncate, summarize_input`.
Imports: `from app.state import state; from app.ws.broadcast import broadcast; from app.settings import settings; import push`.

### Task 8: `app/agent/options.py`
Move: `CHAT_SYSTEM_PROMPT, AVAILABLE_MODES, AVAILABLE_MODELS, PB_MCP_SERVER init, make_options`.
Imports: `from app.agent.permission import AUTO_ALLOW, CHAT_TOOLS, can_use_tool; from app.state import state; import pb_tools`.

**KEEP** `/home/dev/phone-bridge/CHECKIN.md` literal in CHAT_SYSTEM_PROMPT verbatim.

### Task 9: `app/agent/session.py`
Move: `init_client, open_session, new_session`.
Imports: `from app.agent.options import make_options; from app.persistence.files import _resolve_in_root, _to_rel; from app.state import state; from app.ws.broadcast import broadcast; import db`.

### Task 10: `app/agent/turn.py`
Move: `_save_msg, _block_to_event, run_user_turn`.
Imports: `from app.agent.content import _build_user_content; from app.agent.permission import summarize_input, truncate; from app.state import state; from app.ws.broadcast import broadcast; import db`.

### Task 11: `app/reporting/weekly_report.py`
Move: `_weekly_report_posted`.
Imports: `from app.state import state; from app.ws.broadcast import broadcast; import push; import report`.

### Task 12: 10 API routers

Each becomes its own sub-task + commit:

| Sub-task | File | Routes | Server.py lines |
|---|---|---|---|
| 12.1 | `app/api/__init__.py` + `app/api/meta.py` | /api/health, /api/usage, /api/meta | 1164-1182, 1433-1490 |
| 12.2 | `app/api/well_known.py` | 2× /.well-known/oauth-* | 1282-1318 |
| 12.3 | `app/api/push.py` | vapid-public-key, subscribe, unsubscribe | 1320-1337 |
| 12.4 | `app/api/today_todos.py` | GET /api/today-todos, POST .../ack + _PBError + helpers | 1184-1280 |
| 12.5 | `app/api/browse.py` | GET /api/browse, POST /api/mkdir | 1339-1391 |
| 12.6 | `app/api/sessions.py` | 5 sessions routes | 1393-1431, 2090-2111 |
| 12.7 | `app/api/uploads.py` | POST /api/upload | 2113-2166 |
| 12.8 | `app/api/poi.py` | GET /api/poi/around + 3 providers + merge | 1828-2069 |
| 12.9 | `app/api/settings.py` | weekly-report + notion-sync + _pb_sync_global | 1438-1490, 1551-1633 |
| 12.10 | `app/api/sync.py` | sync/now + targets CRUD + registry export + helpers | 1492-1551, 1639-1828 |

Each router file structure:
```python
from __future__ import annotations
from fastapi import APIRouter, ...
# (other needed imports from app/db/etc)

router = APIRouter()

@router.get("/api/...")
async def ...(...):
    ...  # body verbatim
```

In server.py: `from app.api.X import router as X_router; app.include_router(X_router)`.

### Task 13: `app/ws/handler.py` — the boss

Move: `/ws + handle_ws_message + handle_cmd` (lines 2180-2362).

Imports:
```python
from __future__ import annotations
import asyncio
import json
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from app.agent.options import AVAILABLE_MODELS
from app.agent.session import init_client, new_session, open_session
from app.agent.turn import run_user_turn
from app.auth.state import auth_state
from app.persistence.files import _resolve_in_root, uploads_dir, _to_rel
from app.state import state
from app.ws.broadcast import broadcast
import auth as auth_mod
import db
```

**PRESERVE** the BRIDGE_RECORD hooks added in Task 0 — copy them into the new file. They get removed at Task 16.

In server.py: `from app.ws.handler import router as ws_router; app.include_router(ws_router)`.

---

## Task 14: `app/main.py` + thin `server.py` shim

**Files:**
- Create: `app/main.py`
- Replace: `server.py` (thin re-export)

- [ ] **Step 1: Create `app/main.py`** with:
- FastAPI app + lifespan + CORS + StaticFiles mount + 4 static routes
- 12 router includes
- `if __name__ == "__main__":` uvicorn launcher

The lifespan reads `settings.default_cwd` and writes `state.cwd_root = ...; state.cwd = state.cwd_root` BEFORE anything else, then runs the rest of startup (push.init, db.init, pb_refresh, weekly-report scheduler, default session creation, etc) — same sequence as the old server.py lifespan.

CORS block:
```python
_origins_env = settings.allowed_origins
if _origins_env.strip() == "*":
    _allowed_origins = ["*"]
else:
    _allowed_origins = [o.strip() for o in _origins_env.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)
```

- [ ] **Step 2: Replace `server.py`** with a 16-line shim:

```python
"""Thin shim — the FastAPI app lives in app.main now.

Kept so:
- systemd's ExecStart=uvicorn server:app keeps working without unit edits
- `python server.py` still launches the dev server
- external scripts that `import server` see the same `app` symbol

Phase 6 cleanup: switch the systemd unit to `app.main:app` and delete this file.
"""
from app.main import app

if __name__ == "__main__":
    import uvicorn
    from app.settings import settings
    uvicorn.run("server:app", host=settings.host, port=settings.port)
```

- [ ] **Step 3: Sanity + line counts**

```bash
python -c "import server; print(server.app.__class__.__name__)"  # FastAPI
wc -l server.py app/main.py
```

Expected: `server.py < 20`, `app/main.py < 200`.

- [ ] **Step 4: Smoke**

```bash
python tests/smoke_backend.py
```

- [ ] **Step 5: Commit**

```bash
git add app/main.py server.py
git commit -m "refactor(main): land app.main as the FastAPI entrypoint

server.py shrunk to a 16-line shim preserving 'server:app' uvicorn target."
```

---

## Task 15: Deploy + record after-fixture + diff = 0

- [ ] **Step 1: Deploy**

```powershell
deploy
```

- [ ] **Step 2: Re-enable BRIDGE_RECORD with after.jsonl path**

```bash
ssh dashboard-server "sudo systemctl edit phone-bridge"
# Set:
#   Environment="BRIDGE_RECORD=1"
#   Environment="BRIDGE_RECORD_PATH=/home/dev/phone-bridge/tests/fixtures/phase2_after.jsonl"
sudo systemctl daemon-reload
sudo systemctl restart phone-bridge
```

- [ ] **Step 3: Replay the SAME 100 HTTP + 30 WS interactions** as Task 0 Step 4

- [ ] **Step 4: Stop recorder, pull after.jsonl**

```bash
scp dashboard-server:/home/dev/phone-bridge/tests/fixtures/phase2_after.jsonl tests/fixtures/
```

Remove the BRIDGE_RECORD env vars from systemd edit; restart.

- [ ] **Step 5: Diff**

```bash
python tests/replay.py diff tests/fixtures/phase2_baseline.jsonl tests/fixtures/phase2_after.jsonl
```

Expected: `OK: <N> records match after normalization`.

If FAIL with new fields not handled by normalization — extend `_normalize_value`'s rules and re-run. If FAIL with a real regression — git bisect within Phase 2 commits to find the responsible task; fix or revert.

- [ ] **Step 6: Commit after.jsonl as evidence**

```bash
git add tests/fixtures/phase2_after.jsonl
git commit -m "test(replay): record post-Phase-2 traffic; diff vs baseline = 0"
```

---

## Task 16: Strip recorder + completion report + finish branch

- [ ] **Step 1: Remove BRIDGE_RECORD scaffolding**

Delete the `if os.environ.get("BRIDGE_RECORD"):` blocks and surrounding middleware + WS frame hooks from `app/main.py` and `app/ws/handler.py`. `tests/replay.py` and the two fixture files STAY (evidence + future use).

- [ ] **Step 2: Final smoke + deploy**

```bash
python tests/smoke_backend.py
deploy
python tests/smoke_backend.py  # post-deploy
```

- [ ] **Step 3: Write Phase 2 completion report**

Append to `CHANGELOG.md` after the Phase 1 entry. Template per the Phase 1 completion report.

- [ ] **Step 4: Update spec progress table**

Mark Phase 2 ✅ (or 🚧 已部署 待合并 if you want to soak) with merge SHA; 下一步入口 → Phase 3.

- [ ] **Step 5: Commit + invoke finishing-a-development-branch**

```bash
git add app/main.py app/ws/handler.py CHANGELOG.md docs/superpowers/specs/2026-06-06-refactor-roadmap.md
git commit -m "docs(changelog): Phase 2 completion report + strip recorder"
```

Use `superpowers:finishing-a-development-branch`. Choose Option 1.

---

## Coverage Checklist (for Task 0 + Task 15 recording)

When recording traffic, hit each item to maximize replay coverage:

**HTTP** (≥ 80 records):
- `/api/health` anonymous + authed (2)
- Login flow: `/setup`, `/setup/verify`, `/login` (5)
- Sessions: GET list, POST create, GET single, PATCH rename, DELETE (5+5 = 10)
- Meta: `/api/meta`, `/api/usage`, `/api/today-todos`, `/api/vapid-public-key` (4)
- Browse: `/api/browse?path=...` 4 different paths; `/api/mkdir` success+409+400 (7)
- Upload: image + pdf + text + xlsx (4)
- Weekly report: GET/PUT/run-now (3)
- Notion sync settings: GET/PUT (2)
- Sync ops: now / targets CRUD (POST/PATCH/DELETE) / registry export (5)
- POI: 2 different coord regions (2)
- Today-todos ack (1)
- `/.well-known/*` × 2
- Static `/`, `/sw.js`, `/manifest.json`, `/icon.svg` (4)
- Error codes: 404 (sessions/{bad}), 400 (mkdir invalid), 401 (no cookie), 413 (upload too big) (4)

**WebSocket** (≥ 20 frames over the conversation):
- hello (1)
- user_message → assistant_text → tool_use → permission_request → respond → tool_result → turn_done (≥ 7)
- cmd:set_auto_approve, cmd:cwd, cmd:new_session, cmd:set_model, cmd:cancel (mid-turn), cmd:rename_session, cmd:delete_session, cmd:load_session, cmd:switch_workspace (≥ 9)

Both Task 0 and Task 15 must drive the SAME interactions for the comparator to byte-diff meaningfully.

---

## Self-Review

**1. Spec coverage**:
- ✅ 8 sub-packages from spec (`api/`, `auth/`, `ws/`, `agent/`, `persistence/`, `reporting/`, `main.py`)
- ⚠️ `deps.py` from spec NOT created. Rationale: per-request Depends weren't needed; all shared state is module-level (`state`, `auth_state`). Add `deps.py` in a future phase if a real DI use-case emerges.
- ⚠️ POI Strategy abstraction DEFERRED. Phase 2 moves the 3 providers verbatim; the Strategy refactor is a follow-up that doesn't relocate code. Out of scope for "pure relocation".
- ✅ CORS displayed origins — addressed in Task 14 main.py CORS block.
- ⚠️ Bare except narrowing — narrowed only inside moved files. 37 → ~32. Phase 6 finishes the sweep.
- ✅ `server.py` < 200 lines after — becomes 16 lines (shim).
- ✅ `tests/replay.py` with diff = 0 → Task 15.

**2. Placeholder scan** — no TBD/TODO. Runtime values clearly marked.

**3. Type consistency**:
- `state` singleton signature stable across Tasks 1-13
- `broadcast(event: dict)` signature consistent
- Router naming convention: every file exports `router = APIRouter()`; main imports as `from app.api.X import router as X_router`
- Function names not renamed; only locations change

**4. Order dependencies**:
- Task 0 MUST precede Task 1 (need baseline before any move)
- Task 1 precedes everything (every later module imports `state`)
- Tasks 2-3 before 6-10 (agent needs persistence + broadcast)
- Tasks 4-5 (auth) independent of agent
- Tasks 7-10 strict chain (permission → options → session → turn)
- Task 12.* after agent stack done
- Task 13 (ws.handler) MUST be last among code moves
- Task 14 (main.py) after Task 13
- Tasks 15-16 close the loop

**5. Honest scope**:
- 16 distinct commits (+ ~10 sub-commits in Task 12). Wall-clock: 3-5 days.
- Each task independently revertible.
- Replay fixture is the critical safety net.

---

**Plan complete.**
