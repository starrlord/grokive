"""Golden tests for server-side Prompt Studio maintenance logic.

Currently pins saved Prompt Studio prompt length handling and
_import_library_into_saved's dedup key: it must hash the text AS STORED (after
the saved-prompt cap). Hashing the full library text made every over-limit
prompt permanently "new" — its stored truncation hashes differently, so every
sync re-imported and re-tagged the same prompts, one duplicate copy per sync
(observed in production: 6 prompts x ~88 syncs = 525 junk records). Also pins
the self-heal: exact-duplicate records collapse (first copy kept, tags unioned)
on the next import pass.

Run: python test_server.py
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp())
os.environ["GROK_DATA_DIR"] = str(_tmp)  # must be set before importing server
os.environ["AUTH_DISABLED"] = "true"

import server  # noqa: E402


def _write_state(saved: list | None) -> None:
    long_text = "a detailed cinematic prompt " + "x" * 2500  # survives normalize, old >2000 cap
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
    assert all(len(x["text"]) <= server.SAVED_PROMPT_TEXT_LIMIT for x in merged)
    assert any(len(x["text"]) > 2000 for x in merged)
    # The bug: this second pass used to find the over-limit prompt "missing" again.
    merged2, new2, _ = server._import_library_into_saved()
    assert new2 == [], [x["text"][:40] for x in new2]
    assert len(merged2) == len(merged)
    print("  import: >2000-char prompt imported once, stable on re-run OK")


def test_duplicate_pile_collapses_and_merges_tags():
    long_text = _write_state(saved=None)
    stored = long_text[:server.SAVED_PROMPT_TEXT_LIMIT]
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


def test_saved_response_text_cap_is_100k():
    _write_state(saved=[])
    long_text = "manual prompt " + "x" * 50_000
    too_long = "manual prompt " + "y" * (server.SAVED_PROMPT_TEXT_LIMIT + 50)
    with server.app.test_client() as client:
        r = client.post("/api/prompts/responses/add", json={"text": long_text})
        assert r.status_code == 200, r.get_data(as_text=True)
        data = r.get_json()
        assert data["responses"][0]["text"] == long_text

        r = client.post("/api/prompts/responses", json={"responses": [
            {"id": "rs-long", "text": too_long, "folder": "Manual", "tags": ["long"]}
        ]})
        assert r.status_code == 200, r.get_data(as_text=True)
        saved = json.loads(server.RESPONSES_FILE.read_text(encoding="utf-8"))
        by_id = {x["id"]: x for x in saved}
        assert len(by_id["rs-long"]["text"]) == server.SAVED_PROMPT_TEXT_LIMIT
        assert by_id["rs-long"]["text"] == too_long[:server.SAVED_PROMPT_TEXT_LIMIT]
    print("  save: 50k prompt round-trips, >100k prompt caps at 100k OK")


if __name__ == "__main__":
    print("server prompt-import golden tests")
    test_long_prompt_imports_exactly_once()
    test_duplicate_pile_collapses_and_merges_tags()
    test_saved_response_text_cap_is_100k()
    print("all passed")
