# Frontend Baseline Screenshots

Reference images for visual regression after Phase 4 (frontend modularization).
Captured manually on a real device — playwright automation is deferred (see
phase-4 plan if you want to add it later).

## Capture procedure (one-time, before any phase starts touching frontend)

Open the PWA on a recent iPhone or Android (Tailscale-connected).
For each scene below, take a portrait screenshot and save under
`tests/baseline/<filename>.png` (PNG, no resize).

### Required scenes

| # | Filename | How to reach it |
|---|---|---|
| 1 | `01-home.png` | Open PWA at root, default mode, no messages yet |
| 2 | `02-chat-streaming.png` | Send a message that triggers streaming; capture mid-stream |
| 3 | `03-tool-group-closed.png` | After a tool call collapses (the `▸` state) |
| 4 | `03-tool-group-open.png` | Same tool group expanded (`▾` state) |
| 5 | `04-permission-card.png` | Trigger a permission_request and capture the card before approving |
| 6 | `05-drawer-sessions.png` | Open the left drawer with session list |
| 7 | `06-modal-usage.png` | Open the usage modal |
| 8 | `07-modal-weekly.png` | Open weekly-report settings |
| 9 | `08-modal-sync.png` | Open sync-settings; capture the targets table |
| 10 | `09-modal-cwd.png` | Open the cwd browser |
| 11 | `10-bell-todos.png` | Open the bell with at least 2 today-todos |
| 12 | `11-checkin-dialog.png` | Open the checkin dialog at stage 1 (POI list) |
| 13 | `12-source-picker.png` | Open the source picker |

### After capture

```bash
git add tests/baseline/*.png
git commit -m "refactor(tests): add baseline frontend screenshots for visual regression"
```

## Comparing after a refactor phase

Open each new screenshot side-by-side with its baseline. Eyeball the diff:
spacing, color, font, missing elements, extra elements. Any meaningful drift =
flag it in the phase's completion report.

(There is no auto-diff tool yet. If you want pixel diff, install ImageMagick
and `compare baseline.png new.png diff.png`.)
