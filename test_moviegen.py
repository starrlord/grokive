"""Golden tests for the cut planner — the one stage with no off-the-shelf
equivalent. Synthetic beat grid + synthetic motion curves with known answers so
regressions in tiling, alignment, and the duration-sum invariant are caught.

Run: python test_moviegen.py   (no pytest dependency required)
"""

from __future__ import annotations

import moviegen as m


def _grid(n_beats: int, spacing: float, intensity: float = 0.6) -> m.BeatGrid:
    dur = n_beats * spacing
    section = m.Section(0, 0.0, dur, intensity)
    beats = [m.Beat(time=i * spacing, is_downbeat=(i % 4 == 0), energy=0.5, section_id=0)
             for i in range(n_beats)]
    return m.BeatGrid(duration=dur, tempo=120.0, beats=beats, sections=[section])


def _curve(cid: str, dur: float, peak_at: float, fps: float = 8.0) -> m.MotionCurve:
    n = int(dur * fps)
    peak_i = int(peak_at * fps)
    samples = [0.1] * n
    if 0 <= peak_i < n:
        samples[peak_i] = 1.0
    return m.MotionCurve(clip_id=cid, src_path=f"/tmp/{cid}.mp4", duration=dur,
                         fps_analyzed=fps, samples=samples)


def _curve_multi(cid: str, dur: float, peaks: list[float], fps: float = 8.0) -> m.MotionCurve:
    n = int(dur * fps)
    samples = [0.1] * n
    for p in peaks:
        i = int(p * fps)
        if 0 <= i < n:
            samples[i] = 1.0
    return m.MotionCurve(clip_id=cid, src_path=f"/tmp/{cid}.mp4", duration=dur,
                         fps_analyzed=fps, samples=samples)


def _ramp_grid(n_beats: int, spacing: float) -> m.BeatGrid:
    """Beat grid whose energy ramps 0.1 -> 1.0 (a slow build)."""
    dur = n_beats * spacing
    beats = [m.Beat(time=i * spacing, is_downbeat=(i % 4 == 0),
                    energy=0.1 + 0.9 * i / (n_beats - 1), section_id=0)
             for i in range(n_beats)]
    return m.BeatGrid(duration=dur, tempo=120.0, beats=beats,
                      sections=[m.Section(0, 0.0, dur, 0.5)])


def test_tiling_invariant():
    grid = _grid(16, 0.5)
    curves = [_curve("a", 5.0, 1.0), _curve("b", 5.0, 2.0), _curve("c", 5.0, 3.0)]
    edl = m.plan_cuts(grid, curves, tightness=0.7, target_duration=None)
    total = sum(e.duration for e in edl.entries)
    assert abs(total - grid.duration) < 0.05, (total, grid.duration)
    # place_at values are contiguous: each starts where the previous ended.
    for prev, nxt in zip(edl.entries, edl.entries[1:]):
        assert abs((prev.place_at + prev.duration) - nxt.place_at) < 1e-3
    print(f"  tiling: {len(edl.entries)} cuts tile {total:.2f}s OK")


def test_peak_alignment():
    # A clip whose only motion spike is at t=2.0; the chosen in_point should
    # place that spike at the start of the interval (on the beat).
    grid = _grid(8, 1.0)
    curves = [_curve("spike", 6.0, 2.0), _curve("flat", 6.0, 0.0)]
    edl = m.plan_cuts(grid, curves, tightness=0.0, target_duration=None)
    spike_entries = [e for e in edl.entries if e.clip_id == "spike"]
    assert spike_entries, "spike clip never chosen"
    for e in spike_entries:
        assert abs(e.in_point - 2.0) < 0.2, e.in_point
    print(f"  alignment: spike in_point={spike_entries[0].in_point:.2f}s (~2.0) OK")


def test_density_scales_with_tightness():
    grid = _grid(32, 0.5)
    curves = [_curve("a", 8.0, 1.0), _curve("b", 8.0, 2.0)]
    relaxed = m.plan_cuts(grid, curves, tightness=0.0, target_duration=None)
    tight = m.plan_cuts(grid, curves, tightness=1.0, target_duration=None)
    assert len(tight.entries) > len(relaxed.entries), (len(tight.entries), len(relaxed.entries))
    print(f"  density: relaxed={len(relaxed.entries)} cuts, tight={len(tight.entries)} cuts OK")


def test_oversized_interval_subdivided():
    # Sparse beats (4s apart) but clips only 2s long -> intervals must subdivide
    # so every entry is fillable and the timeline still tiles exactly.
    grid = _grid(8, 4.0)
    curves = [_curve("a", 2.0, 0.5), _curve("b", 2.0, 1.0)]
    edl = m.plan_cuts(grid, curves, tightness=0.0, target_duration=None)
    assert all(e.duration <= 2.0 + 1e-3 for e in edl.entries)
    assert abs(sum(e.duration for e in edl.entries) - grid.duration) < 0.05
    print(f"  subdivide: max seg={max(e.duration for e in edl.entries):.2f}s <= 2.0 OK")


def test_seed_variation():
    # Same inputs: same seed -> identical cut; different seeds -> different cut;
    # no seed -> deterministic. Clips have multiple motion peaks so there's room
    # to vary which moment is chosen.
    grid = _grid(32, 0.5)
    curves = [_curve_multi("a", 8.0, [1.0, 5.0]), _curve_multi("b", 8.0, [2.0, 6.0]),
              _curve_multi("c", 8.0, [3.0, 7.0])]
    seq = lambda edl: [(e.clip_id, e.in_point, e.place_at) for e in edl.entries]
    s1 = seq(m.plan_cuts(grid, curves, tightness=0.6, target_duration=None, seed=1))
    s1b = seq(m.plan_cuts(grid, curves, tightness=0.6, target_duration=None, seed=1))
    s2 = seq(m.plan_cuts(grid, curves, tightness=0.6, target_duration=None, seed=2))
    assert s1 == s1b, "same seed must reproduce"
    assert s1 != s2, "different seeds must differ"
    # Invariant still holds under exploration.
    edl = m.plan_cuts(grid, curves, tightness=0.6, target_duration=None, seed=7)
    assert abs(sum(e.duration for e in edl.entries) - grid.duration) < 0.05
    print(f"  variation: seed1=={len(s1)} cuts reproducible, seed1!=seed2 OK")


def test_slow_build_accelerates():
    # Energy ramps up; cuts should concentrate in the back half (the climax).
    grid = _ramp_grid(40, 0.5)
    curves = [_curve("a", 8.0, 1.0), _curve("b", 8.0, 2.0)]
    edl = m.plan_cuts(grid, curves, tightness=0.5, target_duration=None)
    half = grid.duration / 2
    first = sum(1 for e in edl.entries if e.place_at < half)
    second = sum(1 for e in edl.entries if e.place_at >= half)
    assert second > first, (first, second)
    assert abs(sum(e.duration for e in edl.entries) - grid.duration) < 0.05
    print(f"  build: front-half={first} cuts, back-half={second} cuts (accelerates) OK")


if __name__ == "__main__":
    print("cut planner golden tests")
    test_tiling_invariant()
    test_peak_alignment()
    test_density_scales_with_tightness()
    test_oversized_interval_subdivided()
    test_seed_variation()
    test_slow_build_accelerates()
    print("all passed")
