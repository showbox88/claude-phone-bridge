# Phone Bridge Rollback Drill

Use this when a refactor merged to `main` causes regressions in production
(`dashboard-server.tail4cfa2.ts.net`). The whole drill should take < 10 minutes.

## When to roll back

- `tests/smoke_backend.py` fails against prod
- `journalctl -u phone-bridge` shows new ERROR-level lines that weren't there before deploy
- Any user-facing flow stops working

## Procedure

1. **Identify the bad commit:**

   ```bash
   git log --oneline -10
   ```

   The most recent commit on `main` is usually the culprit. If unsure: look
   at `journalctl -u phone-bridge --since "2 hours ago"` for the first error
   timestamp, then `git log --until=<that timestamp>`.

2. **Roll the working tree back:**

   ```bash
   git checkout <last-good-SHA>
   ```

   Do NOT use `git reset --hard` — checkout leaves `main` intact so you can
   investigate and re-roll forward later.

3. **Redeploy:**

   ```bash
   deploy
   ```

   The shared `deploy` tool tars + uploads + restarts `phone-bridge.service`
   and hits `/api/health` to confirm.

4. **Verify the rollback worked:**

   ```bash
   BASE=https://dashboard-server.tail4cfa2.ts.net \
     BRIDGE_COOKIE='bridge_session=...' \
     python tests/smoke_backend.py
   ```

   Expected: `OK: all smoke checks passed` within ~5 seconds.

5. **Return to main (so future commits flow normally):**

   ```bash
   git checkout main
   ```

   (Working tree is back on main but deployed version is still the rolled-back
   one — that's fine. The next intentional `deploy` will push main again.)

## Investigation: why did it fail?

After rollback, the bad SHA still exists in `main` history. Reproduce locally
on the original branch:

```bash
git checkout <bad-SHA>
# repro
```

Fix on a new commit and re-deploy.

## Drill verification

This procedure was verified on YYYY-MM-DD by rolling from `<test-SHA>` to
`<test-SHA>~1` and confirming /api/health responded within 10 minutes total.

(Update the date and SHAs above whenever you re-run the drill — at minimum
once per quarter or before any high-risk merge.)
