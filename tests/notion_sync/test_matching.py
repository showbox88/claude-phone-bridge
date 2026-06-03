"""Fuzzy match tests."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from notion_sync.matching import (
    normalize_title,
    bigram_jaccard,
    best_match,
)


def test_normalize_title_lowercases_and_strips():
    assert normalize_title("  Trip to Paris  ") == "trip to paris"
    assert normalize_title("Trip-to-Paris!") == "trip to paris"


def test_normalize_handles_chinese():
    assert normalize_title("巴黎旅行") == "巴黎旅行"


def test_bigram_jaccard_identical():
    assert bigram_jaccard("paris", "paris") == 1.0


def test_bigram_jaccard_similar():
    s = bigram_jaccard("trip to paris", "trip to parris")
    assert 0.7 < s < 1.0


def test_bigram_jaccard_unrelated():
    assert bigram_jaccard("paris", "tokyo") < 0.1


def test_best_match_exact():
    candidates = [
        {"id": "a", "title": "Trip to Paris", "date": "2026-06-15"},
        {"id": "b", "title": "Trip to Tokyo", "date": "2026-07-01"},
    ]
    target = {"title": "Trip to Paris", "date": "2026-06-15"}
    m = best_match(target, candidates, title_key="title", date_key="date")
    assert m.record["id"] == "a"
    assert m.score >= 0.95


def test_best_match_fuzzy_title_same_date():
    candidates = [
        {"id": "a", "title": "Trip to Paris!", "date": "2026-06-15"},
    ]
    target = {"title": "Trip to Paris", "date": "2026-06-15"}
    m = best_match(target, candidates, title_key="title", date_key="date")
    assert m.record["id"] == "a"
    assert m.score >= 0.85


def test_best_match_different_date_penalized():
    candidates = [
        {"id": "a", "title": "Trip to Paris", "date": "2026-06-15"},
        {"id": "b", "title": "Trip to Paris", "date": "2027-01-01"},
    ]
    target = {"title": "Trip to Paris", "date": "2026-06-15"}
    m = best_match(target, candidates, title_key="title", date_key="date")
    assert m.record["id"] == "a"


def test_best_match_no_candidates():
    m = best_match({"title": "X", "date": ""}, [], title_key="title", date_key="date")
    assert m is None


def test_best_match_below_threshold():
    candidates = [{"id": "a", "title": "Unrelated thing", "date": ""}]
    target = {"title": "Completely different", "date": ""}
    m = best_match(target, candidates, title_key="title", date_key="date",
                   min_score=0.6)
    assert m is None or m.score < 0.6


def test_best_match_date_format_difference_does_not_penalize():
    # Notion gives 'YYYY-MM-DD'; PB gives 'YYYY-MM-DD HH:MM:SS.SSSZ'.
    # Same calendar date — must NOT trigger the ×0.5 penalty.
    candidates = [{"id": "a", "title": "Trip", "date": "2026-05-31 00:00:00.000Z"}]
    target = {"title": "Trip", "date": "2026-05-31"}
    m = best_match(target, candidates, title_key="title", date_key="date")
    assert m.record["id"] == "a"
    assert m.score >= 0.95


def test_best_match_iso_with_t_separator_normalizes():
    # Notion datetimes like '2026-05-27T11:00:00.000+00:00' vs PB
    # '2026-05-27 11:00:00.000Z' — both normalize to '2026-05-27'.
    candidates = [{"id": "a", "title": "Workout", "date": "2026-05-27 11:00:00.000Z"}]
    target = {"title": "Workout", "date": "2026-05-27T11:00:00.000+00:00"}
    m = best_match(target, candidates, title_key="title", date_key="date")
    assert m.record["id"] == "a"
    assert m.score >= 0.95
