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


def _curve_samples(cid: str, dur: float, samples: list[float], fps: float = 8.0) -> m.MotionCurve:
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


def test_scored_window_is_rendered_with_internal_beat_peak():
    # The strongest 4s source window is 0..4s, with the peak at 2s. Rendering
    # from the peak itself would land the peak on the opening beat, but it would
    # throw away half of the scored motion. The planner should keep the strong
    # window and land the peak on the beat 2s into the shot instead.
    fps = 8.0
    samples = [0.1] * int(8 * fps)
    for i in range(0, int(4 * fps)):
        samples[i] = 0.8
    samples[int(2 * fps)] = 1.0
    grid = _grid(8, 1.0)
    curves = [_curve_samples("strong", 8.0, samples, fps), _curve("flat", 8.0, 0.0, fps)]
    edl = m.plan_cuts(grid, curves, tightness=0.0, target_duration=None)
    first = edl.entries[0]
    assert first.clip_id == "strong"
    assert abs(first.in_point - 0.0) < 0.2, first.in_point
    assert abs((first.place_at + (2.0 - first.in_point)) - 2.0) < 0.2
    print(f"  window-fit: strong window starts at {first.in_point:.2f}s; peak lands on internal beat OK")


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


def _grid_two_sections(n_beats: int = 16, spacing: float = 0.5) -> m.BeatGrid:
    """Two equal sections; the second is louder (a 'drop') and its boundary lands
    on a beat at the midpoint, so the planner has a place to put an accent."""
    dur = n_beats * spacing
    half = dur / 2
    secs = [m.Section(0, 0.0, half, 0.3), m.Section(1, half, dur, 0.8)]
    beats = [m.Beat(time=i * spacing, is_downbeat=(i % 4 == 0), energy=0.5,
                    section_id=(0 if i * spacing < half else 1)) for i in range(n_beats)]
    return m.BeatGrid(duration=dur, tempo=120.0, beats=beats, sections=secs)


def test_transitions_default_off():
    # Without the accents policy, the EDL is exactly a hard-cut timeline.
    grid = _grid_two_sections()
    curves = [_curve("a", 6.0, 3.0), _curve("b", 6.0, 2.5)]
    edl = m.plan_cuts(grid, curves, tightness=1.0, target_duration=None)
    assert all(e.transition == "cut" and e.transition_dur == 0.0 for e in edl.entries)
    print("  transitions-off: all hard cuts OK")


def test_transitions_accents_preserve_invariant():
    grid = _grid_two_sections()
    curves = [_curve("a", 6.0, 3.0), _curve("b", 6.0, 2.5), _curve("c", 6.0, 3.5)]
    edl = m.plan_cuts(grid, curves, tightness=1.0, target_duration=None, transitions="accents")
    # Tiling + contiguity must be untouched by transition metadata.
    assert abs(sum(e.duration for e in edl.entries) - grid.duration) < 0.05
    for prev, nxt in zip(edl.entries, edl.entries[1:]):
        assert abs((prev.place_at + prev.duration) - nxt.place_at) < 1e-3
    # A transition is placed, only at the section boundary (~midpoint), and it's
    # the canonical drop flash since the second section is louder.
    trans = [e for e in edl.entries if e.transition_dur > 0]
    assert trans, "expected an accent transition at the section boundary"
    for e in trans:
        assert abs(e.place_at - grid.duration / 2) < 0.26, e.place_at
        assert e.transition == m._TRANSITION_DROP and e.transition_dur > 0
    print(f"  accents: {len(trans)} transition(s) at section boundary, invariant holds OK")


def test_transitions_deterministic_without_seed():
    grid = _grid_two_sections()
    curves = [_curve("a", 6.0, 3.0), _curve("b", 6.0, 2.5), _curve("c", 6.0, 3.5)]
    seq = lambda: [(e.transition, round(e.transition_dur, 4))
                   for e in m.plan_cuts(grid, curves, tightness=1.0,
                                        target_duration=None, transitions="accents").entries]
    assert seq() == seq(), "seed=None transition assignment must be deterministic"
    print("  determinism: seed=None transitions reproducible OK")


def test_transitions_downgrade_without_headroom():
    # Clips exactly as long as their interval have no source to spare for handles,
    # so every boundary must fall back to a hard cut.
    grid = _grid_two_sections()
    curves = [_curve("a", 1.0, 0.5), _curve("b", 1.0, 0.5), _curve("c", 1.0, 0.5)]
    edl = m.plan_cuts(grid, curves, tightness=1.0, target_duration=None, transitions="accents")
    assert all(e.transition_dur == 0.0 for e in edl.entries), "no headroom must downgrade to cut"
    assert abs(sum(e.duration for e in edl.entries) - grid.duration) < 0.05
    print("  headroom: no-handle clips downgrade to hard cuts OK")


def _grid_calm_loud(n_beats: int = 64, spacing: float = 0.5) -> m.BeatGrid:
    """First half calm, second half loud — so phrase rhythm should hold long shots
    in the calm half and burst-cut the loud half."""
    dur = n_beats * spacing
    half = n_beats // 2
    beats = [m.Beat(time=i * spacing, is_downbeat=(i % 4 == 0),
                    energy=(0.2 if i < half else 0.9), section_id=(0 if i < half else 1))
             for i in range(n_beats)]
    secs = [m.Section(0, 0.0, dur / 2, 0.2), m.Section(1, dur / 2, dur, 0.9)]
    return m.BeatGrid(duration=dur, tempo=120.0, beats=beats, sections=secs)


def test_phrase_rhythm_is_bimodal():
    grid = _grid_calm_loud()
    curves = [_curve("a", 6.0, 3.0), _curve("b", 6.0, 2.5), _curve("c", 6.0, 3.5)]
    edl = m.plan_cuts(grid, curves, tightness=0.5, target_duration=None, rhythm="phrase")
    durs = [e.duration for e in edl.entries]
    assert abs(sum(durs) - grid.duration) < 0.05
    for prev, nxt in zip(edl.entries, edl.entries[1:]):
        assert abs((prev.place_at + prev.duration) - nxt.place_at) < 1e-3
    assert max(durs) >= 3.0, ("phrase rhythm produced no long holds", max(durs))
    assert min(durs) <= 0.4, ("phrase rhythm produced no bursts", min(durs))
    print(f"  phrase: holds up to {max(durs):.1f}s, bursts down to {min(durs):.2f}s, tiles OK")


def test_calm_bias_prefers_calm_clips():
    dur = 8.0
    beats = [m.Beat(time=i * 1.0, is_downbeat=(i % 4 == 0), energy=0.12, section_id=0)
             for i in range(8)]
    grid = m.BeatGrid(duration=dur, tempo=60.0, beats=beats,
                      sections=[m.Section(0, 0.0, dur, 0.12)])
    hot = _curve_samples("hot", 8.0, [0.9] * 64)
    calm = _curve_samples("calm", 8.0, [0.1] * 64)
    d = m.plan_cuts(grid, [hot, calm], tightness=0.0, target_duration=None)
    c = m.plan_cuts(grid, [hot, calm], tightness=0.0, target_duration=None, motion_weight=0.0)
    assert any(e.clip_id == "hot" for e in d.entries), "default should pick the high-motion clip"
    assert any(e.clip_id == "calm" for e in c.entries), "motion_weight=0 should pull in the calm clip"
    print("  calm-bias: motion_weight=0 chooses the low-motion clip in a quiet passage OK")


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


def test_subtitle_parsing_srt_and_vtt():
    srt = ("1\n00:00:01,000 --> 00:00:03,500\nHello there friend\n\n"
           "2\n00:00:04,000 --> 00:00:05,000\nYeah\n")
    vtt = ("WEBVTT\n\nNOTE something\n\n"
           "1\n00:00:01.000 --> 00:00:03.500 align:start\n<i>Hello</i> there friend\n\n"
           "2\n00:00:04.000 --> 00:00:05.000\nYeah\n")
    cs = m._parse_cues(srt)
    cv = m._parse_cues(vtt)
    assert len(cs) == 2 and len(cv) == 2, (cs, cv)
    assert cs[0][0] == 1.0 and abs(cs[0][1] - 3.5) < 1e-6, cs[0]
    assert cs[0][2] == "Hello there friend", cs[0]
    # VTT: header/NOTE skipped, cue settings on the timing line ignored, tags stripped.
    assert cv[0][2] == "Hello there friend", cv[0]
    print("  subtitles: SRT + VTT parse, tags/settings/NOTE handled OK")


def test_utterance_word_filter_and_merge(tmp=None):
    import tempfile, os
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    clip = d / "clip.mp4"
    clip.write_bytes(b"")  # only the sidecar is read
    # Two short cues <0.4s apart merge into one line; a lone 1-word cue is dropped.
    (d / "clip.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,200\nI was never\n\n"
        "2\n00:00:01,400 --> 00:00:02,600\nyour type at all\n\n"
        "3\n00:00:05,000 --> 00:00:06,000\nYeah\n", encoding="utf-8")
    us = m._utterances_for_clip(0, clip, clip_dur=10.0)
    assert len(us) == 1, [u.text for u in us]
    assert us[0].words >= m.MIN_SPEAK_WORDS and "your type" in us[0].text
    assert abs(us[0].start - 0.0) < 1e-6 and abs(us[0].end - 2.6) < 1e-6, us[0]
    print(f"  utterances: merged to '{us[0].text}' ({us[0].words}w), dropped 1-word cue OK")


def test_trim_to_sentences_never_cuts_midsentence():
    # A 5-sentence line spanning 10s; with max_dur 4s we must keep whole sentences
    # only (ending on a boundary), never slice one in half.
    text = "One two three. Four five six. Seven eight nine. Ten eleven twelve. End here now."
    end, kept = m._trim_to_sentences(0.0, 10.0, text, max_dur=4.0)
    assert kept.endswith(("three.", "six.", "nine.")), kept
    # kept is a prefix of whole sentences (no partial trailing sentence)
    assert all(s.strip() in text for s in kept.split(". ") if s.strip())
    assert end <= 10.0
    # A single long sentence is kept whole even past max_dur (don't cut it).
    end2, kept2 = m._trim_to_sentences(0.0, 9.0, "a b c d e f g h i j k l m n o p", max_dur=3.0)
    assert kept2 == "a b c d e f g h i j k l m n o p" and abs(end2 - 9.0) < 1e-6
    print(f"  trim: kept whole sentences '{kept}' (no mid-sentence cut) OK")


def test_speech_moment_in_quiet_pocket():
    # First half loud, second half quiet -> a line must land in the quiet half,
    # beat-aligned, and not in the edge pad.
    grid = _grid_calm_loud()  # but this is calm-then-loud; flip via a custom grid
    dur = 64 * 0.5
    half = 32
    beats = [m.Beat(time=i * 0.5, is_downbeat=(i % 4 == 0),
                    energy=(0.9 if i < half else 0.15), section_id=(0 if i < half else 1))
             for i in range(64)]
    grid = m.BeatGrid(duration=dur, tempo=120.0, beats=beats,
                      sections=[m.Section(0, 0.0, dur / 2, 0.9), m.Section(1, dur / 2, dur, 0.15)])
    u = m.Utterance(clip_idx=0, src_path="/tmp/a.mp4", start=0.0, end=2.0, text="one two three four five")
    moments = m._pick_speech_moments(grid, [u], dur, count=1)
    assert len(moments) == 1, moments
    mo = moments[0]
    assert mo.place_at >= dur / 2 - 1e-6, ("line landed in the loud half", mo.place_at)
    assert mo.place_at >= m.SPEAK_EDGE_PAD and mo.place_at + mo.dur <= dur - m.SPEAK_EDGE_PAD
    print(f"  speech-pick: line placed at {mo.place_at:.1f}s in the quiet half OK")


def test_forced_speech_owns_interval_and_tiles():
    # A forced spoken line must appear as exactly one entry of the right clip,
    # span, and in_point — and the timeline must still tile.
    dur = 64 * 0.5
    beats = [m.Beat(time=i * 0.5, is_downbeat=(i % 4 == 0),
                    energy=(0.9 if i < 32 else 0.15), section_id=(0 if i < 32 else 1))
             for i in range(64)]
    grid = m.BeatGrid(duration=dur, tempo=120.0, beats=beats,
                      sections=[m.Section(0, 0.0, dur / 2, 0.9), m.Section(1, dur / 2, dur, 0.15)])
    curves = [_curve("0", 12.0, 3.0), _curve("1", 12.0, 2.5), _curve("2", 12.0, 3.5)]
    mo = m.SpeechMoment(place_at=20.0, dur=2.0, clip_idx=1, src_path="/tmp/1.mp4",
                        in_point=0.5, text="one two three four")
    edl = m.plan_cuts(grid, curves, tightness=0.6, target_duration=None,
                      transitions="accents", forced_speech=[mo])
    spoken = [e for e in edl.entries if e.speak]
    assert len(spoken) == 1, spoken
    e = spoken[0]
    assert e.clip_id == "1" and abs(e.place_at - 20.0) < 1e-3 and abs(e.duration - 2.0) < 1e-3
    assert abs(e.in_point - 0.5) < 1e-3 and abs(e.out_point - 2.5) < 1e-3
    assert abs(sum(x.duration for x in edl.entries) - grid.duration) < 0.05
    for prev, nxt in zip(edl.entries, edl.entries[1:]):
        assert abs((prev.place_at + prev.duration) - nxt.place_at) < 1e-3
    print("  forced-speech: line owns one interval, in_point honoured, tiles OK")


def test_no_forced_speech_leaves_edl_unchanged():
    grid = _grid(16, 0.5)
    curves = [_curve("a", 5.0, 1.0), _curve("b", 5.0, 2.0), _curve("c", 5.0, 3.0)]
    a = m.plan_cuts(grid, curves, tightness=0.7, target_duration=None)
    b = m.plan_cuts(grid, curves, tightness=0.7, target_duration=None, forced_speech=[])
    seq = lambda edl: [(e.clip_id, e.in_point, e.place_at, e.speak) for e in edl.entries]
    assert seq(a) == seq(b), "empty forced_speech must not change the EDL"
    assert all(e.speak is False for e in b.entries)
    print("  no-speech: empty forced_speech is a no-op OK")


if __name__ == "__main__":
    print("cut planner golden tests")
    test_tiling_invariant()
    test_peak_alignment()
    test_scored_window_is_rendered_with_internal_beat_peak()
    test_density_scales_with_tightness()
    test_oversized_interval_subdivided()
    test_seed_variation()
    test_transitions_default_off()
    test_transitions_accents_preserve_invariant()
    test_transitions_deterministic_without_seed()
    test_transitions_downgrade_without_headroom()
    test_phrase_rhythm_is_bimodal()
    test_calm_bias_prefers_calm_clips()
    test_slow_build_accelerates()
    test_subtitle_parsing_srt_and_vtt()
    test_utterance_word_filter_and_merge()
    test_trim_to_sentences_never_cuts_midsentence()
    test_speech_moment_in_quiet_pocket()
    test_forced_speech_owns_interval_and_tiles()
    test_no_forced_speech_leaves_edl_unchanged()
    print("all passed")
