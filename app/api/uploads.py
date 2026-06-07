"""POST /api/upload — multipart receiver for images, PDFs, text, sheets.

Streams each file into `<uploads_dir>/<session_id>/<short_uuid>/<safe_name>`.
Enforces MAX_UPLOAD_BYTES per file and MAX_IMAGES_PER_MESSAGE count.
"""
from __future__ import annotations

import mimetypes
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

import db

from app.persistence.files import (
    ALLOWED_IMAGE_MIMES,
    MAX_IMAGES_PER_MESSAGE,
    MAX_UPLOAD_BYTES,
    _safe_filename,
    classify_upload,
    uploads_dir,
)

router = APIRouter()


@router.post("/api/upload")
async def api_upload(
    session_id: str = Form(...),
    files: list[UploadFile] = File(...),
):
    sess = db.get_session(session_id)
    if not sess:
        raise HTTPException(404, "session not found")
    if len(files) > MAX_IMAGES_PER_MESSAGE:
        raise HTTPException(400, f"too many files (max {MAX_IMAGES_PER_MESSAGE})")

    sdir = uploads_dir() / session_id
    sdir.mkdir(parents=True, exist_ok=True)

    saved: list[dict] = []
    for f in files:
        original_name = f.filename or "upload.bin"
        ext_in = Path(original_name).suffix.lower()  # noqa: F841
        mime = (f.content_type or "").lower()
        kind = classify_upload(original_name, mime)
        if not kind:
            raise HTTPException(400, f"unsupported file type: {original_name}")
        # Sanitize the user's original filename; this becomes the on-disk
        # basename so Claude (and `ls`) see the real name.
        safe_name = _safe_filename(original_name)
        # If the sanitized name has no extension AND we have a higher-confidence
        # mime-derived one for images, append it. Other kinds keep whatever the
        # user provided (PDF/text/sheet extensions are already meaningful).
        if "." not in safe_name and kind == "image" and mime in ALLOWED_IMAGE_MIMES:
            guessed = mimetypes.guess_extension(mime) or ""
            if guessed == ".jpe":
                guessed = ".jpg"
            if guessed:
                safe_name = safe_name + guessed
        # Each file gets its own short-uuid subdir; eliminates name collisions
        # within a session and keeps cleanup as a single rmtree.
        uid = uuid.uuid4().hex[:8]
        sub = sdir / uid
        sub.mkdir(parents=True, exist_ok=True)
        dest = sub / safe_name
        name = f"{uid}/{safe_name}"  # used in the relative path below
        size = 0
        with dest.open("wb") as out:
            while True:
                chunk = await f.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(413, f"file too large (>{MAX_UPLOAD_BYTES} bytes)")
                out.write(chunk)
        rel = f"{session_id}/{name}"
        saved.append({
            "path": rel,
            "url": f"/uploads/{rel}",
            "mime": mime or mimetypes.guess_type(original_name)[0] or "",
            "size": size,
            "name": original_name,
            "kind": kind,  # 'image' | 'pdf' | 'text' | 'sheet'
        })
    return {"files": saved}
