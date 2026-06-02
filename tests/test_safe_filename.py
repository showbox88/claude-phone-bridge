"""Tests for server._safe_filename. Run: python tests/test_safe_filename.py"""
import sys
from pathlib import Path

# Make `server` importable when running this file directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import _safe_filename


def test_plain_ascii_name_preserved():
    assert _safe_filename("vacation.jpg") == "vacation.jpg"


def test_chinese_name_preserved():
    assert _safe_filename("照片.jpg") == "照片.jpg"


def test_spaces_preserved():
    assert _safe_filename("my photo.jpg") == "my photo.jpg"


def test_emoji_preserved():
    assert _safe_filename("trip 🌴.jpg") == "trip 🌴.jpg"


def test_forward_slash_stripped_to_basename():
    assert _safe_filename("foo/bar/baz.jpg") == "baz.jpg"


def test_backslash_stripped_to_basename():
    assert _safe_filename("C:\\evil\\path.jpg") == "path.jpg"


def test_null_byte_removed():
    assert _safe_filename("a\x00b.jpg") == "ab.jpg"


def test_control_chars_removed():
    assert _safe_filename("a\x01b\x1fc.jpg") == "abc.jpg"


def test_leading_dot_stripped():
    assert _safe_filename(".hidden.jpg") == "hidden.jpg"


def test_multiple_leading_dots_stripped():
    assert _safe_filename("...hidden.jpg") == "hidden.jpg"


def test_empty_string_falls_back():
    assert _safe_filename("") == "upload.bin"


def test_only_dots_falls_back():
    assert _safe_filename("...") == "upload.bin"


def test_only_path_separators_falls_back():
    assert _safe_filename("///") == "upload.bin"


def test_long_name_truncated_to_200():
    long = "a" * 500 + ".jpg"
    result = _safe_filename(long)
    assert len(result) == 200
    assert result.startswith("a")


def test_dot_inside_name_preserved():
    assert _safe_filename("my.report.v2.pdf") == "my.report.v2.pdf"


if __name__ == "__main__":
    tests = [
        (name, fn) for name, fn in globals().items()
        if name.startswith("test_") and callable(fn)
    ]
    failures = []
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as e:
            failures.append(name)
            print(f"FAIL  {name}: {e}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
