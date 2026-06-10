#!/usr/bin/env python3
"""Thin shim — Phase 5 Task 14 split runner.py into bootstrap/decisions/
dispatch/post_phases. Preserves the public surface so:
- systemd unit `python -m notion_sync.runner` works
- scripts/reconcile_initial.py `from notion_sync.runner import sync_collection` resolves
- tests' `from notion_sync.runner import should_run_now` resolves

Phase 6 cleanup: callers should migrate to direct imports from
notion_sync.bootstrap / .dispatch / etc., then this shim can shrink.
"""
from notion_sync.bootstrap import (  # noqa: F401
    main,
    should_run_now,
    now_iso_date,
    now_iso_datetime,
)
from notion_sync.decisions import (  # noqa: F401
    apply_pending_decisions,
    _apply_one_decision,
)
from notion_sync.dispatch import (  # noqa: F401
    sync_collection,
    _action_ids,
    _ACTION_ID_GETTERS,
    ACTION_HANDLERS,
)
from notion_sync.post_phases import (  # noqa: F401
    cleanup_resolved_activity,
    notify_pending,
)

if __name__ == "__main__":
    raise SystemExit(main())
