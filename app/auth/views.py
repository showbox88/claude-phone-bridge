"""Shared HTML rendering helpers for the auth surface (login gate + device
management). Extracted from pages.py so both pages.py and gate.py can use them
without a circular import via middleware."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse

_AUTH_PAGE_CSS = """
:root{--bg:#0e1116;--card:#161b22;--line:#2a313a;--text:#e6edf3;--muted:#8b949e;
      --accent:#58a6ff;--red:#f85149;--green:#3fb950}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--text);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
.wrap{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:1rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:1.6rem 1.4rem;width:100%;max-width:420px}
h1{margin:0 0 0.25rem;font-size:1.2rem}
.sub{color:var(--muted);font-size:0.85rem;margin-bottom:1.2rem}
label{display:block;color:var(--muted);font-size:0.78rem;text-transform:uppercase;
  letter-spacing:.05em;margin:0.85rem 0 0.3rem}
input[type=text],input[type=password]{width:100%;padding:0.65rem 0.75rem;
  background:#0b0f14;border:1px solid var(--line);border-radius:8px;color:var(--text);
  font:inherit;font-size:1rem}
input:focus{outline:none;border-color:var(--accent)}
button{width:100%;padding:0.7rem;margin-top:1.1rem;background:var(--accent);
  color:#0b0f14;border:0;border-radius:8px;font:inherit;font-weight:600;cursor:pointer;
  font-size:0.95rem}
button:hover{filter:brightness(1.07)}
.error{color:var(--red);font-size:0.85rem;margin-top:0.6rem;min-height:1.2em}
.muted{color:var(--muted);font-size:0.82rem}
.qr{display:flex;justify-content:center;margin:1rem 0;background:#ffffff;border-radius:8px;padding:0.75rem}
.qr svg{max-width:260px;height:auto;display:block}
.code{background:#0b0f14;border:1px solid var(--line);border-radius:6px;
  padding:0.6rem;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:0.85rem;
  word-break:break-all;color:#d2d2d2}
.devices li{list-style:none;padding:0.6rem 0;border-bottom:1px solid var(--line)}
.devices li:last-child{border:none}
.devices .row{display:flex;justify-content:space-between;align-items:center;gap:0.5rem}
.devices small{color:var(--muted);display:block;margin-top:0.15rem;font-size:0.75rem}
.devices form{margin:0}
.devices button.danger{padding:0.3rem 0.7rem;font-size:0.78rem;width:auto;
  background:transparent;border:1px solid var(--red);color:var(--red)}
.devices button.danger:hover{background:rgba(248,81,73,0.1)}
.this-device{color:var(--green);font-size:0.7rem;margin-left:0.4rem}
"""


def _page(title: str, body: str, *, status: int = 200) -> HTMLResponse:
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — Phone Bridge</title>
<style>{_AUTH_PAGE_CSS}</style></head>
<body><div class="wrap"><div class="card">{body}</div></div></body></html>"""
    return HTMLResponse(html, status_code=status)


def _ua_short(request: Request) -> str:
    ua = request.headers.get("user-agent", "")
    if "iPhone" in ua: return "iPhone"
    if "iPad" in ua: return "iPad"
    if "Android" in ua: return "Android"
    if "Macintosh" in ua: return "Mac"
    if "Windows" in ua: return "Windows"
    if "Linux" in ua: return "Linux"
    return "device"


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
              .replace('"', "&quot;").replace("'", "&#39;"))
