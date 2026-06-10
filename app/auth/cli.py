"""Host-side auth management CLI (run over SSH).

    python -m app.auth.cli rotate-link    # mint a new super link (prints it ONCE)
    python -m app.auth.cli init           # first-time password+TOTP setup
    python -m app.auth.cli list-devices
    python -m app.auth.cli revoke <hash>

The super link is the only public door to log in; `rotate-link` is also the
recovery path if the link is lost or every trusted device dies (reachable in
China via the user's own VPN -> SSH).
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from app.auth.state import auth_state

_BASE = os.environ.get("BRIDGE_PUBLIC_URL", "https://dashboard-server.tail4cfa2.ts.net").rstrip("/")


def _rotate_link() -> int:
    secret = auth_state.set_super_link()
    print("New super link (save it now — it will NOT be shown again):")
    print(f"  {_BASE}/{secret}")
    print("The previous link (if any) is now invalid.")
    return 0


def _init() -> int:
    if auth_state.is_initialized():
        print("Already initialized. Use rotate-link to mint a super link.", file=sys.stderr)
        return 1
    pw = getpass.getpass("Master password (min 12 chars): ")
    secret = auth_state.initialize(pw)
    print(f"TOTP secret (add to your authenticator): {secret}")
    print("Now run: python -m app.auth.cli rotate-link")
    return 0


def _list_devices() -> int:
    for d in auth_state.list_devices():
        print(f"  {d['hash'][:12]}  {d.get('name','?'):20}  last_ip={d.get('last_ip','')}")
    return 0


def _revoke(token_hash: str) -> int:
    ok = auth_state.revoke(token_hash)
    print("revoked" if ok else "no such device")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="app.auth.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("rotate-link")
    sub.add_parser("init")
    sub.add_parser("list-devices")
    rv = sub.add_parser("revoke")
    rv.add_argument("hash")
    args = p.parse_args(argv)
    if args.cmd == "rotate-link":
        return _rotate_link()
    if args.cmd == "init":
        return _init()
    if args.cmd == "list-devices":
        return _list_devices()
    if args.cmd == "revoke":
        return _revoke(args.hash)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
