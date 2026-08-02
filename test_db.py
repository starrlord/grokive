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


def _res_setup(tmp: Path) -> Path:
    """Index with mixed orientations (dims supplied in metadata, so nothing is
    probed): 3 landscape, 2 portrait, 1 square, 1 dimension-less video, 1 image,
    1 montage output — the last three must never count as pool material."""
    gallery = tmp / "gallery"
    (gallery / "media").mkdir(parents=True)
    items = []

    def _vid(mid, w=None, h=None, model="grok-video"):
        p = gallery / "media" / f"{mid}.mp4"
        p.write_bytes(b"x")
        it = {"id": mid, "media_type": "video", "prompt": mid, "model": model,
              "local_path": f"media/{mid}.mp4"}
        if w:
            it.update(width=w, height=h)
        items.append(it)

    _vid("land1", 1280, 720); _vid("land2", 1280, 720); _vid("land3", 1920, 1080)
    _vid("port1", 720, 1280); _vid("port2", 1080, 1920)
    _vid("sq1", 1080, 1080)
    _vid("montage", 1920, 1080, model="Beat Montage")
    (gallery / "media" / "img.jpg").write_bytes(b"x")
    items.append({"id": "img1", "media_type": "image", "prompt": "pic",
                  "width": 512, "height": 512, "local_path": "media/img.jpg"})
    meta = tmp / "metadata.json"
    meta.write_text(json.dumps(items), encoding="utf-8")
    dbfile = tmp / "index.db"
    # The dimension-less video would be ffprobed — stub the probe to "unknown".
    real = db._video_dims
    db._video_dims = lambda path: (None, None)
    try:
        _vid("nodims")
        meta.write_text(json.dumps(items), encoding="utf-8")
        db.build_index(dbfile, meta, gallery)
    finally:
        db._video_dims = real
    return dbfile


def test_video_resolution_stats_and_orientation_filter():
    with tempfile.TemporaryDirectory() as td:
        dbfile = _res_setup(Path(td))
        stats = db.video_resolution_stats(dbfile)
        by_dim = {(s["w"], s["h"]): s for s in stats}
        assert by_dim[(1280, 720)]["count"] == 2 and by_dim[(1280, 720)]["orientation"] == "landscape"
        assert by_dim[(720, 1280)]["orientation"] == "portrait"
        assert by_dim[(1080, 1080)]["orientation"] == "square"
        assert (1920, 1080) in by_dim and by_dim[(1920, 1080)]["count"] == 1, \
            "the montage output must not inflate the 1920x1080 bucket"
        assert by_dim[(None, None)]["count"] == 1, "dimension-less video reported as unknown"
        assert stats[0] == by_dim[(1280, 720)], "commonest size sorts first"
        # Restricted to an id list: order-independent counts, montage/image ignored.
        sub = db.video_resolution_stats(dbfile, ["port1", "montage", "img1", "land1"])
        assert {(s["w"], s["h"]): s["count"] for s in sub} == {(720, 1280): 1, (1280, 720): 1}
        # Orientation filter: order preserved, unknown dims never match.
        ids = ["land3", "nodims", "port2", "sq1", "port1", "montage"]
        assert db.filter_video_ids_by_orientation(dbfile, ids, "portrait") == ["port2", "port1"]
        assert db.filter_video_ids_by_orientation(dbfile, ids, "landscape") == ["land3"]
        assert db.filter_video_ids_by_orientation(dbfile, None, "square") == ["sq1"]
        assert db.filter_video_ids_by_orientation(dbfile, ids, "nonsense") == [str(i) for i in ids]
    print("  resolutions: histogram buckets/sorting, montage+image excluded, filter order-preserving OK")


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
    test_video_resolution_stats_and_orientation_filter()
    test_busy_timeout_set()
    print("all passed")
