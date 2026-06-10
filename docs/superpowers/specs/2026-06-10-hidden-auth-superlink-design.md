# Hidden auth via a secret "super link" + decoy error page

**Date:** 2026-06-10
**Status:** Design — pending implementation
**Branch:** `feature/hidden-auth-superlink` (new feature → NOT `main` during the refactor period)

## 1. Problem

Phone Bridge is reachable on the public internet via Tailscale Funnel
(`https://dashboard-server.tail4cfa2.ts.net/`). Funnel must stay on: when the
user travels to China, the Tailscale VPN (WireGuard) is often blocked, but the
Funnel HTTPS endpoint (a plain public CDN edge) has a much better chance of
working — so it is the primary remote-access path, not a convenience.

Today the public root serves a password + TOTP **login page** to anyone. That
means the entire auth surface — a login form, a TOTP oracle, the fact that
"this is a Claude bridge" — is visible to the whole internet. The only thing
standing between an attacker and the system is the password + TOTP pair.

The user typically uses Phone Bridge from **fewer than 5 devices**. We want to
exploit that: lock the system to a small set of trusted devices and make the
public surface reveal *nothing*.

## 2. Goal

- The public surface exposes **no interactive auth** and **no hint** that an
  auth system (or Phone Bridge at all) exists. Strangers see a generic,
  misleading server error.
- The only door to log in / manage devices is a **secret "super link"** — a
  high-entropy URL the user saves once (e.g. in a password manager).
- Opening the super link still requires **password + TOTP** (defense in depth:
  link + password + TOTP = three independent factors).
- The user's existing trusted devices keep working uninterrupted (sliding
  session cookie, unchanged).
- A self-controlled backstop exists for losing the link or all devices: **SSH
  to the host** (reachable in China via the user's own VPN).

### Non-goals (explicitly dropped)

- **Recovery codes.** Considered, then dropped (YAGNI): the SSH backstop is
  reachable from China via the user's VPN, so the "all devices dead + no SSH"
  edge case that recovery codes guarded against does not apply.
- **mTLS / client certificates.** Infeasible over Funnel — TLS terminates at
  the Tailscale ingress edge and the origin only sees plain HTTP, so client
  certs never reach the origin. The "secure channel" is implemented at the
  application layer instead, with equivalent effect (unknown devices cannot
  get in).

## 3. Threat model

| Actor | Before | After |
|---|---|---|
| Internet scanner hitting `/` | Sees login page → knows a system is here, can probe TOTP/password | Sees a generic `503`/`502` error → concludes the backend is down, moves on |
| Attacker who guesses/knows the URL exists | Has a login form to brute-force (rate-limited) | Has nothing — the login form lives only behind the secret link |
| Attacker who leaks the super link | n/a | Reaches the login gate but still needs password **and** TOTP |
| Attacker who leaks the auth file | Sees password hash + TOTP secret + device token hashes | Same, plus the super-link is stored **hashed** — the live link is not recoverable from the file |

The super link is treated as a secret credential: stored hashed, compared in
constant time, never written to logs, rotatable.

## 4. Architecture

### 4.1 Three request outcomes

Every inbound HTTP/WS request resolves to exactly one of:

1. **Valid device cookie** → the real Phone Bridge app / API / WS, as today.
2. **Path matches the super-link secret** (and no valid cookie) → the auth gate
   (password + TOTP form). On success the *current device* is enrolled (issued
   a session cookie) and redirected into the app; the device-management page
   (view / revoke) is reachable from here.
3. **Everything else** → the **decoy error response** (generic `503 Service
   Unavailable`, see §4.4). No redirect, no login form, no hint.

This replaces the current middleware behavior where unauthed HTML requests get
`303 → /login` and unauthed JSON requests get `401`.

### 4.2 The super link

- Format: a single high-entropy path segment, no telltale prefix —
  `https://<host>/<48-char url-safe random>`. (Not `/login`, not `/manage`,
  nothing that signals "auth here".)
- Stored server-side as `sha256(secret)` in the auth state file, alongside the
  existing password hash / TOTP secret / device map.
- Matched by comparing `sha256(first_path_segment)` to the stored hash in
  **constant time** (`hmac.compare_digest`).
- **Rotatable:** regenerating it invalidates the old link immediately. Rotation
  happens via the SSH CLI (§4.5) or a button on the management page that
  displays the new full link exactly once.
- **Never logged:** access-log middleware (if any) must redact the path for
  super-link hits; the value is never emitted to journald or `sync.log`.

### 4.3 Enrollment model ("locked")

There is **no separate `enrollment_locked` boolean** — the lock is structural:

- The public `/login` and `/setup` routes are **removed** from the public
  surface (they now resolve to the decoy unless reached via the super link).
- The **only** ways to mint a new device token are:
  1. Open the super link, pass password + TOTP → the opening device is enrolled.
  2. SSH to the host and run the management CLI (§4.5).

So "lock these 5 devices now" = deploy this change. Existing devices keep their
cookies and the app keeps working; the public door simply closes.

### 4.4 Decoy error response

- Returned for outcome (3) above: any path that is neither a valid-cookie
  request nor the super link.
- **Decided:** HTTP `503 Service Unavailable` with a minimal, generic
  reverse-proxy-style body (plausible because the service genuinely runs behind
  the Tailscale reverse proxy) and a `Retry-After` header to look like a real
  transient outage. Body is configurable; status code is `503`.
- Applies uniformly to HTML, JSON, and unknown paths so there is no
  content-type-based tell.

### 4.5 Backstop: SSH management CLI

A small CLI (e.g. `python -m app.auth.cli`) runnable on the host:

- `rotate-link` → generate a new super link, print the full URL once, store its
  hash. (Also the initial-setup command that prints the first link.)
- `list-devices` / `revoke <hash>` → manage the allowlist from the server.

Reachable in China via the user's own VPN → SSH. This is the ultimate recovery
path if the super link is lost or every trusted device dies.

## 5. Components changed

| File | Change |
|---|---|
| `auth.py` | Add `super_link_hash` to the state schema; helpers to set/verify/rotate it (constant-time compare); store `sha256`. |
| `app/auth/state.py` | Surface the super-link accessors on the shared `auth_state`. |
| `app/auth/middleware.py` | Rewrite outcome logic: cookie → app; super-link match → gate; else → decoy. Drop the `/login` redirect and `401` JSON behaviors. Keep `/api/health` public + 200 (deploy depends on it — see §7). |
| `app/auth/pages.py` | Move the login form + device management behind the super-link route. Remove public `/login` and `/setup`. First-time setup happens via CLI-printed link. |
| `app/ws/handler.py` | WS already rejects on bad cookie (`4401`); confirm it does not leak via a distinguishable close vs the decoy. |
| `app/auth/cli.py` (new) | The SSH management CLI (§4.5). |
| Docs | Update `CLAUDE.md` auth section + `docs/operations/` with the new model and the "save your super link" + SSH-backstop runbook. |

## 6. Flows

**First lockdown (one-time):**
1. Deploy the feature branch to staging, soak, then to the VM.
2. On the host: `python -m app.auth.cli rotate-link` → prints the super link.
3. Save the link (password manager). Existing devices keep working.
4. Verify `/` returns the decoy from a non-trusted browser; verify the super
   link reaches the gate and password + TOTP enrolls a new device.

**Add a device (normal):** open the saved super link on the new device → enter
password + TOTP → device enrolled, redirected into the app.

**Revoke a device:** from any trusted device, open device management → revoke;
or `revoke` via the SSH CLI.

**Lost the super link:** SSH → `rotate-link` → save the new one. Old link dead.

## 7. Security considerations

- Super link stored hashed; compared constant-time; never logged; rotatable.
- Keep the existing per-IP rate limiting on the password+TOTP gate (brute force
  is now also gated by needing the secret URL first).
- Cookie flags unchanged (`HttpOnly`, `Secure`, `SameSite=Lax`, sliding
  expiry), but **`bridge_cookie_days` default is raised 30 → 90** so trusted
  devices don't silently expire while abroad. Sliding expiry means active
  devices effectively never need re-enrollment.
- Decoy must be indistinguishable across content types and must not vary in a
  way that signals "auth lives elsewhere."
- `/api/health` **stays public + 200**: the `deploy` tool health-checks it over
  the public Funnel URL (`CLAUDE.md` deploy step 5), so it cannot be hidden or
  localhost-only. Its body is a generic `{"ok":true}` — it reveals that *a*
  service is alive but nothing about Phone Bridge or the auth model, which is an
  accepted minor tell. (Keep its response generic; never add identifying info.)

## 8. Testing

- `tests/smoke_backend.py` must still pass (`BRIDGE_COOKIE` is a valid device
  cookie → real app). Add cases: no cookie → decoy `503`; super link → gate.
- Manual: from a clean browser (no cookie) confirm `/`, `/login`, `/api/*` all
  return the decoy; confirm the super link gate enrolls a device; confirm an
  already-trusted device is unaffected.

## 9. Rollout

- New feature → `feature/hidden-auth-superlink` branch (refactor-period rule:
  `main` takes only `refactor:`/`docs:`).
- Deploy to staging, run smoke tests, soak per roadmap, then merge + deploy.
- Do NOT deploy while in an active Phone Bridge chat (restart drops the WS).
- Have `docs/operations/rollback.md` handy; the change is auth-critical.
