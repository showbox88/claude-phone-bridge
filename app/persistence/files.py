"""Upload / path-safety helpers.

Centralizes:
- The list of allowed upload kinds (image / pdf / text / sheet) + their
  mime/extension lists.
- Path-inside-cwd_root sandboxing (_resolve_in_root, _to_rel).
- uploads_dir() — the on-disk root for stored uploads.
- _safe_filename() — strips filesystem-hostile bytes from user-supplied names.

All routines that depend on the live cwd_root read it from `app.state.state`
(mutable singleton). Nothing here imports settings or FastAPI; this module
stays pure-Python and import-time cheap.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.state import state

UPLOAD_DIRNAME = ".bridge_uploads"
MAX_IMAGES_PER_MESSAGE = 4
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25MB per file
ALLOWED_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
ALLOWED_DOC_MIMES = {"application/pdf"}
# Extensions for text-like attachments — read as UTF-8 and embedded inline.
TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".log", ".csv", ".tsv",
    ".json", ".xml", ".yaml", ".yml", ".toml", ".ini", ".env",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".htm", ".css", ".scss",
    ".cpp", ".cc", ".c", ".h", ".hpp", ".java", ".kt", ".go", ".rs",
    ".rb", ".php", ".sh", ".bat", ".ps1", ".sql",
}
SHEET_EXTS = {".xlsx", ".xls"}
MAX_TEXT_INLINE_CHARS = 50_000   # cap per file when inlining text content
MAX_SHEET_ROWS_PER_SHEET = 200   # cap rows when converting xlsx to CSV-like text


def uploads_dir() -> Path:
    p = state.cwd_root / UPLOAD_DIRNAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def _resolve_in_root(rel: str) -> Path | None:
    """Return absolute path inside cwd_root, or None if it would escape."""
    rel = (rel or "").strip().lstrip("/\\")
    if rel in (".", ""):
        return state.cwd_root
    try:
        target = (state.cwd_root / rel).resolve()
        target.relative_to(state.cwd_root)
        return target
    except (ValueError, OSError):
        return None


def _to_rel(p: Path) -> str:
    try:
        rel = p.resolve().relative_to(state.cwd_root)
        s = str(rel).replace("\\", "/")
        return "" if s == "." else s
    except ValueError:
        return ""


def classify_upload(filename: str, mime: str) -> str:
    """Return 'image' | 'pdf' | 'text' | 'sheet' | '' based on filename + mime."""
    ext = Path(filename).suffix.lower()
    mime = (mime or "").lower()
    if mime in ALLOWED_IMAGE_MIMES or ext in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return "image"
    if mime in ALLOWED_DOC_MIMES or ext == ".pdf":
        return "pdf"
    if ext in TEXT_EXTS or mime.startswith("text/") or mime in {"application/json", "application/xml"}:
        return "text"
    if ext in SHEET_EXTS:
        return "sheet"
    return ""


def _safe_filename(name: str) -> str:
    """Strip filesystem-hostile bytes from an uploaded filename.

    Preserves spaces and Unicode (CJK, emoji); rejects only path separators,
    control bytes, leading dots, and over-long names. Falls back to
    'upload.bin' when nothing usable remains.
    """
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f]", "", name)
    name = name.lstrip(".")
    name = name[:200]
    return name or "upload.bin"
