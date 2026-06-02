"""Snapshot every PB base-collection to a timestamped folder.

Used before destructive operations (reconcile_initial, eventually PR3's
'Delete both' decisions). Notion has no equivalent because Notion's API
can't trigger a workspace backup — we accept that asymmetry by never doing
destructive Notion writes without a Sync Activity entry first.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def backup_collections(pb, root: Path) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = Path(root) / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    for c in pb.list_collections():
        if c.get("type") != "base":
            continue
        rows = pb.list_records(c["name"])
        path = out_dir / f"{c['name']}.json"
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    return out_dir
