"""Phase 2 baseline / after recorder + comparator.

Usage (recording, requires running server with BRIDGE_RECORD=1):
    BRIDGE_RECORD=1 BRIDGE_RECORD_PATH=tests/fixtures/phase2_baseline.jsonl \
        python -m uvicorn server:app ...

Usage (comparing):
    python tests/replay.py diff \
        tests/fixtures/phase2_baseline.jsonl \
        tests/fixtures/phase2_after.jsonl

Records JSONL: one object per HTTP req/resp, WS open/close, WS frame.
Comparator normalizes random fields (session_id, cb_id, ISO timestamps,
cost_usd, duration_ms, token counts) via first-occurrence remap, then
byte-diffs each record.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


_ISO_TS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?"
)
_HEX_UUID_RE = re.compile(r"^[0-9a-f]{15,}$")
_PATH_HEX_RE = re.compile(r"/([0-9a-f]{15,})(?=/|$|\?)")


def _normalize_path(value: str, remap: dict, counters: dict) -> str:
    def _sub(m: re.Match) -> str:
        hexid = m.group(1)
        if hexid not in remap:
            counters["sid"] = counters.get("sid", 0) + 1
            remap[hexid] = f"<sid_{counters['sid']}>"
        return "/" + remap[hexid]
    return _PATH_HEX_RE.sub(_sub, value)


def _normalize(obj: Any, remap: dict[str, str], counters: dict[str, int]) -> Any:
    if isinstance(obj, dict):
        return {k: _normalize_value(k, v, remap, counters) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize(v, remap, counters) for v in obj]
    return obj


def _normalize_value(key: str, value: Any, remap: dict[str, str],
                     counters: dict[str, int]) -> Any:
    if key == "path" and isinstance(value, str):
        return _normalize_path(value, remap, counters)
    if key in {"session_id", "sdk_session_id", "id", "cb_id"} and isinstance(value, str):
        if value and _HEX_UUID_RE.match(value):
            tag = "sid" if "session" in key or key == "id" else "cb"
            if value not in remap:
                counters[tag] = counters.get(tag, 0) + 1
                remap[value] = f"<{tag}_{counters[tag]}>"
            return remap[value]
    if key in {"notion_id", "notion_db_id"} and isinstance(value, str) and value:
        if value not in remap:
            counters["nid"] = counters.get("nid", 0) + 1
            remap[value] = f"<nid_{counters['nid']}>"
        return remap[value]
    if isinstance(value, str) and _ISO_TS_RE.search(value):
        return _ISO_TS_RE.sub("<TS>", value)
    if key in {"cost_usd", "duration_ms", "duration_api_ms",
              "input_tokens", "output_tokens", "cache_creation_input_tokens",
              "cache_read_input_tokens", "num_turns"}:
        return "<NUM>"
    if key in {"key", "vapid_public_key"} and isinstance(value, str) and len(value) > 40:
        return "<VAPID>"
    return _normalize(value, remap, counters)


class Recorder:
    """Single-process JSONL append-only recorder."""
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")
        self._seq = 0

    def _emit(self, rec: dict[str, Any]) -> None:
        self._seq += 1
        rec["seq"] = self._seq
        self._fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()

    def http(self, method: str, path: str, query: str,
             req_body: bytes | None, status: int, resp_body: bytes) -> None:
        def _body(b: bytes | None) -> dict:
            if b is None or len(b) == 0:
                return {"len": 0}
            if len(b) > 4096:
                return {"len": len(b), "sha256": hashlib.sha256(b).hexdigest()}
            try:
                return {"json": json.loads(b)}
            except (json.JSONDecodeError, ValueError):
                return {"len": len(b), "sha256": hashlib.sha256(b).hexdigest()}
        self._emit({
            "kind": "http",
            "req": {"method": method, "path": path, "query": query,
                    "body": _body(req_body)},
            "resp": {"status": status, "body": _body(resp_body)},
        })

    def ws_open(self) -> None:
        self._emit({"kind": "ws_open"})

    def ws_close(self, code: int | None) -> None:
        self._emit({"kind": "ws_close", "code": code})

    def ws_frame(self, direction: str, frame_text: str) -> None:
        try:
            frame = json.loads(frame_text)
        except (json.JSONDecodeError, ValueError):
            frame = {"_raw": frame_text[:500]}
        self._emit({"kind": "ws", "dir": direction, "frame": frame})


def diff(baseline_path: str, after_path: str) -> int:
    base_records = [json.loads(l) for l in Path(baseline_path).read_text(encoding="utf-8").splitlines() if l.strip()]
    after_records = [json.loads(l) for l in Path(after_path).read_text(encoding="utf-8").splitlines() if l.strip()]

    if len(base_records) != len(after_records):
        print(f"FAIL: record count differs - baseline={len(base_records)} after={len(after_records)}")
        return 1

    base_remap: dict[str, str] = {}
    after_remap: dict[str, str] = {}
    base_counts: dict[str, int] = {}
    after_counts: dict[str, int] = {}

    fails = 0
    for i, (b, a) in enumerate(zip(base_records, after_records), 1):
        b_norm = _normalize(b, base_remap, base_counts)
        a_norm = _normalize(a, after_remap, after_counts)
        for d in (b_norm, a_norm):
            d.pop("seq", None)
        if b_norm != a_norm:
            fails += 1
            print(f"FAIL record #{i}:")
            print(f"  baseline: {json.dumps(b_norm, sort_keys=True)[:300]}")
            print(f"  after   : {json.dumps(a_norm, sort_keys=True)[:300]}")
            if fails >= 10:
                print("(stopping after 10 mismatches)")
                break

    if fails == 0:
        print(f"OK: {len(base_records)} records match after normalization")
        return 0
    print(f"FAIL: {fails} record(s) differ")
    return 1


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in {"diff"}:
        print("usage: python tests/replay.py diff <baseline.jsonl> <after.jsonl>")
        sys.exit(2)
    sys.exit(diff(sys.argv[2], sys.argv[3]))
