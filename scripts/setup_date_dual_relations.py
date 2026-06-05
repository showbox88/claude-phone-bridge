#!/usr/bin/env python3
"""One-time setup: convert Stops.Day and Stops.Trip to dual_property
so Days and Trips automatically gain inverse "Stops" columns.

Idempotent — running twice does not double-create columns. After running,
the linkage step in the runner populates the actual relation values
based on date matching.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notion_sync.notion_api import NotionClient
from notion_sync.pb_api import PBClient
from notion_sync.config import get


def main() -> int:
    nc = NotionClient()
    pb = PBClient()
    stops_target = get("stops", pb, fresh=True)
    days_target  = get("days",  pb, fresh=True)
    trips_target = get("trips", pb, fresh=True)
    if not (stops_target and days_target and trips_target):
        print("error: stops/days/trips sync_config rows must all exist", file=sys.stderr)
        return 1

    print(f"stops DB: {stops_target.notion_db_id}")
    print(f"days DB:  {days_target.notion_db_id}")
    print(f"trips DB: {trips_target.notion_db_id}")

    # Convert Stops.Day → dual_property, inverse on Days DB named "Stops"
    print("\n[1/2] Stops.Day → dual_property (inverse: Days.Stops)")
    nc.update_database(stops_target.notion_db_id, {
        "properties": {
            "Day": {
                "relation": {
                    "database_id": days_target.notion_db_id,
                    "type": "dual_property",
                    "dual_property": {
                        "synced_property_name": "Stops",
                    },
                },
            },
        },
    })
    print("  ok")

    # Convert Stops.Trip → dual_property, inverse on Trips DB named "Stops"
    print("\n[2/2] Stops.Trip → dual_property (inverse: Trips.Stops)")
    nc.update_database(stops_target.notion_db_id, {
        "properties": {
            "Trip": {
                "relation": {
                    "database_id": trips_target.notion_db_id,
                    "type": "dual_property",
                    "dual_property": {
                        "synced_property_name": "Stops",
                    },
                },
            },
        },
    })
    print("  ok")

    print("\nDone. Days DB now has a 'Stops' inverse column; Trips DB now has a 'Stops' inverse column.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
