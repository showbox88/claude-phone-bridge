# Attachment Paths to Claude — Design

**Date:** 2026-06-01
**Status:** Approved, ready for planning
**Author:** Claude + Showbox

## Problem

Phone Bridge accepts file uploads (images, PDFs, text, spreadsheets) and forwards them to the Claude Agent SDK as multimodal content blocks. Claude can *analyze* the content but has no way to *manipulate* the underlying file: it never learns the disk path, and the on-disk name is a random UUID with no relation to the original filename.

This blocks downstream workflows the user wants:

- Suggest a better filename for a photo and rename it on disk
- Upload a photo or PDF to Google Drive
- Move attachments into project folders
- Any operation that requires `Bash` or `Read` on the actual file

The Anthropic content block contains base64 bytes; it does not carry the filesystem location.

## Goal

After upload, the Claude SDK turn receives both:

1. The image/document bytes (unchanged, so analysis still works), and
2. A textual list of absolute file paths and original filenames for every attachment in that turn,

so Claude can call `Bash`/`Read` to act on the files.

## Non-Goals

- Google Drive integration (OAuth, `rclone`, `gdrive` CLI, MCP server) — out of scope; this design only exposes paths so a future turn can drive that workflow.
- Cross-session file persistence — user confirmed deletion should follow session deletion.
- WebSocket protocol changes — keep `images: [path]` and `files: [path]` as flat lists of strings.
- Client-side changes — the PWA does not need to know about this.
- Database schema changes — `_save_msg` continues to record the same shape.

## Design

### Storage layout

Today (single flat dir per session):

```
.bridge_uploads/<session_id>/<uuid32>.<ext>
```

After:

```
.bridge_uploads/<session_id>/<uuid8>/<safe_original_filename>
```

- `<uuid8>` is `uuid.uuid4().hex[:8]` — collision-proof per session, short enough for paths.
- `<safe_original_filename>` preserves the user's filename, including spaces, Unicode (CJK, emoji), and the original extension.
- One file per subdirectory eliminates same-name collisions across uploads in the same session.
- Existing teardown (`shutil.rmtree(uploads_dir() / sid)` in session-delete) still works unchanged — it removes the session dir and everything under it.

### Filename sanitization

Minimal — only strip what breaks filesystem behavior or path parsing:

```python
def _safe_filename(name: str) -> str:
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

Deliberately **kept**: spaces, Chinese characters, emoji, parentheses, brackets, `+`, `&`, `'`. All are valid on ext4 and Claude reads them via `Read` / `Bash` without issue (`Bash` quotes paths automatically when needed).

Deliberately **rejected**: literal path separators, control bytes, leading dots, names > 200 chars.

### Upload endpoint changes (`POST /api/upload`)

Two changes in `api_upload` (server.py around line 1702):

1. Replace the saved filename construction:
   ```python
   # was:
   name = f"{uuid.uuid4().hex}{ext}"
   dest = sdir / name

   # becomes:
   uid = uuid.uuid4().hex[:8]
   safe_name = _safe_filename(original_name)
   # Preserve the user's original extension; only override when we have a
   # higher-confidence mime-derived one and the original lacks any ext.
   if "." not in safe_name and kind == "image" and mime in ALLOWED_IMAGE_MIMES:
       guessed = mimetypes.guess_extension(mime) or ""
       if guessed == ".jpe":
           guessed = ".jpg"
       if guessed:
           safe_name = safe_name + guessed
   sub = sdir / uid
   sub.mkdir(parents=True, exist_ok=True)
   dest = sub / safe_name
   ```

2. The returned `path` (`rel`) becomes `<session_id>/<uid>/<safe_name>` — one more path segment than before. The `/uploads/<rel>` static mount handles arbitrary subdirectory depth, so the preview URL still works without code changes.

### Content-block injection (`_build_user_content`)

Today the function (server.py around line 477) takes two attachment params with confusing names:

- `images: list[str]` — uploaded relative paths under `.bridge_uploads/` for **all** kinds (image, pdf, text, sheet). The name is historical; treat it as "uploaded attachments".
- `files: list[str]` — already-absolute paths Claude should `Read`, used only in code-mode. These are already injected as text in `text_parts` at lines 486-489, so we do not need to advertise them again.

It returns `content = [{"type": "text", "text": full_text}] + blocks`.

The existing loop (lines 495-527) already resolves each `images` entry to a vetted `abs_p` inside the uploads dir. The cleanest change is to piggy-back on that loop — collect `(abs_p, mime)` for every successfully-handled entry — then append a final text block to `content`:

```python
# Inside the existing `for rel in images[:MAX_IMAGES_PER_MESSAGE]:` loop,
# after the `if kind == "image" / pdf / text / sheet` dispatch, record
# the path-mime pair for every entry that was not `continue`d above:
advertised_paths: list[tuple[Path, str]] = []
# ... (initialize before the loop)
# ... at the end of each handled branch (image/pdf/text/sheet), append:
advertised_paths.append((abs_p, mime))

# After the loop, after `content` is built:
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
```

Notes:

- Appended to `content` (the final return value), so the path block lands **after** all the image / document blocks — Claude has already "seen" the bytes when it reads the paths.
- Reuses the loop's existing path-traversal vetting (`abs_p.relative_to(udir.resolve())` at line 499) — no second round of security checks needed.
- Only the `images` list is iterated; `files` are already advertised by the earlier `text_parts` block at lines 486-489, so we do not double-list them.
- Absolute paths are used because the SDK's working directory is `/home/dev` (see CLAUDE.md `DEFAULT_CWD`), not the project. Relative paths would require Claude to guess.
- Entries skipped earlier in the loop (not found, outside uploads, unknown kind) are also skipped here automatically — they never made it into `advertised_paths`.

### Path display in transcript

The new content block is sent to the SDK but should **not** be re-rendered in the phone UI — it would clutter the user's view of their own message. Two options:

- **Chosen**: leave the existing client untouched. The PWA renders user messages from `images`/`files` arrays it already has, never from the SDK content array, so the extra block is naturally invisible. No client change needed.
- Rejected: explicitly tag the block with a custom marker for the client to strip — unnecessary given the above.

### Backward compatibility

- Old uploads (flat `<uuid32>.ext` in session dirs from before this change): still readable by `_build_user_content` because the loop just reads whatever path `images` contains. The advertised disk path will simply be the flat one. No migration needed; sessions are ephemeral.
- Database rows storing old-style relative paths: same — the path string is opaque to the code that reads it back.

## Affected files

| File | Change |
|---|---|
| `server.py` — `api_upload` | New per-file uid subdirectory + sanitized original filename |
| `server.py` — `_build_user_content` | Append a path-listing text block |
| `server.py` — module top | Add `_safe_filename` helper |

No new modules, no new dependencies, no schema or config changes.

## Testing

Manual smoke tests after deploy:

1. Upload a single JPEG with an ASCII name (`vacation.jpg`) → confirm file lands at `.bridge_uploads/<sid>/<uid8>/vacation.jpg`, confirm Claude turn shows the path in its content (via SDK debug logs or by asking Claude "what file did I attach?").
2. Upload a JPEG with a Chinese name (`照片.jpg`) → confirm the basename round-trips correctly and Claude can `ls` it.
3. Upload two files with the same name in one turn → confirm each gets its own `<uid8>` subdir and no overwrite occurs.
4. Upload a PDF → confirm the path is advertised alongside the document content block and Claude can `Read` the file.
5. Delete the session via the UI → confirm `<sid>` and all child `<uid8>/` dirs are removed from disk.
6. Have Claude rename one of the uploaded files in-place and re-list — confirm Bash can act on the path.

## Open questions

None at design-approval time.

## Out-of-scope follow-ups

- **Google Drive workflow** (next conversation): probably an SDK MCP server wrapping `rclone copy` to a pre-configured remote, or a Python helper using the official `google-api-python-client` with OAuth installed on `dashboard-server`.
- **Smart filename suggestions**: leave to Claude — it has the image content and the original name, that's enough to suggest a better one.
- **Cross-session attachment archive**: not requested; user explicitly preferred ephemeral-with-session.
