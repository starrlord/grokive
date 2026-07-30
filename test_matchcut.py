"""Tests for matchcut.py.

Two tiers, mirroring test_moviegen.py's split:

  * SYNTHETIC-DESCRIPTOR tests (fast, no ffmpeg) pin the scorer, the gate, the chain
    DP's legality invariants and the speed solve.
  * FFMPEG tests (skipped when ffmpeg is absent) pin the descriptor extractor against
    footage with a KNOWN motion — most importantly the portrait direction test, which
    is the regression guard for the aspect-squeeze bug that silently biases direction
    by 15-23 degrees.

Run: venv/Scripts/python.exe test_matchcut.py
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

import matchcut as mc

FAILURES: list[str] = []
SKIPPED: list[str] = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)


def _desc(clip_id, *, dur=6.0, vx=0.0, vy=0.0, zoom=0.0, roll=0.0, conf=0.9,
          energy=0.3, cx=0.5, cy=0.5, luma=0.5, thumb=0.5, fps=12.0,
          head_offset=0.0, scene_cuts=None) -> mc.ClipDesc:
    """A synthetic ClipDesc with constant motion — the analogue of test_moviegen's
    _grid()/_curve() helpers."""
    n = int(dur * fps)
    one = np.ones(n)
    return mc.ClipDesc(
        clip_id=str(clip_id), src_path=f"/fake/{clip_id}.mp4", duration=dur, fps=fps,
        head_offset=head_offset,
        t=np.arange(n) / fps,
        vx=one * vx, vy=one * vy, zoom=one * zoom, roll=one * roll,
        conf=one * conf, energy=one * energy, cx=one * cx, cy=one * cy,
        luma=one * luma, thumb=np.full((n, 64), thumb),
        scene_cuts=list(scene_cuts or []),
    )


# --------------------------------------------------------------------------- #
# Gate + scorer
# --------------------------------------------------------------------------- #

def test_static_clip_fails_the_gate():
    """The single most important behaviour: without the gate the scorer happily
    'matches' two motionless frames and reports a healthy score while doing it."""
    still = _desc("s", vx=0.0, vy=0.0, zoom=0.0, roll=0.0)
    check(_role_size(still, "out") == 0, "a motionless clip must yield NO out-candidates")
    check(_role_size(still, "in") == 0, "a motionless clip must yield NO in-candidates")


def _role_size(c, role):
    return mc._role_candidates(c, role).size


def test_zoom_alone_passes_the_gate():
    """Zoom is the dominant cue in this library — a clip that only pushes in must be
    usable, or ~half the library is thrown away."""
    z = _desc("z", vx=0.0, vy=0.0, zoom=8.0)
    check(_role_size(z, "out") > 0, "a zoom-only clip must produce candidates")
    r = _desc("r", roll=10.0)
    check(_role_size(r, "out") > 0, "a roll-only clip must produce candidates")


def test_direction_beats_antidirection():
    """Matching motion must outscore opposing motion."""
    a = _desc("a", vx=0.15)
    same = _desc("b", vx=0.15)
    opp = _desc("c", vx=-0.15)
    ia, ib = mc._role_candidates(a, "out"), mc._role_candidates(same, "in")
    ic = mc._role_candidates(opp, "in")
    s_same = mc.seam_matrix(a, ia, same, ib).max()
    s_opp = mc.seam_matrix(a, ia, opp, ic).max()
    check(s_same > s_opp, f"aligned motion must score above opposed ({s_same:.3f} vs {s_opp:.3f})")


def test_zoom_sign_must_agree():
    """A push-in meeting a pull-out is not a match, however similar the magnitudes."""
    a = _desc("a", zoom=9.0)
    same = _desc("b", zoom=9.0)
    opp = _desc("c", zoom=-9.0)
    ia = mc._role_candidates(a, "out")
    s_same = mc.seam_matrix(a, ia, same, mc._role_candidates(same, "in")).max()
    s_opp = mc.seam_matrix(a, ia, opp, mc._role_candidates(opp, "in")).max()
    check(s_same > s_opp, f"same-sign zoom must beat opposite-sign ({s_same:.3f} vs {s_opp:.3f})")


def test_score_never_exceeds_maxs():
    a = _desc("a", vx=0.2, zoom=6.0, roll=8.0)
    b = _desc("b", vx=0.2, zoom=6.0, roll=8.0)
    m = mc.seam_matrix(a, mc._role_candidates(a, "out"), b, mc._role_candidates(b, "in"))
    check(m.max() <= mc.MAXS + 1e-6, f"score {m.max():.3f} exceeded MAXS {mc.MAXS}")


def test_speed_band_is_free():
    """A speed ratio inside the band a retime absorbs must cost nothing versus 1.0."""
    a = _desc("a", vx=0.20)
    exact = _desc("b", vx=0.20)
    inband = _desc("c", vx=0.20 * 1.20)
    outband = _desc("d", vx=0.20 * 2.20)
    ia = mc._role_candidates(a, "out")
    s_exact = mc.seam_matrix(a, ia, exact, mc._role_candidates(exact, "in")).max()
    s_in = mc.seam_matrix(a, ia, inband, mc._role_candidates(inband, "in")).max()
    s_out = mc.seam_matrix(a, ia, outband, mc._role_candidates(outband, "in")).max()
    check(abs(s_exact - s_in) < 0.05, f"in-band ratio should be ~free ({s_exact:.3f} vs {s_in:.3f})")
    check(s_out < s_in - 0.05, f"out-of-band ratio must be penalised ({s_out:.3f} vs {s_in:.3f})")


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #

def test_scene_cut_frames_are_rejected():
    """A hard cut inside a clip fits as huge, confident, entirely fake motion — it
    would otherwise dominate the gate and produce cuts that land on a cut."""
    c = _desc("c", vx=0.2, scene_cuts=[3.0])
    idx = mc._role_candidates(c, "out")
    near = [t for t in c.t[idx] if abs(t - 3.0) <= mc.SCENE_GUARD]
    check(not near, f"frames within SCENE_GUARD of a scene cut must be rejected: {near}")


def test_head_offset_widens_the_edge_pad():
    """A trimmed intro card's dissolve tail leaks a spurious velocity spike."""
    c = _desc("c", vx=0.2, head_offset=0.4)
    idx = mc._role_candidates(c, "in")
    check(idx.size and c.t[idx].min() >= mc.HEAD_PAD - 1e-9,
          "a clip with a trimmed head must use the larger head pad")


def test_roles_are_asymmetric():
    """out-role needs MIN_SHOT before the frame, in-role needs it after — this is why
    12.5% of pairs are legal in only one direction and W must stay directed."""
    c = _desc("c", vx=0.2, dur=6.0)
    o, i = mc._role_candidates(c, "out"), mc._role_candidates(c, "in")
    check(c.t[o].min() >= mc.EDGE_PAD + mc.MIN_SHOT - 1e-9, "out-role must reserve MIN_SHOT before")
    check(c.t[i].max() <= c.duration - mc.EDGE_PAD - mc.MIN_SHOT + 1e-9, "in-role must reserve MIN_SHOT after")


def _ramp_desc(clip_id, *, dur=8.0, fps=12.0, a0=0.0, a1=math.pi, sp=0.20):
    """A clip whose motion direction SWEEPS over its length — the realistic case, and
    the one that makes the edge matrix genuinely directed."""
    n = int(dur * fps)
    ang = np.linspace(a0, a1, n)
    one = np.ones(n)
    return mc.ClipDesc(
        clip_id=str(clip_id), src_path=f"/fake/{clip_id}.mp4", duration=dur, fps=fps,
        head_offset=0.0, t=np.arange(n) / fps,
        vx=sp * np.cos(ang), vy=sp * np.sin(ang),
        zoom=one * 0.0, roll=one * 0.0, conf=one * 0.9, energy=one * 0.3,
        cx=one * 0.5, cy=one * 0.5, luma=one * 0.5, thumb=np.full((n, 64), 0.5),
        scene_cuts=[],
    )


def test_edge_matrix_is_directed():
    """W[a,b] and W[b,a] are DIFFERENT problems: a's out-role draws on its late frames
    while its in-role draws on its early ones. Guard against anyone 'optimizing' W
    into a symmetric or triangular matrix — and against 2-opt, which reverses
    sub-paths and would flip every internal edge onto a possibly-illegal one.

    Note the score is near-symmetric for CONSTANT motion (cosine, acceleration,
    composition and tone are all symmetric, and the log speed band is symmetric too),
    so this must be exercised with motion that varies along the clip."""
    clips = [_ramp_desc(0, a0=0.0, a1=math.pi),
             _ramp_desc(1, a0=math.pi / 2, a1=-math.pi / 2),
             _ramp_desc(2, a0=math.pi, a1=2 * math.pi)]
    W, outs, ins = mc.build_edges(clips)
    check(W.shape == (3, 3), "W must be n x n")
    off = [(i, j) for i in range(3) for j in range(3) if i != j]
    diffs = [abs(W[i, j] - W[j, i]) for i, j in off if np.isfinite(W[i, j]) and np.isfinite(W[j, i])]
    check(diffs and max(diffs) > 1e-6,
          f"W must be directed — no asymmetric pair found (max |W[i,j]-W[j,i]| = {max(diffs) if diffs else 0:.2e})")
    check(np.all(np.isneginf(np.diag(W))), "a clip must never seam to itself")


# --------------------------------------------------------------------------- #
# The chain DP — the invariant that a naive score-matrix path violates
# --------------------------------------------------------------------------- #

def _chain_clips(n=6):
    rng = np.random.default_rng(3)
    out = []
    for i in range(n):
        ang = rng.uniform(0, 2 * math.pi)
        sp = rng.uniform(0.12, 0.25)
        out.append(_desc(i, dur=rng.uniform(6.0, 10.0),
                         vx=sp * math.cos(ang), vy=sp * math.sin(ang),
                         zoom=rng.uniform(-8, 8), cx=rng.uniform(0.3, 0.7),
                         luma=rng.uniform(0.35, 0.65)))
    return out


def test_chain_shots_are_all_legal():
    """The headline regression: independently-chosen seams produced 7 of 16 shots with
    NEGATIVE length. The chain DP must make that impossible by construction."""
    clips = _chain_clips(6)
    W, outs, ins = mc.build_edges(clips)
    order = mc.order_clips(W, 6)
    shots, _, _ = mc.realize_chain(clips, order, outs, ins)
    for k, (ci, in_t, out_t) in enumerate(shots):
        check(out_t - in_t >= mc.MIN_SHOT - 1e-6,
              f"shot {k} length {out_t - in_t:.3f}s is below MIN_SHOT {mc.MIN_SHOT}")
        check(in_t >= 0.0 and out_t <= clips[ci].duration + 1e-6,
              f"shot {k} [{in_t:.2f},{out_t:.2f}] escapes clip duration {clips[ci].duration:.2f}")


def test_edl_tiling_invariant():
    """moviegen's assembler silently depends on contiguous place_at and
    timeline_duration == sum(durations); plan_cuts enforces the same thing."""
    clips = _chain_clips(6)
    edl, stats = mc.plan_match_cuts(clips, fps=30)
    total = sum(e.duration for e in edl.entries)
    check(abs(edl.timeline_duration - total) < 1e-6,
          f"timeline_duration {edl.timeline_duration:.4f} != sum(durations) {total:.4f}")
    place = 0.0
    for e in edl.entries:
        check(abs(e.place_at - place) < 1e-6, f"place_at not contiguous at {e.place_at:.4f}")
        place += e.duration


def test_no_clip_is_reused():
    """Repetition breaks the very illusion the mode exists to create."""
    clips = _chain_clips(8)
    edl, _ = mc.plan_match_cuts(clips, fps=30)
    ids = [e.clip_id for e in edl.entries]
    check(len(ids) == len(set(ids)), f"clips reused in the chain: {ids}")


def test_target_duration_selects_clip_count():
    clips = _chain_clips(12)
    short, _ = mc.plan_match_cuts(clips, target_duration=15.0, fps=30)
    long_, _ = mc.plan_match_cuts(clips, target_duration=45.0, fps=30)
    check(len(short.entries) < len(long_.entries),
          f"a shorter target must use fewer clips ({len(short.entries)} vs {len(long_.entries)})")


def _late_motion_desc(clip_id, *, dur=10.0, fps=12.0, onset=6.0, sp=0.20):
    """A clip that is static until `onset`, then moves — i.e. every salient frame is
    LATE. Exactly the shape that used to kill a whole plan."""
    n = int(dur * fps)
    t = np.arange(n) / fps
    live = t >= onset
    one = np.ones(n)
    return mc.ClipDesc(
        clip_id=str(clip_id), src_path=f"/fake/{clip_id}.mp4", duration=dur, fps=fps,
        head_offset=0.0, t=t,
        vx=np.where(live, sp, 0.0), vy=np.zeros(n),
        zoom=one * 0.0, roll=one * 0.0,
        conf=np.where(live, 0.9, 0.9), energy=np.where(live, 0.3, 0.0),
        cx=one * 0.5, cy=one * 0.5, luma=one * 0.5, thumb=np.full((n, 64), 0.5),
        scene_cuts=[],
    )


def test_late_motion_first_clip_does_not_kill_the_plan():
    """REGRESSION: the first shot's in-point was PINNED to the clip start while the
    shot was also capped at MAX_EDGE_SHOT. A clip whose salient frames begin after
    that cap made every out-frame illegal, the DP went all -inf, and the whole render
    failed with 'no legal match-cut chain for these clips'. Reproduced on real footage
    with a clip whose out-frames started at 6.17s.

    The in-point must FLOAT back from the chosen out-frame instead."""
    clips = [_late_motion_desc(i, onset=6.0 + 0.4 * i) for i in range(4)]
    edl, stats = mc.plan_match_cuts(clips, fps=30)
    check(len(edl.entries) >= 2, "a late-motion selection must still plan")
    first = edl.entries[0]
    length = first.out_point - first.in_point
    check(length <= mc.MAX_EDGE_SHOT + 1e-6,
          f"first shot {length:.2f}s must still respect MAX_EDGE_SHOT {mc.MAX_EDGE_SHOT}")
    check(length >= mc.MIN_SHOT - 1e-6, f"first shot {length:.2f}s below MIN_SHOT")
    check(first.in_point > mc.EDGE_PAD,
          "the first shot's in-point must float toward its out-frame, not pin to the clip start")


def test_single_burst_clip_still_yields_a_legal_chain():
    """A clip whose salient frames are one tight burst has min(in) > max(out) - MIN_SHOT,
    so it can host no legal shot from salient frames alone. It must fall back to an
    unmatched in-point rather than collapsing the chain — and that must be REPORTED."""
    def burst(cid):
        dur, fps = 8.0, 12.0
        n = int(dur * fps)
        t = np.arange(n) / fps
        live = (t >= 4.0) & (t <= 4.3)          # ~4 frames of motion, nothing else
        one = np.ones(n)
        return mc.ClipDesc(
            clip_id=str(cid), src_path=f"/fake/{cid}.mp4", duration=dur, fps=fps,
            head_offset=0.0, t=t, vx=np.where(live, 0.2, 0.0), vy=np.zeros(n),
            zoom=one * 0.0, roll=one * 0.0, conf=one * 0.9,
            energy=np.where(live, 0.3, 0.0), cx=one * 0.5, cy=one * 0.5,
            luma=one * 0.5, thumb=np.full((n, 64), 0.5), scene_cuts=[])
    clips = [burst(i) for i in range(4)]
    edl, stats = mc.plan_match_cuts(clips, fps=30)
    check(len(edl.entries) >= 2, "a single-burst selection must still plan")
    for i, e in enumerate(edl.entries):
        src = e.out_point - e.in_point
        check(src >= mc.MIN_SHOT - 1e-6, f"shot {i} length {src:.3f}s below MIN_SHOT")
    check("seams_unmatched" in stats, "an unmatched fallback seam must be reported in stats")


def test_clips_too_short_are_dropped():
    tiny = [_desc(i, dur=0.8, vx=0.2) for i in range(3)]
    try:
        mc.plan_match_cuts(tiny, fps=30)
        FAILURES.append("clips too short for one shot must raise, not plan")
    except RuntimeError as exc:
        check("longer than" in str(exc) or "usable motion" in str(exc),
              f"error should name the real cause, got: {exc}")


def test_two_clip_chain_works():
    """Degenerate but legitimate — the sequencer is skipped and both orientations matter."""
    clips = [_desc(0, vx=0.2, dur=8.0), _desc(1, vx=0.2, dur=8.0)]
    edl, stats = mc.plan_match_cuts(clips, fps=30)
    check(len(edl.entries) == 2, f"2 clips must yield 2 shots, got {len(edl.entries)}")


def test_all_static_raises_actionably():
    clips = [_desc(i, vx=0.0, zoom=0.0, roll=0.0) for i in range(4)]
    try:
        mc.plan_match_cuts(clips, fps=30)
        FAILURES.append("an all-static selection must raise, not ship a silent montage")
    except RuntimeError as exc:
        check("no usable motion" in str(exc),
              f"the error must name the real cause, got: {exc}")


# --------------------------------------------------------------------------- #
# Speed solve
# --------------------------------------------------------------------------- #

def test_speeds_stay_inside_the_band():
    """Outside the measured free band a retime reintroduces duplicate-frame judder."""
    clips = _chain_clips(6)
    W, outs, ins = mc.build_edges(clips)
    order = mc.order_clips(W, 6)
    shots, _, _ = mc.realize_chain(clips, order, outs, ins)
    speeds = mc.solve_speeds(clips, shots)
    for k, f in enumerate(speeds):
        check(mc.SPEED_LO - 1e-9 <= f <= mc.SPEED_HI_DEFAULT + 1e-9,
              f"shot {k} speed {f:.3f} escaped [{mc.SPEED_LO}, {mc.SPEED_HI_DEFAULT}]")


def test_speed_never_reads_past_the_end_of_a_clip():
    """At speed > 1 the segment reads MORE source than its slot; slow-mo never did,
    so the existing renderer has no headroom check."""
    clips = _chain_clips(6)
    edl, _ = mc.plan_match_cuts(clips, fps=30)
    for e in edl.entries:
        src = e.in_point + e.duration * e.playback_speed
        clip = next(c for c in clips if c.clip_id == e.clip_id)
        check(src <= clip.duration + 1e-6,
              f"clip {e.clip_id} reads to {src:.3f}s of a {clip.duration:.3f}s source")


def test_disabling_speed_match_yields_unity():
    clips = _chain_clips(6)
    edl, _ = mc.plan_match_cuts(clips, match_speed=False, fps=30)
    check(all(abs(e.playback_speed - 1.0) < 1e-9 for e in edl.entries),
          "match_speed=False must leave every playback_speed at 1.0")


def test_reserving_for_beats_tightens_the_speed_band():
    """The beat-alignment budget and the speed-match budget are the same scalar, so
    when timing also needs the band it must shrink to leave room."""
    clips = _chain_clips(6)
    edl, _ = mc.plan_match_cuts(clips, reserve_speed_for_beats=True, fps=30)
    for e in edl.entries:
        check(mc.SPEED_LO_SONG - 1e-9 <= e.playback_speed <= mc.SPEED_HI_SONG + 1e-9,
              f"with a song, speed {e.playback_speed:.3f} escaped the tightened band")


def test_stats_are_reported():
    clips = _chain_clips(6)
    _, stats = mc.plan_match_cuts(clips, fps=30)
    for k in ("clips_in", "clips_gated", "clips_used", "seam_median",
              "seam_mean_chain", "seam_max_possible"):
        check(k in stats, f"stats must report {k} so a weak run is diagnosable")


# --------------------------------------------------------------------------- #
# ffmpeg-backed: the descriptor extractor
# --------------------------------------------------------------------------- #

def _have_ffmpeg():
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _texture(path: Path, size=1400):
    from PIL import Image
    rng = np.random.default_rng(7)
    Image.fromarray(rng.integers(0, 255, (size, size), dtype=np.uint8)).save(path)


def test_portrait_pan_direction_is_exact():
    """REGRESSION GUARD for the aspect-squeeze bug.

    moviegen's forced scale=320:180 biases measured direction by 15-23 degrees median
    on portrait clips (p90 up to 57 degrees) — larger than the direction term's useful
    tolerance of ~26 degrees. Match mode therefore decodes aspect-preserving. A 45
    degree pan in a 400x736 clip must measure 45 +- 3 degrees."""
    if not _have_ffmpeg():
        SKIPPED.append("test_portrait_pan_direction_is_exact (no ffmpeg)")
        return
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        tex = tdp / "tex.png"
        _texture(tex)
        clip = tdp / "pan.mp4"
        W, H, FPS, PX = 400, 736, 24, 60.0
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-loop", "1", "-framerate", str(FPS), "-t", "3.0",
             "-i", str(tex), "-vf", f"crop={W}:{H}:x='200+{PX}*t':y='200+{PX}*t',fps={FPS}",
             "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", str(clip)], check=True)

        tw, th = mc._analysis_dims(W, H)
        check(abs(tw / th - W / H) < 0.01,
              f"analysis dims {tw}x{th} must preserve the source aspect {W}/{H}")

        d = mc.analyze_clip("0", clip)
        check(d is not None, "analyze_clip returned None for a valid pan")
        if d is None:
            return
        m = slice(4, d.n - 4)
        ang = math.degrees(math.atan2(d.vy[m].mean(), d.vx[m].mean())) % 360
        # The crop window moves +x/+y, so CONTENT moves -x/-y => 225 degrees.
        err = abs((ang - 225.0 + 180) % 360 - 180)
        check(err <= 3.0, f"portrait pan direction off by {err:.1f} deg (measured {ang:.1f})")
        check(abs(np.median(d.zoom[m])) < 1.5, "a pure pan must not register as zoom")
        check(abs(np.median(d.roll[m])) < 1.5, "a pure pan must not register as roll")


def test_zoom_sign_is_correct_on_real_footage():
    """A push-in must read positive and a pull-out negative — a silent sign flip here
    would pair push-ins with pull-outs, the exact opposite of a match."""
    if not _have_ffmpeg():
        SKIPPED.append("test_zoom_sign_is_correct_on_real_footage (no ffmpeg)")
        return
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        tex = tdp / "tex.png"
        _texture(tex)
        FPS, ZR = 24, 0.06

        def mk(name, z):
            p = tdp / name
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-loop", "1", "-framerate", str(FPS), "-t", "3.0",
                 "-i", str(tex), "-vf",
                 f"scale=1280:720,zoompan=z='{z}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                 f":s=640x360:fps={FPS}",
                 "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", "-r", str(FPS), str(p)],
                check=True)
            return p

        push = mc.analyze_clip("0", mk("push.mp4", f"1+{ZR}*on/{FPS}"))
        pull = mc.analyze_clip("1", mk("pull.mp4", f"1.5-{ZR}*on/{FPS}"))
        check(push is not None and pull is not None, "zoom clips failed to analyze")
        if not (push and pull):
            return
        zp = np.median(push.zoom[4:push.n - 4])
        zl = np.median(pull.zoom[4:pull.n - 4])
        check(zp > 2.0, f"a push-in must read clearly positive, got {zp:+.2f} %/s")
        check(zl < -2.0, f"a pull-out must read clearly negative, got {zl:+.2f} %/s")


def test_static_footage_scores_zero_salience():
    if not _have_ffmpeg():
        SKIPPED.append("test_static_footage_scores_zero_salience (no ffmpeg)")
        return
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        tex = tdp / "tex.png"
        _texture(tex)
        p = tdp / "static.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-loop", "1", "-framerate", "24", "-t", "3.0",
             "-i", str(tex), "-vf", "crop=800:450:200:200,scale=640:360,fps=24",
             "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", str(p)], check=True)
        d = mc.analyze_clip("0", p)
        check(d is not None, "static clip failed to analyze")
        if d is None:
            return
        s = d.salience[4:d.n - 4]
        check((s >= mc.SAL_GATE).sum() == 0,
              f"static footage must fail the gate, {(s >= mc.SAL_GATE).sum()}/{s.size} passed")


def _mk_cache(dirpath: Path, n: int, *, size=4096, age_days=0.0, prefix="e"):
    """Fabricate n cache-shaped files with a given last-use age. `prefix` keeps
    separate batches from colliding (they share the shard layout)."""
    import time as _t
    made = []
    for i in range(n):
        f = dirpath / f"{i % 7:02x}" / f"{prefix}{i}.json.gz"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"\x00" * size)
        when = _t.time() - age_days * 86400
        os.utime(f, (when, when))
        made.append(f)
    return made


def test_prune_is_a_noop_under_budget():
    with tempfile.TemporaryDirectory() as td:
        c = Path(td)
        _mk_cache(c, 5, size=1000)
        r = mc.prune_cache(c, max_bytes=10 * 1024 * 1024, max_age_days=90)
        check(r["removed"] == 0, f"nothing should be pruned under budget, removed {r['removed']}")
        check(r["kept"] == 5, f"all 5 entries should survive, kept {r['kept']}")


def test_prune_drops_entries_unused_past_the_age_limit():
    """This is what actually collects orphans: an entry whose source was moved,
    re-synced or deleted can never be hit again, so it ages out."""
    with tempfile.TemporaryDirectory() as td:
        c = Path(td)
        _mk_cache(c, 3, size=1000, age_days=200, prefix="stale")
        _mk_cache(c, 2, size=1000, age_days=0, prefix="fresh")
        r = mc.prune_cache(c, max_bytes=0, max_age_days=90)
        left = list(c.rglob("*.json.gz"))
        check(r["removed"] == 3, f"3 stale entries should go, removed {r['removed']}")
        check(len(left) == 2, f"2 fresh entries should remain, found {len(left)}")


def test_prune_evicts_least_recently_used_until_under_budget():
    with tempfile.TemporaryDirectory() as td:
        c = Path(td)
        import time as _t
        files = []
        for i in range(10):
            f = c / "aa" / f"e{i}.json.gz"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"\x00" * 1000)
            when = _t.time() - (10 - i) * 3600      # e0 oldest ... e9 newest
            os.utime(f, (when, when))
            files.append(f)
        r = mc.prune_cache(c, max_bytes=5000, max_age_days=0)
        left = sorted(p.name for p in c.rglob("*.json.gz"))
        check(r["bytes"] <= 5000, f"cache must end under budget, got {r['bytes']}")
        check(len(left) == 5, f"should keep 5 of 10, kept {left}")
        check(left == ['e5.json.gz', 'e6.json.gz', 'e7.json.gz', 'e8.json.gz', 'e9.json.gz'],
              f"must evict LEAST-recently-used first, kept {left}")


def test_prune_tolerates_a_missing_or_empty_cache():
    with tempfile.TemporaryDirectory() as td:
        missing = Path(td) / "nope"
        r = mc.prune_cache(missing)
        check(r["removed"] == 0 and r["kept"] == 0, "a missing cache dir must be a clean no-op")
        check(mc.prune_cache(None)["removed"] == 0, "None cache dir must be a clean no-op")
        empty = Path(td) / "empty"
        empty.mkdir()
        check(mc.prune_cache(empty)["kept"] == 0, "an empty cache dir must be a clean no-op")


def test_prune_removes_empty_shard_dirs():
    with tempfile.TemporaryDirectory() as td:
        c = Path(td)
        _mk_cache(c, 4, size=1000, age_days=200)
        mc.prune_cache(c, max_bytes=0, max_age_days=90)
        check(not [d for d in c.iterdir() if d.is_dir()],
              "emptied shard dirs should be tidied away")


def test_cache_hit_refreshes_last_use():
    """LRU recency must mean LAST USE, not creation — otherwise a clip you use every
    day is evicted alongside one you analysed once and abandoned."""
    if not _have_ffmpeg():
        SKIPPED.append("test_cache_hit_refreshes_last_use (no ffmpeg)")
        return
    import time as _t
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        tex = tdp / "tex.png"
        _texture(tex, 600)
        clip = tdp / "c.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-loop", "1", "-framerate", "24", "-t", "2.0",
             "-i", str(tex), "-vf", "crop=400:300:x='50+40*t':y=50,fps=24",
             "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", str(clip)], check=True)
        cache = tdp / "cache"
        mc.load_or_analyze("0", clip, cache_dir=cache)
        entry = next(cache.rglob("*.json.gz"))
        old = _t.time() - 30 * 86400
        os.utime(entry, (old, old))
        before = entry.stat().st_mtime
        mc.load_or_analyze("0", clip, cache_dir=cache)          # a HIT
        after = entry.stat().st_mtime
        check(after > before + 86400,
              f"a cache hit must refresh last-use (before {before:.0f}, after {after:.0f})")


def test_cache_roundtrip():
    if not _have_ffmpeg():
        SKIPPED.append("test_cache_roundtrip (no ffmpeg)")
        return
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        tex = tdp / "tex.png"
        _texture(tex, 600)
        clip = tdp / "c.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-loop", "1", "-framerate", "24", "-t", "2.0",
             "-i", str(tex), "-vf", "crop=400:300:x='50+40*t':y=50,fps=24",
             "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", str(clip)], check=True)
        cache = tdp / "cache"
        a = mc.load_or_analyze("0", clip, cache_dir=cache)
        b = mc.load_or_analyze("0", clip, cache_dir=cache)
        check(a is not None and b is not None, "cache roundtrip returned None")
        if a and b:
            check(np.allclose(a.vx, b.vx, atol=1e-5), "cached vx differs from freshly computed")
            check(abs(a.duration - b.duration) < 1e-6, "cached duration differs")
            check(a.scene_cuts == b.scene_cuts, "cached scene_cuts differ")


# --------------------------------------------------------------------------- #

def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        try:
            t()
        except Exception as exc:  # a raising test is a failing test
            import traceback
            FAILURES.append(f"{t.__name__} raised: {exc}\n{traceback.format_exc(limit=3)}")
    print(f"ran {len(tests)} tests")
    for s in SKIPPED:
        print(f"  SKIP {s}")
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
