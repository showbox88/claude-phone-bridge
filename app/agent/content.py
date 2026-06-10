"""User-turn content builder.

Turns the phone-side `(text, images[], files[])` payload into the structured
content blocks the Claude SDK expects:
- A single text block (`type: text`) including any inlined text-file bodies.
- One image block per uploaded image (base64-embedded).
- One document block per uploaded PDF (base64-embedded).
- A trailing text block listing every advertised path on disk so Claude can
  Read/Bash on the originals.

xlsx attachments are converted to CSV-like text via openpyxl and inlined into
the text block (capped per-sheet via MAX_SHEET_ROWS_PER_SHEET).
"""
from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

from app.log import get_logger
from app.persistence.files import (
    MAX_IMAGES_PER_MESSAGE,
    MAX_SHEET_ROWS_PER_SHEET,
    MAX_TEXT_INLINE_CHARS,
    classify_upload,
    uploads_dir,
)

log = get_logger("bridge")


def _read_text_safe(path: Path) -> str:
    """Read a text file with reasonable encoding fallbacks."""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            txt = data.decode(enc)
            if len(txt) > MAX_TEXT_INLINE_CHARS:
                txt = txt[:MAX_TEXT_INLINE_CHARS] + f"\n…(truncated, {len(txt) - MAX_TEXT_INLINE_CHARS} more chars)"
            return txt
        except UnicodeDecodeError:
            continue
    return "(unreadable encoding)"


def _read_xlsx_as_text(path: Path) -> str:
    """Convert an .xlsx file into a CSV-like text snapshot. Requires openpyxl."""
    try:
        import openpyxl  # type: ignore
    except ImportError:
        return "(无法解析 .xlsx：服务器未安装 openpyxl，运行 `pip install openpyxl` 后重试)"
    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except Exception as e:  # noqa: BLE001
        return f"(无法打开 xlsx: {e})"
    sections: list[str] = []
    for ws in wb.worksheets:
        rows: list[str] = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= MAX_SHEET_ROWS_PER_SHEET:
                rows.append(f"… (truncated at {MAX_SHEET_ROWS_PER_SHEET} rows)")
                break
            rows.append(",".join("" if v is None else str(v).replace(",", "\\,") for v in row))
        sections.append(f"--- Sheet: {ws.title} ---\n" + "\n".join(rows))
    wb.close()
    return "\n\n".join(sections) if sections else "(empty workbook)"


def _build_user_content(text: str, images: list[str], files: list[str]) -> list[dict]:
    """Build Anthropic-style content blocks: text + image/document base64 entries.

    `images` is the list of uploaded attachment relative paths under .bridge_uploads/;
    each entry is dispatched to an image block (PNG/JPEG/WEBP/GIF) or a document
    block (PDF) based on its mime. `files` are absolute paths on disk that Claude
    will read via its Read tool (only useful in code mode).
    """
    text_parts = [text] if text else []
    if files:
        text_parts.append("\n附加文件（已在本机，请按需 Read）：")
        for f in files:
            text_parts.append(f"- {f}")

    udir = uploads_dir()
    inline_text_blobs: list[str] = []   # text/sheet content collected for the text block
    blocks: list[dict] = []             # image/document content blocks
    advertised_paths: list[tuple[Path, str]] = []  # (abs_p, mime) for the trailing path block

    for rel in images[:MAX_IMAGES_PER_MESSAGE]:
        rel_norm = rel.replace("\\", "/").lstrip("/")
        try:
            abs_p = (udir / rel_norm).resolve()
            abs_p.relative_to(udir.resolve())
        except (ValueError, OSError):
            log.warning("rejecting upload path outside uploads dir: %s", rel)
            continue
        if not abs_p.is_file():
            log.warning("upload not found: %s", abs_p)
            continue
        kind = classify_upload(abs_p.name, mimetypes.guess_type(abs_p.name)[0] or "")
        mime = mimetypes.guess_type(abs_p.name)[0] or "application/octet-stream"
        if kind == "image":
            data = base64.standard_b64encode(abs_p.read_bytes()).decode("ascii")
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": data},
            })
            advertised_paths.append((abs_p, mime))
        elif kind == "pdf":
            data = base64.standard_b64encode(abs_p.read_bytes()).decode("ascii")
            blocks.append({
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": data},
            })
            advertised_paths.append((abs_p, mime))
        elif kind == "text":
            body = _read_text_safe(abs_p)
            inline_text_blobs.append(f"\n--- 附件: {abs_p.name} ---\n```\n{body}\n```")
            advertised_paths.append((abs_p, mime))
        elif kind == "sheet":
            body = _read_xlsx_as_text(abs_p)
            inline_text_blobs.append(f"\n--- 附件: {abs_p.name} ---\n```csv\n{body}\n```")
            advertised_paths.append((abs_p, mime))
        else:
            log.warning("skipping unsupported file %s", abs_p)

    if inline_text_blobs:
        text_parts.extend(inline_text_blobs)
    full_text = "\n".join(text_parts).strip() or "(no text)"
    content: list[dict] = [{"type": "text", "text": full_text}]
    content.extend(blocks)

    # Trailing "files on disk" block so Claude can Read / Bash on the originals.
    if advertised_paths:
        path_lines = []
        for abs_p, mime in advertised_paths:
            try:
                size_kb = max(1, abs_p.stat().st_size // 1024)
            except OSError:
                continue
            path_lines.append(f"- {abs_p} ({mime}, {size_kb} KB)")
        if path_lines:
            content.append({
                "type": "text",
                "text": (
                    "[Attached files on server disk — you can read, rename, move, "
                    "or upload them with Bash or Read tools:\n"
                    + "\n".join(path_lines)
                    + "]"
                ),
            })

    return content
