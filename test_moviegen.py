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


def _drop_grid(n_beats: int = 32, spacing: float = 0.5) -> m.BeatGrid:
    """A quiet first half then a loud second half — a single clear _accent_times drop
    at the midpoint, with long held shots in the calm pre-drop section (so the cinematic
    breakdown slow-mo has somewhere to land)."""
    dur = n_beats * spacing
    half = n_beats // 2
    beats = [m.Beat(time=i * spacing, is_downbeat=(i % 4 == 0),
                    energy=(0.08 if i < half else 0.9),
                    section_id=(0 if i < half else 1))
             for i in range(n_beats)]
    secs = [m.Section(0, 0.0, half * spacing, 0.12),
            m.Section(1, half * spacing, dur, 0.9)]
    return m.BeatGrid(duration=dur, tempo=120.0, beats=beats, sections=secs)


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


def test_default_path_has_no_slowmo():
    # The cinematic breakdown features are opt-in: without the kwargs (every other
    # preset + all the golden tests above), no entry is ever slowed.
    grid = _grid(16, 0.5)
    curves = [_curve("a", 5.0, 1.0), _curve("b", 5.0, 2.0), _curve("c", 5.0, 3.0)]
    edl = m.plan_cuts(grid, curves, tightness=0.7, target_duration=None)
    assert all(e.playback_speed == 1.0 for e in edl.entries), \
        [e.playback_speed for e in edl.entries]
    print("  default-off: every entry plays at 1.0x (other presets unchanged) OK")


def test_cinematic_breakdown_slowmo_preserves_invariant():
    grid = _drop_grid()
    # Three calm clips + one clearly-most-cinematic clip ("3", sustained high motion).
    curves = [_curve_samples(str(i), 16.0, [0.1] * 128) for i in range(3)]
    curves.append(_curve_samples("3", 16.0, [0.9] * 128))
    edl = m.plan_cuts(grid, curves, tightness=0.15, target_duration=None,
                      transitions="accents", breakdown_slowmo=0.5, hero_shot=True)
    # Tiling is untouched by slow-mo (playback_speed enters no sum).
    total = sum(e.duration for e in edl.entries)
    assert abs(total - grid.duration) < 0.05, (total, grid.duration)
    for prev, nxt in zip(edl.entries, edl.entries[1:]):
        assert abs((prev.place_at + prev.duration) - nxt.place_at) < 1e-3
    # Slow-mo fired, stayed surgical, and every slowed shot is valid.
    slow = [e for e in edl.entries if e.playback_speed < 1.0]
    assert 1 <= len(slow) <= m.SLOWMO_MAX_ENTRIES, [e.playback_speed for e in edl.entries]
    assert all(m.SLOWMO_MIN_SPEED <= e.playback_speed < 1.0 for e in slow)
    assert all(e.transition_dur == 0.0 and not e.speak for e in slow)
    # The slowed shots lead INTO the drop; the drop entry itself slams at full speed.
    drop = grid.duration / 2.0  # _drop_grid's quiet->loud breakdown is at the midpoint
    h = min(range(len(edl.entries)), key=lambda k: abs(edl.entries[k].place_at - drop))
    assert edl.entries[h].playback_speed == 1.0
    assert all(e.place_at < edl.entries[h].place_at for e in slow)
    # Decel ramp: shots nearer the drop are slowed at least as hard as earlier ones.
    sl = sorted(slow, key=lambda e: e.place_at)
    for earlier, later in zip(sl, sl[1:]):
        assert later.playback_speed <= earlier.playback_speed + 1e-9, [s.playback_speed for s in sl]
    print(f"  cinematic slow-mo: {len(slow)} shot(s) ramped into the drop "
          f"(speeds {[s.playback_speed for s in sl]}), tiles {total:.2f}s OK")


def test_cinematic_hero_and_slowmo_deterministic():
    grid = _drop_grid()
    curves = [_curve_samples(str(i), 16.0, [0.1] * 128) for i in range(3)]
    curves.append(_curve_samples("3", 16.0, [0.9] * 128))
    kw = dict(tightness=0.15, target_duration=None, transitions="accents",
              breakdown_slowmo=0.5, hero_shot=True)
    a = m.plan_cuts(grid, curves, **kw)
    b = m.plan_cuts(grid, curves, **kw)
    seq = lambda edl: [(e.clip_id, e.in_point, e.place_at, e.playback_speed,
                        e.transition_dur) for e in edl.entries]
    assert seq(a) == seq(b), "seed=None must be fully deterministic incl. playback_speed"
    # The reserved hero clip ("3") lands on the strongest drop interval.
    median_len = (grid.duration) / max(1, len(a.entries))
    hero = m._hero_clip(curves, median_len)
    assert hero == "3", hero
    drop = grid.duration / 2.0
    h = min(range(len(a.entries)), key=lambda k: abs(a.entries[k].place_at - drop))
    assert a.entries[h].clip_id == "3", (a.entries[h].clip_id, drop)
    print("  cinematic hero+slow-mo: deterministic, hero clip reserved for the drop OK")


def test_breakdown_presets_configured():
    # Slow-mo + hero live on cinematic AND music video; classic/moody stay pure.
    for p in ("cinematic", "musicvideo"):
        cfg = m.PRESETS[p]
        assert cfg.get("hero_shot") is True, p
        assert m.SLOWMO_MIN_SPEED <= cfg.get("breakdown_slowmo", 0.0) < 1.0, p
    for p in ("classic", "moody"):
        assert "breakdown_slowmo" not in m.PRESETS[p], p
        assert "hero_shot" not in m.PRESETS[p], p
    print("  presets: slow-mo on cinematic+musicvideo, off classic/moody OK")


def test_head_trim_detects_reference_sheet():
    fps = 8.0
    # HELD card, 0.5s: frames 0..3 held (3 near-zero raw diffs), hard cut at
    # raw[3], then normal motion. Content starts at frame 4 -> trim 4/8 = 0.5s.
    raw = [0.4, 0.5, 0.3, 38.0] + [9.0, 11.0, 7.5] * 20
    assert m._head_trim_from_diffs(raw, fps) == 0.5, m._head_trim_from_diffs(raw, fps)
    # HELD card, 1.0s: 7 static diffs, cut at raw[7] -> trim 1.0s.
    raw = [0.5] * 7 + [45.0] + [8.0] * 40
    assert m._head_trim_from_diffs(raw, fps) == 1.0
    # HELD card exiting via a short dissolve instead of a single hard cut.
    raw = [0.5] * 4 + [30.0, 25.0, 18.0] + [5.0] * 30
    assert m._head_trim_from_diffs(raw, fps) == 7 / 8
    # FLASH cards — raw diffs measured on real Grok exports: the card lives on
    # frame 0 and cross-fades out within 1-2 analysis frames.
    real_a = [45.2, 18.0, 2.8, 3.2, 3.0, 3.2, 3.5, 3.9, 3.5, 3.2, 3.0, 3.4, 2.9, 2.7]
    assert m._head_trim_from_diffs(real_a, fps) == 0.25, m._head_trim_from_diffs(real_a, fps)
    real_b = [65.7, 5.2, 4.3, 3.3, 2.8, 2.6, 2.1, 1.6, 1.6, 2.1, 2.0, 2.0, 2.1, 1.9]
    assert m._head_trim_from_diffs(real_b, fps) == 0.125, m._head_trim_from_diffs(real_b, fps)
    print("  head-trim: held (0.5s/1.0s/dissolve) + real flash cards detected OK")


def test_head_trim_leaves_normal_footage_alone():
    fps = 8.0
    # Motion from the very first frame — no static run, nothing to trim.
    assert m._head_trim_from_diffs([12.0, 9.0, 14.0] * 10, fps) == 0.0
    # A fade-in rises gradually — the static run ends on a gentle rise, not a spike.
    ramp = [0.2, 0.5, 1.0, 2.5, 4.0, 6.0, 9.0, 12.0] + [10.0] * 30
    assert m._head_trim_from_diffs(ramp, fps) == 0.0
    # A hard cut at ~2s is a real scene change, not an intro card.
    late = [0.5] * 16 + [40.0] + [9.0] * 30
    assert m._head_trim_from_diffs(late, fps) == 0.0
    # A clip that is static throughout has no cut to trim to.
    assert m._head_trim_from_diffs([0.3] * 40, fps) == 0.0
    # An opening spike over BUSY content isn't a flash card (ratio < 5x baseline).
    assert m._head_trim_from_diffs([38.0] + [9.0] * 30, fps) == 0.0
    # Sustained fast action from frame 0 — a long spike-run is not a dissolve.
    assert m._head_trim_from_diffs([40.0, 35.0, 30.0, 25.0, 20.0, 15.0, 13.0] + [9.0] * 30, fps) == 0.0
    # Real calm clips (measured): baselines hover at/under the static floor with
    # no exit spike — never confused with a held card.
    real_calm = [2.2, 2.0, 2.0, 2.1, 2.5, 2.5, 2.5, 2.8, 3.1, 3.1] + [3.0] * 10
    assert m._head_trim_from_diffs(real_calm, fps) == 0.0
    real_fade = [0.6, 0.8, 1.2, 2.2, 3.9, 5.3, 5.6, 5.0, 5.1, 4.6] + [5.0] * 10
    assert m._head_trim_from_diffs(real_fade, fps) == 0.0
    assert m._head_trim_from_diffs([], fps) == 0.0
    print("  head-trim: motion/fade-in/late-cut/static/busy-open clips untouched OK")


def test_head_offset_flows_to_edl():
    # A trimmed clip's head_offset must ride every EDL entry that uses it (the
    # render adds it to the source seek); untrimmed clips stay 0.
    grid = _grid(16, 0.5)
    trimmed = m.MotionCurve(clip_id="t", src_path="/tmp/t.mp4", duration=6.0,
                            fps_analyzed=8.0, samples=[0.1] * 48, head_offset=0.75)
    other = _curve("o", 6.0, 2.0)
    edl = m.plan_cuts(grid, [trimmed, other], tightness=0.5, target_duration=None)
    assert any(e.clip_id == "t" for e in edl.entries), "trimmed clip never chosen"
    for e in edl.entries:
        want = 0.75 if e.clip_id == "t" else 0.0
        assert e.head_offset == want, (e.clip_id, e.head_offset)
    print("  head-offset: rides EDL entries (0.75s on trimmed clip, 0 elsewhere) OK")


def test_utterance_head_offset_shift():
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    clip = d / "clip.mp4"
    clip.write_bytes(b"")  # only the sidecar is read
    (d / "clip.srt").write_text(
        "1\n00:00:00,200 --> 00:00:01,800\nthis line starts on the card\n\n"
        "2\n00:00:03,000 --> 00:00:05,000\nthis line is real dialogue\n", encoding="utf-8")
    us = m._utterances_for_clip(0, clip, clip_dur=9.0, head_offset=1.0)
    # The line starting inside the trimmed head is dropped; the survivor is
    # shifted into trimmed clip time (3.0..5.0 -> 2.0..4.0).
    assert len(us) == 1, [u.text for u in us]
    assert abs(us[0].start - 2.0) < 1e-6 and abs(us[0].end - 4.0) < 1e-6, us[0]
    print("  head-offset: card-time line dropped, survivor shifted into trimmed time OK")


def test_beat_cache_key_content_hashed():
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    a = d / "a.mp3"; a.write_bytes(b"fake-song-bytes-AAAA")
    b = d / "sub"; b.mkdir(); b = b / "other-name.mp3"
    b.write_bytes(b"fake-song-bytes-AAAA")     # same content, different path+mtime
    c = d / "c.mp3"; c.write_bytes(b"totally-different-bytes")
    ka = m._beat_cache_key(a, enhanced=True, beat_engine="neural")
    kb = m._beat_cache_key(b, enhanced=True, beat_engine="neural")
    kc = m._beat_cache_key(c, enhanced=True, beat_engine="neural")
    kd = m._beat_cache_key(a, enhanced=False, beat_engine="librosa")
    ke = m._beat_cache_key(a, enhanced=True, beat_engine="librosa")
    assert ka == kb, "same song content must share a key regardless of path/mtime"
    assert len({ka, kc, kd, ke}) == 4, "content/engine/enhanced must all split the key"
    print("  beat-cache key: content-hashed (path/mtime-proof), params split OK")


def test_beat_cache_hit_roundtrips_grid_exactly():
    import gzip, json, tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    song = d / "song.mp3"; song.write_bytes(b"pretend-this-is-audio")
    grid = _grid(16, 0.5)
    grid.onsets = [0.123456789012345, 1.5, 2.25]
    grid.engine, grid.device, grid.engine_note = "madmom", "cpu", ""
    cache = d / "beat_cache"; cache.mkdir()
    key = m._beat_cache_key(song, enhanced=True, beat_engine="neural")
    with gzip.open(m._beat_cache_path(cache, key), "wt", encoding="utf-8") as fh:
        json.dump(m._grid_to_payload(grid), fh)
    # The pre-seeded entry must HIT (so analyze_audio/librosa is never touched)
    # and come back field-identical — floats round-trip JSON exactly.
    g2, hit = m.load_or_analyze_audio(song, enhanced=True, beat_engine="neural",
                                      cache_dir=cache)
    assert hit is True, "expected a cache hit for the pre-seeded key"
    assert (g2.duration, g2.tempo) == (grid.duration, grid.tempo)
    assert [(b.time, b.is_downbeat, b.energy, b.section_id) for b in g2.beats] == \
           [(b.time, b.is_downbeat, b.energy, b.section_id) for b in grid.beats]
    assert [(s.id, s.start, s.end, s.intensity) for s in g2.sections] == \
           [(s.id, s.start, s.end, s.intensity) for s in grid.sections]
    assert g2.onsets == grid.onsets
    assert (g2.engine, g2.device, g2.engine_note) == ("madmom", "cpu", "")
    # No cache dir -> the loader must report a miss path (falls through to
    # analyze_audio; not exercised here to keep librosa out of the tests).
    print("  beat-cache hit: pre-seeded grid returned bit-identical OK")


def test_beat_cache_prune_lru():
    import os, tempfile, time
    from pathlib import Path
    cache = Path(tempfile.mkdtemp())
    files = []
    now = time.time()
    for i in range(4):
        f = cache / f"{i:040d}.json.gz"
        f.write_bytes(b"x")
        os.utime(f, (now + i, now + i))        # staggered mtimes: 3 is newest
        files.append(f)
    old = m.BEAT_CACHE_MAX_ENTRIES
    m.BEAT_CACHE_MAX_ENTRIES = 2
    try:
        m._prune_beat_cache(cache)
    finally:
        m.BEAT_CACHE_MAX_ENTRIES = old
    left = sorted(f.name for f in cache.glob("*.json.gz"))
    assert left == [files[2].name, files[3].name], left
    print("  beat-cache prune: LRU keeps the 2 newest of 4 OK")


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
    test_default_path_has_no_slowmo()
    test_cinematic_breakdown_slowmo_preserves_invariant()
    test_cinematic_hero_and_slowmo_deterministic()
    test_breakdown_presets_configured()
    test_head_trim_detects_reference_sheet()
    test_head_trim_leaves_normal_footage_alone()
    test_head_offset_flows_to_edl()
    test_utterance_head_offset_shift()
    test_beat_cache_key_content_hashed()
    test_beat_cache_hit_roundtrips_grid_exactly()
    test_beat_cache_prune_lru()
    print("all passed")
