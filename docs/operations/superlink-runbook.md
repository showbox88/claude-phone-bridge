# Super-Link Auth — Operator Runbook

## What the super link is

Phone Bridge does not expose a login page. Every unauthenticated request —
regardless of path — receives a plain `503 Service Temporarily Unavailable`
with a generic nginx-style body. From the outside the service looks like a
misconfigured or dead backend. This is intentional misdirection.

The **super link** is the single secret URL that opens the real login door.
It points to a password + TOTP form. Once a device passes that form it
receives a 90-day sliding session cookie; subsequent requests are served
normally until the cookie expires or the device is revoked.

Treat the super link exactly like a password:

- Save it in your password manager immediately after minting it.
- Never share it over plaintext channels (email, SMS, unencrypted notes).
- Anyone who obtains the link can attempt the password + TOTP challenge, so
  rotate it the moment you suspect exposure.

---

## Mint / rotate the super link

SSH to the host and run:

```bash
ssh dashboard-server
cd /home/dev/phone-bridge
set -a; . ./.env; set +a
.venv/bin/python -m app.auth.cli rotate-link
```

The command prints the full link **once**. Copy it immediately — it cannot be
recovered from the server later (only a sha256 hash is stored). The old link
dies the moment you rotate.

The running service does **not** need a restart: `AuthState` watches the auth
file's `(mtime, size)` and reloads when the CLI (a separate process) writes it,
so the new link is live within one request and the server's own writes never
clobber it. (Historically this required stopping the service first; that
workaround is no longer needed.)

If the printed base host is wrong (e.g. you are testing behind a different
proxy), set `BRIDGE_PUBLIC_URL` in `.env` before running:

```bash
BRIDGE_PUBLIC_URL=https://dashboard-server.tail4cfa2.ts.net \
  .venv/bin/python -m app.auth.cli rotate-link
```

---

## Add a new device

1. Open the saved super link on the new device in a browser.
2. Enter the master password and the current TOTP code from your authenticator
   app.
3. On success the browser is enrolled: a 90-day sliding cookie is set.

There is no authenticator-free shortcut by design. The authenticator (TOTP)
works fully offline, so it functions normally in China or anywhere without
internet — only the authenticator app itself needs to have been seeded once
while online.

---

## List enrolled devices

On a trusted (already-enrolled) device:

1. Open Phone Bridge in the browser and navigate to **Settings → Devices**
   (`/devices`), or
2. From the server:

```bash
ssh dashboard-server
cd /home/dev/phone-bridge
set -a; . ./.env; set +a
.venv/bin/python -m app.auth.cli list-devices
```

The output lists each enrolled device with a truncated hash prefix, user
agent, last-seen timestamp, and expiry.

---

## Revoke a device

### Via the in-app devices page

On any trusted device, open `/devices`, find the entry, click **Revoke**.

### Via the CLI

```bash
ssh dashboard-server
cd /home/dev/phone-bridge
set -a; . ./.env; set +a
.venv/bin/python -m app.auth.cli list-devices          # note the hash prefix
.venv/bin/python -m app.auth.cli revoke <hash-prefix>  # e.g. revoke a3f9
```

Revocation takes effect immediately — the device's next request will be
treated as unauthenticated and receive the 503 decoy.

---

## First-time setup (fresh install only)

The current production install on `dashboard-server` is **already initialized**.
Skip this section unless you are standing up a brand-new instance.

```bash
ssh dashboard-server
cd /home/dev/phone-bridge
set -a; . ./.env; set +a
.venv/bin/python -m app.auth.cli init        # creates .bridge_auth.json
.venv/bin/python -m app.auth.cli rotate-link # mint the first super link
```

Save the printed link in your password manager before closing the terminal.

---

## Decoy behavior — what unauthenticated callers see

Any request that does not carry a valid session cookie receives:

```
HTTP/1.1 503 Service Temporarily Unavailable
Retry-After: 120
Content-Type: text/html

<html>
<head><title>503 Service Temporarily Unavailable</title></head>
<body>
<center><h1>503 Service Temporarily Unavailable</h1></center>
<hr><center>nginx</center>
</body>
</html>
```

The following paths are **exempted** from the decoy and always respond
normally (they are needed for the health-check and MCP discovery):

- `GET /api/health`
- `GET /.well-known/oauth-protected-resource/mcp`
- `GET /.well-known/oauth-authorization-server/mcp`

Everything else — including `GET /`, `GET /login`, and all API paths — returns
503 to unauthenticated callers.

---

## Recovery if the super link is lost or all devices are locked out

Because the super link cannot be retrieved from the server (only its sha256
hash is stored in `.bridge_auth.json`), the recovery path is:

1. SSH to `dashboard-server`. The host is reachable from China via your own
   VPN / Tailscale mesh even without a browser.
2. Run `rotate-link` (see above) to mint a fresh super link.
3. Open the new link on one device and re-enrol.

This SSH backstop is the ultimate recovery mechanism — protect your SSH key
with the same care as the super link itself.

---

## Security notes

- The super link token is stored only as `sha256(token)` in
  `.bridge_auth.json`. If the file is compromised an attacker still needs the
  master password and TOTP to gain access.
- Rotate the super link whenever: a device is lost or stolen, you suspect the
  link leaked, or as a routine periodic hygiene measure.
- The 503 decoy is not a substitute for network-level isolation — keep
  Tailscale as the outer perimeter.
