"""Thin shim — the FastAPI app lives in app.main now.

Kept so:
- systemd's ExecStart=uvicorn server:app keeps working without unit edits
- `python server.py` still launches the dev server
- existing call sites (`from server import _pb_client`, the WS handler's
  `getattr(server, "_recorder", None)`) keep resolving correctly.

Phase 6 cleanup: switch the systemd unit to `app.main:app` and delete
this file.
"""
from app.main import app, _pb_client, _recorder  # noqa: F401

if __name__ == "__main__":
    import uvicorn
    from app.settings import settings
    uvicorn.run("server:app", host=settings.host, port=settings.port,
                log_level="info")
