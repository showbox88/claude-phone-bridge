"""Atomic JSON write + safe JSON read.

Replaces the 3 places that did `path.write_text(json.dumps(d))` which
left half-written files on crash. Uses tempfile + os.replace for an
atomic swap on the same filesystem (atomic on both POSIX and Windows).

`read_json_safe` returns `default` on missing/corrupt/unreadable file.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_json_atomic(path: Path | str, data: Any, *, indent: int | None = 2) -> None:
    """Write JSON to *path* atomically.

    Writes to a tempfile in the SAME directory (so os.replace is atomic),
    fsyncs, then renames. Creates parent dirs if missing. On error,
    cleans up the tempfile.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=p.name + ".tmp.",
        dir=str(p.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, p)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_json_safe(path: Path | str, default: Any = None) -> Any:
    """Read JSON from *path*. Return *default* on missing/corrupt/unreadable.

    Catches FileNotFoundError, PermissionError, UnicodeDecodeError,
    JSONDecodeError, OSError. Never raises.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, UnicodeDecodeError, OSError):
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default
