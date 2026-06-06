# Phone Bridge Rollback Drill

Use this when a refactor merged to `main` causes regressions in production
(`dashboard-server.tail4cfa2.ts.net`). The whole drill should take < 10 minutes
— in practice the fast path is **under 30 seconds** end-to-end.

## When to roll back

- `tests/smoke_backend.py` fails against prod
- `journalctl -u phone-bridge` shows new ERROR-level lines that weren't there before deploy
- Any user-facing flow stops working

## Two rollback mechanisms

`deploy` (`C:\Users\Showbox\bin\deploy.ps1`) keeps the previous 3 deploys on
the VM as `/home/dev/phone-bridge.bak.<TS>` directories. That gives you two
ways to recover, with different speed / reproducibility trade-offs.

### Fast path: swap to the latest backup (≈ 7 seconds)

Use this **first** when prod is broken — it's nearly instant and what
`deploy` itself uses for its auto-rollback when a health check fails.

```bash
ssh dashboard-server '
  set -e
  LATEST_BAK=$(ls -dt /home/dev/phone-bridge.bak.* 2>/dev/null | head -1)
  if [ -z "$LATEST_BAK" ]; then
    echo "no backup available"; exit 1
  fi
  sudo systemctl stop phone-bridge
  mv /home/dev/phone-bridge /home/dev/phone-bridge.failed.$(date +%Y%m%d-%H%M%S)
  mv "$LATEST_BAK" /home/dev/phone-bridge
  sudo systemctl start phone-bridge
  systemctl is-active phone-bridge
'
```

Then verify:

```bash
BASE=https://dashboard-server.tail4cfa2.ts.net \
  BRIDGE_COOKIE='bridge_session=...' \
  python tests/smoke_backend.py
```

Expected: `OK: all smoke checks passed` within ~5 seconds.

The bad deploy is preserved at `/home/dev/phone-bridge.failed.<TS>` for
investigation. Once you're done debugging, delete it manually.

### Reproducible path: redeploy a known-good SHA (≈ 14 seconds)

Use this when you want to pin prod to a specific commit (not "whichever
backup happens to exist").

1. **Identify the bad commit:**

   ```bash
   git log --oneline -10
   ```

   The most recent commit on `main` is usually the culprit. If unsure: look
   at `journalctl -u phone-bridge --since "2 hours ago"` for the first error
   timestamp, then `git log --until=<that timestamp>`.

2. **Check out the last-good SHA locally:**

   ```bash
   git checkout <last-good-SHA>
   ```

   Do NOT use `git reset --hard` — checkout leaves `main` intact so you can
   investigate and re-roll forward later.

3. **Deploy:**

   ```bash
   deploy
   ```

   Packages the local working tree → uploads → restarts `phone-bridge.service`
   → polls `/api/health` 5 times with 3s gap. Auto-rollback fires if health
   doesn't come up.

4. **Verify with smoke (same as fast path above).**

5. **Return to main when you're ready to roll forward:**

   ```bash
   git checkout main
   ```

   The deployed bytes stay on the rolled-back SHA; the next intentional
   `deploy` will push main again.

## Investigation: why did it fail?

After rollback, the bad SHA is still in `main` history. Reproduce locally on
the original branch:

```bash
git checkout <bad-SHA>
# repro
```

Fix on a new commit and re-deploy.

## Drill verification

Verified 2026-06-06 01:07~01:08 UTC by running the fast path against prod:

- Start: 01:07:12 — swapped current with `.bak.20260605-195946`
- 01:07:19 — smoke passed against rolled-back state (7s elapsed)
- 01:08:09 — re-deployed from `refactor/phase-minus1-guardrails` (HEAD `e73876a`)
- 01:08:23 — deploy + health check completed (14s deploy round-trip)
- 01:08:30 — final smoke passed

Total: 1 min 18 sec, two brief service restarts (~5s and ~10s),
nothing user-visible lost. Cleanup confirmed all `.bak.*` backups intact:

```
/home/dev/phone-bridge.bak.20260606-010809
/home/dev/phone-bridge.bak.20260605-155808
/home/dev/phone-bridge.bak.20260605-153541
```

(Re-run this drill at least once per quarter, or before any high-risk merge.
Update the dates and SHAs above each time.)
