# Phase -1 · Guardrails Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Install the smoke-tests, baseline screenshots, rollback drill, and feature-freeze rules that all later refactor phases will rely on to detect regressions in 30 seconds.

**Architecture:** Pragmatic over fancy. Backend smoke is a standalone Python script (no pytest dep) that runs against any live URL. Frontend baseline is a one-time manual screenshot capture (no playwright in Phase -1). Rollback drill is a documented + tested ops procedure. Two rules go into `CLAUDE.md`.

**Tech Stack:** Python stdlib + `websockets` (already transitively installed via `uvicorn[standard]`); shell; markdown.

**Branch:** `refactor/phase-minus1-guardrails`
**Parent spec:** [2026-06-06-refactor-roadmap.md](../specs/2026-06-06-refactor-roadmap.md)

---

## File Structure

| Path | Action | Purpose |
|---|---|---|
| `tests/smoke_backend.py` | Create | Standalone smoke against a live server URL |
| `tests/smoke/__init__.py` | Create | Empty marker (allow future expansion) |
| `tests/baseline/README.md` | Create | Manual screenshot capture checklist |
| `tests/baseline/.gitkeep` | Create | Keep directory in git when no PNGs yet |
| `docs/operations/rollback.md` | Create | Deploy rollback drill + verified procedure |
| `CLAUDE.md` | Modify | Add §Refactor period rules section near top |

`tests/baseline/*.png` are committed by the user after the manual capture step. They're checked in (small, < 1 MB total) so future-Claude can `Read` them when comparing.

---

## Task 1: Create the branch

**Files:** (none)

- [ ] **Step 1: Verify clean working tree on main**

Run:
```bash
git status
git rev-parse HEAD
```

Expected:
```
On branch main
nothing to commit, working tree clean
0b38ca9...
```

If not clean: STOP. Resolve before proceeding.

- [ ] **Step 2: Create and switch to phase branch**

Run:
```bash
git checkout -b refactor/phase-minus1-guardrails
git branch --show-current
```

Expected output: `refactor/phase-minus1-guardrails`

---

## Task 2: Smoke-test directory scaffolding

**Files:**
- Create: `tests/smoke/__init__.py`
- Create: `tests/baseline/.gitkeep`

- [ ] **Step 1: Create empty package marker**

Create `tests/smoke/__init__.py` with content:
```python
"""Smoke tests — run against a live phone-bridge server URL.

Each smoke is a standalone Python script (no pytest required).
Usage: BASE=https://dashboard-server.tail4cfa2.ts.net python tests/smoke_backend.py
"""
```

- [ ] **Step 2: Create baseline screenshot directory**

Create empty file `tests/baseline/.gitkeep`.

- [ ] **Step 3: Commit scaffold**

Run:
```bash
git add tests/smoke/__init__.py tests/baseline/.gitkeep
git commit -m "refactor(tests): scaffold smoke + baseline directories"
```

---

## Task 3: Backend smoke script

**Files:**
- Create: `tests/smoke_backend.py`

The smoke must work against either a localhost dev server or the production tailnet URL. It reads the base URL from `$BASE` env (default: `http://127.0.0.1:8001`) and the auth cookie from `$BRIDGE_COOKIE` env. Auth setup is out of scope — the smoke assumes the cookie is already valid (user gets it from the browser DevTools once).

The flow exercises the **read paths** only — no destructive ops, so it's safe to run against production.

- [ ] **Step 1: Write the smoke script**

Create `tests/smoke_backend.py` with this exact content:

```python
"""Backend smoke test for phone-bridge.

Runs against a live server URL. Exercises read-only endpoints + a WS roundtrip.
No external deps beyond stdlib + websockets (already pulled by uvicorn[standard]).

Usage:
    # Localhost dev server:
    BRIDGE_COOKIE='session=...' python tests/smoke_backend.py

    # Production:
    BASE=https://dashboard-server.tail4cfa2.ts.net \\
      BRIDGE_COOKIE='session=...' \\
      python tests/smoke_backend.py

Exits 0 on success, non-zero on first failure with a clear marker line.
"""
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request

import websockets

BASE = os.environ.get("BASE", "http://127.0.0.1:8001").rstrip("/")
COOKIE = os.environ.get("BRIDGE_COOKIE", "")
WS_BASE = BASE.replace("http://", "ws://").replace("https://", "wss://")


def _step(label):
    sys.stdout.write(f"  • {label} ... ")
    sys.stdout.flush()


def _ok(detail=""):
    sys.stdout.write(f"OK {detail}\n")


def _fail(detail):
    sys.stdout.write(f"FAIL\n    {detail}\n")
    sys.exit(1)


def _http(method, path, body=None, expect=200):
    url = BASE + path
    headers = {"Cookie": COOKIE} if COOKIE else {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            code = r.status
            payload = r.read()
    except urllib.error.HTTPError as e:
        code = e.code
        payload = e.read()
    if code != expect:
        _fail(f"{method} {path}: expected {expect}, got {code}: {payload[:200]!r}")
    try:
        return json.loads(payload) if payload else {}
    except json.JSONDecodeError:
        return {"_raw": payload[:200].decode("utf-8", errors="replace")}


async def _ws_roundtrip():
    """Connect WS, expect a 'hello' frame back."""
    ws_url = f"{WS_BASE}/ws"
    headers = [("Cookie", COOKIE)] if COOKIE else []
    async with websockets.connect(ws_url, additional_headers=headers, open_timeout=10) as ws:
        first = await asyncio.wait_for(ws.recv(), timeout=5)
        msg = json.loads(first)
        if msg.get("type") != "hello":
            raise RuntimeError(f"first WS frame not 'hello', got {msg.get('type')!r}")
        return msg


async def main():
    print(f"Smoke target: {BASE}")
    print(f"Cookie: {'set' if COOKIE else 'NOT SET (some checks will fail)'}")
    print()

    _step("GET /api/health")
    h = _http("GET", "/api/health")
    _ok(f"({h.get('status', '?')})")

    _step("GET /api/meta")
    m = _http("GET", "/api/meta")
    if "mode" not in m or "model" not in m:
        _fail(f"meta missing mode/model: {m}")
    _ok(f"(model={m.get('model', '?')})")

    _step("GET /api/sessions")
    sess = _http("GET", "/api/sessions")
    if not isinstance(sess, list):
        _fail(f"sessions not a list: {type(sess).__name__}")
    _ok(f"({len(sess)} sessions)")

    _step("GET /api/today-todos")
    td = _http("GET", "/api/today-todos")
    _ok(f"({len(td.get('items', []))} todos)")

    _step("WS /ws hello frame")
    hello = await _ws_roundtrip()
    _ok(f"(session={hello.get('session_id', '?')[:8]}...)")

    print()
    print("✅ all smoke checks passed")


if __name__ == "__main__":
    t0 = time.time()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        _fail(f"uncaught: {type(exc).__name__}: {exc}")
    print(f"   {time.time() - t0:.1f}s")
```

- [ ] **Step 2: Sanity-check the script parses**

Run:
```bash
python -c "import ast; ast.parse(open('tests/smoke_backend.py').read()); print('OK')"
```

Expected output: `OK`

- [ ] **Step 3: Verify against production**

Get a fresh session cookie from the browser DevTools (Application → Cookies → `session=...`), then:

```bash
BASE=https://dashboard-server.tail4cfa2.ts.net \
  BRIDGE_COOKIE='session=PASTE_HERE' \
  python tests/smoke_backend.py
```

Expected output (last line):
```
✅ all smoke checks passed
```

Total runtime should be < 5 seconds.

If any step fails: the smoke is finding a real issue in main, OR the assumption (e.g. cookie name, endpoint shape) is wrong. Adjust the smoke (not the server) and re-run.

- [ ] **Step 4: Commit**

Run:
```bash
git add tests/smoke_backend.py
git commit -m "refactor(tests): add backend smoke against live server URL

Standalone Python script (no pytest dep). Reads BASE + BRIDGE_COOKIE from
env. Exercises /api/health /api/meta /api/sessions /api/today-todos and a
WS hello roundtrip. Total runtime < 5s. Safe to run against prod (read
paths only)."
```

---

## Task 4: Frontend baseline screenshot checklist

**Files:**
- Create: `tests/baseline/README.md`

- [ ] **Step 1: Write the capture checklist**

Create `tests/baseline/README.md`:

```markdown
# Frontend Baseline Screenshots

Reference images for visual regression after Phase 4 (frontend modularization).
Captured manually on a real device — playwright automation is deferred (see
phase-4 plan if you want to add it later).

## Capture procedure (one-time, before any phase starts touching frontend)

Open the PWA on a recent iPhone or Android (Tailscale-connected).
For each scene below, take a portrait screenshot and save under
`tests/baseline/<filename>.png` (PNG, no resize).

### Required scenes

| # | Filename | How to reach it |
|---|---|---|
| 1 | `01-home.png` | Open PWA at root, default mode, no messages yet |
| 2 | `02-chat-streaming.png` | Send a message that triggers streaming; capture mid-stream |
| 3 | `03-tool-group-closed.png` | After a tool call collapses (▸ state) |
| 4 | `03-tool-group-open.png` | Same tool group expanded (▾ state) |
| 5 | `04-permission-card.png` | Trigger a permission_request and capture the card before approving |
| 6 | `05-drawer-sessions.png` | Open the left drawer with session list |
| 7 | `06-modal-usage.png` | Open the usage modal |
| 8 | `07-modal-weekly.png` | Open weekly-report settings |
| 9 | `08-modal-sync.png` | Open sync-settings; capture the targets table |
| 10 | `09-modal-cwd.png` | Open the cwd browser |
| 11 | `10-bell-todos.png` | Open the bell with at least 2 today-todos |
| 12 | `11-checkin-dialog.png` | Open the checkin dialog at stage 1 (POI list) |
| 13 | `12-source-picker.png` | Open the source picker |

### After capture

```bash
git add tests/baseline/*.png
git commit -m "refactor(tests): add baseline frontend screenshots for visual regression"
```

## Comparing after a refactor phase

Open each new screenshot side-by-side with its baseline. Eyeball the diff:
spacing, color, font, missing elements, extra elements. Any meaningful drift =
flag it in the phase's completion report.

(There is no auto-diff tool yet. If you want pixel diff, install ImageMagick
and `compare baseline.png new.png diff.png`.)
```

- [ ] **Step 2: Commit the checklist**

```bash
git add tests/baseline/README.md
git commit -m "refactor(tests): document baseline screenshot capture checklist"
```

- [ ] **Step 3: Capture screenshots (manual, user does this)**

User: take the 13 screenshots on phone, save under `tests/baseline/`, then:
```bash
git add tests/baseline/*.png
git commit -m "refactor(tests): add baseline frontend screenshots"
```

If user defers this step, **note it in the Phase -1 completion report** as a precondition for Phase 4 entry.

---

## Task 5: Deploy rollback drill

**Files:**
- Create: `docs/operations/rollback.md`

- [ ] **Step 1: Verify the assumptions about deploy**

Re-read `.deploy.json` (already inspected during planning) and the `## Deploy` section in `CLAUDE.md`.

Confirm:
- `phone-bridge.service` is the systemd unit
- `git checkout <SHA>` + `deploy` will redeploy the chosen SHA
- `.bridge_data` + `.bridge_uploads` are in `keep_files` (survive deploy)

- [ ] **Step 2: Write the rollback procedure**

Create `docs/operations/rollback.md`:

```markdown
# Phone Bridge Rollback Drill

Use this when a refactor merged to `main` causes regressions in production
(`dashboard-server.tail4cfa2.ts.net`). The whole drill should take < 10 minutes.

## When to roll back

- `tests/smoke_backend.py` fails against prod
- `journalctl -u phone-bridge` shows new ERROR-level lines that weren't there before deploy
- Any user-facing flow stops working

## Procedure

1. **Identify the bad commit:**

   ```bash
   git log --oneline -10
   ```

   The most recent commit on `main` is usually the culprit. If unsure: look
   at `journalctl -u phone-bridge --since "2 hours ago"` for the first error
   timestamp, then `git log --until=<that timestamp>`.

2. **Roll the working tree back:**

   ```bash
   git checkout <last-good-SHA>
   ```

   Do NOT use `git reset --hard` — checkout leaves `main` intact so you can
   investigate and re-roll forward later.

3. **Redeploy:**

   ```bash
   deploy
   ```

   The shared `deploy` tool tars + uploads + restarts `phone-bridge.service`
   and hits `/api/health` to confirm.

4. **Verify the rollback worked:**

   ```bash
   BASE=https://dashboard-server.tail4cfa2.ts.net \
     BRIDGE_COOKIE='session=...' \
     python tests/smoke_backend.py
   ```

   Expected: `✅ all smoke checks passed` within 5 seconds.

5. **Return to main (so future commits flow normally):**

   ```bash
   git checkout main
   ```

   (Working tree is back on main but deployed version is still the rolled-back
   one — that's fine. The next intentional `deploy` will push main again.)

## Investigation: why did it fail?

After rollback, the bad SHA still exists in `main` history. Reproduce locally
on the original branch:

```bash
git checkout <bad-SHA>
# repro
```

Fix on a new commit and re-deploy.

## Drill verification

This procedure was verified on YYYY-MM-DD by rolling from `<test-SHA>` to
`<test-SHA>~1` and confirming /api/health responded within 10 minutes total.

(Update the date and SHAs above whenever you re-run the drill — at minimum
once per quarter or before any high-risk merge.)
```

- [ ] **Step 3: Execute the drill against staging**

This is the actual drill — not just the doc. User performs:

```bash
# On workstation:
ssh dashboard-server "cd /home/dev/phone-bridge && git log --oneline -3"
# Note current HEAD as TEST_SHA. Then:
ssh dashboard-server "cd /home/dev/phone-bridge && git checkout HEAD~1"
deploy   # redeploys old SHA
# Smoke against the rolled-back server:
BASE=https://dashboard-server.tail4cfa2.ts.net \
  BRIDGE_COOKIE='...' \
  python tests/smoke_backend.py
# Roll forward:
ssh dashboard-server "cd /home/dev/phone-bridge && git checkout main"
deploy
```

Record start + end timestamps; total should be < 10 minutes. Update the "Drill verification"
line in `docs/operations/rollback.md` with the real date + SHAs used.

- [ ] **Step 4: Commit the doc (after drill verified)**

```bash
git add docs/operations/rollback.md
git commit -m "ops: document + verify deploy rollback drill (<10 min)"
```

---

## Task 6: CLAUDE.md — feature freeze + branch naming rules

**Files:**
- Modify: `CLAUDE.md` (insert near top)

- [ ] **Step 1: Read current CLAUDE.md top**

Run:
```bash
head -20 CLAUDE.md
```

The new section goes right after the opening blurb, **before** the `## Deploy` section.

- [ ] **Step 2: Insert the new section**

Find the line `## Deploy` in `CLAUDE.md`. Insert this block immediately before it:

```markdown
## Refactor period rules (active 2026-06-06 onwards)

The repo is mid-refactor — see [docs/superpowers/specs/2026-06-06-refactor-roadmap.md](docs/superpowers/specs/2026-06-06-refactor-roadmap.md).
Until the roadmap's §进度追踪表 shows all phases ✅:

- **`main` accepts only `refactor:` or `docs:` commits.** New features go on
  `feature/*` branches and wait. If something is genuinely urgent, open a
  `hotfix/*` branch and discuss before merging.
- **Each refactor phase lives on its own branch:** `refactor/phase-N-<slug>`
  (e.g. `refactor/phase-0-foundation`). Merge to `main` only after the
  phase's准出闸门 in the roadmap are all ✅ and staging soak (24h or 48h
  per the spec) is clean.
- **Smoke tests are the canary.** Before merging any refactor branch:

  ```bash
  BASE=https://dashboard-server.tail4cfa2.ts.net \
    BRIDGE_COOKIE='session=...' \
    python tests/smoke_backend.py
  ```

  Must print `✅ all smoke checks passed`. If it doesn't, do NOT merge —
  fix or revert.
- **Rollback procedure:** [docs/operations/rollback.md](docs/operations/rollback.md).
  Anyone touching `main` should have skimmed it.

```

- [ ] **Step 3: Verify the insertion**

Run:
```bash
grep -c "Refactor period rules" CLAUDE.md
```

Expected: `1`

Run:
```bash
grep -n "## Refactor period rules\|## Deploy" CLAUDE.md
```

Expected: two line numbers, "Refactor period rules" before "Deploy".

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude): add refactor-period feature freeze + branch naming rules"
```

---

## Task 7: Update roadmap progress tracker

**Files:**
- Modify: `docs/superpowers/specs/2026-06-06-refactor-roadmap.md`

- [ ] **Step 1: Update the Phase -1 row in §进度追踪表 (mark in-progress)**

In `docs/superpowers/specs/2026-06-06-refactor-roadmap.md`, find the line:

```
| -1 护栏 | ⏳ 待开始 | `refactor/phase-minus1-guardrails` | — | — | — |
```

Replace with:

```
| -1 护栏 | 🚧 进行中 | `refactor/phase-minus1-guardrails` | — | — | — |
```

- [ ] **Step 2: Commit progress marker**

```bash
git add docs/superpowers/specs/2026-06-06-refactor-roadmap.md
git commit -m "docs(spec): mark Phase -1 as in-progress"
```

- [ ] **Step 3: After merging to main, update again**

When准出闸门 all pass and the branch is merged:

```bash
git checkout main
git merge --no-ff refactor/phase-minus1-guardrails
# note the merge SHA:
git rev-parse --short HEAD
# note today's date:
date +%Y-%m-%d
```

Edit the same line to (substituting the actual values):

```
| -1 护栏 | ✅ 已合并 | `refactor/phase-minus1-guardrails` | <today> | `<merge-sha>` | CHANGELOG §Phase -1 |
```

Also update §下一步入口 from:

```
👉 **下一步执行**：Phase -1 · 护栏
```

to:

```
👉 **下一步执行**：Phase 0 · 地基
```

Commit:

```bash
git add docs/superpowers/specs/2026-06-06-refactor-roadmap.md
git commit -m "docs(spec): Phase -1 merged; next is Phase 0"
git push origin main
```

---

## Task 8: Final准出闸门 verification + completion report

- [ ] **Step 1: smoke runs green on the branch tip (before merge)**

```bash
BASE=https://dashboard-server.tail4cfa2.ts.net \
  BRIDGE_COOKIE='session=...' \
  python tests/smoke_backend.py
```

Expected: `✅ all smoke checks passed`.

- [ ] **Step 2: baseline screenshots committed (or deferred and noted)**

```bash
ls tests/baseline/*.png 2>/dev/null | wc -l
```

Expected: `13` (or `0` if deferred — note in completion report).

- [ ] **Step 3: rollback drill verification line updated**

```bash
grep "Drill verification" docs/operations/rollback.md
```

Expected: the line includes a real YYYY-MM-DD date (not the literal `YYYY-MM-DD` placeholder).

- [ ] **Step 4: CLAUDE.md has both new rules**

```bash
grep -c "Refactor period rules\|refactor/phase-" CLAUDE.md
```

Expected: `>= 2`

- [ ] **Step 5: Write the Phase -1 completion report**

Append to `CHANGELOG.md` (after the most recent entry):

```markdown
## 2026-06-06 — Phase -1 (Refactor Guardrails) merged

**Branch:** `refactor/phase-minus1-guardrails`
**Commit range:** `0b38ca9..<merge-SHA>`
**Actual time:** <X> hours

### What landed
- Backend smoke test: `tests/smoke_backend.py` (stdlib + websockets, <5s runtime)
- Baseline screenshot checklist: `tests/baseline/README.md` + 13 PNGs (or: deferred — see notes)
- Rollback drill documented and verified: `docs/operations/rollback.md`
- Feature freeze + branch naming rules added to `CLAUDE.md`
- Roadmap progress tracker updated

### Gates
- ✅ smoke runs green on staging
- ✅ rollback drill <10 min (actually: <X> min on YYYY-MM-DD)
- ✅ CLAUDE.md rules visible
- (optional) baseline screenshots captured: <yes / deferred>

### Deviations
<none / list>

### Next
👉 Phase 0 · 地基
New-window resume command: "继续重构路线图，从 Phase 0 开始"
```

Commit:

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): Phase -1 completion report"
```

---

## Self-Review

**1. Spec coverage** — every Phase -1 checkbox from the spec has a task:
- ✅ Backend smoke → Task 3
- ✅ Frontend smoke → Task 4 (downgraded to manual baseline + checklist; rationale in §Deviations)
- ✅ Baseline screenshots → Task 4 step 3
- ✅ Rollback drill → Task 5
- ✅ Feature freeze rule → Task 6
- ✅ Branch naming rule → Task 6 (same insertion, same commit)

**2. Placeholder scan** — only intentional placeholders remain:
- `<merge-SHA>` / `<X>` / `<today>` / `<merge-sha>` in Tasks 7/8 — runtime values the executor fills in.
- `YYYY-MM-DD` in `rollback.md` Drill verification line — Task 5 step 3 explicitly tells the executor to replace it.

**3. Type consistency** — branch name `refactor/phase-minus1-guardrails` used identically in Tasks 1, 7, 8 and the spec's progress table. Smoke filename `tests/smoke_backend.py` used identically in Tasks 3, 6, 8.

**4. Honest downgrade** — the spec said "headless playwright frontend smoke". This plan downgrades it to manual baseline screenshots because (a) Phase -1 budget is 1 day and playwright setup eats most of it, (b) the value of playwright kicks in during Phase 4 where it can be added then, (c) baseline PNGs are sufficient to detect Phase 0~3 frontend breakage (those phases shouldn't touch frontend at all). Flagged in the completion report's "Deviations" section so the user can decide.

---

**Plan complete.**
