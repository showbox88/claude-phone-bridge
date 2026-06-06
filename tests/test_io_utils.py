"""Tests for app.io_utils — atomic JSON write + safe read.
Run directly: python tests/test_io_utils.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.io_utils import read_json_safe, write_json_atomic


def test_write_json_atomic_creates_file():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "out.json"
        write_json_atomic(path, {"k": "v"})
        assert json.loads(path.read_text(encoding="utf-8")) == {"k": "v"}


def test_write_json_atomic_overwrites():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "out.json"
        write_json_atomic(path, {"a": 1})
        write_json_atomic(path, {"b": 2})
        assert json.loads(path.read_text(encoding="utf-8")) == {"b": 2}


def test_write_json_atomic_no_leftover_tmp():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "out.json"
        path.write_text('{"old": true}', encoding="utf-8")
        write_json_atomic(path, {"new": True})
        siblings = sorted(p.name for p in Path(d).iterdir())
        assert siblings == ["out.json"], f"unexpected leftovers: {siblings}"


def test_write_json_atomic_unicode():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "out.json"
        write_json_atomic(path, {"city": "Tokyo", "emoji": "tree"})
        decoded = json.loads(path.read_text(encoding="utf-8"))
        assert decoded == {"city": "Tokyo", "emoji": "tree"}


def test_write_json_atomic_creates_parent_dir():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "sub" / "nested" / "out.json"
        write_json_atomic(path, {"k": "v"})
        assert json.loads(path.read_text(encoding="utf-8")) == {"k": "v"}


def test_read_json_safe_default_when_missing():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "nope.json"
        assert read_json_safe(path, default={"x": 1}) == {"x": 1}


def test_read_json_safe_returns_parsed():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "exists.json"
        path.write_text('{"y": 2}', encoding="utf-8")
        assert read_json_safe(path, default={}) == {"y": 2}


def test_read_json_safe_default_on_corrupt():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "corrupt.json"
        path.write_text("not-json", encoding="utf-8")
        assert read_json_safe(path, default={"safe": True}) == {"safe": True}


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  OK  {t.__name__}")
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR  {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
