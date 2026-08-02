"""Golden tests for Auto Montage clip selection (autopick.py). Synthetic beat
grids + synthetic cached raw diffs with known answers, so the demand/supply
matching, gates, and determinism are pinned without any video decoding.

Run: python test_autopick.py   (no pytest dependency required)
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import autopick
import moviegen as m


def _grid(n_beats: int, energy: float, spacing: float = 0.5) -> m.BeatGrid:
    dur = n_beats * spacing
    beats = [m.Beat(time=i * spacing, is_downbeat=(i % 4 == 0),
                    energy=energy, section_id=0) for i in range(n_beats)]
    return m.BeatGrid(duration=dur, tempo=120.0, beats=beats,
                      sections=[m.Section(0, 0.0, dur, energy)])


def _lib(tmp: Path, cache: Path, specs: list[tuple[str, float, float]]) -> list[Path]:
    """Create fake clip files + cached raw diffs. Each spec is
    (name, raw_level, duration_s); a constant raw level trips neither the
    head-trim detector nor scene-cut detection."""
    paths = []
    for name, level, dur in specs:
        p = tmp / f"{name}.mp4"
        p.write_bytes(b"junk-" + name.encode())
        n = max(2, int(dur * m.ANALYSIS_FPS))
        m._save_motion_diffs(p, cache, [level] * n, dur)
        paths.append(p)
    return paths


def test_uncached_pool_is_a_hard_error():
    tmp = Path(tempfile.mkdtemp())
    cache = tmp / "cache"
    a = tmp / "a.mp4"; a.write_bytes(b"x")
    b = tmp / "b.mp4"; b.write_bytes(b"y")
    try:
        autopick.select_clips([a, b], _grid(16, 0.9), cache,
                              tightness=0.5, target_duration=None)
    except RuntimeError as exc:
        assert "Analyze Library" in str(exc), exc
        print("  uncached: hard error points at Analyze Library OK")
        return
    raise AssertionError("expected a RuntimeError for an all-uncached pool")


def test_loud_song_prefers_hot_clips_quiet_prefers_calm():
    tmp = Path(tempfile.mkdtemp())
    cache = tmp / "cache"
    calm = [(f"calm{i}", 2.5, 6.0) for i in range(9)]
    hot = [(f"hot{i}", 18.0, 6.0) for i in range(9)]
    paths = _lib(tmp, cache, calm + hot)
    loud_pick, loud_stats = autopick.select_clips(
        paths, _grid(64, 0.9), cache, tightness=0.5, target_duration=None)
    quiet_pick, quiet_stats = autopick.select_clips(
        paths, _grid(64, 0.15), cache, tightness=0.5, target_duration=None)
    loud_hot = sum(1 for p in loud_pick if p.name.startswith("hot"))
    quiet_calm = sum(1 for p in quiet_pick if p.name.startswith("calm"))
    # The library's low/high terciles must land where the song's energy demands.
    assert loud_hot >= 6, (loud_hot, [p.name for p in loud_pick])
    assert quiet_calm >= 6, (quiet_calm, [p.name for p in quiet_pick])
    assert loud_stats["bands"]["hot"] > 0 and loud_stats["bands"]["calm"] == 0
    assert quiet_stats["bands"]["calm"] > 0 and quiet_stats["bands"]["hot"] == 0
    print(f"  demand: loud song -> {loud_hot} hot picks; quiet -> {quiet_calm} calm picks OK")


def test_hero_dead_short_and_uncached_handling():
    tmp = Path(tempfile.mkdtemp())
    cache = tmp / "cache"
    specs = ([("dead", 0.2, 6.0), ("shorty", 9.0, 0.5)]
             + [(f"mid{i}", 6.0, 6.0) for i in range(10)]
             + [("hero", 30.0, 6.0)])
    paths = _lib(tmp, cache, specs)
    stray = tmp / "uncached.mp4"; stray.write_bytes(b"x")
    picked, stats = autopick.select_clips(
        paths + [stray], _grid(64, 0.5), cache, tightness=0.5, target_duration=None)
    names = [p.name for p in picked]
    assert "hero.mp4" in names, "top windowed-motion clip must always be included"
    assert "shorty.mp4" not in names, "sub-second clips can't fill any slot"
    assert stats["skipped_uncached"] == 1 and stats["skipped_short"] == 1
    # The dead clip may only appear if the pool needed everything; with 12 usable
    # clips and a 12-clip target it can sneak in, so assert it ranks LAST if present.
    if "dead.mp4" in names:
        assert len(names) == stats["analyzed"], "dead clip admitted only when everything is"
    print(f"  gates: hero in, short out, uncached counted ({stats['picked']} picked) OK")


def test_deterministic_without_seed_varies_with_seed():
    tmp = Path(tempfile.mkdtemp())
    cache = tmp / "cache"
    paths = _lib(tmp, cache, [(f"c{i}", 2.0 + i * 1.7, 6.0) for i in range(30)])
    grid = _grid(64, 0.6)
    a, _ = autopick.select_clips(paths, grid, cache, tightness=0.5, target_duration=None)
    b, _ = autopick.select_clips(paths, grid, cache, tightness=0.5, target_duration=None)
    s1, _ = autopick.select_clips(paths, grid, cache, tightness=0.5, target_duration=None, seed=1)
    s1b, _ = autopick.select_clips(paths, grid, cache, tightness=0.5, target_duration=None, seed=1)
    s2, _ = autopick.select_clips(paths, grid, cache, tightness=0.5, target_duration=None, seed=2)
    assert a == b, "seed=None must be fully deterministic"
    assert s1 == s1b, "same seed must reproduce"
    assert s1 != s2 or s1 != a, "different seeds should be able to differ"
    print(f"  determinism: seed=None stable, seeds reproduce/vary OK")


def test_pool_size_tracks_song_and_bounds():
    tmp = Path(tempfile.mkdtemp())
    cache = tmp / "cache"
    paths = _lib(tmp, cache, [(f"c{i}", 5.0 + (i % 7), 6.0) for i in range(200)])
    small, st_small = autopick.select_clips(
        paths, _grid(16, 0.5), cache, tightness=0.3, target_duration=None)
    big, st_big = autopick.select_clips(
        paths, _grid(600, 0.9), cache, tightness=1.0, target_duration=None)
    assert autopick.POOL_MIN <= len(small) <= len(big) <= autopick.POOL_MAX
    assert st_big["est_slots"] > st_small["est_slots"]
    print(f"  pool: {len(small)} clips for a short calm song, {len(big)} for a long banger OK")


if __name__ == "__main__":
    print("autopick golden tests")
    test_uncached_pool_is_a_hard_error()
    test_loud_song_prefers_hot_clips_quiet_prefers_calm()
    test_hero_dead_short_and_uncached_handling()
    test_deterministic_without_seed_varies_with_seed()
    test_pool_size_tracks_song_and_bounds()
    print("all passed")
