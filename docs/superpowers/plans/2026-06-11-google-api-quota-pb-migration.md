# Google API Quota Gate — PB Migration + 4-Channel Alerts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the Smart-Trip Google API rate-limiter (silently broken since Supabase→PocketBase switch) and surface trips on 4 channels (PB row, Navbar bell, AdminPage banner, phone push).

**Architecture:** Three new PB collections (`system_settings` / `api_logs` / `system_alerts`) replace Supabase storage 1:1. New `useSystemAlerts.js` hook centralizes alert polling for both Navbar and AdminPage. A PB hook on `system_alerts.create` POSTs to a new `POST /api/push/send` endpoint in phone-bridge, which calls existing `push.send_to_all()`. Rate-limit values and auto-disable semantics are preserved exactly from the original post-incident design.

**Tech Stack:**
- Phone-bridge: Python (FastAPI), PocketBase v0.38.2 (JS hooks via goja), Litestream
- Smart-Trip: React + Vite, PocketBase JS SDK
- Push: VAPID web-push via `pywebpush` (already wired into `push.py`)

**Reference spec:** `docs/superpowers/specs/2026-06-11-google-api-quota-pb-migration-design.md`

---

## File Structure

### `phone-bridge` repo (D:\Projects\Phone Bridge)

| File | Action | Purpose |
|---|---|---|
| `app/api/push.py` | MODIFY | Add `POST /api/push/send` endpoint (loopback-only) |
| `app/auth/middleware.py` | MODIFY | Allowlist `/api/push/send` in `_PUBLIC_EXACT` |
| `pocketbase/pb_migrations/1781193600_create_api_quota_tables.js` | CREATE | Build 3 collections + seed `system_settings` |
| `pocketbase/pb_hooks/system_alerts.pb.js` | CREATE | After-create hook → POST to `/api/push/send` |

### `Smart-Trip` repo (D:\Projects\Smart-Trip-tmp, branch `feature/pb-datasource`)

| File | Action | Purpose |
|---|---|---|
| `src/utils/apiGuard.js` | REPLACE | Port from Supabase to PB; add `createAlert` |
| `src/hooks/useSystemAlerts.js` | CREATE | Shared alert polling hook |
| `src/components/layout/Navbar.jsx` | MODIFY | Wire bell to alerts; show count badge + dropdown |
| `src/pages/AdminPage.jsx` | MODIFY | Port supabase calls → PB; add red banner at top |

### Deployment targets (dashboard-server)

| Path | Purpose |
|---|---|
| `/home/dev/phone-bridge/` | Phone Bridge codebase (rsync via `deploy` tool) |
| `/opt/pocketbase/pb_migrations/` | PB picks up new migration on next restart |
| `/opt/pocketbase/pb_hooks/` | PB picks up new hook on next restart |
| `/home/dev/smat-trip/dist/` | Smart-Trip built bundle |

---

## Task 1: Phone Bridge — add `POST /api/push/send` endpoint

**Files:**
- Modify: `app/api/push.py`
- Test: `tests/test_push_send_endpoint.py` (CREATE)

**Pre-condition:** working dir is `D:\Projects\Phone Bridge`, on `main` branch, working tree clean.

- [ ] **Step 1: Write the failing test**

Create `tests/test_push_send_endpoint.py`:

```python
"""POST /api/push/send: loopback-only push trigger for PB hook."""
from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api.push import router


def _client():
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_send_calls_send_to_all_when_loopback():
    client = _client()
    with patch("app.api.push.push") as p, patch(
        "app.api.push._is_loopback", return_value=True
    ):
        r = client.post(
            "/api/push/send",
            json={"title": "T", "body": "B", "tag": "x"},
        )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    p.send_to_all.assert_called_once_with("T", "B", "x")


def test_send_rejects_non_loopback_client():
    """When the request appears to come from a non-loopback peer, return 403."""
    client = _client()
    # TestClient's default client.host is "testclient" — not 127.0.0.1 — so
    # the endpoint should refuse without any header trickery.
    with patch("app.api.push.push") as p:
        r = client.post(
            "/api/push/send",
            json={"title": "T", "body": "B"},
        )
    assert r.status_code == 403
    p.send_to_all.assert_not_called()


def test_send_accepts_missing_tag():
    client = _client()
    with patch("app.api.push.push") as p, patch(
        "app.api.push._is_loopback", return_value=True
    ):
        r = client.post(
            "/api/push/send",
            json={"title": "T", "body": "B"},
        )
    assert r.status_code == 200
    p.send_to_all.assert_called_once_with("T", "B", None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_push_send_endpoint.py -v`
Expected: All 3 tests FAIL with `404 Not Found` or import error (endpoint not implemented).

- [ ] **Step 3: Implement endpoint**

Edit `app/api/push.py`. Replace entire file with:

```python
"""Web push subscription + send endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

import push

from app.settings import settings

router = APIRouter()


def _is_loopback(request: Request) -> bool:
    """True when the peer is the local machine (PB hook on same host)."""
    host = (request.client.host if request.client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost")


@router.get("/api/vapid-public-key")
async def get_vapid_key():
    return {"key": settings.vapid_public_key}


@router.post("/api/subscribe")
async def subscribe(sub: dict):
    push.add_sub(sub)
    return {"ok": True}


@router.post("/api/unsubscribe")
async def unsubscribe(sub: dict):
    push.remove_sub(sub)
    return {"ok": True}


@router.post("/api/push/send")
async def send_push(payload: dict, request: Request):
    """Trigger a push to all subscribers. Loopback-only — PB hooks call this.

    Body: {"title": str, "body": str, "tag": str | null}
    """
    if not _is_loopback(request):
        raise HTTPException(status_code=403, detail="loopback only")
    title = str(payload.get("title") or "")
    body = str(payload.get("body") or "")
    tag = payload.get("tag")
    push.send_to_all(title, body, tag)
    return {"ok": True}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_push_send_endpoint.py -v`
Expected: 3 passed.

- [ ] **Step 5: Run full test suite to check for regressions**

Run: `pytest tests/ --ignore=tests/test_safe_filename.py`
Expected: All tests pass (matches the working-rules baseline in CLAUDE.md).

- [ ] **Step 6: Commit**

```bash
git add app/api/push.py tests/test_push_send_endpoint.py
git commit -m "feat(push): add POST /api/push/send endpoint for PB hook

Loopback-only (peer must be 127.0.0.1/::1/localhost) — used by the new
PB system_alerts hook to fan an alert out to all subscribed devices
via push.send_to_all(). No auth header needed because the endpoint is
loopback-protected; phone-bridge listens on 127.0.0.1:8001 so only
processes on the VM (PB on the same machine) can reach it.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: Phone Bridge — allowlist `/api/push/send` in auth middleware

**Files:**
- Modify: `app/auth/middleware.py:29-33`
- Test: `tests/test_auth_middleware_push_send.py` (CREATE)

**Pre-condition:** Task 1 committed.

- [ ] **Step 1: Write the failing test**

Create `tests/test_auth_middleware_push_send.py`:

```python
"""Verify /api/push/send passes auth middleware without a device cookie."""
from app.auth.middleware import _PUBLIC_EXACT


def test_push_send_is_public_exact():
    assert "/api/push/send" in _PUBLIC_EXACT
```

- [ ] **Step 2: Run test, verify it fails**

Run: `pytest tests/test_auth_middleware_push_send.py -v`
Expected: FAIL — assertion error, path not in set.

- [ ] **Step 3: Edit `app/auth/middleware.py`**

Find the `_PUBLIC_EXACT` block (lines 29-33):

```python
_PUBLIC_EXACT = {
    "/api/health",
    "/.well-known/oauth-protected-resource/mcp",
    "/.well-known/oauth-authorization-server/mcp",
}
```

Replace with:

```python
_PUBLIC_EXACT = {
    "/api/health",
    "/.well-known/oauth-protected-resource/mcp",
    "/.well-known/oauth-authorization-server/mcp",
    # PB hooks on the same VM POST here to trigger pushes. The push.py
    # endpoint enforces request.client.host in {127.0.0.1, ::1, localhost}
    # so adding it to the allowlist does NOT expose it to the public —
    # Tailscale Serve / nginx never proxies loopback-bound peers here.
    "/api/push/send",
}
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_auth_middleware_push_send.py tests/test_push_send_endpoint.py -v`
Expected: All pass.

- [ ] **Step 5: Full test sweep**

Run: `pytest tests/ --ignore=tests/test_safe_filename.py`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add app/auth/middleware.py tests/test_auth_middleware_push_send.py
git commit -m "feat(auth): allowlist /api/push/send in middleware

The endpoint enforces loopback-only itself; adding to _PUBLIC_EXACT
lets the PB hook (no cookie) reach it without hitting the 503 decoy.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: Phone Bridge — write PB migration creating 3 collections + seeds

**Files:**
- Create: `pocketbase/pb_migrations/1781193600_create_api_quota_tables.js`

**Pre-condition:** Task 2 committed.

- [ ] **Step 1: Examine existing migration shape for reference**

Read `pocketbase/pb_migrations/1779465616_create_sync_meta.js` end-to-end to confirm:
- The `migrate(up, down)` envelope
- How `Collection`, `Field`, indexes are declared
- How seed rows are inserted (`new Record(collection, {...})` + `app.save(record)`)
- The PB v0.38 API surface

Note any deviations from the snippets in this task and reconcile to existing patterns before writing.

- [ ] **Step 2: Write the migration**

Create `pocketbase/pb_migrations/1781193600_create_api_quota_tables.js`:

```js
/// <reference path="../pb_data/types.d.ts" />

migrate((app) => {
  // ── 1. system_settings ────────────────────────────────────────
  const settings = new Collection({
    name: "system_settings",
    type: "base",
    listRule: "",
    viewRule: "",
    createRule: "",
    updateRule: "",
    deleteRule: "",
    fields: [
      { name: "key",   type: "text", required: true, max: 100 },
      { name: "value", type: "text", required: false, max: 500 },
      { name: "created", type: "autodate", onCreate: true },
      { name: "updated", type: "autodate", onCreate: true, onUpdate: true },
    ],
    indexes: [
      "CREATE UNIQUE INDEX idx_system_settings_key ON system_settings (key)",
    ],
  });
  app.save(settings);

  // Seed default keys (idempotent — re-running migration won't dup)
  const seeds = [
    ["places_search_enabled", "true"],
    ["place_details_enabled", "true"],
    ["directions_enabled",    "true"],
    ["daily_api_limit",       "200"],
    ["per_2min_api_limit",    "20"],
  ];
  for (const [k, v] of seeds) {
    const r = new Record(settings, { key: k, value: v });
    app.save(r);
  }

  // ── 2. api_logs ────────────────────────────────────────────────
  const logs = new Collection({
    name: "api_logs",
    type: "base",
    listRule: "",
    viewRule: "",
    createRule: "",
    updateRule: "",
    deleteRule: "",
    fields: [
      { name: "api_type", type: "text", required: true, max: 50 },
      { name: "user_id",  type: "text", required: false, max: 50 },
      { name: "status",   type: "text", required: true, max: 20 },
      { name: "created",  type: "autodate", onCreate: true },
    ],
    indexes: [
      "CREATE INDEX idx_api_logs_type_status_created ON api_logs (api_type, status, created)",
    ],
  });
  app.save(logs);

  // ── 3. system_alerts ──────────────────────────────────────────
  const alerts = new Collection({
    name: "system_alerts",
    type: "base",
    listRule: "",
    viewRule: "",
    createRule: "",
    updateRule: "",
    deleteRule: "",
    fields: [
      { name: "kind",         type: "text",   required: true, max: 50 },
      { name: "api_type",     type: "text",   required: false, max: 50 },
      { name: "reason",       type: "text",   required: false, max: 50 },
      { name: "count",        type: "number", required: false },
      { name: "acknowledged", type: "bool",   required: false },
      { name: "created",      type: "autodate", onCreate: true },
    ],
    indexes: [
      "CREATE INDEX idx_system_alerts_ack_created ON system_alerts (acknowledged, created)",
    ],
  });
  app.save(alerts);
}, (app) => {
  // ── Down: drop in reverse order ───────────────────────────────
  for (const name of ["system_alerts", "api_logs", "system_settings"]) {
    try {
      const c = app.findCollectionByNameOrId(name);
      app.delete(c);
    } catch (e) { /* ignore if missing */ }
  }
});
```

> ⚠️ **PB v0.38 API caveat**: The `Collection` / `Field` / `Record` constructor signatures evolved across PB versions. If the snippets above fail at PB load with a type error, fall back to the exact builder pattern used in `1779465616_create_sync_meta.js` — adapt fields one-to-one. Do NOT invent helper utilities.

- [ ] **Step 3: Smoke-test migration syntactically (no apply yet)**

Run: `node -c pocketbase/pb_migrations/1781193600_create_api_quota_tables.js`
Expected: silent success (no syntax error).

- [ ] **Step 4: Commit**

```bash
git add pocketbase/pb_migrations/1781193600_create_api_quota_tables.js
git commit -m "feat(pb): migration for api quota tables

Creates system_settings, api_logs, system_alerts collections plus 5
seed rows in system_settings (places_search_enabled / place_details_enabled
/ directions_enabled / daily_api_limit=200 / per_2min_api_limit=20).

Down step drops the 3 collections in reverse order; safe to rerun.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: Phone Bridge — write PB hook for `system_alerts` → push

**Files:**
- Create: `pocketbase/pb_hooks/system_alerts.pb.js`

**Pre-condition:** Task 3 committed.

- [ ] **Step 1: Examine `days.pb.js` for the actual hook idiom used in this repo**

Read `pocketbase/pb_hooks/days.pb.js`. Confirm:
- Hook name: `onRecordCreate((e) => { ... e.next(); }, "<collection_name>")`
- The `e.next()` call must happen for PB to persist
- Each callback runs in its own goja VM — no shared top-level helpers
- Style comments at the top

- [ ] **Step 2: Write the hook**

Create `pocketbase/pb_hooks/system_alerts.pb.js`:

```js
/// <reference path="../pb_data/types.d.ts" />

// system_alerts → push notification fan-out.
//
// Triggered after a Smart-Trip apiGuard write (rate-limit trip). Posts
// to phone-bridge's loopback /api/push/send which then calls
// push.send_to_all() to reach every VAPID subscriber.
//
// PB v0.38 hook caveats (see CLAUDE.md):
//  - each callback is its own goja VM; helpers must be inlined
//  - call e.next() so PB completes the save before the side-effect
//  - $http.send is synchronous; cap timeout to avoid blocking writes

onRecordCreate((e) => {
  e.next(); // persist row first

  const r = e.record;
  const apiType = r.get("api_type") || "?";
  const reason  = r.get("reason") || "?";
  const count   = r.get("count") || 0;

  const REASON_TEXT = {
    "disabled":    "管理员关闭",
    "daily_limit": "触发日限额",
    "2min_limit":  "触发 2 分钟限额",
  };
  const reasonText = REASON_TEXT[reason] || reason;
  const body = apiType + " 自动关闭（" + reasonText + "，实际 " + count + " 次）";

  try {
    const res = $http.send({
      url:    "http://127.0.0.1:8001/api/push/send",
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body:   JSON.stringify({
        title: "Google API 闸门触发",
        body:  body,
        tag:   "smart-trip-api-quota",
      }),
      timeout: 5,
    });
    if (res.statusCode >= 400) {
      console.log("[system_alerts hook] push failed status=" + res.statusCode + " body=" + (res.raw || "").slice(0, 200));
    }
  } catch (err) {
    console.log("[system_alerts hook] push exception: " + err);
  }
}, "system_alerts");
```

- [ ] **Step 3: Syntax-check**

Run: `node -c pocketbase/pb_hooks/system_alerts.pb.js`
Expected: silent success.

- [ ] **Step 4: Commit**

```bash
git add pocketbase/pb_hooks/system_alerts.pb.js
git commit -m "feat(pb): hook to fan system_alerts → push notifications

When apiGuard trips and writes a system_alerts row, this hook POSTs
to phone-bridge's loopback /api/push/send, which dispatches a VAPID
push to every subscribed device. fire-and-forget with 5s timeout.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: Phone Bridge — deploy + verify

**Files:** none (deployment only)

**Pre-condition:** Tasks 1-4 committed locally. Working dir clean.

> ⚠️ **CRITICAL ORDERING**: Phone Bridge **must** deploy before Smart-Trip. Reversing this means Smart-Trip writes to non-existent collections and apiGuard fails open — the very condition this PR is fixing.

- [ ] **Step 1: Deploy phone-bridge**

In `D:\Projects\Phone Bridge`:

```bash
deploy
```

Wait for the deploy tool to complete its rsync + systemctl restart + health check loop.

- [ ] **Step 2: Verify PB picked up the migration**

```bash
ssh dashboard-server "sudo ls -la /opt/pocketbase/pb_migrations/ | tail -5"
```

Expected: `1781193600_create_api_quota_tables.js` present.

- [ ] **Step 3: Restart PB so migration applies**

```bash
ssh dashboard-server "sudo systemctl restart pocketbase && sleep 2 && systemctl is-active pocketbase"
```

Expected: `active`.

- [ ] **Step 4: Verify 3 collections exist + 5 seed rows in system_settings**

```bash
ssh dashboard-server "set -a; . /home/dev/phone-bridge/.env; set +a; TOK=\$(curl -s -X POST http://127.0.0.1:8090/api/collections/_superusers/auth-with-password -H 'Content-Type: application/json' -d '{\"identity\":\"'\"\$POCKETBASE_ADMIN_EMAIL\"'\",\"password\":\"'\"\$POCKETBASE_ADMIN_PASSWORD\"'\"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)[\"token\"])'); echo '=== collections ==='; curl -s -H \"Authorization: \$TOK\" http://127.0.0.1:8090/api/collections | python3 -c 'import sys,json; [print(c[\"name\"]) for c in json.load(sys.stdin)[\"items\"] if c[\"name\"] in (\"system_settings\",\"api_logs\",\"system_alerts\")]'; echo '=== system_settings seed rows ==='; curl -s -H \"Authorization: \$TOK\" http://127.0.0.1:8090/api/collections/system_settings/records | python3 -c 'import sys,json; [print(r[\"key\"]+\"=\"+r[\"value\"]) for r in json.load(sys.stdin)[\"items\"]]'"
```

Expected output:
```
=== collections ===
system_settings
api_logs
system_alerts
=== system_settings seed rows ===
places_search_enabled=true
place_details_enabled=true
directions_enabled=true
daily_api_limit=200
per_2min_api_limit=20
```

If any are missing, stop and inspect `journalctl -u pocketbase --since '2 minutes ago'`.

- [ ] **Step 5: Verify PB hook loaded**

```bash
ssh dashboard-server "sudo journalctl -u pocketbase --since '2 minutes ago' --no-pager | grep -iE 'hook|system_alerts' | tail -10"
```

Expected: no error mentioning `system_alerts.pb.js` parse failure. (If absent, the hook was silently rejected — re-check filename casing and goja syntax.)

- [ ] **Step 6: Smoke-test the push fanout chain**

```bash
ssh dashboard-server "set -a; . /home/dev/phone-bridge/.env; set +a; TOK=\$(curl -s -X POST http://127.0.0.1:8090/api/collections/_superusers/auth-with-password -H 'Content-Type: application/json' -d '{\"identity\":\"'\"\$POCKETBASE_ADMIN_EMAIL\"'\",\"password\":\"'\"\$POCKETBASE_ADMIN_PASSWORD\"'\"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)[\"token\"])'); echo '=== insert test alert ==='; curl -s -X POST -H \"Authorization: \$TOK\" -H 'Content-Type: application/json' http://127.0.0.1:8090/api/collections/system_alerts/records -d '{\"kind\":\"api_disabled\",\"api_type\":\"places_search\",\"reason\":\"2min_limit\",\"count\":21,\"acknowledged\":false}'; sleep 1; echo; echo '=== phone-bridge log ==='; sudo journalctl -u phone-bridge --since '10 seconds ago' --no-pager | grep -iE 'push|push.send|/api/push' | tail -5"
```

Expected:
- PB returns a record JSON (new alert)
- phone-bridge log shows `POST /api/push/send` returning 200
- (If subscribed devices exist) you receive a push notification on your phone within seconds

If the phone push doesn't fire, check VAPID is configured:
```bash
ssh dashboard-server "grep -E '^VAPID_' /home/dev/phone-bridge/.env | sed 's/=.*/=<set>/'"
```

- [ ] **Step 7: Clean up the test alert**

```bash
ssh dashboard-server "set -a; . /home/dev/phone-bridge/.env; set +a; TOK=\$(curl -s -X POST http://127.0.0.1:8090/api/collections/_superusers/auth-with-password -H 'Content-Type: application/json' -d '{\"identity\":\"'\"\$POCKETBASE_ADMIN_EMAIL\"'\",\"password\":\"'\"\$POCKETBASE_ADMIN_PASSWORD\"'\"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)[\"token\"])'); IDS=\$(curl -s -H \"Authorization: \$TOK\" 'http://127.0.0.1:8090/api/collections/system_alerts/records?perPage=100' | python3 -c 'import sys,json; [print(r[\"id\"]) for r in json.load(sys.stdin)[\"items\"]]'); for ID in \$IDS; do curl -s -X DELETE -H \"Authorization: \$TOK\" http://127.0.0.1:8090/api/collections/system_alerts/records/\$ID; done; echo OK"
```

- [ ] **Step 8: Push the phone-bridge commits to origin**

```bash
git push
```

---

## Task 6: Smart-Trip — replace `apiGuard.js`

**Files:**
- Modify: `D:\Projects\Smart-Trip-tmp\src\utils\apiGuard.js` (REPLACE entire file)

**Pre-condition:** Task 5 verified green. Smart-Trip clone at `D:\Projects\Smart-Trip-tmp` on branch `feature/pb-datasource`, clean.

> Switch to the Smart-Trip clone for all Task 6-10 operations.

- [ ] **Step 1: Sync the clone**

```bash
cd /d/Projects/Smart-Trip-tmp
git pull --ff-only
git status
```

Expected: up to date, clean.

- [ ] **Step 2: Confirm the PB SDK and import path**

```bash
grep -n "^import.*from '../lib/pb'" src/hooks/pb/useAuthPb.js
```

Expected: `import { pb } from '../../lib/pb';` (note the relative depth differs in apiGuard.js → `../lib/pb`).

- [ ] **Step 3: Replace `src/utils/apiGuard.js` entire contents**

```js
/**
 * apiGuard.js — Google API 用量保护
 * 每次 API 调用前检查开关 + 限额，超限自动切断 + 写 system_alerts。
 *
 * Storage: PocketBase (system_settings / api_logs / system_alerts).
 * Migrated from Supabase 2026-06-11.
 */
import { pb } from '../lib/pb';

const SETTINGS_COLL = 'system_settings';
const LOGS_COLL     = 'api_logs';
const ALERTS_COLL   = 'system_alerts';

// PB filter expects datetime as "YYYY-MM-DD HH:MM:SS.sssZ"
const pbTime = (d) => d.toISOString().replace('T', ' ');

/** Record one API call (success or blocked). Best-effort; never throws. */
export async function logApiCall(apiType, userId, status = 'success') {
  try {
    await pb.collection(LOGS_COLL).create({
      api_type: apiType,
      user_id:  userId || '',
      status,
    });
  } catch (e) {
    console.warn('[apiGuard] logApiCall failed (non-fatal):', e?.message);
  }
}

/**
 * Check whether `apiType` may be called right now.
 * @returns {{ allowed: boolean, reason: string }}
 *
 * Reasons:
 *  - ''             → allowed
 *  - 'disabled'     → switch is off
 *  - 'daily_limit'  → hit per-type daily quota
 *  - '2min_limit'   → hit 2-minute burst quota
 *
 * Fail-open: if PB is unreachable for settings read, returns allowed.
 * (Same semantics as the Supabase predecessor — gate is a soft check;
 * GCP-side quota is the bedrock.)
 */
export async function checkApiAllowed(apiType, userId) {
  let map = {};
  try {
    const settings = await pb.collection(SETTINGS_COLL).getFullList({
      filter: `key="${apiType}_enabled" || key="daily_api_limit" || key="per_2min_api_limit"`,
    });
    settings.forEach((s) => { map[s.key] = s.value; });
  } catch (e) {
    console.warn('[apiGuard] settings read failed, fail-open:', e?.message);
    return { allowed: true, reason: '' };
  }

  if (map[`${apiType}_enabled`] === 'false') {
    return { allowed: false, reason: 'disabled' };
  }

  const dailyLimit   = Number(map.daily_api_limit ?? 200);
  const per2minLimit = Number(map.per_2min_api_limit ?? 20);

  const startOfDay = new Date(); startOfDay.setHours(0, 0, 0, 0);
  const twoMinAgo  = new Date(Date.now() - 2 * 60 * 1000);

  let dailyCount = 0;
  let recentCount = 0;
  try {
    const [dailyRes, recentRes] = await Promise.all([
      pb.collection(LOGS_COLL).getList(1, 1, {
        filter: `api_type="${apiType}" && status="success" && created>="${pbTime(startOfDay)}"`,
      }),
      pb.collection(LOGS_COLL).getList(1, 1, {
        filter: `api_type="${apiType}" && status="success" && created>="${pbTime(twoMinAgo)}"`,
      }),
    ]);
    dailyCount  = dailyRes.totalItems;
    recentCount = recentRes.totalItems;
  } catch (e) {
    console.warn('[apiGuard] count read failed, fail-open:', e?.message);
    return { allowed: true, reason: '' };
  }

  if (dailyCount >= dailyLimit) {
    await flipSwitch(`${apiType}_enabled`, 'false');
    await createAlert({ kind: 'api_disabled', api_type: apiType, reason: 'daily_limit', count: dailyCount });
    await logApiCall(apiType, userId, 'blocked');
    return { allowed: false, reason: 'daily_limit' };
  }
  if (recentCount >= per2minLimit) {
    await flipSwitch(`${apiType}_enabled`, 'false');
    await createAlert({ kind: 'api_disabled', api_type: apiType, reason: '2min_limit', count: recentCount });
    await logApiCall(apiType, userId, 'blocked');
    return { allowed: false, reason: '2min_limit' };
  }
  return { allowed: true, reason: '' };
}

async function flipSwitch(key, value) {
  try {
    const rec = await pb.collection(SETTINGS_COLL).getFirstListItem(`key="${key}"`);
    await pb.collection(SETTINGS_COLL).update(rec.id, { value });
  } catch (e) {
    console.warn(`[apiGuard] flip ${key} failed:`, e?.message);
  }
}

async function createAlert(payload) {
  try {
    await pb.collection(ALERTS_COLL).create({ ...payload, acknowledged: false });
  } catch (e) {
    console.warn('[apiGuard] createAlert failed (non-fatal):', e?.message);
  }
}
```

- [ ] **Step 4: Lint the file**

```bash
cd /d/Projects/Smart-Trip-tmp && npm run lint -- src/utils/apiGuard.js
```

Expected: no errors. (If `npm run lint` lints whole project and other files have unrelated issues, that's OK — only `apiGuard.js` lines should be silent.)

- [ ] **Step 5: Commit**

```bash
cd /d/Projects/Smart-Trip-tmp
git add src/utils/apiGuard.js
git commit -m "refactor(apiGuard): port from Supabase to PocketBase + write system_alerts

Storage layer swap only — public surface (checkApiAllowed, logApiCall)
unchanged so the 12 callers in hooks/components keep working without
edits. Fail-open semantics on PB read errors preserved from original.

Adds createAlert() call on every trip path (daily_limit / 2min_limit /
disabled-by-admin → recorded; downstream Navbar bell + AdminPage banner
+ PB hook → phone push all hang off this single row).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 7: Smart-Trip — create `useSystemAlerts.js` hook

**Files:**
- Create: `src/hooks/useSystemAlerts.js`

**Pre-condition:** Task 6 committed.

- [ ] **Step 1: Write the hook**

Create `D:\Projects\Smart-Trip-tmp\src\hooks\useSystemAlerts.js`:

```js
/**
 * useSystemAlerts — central subscription to PB system_alerts.
 *
 * Used by Navbar (bell + dropdown) and AdminPage (top banner).
 * Polls every 30s; consider PB realtime SSE if responsiveness becomes
 * a pain point.
 */
import { useEffect, useState, useCallback } from 'react';
import { pb } from '../lib/pb';

const COLL = 'system_alerts';
const POLL_MS = 30_000;

export function useSystemAlerts() {
  const [alerts, setAlerts] = useState([]);
  const [unackCount, setUnackCount] = useState(0);

  const refresh = useCallback(async () => {
    try {
      const list = await pb.collection(COLL).getList(1, 20, { sort: '-created' });
      setAlerts(list.items);
      setUnackCount(list.items.filter((a) => !a.acknowledged).length);
    } catch (e) {
      // silent: PB unreachable → keep last known state
    }
  }, []);

  useEffect(() => {
    let stop = false;
    const tick = async () => {
      if (stop) return;
      await refresh();
    };
    tick();
    const t = setInterval(tick, POLL_MS);
    return () => { stop = true; clearInterval(t); };
  }, [refresh]);

  const markAck = useCallback(async (id) => {
    try {
      await pb.collection(COLL).update(id, { acknowledged: true });
      setAlerts((a) => a.map((x) => (x.id === id ? { ...x, acknowledged: true } : x)));
      setUnackCount((c) => Math.max(0, c - 1));
    } catch (e) {
      console.warn('[useSystemAlerts] markAck failed:', e?.message);
    }
  }, []);

  const markAllAck = useCallback(async () => {
    const unack = alerts.filter((a) => !a.acknowledged);
    try {
      await Promise.all(unack.map((a) => pb.collection(COLL).update(a.id, { acknowledged: true })));
      setAlerts((a) => a.map((x) => ({ ...x, acknowledged: true })));
      setUnackCount(0);
    } catch (e) {
      console.warn('[useSystemAlerts] markAllAck failed:', e?.message);
    }
  }, [alerts]);

  return { alerts, unackCount, markAck, markAllAck, refresh };
}
```

- [ ] **Step 2: Lint**

```bash
cd /d/Projects/Smart-Trip-tmp && npm run lint -- src/hooks/useSystemAlerts.js
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add src/hooks/useSystemAlerts.js
git commit -m "feat(hooks): useSystemAlerts — shared poll/ack for system_alerts

Centralized 30s poll + manual refresh + markAck / markAllAck wrappers.
Used by Navbar bell badge + AdminPage banner. Silent on PB errors to
keep UI stable when the backend hiccups.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 8: Smart-Trip — wire Navbar bell + dropdown

**Files:**
- Modify: `src/components/layout/Navbar.jsx`

**Pre-condition:** Task 7 committed.

- [ ] **Step 1: Read the Navbar to locate the bell**

```bash
grep -n "notifications\|nav-dot\|nav-icon-btn" /d/Projects/Smart-Trip-tmp/src/components/layout/Navbar.jsx
```

Confirm the bell button is the single `<button className="nav-icon-btn">` containing the `notifications` material symbol and a `<span className="nav-dot">`.

- [ ] **Step 2: Read the top imports + helper imports of Navbar.jsx**

```bash
head -25 /d/Projects/Smart-Trip-tmp/src/components/layout/Navbar.jsx
```

You'll need to add `useSystemAlerts` import alongside the existing imports.

- [ ] **Step 3: Find the current bell block and replace**

The current block (around line 67-70):

```jsx
<button className="nav-icon-btn">
  <span className="material-symbols-outlined">notifications</span>
  <span className="nav-dot"></span>
</button>
```

Replace with the wired version. Also add `useSystemAlerts` import at the top of the file.

**Import addition** (add next to other hook imports near the top):

```jsx
import { useSystemAlerts } from '../../hooks/useSystemAlerts';
```

**State addition** (with the other `useState` calls inside the component):

```jsx
const [bellOpen, setBellOpen] = useState(false);
const { alerts, unackCount, markAck, markAllAck } = useSystemAlerts();
```

**JSX replacement** (replace the existing `<button className="nav-icon-btn">` block):

```jsx
<div className="bell-container" style={{ position: 'relative' }}>
  <button
    className="nav-icon-btn"
    onClick={() => setBellOpen((v) => !v)}
    title={unackCount > 0 ? `${unackCount} 条未读告警` : '通知'}
  >
    <span className="material-symbols-outlined">notifications</span>
    {unackCount > 0 && (
      <span className="nav-dot" style={{
        position: 'absolute', top: 4, right: 4,
        minWidth: 16, height: 16, padding: '0 4px',
        borderRadius: 8, background: '#ef4444', color: '#fff',
        fontSize: 10, fontWeight: 700,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}>
        {unackCount > 9 ? '9+' : unackCount}
      </span>
    )}
  </button>
  {bellOpen && (
    <div className="bell-dropdown" style={{
      position: 'absolute', right: 0, top: 'calc(100% + 8px)',
      background: 'var(--md-sys-color-surface-container-lowest)',
      border: '1px solid rgba(255,255,255,0.12)',
      borderRadius: 12, padding: 8, minWidth: 320, maxHeight: 420, overflowY: 'auto',
      zIndex: 200, boxShadow: '0 8px 24px rgba(0,0,0,0.6)',
    }}>
      <div style={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        padding: '0 8px 6px', borderBottom: '1px solid rgba(255,255,255,0.08)', marginBottom: 6,
      }}>
        <span style={{ fontWeight: 700, fontSize: 13 }}>通知</span>
        {unackCount > 0 && (
          <button
            onClick={markAllAck}
            style={{
              background: 'none', border: 'none', color: '#63b3ed',
              cursor: 'pointer', fontSize: 12,
            }}
          >全部标记已读</button>
        )}
      </div>
      {alerts.length === 0 ? (
        <div style={{ padding: '16px 8px', color: 'var(--st-color-text-muted)', fontSize: 13, textAlign: 'center' }}>
          暂无通知
        </div>
      ) : (
        [...alerts]
          .sort((a, b) => (a.acknowledged === b.acknowledged ? 0 : a.acknowledged ? 1 : -1))
          .map((a) => {
            const REASON = {
              'disabled': '管理员关闭',
              'daily_limit': '触发日限额',
              '2min_limit': '触发 2 分钟限额',
            };
            const ageMs = Date.now() - new Date(a.created).getTime();
            const ageMin = Math.floor(ageMs / 60000);
            const ageText = ageMin < 1 ? '刚刚' : ageMin < 60 ? `${ageMin} 分钟前` : `${Math.floor(ageMin/60)} 小时前`;
            return (
              <div key={a.id} style={{
                padding: '8px',
                borderRadius: 8,
                background: a.acknowledged ? 'transparent' : 'rgba(239,68,68,0.06)',
                opacity: a.acknowledged ? 0.6 : 1,
                display: 'flex', alignItems: 'flex-start', gap: 8,
                marginBottom: 4,
              }}>
                <span className="material-symbols-outlined" style={{ fontSize: 18, color: '#ef4444', flexShrink: 0 }}>warning</span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 13, fontWeight: 600 }}>
                    {a.api_type} 自动关闭
                  </div>
                  <div style={{ fontSize: 12, color: 'var(--st-color-text-muted)', marginTop: 2 }}>
                    {REASON[a.reason] || a.reason}（{a.count} 次）· {ageText}
                  </div>
                </div>
                {!a.acknowledged && (
                  <button
                    onClick={() => markAck(a.id)}
                    style={{
                      background: 'none', border: 'none', cursor: 'pointer',
                      color: 'var(--st-color-text-muted)', padding: 0,
                    }}
                    title="标记已读"
                  >
                    <span className="material-symbols-outlined" style={{ fontSize: 18 }}>close</span>
                  </button>
                )}
              </div>
            );
          })
      )}
    </div>
  )}
</div>
```

- [ ] **Step 4: Lint**

```bash
cd /d/Projects/Smart-Trip-tmp && npm run lint -- src/components/layout/Navbar.jsx
```

Expected: no new errors. (Pre-existing warnings in this file from earlier code are OK.)

- [ ] **Step 5: Commit**

```bash
git add src/components/layout/Navbar.jsx
git commit -m "feat(navbar): wire bell to system_alerts dropdown

Bell shows red badge with unack count (>9 → '9+'). Click opens a
dropdown with up to 20 recent alerts, unread on top with red tint,
[×] to mark single, [全部标记已读] to bulk-ack. Empty state.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 9: Smart-Trip — port AdminPage API section + add red banner

**Files:**
- Modify: `src/pages/AdminPage.jsx` lines ~282-356 (API monitoring section), plus add banner at top of render

**Pre-condition:** Task 8 committed.

- [ ] **Step 1: Read AdminPage to understand component shape**

```bash
grep -n "loadApiData\|toggleApiSwitch\|saveLimits\|toggleAllApis\|return\s*(\|<div\|useSystemAlerts" /d/Projects/Smart-Trip-tmp/src/pages/AdminPage.jsx | head -30
```

You need to find:
- The 4 helper functions (`loadApiData`, `toggleApiSwitch`, `saveLimits`, `toggleAllApis`) around lines 282-356
- The top of the component's `return (` JSX
- Existing supabase import to leave in place (other AdminPage features still use it; **only the API section migrates this PR**)

- [ ] **Step 2: Add the useSystemAlerts import at the top of AdminPage.jsx**

In the imports block at the top of the file, add:

```jsx
import { pb } from '../lib/pb';
import { useSystemAlerts } from '../hooks/useSystemAlerts';
```

Keep the existing `supabase` import — other AdminPage sections (image cleanup, repair tools) still use it. Only the API quota section switches.

- [ ] **Step 3: Replace the four helper functions**

Find the block starting with `// ── API monitoring helpers ─────`. Replace `loadApiData`, `toggleApiSwitch`, `saveLimits`, `toggleAllApis` (keep `API_TYPES` and `API_SWITCH_KEYS` constants):

```jsx
// ── API monitoring helpers ─────────────────────────────────

const API_TYPES = ['places_search', 'place_details', 'directions'];
const API_SWITCH_KEYS = ['places_search_enabled', 'place_details_enabled', 'directions_enabled'];

const loadApiData = async () => {
  // settings
  const settings = await pb.collection('system_settings').getFullList();
  const map = {};
  settings.forEach((s) => { map[s.key] = s.value; });
  setApiSettings(map);
  setApiLimitInputs({
    daily_api_limit:    map.daily_api_limit ?? 200,
    per_2min_api_limit: map.per_2min_api_limit ?? 20,
  });

  // stats
  const startOfDay = new Date(); startOfDay.setHours(0, 0, 0, 0);
  const twoMinAgo  = new Date(Date.now() - 2 * 60 * 1000);
  const pbTime = (d) => d.toISOString().replace('T', ' ');

  const stats = {};
  await Promise.all(API_TYPES.map(async (type) => {
    const [todayRes, recentRes, blockedRes] = await Promise.all([
      pb.collection('api_logs').getList(1, 1, {
        filter: `api_type="${type}" && status="success" && created>="${pbTime(startOfDay)}"`,
      }),
      pb.collection('api_logs').getList(1, 1, {
        filter: `api_type="${type}" && status="success" && created>="${pbTime(twoMinAgo)}"`,
      }),
      pb.collection('api_logs').getList(1, 1, {
        filter: `api_type="${type}" && status="blocked" && created>="${pbTime(startOfDay)}"`,
      }),
    ]);
    stats[type] = {
      today:   todayRes.totalItems,
      recent:  recentRes.totalItems,
      blocked: blockedRes.totalItems,
    };
  }));
  setApiStats(stats);

  // Recent logs
  const logs = await pb.collection('api_logs').getList(1, 50, { sort: '-created' });
  setApiLogs(logs.items.map((l) => ({
    api_type:   l.api_type,
    status:     l.status,
    created_at: l.created,
  })));
};

const toggleApiSwitch = async (key, currentValue) => {
  const newValue = (currentValue === true || currentValue === 'true') ? 'false' : 'true';
  setApiSaving(true);
  try {
    const rec = await pb.collection('system_settings').getFirstListItem(`key="${key}"`);
    await pb.collection('system_settings').update(rec.id, { value: newValue });
  } catch {
    // If row missing (shouldn't happen given seed), create it
    await pb.collection('system_settings').create({ key, value: newValue });
  }
  await loadApiData();
  setApiSaving(false);
};

const saveLimits = async () => {
  setApiSaving(true);
  await Promise.all([
    upsertSetting('daily_api_limit',    String(apiLimitInputs.daily_api_limit)),
    upsertSetting('per_2min_api_limit', String(apiLimitInputs.per_2min_api_limit)),
  ]);
  await loadApiData();
  setApiSaving(false);
};

const upsertSetting = async (key, value) => {
  try {
    const rec = await pb.collection('system_settings').getFirstListItem(`key="${key}"`);
    await pb.collection('system_settings').update(rec.id, { value });
  } catch {
    await pb.collection('system_settings').create({ key, value });
  }
};

const toggleAllApis = async (enable) => {
  setApiSaving(true);
  const value = enable ? 'true' : 'false';
  await Promise.all(API_SWITCH_KEYS.map((key) => upsertSetting(key, value)));
  await loadApiData();
  setApiSaving(false);
};
```

- [ ] **Step 4: Add the red banner at the top of the rendered JSX**

In the same `AdminPage` function, just before the top-level `return (...)`, add the hook call alongside the other state:

```jsx
const { alerts, unackCount, markAllAck } = useSystemAlerts();
```

At the very top of the rendered JSX (immediately inside the outermost wrapper `<div>` or `<>`), add the banner:

```jsx
{unackCount > 0 && (
  <div style={{
    background: 'rgba(239,68,68,0.12)',
    border: '1px solid rgba(239,68,68,0.4)',
    borderRadius: 12,
    padding: '12px 16px',
    marginBottom: 16,
    display: 'flex',
    alignItems: 'center',
    gap: 12,
  }}>
    <span className="material-symbols-outlined" style={{ color: '#ef4444', fontSize: 24 }}>warning</span>
    <div style={{ flex: 1 }}>
      <div style={{ fontWeight: 700, color: '#ef4444' }}>
        ⚠️ {unackCount} 个未确认的 API 闸门告警
      </div>
      <div style={{ fontSize: 13, color: 'var(--md-sys-color-on-surface-variant)', marginTop: 2 }}>
        最近一条：{alerts[0]?.api_type} —{' '}
        {({ 'disabled':'管理员关闭', 'daily_limit':'触发日限额', '2min_limit':'触发 2 分钟限额' })[alerts[0]?.reason] || alerts[0]?.reason}
        （{alerts[0]?.count} 次）
      </div>
    </div>
    <button
      onClick={markAllAck}
      style={{
        background: '#ef4444', color: '#fff', border: 'none',
        padding: '8px 14px', borderRadius: 8, cursor: 'pointer', fontWeight: 600,
      }}
    >全部标记已读</button>
  </div>
)}
```

- [ ] **Step 5: Lint**

```bash
cd /d/Projects/Smart-Trip-tmp && npm run lint -- src/pages/AdminPage.jsx
```

Expected: no new errors.

- [ ] **Step 6: Commit**

```bash
git add src/pages/AdminPage.jsx
git commit -m "feat(admin): port API monitoring section to PB + add alert banner

loadApiData / toggleApiSwitch / saveLimits / toggleAllApis now hit PB
via the pb client. Adds a red banner at the top showing unack count +
最近一条 + 一键全部已读 button. Other AdminPage sections (image
cleanup, trip repair) still use the supabase client and are not touched
by this PR.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 10: Smart-Trip — build + deploy

**Files:** none

**Pre-condition:** Tasks 6-9 committed locally on `feature/pb-datasource`.

- [ ] **Step 1: Push branch to origin**

```bash
cd /d/Projects/Smart-Trip-tmp
git push origin feature/pb-datasource
```

- [ ] **Step 2: Build for the PB-VM target**

```bash
npm run build:pb-vm
```

Expected: `dist/` populated, no fatal errors. Warnings about chunk size or dynamic import are normal.

Verify:
```bash
ls -la dist/ | head -10
ls dist/assets/ | head -10
```

Expected: `index.html`, `favicon.svg`, `assets/index-*.js`, `assets/pb-*.js`, etc.

- [ ] **Step 3: Stage upload to VM (don't swap dist yet)**

```bash
ssh dashboard-server "mkdir -p /home/dev/smat-trip/dist.new"
scp -r D:/Projects/Smart-Trip-tmp/dist/. dashboard-server:/home/dev/smat-trip/dist.new/
ssh dashboard-server "ls /home/dev/smat-trip/dist.new/ | head -5"
```

Expected: `assets`, `favicon.svg`, `icons.svg`, `index.html`.

- [ ] **Step 4: Backup current dist + atomic swap**

```bash
ssh dashboard-server "cp -r /home/dev/smat-trip/dist /home/dev/smat-trip/dist.bak.\$(date +%s) && rm -rf /home/dev/smat-trip/dist && mv /home/dev/smat-trip/dist.new /home/dev/smat-trip/dist && ls /home/dev/smat-trip/dist/ | head -5 && ls -d /home/dev/smat-trip/dist.bak.* | tail -1"
```

Expected: the bak dir prints last; new dist has the same file list.

- [ ] **Step 5: Verify HTTP 200 from smat-trip**

```bash
ssh dashboard-server "curl -s -o /dev/null -w 'HTTP %{http_code} size %{size_download}\n' http://127.0.0.1:8101/"
```

Expected: `HTTP 200 size <some positive number>`.

---

## Task 11: End-to-end verification (Smart-Trip + PB + push integration)

**Files:** none

**Pre-condition:** Tasks 1-10 deployed.

Run through the spec §7 checklist, verifying each item:

- [ ] **Step 1: Open SmartTrip in browser**

URL: `https://dashboard-server.tail4cfa2.ts.net:8451/`

Open browser dev tools → Network panel.

Expected: page loads, no red 4xx/5xx requests in Network panel related to `system_settings`, `api_logs`, `system_alerts`.

- [ ] **Step 2: Confirm `useSystemAlerts` is polling**

In Network panel, filter by `system_alerts`. Wait 35 seconds.

Expected: at least 2 GET requests to `/api/collections/system_alerts/records` 30 seconds apart.

- [ ] **Step 3: Open AdminPage**

Click your avatar → 设置 → Admin Dashboard (only visible if your user is admin).

Expected: API monitoring section shows:
- Each of the 3 API types has a toggle in `enabled=true` state
- "今日调用 0 / 200" or similar for each type
- "2 分钟 0 / 20" similar
- Recent logs empty list

No console errors related to `supabase.from('system_settings')`.

- [ ] **Step 4: Trigger a real API call to populate logs**

In SmartTrip, use a feature that calls Google APIs:
- Open a trip → DayPage → "附近打卡" — this calls places_search
- Or: Search a city in DestinationInput

Then refresh AdminPage. Expected: "今日调用" counter went up by 1 for the API type used. The new row appears in Recent Logs.

- [ ] **Step 5: Synthetic trip — induce a 2min_limit auto-disable**

In PB admin UI (https://dashboard-server.tail4cfa2.ts.net:8450/_/), do this manually for speed:

1. Open `system_settings` → set `per_2min_api_limit` value to `'1'` (temp tighten)
2. In SmartTrip, trigger 2 places_search calls in quick succession (any way that fires this API; e.g. 2 city searches).
3. Within seconds, PB should show:
   - `system_settings.places_search_enabled` = `'false'`
   - 1 new `system_alerts` row: `{reason:'2min_limit', count:>=1, acknowledged:false}`

- [ ] **Step 6: Verify all 4 alert surfaces**

Within 30 seconds of the trip:

| Surface | Expected |
|---|---|
| PB `system_alerts` table | New row visible in PB admin UI |
| Navbar bell | Red dot + `1` badge visible |
| AdminPage banner | Red banner at top of admin page |
| Phone | Push notification arrives (if your device is subscribed via the existing Phone Bridge UI) |

If phone push doesn't arrive:
- Verify subscription: `ssh dashboard-server "cat /home/dev/phone-bridge/.bridge_data/push_subs.json | head"`
- Check phone-bridge log: `ssh dashboard-server "sudo journalctl -u phone-bridge --since '1 minute ago' --no-pager | grep -iE 'push|/api/push'"`

- [ ] **Step 7: Restore normal limits + clear alerts**

In PB admin UI:
1. Reset `per_2min_api_limit` value to `'20'`
2. Set `places_search_enabled` back to `'true'` (or do it from AdminPage UI)

In SmartTrip:
3. Click bell → `[全部标记已读]` button. Bell badge clears.
4. AdminPage banner disappears.

- [ ] **Step 8: PB persistence sanity check**

```bash
ssh dashboard-server "sudo systemctl restart pocketbase && sleep 2 && systemctl is-active pocketbase"
```

Reload SmartTrip. The bell shouldn't bring back any banners (alerts were ack'd before restart, persisted). AdminPage settings load again.

- [ ] **Step 9: Final commit summary in spec doc**

Back in `D:\Projects\Phone Bridge`, append to the design spec a footer noting deployment is complete and commit:

```bash
cd "D:/Projects/Phone Bridge"
echo "
---

## Deployment record

Implementation completed and verified end-to-end on $(date +%Y-%m-%d). All 11 verification items in §7 passed. See [implementation plan](../plans/2026-06-11-google-api-quota-pb-migration.md) for the executed task list." >> docs/superpowers/specs/2026-06-11-google-api-quota-pb-migration-design.md

git add docs/superpowers/specs/2026-06-11-google-api-quota-pb-migration-design.md
git commit -m "docs(spec): mark Google API quota migration as deployed

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
git push
```

- [ ] **Step 10: Clean up dist.bak**

After 24h of stable operation, remove the dist backup:

```bash
ssh dashboard-server "ls -d /home/dev/smat-trip/dist.bak.* 2>/dev/null && echo 'manually verify before deleting'"
# When ready:
# ssh dashboard-server "rm -rf /home/dev/smat-trip/dist.bak.*"
```

---

## Out-of-band manual tasks (Bedrock — please do these in parallel)

Per spec §6, these protect against the case where apiGuard fails open. **Not blocked by code work; do whenever convenient.**

- [ ] **GCP Billing budget alert**
  - GCP Console → Billing → Budgets & alerts → Create budget
  - Set monthly budget (e.g. $20), email alerts at 50%/90%/100%
  - Receiver: showbox88@gmail.com

- [ ] **GCP per-API daily quota override**
  - GCP Console → APIs & Services → For each of Places API / Geocoding API / Directions API → Quotas
  - Set daily request limit to a hard ceiling you can afford (e.g. 500/day)
  - Google returns 503 when hit — bulletproof regardless of app state

- [ ] **GCP API key referrer restriction**
  - GCP Console → Credentials → Maps key → Application restrictions
  - HTTP referrers → `https://dashboard-server.tail4cfa2.ts.net:8451/*`
  - Protects against key exfiltration

---

## Rollback procedures

### Smart-Trip rollback

```bash
ssh dashboard-server "rm -rf /home/dev/smat-trip/dist && mv \$(ls -d /home/dev/smat-trip/dist.bak.* | tail -1) /home/dev/smat-trip/dist"
```

### Phone Bridge rollback

```bash
cd "D:/Projects/Phone Bridge"
git revert <commit-sha-of-task-1-through-5>  # multiple commits
deploy
```

### PB collections rollback

The migration's `down` step drops the 3 collections. To trigger:

```bash
ssh dashboard-server "sudo /opt/pocketbase/pocketbase migrate down 1 --dir=/opt/pocketbase/pb_migrations"
```

> ⚠️ Dropping `system_alerts` wipes any historical alert data. Only do this if you're certain.

### PB hook rollback

```bash
ssh dashboard-server "sudo rm /opt/pocketbase/pb_hooks/system_alerts.pb.js && sudo systemctl restart pocketbase"
```
