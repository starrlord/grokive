"""Auto Montage clip selection for Grokive.

Given a song's BeatGrid and a candidate pool (typically the whole library or a
few chosen collections), pick the clips that best serve THIS song — using only
cached artifacts: the beat grid comes from beat_cache and each clip's raw motion
diffs from motion_cache, so scoring thousands of candidates decodes nothing.

This is deliberately a PRE-SELECTION layer, not a planner: ``plan_cuts`` already
does the per-slot matching (motion-to-energy fit, beat alignment, overuse
spreading). What it cannot do is decide which clips are in the room, and with a
whole library in the room its overuse math degrades (max_uses collapses to 1 and
every clip is "fresh", so nothing spreads deliberately). Selection therefore
answers three questions the planner can't:

  1. HOW MANY clips this song wants — estimated slot count / SLOTS_PER_CLIP, so
     reuse stays mild without flooding the planner with thousands of curves.
  2. WHAT MIX of energies — the pool's calm/mid/hot split mirrors where the
     song's cut slots actually are (a wall-to-wall banger gets mostly hot
     footage; a ballad mostly calm), measured in slots, not seconds.
  3. WHICH clips — within each band, the strongest windowed motion, penalising
     dead footage and hidden scene cuts, with the top "hero" clips always
     included (plan_cuts reserves one for the drop).

Kinetics only: motion says nothing about subject or look. Thematic selection
(the clips' generation prompts + Prompt Studio embeddings) is a future layer on
top of this one.
"""

from __future__ import annotations

import random
from pathlib import Path

import moviegen
from moviegen import BeatGrid, _clamp, _lerp, _smooth

# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #

POOL_MIN = 12            # never hand the planner fewer than this (if available)
POOL_MAX = 120           # ...or more: beyond this the planner slows and the
                         # extra clips can't all appear anyway
SLOTS_PER_CLIP = 2.0     # target ~1 clip per 2 slots -> overuse_ratio ~0.5, the
                         # mild region of every preset's reuse penalty
BAND_EDGES = (0.35, 0.65)  # smoothed song energy -> calm / mid / hot slots
MIN_CLIP_S = 1.0         # too short to fill any realistic beat slot
DEAD_RAW_MEAN = 0.8      # raw mean-abs-diff (0-255) below this = static footage;
                         # RAW is the one absolute-kinetics signal we have (the
                         # pipeline's normalized curves are per-clip relative, so
                         # a uniformly-dead clip normalizes to mean ~1.0 and
                         # would otherwise read as busy)
W_PEAK = 1.0             # band-fit quality: strongest windowed motion wins...
W_DUR = 0.15             # ...longer clips give the planner more windows...
W_SCENE = 0.30           # ...hidden internal cuts cost (scene-aware presets)
W_DEAD = 1.0             # ...and static footage is heavily demoted
HERO_COUNT = 2           # top peak-motion clips always included (drop slam fuel)
EXPLORE_OVERSAMPLE = 2   # with a seed, sample each band's quota from the top
                         # quota*this candidates (weighted) instead of strict top


def _estimate_slots(grid: BeatGrid, tightness: float, T: float) -> tuple[int, list[float]]:
    """Approximate plan_cuts' density boundary walk: how many cut slots this song
    yields at this tightness, and each slot's governing (smoothed) energy. An
    estimate — machine-gun onsets and accent snapping add a few more — but band
    shares and pool sizing only need the shape, not exactness."""
    beats = [b for b in grid.beats if b.time < T]
    if not beats:
        return 1, [0.5]
    energies = _smooth([b.energy for b in beats], moviegen.ENERGY_SMOOTH)
    slots: list[float] = []
    i = 0
    while i < len(beats):
        slots.append(energies[i])
        step = int(_clamp(round(_lerp(4, 1, tightness) / max(energies[i], 0.25)), 1, 8))
        i += step
    return max(1, len(slots)), slots


def select_clips(candidates: list[Path], grid: BeatGrid, cache_dir: Path | None, *,
                 tightness: float, target_duration: float | None,
                 scene_aware: bool = True, allow_stills: bool = False,
                 seed: int | None = None) -> tuple[list[Path], dict]:
    """Choose the clips that best serve this song. Returns (chosen_paths, stats).

    Deterministic for seed=None (strict best per band, index tie-break); a seed
    samples within each band's top candidates so re-rolls stay good but differ.
    Uncached candidates are skipped and counted — coverage converges via the
    warm-up / sync — and fewer than 2 analyzed candidates is a hard error that
    tells the user to run Analyze Library.
    """
    T = float(target_duration or grid.duration)
    tightness = _clamp(tightness, 0.0, 1.0)
    n_slots, slot_energies = _estimate_slots(grid, tightness, T)

    # --- features per analyzed candidate (pure cache reads) ----------------- #
    feats: list[dict] = []
    skipped_uncached = 0
    skipped_short = 0
    for idx, p in enumerate(candidates):
        if moviegen._is_image(p):
            continue  # stills ride along via manual selection only (picvideo)
        hit = moviegen._load_motion_diffs(p, cache_dir) if cache_dir else None
        if hit is None:
            skipped_uncached += 1
            continue
        raw, duration, head = hit
        curve = moviegen._curve_from_raw(str(idx), p, raw, duration, head)
        if curve.duration < MIN_CLIP_S or not curve.samples:
            skipped_short += 1
            continue
        # RAW stats over the trimmed region (drop the intro card's samples so a
        # character sheet's exit spike doesn't read as motion).
        raw_trim = raw[round(curve.head_offset * moviegen.ANALYSIS_FPS):] or raw
        n = len(raw_trim)
        raw_mean = sum(raw_trim) / n
        raw_peak = sum(sorted(raw_trim)[max(0, n - max(1, n // 10)):]) / max(1, n // 10)
        feats.append({
            "idx": idx, "path": p, "duration": curve.duration,
            "raw_mean": raw_mean, "raw_peak": raw_peak,
            "scene_cuts": len(curve.scene_cuts),
        })

    if len(feats) < 2:
        raise RuntimeError(
            f"auto-pick needs analyzed clips: {len(feats)} of {len(candidates)} "
            f"candidates are in the motion cache — run Analyze Library (or Sync, "
            f"which warms it) and try again")

    # --- demand: what mix of energies do this song's slots want? ------------ #
    lo, hi = BAND_EDGES
    demand = [0, 0, 0]  # calm / mid / hot, in SLOTS (loud sections cut faster,
    for e in slot_energies:  # so they consume proportionally more clips)
        demand[0 if e < lo else (1 if e < hi else 2)] += 1
    total_demand = sum(demand) or 1

    # --- supply: band candidates by the LIBRARY's own motion distribution --- #
    # Absolute thresholds would misread a uniformly-gentle or uniformly-wild
    # library; rank terciles adapt the bands to what's actually available.
    by_motion = sorted(feats, key=lambda f: (f["raw_mean"], f["idx"]))
    third = max(1, len(by_motion) // 3)
    bands: list[list[dict]] = [by_motion[:third], by_motion[third:2 * third],
                               by_motion[2 * third:]]

    peak_hi = max(f["raw_peak"] for f in feats) or 1.0

    def quality(f: dict) -> float:
        q = (W_PEAK * (f["raw_peak"] / peak_hi)
             + W_DUR * min(1.0, f["duration"] / 10.0)
             - W_DEAD * (1.0 if f["raw_mean"] < DEAD_RAW_MEAN else 0.0))
        if scene_aware and f["scene_cuts"]:
            q -= W_SCENE
        return q

    # --- pool size + per-band quotas ---------------------------------------- #
    pool_target = int(_clamp(round(n_slots / SLOTS_PER_CLIP), POOL_MIN, POOL_MAX))
    pool_target = min(pool_target, len(feats))
    quotas = [round(pool_target * d / total_demand) for d in demand]
    while sum(quotas) < pool_target:   # fix rounding against the largest band
        quotas[demand.index(max(demand))] += 1
    while sum(quotas) > pool_target:
        quotas[quotas.index(max(quotas))] -= 1

    rng = random.Random(seed)
    chosen: dict[int, dict] = {}

    def take(band: list[dict], want: int) -> int:
        avail = sorted((f for f in band if f["idx"] not in chosen),
                       key=lambda f: (-quality(f), f["idx"]))
        if not avail or want <= 0:
            return 0
        if seed is not None and len(avail) > want:
            picked = []
            top = avail[:max(want, min(len(avail), want * EXPLORE_OVERSAMPLE))]
            base = min(quality(f) for f in top)
            pool = list(top)
            for _ in range(min(want, len(pool))):
                f = moviegen._weighted_choice(
                    rng, pool, [(quality(x) - base) + 0.1 for x in pool])
                picked.append(f)
                pool.remove(f)
        else:
            picked = avail[:want]
        for f in picked:
            chosen[f["idx"]] = f
        return len(picked)

    for band, want in zip(bands, quotas):
        take(band, want)
    # Underfilled bands (library skews away from the song) spill into neighbours
    # so the pool still reaches its target size.
    short = pool_target - len(chosen)
    if short > 0:
        take(sorted(feats, key=lambda f: (-quality(f), f["idx"])), short)
    # Hero guarantee: the strongest windowed-motion clips are always available
    # for plan_cuts' drop reservation, whatever band math said.
    for f in sorted(feats, key=lambda x: (-x["raw_peak"], x["idx"]))[:HERO_COUNT]:
        chosen[f["idx"]] = f

    picked = sorted(chosen.values(), key=lambda f: f["idx"])
    stats = {
        "candidates": len(candidates), "analyzed": len(feats),
        "skipped_uncached": skipped_uncached, "skipped_short": skipped_short,
        "picked": len(picked), "est_slots": n_slots,
        "bands": {"calm": demand[0], "mid": demand[1], "hot": demand[2]},
        "picked_indices": [f["idx"] for f in picked],
    }
    return [f["path"] for f in picked], stats
