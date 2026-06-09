"""Compute and apply date-based relations between synced Notion DBs.

Pure Notion-side derivation, independent of PB relation field values:

  Stop.Day  = day where day.Date == stop.Date
  Stop.Trip = trip where trip.Dates.start <= stop.Date <= trip.Dates.end
  Day.Trip  = trip where trip.Dates.start <= day.Date <= trip.Dates.end

Notion's dual_property handles inverse columns (Days.Stops, Trips.Stops,
Trips.Days) automatically.

PATCHes only when the current value differs from the computed target.
Idempotent — running twice with no data changes produces zero PATCHes.
"""
from __future__ import annotations

from notion_sync.notion_api import NotionClient


def _date_scalar(page: dict, prop_name: str) -> str:
    """Extract a YYYY-MM-DD date from a Notion `date`-type property.
    Returns "" if missing. Strips any time component if present."""
    p = page.get("properties", {}).get(prop_name, {})
    d = p.get("date") or {}
    start = (d.get("start") or "").strip()
    # Strip time if present (Notion uses YYYY-MM-DD or full ISO)
    return start[:10] if len(start) >= 10 else ""


def _date_range(page: dict, prop_name: str) -> tuple[str, str]:
    """Extract (start_date, end_date) from a Notion date range property.
    end defaults to start when null (single-day trip)."""
    p = page.get("properties", {}).get(prop_name, {})
    d = p.get("date") or {}
    start = (d.get("start") or "").strip()[:10]
    end_raw = d.get("end")
    end = (end_raw or "").strip()[:10] if end_raw else start
    return start, end


def _relation_id(page: dict, prop_name: str) -> str:
    """Return the single related page id, or "" if empty/multi."""
    p = page.get("properties", {}).get(prop_name, {})
    rels = p.get("relation") or []
    return rels[0]["id"] if rels else ""


def _find_trip(date: str, trips_index: list) -> str:
    """Find the first trip whose range contains `date`. Returns trip id or "".

    `trips_index` is a list of (start, end, trip_id) tuples, NOT sorted
    in any meaningful order — we just linear-scan. If multiple trips
    overlap, the first match wins (caller responsibility to keep date
    ranges non-overlapping; document this in the linkage report).
    """
    for start, end, tid in trips_index:
        if start and end and start <= date <= end:
            return tid
    return ""


def _col(overrides: dict | None, pb_field: str, default: str) -> str:
    """Resolve a Notion column name from a sync_config field_map_overrides
    dict. Falls back to the supplied legacy default when the override is
    missing — keeps behaviour identical for collections that don't rename
    columns."""
    if overrides and pb_field in overrides:
        return overrides[pb_field]
    return default


def update_date_linkages(
    nc: NotionClient,
    *,
    days_db_id: str,
    stops_db_id: str,
    trips_db_id: str,
    days_overrides: dict | None = None,
    stops_overrides: dict | None = None,
    trips_overrides: dict | None = None,
) -> dict[str, int]:
    """Recompute Day↔Stops, Day↔Trip, Stop↔Trip linkages by date.

    Column names are resolved via the per-collection ``*_overrides`` dicts
    (PB field name → Notion column name). When an override is absent the
    legacy default (`Date` / `Day` / `Trip` / `Dates`) is used, so existing
    sync targets keep working unchanged.

    Returns counters: {stops_patched, days_patched, no_change_stops,
    no_change_days}. PATCHes only where target differs from current.
    """
    days_date_col  = _col(days_overrides,  "date", "Date")
    stops_date_col = _col(stops_overrides, "date", "Date")
    stops_day_col  = _col(stops_overrides, "day",  "Day")
    stops_trip_col = _col(stops_overrides, "trip", "Trip")
    days_trip_col  = _col(days_overrides,  "trip", "Trip")
    # Trips' range column is "Dates" in Notion but PB stores it as
    # date_start/date_end (not round-tripped via codec); allow override
    # under the lookup key "dates".
    trips_range_col = _col(trips_overrides, "dates", "Dates")

    days  = nc.query_database(days_db_id)
    stops = nc.query_database(stops_db_id)
    trips = nc.query_database(trips_db_id)

    day_id_by_date: dict[str, str] = {}
    for d in days:
        dd = _date_scalar(d, days_date_col)
        if dd:
            day_id_by_date.setdefault(dd, d["id"])

    trips_index = []
    for t in trips:
        s, e = _date_range(t, trips_range_col)
        if s:
            trips_index.append((s, e, t["id"]))

    counts = {
        "stops_patched": 0, "no_change_stops": 0,
        "days_patched": 0,  "no_change_days":  0,
    }

    # Patch stops: Day + Trip
    for s in stops:
        s_date = _date_scalar(s, stops_date_col)
        if not s_date:
            continue
        target_day  = day_id_by_date.get(s_date, "")
        target_trip = _find_trip(s_date, trips_index)
        current_day  = _relation_id(s, stops_day_col)
        current_trip = _relation_id(s, stops_trip_col)
        patch: dict = {}
        if current_day != target_day:
            patch[stops_day_col] = {"relation":
                             [{"id": target_day}] if target_day else []}
        if current_trip != target_trip:
            patch[stops_trip_col] = {"relation":
                              [{"id": target_trip}] if target_trip else []}
        if patch:
            nc.update_page(s["id"], properties=patch)
            counts["stops_patched"] += 1
        else:
            counts["no_change_stops"] += 1

    # Patch days: Trip
    for d in days:
        d_date = _date_scalar(d, days_date_col)
        if not d_date:
            continue
        target_trip = _find_trip(d_date, trips_index)
        current_trip = _relation_id(d, days_trip_col)
        if current_trip != target_trip:
            nc.update_page(d["id"], properties={
                days_trip_col: {"relation":
                          [{"id": target_trip}] if target_trip else []}
            })
            counts["days_patched"] += 1
        else:
            counts["no_change_days"] += 1

    return counts
