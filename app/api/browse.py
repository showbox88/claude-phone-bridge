"""Workspace filesystem browse + mkdir.

Both routes sandbox the path strictly inside `state.cwd_root` via
`_resolve_in_root`. Hidden files (dot-prefixed) are filtered out.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.persistence.files import _resolve_in_root, _to_rel
from app.state import state

router = APIRouter()


@router.get("/api/browse")
async def browse(path: str = ""):
    target = _resolve_in_root(path)
    if target is None or not target.is_dir():
        raise HTTPException(404, "not a directory or outside root")
    entries = []
    try:
        for e in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if e.name.startswith("."):
                continue
            try:
                is_dir = e.is_dir()
                size = e.stat().st_size if not is_dir else 0
            except OSError:
                continue
            entries.append({"name": e.name, "is_dir": is_dir, "size": size})
    except PermissionError:
        raise HTTPException(403, "permission denied")
    rel = _to_rel(target)
    parent = None
    if rel:
        parent = _to_rel(target.parent)
    return {
        "root": str(state.cwd_root).replace("\\", "/"),
        "path": rel,
        "abs": str(target).replace("\\", "/"),
        "parent": parent,
        "entries": entries,
        "current": _to_rel(state.cwd),
    }


@router.post("/api/mkdir")
async def mkdir(body: dict):
    name = (body.get("name", "") or "").strip()
    if not name or any(c in name for c in "/\\:*?\"<>|") or name in (".", "..") or len(name) > 100:
        raise HTTPException(400, "invalid folder name")
    base = _resolve_in_root(body.get("path", ""))
    if base is None or not base.is_dir():
        raise HTTPException(404, "base not a directory")
    new_dir = base / name
    if _resolve_in_root(_to_rel(new_dir)) is None:
        raise HTTPException(400, "outside root")
    try:
        new_dir.mkdir(exist_ok=False)
    except FileExistsError:
        raise HTTPException(409, "already exists")
    except OSError as e:
        raise HTTPException(500, str(e))
    return {"ok": True, "path": _to_rel(new_dir)}
