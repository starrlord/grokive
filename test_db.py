"""Golden tests for db.build_index's dimension cache (no ffmpeg needed — the
probe functions are stubbed and counted).

The upscaled-video (_hd/1080) branch used to re-ffprobe EVERY such video on
EVERY rebuild (~50s per rebuild at 15k-library scale). It now trusts the
previous build's cached dims while the file's size_bytes is unchanged and only
re-probes when the file actually changed (an in-place upscale always changes
the size). These tests pin that: probe exactly once, zero on a no-change
rebuild, again after the file changes.

Run: python test_db.py
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import db


def _setup(tmp: Path):
    gallery = tmp / "gallery"
    (gallery / "media" / "videos" / "aa").mkdir(parents=True)
    hd = gallery / "media" / "videos" / "aa" / "vid_hd.mp4"
    norm = gallery / "media" / "videos" / "aa" / "vid_norm.mp4"
    hd.write_bytes(b"A" * 1000)
    norm.write_bytes(b"B" * 2000)
    items = [
        {  # upscaled-in-place candidate: _hd source name, no dims from Grok
            "id": "vid1", "media_type": "video", "prompt": "an upscaled clip",
            "source_url": "https://x/clip_hd.mp4",
            "local_path": "media/videos/aa/vid_hd.mp4",
        },
        {  # ordinary video, no dims from Grok -> probed once, cached thereafter
            "id": "vid2", "media_type": "video", "prompt": "a normal clip",
            "source_url": "https://x/clip.mp4",
            "local_path": "media/videos/aa/vid_norm.mp4",
        },
        {  # dims captured at download time -> never probed at all
            "id": "img1", "media_type": "image", "prompt": "a picture",
            "width": 1024, "height": 768,
            "local_path": "media/images/aa/img1.jpg",
        },
    ]
    meta = tmp / "metadata.json"
    meta.write_text(json.dumps(items), encoding="utf-8")
    return gallery, meta, hd


def _dims(dbfile: Path) -> dict[str, tuple]:
    conn = db._connect(dbfile)
    try:
        return {r["id"]: (r["media_w"], r["media_h"]) for r in
                conn.execute("SELECT id, media_w, media_h FROM media")}
    finally:
        conn.close()


def test_hd_reprobe_only_on_file_change():
    calls = {"video": 0, "media": 0}
    real_video_dims, real_media_dims = db._video_dims, db._media_dims

    def fake_video_dims(path):
        calls["video"] += 1
        return (1424, 1424) if calls["video"] == 1 else (2848, 2848)

    def fake_media_dims(media_type, path):
        calls["media"] += 1
        return (544, 544)

    db._video_dims, db._media_dims = fake_video_dims, fake_media_dims
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            gallery, meta, hd_file = _setup(tmp)
            dbfile = tmp / "index.db"

            # Build 1: cold cache — the _hd video and the dim-less normal video
            # are each probed exactly once; the image with metadata dims never is.
            assert db.build_index(dbfile, meta, gallery) == 3
            assert calls == {"video": 1, "media": 1}, calls
            d = _dims(dbfile)
            assert d["vid1"] == (1424, 1424) and d["vid2"] == (544, 544), d
            assert d["img1"] == (1024, 768), d

            # Build 2: nothing changed on disk — ZERO probes (this was the ~50s
            # per-rebuild ffprobe storm), dims carried forward from the cache.
            assert db.build_index(dbfile, meta, gallery) == 3
            assert calls == {"video": 1, "media": 1}, calls
            assert _dims(dbfile)["vid1"] == (1424, 1424)

            # Build 3: the _hd file was replaced (size changed — an in-place
            # upscale) — that one video is re-probed and its dims refresh.
            hd_file.write_bytes(b"A" * 5000)
            assert db.build_index(dbfile, meta, gallery) == 3
            assert calls == {"video": 2, "media": 1}, calls
            assert _dims(dbfile)["vid1"] == (2848, 2848)
    finally:
        db._video_dims, db._media_dims = real_video_dims, real_media_dims
    print("  dim cache: probe once cold, zero when unchanged, re-probe on file change OK")


def test_busy_timeout_set():
    with tempfile.TemporaryDirectory() as td:
        conn = db._connect(Path(td) / "t.db")
        try:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
        finally:
            conn.close()
    print("  busy_timeout: 10s on every connection OK")


if __name__ == "__main__":
    print("db index golden tests")
    test_hd_reprobe_only_on_file_change()
    test_busy_timeout_set()
    print("all passed")
