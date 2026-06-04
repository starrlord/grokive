"""Beat-synced montage generator for Grokive.

Takes a list of source video files plus a music track and produces a tight,
beat-synced montage where visual motion peaks land on musical beats. The four
analysis stages are pure Python; the render stage shells out to ffmpeg.

This module is intentionally self-contained and decoupled from ``server.py``:
the caller passes in a ``progress`` callback, the resolved ffmpeg *video encode
args* (so NVENC/CPU selection stays in one place — ``server._video_encode_args``),
and whether CUDA hardware decode is available. Nothing here imports Flask.

Pipeline (mirrors the five stages in docs/generate-movie-spec.md):

    AudioAnalyzer   -> BeatGrid          (librosa: tempo, beats, RMS, sections)
    MotionAnalyzer  -> MotionCurve/clip  (ffmpeg frame-diff, prefix-sum scoring)
    CutPlanner      -> EDL               (the differentiator — see plan_cuts)
    SegmentRenderer -> normalized clips  (ffmpeg trim/scale/pad, uniform W/H/fps)
    assemble        -> final mp4         (concat segments + mux the song over them)

v1 simplifications vs. the spec (all documented there as acceptable fallbacks):
  * No madmom/essentia — assume 4/4 and tag every 4th beat a downbeat.
  * No OpenCV — CPU frame-diff straight off an ffmpeg raw-gray pipe (numpy only).
  * Half-beat cutting is disabled (needs onset detection); min one beat per cut.
"""

from __future__ import annotations

import bisect
import json
import os
import random
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# librosa JIT-compiles parts of itself with numba, which by default caches the
# compiled functions *next to its own source files* in site-packages. In the
# container the app runs as a non-root PUID/PGID user for whom site-packages is
# read-only, so numba raises "no locator available". Redirect its cache to a
# writable dir before librosa (hence numba) is ever imported. setdefault lets a
# deployment override it (the Docker image sets NUMBA_CACHE_DIR explicitly).
os.environ.setdefault("NUMBA_CACHE_DIR", str(Path(tempfile.gettempdir()) / "grokive-numba"))

# --------------------------------------------------------------------------- #
# Tunables (overridable via the options dict / server config)
# --------------------------------------------------------------------------- #

ANALYSIS_FPS = 8          # motion sampling rate; full fps is unnecessary
ANALYSIS_W = 320          # downscaled frame size for motion analysis
ANALYSIS_H = 180

# Planner weights (see spec §5.3). Lower-is-better penalties are subtracted.
W_MOTION = 1.0
W_INTENSITY = 0.5        # match clip motion to the song's local energy
W_RECENT = 0.8
W_OVERUSE = 0.5
RECENT_K = 2              # don't reuse a clip within this many cuts
ENERGY_SMOOTH = 5         # beats; smooths per-beat energy so a build reads as a ramp
TOP_WINDOWS_K = 3         # candidate motion windows per clip to choose among
TOP_CANDIDATES_K = 3      # top-scoring clips to choose among when exploring

# Overall-progress weighting so the bar doesn't stall on the slow stage.
STAGE_WEIGHTS = {"analyzing_audio": 0.10, "analyzing_motion": 0.45,
                 "planning": 0.05, "rendering": 0.40}
_STAGE_ORDER = ["analyzing_audio", "analyzing_motion", "planning", "rendering"]


ProgressFn = Callable[..., None]


# --------------------------------------------------------------------------- #
# Data contracts
# --------------------------------------------------------------------------- #

@dataclass
class Beat:
    time: float          # seconds
    is_downbeat: bool
    energy: float        # 0..1 normalized
    section_id: int


@dataclass
class Section:
    id: int
    start: float
    end: float
    intensity: float     # 0..1, drives cut density


@dataclass
class BeatGrid:
    duration: float
    tempo: float
    beats: list[Beat]
    sections: list[Section]


@dataclass
class MotionCurve:
    clip_id: str
    src_path: str
    duration: float
    fps_analyzed: float
    samples: list[float] = field(default_factory=list)  # normalized 0..1
    _prefix: list[float] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        # Prefix sums for O(1) window means.
        acc = 0.0
        self._prefix = [0.0]
        for s in self.samples:
            acc += s
            self._prefix.append(acc)

    def best_window(self, length: float) -> tuple[float, float, float]:
        """Best-motion window of ``length`` seconds.

        Returns (mean_motion, peak_time, window_start) in seconds. Falls back to
        the whole clip when it's shorter than ``length``.
        """
        fps = self.fps_analyzed
        n = max(1, round(length * fps))
        if not self.samples or n >= len(self.samples):
            mean = (self._prefix[-1] / len(self.samples)) if self.samples else 0.0
            peak = self._peak_time(0, len(self.samples))
            return mean, peak, 0.0
        best_mean, best_i = -1.0, 0
        for i in range(0, len(self.samples) - n + 1):
            mean = (self._prefix[i + n] - self._prefix[i]) / n
            if mean > best_mean:
                best_mean, best_i = mean, i
        peak = self._peak_time(best_i, best_i + n)
        return best_mean, peak, best_i / fps

    def top_windows(self, length: float, k: int = 3) -> list[tuple[float, float, float]]:
        """Up to ``k`` strong, well-separated motion windows of ``length`` seconds,
        each (mean_motion, peak_time, window_start). Lets the planner pick a
        *different* good moment from the same clip across runs."""
        fps = self.fps_analyzed
        n = max(1, round(length * fps))
        if not self.samples or n >= len(self.samples):
            return [self.best_window(length)]
        means = sorted(
            ((self._prefix[i + n] - self._prefix[i]) / n, i)
            for i in range(0, len(self.samples) - n + 1)
        )
        means.reverse()
        chosen: list[tuple[float, int]] = []
        min_sep = max(1, n // 2)
        for mean, i in means:
            if all(abs(i - j) >= min_sep for _, j in chosen):
                chosen.append((mean, i))
            if len(chosen) >= k:
                break
        return [(mean, self._peak_time(i, i + n), i / fps) for mean, i in chosen]

    def _peak_time(self, lo: int, hi: int) -> float:
        if not self.samples:
            return 0.0
        hi = min(hi, len(self.samples))
        seg = self.samples[lo:hi] or self.samples
        local = max(range(len(seg)), key=lambda k: seg[k])
        return (lo + local) / self.fps_analyzed


@dataclass
class EDLEntry:
    clip_id: str
    src_path: str
    in_point: float
    out_point: float
    duration: float
    place_at: float       # position in the final timeline


@dataclass
class EDL:
    timeline_duration: float
    entries: list[EDLEntry]

    def to_json(self) -> str:
        return json.dumps({
            "timeline_duration": self.timeline_duration,
            "entries": [vars(e) for e in self.entries],
        }, indent=2)


# --------------------------------------------------------------------------- #
# ffmpeg helpers (self-contained; the caller supplies encode args)
# --------------------------------------------------------------------------- #

def _run(cmd: list[str], what: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "").strip()[-400:] or f"ffmpeg failed ({what})")


def probe_duration(path: Path) -> float:
    """Container duration in seconds (0.0 if unknown)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float((out.stdout or "0").strip() or 0.0)
    except Exception:
        return 0.0


# --------------------------------------------------------------------------- #
# Stage 1 — audio analysis -> BeatGrid
# --------------------------------------------------------------------------- #

def analyze_audio(song_path: Path) -> BeatGrid:
    """librosa beat grid + per-beat energy + energy-threshold sections.

    Downbeats are approximated as every 4th beat (assume 4/4); madmom-quality
    downbeats are a documented enhancement, not part of v1.
    """
    import numpy as np
    import librosa

    y, sr = librosa.load(str(song_path), sr=None, mono=True)
    duration = float(len(y) / sr) if sr else 0.0
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units="frames")
    beat_times = librosa.frames_to_time(beat_frames, sr=sr)
    tempo = float(np.atleast_1d(tempo)[0])

    # Per-beat RMS energy, normalized 0..1.
    rms = librosa.feature.rms(y=y)[0]
    rms_times = librosa.frames_to_time(np.arange(len(rms)), sr=sr)
    if len(beat_times) == 0:
        # Degenerate (silence / very short clip): one beat per second.
        beat_times = np.arange(0, max(duration, 1.0), 1.0)
    beat_energy = np.interp(beat_times, rms_times, rms) if len(rms) else np.zeros(len(beat_times))
    if beat_energy.max() > 0:
        beat_energy = beat_energy / beat_energy.max()

    # Sections: split the beat sequence where energy crosses the median by a
    # margin. Cheap stand-in for librosa.segment that needs no extra model.
    median = float(np.median(beat_energy)) if len(beat_energy) else 0.0
    sections: list[Section] = []
    cur_start_i = 0
    cur_high = beat_energy[0] >= median if len(beat_energy) else True
    times = list(beat_times)
    energies = list(beat_energy)

    def _close_section(i_end: int) -> None:
        seg = energies[cur_start_i:i_end] or [0.0]
        sections.append(Section(
            id=len(sections),
            start=times[cur_start_i],
            end=times[i_end] if i_end < len(times) else duration,
            intensity=float(min(1.0, max(0.0, sum(seg) / len(seg)))),
        ))

    for i in range(1, len(energies)):
        high = energies[i] >= median + 0.12
        low = energies[i] < median - 0.12
        if (cur_high and low) or (not cur_high and high):
            _close_section(i)
            cur_start_i = i
            cur_high = high
    _close_section(len(energies))
    if not sections:
        sections = [Section(0, 0.0, duration, 0.5)]

    # Map each beat to its section + downbeat flag.
    beats: list[Beat] = []
    for i, t in enumerate(times):
        sid = next((s.id for s in sections if s.start <= t < s.end), sections[-1].id)
        beats.append(Beat(time=float(t), is_downbeat=(i % 4 == 0),
                          energy=float(energies[i]), section_id=sid))

    return BeatGrid(duration=duration, tempo=tempo, beats=beats, sections=sections)


# --------------------------------------------------------------------------- #
# Stage 2 — motion analysis -> MotionCurve per clip
# --------------------------------------------------------------------------- #

def analyze_motion(clip_id: str, src: Path, hwaccel_decode: bool) -> MotionCurve:
    """Per-frame motion curve via CPU frame-differencing.

    Decodes the clip to a downscaled grayscale raw-video stream at ANALYSIS_FPS,
    then motion[t] = mean(|frame[t] - frame[t-1]|), normalized 0..1. No OpenCV.
    """
    import numpy as np

    duration = probe_duration(src)
    frame_bytes = ANALYSIS_W * ANALYSIS_H
    cmd = ["ffmpeg", "-v", "error"]
    if hwaccel_decode:
        cmd += ["-hwaccel", "cuda"]
    cmd += [
        "-i", str(src),
        "-vf", f"fps={ANALYSIS_FPS},scale={ANALYSIS_W}:{ANALYSIS_H},format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert proc.stdout is not None
    samples: list[float] = []
    prev = None
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            arr = np.frombuffer(buf, dtype=np.uint8).astype(np.int16)
            if prev is not None:
                samples.append(float(np.abs(arr - prev).mean()))
            prev = arr
    finally:
        proc.stdout.close()
        proc.wait()

    peak = max(samples) if samples else 0.0
    if peak > 0:
        samples = [s / peak for s in samples]
    return MotionCurve(clip_id=clip_id, src_path=str(src), duration=duration,
                       fps_analyzed=float(ANALYSIS_FPS), samples=samples)


# --------------------------------------------------------------------------- #
# Stage 3 — cut planner -> EDL  (the product)
# --------------------------------------------------------------------------- #

def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _smooth(vals: list[float], win: int) -> list[float]:
    """Centered moving average so a gradual build reads as a smooth ramp."""
    if win <= 1 or len(vals) <= 2:
        return list(vals)
    half = win // 2
    out: list[float] = []
    for i in range(len(vals)):
        lo, hi = max(0, i - half), min(len(vals), i + half + 1)
        out.append(sum(vals[lo:hi]) / (hi - lo))
    return out


def _weighted_choice(rng: random.Random, items: list, weights: list[float]):
    total = sum(weights)
    if total <= 0:
        return rng.choice(items)
    r = rng.random() * total
    acc = 0.0
    for it, w in zip(items, weights):
        acc += w
        if r <= acc:
            return it
    return items[-1]


def plan_cuts(grid: BeatGrid, curves: list[MotionCurve], *,
              tightness: float, target_duration: float | None,
              seed: int | None = None) -> EDL:
    """Turn a beat grid + motion curves into an ordered cut list.

    Cut density tracks the song's *local* energy (smoothed per-beat), so a slow
    builder cuts sparsely in the intro and accelerates into the climax. Each
    interval is filled by a clip whose motion window scores highest — motion,
    plus a match between the clip's energy and the song's local energy (calm
    shots in quiet passages, hot shots at the peak), minus recency/overuse
    penalties — and the clip is aligned so its motion peak lands on the opening
    beat.

    ``seed`` enables exploration: instead of always taking the single best clip
    and window, it samples among the top few (weighted by score), so successive
    runs of the same inputs yield different — but still good — cuts. ``seed=None``
    is fully deterministic (strict best), which keeps the golden tests stable.
    """
    if not curves:
        raise ValueError("no usable clips for planning")
    T = float(target_duration or grid.duration)
    if T <= 0:
        raise ValueError("non-positive timeline duration")
    tightness = _clamp(tightness, 0.0, 1.0)
    max_clip = max(c.duration for c in curves)
    explore = seed is not None
    rng = random.Random(seed)

    beats = [b for b in grid.beats if b.time < T]
    if not beats:
        beats = [Beat(0.0, True, 0.5, 0)]
    times = [b.time for b in beats]
    energies = _smooth([b.energy for b in beats], ENERGY_SMOOTH)

    def energy_at(t: float) -> float:
        idx = min(max(bisect.bisect_left(times, t), 0), len(times) - 1)
        return energies[idx]

    # --- boundaries: density driven by local energy (continuous build) ----- #
    boundaries: list[float] = [0.0]
    i = 0
    while i < len(beats):
        # 4 beats/cut when relaxed -> ~1 when tight; fewer beats/cut as energy rises.
        step = int(_clamp(round(_lerp(4, 1, tightness) / max(energies[i], 0.25)), 1, 8))
        i += step
        if i < len(beats) and beats[i].time > boundaries[-1] + 1e-3 and beats[i].time < T:
            boundaries.append(beats[i].time)
    boundaries.append(T)
    boundaries = sorted(set(boundaries))

    # No clip can cover an interval longer than the longest clip — subdivide
    # oversized intervals evenly so the timeline stays exactly tiled (the
    # duration-sum invariant below depends on it).
    tiled: list[float] = [boundaries[0]]
    for nxt in boundaries[1:]:
        start = tiled[-1]
        span = nxt - start
        if max_clip > 0 and span > max_clip:
            parts = int(span // max_clip) + 1
            for k in range(1, parts):
                tiled.append(start + span * k / parts)
        tiled.append(nxt)
    boundaries = sorted(set(tiled))

    # --- fill each interval ------------------------------------------------ #
    entries: list[EDLEntry] = []
    recent: list[str] = []
    use_count: dict[str, int] = {c.clip_id: 0 for c in curves}
    n_intervals = max(1, len(boundaries) - 1)
    max_uses = max(1, -(-n_intervals // len(curves)))  # ceil

    for idx in range(len(boundaries) - 1):
        b0, b1 = boundaries[idx], boundaries[idx + 1]
        d = b1 - b0
        if d <= 0:
            continue
        e_local = energy_at(b0)

        scored: list[tuple[float, MotionCurve, float]] = []
        for c in curves:
            if c.duration < d - 1e-3:
                continue  # too short to fill this interval
            windows = c.top_windows(d, TOP_WINDOWS_K)
            if explore and len(windows) > 1:
                mean, peak, _ = _weighted_choice(rng, windows, [max(w[0], 1e-3) for w in windows])
            else:
                mean, peak, _ = windows[0]
            score = (W_MOTION * mean
                     + W_INTENSITY * (1 - abs(mean - e_local))
                     - (W_RECENT if c.clip_id in recent[-RECENT_K:] else 0.0)
                     - W_OVERUSE * (use_count[c.clip_id] / max_uses))
            # Align so the motion peak sits on the opening beat.
            in_point = _clamp(peak, 0.0, max(0.0, c.duration - d))
            scored.append((score, c, in_point))

        if not scored:  # no clip long enough; use the longest, full length
            c = max(curves, key=lambda x: x.duration)
            d = min(d, c.duration)
            scored = [(0.0, c, 0.0)]

        scored.sort(key=lambda s: s[0], reverse=True)
        if explore and len(scored) > 1:
            top = scored[:TOP_CANDIDATES_K]
            base = min(s[0] for s in top)
            pick = _weighted_choice(rng, top, [(s[0] - base) + 0.1 for s in top])
        else:
            pick = scored[0]

        _, c, in_point = pick
        entries.append(EDLEntry(
            clip_id=c.clip_id, src_path=c.src_path,
            in_point=round(in_point, 4), out_point=round(in_point + d, 4),
            duration=round(d, 4), place_at=round(b0, 4),
        ))
        recent.append(c.clip_id)
        use_count[c.clip_id] += 1

    timeline = sum(e.duration for e in entries)
    # Invariant: durations tile the timeline (audio stays in sync after concat).
    if abs(timeline - boundaries[-1]) > 0.05:
        raise RuntimeError(
            f"EDL duration {timeline:.3f}s != timeline {boundaries[-1]:.3f}s")
    return EDL(timeline_duration=timeline, entries=entries)


# --------------------------------------------------------------------------- #
# Stage 4 — render & assemble
# --------------------------------------------------------------------------- #

def _render_segment(entry: EDLEntry, out: Path, *, width: int, height: int,
                    fps: int, video_encode_args: list[str], gpu: bool) -> None:
    """Trim + normalize one segment to a uniform W/H/fps/SAR clip (no audio).

    Re-encoding is mandatory: beat-aligned cut points aren't keyframes, and the
    concat path needs uniform inputs. When ``gpu`` is set the whole chain stays
    resident on the GPU — NVDEC decode -> scale_cuda/pad_cuda -> NVENC — so frames
    never round-trip to system memory. Any clip NVDEC/cuda-filters can't handle
    falls back to the CPU scale/pad path for that one segment.
    """
    def _cpu_cmd() -> list[str]:
        vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
              f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,fps={fps},setsar=1")
        return ["ffmpeg", "-y", "-v", "error",
                "-ss", f"{entry.in_point:.4f}", "-i", entry.src_path, "-t", f"{entry.duration:.4f}",
                "-vf", vf, "-an", *video_encode_args, "-movflags", "+faststart", str(out)]

    def _gpu_cmd() -> list[str]:
        vf = (f"scale_cuda={width}:{height}:force_original_aspect_ratio=decrease,"
              f"pad_cuda={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,fps={fps}")
        return ["ffmpeg", "-y", "-v", "error", "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
                "-ss", f"{entry.in_point:.4f}", "-i", entry.src_path, "-t", f"{entry.duration:.4f}",
                "-vf", vf, "-an", *video_encode_args, "-movflags", "+faststart", str(out)]

    if gpu:
        try:
            _run(_gpu_cmd(), f"render segment @ {entry.place_at:.2f}s (gpu)")
            return
        except RuntimeError:
            pass  # this clip won't decode/filter on the GPU — fall back to CPU
    _run(_cpu_cmd(), f"render segment @ {entry.place_at:.2f}s")


def assemble(segments: list[Path], song_path: Path, out_path: Path) -> None:
    """Concat normalized segments and mux the song over them as the only audio."""
    listfile = out_path.parent / "concat.txt"
    lines = []
    for p in segments:
        safe = str(p).replace("'", "'\\''")
        lines.append(f"file '{safe}'\n")
    listfile.write_text("".join(lines), encoding="utf-8")
    _run(
        ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listfile),
         "-i", str(song_path), "-map", "0:v:0", "-map", "1:a:0",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
         "-movflags", "+faststart", str(out_path)],
        "final assembly",
    )


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #

def _overall(stage: str, stage_progress: float) -> float:
    """Weighted overall progress so the bar advances smoothly across stages."""
    done = sum(STAGE_WEIGHTS[s] for s in _STAGE_ORDER[:_STAGE_ORDER.index(stage)])
    return round(done + STAGE_WEIGHTS[stage] * _clamp(stage_progress, 0, 1), 4)


def generate(*, video_paths: list[Path], song_path: Path, options: dict,
             work_dir: Path, out_path: Path, video_encode_args: list[str],
             hwaccel_decode: bool, progress: ProgressFn) -> dict:
    """Run the full pipeline. ``progress(status, overall, stage_progress, detail)``
    is called throughout. Returns result metadata for the finished file."""
    width = int(options.get("width", 1920))
    height = int(options.get("height", 1080))
    fps = int(options.get("fps", 30))
    tightness = float(options.get("tightness", 0.5))
    target = options.get("target_duration")
    seed = options.get("seed")

    # 1. audio --------------------------------------------------------------- #
    progress("analyzing_audio", _overall("analyzing_audio", 0.0), 0.0, "Analyzing audio…")
    grid = analyze_audio(song_path)
    progress("analyzing_audio", _overall("analyzing_audio", 1.0), 1.0,
             f"{len(grid.beats)} beats @ {grid.tempo:.0f} BPM")

    # 2. motion (the slow stage — surface per-clip progress) ----------------- #
    curves: list[MotionCurve] = []
    total = len(video_paths)
    for idx, src in enumerate(video_paths):
        sp = (idx) / total
        progress("analyzing_motion", _overall("analyzing_motion", sp), sp,
                 f"Analyzing motion: clip {idx + 1} of {total}")
        try:
            curve = analyze_motion(str(idx), Path(src), hwaccel_decode)
            if curve.duration > 0 and curve.samples:
                curves.append(curve)
        except Exception:
            pass  # skip undecodable clips; fail later only if <2 remain
    if len(curves) < 2:
        raise RuntimeError("fewer than 2 usable clips after motion analysis")
    progress("analyzing_motion", _overall("analyzing_motion", 1.0), 1.0,
             f"Analyzed {len(curves)} clips")

    # 3. plan ---------------------------------------------------------------- #
    progress("planning", _overall("planning", 0.0), 0.0, "Planning cuts…")
    edl = plan_cuts(grid, curves, tightness=tightness, target_duration=target, seed=seed)
    (work_dir / "edl.json").write_text(edl.to_json(), encoding="utf-8")
    progress("planning", _overall("planning", 1.0), 1.0, f"{len(edl.entries)} cuts")

    # 4. render + assemble --------------------------------------------------- #
    segments: list[Path] = []
    n = len(edl.entries)
    for i, entry in enumerate(edl.entries):
        sp = i / max(1, n)
        progress("rendering", _overall("rendering", sp * 0.9), sp,
                 f"Rendering segment {i + 1} of {n}")
        seg = work_dir / f"seg_{i:04d}.mp4"
        _render_segment(entry, seg, width=width, height=height, fps=fps,
                        video_encode_args=video_encode_args, gpu=hwaccel_decode)
        segments.append(seg)
    progress("rendering", _overall("rendering", 0.95), 0.95, "Assembling final cut…")
    assemble(segments, song_path, out_path)

    size = out_path.stat().st_size
    return {
        "width": width, "height": height, "fps": fps,
        "duration": round(edl.timeline_duration, 3),
        "size_bytes": size, "cuts": len(edl.entries), "seed": seed,
    }
