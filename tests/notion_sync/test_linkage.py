"""Tests for date-based Notion linkage. No real Notion calls."""
import pytest
from notion_sync import linkage


class FakeNotion:
    """Stand-in for NotionClient. Stores pages by id and lets us replay queries."""
    def __init__(self, pages_by_db):
        self.pages_by_db = pages_by_db          # db_id -> list of page dicts
        self.patches: list = []
    def query_database(self, db_id, *args, **kwargs):
        return list(self.pages_by_db.get(db_id, []))
    def update_page(self, page_id, properties=None, archived=None):
        self.patches.append((page_id, properties))
        # Apply patch into the in-memory store so a second pass sees fresh state
        for db in self.pages_by_db.values():
            for p in db:
                if p["id"] == page_id:
                    p.setdefault("properties", {}).update(properties or {})
                    return p


def _page(pid, *, date=None, dates_start=None, dates_end=None,
           day_rel=None, trip_rel=None):
    """Build a fake Notion page dict with the given properties."""
    props = {}
    if date:
        props["Date"] = {"date": {"start": date}}
    if dates_start:
        props["Dates"] = {"date": {"start": dates_start, "end": dates_end}}
    if day_rel is not None:
        props["Day"] = {"relation":
                         [{"id": day_rel}] if day_rel else []}
    if trip_rel is not None:
        props["Trip"] = {"relation":
                          [{"id": trip_rel}] if trip_rel else []}
    return {"id": pid, "properties": props}


def test_stop_with_matching_day_gets_linked():
    days  = [_page("day-A", date="2026-06-04")]
    stops = [_page("stop-1", date="2026-06-04", day_rel="")]
    nc = FakeNotion({"days-db": days, "stops-db": stops, "trips-db": []})
    counts = linkage.update_date_linkages(nc,
        days_db_id="days-db", stops_db_id="stops-db", trips_db_id="trips-db")
    assert counts["stops_patched"] == 1
    assert nc.patches[0][0] == "stop-1"
    assert nc.patches[0][1]["Day"]["relation"] == [{"id": "day-A"}]


def test_stop_already_correctly_linked_is_not_patched():
    days  = [_page("day-A", date="2026-06-04")]
    stops = [_page("stop-1", date="2026-06-04", day_rel="day-A", trip_rel="")]
    nc = FakeNotion({"days-db": days, "stops-db": stops, "trips-db": []})
    counts = linkage.update_date_linkages(nc,
        days_db_id="days-db", stops_db_id="stops-db", trips_db_id="trips-db")
    assert counts["stops_patched"] == 0
    assert counts["no_change_stops"] == 1
    assert nc.patches == []


def test_stop_with_no_matching_day_clears_link():
    days  = [_page("day-A", date="2026-06-03")]  # different date
    stops = [_page("stop-1", date="2026-06-04", day_rel="day-X-stale")]
    nc = FakeNotion({"days-db": days, "stops-db": stops, "trips-db": []})
    counts = linkage.update_date_linkages(nc,
        days_db_id="days-db", stops_db_id="stops-db", trips_db_id="trips-db")
    assert counts["stops_patched"] == 1
    assert nc.patches[0][1]["Day"]["relation"] == []


def test_stop_in_trip_range_gets_trip_linked():
    trips = [_page("trip-A", dates_start="2026-06-01", dates_end="2026-06-30")]
    stops = [_page("stop-1", date="2026-06-15", trip_rel="")]
    nc = FakeNotion({"days-db": [], "stops-db": stops, "trips-db": trips})
    counts = linkage.update_date_linkages(nc,
        days_db_id="days-db", stops_db_id="stops-db", trips_db_id="trips-db")
    assert counts["stops_patched"] == 1
    assert nc.patches[0][1]["Trip"]["relation"] == [{"id": "trip-A"}]


def test_stop_outside_trip_range_no_link():
    trips = [_page("trip-A", dates_start="2026-06-01", dates_end="2026-06-10")]
    stops = [_page("stop-1", date="2026-06-15", trip_rel="")]
    nc = FakeNotion({"days-db": [], "stops-db": stops, "trips-db": trips})
    counts = linkage.update_date_linkages(nc,
        days_db_id="days-db", stops_db_id="stops-db", trips_db_id="trips-db")
    assert counts["stops_patched"] == 0
    assert counts["no_change_stops"] == 1


def test_day_in_trip_range_gets_trip_linked():
    trips = [_page("trip-A", dates_start="2026-06-01", dates_end="2026-06-30")]
    days  = [_page("day-A", date="2026-06-15", trip_rel="")]
    nc = FakeNotion({"days-db": days, "stops-db": [], "trips-db": trips})
    counts = linkage.update_date_linkages(nc,
        days_db_id="days-db", stops_db_id="stops-db", trips_db_id="trips-db")
    assert counts["days_patched"] == 1
    assert nc.patches[0][1]["Trip"]["relation"] == [{"id": "trip-A"}]


def test_trip_with_single_day_range_matches_only_that_day():
    """A trip with start=end (single-day trip): only that date matches."""
    trips = [_page("trip-A", dates_start="2026-06-15", dates_end="2026-06-15")]
    stops = [_page("stop-1", date="2026-06-15", trip_rel=""),
             _page("stop-2", date="2026-06-16", trip_rel="")]
    nc = FakeNotion({"days-db": [], "stops-db": stops, "trips-db": trips})
    counts = linkage.update_date_linkages(nc,
        days_db_id="days-db", stops_db_id="stops-db", trips_db_id="trips-db")
    assert counts["stops_patched"] == 1
    assert nc.patches[0][0] == "stop-1"


def test_idempotent_second_run_no_patches():
    days  = [_page("day-A", date="2026-06-04")]
    stops = [_page("stop-1", date="2026-06-04", day_rel="", trip_rel="")]
    nc = FakeNotion({"days-db": days, "stops-db": stops, "trips-db": []})
    linkage.update_date_linkages(nc,
        days_db_id="days-db", stops_db_id="stops-db", trips_db_id="trips-db")
    nc.patches = []  # reset
    counts = linkage.update_date_linkages(nc,
        days_db_id="days-db", stops_db_id="stops-db", trips_db_id="trips-db")
    assert counts["stops_patched"] == 0
    assert nc.patches == []


def test_stop_with_no_date_is_skipped():
    days = [_page("day-A", date="2026-06-04")]
    stops = [{"id": "stop-1", "properties": {}}]   # no Date prop at all
    nc = FakeNotion({"days-db": days, "stops-db": stops, "trips-db": []})
    counts = linkage.update_date_linkages(nc,
        days_db_id="days-db", stops_db_id="stops-db", trips_db_id="trips-db")
    assert counts["stops_patched"] == 0
    assert counts["no_change_stops"] == 0   # skipped, not counted
