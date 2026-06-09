"""Verify the frontend module manifest stays consistent.

Phase 4 introduces 25+ ES modules under static/js/. This test catches:
- index.html references a file that doesn't exist
- An ES module imports another module that's missing from disk
- DOMPurify is referenced but the vendor file is missing
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
JS_ROOT = STATIC / "js"


def _index_html_text() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


def _list_js_modules() -> list[Path]:
    if not JS_ROOT.exists():
        return []
    return sorted(JS_ROOT.rglob("*.js"))


def test_index_html_references_exist():
    """Every src=/static/... and href=/static/... in index.html points to a real file."""
    html = _index_html_text()
    refs = set()
    for m in re.finditer(r'(?:src|href)="(/static/[^"?]+)', html):
        refs.add(m.group(1))
    missing = []
    for ref in refs:
        rel = ref.removeprefix("/static/")
        if not (STATIC / rel).exists():
            missing.append(ref)
    assert not missing, f"index.html references non-existent files: {missing}"


def test_no_imports_to_missing_modules():
    """Every `from './x.js'` or `from '../foo/y.js'` resolves to a real file."""
    missing = []
    for mod in _list_js_modules():
        text = mod.read_text(encoding="utf-8")
        for m in re.finditer(r'from\s+[\'"](\.{1,2}/[^\'"]+)[\'"]', text):
            rel = m.group(1)
            target = (mod.parent / rel).resolve()
            if not target.exists():
                missing.append((str(mod.relative_to(STATIC)), rel))
    assert not missing, f"Imports to missing modules: {missing}"


def test_purify_present_if_referenced():
    """If index.html references purify.min.js, it must be on disk."""
    html = _index_html_text()
    if "/static/vendor/purify.min.js" in html:
        assert (STATIC / "vendor" / "purify.min.js").exists(), (
            "index.html references purify.min.js but file is missing")
