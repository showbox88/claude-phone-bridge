"""Fuzzy matching for the initial reconcile.

Strategy:
  1. Normalize titles (lowercase, strip non-alnum-and-CJK, collapse whitespace).
  2. Character-bigram Jaccard similarity.
  3. Date weighting: same date → +0.1; differing dates → ×0.5.
  4. Caller picks thresholds (auto-link >= 0.95, queue >= 0.60).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any


@dataclass
class Match:
    record: dict
    score: float


def normalize_title(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.lower()
    # Preserve word chars (Unicode \w covers Latin + CJK + Korean etc.).
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _bigrams(s: str) -> set[str]:
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def bigram_jaccard(a: str, b: str) -> float:
    a, b = normalize_title(a), normalize_title(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / len(ba | bb)


def best_match(target: dict, candidates: list[dict], *,
               title_key: str,
               date_key: str = "",
               min_score: float = 0.0) -> Match | None:
    if not candidates:
        return None

    target_title = str(target.get(title_key) or "")
    target_date = str(target.get(date_key) or "") if date_key else ""

    best: Match | None = None
    for c in candidates:
        title_score = bigram_jaccard(target_title, str(c.get(title_key) or ""))
        score = title_score
        if date_key:
            c_date = str(c.get(date_key) or "")
            if target_date and c_date:
                if target_date == c_date:
                    score = min(1.0, score + 0.1)
                else:
                    score *= 0.5
        if best is None or score > best.score:
            best = Match(record=c, score=score)

    if best is None or best.score < min_score:
        return None
    return best
