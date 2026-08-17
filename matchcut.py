"""Motion Match Cut — splice clips where their MOTION aligns, so movement appears to
continue across the cut.

This is the second render mode of the Generate Movie job (see ``moviegen.py``); the
song is OPTIONAL here because the edit is driven by motion continuity rather than a
beat grid. The pipeline is:

    analyze_clip()   per clip: one aspect-preserving decode -> per-frame motion
                     descriptors (translation, zoom, roll, confidence, composition,
                     tone) via FFT phase correlation. numpy only, no OpenCV.
    build_edges()    score every ordered clip pair's best seam, gated hard on motion
                     salience.
    order_clips()    greedy multi-start + or-opt over a SUBSET of clips.
    realize_chain()  1-D DP that re-picks every seam frame jointly, so shot lengths
                     are legal by construction.
    plan_match_cuts()-> an EDL that moviegen's existing render/assemble stages consume
                     unchanged.

Everything here is measured against this library's real footage — see
``specs/match-cut-spec.md`` for the numbers behind each constant. Three findings drive
the design and are easy to get wrong:

  * The footage is nearly STATIC (median 0.019 frame-heights/s) and ZOOM is the
    dominant cue, not translation. Zoom/roll are first-class match axes; without them
    only 28/64 clips are usable, with them 52/64.
  * Without a hard salience gate the scorer degenerates into a tone matcher and
    happily "matches" two motionless frames — and reports a healthy score while doing
    it. The gate is not optional.
  * Per-edge argmax seams are NOT valid path weights: clip B's in-point comes from the
    edge into it and its out-point from the edge out of it, so independently-chosen
    seams produce negative-length shots. Hence the chain DP.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

import moviegen
from moviegen import EDL, EDLEntry, MotionCurve

# --------------------------------------------------------------------------- #
# Tunables (see specs/match-cut-spec.md for the measurements behind each)
# --------------------------------------------------------------------------- #

ANALYSIS_FPS = 12          # descriptor sampling rate; heading half-life is ~350ms
BOX_W, BOX_H = 320, 180    # analysis frames fit INSIDE this box, aspect PRESERVED.
                           # moviegen's forced scale=320:180 squeeze biases direction
                           # by 15-23 deg on portrait clips — fatal for a direction
                           # matcher, harmless for its own scalar frame-diffs.
TILE_COLS, TILE_ROWS = 4, 3   # phase-correlation grid -> 12 local shifts per frame pair

# Reference normalizers. Velocity is in frame-HEIGHTS/s (resolution- and
# framing-invariant), zoom in %/s, roll in deg/s.
V_REF = 0.10
Z_REF = 6.0
R_REF = 8.0

SMOOTH_TAPS = 3            # 250ms boxcar on velocity/zoom/roll; NONE on luma/thumb
                           # (those describe the instant of the cut)

# Hard gate. Below this a "match" is two static look-alike frames.
SAL_GATE = 0.55
CONF_FLOOR = 0.30
ENERGY_FLOOR = 0.012

# Seam-score term weights. MAXS is the ceiling used to express relative thresholds.
W_DIR, W_SPEED, W_ACC, W_COMP, W_TONE = 1.00, 0.80, 0.35, 0.45, 0.30
MAXS = W_DIR + W_SPEED + W_ACC + W_COMP + W_TONE   # 2.90

# Free speed band a setpts retime can absorb at no visible cost. The lower bound is
# the judder limit (at 0.80 roughly 1 frame in 3 is duplicated by the fps filter); the
# upper bound is out_fps/src_fps, where the pulldown becomes a clean 1:1.
SPEED_LO = 0.80
SPEED_HI_DEFAULT = 1.25
# When a song IS supplied the scorer's band is tightened so the remainder of the
# budget stays available for beat alignment (they are the same scalar).
SPEED_LO_SONG, SPEED_HI_SONG = 0.90, 1.10
# Ridge weight pulling every shot back toward real time in the speed solve. The chain
# constraint alone fixes only the DIFFERENCES between consecutive speeds, so without
# this a small consistent bias walks the whole chain onto the clamp.
SPEED_REG = 0.25

# Geometry guards.
EDGE_PAD = 0.25            # never seam this close to a clip edge
MIN_SHOT = 0.60            # a shot shorter than this reads as a glitch
MAX_EDGE_SHOT = 3.50       # cap the first/last shot, which are otherwise unconstrained
SCENE_GUARD = 0.167        # 2 analysis frames either side of an internal shot change:
                           # a hard cut fits as huge, confident, entirely fake "motion"
HEAD_PAD = 0.50            # larger pad when an intro card was trimmed — its dissolve
                           # tail leaks a spurious velocity spike

MAX_CAND = 32              # top-N candidate frames per role per clip, by salience.
                           # Bounds pair scoring at MAX_CAND^2 per ordered pair.
ACCEPT_PCTL = 0.60         # keep the top 40% of legal edges; thresholds MUST be
                           # relative — the achievable max moves with the library.

TIMELINE_PER_CLIP = 2.7    # measured seconds of timeline per clip, for L = T / this
# Shot-count ceiling when no target duration is given: use the WHOLE gated pool, so
# 300 matchable clips really do become a ~13-minute sequence. That is only affordable
# because or-opt evaluates moves by O(1) delta (see _or_opt) — with the original
# O(L^3) rescore this was minutes of pure Python. The ceiling is a runaway guard, not
# a product decision; an explicit target duration still overrides it downward.
DEFAULT_MAX_SHOTS = 600

CACHE_VERSION = 2          # bump to invalidate every cached descriptor
                           # (v2: head_offset comes from the content-based
                           # sheet detector, shifting every trimmed timeline)

# Descriptor-cache hygiene. Entries are ~60 KB/clip, so a fully-analysed 2,000-clip
# library is ~120 MB — but nothing bounds it, and every re-sync or move of a clip
# strands its old entry forever (the key includes size+mtime+path).
#
# Recency is LAST USE, not creation: a cache HIT touches the file's mtime. That one
# detail makes plain LRU subsume orphan collection — an entry whose source was
# deleted, moved or re-downloaded is simply never touched again, so it ages out
# naturally. No need to store source paths or decompress anything to find orphans.
CACHE_MAX_MB = int(os.environ.get("GROK_MOTION_CACHE_MB", "512") or 512)
CACHE_MAX_AGE_DAYS = int(os.environ.get("GROK_MOTION_CACHE_DAYS", "90") or 90)


ProgressFn = Callable[..., None]


# --------------------------------------------------------------------------- #
# Data contracts
# --------------------------------------------------------------------------- #

@dataclass
class ClipDesc:
    """Per-frame motion descriptors for one clip, in TRIMMED clip time (an intro
    character-sheet card is cut off the head first, exactly as analyze_motion does).
    All arrays are parallel and length N."""
    clip_id: str
    src_path: str
    duration: float            # trimmed
    fps: float
    head_offset: float
    t: np.ndarray              # (N,) seconds
    vx: np.ndarray             # (N,) frame-heights/s
    vy: np.ndarray
    zoom: np.ndarray           # (N,) %/s   (+ = pushing in)
    roll: np.ndarray           # (N,) deg/s
    conf: np.ndarray           # (N,) 0..1
    energy: np.ndarray         # (N,) normalized mean-abs frame diff
    cx: np.ndarray             # (N,) motion centroid, 0..1
    cy: np.ndarray
    luma: np.ndarray           # (N,) 0..1
    thumb: np.ndarray          # (N, 64) 0..1
    scene_cuts: list[float] = field(default_factory=list)

    @property
    def n(self) -> int:
        return int(self.t.shape[0])

    @property
    def speed(self) -> np.ndarray:
        return np.hypot(self.vx, self.vy)

    @property
    def salience(self) -> np.ndarray:
        """Combined motion salience. Zoom and roll count, not just translation —
        this library's motion is mostly push-in, and a translation-only measure finds
        nothing in the median clip."""
        base = np.maximum.reduce([
            self.speed / V_REF,
            np.abs(self.zoom) / Z_REF,
            np.abs(self.roll) / R_REF,
        ])
        return base * np.clip(self.conf / 0.5, 0.0, 1.5)

    def accel(self) -> tuple[np.ndarray, np.ndarray]:
        """d/dt of the (already smoothed) velocity, in frame-heights/s^2."""
        return np.gradient(self.vx) * self.fps, np.gradient(self.vy) * self.fps


# --------------------------------------------------------------------------- #
# Stage 1 — decode + descriptors
# --------------------------------------------------------------------------- #

def _probe_wh(src: Path) -> tuple[int, int]:
    """Source pixel dimensions, or (0, 0) if unprobeable. Never raises."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(src)],
            capture_output=True, text=True, timeout=60).stdout.strip()
        w, h = out.split(",")[:2]
        return int(w), int(h)
    except Exception:
        return 0, 0


def _analysis_dims(w: int, h: int) -> tuple[int, int]:
    """Largest even WxH fitting inside the analysis box while PRESERVING aspect.
    Aspect preservation is the whole point — see the module docstring."""
    if w <= 0 or h <= 0:
        return BOX_W, BOX_H
    if w * BOX_H >= h * BOX_W:          # wider than the box -> width-limited
        tw = BOX_W
        th = max(2, int(round(h * BOX_W / w)))
    else:                                # taller -> height-limited
        th = BOX_H
        tw = max(2, int(round(w * BOX_H / h)))
    return tw - (tw % 2), th - (th % 2)


def _decode_gray(src: Path, tw: int, th: int, hwaccel: bool,
                 timeout_s: float = 900.0) -> np.ndarray:
    """Decode the whole clip to (N, th, tw) uint8 grayscale at ANALYSIS_FPS.

    Same raw-pipe technique as moviegen._gray_diffs (and the same kill-watchdog, so a
    wedged read on a network mount can't block forever), but with an
    aspect-preserving scale and its own fps."""
    cmd = ["ffmpeg", "-v", "error"]
    if hwaccel:
        cmd += ["-hwaccel", "cuda"]
    cmd += ["-i", str(src),
            "-vf", f"fps={ANALYSIS_FPS},scale={tw}:{th},format=gray",
            "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert proc.stdout is not None
    watchdog = threading.Timer(timeout_s, proc.kill)
    watchdog.start()
    frame_bytes = tw * th
    chunks: list[bytes] = []
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            chunks.append(buf)
    finally:
        watchdog.cancel()
        proc.stdout.close()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    if not chunks:
        return np.zeros((0, th, tw), np.uint8)
    return np.frombuffer(b"".join(chunks), np.uint8).reshape(-1, th, tw)


def _tile_view(frames: np.ndarray, tw: int, th: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Split each frame into a TILE_ROWS x TILE_COLS grid.

    Returns (tiles, tx, ty) where tiles is (N, T, h, w) and tx/ty are each tile's
    centre offset from the frame centre, in pixels — the geometry the similarity fit
    needs to turn 12 local shifts into (dx, dy, zoom, roll)."""
    h = th // TILE_ROWS
    w = tw // TILE_COLS
    n = frames.shape[0]
    cropped = frames[:, :h * TILE_ROWS, :w * TILE_COLS]
    tiles = (cropped.reshape(n, TILE_ROWS, h, TILE_COLS, w)
                    .transpose(0, 1, 3, 2, 4)
                    .reshape(n, TILE_ROWS * TILE_COLS, h, w))
    ry, rx = np.meshgrid(np.arange(TILE_ROWS), np.arange(TILE_COLS), indexing="ij")
    tx = ((rx.ravel() + 0.5) * w - tw / 2.0)
    ty = ((ry.ravel() + 0.5) * h - th / 2.0)
    return tiles, tx, ty


def _parabolic(c0: np.ndarray, c1: np.ndarray, c2: np.ndarray) -> np.ndarray:
    """Sub-pixel peak offset in [-0.5, 0.5] from three samples straddling the peak."""
    denom = (c0 - 2.0 * c1 + c2)
    with np.errstate(divide="ignore", invalid="ignore"):
        d = 0.5 * (c0 - c2) / denom
    return np.clip(np.nan_to_num(d, nan=0.0, posinf=0.0, neginf=0.0), -0.5, 0.5)


def _phase_shift(prev: np.ndarray, cur: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Batched phase correlation over a stack of tile pairs.

    ``prev``/``cur`` are (T, h, w) float32. Returns (ux, uy, sharp): the sub-pixel
    displacement of CONTENT from prev to cur, per tile, plus a normalized correlation
    peak height used as a per-tile confidence weight."""
    t, h, w = prev.shape
    wy = np.hanning(h).astype(np.float32)[:, None]
    wx = np.hanning(w).astype(np.float32)[None, :]
    win = wy * wx
    a = (prev - prev.mean(axis=(1, 2), keepdims=True)) * win
    b = (cur - cur.mean(axis=(1, 2), keepdims=True)) * win
    A = np.fft.rfft2(a, axes=(-2, -1))
    B = np.fft.rfft2(b, axes=(-2, -1))
    R = B * np.conj(A)
    mag = np.abs(R)
    mag[mag < 1e-9] = 1e-9
    corr = np.fft.irfft2(R / mag, s=(h, w), axes=(-2, -1))
    flat = corr.reshape(t, -1)
    idx = np.argmax(flat, axis=1)
    py, px = np.divmod(idx, w)
    peak = flat[np.arange(t), idx]
    # Sub-pixel refine along each axis (wrap-around neighbours).
    ym1 = corr[np.arange(t), (py - 1) % h, px]
    yp1 = corr[np.arange(t), (py + 1) % h, px]
    xm1 = corr[np.arange(t), py, (px - 1) % w]
    xp1 = corr[np.arange(t), py, (px + 1) % w]
    dy = py.astype(np.float64) + _parabolic(ym1, peak, yp1)
    dx = px.astype(np.float64) + _parabolic(xm1, peak, xp1)
    # Unwrap the circular correlation to signed displacements.
    dy = np.where(dy > h / 2.0, dy - h, dy)
    dx = np.where(dx > w / 2.0, dx - w, dx)
    # Peak height relative to the correlation's own noise floor.
    sharp = peak / (np.abs(corr).mean(axis=(1, 2)) + 1e-9)
    return dx, dy, sharp


def _fit_similarity(ux: np.ndarray, uy: np.ndarray, wgt: np.ndarray,
                    tx: np.ndarray, ty: np.ndarray) -> tuple[float, float, float, float, float]:
    """Weighted least-squares fit of a 4-DOF similarity to the per-tile shifts:

        ux = dx + s*tx - r*ty
        uy = dy + s*ty + r*tx

    Returns (dx, dy, s, r, conf) in pixels / per-frame fractions / radians, with conf
    from the residual relative to the fitted magnitude."""
    t = ux.shape[0]
    # Rows: 2 per tile, columns [dx, dy, s, r].
    A = np.zeros((2 * t, 4))
    A[0::2, 0] = 1.0
    A[0::2, 2] = tx
    A[0::2, 3] = -ty
    A[1::2, 1] = 1.0
    A[1::2, 2] = ty
    A[1::2, 3] = tx
    y = np.empty(2 * t)
    y[0::2] = ux
    y[1::2] = uy
    sw = np.repeat(np.sqrt(np.maximum(wgt, 0.0)), 2)
    try:
        sol, *_ = np.linalg.lstsq(A * sw[:, None], y * sw, rcond=None)
    except np.linalg.LinAlgError:
        return 0.0, 0.0, 0.0, 0.0, 0.0
    resid = A @ sol - y
    wsum = sw.sum() + 1e-9
    rms = float(np.sqrt((sw * resid * resid).sum() / wsum))
    scale = float(np.hypot(sol[0], sol[1])) + abs(sol[2]) * max(tx.max(), ty.max(), 1.0)
    conf = float(np.clip(1.0 / (1.0 + rms / max(scale, 0.35)), 0.0, 1.0))
    return float(sol[0]), float(sol[1]), float(sol[2]), float(sol[3]), conf


def _boxcar(a: np.ndarray, taps: int) -> np.ndarray:
    """Centred moving average, edge-padded. Kept short (250ms) — the measured
    direction coherence window is only ~350ms, so a wider window smears across it."""
    if taps <= 1 or a.shape[0] < taps:
        return a
    k = np.ones(taps) / taps
    pad = taps // 2
    return np.convolve(np.pad(a, pad, mode="edge"), k, mode="valid")[:a.shape[0]]


def analyze_clip(clip_id: str, src: Path, hwaccel_decode: bool = False) -> ClipDesc | None:
    """One decode pass -> full motion descriptors. Returns None for a clip that can't
    be used (undecodable, too short, or no motion signal at all).

    Head trimming runs FIRST on the raw diffs, exactly as moviegen.analyze_motion
    does, so every time in the returned descriptor is in TRIMMED clip time and
    ``head_offset`` carries the add-back for the render's seek."""
    sw, sh = _probe_wh(src)
    tw, th = _analysis_dims(sw, sh)
    frames = _decode_gray(src, tw, th, hwaccel_decode)
    if frames.shape[0] < 4:
        return None

    f = frames.astype(np.float32)
    diffs = np.abs(np.diff(f, axis=0)).mean(axis=(1, 2))       # RAW 0-255 scale

    # Cut a character-sheet intro card off the head before anything else — its exit
    # spike is often the clip's peak and would otherwise poison normalization AND
    # look like the strongest motion in the library. (Own small decode — the
    # content-based detector needs frames, not our diffs; non-carded clips only
    # pay for its 2-frame gate.)
    head = moviegen.detect_head_trim(src, hwaccel_decode=hwaccel_decode)
    duration = moviegen.probe_duration(src)
    if head > 0 and duration - head >= moviegen.HEAD_MIN_REMAIN_S:
        drop = int(round(head * ANALYSIS_FPS))
        f = f[drop:]
        diffs = diffs[drop:]
        duration -= head
    else:
        head = 0.0
    if f.shape[0] < 4:
        return None

    tiles, tx, ty = _tile_view(f, tw, th)
    n_pairs = tiles.shape[0] - 1
    dxs = np.zeros(n_pairs); dys = np.zeros(n_pairs)
    ss = np.zeros(n_pairs); rr = np.zeros(n_pairs); cc = np.zeros(n_pairs)
    # Per-tile texture: a flat tile's phase correlation is meaningless, so it should
    # not vote in the similarity fit.
    texture = tiles.std(axis=(2, 3))
    for i in range(n_pairs):
        ux, uy, sharp = _phase_shift(tiles[i], tiles[i + 1])
        wgt = np.clip(texture[i] / 12.0, 0.0, 1.0) * np.clip(sharp / 6.0, 0.0, 1.0)
        if wgt.sum() <= 1e-6:
            continue
        dxs[i], dys[i], ss[i], rr[i], cc[i] = _fit_similarity(ux, uy, wgt, tx, ty)

    fps = float(ANALYSIS_FPS)
    # Normalize translation by frame HEIGHT -> frame-heights/s, so a 400x736 portrait
    # and a 1280x720 landscape are directly comparable.
    vx = _boxcar(dxs / th * fps, SMOOTH_TAPS)
    vy = _boxcar(dys / th * fps, SMOOTH_TAPS)
    zoom = _boxcar(ss * fps * 100.0, SMOOTH_TAPS)
    roll = _boxcar(rr * fps * (180.0 / math.pi), SMOOTH_TAPS)
    conf = _boxcar(cc, SMOOTH_TAPS)

    peak = float(diffs.max()) if diffs.size else 0.0
    energy = (diffs / peak) if peak > 0 else diffs * 0.0
    energy = energy[:n_pairs]

    # Motion centroid: where in frame the movement is. Straight off the abs-diff we
    # already have, so it costs nothing.
    ad = np.abs(np.diff(f, axis=0))
    tot = ad.sum(axis=(1, 2)) + 1e-6
    ys = np.arange(th, dtype=np.float32)[None, :, None]
    xs = np.arange(tw, dtype=np.float32)[None, None, :]
    cy = (ad * ys).sum(axis=(1, 2)) / tot / max(th - 1, 1)
    cx = (ad * xs).sum(axis=(1, 2)) / tot / max(tw - 1, 1)

    # Tone: mean luma + an 8x8 mean-pooled thumbnail. NOT smoothed — these describe
    # the instant of the cut, and blurring them hides a real luma jump.
    luma = f[:n_pairs].mean(axis=(1, 2)) / 255.0
    hh, ww = (th // 8) * 8, (tw // 8) * 8
    thumb = (f[:n_pairs, :hh, :ww]
             .reshape(n_pairs, 8, hh // 8, 8, ww // 8)
             .mean(axis=(2, 4)).reshape(n_pairs, 64) / 255.0)

    # Reuse moviegen's scene-cut detector on our own energy curve so an internal shot
    # change is guarded identically to the beat pipeline.
    curve = MotionCurve(clip_id=clip_id, src_path=str(src), duration=duration,
                        fps_analyzed=fps, samples=[float(x) for x in energy])

    return ClipDesc(
        clip_id=clip_id, src_path=str(src), duration=duration, fps=fps,
        head_offset=head,
        t=np.arange(n_pairs, dtype=np.float64) / fps,
        vx=vx[:n_pairs], vy=vy[:n_pairs], zoom=zoom[:n_pairs], roll=roll[:n_pairs],
        conf=conf[:n_pairs], energy=energy, cx=cx[:n_pairs], cy=cy[:n_pairs],
        luma=luma, thumb=thumb, scene_cuts=list(curve.scene_cuts),
    )


# --------------------------------------------------------------------------- #
# Descriptor cache
# --------------------------------------------------------------------------- #

_ARRAYS = ("t", "vx", "vy", "zoom", "roll", "conf", "energy", "cx", "cy", "luma", "thumb")


def _cache_path(cache_dir: Path, key: str) -> Path:
    import hashlib
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return cache_dir / h[:2] / f"{h}.json.gz"


def _cache_key(src: Path) -> str:
    """Identity of the FILE, not the media id — so a re-imported or moved clip still
    hits, and an edited one misses."""
    try:
        st = src.stat()
        return f"v{CACHE_VERSION}:{src.resolve()}:{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        return f"v{CACHE_VERSION}:{src}"


def load_or_analyze(clip_id: str, src: Path, *, cache_dir: Path | None = None,
                    hwaccel_decode: bool = False) -> ClipDesc | None:
    """analyze_clip with a disk cache. Analysis is the slow stage and its result is a
    pure function of the file, so re-planning the same basket is ~free — which is what
    makes interactive re-planning viable later. Cache failures are never fatal."""
    cp = None
    if cache_dir is not None:
        cp = _cache_path(cache_dir, _cache_key(src))
        try:
            if cp.exists():
                with gzip.open(cp, "rt", encoding="utf-8") as fh:
                    d = json.load(fh)
                # Mark last-use so prune_cache's LRU reflects actual usage — this is
                # what lets stranded entries (source moved/re-synced/deleted) age out
                # without ever having to look at the source files.
                try:
                    os.utime(cp, None)
                except OSError:
                    pass
                return ClipDesc(
                    clip_id=clip_id, src_path=str(src), duration=d["duration"],
                    fps=d["fps"], head_offset=d["head_offset"],
                    scene_cuts=d["scene_cuts"],
                    **{k: np.asarray(d[k], dtype=np.float64) for k in _ARRAYS})
        except Exception:
            pass
    desc = analyze_clip(clip_id, src, hwaccel_decode)
    if desc is not None and cp is not None:
        try:
            cp.parent.mkdir(parents=True, exist_ok=True)
            payload = {"duration": desc.duration, "fps": desc.fps,
                       "head_offset": desc.head_offset, "scene_cuts": desc.scene_cuts}
            for k in _ARRAYS:
                payload[k] = np.asarray(getattr(desc, k)).round(6).tolist()
            tmp = cp.with_suffix(".tmp")
            with gzip.open(tmp, "wt", encoding="utf-8") as fh:
                json.dump(payload, fh)
            tmp.replace(cp)
        except Exception:
            pass
    return desc


def prune_cache(cache_dir: Path | None, *, max_bytes: int | None = None,
                max_age_days: int | None = None) -> dict:
    """Bound the descriptor cache. Returns {removed, freed, kept, bytes}.

    Two passes, both driven purely by ``st_mtime`` (which ``load_or_analyze`` refreshes
    on every hit, so it means LAST USE):

      1. AGE — drop anything unused for ``max_age_days``. This is what actually
         collects orphans: a clip that was moved, re-synced or deleted can never be
         hit again, so its entry simply ages out.
      2. SIZE — if still over ``max_bytes``, evict least-recently-used until under.

    Costs one stat() per entry, no decompression. Set either bound to 0 to disable it.
    Best-effort throughout: a cache is derived data, so pruning must never be able to
    break a render."""
    empty = {"removed": 0, "freed": 0, "kept": 0, "bytes": 0}
    if cache_dir is None:
        return empty
    try:
        if not cache_dir.exists():
            return empty
        entries = []
        for f in cache_dir.rglob("*.json.gz"):
            try:
                st = f.stat()
            except OSError:
                continue
            entries.append([st.st_mtime, st.st_size, f])
    except OSError:
        return empty

    max_bytes = (CACHE_MAX_MB * 1024 * 1024) if max_bytes is None else max_bytes
    max_age_days = CACHE_MAX_AGE_DAYS if max_age_days is None else max_age_days
    removed = freed = 0

    def _drop(rec) -> bool:
        nonlocal removed, freed
        try:
            rec[2].unlink()
        except OSError:
            return False
        removed += 1
        freed += rec[1]
        return True

    # 1. age
    kept = entries
    if max_age_days > 0:
        cutoff = time.time() - max_age_days * 86400
        kept = [e for e in entries if not (e[0] < cutoff and _drop(e))]

    # 2. size (least-recently-used first)
    total = sum(e[1] for e in kept)
    if max_bytes > 0 and total > max_bytes:
        kept.sort(key=lambda e: e[0])
        still = []
        for e in kept:
            if total > max_bytes and _drop(e):
                total -= e[1]
            else:
                still.append(e)
        kept = still

    # tidy empty shard dirs so the tree doesn't fill with 256 empty folders
    try:
        for d in cache_dir.iterdir():
            if d.is_dir():
                try:
                    d.rmdir()          # only succeeds when empty
                except OSError:
                    pass
    except OSError:
        pass

    return {"removed": removed, "freed": freed, "kept": len(kept), "bytes": total}


# --------------------------------------------------------------------------- #
# Stage 2 — candidate frames + seam scoring
# --------------------------------------------------------------------------- #

def _role_candidates(c: ClipDesc, role: str) -> np.ndarray:
    """Indices of frames that may serve as an out-frame (``role='out'``) or in-frame
    (``role='in'``), gated hard on motion salience and geometry.

    The roles are ASYMMETRIC — an out-frame needs MIN_SHOT of source before it and an
    in-frame needs MIN_SHOT after — which is why 12.5% of clip pairs are legal in only
    one direction and the edge matrix must stay directed."""
    head_pad = HEAD_PAD if c.head_offset > 0 else EDGE_PAD
    lo, hi = head_pad, c.duration - EDGE_PAD
    if role == "out":
        lo = max(lo, head_pad + MIN_SHOT)
    else:
        hi = min(hi, c.duration - EDGE_PAD - MIN_SHOT)
    ok = (c.t >= lo) & (c.t <= hi)
    ok &= c.salience >= SAL_GATE
    ok &= c.conf >= CONF_FLOOR
    ok &= c.energy >= ENERGY_FLOOR
    for sc in c.scene_cuts:
        ok &= np.abs(c.t - sc) > SCENE_GUARD
    idx = np.flatnonzero(ok)
    if idx.size > MAX_CAND:
        # Keep the most salient, but spread across the clip rather than clustered in
        # one burst — variety of moments matters more than a marginal score.
        order = idx[np.argsort(-c.salience[idx])]
        keep: list[int] = []
        min_sep = max(1, idx.size // (2 * MAX_CAND))
        for i in order:
            if all(abs(int(i) - j) >= min_sep for j in keep):
                keep.append(int(i))
            if len(keep) >= MAX_CAND:
                break
        idx = np.array(sorted(keep), dtype=int) if keep else idx[:MAX_CAND]
    return idx


def seam_matrix(a: ClipDesc, ia: np.ndarray, b: ClipDesc, ib: np.ndarray,
                *, speed_hi: float = SPEED_HI_DEFAULT) -> np.ndarray:
    """Vectorized seam scores for every (out-frame of a) x (in-frame of b) pair.

    Returns (len(ia), len(ib)). Every term is in [0,1] before weighting, so the score
    tops out at MAXS. The gate has already been applied by _role_candidates, so the
    degenerate static-vs-static match cannot appear here."""
    if ia.size == 0 or ib.size == 0:
        return np.zeros((ia.size, ib.size))

    avx, avy = a.vx[ia][:, None], a.vy[ia][:, None]
    bvx, bvy = b.vx[ib][None, :], b.vy[ib][None, :]
    aspd, bspd = np.hypot(avx, avy), np.hypot(bvx, bvy)

    # --- direction: translation, zoom and roll, each weighted by how much of THAT
    # cue is actually present on both sides. Sign agreement is mandatory for zoom and
    # roll — a push-in must meet a push-in, never a pull-out.
    wv = np.minimum(aspd, bspd) / (np.minimum(aspd, bspd) + 0.03)
    cos = (avx * bvx + avy * bvy) / (aspd * bspd + 1e-9)
    az, bz = a.zoom[ia][:, None], b.zoom[ib][None, :]
    ar, br = a.roll[ia][:, None], b.roll[ib][None, :]
    wz = np.clip(np.minimum(np.abs(az), np.abs(bz)) / Z_REF, 0.0, 1.0)
    wr = np.clip(np.minimum(np.abs(ar), np.abs(br)) / R_REF, 0.0, 1.0)
    sgn_z = np.where(np.sign(az) == np.sign(bz), 1.0, -0.5)
    sgn_r = np.where(np.sign(ar) == np.sign(br), 1.0, -0.5)
    fz = np.exp(-(((az - bz) / (0.5 * Z_REF)) ** 2)) * sgn_z
    fr = np.exp(-(((ar - br) / (0.5 * R_REF)) ** 2)) * sgn_r
    s_dir = (wv * cos + wz * fz + wr * fr) / (wv + wz + wr + 1e-9)

    # --- speed: free inside the band a setpts retime can absorb.
    r = bspd / (aspd + 1e-9)
    over = np.maximum.reduce([np.zeros_like(r),
                              np.log(np.maximum(r / speed_hi, 1e-9)),
                              np.log(np.maximum(SPEED_LO / np.maximum(r, 1e-9), 1e-9))])
    s_speed = np.exp(-((over / 0.35) ** 2)) * wv + (1.0 - wv) * 0.5

    # --- acceleration: match the derivative too, else the motion visibly hitches
    # across the cut even when the instantaneous velocity agrees.
    aax, aay = a.accel()
    bax, bay = b.accel()
    da = np.hypot(aax[ia][:, None] - bax[ib][None, :],
                  aay[ia][:, None] - bay[ib][None, :])
    s_acc = np.exp(-((da / 0.25) ** 2))

    # --- composition: keep the moving subject in roughly the same part of frame.
    dc = np.hypot(a.cx[ia][:, None] - b.cx[ib][None, :],
                  a.cy[ia][:, None] - b.cy[ib][None, :])
    s_comp = np.exp(-((dc / 0.28) ** 2))

    # --- tone: don't flash. Weak thumbnail term — compatible framing, not the same shot.
    dl = np.abs(a.luma[ia][:, None] - b.luma[ib][None, :])
    dt = np.sqrt(((a.thumb[ia][:, None, :] - b.thumb[ib][None, :, :]) ** 2).mean(axis=2))
    s_tone = 0.5 * np.exp(-((dl / 0.15) ** 2)) + 0.5 * np.exp(-((dt / 0.22) ** 2))

    return (W_DIR * s_dir + W_SPEED * s_speed + W_ACC * s_acc
            + W_COMP * s_comp + W_TONE * s_tone)


# --------------------------------------------------------------------------- #
# Stage 3 — edges
# --------------------------------------------------------------------------- #

def build_edges(clips: list[ClipDesc], *, speed_hi: float = SPEED_HI_DEFAULT,
                progress: ProgressFn | None = None) -> tuple[np.ndarray, list, list]:
    """Best-seam score for every ORDERED clip pair.

    Returns (W, outs, ins) where W[i][j] is the optimistic best seam from clip i into
    clip j (-inf if none is legal), and outs[i]/ins[i] are that clip's candidate frame
    indices for each role.

    W is deliberately an UPPER BOUND used only for ordering — the seam frames it
    implies are not jointly realizable (see realize_chain)."""
    n = len(clips)
    outs = [_role_candidates(c, "out") for c in clips]
    ins = [_role_candidates(c, "in") for c in clips]
    W = np.full((n, n), -np.inf)
    for i in range(n):
        if progress and n:
            progress(i / n)
        if outs[i].size == 0:
            continue
        for j in range(n):
            if i == j or ins[j].size == 0:
                continue
            m = seam_matrix(clips[i], outs[i], clips[j], ins[j], speed_hi=speed_hi)
            if m.size:
                W[i, j] = float(m.max())
    return W, outs, ins


def accept_threshold(W: np.ndarray, pctl: float = ACCEPT_PCTL) -> float:
    """Acceptance as a PERCENTILE of the observed edges, never an absolute constant —
    the achievable maximum moves with the gate and the library, so a hard-coded
    threshold selects 5% of pairs in one configuration and 45% in another."""
    finite = W[np.isfinite(W)]
    if finite.size == 0:
        return -np.inf
    return float(np.quantile(finite, pctl))


# --------------------------------------------------------------------------- #
# Stage 4 — ordering (max-weight simple path over a SUBSET)
# --------------------------------------------------------------------------- #

def _path_score(W: np.ndarray, path: list[int]) -> float:
    return float(sum(W[path[k], path[k + 1]] for k in range(len(path) - 1)))


def order_clips(W: np.ndarray, L: int, *, starts: int = 5,
                rng: np.random.Generator | None = None) -> list[int]:
    """Greedy multi-start + or-opt over a SUBSET of clips.

    This is NOT a Hamiltonian path: forcing every selected clip in collapses per-seam
    quality ~42%, because clips with no usable seam poison the chain wherever they
    land. L nodes are chosen from the pool.

    Greedy alone is 1.7-25.9% below optimal; the or-opt polish is what closes the gap
    (measured 0.0-0.3% versus exact Held-Karp at n=12..18, 130x faster). 2-opt is
    deliberately NOT used: the graph is directed, so reversing a sub-path flips every
    internal edge onto possibly-illegal reversed ones."""
    n = W.shape[0]
    L = max(2, min(L, n))
    rng = rng or np.random.default_rng(0)

    # Seed starts with the nodes having the strongest outgoing edges.
    best_out = np.where(np.isfinite(W), W, -np.inf).max(axis=1)
    order0 = list(np.argsort(-best_out))
    cands = order0[:max(starts, 1)]

    best: list[int] = []
    best_s = -np.inf
    for s0 in cands:
        path = [int(s0)]
        used = {int(s0)}
        while len(path) < L:
            last = path[-1]
            row = W[last].copy()
            row[list(used)] = -np.inf
            nxt = int(np.argmax(row))
            if not np.isfinite(row[nxt]):
                break
            path.append(nxt)
            used.add(nxt)
        if len(path) < 2:
            continue
        path = _or_opt(W, path, n)
        sc = _path_score(W, path)
        if sc > best_s:
            best_s, best = sc, path
    if not best:
        # Nothing legal — fall back to the strongest-outgoing nodes in order so the
        # caller can still render a best-effort sequence of plain hard cuts.
        best = [int(i) for i in order0[:L]]
    return best


_ILLEGAL = -1e6     # finite stand-in for -inf so deltas stay arithmetic (inf - inf = nan)


def _or_opt(W: np.ndarray, path: list[int], n: int, rounds: int = 60) -> list[int]:
    """Local search: move a run of 1-3 nodes elsewhere, or swap in an unused clip.

    Or-opt touches exactly 3 edges and preserves direction — unlike 2-opt, which
    reverses a sub-path and would flip every internal edge onto a possibly-illegal
    reversed one on this directed graph. Adding 2-opt on top measurably changed nothing.

    Every move is evaluated by its DELTA in O(1) rather than by rescoring the whole
    path, and the inner insertion-position loop is vectorized over numpy. Rescoring
    was O(L) inside an O(L^2) scan, i.e. O(L^3) per round: fine at L=20, and about
    three orders of magnitude too slow at L=300, which is the size a 308-clip
    selection actually asks for.

    Illegal edges are carried as a large finite penalty, not -inf, so subtracting a
    removed edge can never produce inf-inf = nan."""
    Wf = np.where(np.isfinite(W), W, _ILLEGAL)
    P = list(path)
    L = len(P)
    if L < 3:
        return P

    for _ in range(rounds):
        best_delta, best_move = 1e-9, None

        # --- segment moves: lift P[i:i+m] and reinsert it at every position
        for m in (1, 2, 3):
            if m >= L:
                break
            for i in range(L - m + 1):
                # cost of closing the gap the lift leaves behind
                base = 0.0
                if i > 0:
                    base -= Wf[P[i - 1], P[i]]
                if i + m < L:
                    base -= Wf[P[i + m - 1], P[i + m]]
                if i > 0 and i + m < L:
                    base += Wf[P[i - 1], P[i + m]]
                rest = np.fromiter((P[t] for t in range(L) if not (i <= t < i + m)),
                                   dtype=np.int64, count=L - m)
                nr = rest.shape[0]
                s0, s1 = P[i], P[i + m - 1]
                # interior insertion points j = 1..nr-1, vectorized
                if nr >= 2:
                    a, b = rest[:-1], rest[1:]
                    d = base - Wf[a, b] + Wf[a, s0] + Wf[s1, b]
                    k = int(np.argmax(d))
                    if d[k] > best_delta:
                        best_delta, best_move = float(d[k]), ("seg", i, m, k + 1)
                # j = 0 (front) and j = nr (back)
                d0 = base + Wf[s1, rest[0]]
                if d0 > best_delta:
                    best_delta, best_move = float(d0), ("seg", i, m, 0)
                dn = base + Wf[rest[-1], s0]
                if dn > best_delta:
                    best_delta, best_move = float(dn), ("seg", i, m, nr)

        # --- swap in an unused clip (the path is over a SUBSET, so this matters)
        unused = np.setdiff1d(np.arange(n), np.asarray(P, dtype=np.int64),
                              assume_unique=False)
        if unused.size:
            for pos in range(L):
                cur_c = P[pos]
                d = np.zeros(unused.shape[0])
                if pos > 0:
                    d += Wf[P[pos - 1], unused] - Wf[P[pos - 1], cur_c]
                if pos < L - 1:
                    d += Wf[unused, P[pos + 1]] - Wf[cur_c, P[pos + 1]]
                k = int(np.argmax(d))
                if d[k] > best_delta:
                    best_delta, best_move = float(d[k]), ("swap", pos, int(unused[k]))

        if best_move is None:
            break
        if best_move[0] == "seg":
            _, i, m, j = best_move
            seg = P[i:i + m]
            rest = P[:i] + P[i + m:]
            P = rest[:j] + seg + rest[j:]
        else:
            _, pos, u = best_move
            P = list(P)
            P[pos] = u
    return P


# --------------------------------------------------------------------------- #
# Stage 5 — realization (the chain DP)
# --------------------------------------------------------------------------- #

def realize_chain(clips: list[ClipDesc], order: list[int], outs: list, ins: list,
                  *, speed_hi: float = SPEED_HI_DEFAULT) -> tuple[list[tuple[int, float, float]], float, int]:
    """Jointly re-pick every seam frame so shot lengths are legal BY CONSTRUCTION.

    Returns ([(clip_index, in_time, out_time)], total_seam_score, n_unmatched_seams).

    This exists because per-edge argmax seams are not jointly realizable: clip B's
    in-point comes from the edge into it and its out-point from the edge out of it,
    and nothing couples them. Measured on a real 16-clip chain, 7 of 16 shots came out
    with NEGATIVE length while every individual seam scored 2.57-2.67.

    dp[k][b] = best score for shots 0..k with shot k entering at candidate b. The
    transition takes a prefix max over in-candidates (both frame lists are sorted), so
    each step is O(M_out * M_in)."""
    L = len(order)
    if L < 2:
        raise RuntimeError("match-cut chain needs at least 2 clips")

    # Shot 0's in-point is FREE and is derived at backtrack from whichever out-frame
    # wins: max(start_lo, out - MAX_EDGE_SHOT). It never enters a seam score (only the
    # seam OUT of clip 0 does), so a single placeholder DP state is exact.
    #
    # It MUST float. Pinning it to the clip start while also capping the first shot at
    # MAX_EDGE_SHOT makes the cap unsatisfiable whenever clip 0's salient motion is
    # late — every out-frame is rejected, dp goes all -inf, and the whole plan fails
    # with "no legal match-cut chain". Measured: a clip whose out-frames begin at
    # 6.17s killed a 10-clip selection outright.
    first = clips[order[0]]
    start_lo = HEAD_PAD if first.head_offset > 0 else EDGE_PAD
    in_t: list[np.ndarray] = [np.array([start_lo])]
    # Parallel mask marking GEOMETRIC FALLBACK in-points (see below).
    in_fb: list[np.ndarray] = [np.array([False])]
    for k in range(1, L):
        c = clips[order[k]]
        idx = ins[order[k]]
        lo = HEAD_PAD if c.head_offset > 0 else EDGE_PAD
        times = c.t[idx] if idx.size else np.empty(0)
        # Every clip also gets its earliest legal in-point as a fallback candidate.
        # Without one, a clip whose salient frames are a single tight burst has
        # min(in) > max(out) - MIN_SHOT and can host no legal shot at all, which
        # again collapses the whole chain. Heavily penalised below, so it is only
        # ever chosen when nothing matched — the best-effort tier the spec calls for,
        # rather than an error.
        in_t.append(np.concatenate([[lo], times]))
        in_fb.append(np.concatenate([[True], np.zeros(times.shape[0], bool)]))

    dp = [np.zeros(in_t[0].shape[0])]
    back: list[np.ndarray] = []          # (M_in_next, 2) -> (prev_in_idx, out_idx)
    n_fallback = 0

    for k in range(L - 1):
        a, b = clips[order[k]], clips[order[k + 1]]
        oidx = outs[order[k]]
        if oidx.size == 0:
            # No salient out-frame: allow any frame that leaves a legal shot, so the
            # chain degrades to a plain hard cut here instead of failing outright.
            oidx = np.flatnonzero((a.t >= start_lo + MIN_SHOT) & (a.t <= a.duration - EDGE_PAD))
            if oidx.size == 0:
                oidx = np.array([a.n - 1])
        out_times = a.t[oidx]
        nxt_in = in_t[k + 1]
        nxt_fb = in_fb[k + 1]
        # Score against the fallback candidate too — it is a real frame of clip b, it
        # simply wasn't salient enough to be a candidate on its own.
        nxt_idx = np.clip(np.searchsorted(b.t, nxt_in), 0, max(b.n - 1, 0))
        S = seam_matrix(a, oidx, b, nxt_idx, speed_hi=speed_hi)
        if S.shape != (oidx.size, nxt_in.size):
            S = np.zeros((oidx.size, nxt_in.size))
        # Push fallback in-points below every genuine candidate so they are a last
        # resort, never a silently-preferred unmatched cut.
        S = S - MAXS * nxt_fb[None, :]

        # prefix max of dp[k] over in-candidates with in_time <= out_time - MIN_SHOT
        prev_in = in_t[k]
        order_prev = np.argsort(prev_in)
        sorted_in = prev_in[order_prev]
        sorted_dp = dp[k][order_prev]
        run_max = np.maximum.accumulate(sorted_dp)
        run_arg = np.zeros(sorted_dp.shape[0], dtype=int)
        bestv = -np.inf; besti = 0
        for m in range(sorted_dp.shape[0]):
            if sorted_dp[m] > bestv:
                bestv, besti = sorted_dp[m], m
            run_arg[m] = besti

        limit = out_times - MIN_SHOT
        pos = np.searchsorted(sorted_in, limit, side="right") - 1
        valid = pos >= 0
        pm = np.where(valid, run_max[np.clip(pos, 0, None)], -np.inf)
        pm_arg = np.where(valid, order_prev[run_arg[np.clip(pos, 0, None)]], 0)

        # NOTE: the first shot is capped at MAX_EDGE_SHOT by TRIMMING ITS IN-POINT at
        # backtrack, never by rejecting out-frames — see the in_t[0] comment.

        tot = pm[:, None] + S                      # (M_out, M_in_next)
        bi = np.argmax(tot, axis=0)
        dp.append(tot[bi, np.arange(tot.shape[1])])
        back.append(np.stack([pm_arg[bi], oidx[bi]], axis=1))

    # Terminal: the last shot's out-point is free; take MAX_EDGE_SHOT or the clip end.
    last = clips[order[-1]]
    fin = int(np.argmax(dp[-1]))
    total = float(dp[-1][fin])
    if not np.isfinite(total):
        raise RuntimeError("no legal match-cut chain for these clips")

    shots: list[tuple[int, float, float]] = [(0, 0.0, 0.0)] * L
    b_idx = fin
    n_fallback = int(in_fb[L - 1][b_idx])
    last_in = float(in_t[L - 1][b_idx])
    last_out = min(last_in + MAX_EDGE_SHOT, last.duration - EDGE_PAD)
    if last_out - last_in < MIN_SHOT:
        last_out = min(last_in + MIN_SHOT, last.duration)
    shots[L - 1] = (order[L - 1], last_in, last_out)
    for k in range(L - 2, -1, -1):
        prev_i, out_i = back[k][b_idx]
        out_t = float(clips[order[k]].t[out_i])
        if k == 0:
            # Shot 0's in-point floats back from its out-frame, capped at
            # MAX_EDGE_SHOT so the opening shot can't run away.
            in_tk = max(start_lo, out_t - MAX_EDGE_SHOT)
        else:
            in_tk = float(in_t[k][prev_i])
            n_fallback += int(in_fb[k][prev_i])
        shots[k] = (order[k], in_tk, out_t)
        b_idx = int(prev_i)
    return shots, total, n_fallback


# --------------------------------------------------------------------------- #
# Stage 6 — speed solve
# --------------------------------------------------------------------------- #

def solve_speeds(clips: list[ClipDesc], shots: list[tuple[int, float, float]],
                 *, lo: float = SPEED_LO, hi: float = SPEED_HI_DEFAULT) -> list[float]:
    """Per-shot playback speeds that make apparent velocity continuous across seams.

    Shot k plays at f_k, so its apparent velocity is |v|*f_k. Continuity at seam k
    wants |vA|*f_k == |vB|*f_{k+1}, i.e. log f_{k+1} - log f_k = -log r_k. That is a
    tridiagonal least-squares with box constraints, solved here by clamped coordinate
    descent (cheap, and the chain is short).

    Residuals are weighted by how much TRANSLATION is actually present — matching
    speed between two zoom-dominant frames is meaningless.

    A speed > 1 makes the segment read MORE source than the slot length, so every
    result is clamped against the source actually available after the in-point."""
    L = len(shots)
    if L < 2:
        return [1.0] * L
    logs = np.zeros(L)
    tgt = np.zeros(L - 1)
    wts = np.zeros(L - 1)
    for k in range(L - 1):
        ci, _, out_t = shots[k]
        cj, in_t, _ = shots[k + 1]
        a, b = clips[ci], clips[cj]
        ia = int(np.clip(np.searchsorted(a.t, out_t), 0, a.n - 1))
        ib = int(np.clip(np.searchsorted(b.t, in_t), 0, b.n - 1))
        sa = float(np.hypot(a.vx[ia], a.vy[ia]))
        sb = float(np.hypot(b.vx[ib], b.vy[ib]))
        if sa <= 1e-6 or sb <= 1e-6:
            continue
        tgt[k] = -math.log(max(sb / sa, 1e-6))
        wts[k] = min(sa, sb) / (min(sa, sb) + 0.03)

    llo, lhi = math.log(lo), math.log(hi)
    for _ in range(60):
        for k in range(L):
            # Ridge pull toward 1.0. Without it the chain is only determined up to a
            # constant, so a consistent bias in the ratios walks every shot out to the
            # clamp — and the floor of the band is the JUDDER limit (at 0.80 roughly a
            # third of frames are duplicated by the fps pulldown). Retime only as far
            # as a seam actually needs.
            num, den = 0.0, SPEED_REG
            if k < L - 1 and wts[k] > 0:
                num += wts[k] * (logs[k + 1] - tgt[k]); den += wts[k]
            if k > 0 and wts[k - 1] > 0:
                num += wts[k - 1] * (logs[k - 1] + tgt[k - 1]); den += wts[k - 1]
            if den > 0:
                logs[k] = min(lhi, max(llo, num / den))
    logs -= logs.mean()
    speeds = [float(min(hi, max(lo, math.exp(x)))) for x in logs]

    # Source-headroom clamp: at speed f the render reads (out-in) seconds of source
    # from in_point, which must exist. Slow-mo never needed this because it pulls LESS.
    for k, (ci, in_t, out_t) in enumerate(shots):
        avail = clips[ci].duration - in_t
        need = out_t - in_t
        if need > avail:
            speeds[k] = 1.0
    return speeds


# --------------------------------------------------------------------------- #
# Planner entry point
# --------------------------------------------------------------------------- #

def plan_match_cuts(clips: list[ClipDesc], *, target_duration: float | None = None,
                    reserve_speed_for_beats: bool = False, seed: int | None = None,
                    match_speed: bool = True,
                    transition_frames: int = 0, fps: int = 30,
                    progress: ProgressFn | None = None) -> tuple[EDL, dict]:
    """Plan a motion-match-cut sequence. Returns (edl, stats).

    ``stats`` reports what the analysis actually found so a weak run is diagnosable
    rather than silently shipping tasteful hard cuts."""
    if len(clips) < 2:
        raise RuntimeError("Motion Match Cut needs at least 2 usable clips")
    # A clip shorter than one padded minimum shot can never host a legal shot, so it
    # must not reach the sequencer.
    min_len = EDGE_PAD + MIN_SHOT + EDGE_PAD
    clips = [c for c in clips if c.duration >= min_len]
    if len(clips) < 2:
        raise RuntimeError("Motion Match Cut needs at least 2 clips longer than "
                           f"{min_len:.1f}s")

    # playback_speed is ONE scalar per shot and it has two possible jobs: matching
    # apparent velocity across a seam, and (once beat snapping exists) moving a shot's
    # timeline length onto a grid. When both are in play the band must be split, or
    # they fight and push shots outside it into visible judder. Today only velocity
    # matching runs, so the full band is available.
    speed_hi = SPEED_HI_SONG if reserve_speed_for_beats else SPEED_HI_DEFAULT
    speed_lo = SPEED_LO_SONG if reserve_speed_for_beats else SPEED_LO

    W, outs, ins = build_edges(clips, speed_hi=speed_hi, progress=progress)
    gated = sum(1 for o in outs if o.size)
    legal = int(np.isfinite(W).sum())
    if gated < 2 or legal == 0:
        raise RuntimeError(
            f"{len(clips) - gated} of {len(clips)} clips have no usable motion — "
            "Motion Match Cut needs footage that pans, pushes in or rotates")

    thr = accept_threshold(W)
    pool = int(sum(1 for o, i in zip(outs, ins) if o.size or i.size))
    L = (max(2, min(pool, round(float(target_duration) / TIMELINE_PER_CLIP)))
         if target_duration else max(2, min(pool, DEFAULT_MAX_SHOTS)))

    rng = np.random.default_rng(seed if seed is not None else 0)
    order = order_clips(W, L, rng=rng)
    shots, total, n_unmatched = realize_chain(clips, order, outs, ins, speed_hi=speed_hi)

    speeds = (solve_speeds(clips, shots, lo=speed_lo, hi=speed_hi)
              if match_speed else [1.0] * len(shots))

    td = (transition_frames / float(fps)) if transition_frames > 0 else 0.0
    entries: list[EDLEntry] = []
    place = 0.0
    for k, (ci, in_t, out_t) in enumerate(shots):
        c = clips[ci]
        src_len = max(MIN_SHOT, out_t - in_t)
        f = speeds[k]
        dur = src_len / f                     # timeline length; source read stays src_len
        # A transition borrows td/2 of source either side of the join, so a shot must
        # have that much slack before its in-point and after its out-point.
        can_trans = (td > 0 and k > 0
                     and in_t - td / 2.0 >= 0.0
                     and out_t + td / 2.0 <= c.duration)
        entries.append(EDLEntry(
            clip_id=c.clip_id, src_path=c.src_path,
            in_point=in_t, out_point=out_t, duration=dur, place_at=place,
            transition=("dissolve" if can_trans else "cut"),
            transition_dur=(td if can_trans else 0.0),
            playback_speed=f, head_offset=c.head_offset,
        ))
        place += dur

    edl = EDL(timeline_duration=place, entries=entries)
    finite = W[np.isfinite(W)]
    stats = {
        "clips_in": len(clips),
        "clips_gated": gated,
        "clips_used": len(shots),
        "edges_legal": legal,
        "seam_median": round(float(np.median(finite)), 3) if finite.size else 0.0,
        "seam_p95": round(float(np.quantile(finite, 0.95)), 3) if finite.size else 0.0,
        "seam_mean_chain": round(total / max(1, len(shots) - 1), 3),
        "seam_max_possible": round(MAXS, 2),
        # Seams that fell back to an unmatched in-point because the clip had no
        # salient frame that could host a legal shot. >0 means part of the sequence
        # is an ordinary cut, not a motion match — surfaced rather than hidden.
        "seams_unmatched": n_unmatched,
        "accept_threshold": round(thr, 3) if np.isfinite(thr) else 0.0,
        "speed_ramped": sum(1 for f in speeds if abs(f - 1.0) > 0.01),
    }
    return edl, stats
