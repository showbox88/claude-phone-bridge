"""Notion ↔ PocketBase sync package.

PR1 contents: pb_api, notion_api, codec, matching, backup, activity,
              transform (shared by reconcile_initial + runner).
PR2 contents: changeset, logger, runner — daily cron sync runner.
PR3 adds: MCP tools + push notifier + Sync Activity decision applier.
"""
