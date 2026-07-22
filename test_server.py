"""Golden tests for server-side Prompt Studio maintenance logic.

Currently pins _import_library_into_saved's dedup key: it must hash the text AS
STORED (after the 2000-char cap). Hashing the full library text made every
>2000-char prompt permanently "new" — its stored truncation hashes differently,
so every sync re-imported and re-tagged the same prompts, one duplicate copy per
sync (observed in production: 6 prompts x ~88 syncs = 525 junk records). Also
pins the self-heal: exact-duplicate records collapse (first copy kept, tags
unioned) on the next import pass.

Run: python test_server.py
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp())
os.environ["GROK_DATA_DIR"] = str(_tmp)  # must be set before importing server

import server  # noqa: E402


def _write_state(saved: list | None) -> None:
    long_text = "a detailed cinematic prompt " + "x" * 2100  # survives normalize, >2000 chars
    meta = [
        {"id": "m1", "media_type": "video", "prompt": "a short prompt",
         "created_at": "2026-07-01", "local_path": "media/videos/aa/m1.mp4"},
        {"id": "m2", "media_type": "video", "prompt": long_text,
         "created_at": "2026-07-02", "local_path": "media/videos/aa/m2.mp4"},
    ]
    server.METADATA_FILE.write_text(json.dumps(meta), encoding="utf-8")
    if saved is None:
        server.RESPONSES_FILE.unlink(missing_ok=True)
    else:
        server.RESPONSES_FILE.write_text(json.dumps(saved), encoding="utf-8")
    return long_text


def test_long_prompt_imports_exactly_once():
    _write_state(saved=None)
    merged, new1, _ = server._import_library_into_saved()
    assert len(new1) == 2, [(x["text"][:30]) for x in new1]
    assert all(len(x["text"]) <= 2000 for x in merged)
    # The bug: this second pass used to find the >2000-char prompt "missing" again.
    merged2, new2, _ = server._import_library_into_saved()
    assert new2 == [], [x["text"][:40] for x in new2]
    assert len(merged2) == len(merged)
    print("  import: >2000-char prompt imported once, stable on re-run OK")


def test_duplicate_pile_collapses_and_merges_tags():
    long_text = _write_state(saved=None)
    stored = long_text[:2000]
    saved = [
        {"id": "rs-keep", "text": stored, "folder": "Library", "tags": ["cinematic"]},
        {"id": "rs-dup1", "text": stored, "folder": "Library", "tags": ["blonde", "cinematic"]},
        {"id": "rs-dup2", "text": stored, "folder": "Library", "tags": [], "starred": True},
        {"id": "rs-other", "text": "an unrelated saved prompt", "folder": "Faves", "tags": ["keep-me"]},
    ]
    _write_state(saved=saved)
    merged, new, _ = server._import_library_into_saved()
    by_id = {x["id"]: x for x in merged}
    assert "rs-dup1" not in by_id and "rs-dup2" not in by_id, sorted(by_id)
    kept = by_id["rs-keep"]
    assert kept["tags"] == ["cinematic", "blonde"], kept["tags"]  # union, first-copy order
    assert kept.get("starred") is True
    assert by_id["rs-other"]["tags"] == ["keep-me"]  # untouched
    # Only the short library prompt was actually missing.
    assert [x["text"] for x in new] == ["a short prompt"], [x["text"][:30] for x in new]
    assert len(merged) == 3, len(merged)
    print("  heal: 3 duplicate copies -> 1 (tags unioned, star kept), others intact OK")


if __name__ == "__main__":
    print("server prompt-import golden tests")
    test_long_prompt_imports_exactly_once()
    test_duplicate_pile_collapses_and_merges_tags()
    print("all passed")
