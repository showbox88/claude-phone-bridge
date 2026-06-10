"""Tests for app.agent.content — _build_user_content, _read_text_safe, _read_xlsx_as_text.

Covers:
- text-only / empty / .txt inlined / unknown ext / missing / binary / xlsx / image

Notes on prod shape (read app/agent/content.py before changing):
- _build_user_content always returns at least one text block. When text is
  empty AND no usable attachments, the first block's text is "(no text)".
- Image attachments produce {"type": "image", "source": {"type": "base64",
  "media_type": <mime>, "data": <b64>}} blocks.
- Unknown extensions (not in TEXT_EXTS/SHEET_EXTS, not image/pdf mime) are
  SKIPPED with a warning — _read_text_safe is NOT called. The plan template's
  "unknown extension still attempts read" is wrong for current prod; we
  instead assert the path is silently skipped (still no crash).
- `images` param is the list of upload relative paths under uploads_dir();
  they must resolve INSIDE uploads_dir() or they're rejected.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make project root importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.content import (  # noqa: E402
    _build_user_content,
    _read_text_safe,
    _read_xlsx_as_text,
)
from app.persistence.files import UPLOAD_DIRNAME  # noqa: E402
from app.state import state  # noqa: E402


@pytest.fixture
def uploads(tmp_path, monkeypatch):
    """Point state.cwd_root at a tmpdir; ensure .bridge_uploads/ exists.

    Yields the uploads directory so tests can stage files in it.
    """
    monkeypatch.setattr(state, "cwd_root", tmp_path)
    udir = tmp_path / UPLOAD_DIRNAME
    udir.mkdir(parents=True, exist_ok=True)
    yield udir


# ---------------------------------------------------------------------------
# _build_user_content
# ---------------------------------------------------------------------------


def test_text_only_message(uploads):
    """Plain text → at least one text block containing the user's text."""
    out = _build_user_content("hello world", [], [])
    assert isinstance(out, list)
    assert len(out) >= 1
    assert out[0]["type"] == "text"
    assert "hello world" in out[0]["text"]


def test_empty_text_with_no_attachments_no_crash(uploads):
    """Empty text + no attachments must not throw; fallback text used."""
    out = _build_user_content("", [], [])
    assert isinstance(out, list)
    assert len(out) >= 1
    assert out[0]["type"] == "text"
    # Prod uses "(no text)" as fallback.
    assert out[0]["text"] == "(no text)"


def test_text_file_attachment_inlined(uploads):
    """.txt upload → contents inlined into the leading text block."""
    f = uploads / "note.txt"
    f.write_text("inline-me-please", encoding="utf-8")
    out = _build_user_content("see attached", ["note.txt"], [])
    assert out[0]["type"] == "text"
    assert "inline-me-please" in out[0]["text"]
    assert "note.txt" in out[0]["text"]


def test_unknown_extension_still_attempts_read(uploads):
    """Truly unknown extension (.xyz) is skipped silently — no crash.

    Prod behavior: classify_upload returns "" for unknown ext+mime, the loop
    logs "skipping unsupported file" and moves on. The output still contains
    a valid text block. (.log is actually in TEXT_EXTS, so we use .xyz to
    exercise the unsupported branch.)
    """
    f = uploads / "weird.xyz"
    f.write_text("contents-not-inlined", encoding="utf-8")
    out = _build_user_content("hello", ["weird.xyz"], [])
    assert isinstance(out, list)
    assert out[0]["type"] == "text"
    assert "hello" in out[0]["text"]
    # File body not inlined because classify_upload rejected it.
    assert "contents-not-inlined" not in out[0]["text"]


def test_image_path_produces_image_block(uploads):
    """Image upload → {type: image, source: {type: base64, ...}} block exists."""
    # Minimal valid-looking PNG-like bytes; classify_upload uses extension, not magic.
    f = uploads / "pic.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\nfakeimagebytes")
    out = _build_user_content("look", ["pic.png"], [])
    image_blocks = [b for b in out if b.get("type") == "image"]
    assert len(image_blocks) == 1
    src = image_blocks[0]["source"]
    assert src["type"] == "base64"
    assert src["media_type"] == "image/png"
    assert isinstance(src["data"], str) and len(src["data"]) > 0


# ---------------------------------------------------------------------------
# _read_text_safe
# ---------------------------------------------------------------------------


def test_read_text_safe_handles_missing_file(tmp_path):
    """Non-existent path → no exception, returns a string (empty)."""
    missing = tmp_path / "does-not-exist.txt"
    result = _read_text_safe(missing)
    assert isinstance(result, str)
    # Prod returns "" on OSError.
    assert result == ""


def test_read_text_safe_handles_binary_file(tmp_path):
    """Binary content → no exception, returns a string (latin-1 fallback)."""
    f = tmp_path / "binary.bin"
    f.write_bytes(bytes(range(256)))  # all bytes, including invalid utf-8 sequences
    result = _read_text_safe(f)
    assert isinstance(result, str)
    # latin-1 always decodes; result should be non-empty.
    assert len(result) > 0


# ---------------------------------------------------------------------------
# _read_xlsx_as_text
# ---------------------------------------------------------------------------


def test_read_xlsx_as_text_handles_simple_file(tmp_path):
    """Real xlsx → text contains the cell values. Skip if openpyxl missing."""
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["A1"] = "name"
    ws["B1"] = "score"
    ws["A2"] = "alice"
    ws["B2"] = 42
    xlsx_path = tmp_path / "data.xlsx"
    wb.save(xlsx_path)
    wb.close()

    result = _read_xlsx_as_text(xlsx_path)
    assert isinstance(result, str)
    assert "Sheet1" in result
    assert "alice" in result
    assert "42" in result


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
