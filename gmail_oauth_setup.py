"""One-shot Gmail OAuth helper. Run on a machine with a desktop browser.

Usage (from this repo root, on Windows):
    pip install google-auth-oauthlib
    python gmail_oauth_setup.py "C:\\path\\to\\client_secret_xxx.json"

What it does:
    1. Spins up a local HTTP server on a random port.
    2. Pops your default browser to Google's OAuth consent screen.
    3. After you click Allow, captures the auth code, exchanges it for a
       refresh token, writes gmail_token.json next to this script.

Next step (printed at the end too):
    scp gmail_token.json dashboard-server:/tmp/
    ssh dashboard-server "sudo mkdir -p /home/dev/phone-bridge/.gmail \
        && sudo mv /tmp/gmail_token.json /home/dev/phone-bridge/.gmail/token.json \
        && sudo chown dev:dev /home/dev/phone-bridge/.gmail/token.json \
        && sudo chmod 600 /home/dev/phone-bridge/.gmail/token.json"

The server uses the refresh token to mint fresh access tokens forever — you
should not need to re-run this script unless you revoke access or rotate
the OAuth client secret.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
OUT = Path(__file__).parent / "gmail_token.json"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python gmail_oauth_setup.py <client_secret.json>",
              file=sys.stderr)
        return 2
    creds_path = Path(sys.argv[1])
    if not creds_path.exists():
        print(f"file not found: {creds_path}", file=sys.stderr)
        return 2

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Missing dependency. Install with:\n"
              "    pip install google-auth-oauthlib", file=sys.stderr)
        return 3

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    # port=0 → OS picks a free localhost port and registers it as redirect_uri
    creds = flow.run_local_server(port=0, open_browser=True,
                                  authorization_prompt_message="")
    OUT.write_text(creds.to_json(), encoding="utf-8")
    print()
    print(f"OK wrote {OUT.resolve()}")
    print()
    print("Next: copy it to the server:")
    print(f'    scp "{OUT}" dashboard-server:/tmp/gmail_token.json')
    print('    ssh dashboard-server "sudo mkdir -p /home/dev/phone-bridge/.gmail '
          '&& sudo mv /tmp/gmail_token.json /home/dev/phone-bridge/.gmail/token.json '
          '&& sudo chown dev:dev /home/dev/phone-bridge/.gmail/token.json '
          '&& sudo chmod 600 /home/dev/phone-bridge/.gmail/token.json"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
