# Attachment Paths to Claude — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a user uploads files to Phone Bridge, give the Claude SDK turn the absolute disk paths so Claude can read/rename/move/upload them with `Bash` and `Read`.

**Architecture:** Three localized changes in `server.py`: a new `_safe_filename` helper, a new per-file uid-subdirectory upload layout (`<sid>/<uuid8>/<safe_name>`), and a trailing text block in `_build_user_content` listing the absolute paths. One tiny test file for the pure helper. No new dependencies, no schema changes, no frontend changes.

**Tech Stack:** Python 3.11, FastAPI (existing), no test framework — bare-assert smoke tests run with `python`.

**Spec:** [`docs/superpowers/specs/2026-06-01-attachment-paths-to-claude-design.md`](../specs/2026-06-01-attachment-paths-to-claude-design.md)

---

## File Structure

| File | Action | Purpose |
|---|---|---|
| `server.py` (top of helpers, near line 420) | Modify | Add `_safe_filename(name: str) -> str` |
| `server.py` (`_build_user_content`, lines 477-534) | Modify | Collect `(abs_p, mime)` per attachment, append a "files on disk" text block to `content` |
| `server.py` (`api_upload`, lines 1702-1754) | Modify | Save each upload to `<sid>/<uuid8>/<safe_name>` instead of `<sid>/<uuid32>.<ext>` |
| `tests/test_safe_filename.py` | Create | Bare-assert tests for the pure helper |

`tests/` is already in `.deploy.json` `exclude`, so test files never ship to the server.

---

## Task 1: Add `_safe_filename` helper (TDD)

**Files:**
- Create: `tests/test_safe_filename.py`
- Modify: `server.py` (add helper near other private helpers around line 437, just before `_read_text_safe`)

- [ ] **Step 1: Write the failing test**

Create `tests/test_safe_filename.py` with this exact content:

```python
"""Tests for server._safe_filename. Run: python tests/test_safe_filename.py"""
import sys
from pathlib import Path

# Make `server` importable when running this file directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import _safe_filename


def test_plain_ascii_name_preserved():
    assert _safe_filename("vacation.jpg") == "vacation.jpg"


def test_chinese_name_preserved():
    assert _safe_filename("照片.jpg") == "照片.jpg"


def test_spaces_preserved():
    assert _safe_filename("my photo.jpg") == "my photo.jpg"


def test_emoji_preserved():
    assert _safe_filename("trip 🌴.jpg") == "trip 🌴.jpg"


def test_forward_slash_stripped_to_basename():
    assert _safe_filename("foo/bar/baz.jpg") == "baz.jpg"


def test_backslash_stripped_to_basename():
    assert _safe_filename("C:\\evil\\path.jpg") == "path.jpg"


def test_null_byte_removed():
    assert _safe_filename("a\x00b.jpg") == "ab.jpg"


def test_control_chars_removed():
    assert _safe_filename("a\x01b\x1fc.jpg") == "abc.jpg"


def test_leading_dot_stripped():
    assert _safe_filename(".hidden.jpg") == "hidden.jpg"


def test_multiple_leading_dots_stripped():
    assert _safe_filename("...hidden.jpg") == "hidden.jpg"


def test_empty_string_falls_back():
    assert _safe_filename("") == "upload.bin"


def test_only_dots_falls_back():
    assert _safe_filename("...") == "upload.bin"


def test_only_path_separators_falls_back():
    assert _safe_filename("///") == "upload.bin"


def test_long_name_truncated_to_200():
    long = "a" * 500 + ".jpg"
    result = _safe_filename(long)
    assert len(result) == 200
    assert result.startswith("a")


def test_dot_inside_name_preserved():
    assert _safe_filename("my.report.v2.pdf") == "my.report.v2.pdf"


if __name__ == "__main__":
    tests = [
        (name, fn) for name, fn in globals().items()
        if name.startswith("test_") and callable(fn)
    ]
    failures = []
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as e:
            failures.append(name)
            print(f"FAIL  {name}: {e}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
```

- [ ] **Step 2: Run the test and verify it fails**

```bash
python tests/test_safe_filename.py
```

Expected: ImportError — `cannot import name '_safe_filename' from 'server'`.

(If it imports something else by that name from cache, delete `__pycache__/` and retry.)

- [ ] **Step 3: Add the `_safe_filename` helper to `server.py`**

Find the line immediately above `def _read_text_safe(path: Path) -> str:` (currently around line 437). Insert this function above it:

```python
def _safe_filename(name: str) -> str:
    """Strip filesystem-hostile bytes from an uploaded filename.

    Preserves spaces and Unicode (CJK, emoji); rejects only path separators,
    control bytes, leading dots, and over-long names. Falls back to
    'upload.bin' when nothing usable remains.
    """
    # basename only — drop any path components the client tried to sneak in
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    # control chars (incl. null) — disallowed on all real filesystems
    name = re.sub(r"[\x00-\x1f]", "", name)
    # no leading dot (avoid hidden files / dotfile collisions)
    name = name.lstrip(".")
    # cap length to stay well under filesystem limits
    name = name[:200]
    return name or "upload.bin"
```

Confirm `re` is already imported at the top of `server.py` (it is — used elsewhere). If not, add `import re` to the existing import block.

- [ ] **Step 4: Run the test and verify it passes**

```bash
python tests/test_safe_filename.py
```

Expected output ends with `15/15 passed` and exit code 0.

- [ ] **Step 5: Commit**

```bash
git add tests/test_safe_filename.py server.py
git commit -m "Add _safe_filename helper + tests

Sanitizes uploaded filenames so the user's original name can land on
disk as the basename. Preserves Unicode, strips only filesystem-hostile
bytes.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: Update `api_upload` to per-file uid-subdir layout

**Files:**
- Modify: `server.py` lines 1716-1745 (the loop inside `api_upload`)

No new automated test — this is FastAPI-integration territory; the manual smoke test in Task 4 covers it.

- [ ] **Step 1: Read the current loop body**

The current loop (lines 1717-1745) builds `name = f"{uuid.uuid4().hex}{ext}"` and writes to `sdir / name`. Confirm the file still matches this shape before editing.

- [ ] **Step 2: Replace the filename / destination construction**

Find this block inside `api_upload`:

```python
        # Preserve a sensible extension for later mime detection on disk.
        if kind == "image" and mime in ALLOWED_IMAGE_MIMES:
            ext = mimetypes.guess_extension(mime) or ext_in or ".bin"
            if ext == ".jpe":
                ext = ".jpg"
        else:
            ext = ext_in or ".bin"
        name = f"{uuid.uuid4().hex}{ext}"
        dest = sdir / name
```

Replace it with:

```python
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
```

- [ ] **Step 3: Verify the relative path output uses the new shape**

Find the existing line further down:

```python
        rel = f"{session_id}/{name}"
```

Confirm it now produces `<session_id>/<uid>/<safe_name>`. The variable `name` is now `f"{uid}/{safe_name}"` from Step 2, so no change to this line is needed.

- [ ] **Step 4: Quick syntax check**

```bash
python -c "import ast; ast.parse(open('server.py', encoding='utf-8').read()); print('OK')"
```

Expected: `OK`. If you see a SyntaxError, fix the indentation in Step 2's paste — the block sits inside a `for f in files:` loop so all lines need the same leading indent as the surrounding code.

- [ ] **Step 5: Commit**

```bash
git add server.py
git commit -m "Upload: per-file uid subdir, preserve original filename

Each upload now lands at .bridge_uploads/<sid>/<uuid8>/<original-name>
instead of .bridge_uploads/<sid>/<uuid32>.<ext>. Eliminates same-name
collisions and surfaces the user's filename as the on-disk basename.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: Advertise disk paths to Claude in `_build_user_content`

**Files:**
- Modify: `server.py` lines 477-534 (the `_build_user_content` function body)

- [ ] **Step 1: Add a list to collect resolved paths**

Find the line `blocks: list[dict] = []             # image/document content blocks` (around line 493). Add one line below it:

```python
    advertised_paths: list[tuple[Path, str]] = []  # (abs_p, mime) for the trailing path block
```

- [ ] **Step 2: Record each successfully-handled attachment**

Inside the `for rel in images[:MAX_IMAGES_PER_MESSAGE]:` loop, every branch that actually handles the file (image / pdf / text / sheet) should append to `advertised_paths`. Locate the four branches and add `advertised_paths.append((abs_p, mime))` at the end of each.

After the edit, the inside of the loop should look like:

```python
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
```

The only changes are the four new `advertised_paths.append(...)` lines.

- [ ] **Step 3: Append the path block to `content` before returning**

Find the last three lines of the function:

```python
    full_text = "\n".join(text_parts).strip() or "(no text)"
    content: list[dict] = [{"type": "text", "text": full_text}]
    content.extend(blocks)
    return content
```

Replace with:

```python
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
```

- [ ] **Step 4: Quick syntax check**

```bash
python -c "import ast; ast.parse(open('server.py', encoding='utf-8').read()); print('OK')"
```

Expected: `OK`.

- [ ] **Step 5: Confirm the helper test still passes**

```bash
python tests/test_safe_filename.py
```

Expected: `15/15 passed`. (Nothing should have changed for it, but a fast sanity check is cheap.)

- [ ] **Step 6: Commit**

```bash
git add server.py
git commit -m "Advertise uploaded file paths to Claude SDK

Append a trailing text content block listing each uploaded
attachment's absolute path, so Claude can rename / move / upload
the originals via Bash or Read.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: Deploy and end-to-end smoke test

**Files:** none modified — operational task.

- [ ] **Step 1: Deploy to dashboard-server**

From `E:\Project\Chat bot` in PowerShell:

```powershell
$env:PATH = "$env:PATH;C:\Users\showb\bin"; deploy
```

Expected tail of output: `[deploy] HTTP 200 - {"ok":true}` and `[deploy] OK`.

- [ ] **Step 2: Smoke test — ASCII filename**

On your phone, open the Phone Bridge UI, attach a JPEG named `vacation.jpg` to a new turn, send something like "what file did I attach? show me its disk path". Expected behavior: Claude's reply quotes a path of the form `/home/dev/phone-bridge/.bridge_uploads/<sid>/<8-hex-chars>/vacation.jpg`.

Also verify on the server:

```bash
ssh dashboard-server "ls -la /home/dev/phone-bridge/.bridge_uploads/*/  2>/dev/null | tail -20"
```

Expected: the latest entry shows an 8-hex-char directory containing a file literally named `vacation.jpg`.

- [ ] **Step 3: Smoke test — Chinese filename**

Attach a JPEG named `照片.jpg`. Ask Claude to list it. Expected: Claude's reply shows the file with its Chinese name intact and can `ls` it.

- [ ] **Step 4: Smoke test — duplicate names in one turn**

Upload two files both named `image.jpg` in the same message. Expected: server log + `ls` shows two distinct 8-hex-char subdirs, each containing one `image.jpg`. Neither overwrote the other.

- [ ] **Step 5: Smoke test — PDF**

Attach a small PDF. Ask Claude "what's the absolute path of the PDF I just sent?". Expected: a path under `.bridge_uploads/<sid>/<8-hex>/<your-pdf-name>.pdf` is reported. Then ask Claude to copy it somewhere with `cp` — confirm the file system call succeeds.

- [ ] **Step 6: Smoke test — session deletion clean-up**

Delete the test session in the UI. On the server:

```bash
ssh dashboard-server "ls /home/dev/phone-bridge/.bridge_uploads/<sid>/ 2>&1"
```

Expected: `No such file or directory` — the whole session dir (including all per-file uid subdirs) was removed.

- [ ] **Step 7: Note results**

If all six smoke tests pass, the feature is live. If any failed, capture the failure mode and stop here — the next conversation can iterate. Do not roll back unless the service health check is failing.

---

## Self-Review Notes

**Spec coverage check:**

| Spec requirement | Task |
|---|---|
| Storage layout `<sid>/<uid8>/<safe_name>` | Task 2 |
| `_safe_filename` minimal sanitization | Task 1 |
| Append text block listing absolute paths in `_build_user_content` | Task 3 |
| Reuse existing loop's path-traversal vetting | Task 3 step 2 (piggyback on existing abs_p) |
| Iterate `images` only, not `files` | Task 3 (only the in-loop branches are touched) |
| No client / WS protocol / schema changes | (no task — intentionally absent) |
| Manual smoke tests #1–#6 from spec | Task 4 steps 2-6 |

No spec requirements are unaddressed.

**Type / signature consistency:** `_safe_filename(name: str) -> str` used identically in Tasks 1 and 2. `advertised_paths: list[tuple[Path, str]]` introduced in Task 3 step 1, consumed in step 3 — same shape. `abs_p` and `mime` are existing locals from the surrounding loop; no rename needed.

**Placeholder scan:** none — every code block is concrete, every command is runnable.
