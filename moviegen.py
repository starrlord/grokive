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
import re
import signal
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
W_BEAT_ALIGN = 0.35     # keep source motion peaks close to a musical beat
W_WINDOW_FIT = 0.12     # keep the rendered segment near the scored motion window
RECENT_K = 2              # don't reuse a clip within this many cuts
ENERGY_SMOOTH = 5         # beats; smooths per-beat energy so a build reads as a ramp
TOP_WINDOWS_K = 3         # candidate motion windows per clip to choose among
TOP_CANDIDATES_K = 3      # top-scoring clips to choose among when exploring

# Overall-progress weighting so the bar doesn't stall on the slow stage.
STAGE_WEIGHTS = {"analyzing_audio": 0.10, "analyzing_motion": 0.45,
                 "planning": 0.05, "rendering": 0.40}
_STAGE_ORDER = ["analyzing_audio", "analyzing_motion", "planning", "rendering"]

# Presets bundle the analysis mode, cut rhythm, transition policy, framing and
# per-shot motion. "classic" is the original v1 behaviour, byte-for-byte — it
# stays the default so existing renders (and the golden tests) are unchanged.
#   rhythm        "density" (energy-driven, ~even) | "phrase" (long holds + bursts)
#   motion_weight how hard to favour high-motion windows (0 = let the song's local
#                 energy decide, i.e. calm shots in calm passages)
#   transitions   "none" | "accents" (beat-timed xfades at section starts / drops)
#   fill          "fit" (letterbox/pillarbox, whole frame kept) | "cover"
#                 (scale-to-fill + centre-crop so the frame is edge-to-edge, no bars)
#   push_in       slow centred Ken-Burns zoom across long held shots (fraction; 0 = off)
#   punch         centred zoom hit on drop beats, easing back to 1.0 (fraction; 0 = off)
#
# How a good beat montage is actually built (cf. the `mugen` generator + editing
# craft): the *cuts* carry it — hold through builds, burst on the drop — and
# motion is a sparse accent (a punch on the drop, a gentle push-in on held shots),
# never a pan slapped on every shot. A per-shot pan reads as the whole picture
# sliding, not a camera move, so there deliberately isn't one. Cinematic follows
# that recipe: cover-framed (no bars), energy-paced (longer holds in builds,
# accelerating into loud sections), motion-favouring, punching the drops. Moody
# is its calm sibling — letterboxed, phrase-paced calm footage, slow push-in.
PRESETS: dict[str, dict] = {
    "classic":   {"enhanced_analysis": False, "rhythm": "density", "motion_weight": W_MOTION,
                  "transitions": "none",    "fill": "fit",   "push_in": 0.0,  "punch": 0.0,
                  "overuse_w": W_OVERUSE, "overuse_p": 1.0},
    "cinematic": {"enhanced_analysis": True,  "rhythm": "density", "motion_weight": W_MOTION,
                  "transitions": "accents", "fill": "cover", "push_in": 0.08, "punch": 0.12,
                  # Strong, super-linear reuse penalty. With dozens of source clips the
                  # default soft 0.5·(use/cap) lets a few uniformly-busy clips win again and
                  # again while many clips go unused (they never crack the top candidates).
                  # Squaring the ratio and raising the weight keeps reuse mild up to a clip's
                  # fair share, then steep past it — forcing the planner to spread across the
                  # whole library. See overuse_p in plan_cuts.
                  "overuse_w": 1.0, "overuse_p": 2.0},
    "moody":     {"enhanced_analysis": True,  "rhythm": "phrase",  "motion_weight": 0.0,
                  "transitions": "accents", "fill": "fit",   "push_in": 0.12, "punch": 0.0,
                  "overuse_w": W_OVERUSE, "overuse_p": 1.0},
}
DEFAULT_PRESET = "classic"

# Phrase-rhythm tunables (moody preset). Calm passages hold a single shot for a
# whole musical phrase; loud passages cut in 1- or ½-beat bursts.
PHRASE_HOLD_RELAXED = 16   # beats held per shot in calm passages when tightness=0
PHRASE_HOLD_TIGHT = 4      # ...and when tightness=1
# Per-shot motion (render stage).
HELD_MIN_S = 1.2           # only shots at least this long get the slow push-in
PUNCH_DECAY_S = 0.26       # beat-punch zoom eases back to 1.0 over this long
# Accent transitions fire on section changes AND drops, but never closer than this
# so they stay punctuation rather than a constant churn.
MIN_TRANSITION_GAP = 3.0   # seconds
DROP_RISE = 0.15           # smoothed per-beat energy jump that marks a "drop"
DROP_LEVEL = 0.50          # ...and the drop beat must be at least this loud (0..1)

# Accent-transition tunables (cinematic preset only).
TRANSITION_MIN = 0.30     # seconds; shorter is too quick to register as a transition
TRANSITION_MAX = 0.70     # seconds; longer steals too much of a fast shot
# Canonical (deterministic, seed=None) transition per context, plus the taste-set
# sampled from when a seed is supplied. xfade transition names are stock ffmpeg.
_TRANSITION_SECTION = "dissolve"            # at a structural section start
_TRANSITION_DROP = "fadeblack"              # on an energy drop / downbeat hit
_TRANSITION_VARIANTS_SECTION = ["dissolve", "fade", "smoothleft", "smoothright"]
_TRANSITION_VARIANTS_DROP = ["fadeblack", "fadewhite", "circleopen"]

# "Let clips speak" (audio ducking). When enabled, in the song's quiet pockets we
# hold on a clip that has a real spoken line — read from its .srt/.vtt sidecar —
# and dip the music so the line comes through (diegetic audio / a "breakdown").
MIN_SPEAK_WORDS = 4        # a cue needs at least this many words to count as a line
SUB_MERGE_GAP = 0.4        # s; merge consecutive cues closer than this into one line
SPEAK_MIN_GAP = 20.0       # s; keep spoken moments far apart so they stay punctuation
SPEAK_EDGE_PAD = 4.0       # s; never place one in the first/last few seconds
SPEAK_MAX_DUR = 8.0        # s; target length of a featured line — only ever trimmed
                           # at a SENTENCE boundary, never mid-sentence (so a longer
                           # single sentence is kept whole rather than chopped)
SPEAK_SENT_PAD = 0.3       # s; tail pad so the final word fully lands (clamped to cue)
# Lines are placed in the *relatively* quietest spots, but never gated out: even a
# busy song still ducks and plays the line, then swells back up.
DUCK_GAIN = 0.40           # song multiplier under a spoken line (~ -8 dB): the music
                           # stays audible in the background, just under the voice
                           # (0.20≈-14dB nearly gone · 0.50≈-6dB · 1.0 = no duck)
SPEAK_DUCK_FADE = 0.35     # s; how long to fade the music DOWN (a lead-in before the line)
SPEAK_UNDUCK_MIN = 0.20    # s; min fade-UP back to full
SPEAK_UNDUCK_MAX = 1.20    # s; cap the fade-UP — it ends ON the next beat ("into the beat")


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
    # Transient onset times (seconds). Populated only by the enhanced analysis;
    # empty in classic mode, so anything reading this stays backward compatible.
    onsets: list[float] = field(default_factory=list)


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

    def mean_window(self, start: float, length: float) -> float:
        """Mean motion for the exact rendered source window."""
        if not self.samples:
            return 0.0
        fps = self.fps_analyzed
        lo = max(0, min(len(self.samples), round(start * fps)))
        n = max(1, round(length * fps))
        hi = max(lo + 1, min(len(self.samples), lo + n))
        return (self._prefix[hi] - self._prefix[lo]) / (hi - lo)

    def top_windows(self, length: float, k: int = 3) -> list[tuple[float, float, float]]:
        """Up to ``k`` strong, well-separated motion windows of ``length`` seconds,
        each (mean_motion, peak_time, window_start). Lets the planner pick a
        *different* good moment from the same clip across runs."""
        fps = self.fps_analyzed
        n = max(1, round(length * fps))
        if not self.samples or n >= len(self.samples):
            return [self.best_window(length)]
        means = sorted(
            (((self._prefix[i + n] - self._prefix[i]) / n, i)
             for i in range(0, len(self.samples) - n + 1)),
            key=lambda item: (-item[0], item[1]),
        )
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
    # Accent-transition metadata for the join INTO this entry from the previous
    # one. "cut" / 0.0 (the defaults) is a plain hard cut and leaves the classic
    # render path and the duration-tiling invariant completely unchanged — the
    # transition is realised purely at render time via clip handles.
    transition: str = "cut"
    transition_dur: float = 0.0
    # Set when this shot is a held "let it speak" moment: the clip's own dialogue
    # plays while the song ducks under it (see the speech helpers + _duck_and_mux).
    speak: bool = False


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
    if proc.returncode == 0:
        return
    err = (proc.stderr or "").strip()[-400:]
    if err:
        raise RuntimeError(err)
    # ffmpeg exited non-zero but printed nothing (it runs at -v error): it was
    # almost certainly terminated by a signal mid-run. The classic case is the
    # OOM killer (SIGKILL) on a memory-heavy filtergraph — surface that instead
    # of a blank "ffmpeg failed", which is what made this hard to diagnose.
    rc = proc.returncode
    if rc < 0:
        try:
            name = signal.Signals(-rc).name
        except ValueError:
            name = f"signal {-rc}"
        hint = " — likely out of memory" if -rc == getattr(signal, "SIGKILL", 9) else ""
        raise RuntimeError(f"ffmpeg killed by {name}{hint} during {what}")
    raise RuntimeError(f"ffmpeg failed ({what}) with exit code {rc}")


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

def analyze_audio(song_path: Path, *, enhanced: bool = False) -> BeatGrid:
    """librosa beat grid + per-beat energy + sections.

    ``enhanced=False`` (classic) uses ``beat_track`` with median-energy-crossing
    sections and no onsets — the original v1 behaviour, kept byte-for-byte.

    ``enhanced=True`` (cinematic) drives ``beat_track`` from an onset-strength
    envelope (tighter beats), records transient onset times, falls back to PLP
    pulse peaks when beat tracking degenerates on tempo-varying material, and
    derives real structural sections via ``librosa.segment`` instead of the
    median heuristic. Downbeats stay every-4th-beat in both modes (true downbeat
    tracking needs madmom and is out of scope).
    """
    import numpy as np
    import librosa

    y, sr = librosa.load(str(song_path), sr=None, mono=True)
    duration = float(len(y) / sr) if sr else 0.0

    onsets: list[float] = []
    if enhanced:
        onset_env = librosa.onset.onset_strength(y=y, sr=sr)
        tempo, beat_frames = librosa.beat.beat_track(
            onset_envelope=onset_env, sr=sr, units="frames")
        # PLP cross-check: if standard tracking degenerates (very few beats), use
        # the predominant-local-pulse peaks, which follow tempo drift better.
        if len(beat_frames) < 4:
            pulse = librosa.beat.plp(onset_envelope=onset_env, sr=sr)
            beat_frames = np.flatnonzero(librosa.util.localmax(pulse))
        onset_frames = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr)
        onsets = [float(t) for t in librosa.frames_to_time(onset_frames, sr=sr)]
    else:
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

    times = list(beat_times)
    energies = list(beat_energy)

    if enhanced:
        sections = _sections_structural(y, sr, beat_frames, times, energies, duration)
    else:
        sections = _sections_median(times, energies, duration)

    # Map each beat to its section + downbeat flag.
    beats: list[Beat] = []
    for i, t in enumerate(times):
        sid = next((s.id for s in sections if s.start <= t < s.end), sections[-1].id)
        beats.append(Beat(time=float(t), is_downbeat=(i % 4 == 0),
                          energy=float(energies[i]), section_id=sid))

    return BeatGrid(duration=duration, tempo=tempo, beats=beats,
                    sections=sections, onsets=onsets)


def _sections_median(times: list[float], energies: list[float], duration: float) -> list[Section]:
    """Classic sections: split the beat sequence where energy crosses the median
    by a margin. Cheap stand-in for librosa.segment that needs no extra model."""
    import numpy as np

    median = float(np.median(energies)) if len(energies) else 0.0
    sections: list[Section] = []
    cur_start_i = 0
    cur_high = energies[0] >= median if len(energies) else True

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
    return sections


def _sections_structural(y, sr, beat_frames, times: list[float],
                         energies: list[float], duration: float) -> list[Section]:
    """Enhanced sections via beat-synchronous structural segmentation.

    Clusters beat-synced chroma with ``librosa.segment.agglomerative`` into a
    handful of contiguous sections (verse/chorus/drop-scale), then sets each
    section's intensity from its mean per-beat energy. Falls back to the median
    heuristic if there aren't enough beats to segment meaningfully.
    """
    import numpy as np
    import librosa

    n_beats = len(times)
    if n_beats < 8 or len(beat_frames) < 8:
        return _sections_median(times, energies, duration)

    try:
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
        beat_chroma = librosa.util.sync(chroma, beat_frames, aggregate=np.median)
        # Aim for a section roughly every ~15s, clamped to a sane 2..8.
        k = int(min(8, max(2, round(duration / 15.0))))
        k = min(k, beat_chroma.shape[1] - 1)
        bound_beats = librosa.segment.agglomerative(beat_chroma, k)
        bound_beats = sorted({int(b) for b in bound_beats} | {0})
    except Exception:
        return _sections_median(times, energies, duration)

    sections: list[Section] = []
    for j, start_i in enumerate(bound_beats):
        end_i = bound_beats[j + 1] if j + 1 < len(bound_beats) else n_beats
        if end_i <= start_i:
            continue
        seg = energies[start_i:end_i] or [0.0]
        sections.append(Section(
            id=len(sections),
            start=times[start_i],
            end=times[end_i] if end_i < n_beats else duration,
            intensity=float(min(1.0, max(0.0, sum(seg) / len(seg)))),
        ))
    if not sections:
        sections = [Section(0, 0.0, duration, 0.5)]
    # The first beat can sit well into the track (quiet intro); the opening
    # section still owns everything before it, so anchor it at t=0.
    sections[0].start = 0.0
    return sections


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
# Speech ("let clips speak") — read dialogue timings from subtitle sidecars
# --------------------------------------------------------------------------- #

@dataclass
class Utterance:
    clip_idx: int        # index into the source clip list (== MotionCurve.clip_id)
    src_path: str
    start: float         # seconds, in the clip's own timeline
    end: float
    text: str

    @property
    def dur(self) -> float:
        return self.end - self.start

    @property
    def words(self) -> int:
        return len(self.text.split())


@dataclass
class SpeechMoment:
    place_at: float      # timeline position (seconds), beat-aligned
    dur: float           # held-shot length = the line's duration (capped)
    clip_idx: int
    src_path: str
    in_point: float      # clip-time start of the line
    text: str


_TS_RE = re.compile(r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")


def _parse_ts(h: str, m: str, s: str, frac: str) -> float:
    return ((int(h) if h else 0) * 3600 + int(m) * 60 + int(s)
            + int(frac) / (10 ** len(frac)))


def _clean_cue_text(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)        # tags: <i>, <00:00:01.000>, <c> …
    s = re.sub(r"\{[^}]+\}", "", s)      # {\an8} style overrides
    return re.sub(r"\s+", " ", s).strip()


def _parse_cues(text: str) -> list[tuple[float, float, str]]:
    """Parse SRT or VTT text into ``(start, end, text)`` cues. Tolerant of the
    WEBVTT header, cue index lines, NOTE blocks, and cue settings on the timing
    line (``… --> … align:start position:50%``)."""
    cues: list[tuple[float, float, str]] = []
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for block in text.split("\n\n"):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        start = end = None
        body: list[str] = []
        for ln in lines:
            if start is None and "-->" in ln:
                ts = _TS_RE.findall(ln)
                if len(ts) >= 2:
                    start, end = _parse_ts(*ts[0]), _parse_ts(*ts[1])
                continue
            if start is None:
                continue  # index number / WEBVTT / NOTE before the timing line
            body.append(ln)
        if start is not None and end is not None and end > start:
            txt = _clean_cue_text(" ".join(body))
            if txt:
                cues.append((start, end, txt))
    return cues


def _parse_subtitles(clip_path: Path) -> list[tuple[float, float, str]]:
    """Cues from the ``.srt``/``.vtt`` sidecar beside ``clip_path`` ([] if none)."""
    for ext in (".srt", ".vtt"):
        side = clip_path.with_suffix(ext)
        if side.is_file():
            try:
                return _parse_cues(side.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                return []
    return []


def _split_sentences(text: str) -> list[str]:
    """Split on sentence-final punctuation, keeping the punctuation. ASR captions
    aren't always punctuated, so a run with none comes back as a single 'sentence'."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in (p.strip() for p in parts) if p]


def _trim_to_sentences(start: float, end: float, text: str,
                       max_dur: float) -> tuple[float, str]:
    """Trim a merged line to whole sentences from its start, up to ``max_dur`` —
    but never cut a sentence in half: always keep at least the first sentence, and
    stop only at a sentence boundary. Sentence end times are estimated
    proportionally to word counts (we only have cue-level timing), then nudged by
    ``SPEAK_SENT_PAD`` so the last word lands, clamped to the real cue end.
    """
    sents = _split_sentences(text)
    dur = end - start
    if len(sents) <= 1 or dur <= 0:
        return end, text
    total_words = max(1, len(text.split()))
    kept: list[str] = []
    acc_words = 0
    for sent in sents:
        w = len(sent.split())
        if kept and dur * ((acc_words + w) / total_words) > max_dur:
            break                      # next sentence overflows -> stop on this boundary
        kept.append(sent)
        acc_words += w
    est = dur * (acc_words / total_words)
    new_end = min(end, start + est + SPEAK_SENT_PAD)
    return new_end, " ".join(kept)


def _utterances_for_clip(clip_idx: int, clip_path: Path, clip_dur: float,
                         min_words: int = MIN_SPEAK_WORDS) -> list[Utterance]:
    """Merge a clip's cues into lines, trim each to whole sentences, and keep only
    the substantial ones.

    Consecutive cues less than ``SUB_MERGE_GAP`` apart are joined (a sentence split
    across cues becomes one line); each line is then cut to whole sentences up to
    ``SPEAK_MAX_DUR`` (never mid-sentence); finally a line must clear ``min_words``
    and fit inside the clip. Filters out the "I" / "are" one-word noise.
    """
    merged: list[list] = []
    for s, e, t in _parse_subtitles(clip_path):
        if merged and s - merged[-1][1] <= SUB_MERGE_GAP:
            merged[-1][1] = e
            merged[-1][2] = (merged[-1][2] + " " + t).strip()
        else:
            merged.append([s, e, t])
    out: list[Utterance] = []
    for s, e, t in merged:
        e2, t2 = _trim_to_sentences(s, e, t, SPEAK_MAX_DUR)
        if len(t2.split()) >= min_words and (clip_dur <= 0 or e2 <= clip_dur + 0.05):
            out.append(Utterance(clip_idx, str(clip_path), s, e2, t2))
    return out


def _pick_speech_moments(grid: BeatGrid, utterances: list[Utterance], T: float,
                         count: int) -> list[SpeechMoment]:
    """Choose up to ``count`` spoken lines to feature, placed in the song's
    *relatively* quietest spots, beat-aligned, well spread out, at most one per clip.

    Each utterance is scored at its best-fitting beat-aligned window — the music
    never has to actually go quiet, so a busy song still places lines (they just
    duck the music wherever they land). Greedy by score (quieter is better),
    enforcing ``SPEAK_MIN_GAP`` between chosen windows.
    """
    if count <= 0 or not utterances or not grid.beats:
        return []
    times = [b.time for b in grid.beats]
    en = _smooth([b.energy for b in grid.beats], ENERGY_SMOOTH)
    downbeats = [b.time for b in grid.beats if b.is_downbeat] or times

    def energy_over(a: float, b: float) -> float:
        lo = bisect.bisect_left(times, a)
        hi = max(lo + 1, bisect.bisect_right(times, b))
        seg = en[lo:hi] or [en[min(lo, len(en) - 1)]]
        return sum(seg) / len(seg)

    # Score every (line, beat-aligned position) pair, then pick JOINTLY: take the
    # best pair, block out ±SPEAK_MIN_GAP around it and that clip, and repeat. This
    # spreads moments across the song — without it, every line would grab the one
    # globally-quietest spot and all but one would collide on the spacing rule.
    cands: list[tuple[float, float, Utterance]] = []
    for u in utterances:
        dur = u.dur                      # already sentence-bounded; never cut short
        if dur < 0.6:
            continue
        for t0 in downbeats:
            if t0 < SPEAK_EDGE_PAD or t0 + dur > T - SPEAK_EDGE_PAD:
                continue
            # Prefer the quietest placement; longer lines win ties slightly.
            cands.append((-energy_over(t0, t0 + dur) + 0.02 * u.words, t0, u))

    cands.sort(key=lambda c: c[0], reverse=True)
    chosen: list[SpeechMoment] = []
    used_clips: set[int] = set()
    for _, t0, u in cands:
        if u.clip_idx in used_clips:
            continue
        if any(abs(t0 - c.place_at) < SPEAK_MIN_GAP for c in chosen):
            continue
        chosen.append(SpeechMoment(
            place_at=round(t0, 4), dur=round(u.dur, 4), clip_idx=u.clip_idx,
            src_path=u.src_path, in_point=round(u.start, 4), text=u.text))
        used_clips.add(u.clip_idx)
        if len(chosen) >= count:
            break
    chosen.sort(key=lambda m: m.place_at)
    return chosen


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


def _phrase_boundaries(times: list[float], energies: list[float],
                       T: float, tightness: float) -> list[float]:
    """Bimodal 'phrase' cut grid: hold one shot for a whole phrase through calm
    passages, then cut in 1- or ½-beat bursts where the song gets loud.

    Works on a half-beat-resolution candidate grid (beats plus their midpoints) so
    bursts can land between beats; holds always land on a beat. This is what gives
    the moody style its long, breathing shots punctuated by quick flurries.
    """
    cand_t: list[float] = []
    cand_e: list[float] = []
    for j in range(len(times)):
        cand_t.append(times[j]); cand_e.append(energies[j])
        if j + 1 < len(times):
            cand_t.append((times[j] + times[j + 1]) / 2)
            cand_e.append((energies[j] + energies[j + 1]) / 2)

    hold = max(2, round(_lerp(PHRASE_HOLD_RELAXED, PHRASE_HOLD_TIGHT, tightness)))
    burst_thresh = _lerp(0.65, 0.40, tightness)
    boundaries = [0.0]
    k, m = 0, len(cand_t)
    while k < m and cand_t[k] < T:
        e = cand_e[k]
        if e >= burst_thresh:
            step = 1 if e >= 0.8 else 2      # ½-beat or 1-beat burst (candidate units)
        else:
            step = hold * 2                  # long hold (beats -> ½-beat units)
        k += step
        if k < m and cand_t[k] > boundaries[-1] + 1e-3 and cand_t[k] < T:
            boundaries.append(cand_t[k])
    boundaries.append(T)
    return sorted(set(boundaries))


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


def _accent_times(grid: BeatGrid, T: float) -> list[tuple[float, bool]]:
    """Times where an accent transition belongs, each tagged ``is_drop``:

    * **section changes** — structural section starts (the opening one excluded), and
    * **drops** — beats where the smoothed per-beat energy jumps up sharply into a
      loud passage (``DROP_RISE`` over the prior bar, reaching ``DROP_LEVEL``).

    Returned sorted and sparse: never two within ``MIN_TRANSITION_GAP`` (the earlier
    candidate wins), so transitions stay punctuation. Empty if nothing qualifies.
    """
    cands: list[tuple[float, bool]] = []
    for s in sorted(grid.sections, key=lambda s: s.start)[1:]:
        if 1e-3 < s.start < T - 1e-3:
            cands.append((s.start, s.intensity >= 0.5))
    beats = grid.beats
    if len(beats) >= 6:
        en = _smooth([b.energy for b in beats], 3)
        look = 4  # compare against the previous bar
        for i in range(look, len(beats)):
            t = beats[i].time
            if 1e-3 < t < T - 1e-3:
                base = sum(en[i - look:i]) / look
                if en[i] - base >= DROP_RISE and en[i] >= DROP_LEVEL:
                    cands.append((t, True))
    cands.sort()
    kept: list[tuple[float, bool]] = []
    for t, is_drop in cands:
        if not kept or t - kept[-1][0] >= MIN_TRANSITION_GAP:
            kept.append((t, is_drop))
    return kept


def _assign_accent_transitions(grid: BeatGrid, curves: list[MotionCurve],
                               entries: list[EDLEntry], explore: bool,
                               rng: random.Random) -> None:
    """Tag a *few* cuts as accent transitions, in place — hard cuts stay the rule.

    A transition is placed only where a cut lands (within ~half a beat) on an
    accent time — a section change or a drop (see ``_accent_times``) — AND both
    neighbouring clips have enough source beyond their in/out points for the
    overlap "handles" the render needs. The type is canonical when deterministic
    (``dissolve`` at a section start, ``fadeblack`` flash on a drop) and sampled
    from a taste-set when a seed is supplied. This never touches
    ``duration``/``place_at``, so the timeline still tiles exactly.
    """
    if len(entries) < 2:
        return
    clip_dur = {c.clip_id: c.duration for c in curves}
    beat_period = 60.0 / grid.tempo if grid.tempo and grid.tempo > 0 else 0.5
    tol = max(0.5 * beat_period, 0.1)
    T = entries[-1].place_at + entries[-1].duration
    accents = _accent_times(grid, T)
    if not accents:
        return
    times = [a[0] for a in accents]
    last_assigned = -1e9

    for k in range(1, len(entries)):
        cur, prev = entries[k], entries[k - 1]
        bt = cur.place_at
        if bt - last_assigned < MIN_TRANSITION_GAP:
            continue
        j = min(range(len(accents)), key=lambda x: abs(times[x] - bt))
        if abs(times[j] - bt) > tol:
            continue  # this cut isn't on an accent -> leave it a hard cut
        td = _clamp(1.0 * beat_period, TRANSITION_MIN, TRANSITION_MAX)
        td = min(td, 0.8 * min(prev.duration, cur.duration))
        if td < TRANSITION_MIN:
            continue
        half = td / 2.0
        prev_room = clip_dur.get(prev.clip_id, prev.out_point)
        if prev.out_point + half > prev_room + 1e-6 or cur.in_point - half < -1e-6:
            continue  # no handle headroom -> hard cut
        is_drop = accents[j][1]
        if explore:
            variants = _TRANSITION_VARIANTS_DROP if is_drop else _TRANSITION_VARIANTS_SECTION
            cur.transition = rng.choice(variants)
        else:
            cur.transition = _TRANSITION_DROP if is_drop else _TRANSITION_SECTION
        cur.transition_dur = round(td, 4)
        last_assigned = bt


def plan_cuts(grid: BeatGrid, curves: list[MotionCurve], *,
              tightness: float, target_duration: float | None,
              seed: int | None = None, transitions: str = "none",
              rhythm: str = "density", motion_weight: float = W_MOTION,
              overuse_w: float = W_OVERUSE, overuse_p: float = 1.0,
              forced_speech: list[SpeechMoment] | None = None) -> EDL:
    """Turn a beat grid + motion curves into an ordered cut list.

    ``rhythm="density"`` (default): cut density tracks the song's *local* energy
    (smoothed per-beat), so a slow builder cuts sparsely in the intro and
    accelerates into the climax. ``rhythm="phrase"``: hold one shot for a whole
    musical phrase through calm passages, then cut in 1-/½-beat bursts where the
    song gets loud — long breathing shots punctuated by flurries (the moody look).

    Each interval is filled by a clip whose actual rendered window scores highest:
    motion (scaled by ``motion_weight`` — set 0 to let the song's local energy
    pick calm shots in calm passages), a match between the clip's energy and the
    song's local energy, beat-alignment of the motion peak, minus recency/overuse.

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

    def beat_anchors(start: float, end: float) -> list[tuple[float, float]]:
        """Candidate offsets, in seconds from the cut, where a motion peak can land."""
        anchors: list[tuple[float, float]] = [(0.0, 0.0)]
        for beat in beats:
            if beat.time < start - 1e-3 or beat.time >= end - 1e-3:
                continue
            offset = max(0.0, beat.time - start)
            bonus = 0.08 * beat.energy + (0.08 if beat.is_downbeat else 0.0)
            if offset < 1e-3:
                anchors[0] = (0.0, max(anchors[0][1], bonus))
            elif all(abs(offset - a[0]) > 1e-3 for a in anchors):
                anchors.append((offset, bonus))
        return anchors

    # --- boundaries ------------------------------------------------------- #
    if rhythm == "phrase":
        boundaries = _phrase_boundaries(times, energies, T, tightness)
    else:
        # density: driven by local energy (continuous build).
        boundaries = [0.0]
        i = 0
        while i < len(beats):
            # 4 beats/cut when relaxed -> ~1 when tight; fewer as energy rises.
            step = int(_clamp(round(_lerp(4, 1, tightness) / max(energies[i], 0.25)), 1, 8))
            i += step
            if i < len(beats) and beats[i].time > boundaries[-1] + 1e-3 and beats[i].time < T:
                boundaries.append(beats[i].time)
        boundaries.append(T)
    # For accent transitions, force a cut onto each accent time — section change
    # OR drop (snapped to the nearest beat so it stays on-grid). This guarantees a
    # cut exists exactly where a transition wants to land, instead of relying on
    # the energy-driven boundaries happening to coincide with it.
    if transitions == "accents":
        beat_times = [b.time for b in beats]
        for at, _ in _accent_times(grid, T):
            if beat_times:
                snapped = min(beat_times, key=lambda t: abs(t - at))
                if 1e-3 < snapped < T - 1e-3:
                    boundaries.append(snapped)
    boundaries = sorted(set(boundaries))

    # "Let clips speak": carve out each chosen line as exactly one interval — add
    # its edges and drop any boundary that would split it, so a single held shot
    # spans the whole sentence. (Subdivision below can't split it: a line never
    # outlasts its own clip, and intervals are only split when longer than that.)
    forced = forced_speech or []
    if forced:
        windows = [(m.place_at, m.place_at + m.dur) for m in forced]
        boundaries = [b for b in boundaries
                      if not any(w0 + 1e-3 < b < w1 - 1e-3 for w0, w1 in windows)]
        for w0, w1 in windows:
            boundaries.extend((w0, w1))
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
        # A reserved spoken line owns this interval outright: hold the speaking
        # clip from the line's start so the viewer sees the speaker. No scoring.
        fm = next((m for m in forced if abs(m.place_at - b0) < 1e-3
                   and abs((m.place_at + m.dur) - b1) < 1e-3), None)
        if fm is not None:
            cid = str(fm.clip_idx)
            entries.append(EDLEntry(
                clip_id=cid, src_path=fm.src_path,
                in_point=round(fm.in_point, 4), out_point=round(fm.in_point + d, 4),
                duration=round(d, 4), place_at=round(b0, 4), speak=True))
            recent.append(cid)
            if cid in use_count:
                use_count[cid] += 1
            continue
        e_local = energy_at(b0)
        anchors = beat_anchors(b0, b1)

        scored: list[tuple[float, MotionCurve, float]] = []
        for c in curves:
            if c.duration < d - 1e-3:
                continue  # too short to fill this interval
            # Reuse penalties are per-clip (constant across its windows/anchors), so
            # compute them once. Recency blocks a near-back-to-back repeat; overuse
            # spreads usage across the library. overuse_p > 1 makes the overuse penalty
            # super-linear — mild up to a clip's fair share, then steep past it. At
            # overuse_p == 1.0 this is exactly the original soft penalty (classic/moody,
            # byte-for-byte unchanged); cinematic raises both for real coverage.
            recent_pen = W_RECENT if c.clip_id in recent[-RECENT_K:] else 0.0
            overuse_ratio = use_count[c.clip_id] / max_uses
            overuse_pen = overuse_w * (overuse_ratio ** overuse_p
                                       if overuse_p != 1.0 else overuse_ratio)
            for _, peak, window_start in c.top_windows(d, TOP_WINDOWS_K):
                for anchor_offset, anchor_bonus in anchors:
                    desired = peak - anchor_offset
                    in_point = _clamp(desired, 0.0, max(0.0, c.duration - d))
                    actual_mean = c.mean_window(in_point, d)
                    align_error = abs((peak - in_point) - anchor_offset)
                    align_score = 1.0 - _clamp(align_error / max(0.25, d), 0.0, 1.0)
                    window_fit = abs(in_point - window_start) / max(d, 1e-3)
                    score = (motion_weight * actual_mean
                             + W_INTENSITY * (1 - abs(actual_mean - e_local))
                             + W_BEAT_ALIGN * align_score
                             + anchor_bonus
                             - W_WINDOW_FIT * window_fit
                             - recent_pen
                             - overuse_pen)
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

    if transitions == "accents":
        _assign_accent_transitions(grid, curves, entries, explore, rng)

    timeline = sum(e.duration for e in entries)
    # Invariant: durations tile the timeline (audio stays in sync after concat).
    if abs(timeline - boundaries[-1]) > 0.05:
        raise RuntimeError(
            f"EDL duration {timeline:.3f}s != timeline {boundaries[-1]:.3f}s")
    return EDL(timeline_duration=timeline, entries=entries)


# --------------------------------------------------------------------------- #
# Stage 4 — render & assemble
# --------------------------------------------------------------------------- #

def _zoom_filter(zoom: tuple | None, dur: float, fps: int,
                 width: int, height: int) -> str:
    """A centred `zoompan` clause for per-shot motion, or "" if no zoom. CPU-only.

    Both kinds are a *centred* zoom — the framing never translates, so the shot
    reads as a camera push, not the whole picture sliding (the cardinal montage
    sin, and the bug that made the old "fly-by" look broken). The caller must hand
    zoompan a frame whose aspect already matches WxH (the render does
    scale-to-cover + centre-crop first), so the crop window keeps aspect and
    nothing distorts.

    ``("push_in", amt)`` — slow push from 1.0 to 1.0+amt across the whole shot, so
    a long held shot keeps breathing.

    ``("punch", amt)`` — a hit: start at 1.0+amt and ease back to 1.0 over
    ``PUNCH_DECAY_S``, then hold. Placed on a drop beat it lands the camera kick
    exactly on the drop.
    """
    if not zoom or len(zoom) < 2 or zoom[1] <= 0:
        return ""
    kind, amt = zoom[0], zoom[1]
    n = max(1, round(dur * fps))
    center = "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    if kind == "push_in":
        return (f"zoompan=z='1+{amt:.4f}*on/{n}':d=1:fps={fps}:"
                f"s={width}x{height}:{center}")
    if kind == "punch":
        decay = max(1, round(PUNCH_DECAY_S * fps))
        # 1+amt at the cut, easing linearly to 1.0 by `decay` frames, then steady.
        # The comma in max() is safe: it sits inside the single-quoted z value, so
        # the filtergraph parser doesn't read it as a filter separator.
        return (f"zoompan=z='1+{amt:.4f}*max(0,1-on/{decay})':d=1:fps={fps}:"
                f"s={width}x{height}:{center}")
    return ""


def _render_segment(entry: EDLEntry, out: Path, *, width: int, height: int,
                    fps: int, video_encode_args: list[str], gpu: bool,
                    lead: float = 0.0, tail: float = 0.0,
                    fill: str = "fit",
                    zoom: tuple[str, float] | None = None) -> None:
    """Trim + normalize one segment to a uniform W/H/fps/SAR clip (no audio).

    Re-encoding is mandatory: beat-aligned cut points aren't keyframes, and the
    concat path needs uniform inputs. When ``gpu`` is set the whole chain stays
    resident on the GPU — NVDEC decode -> scale_cuda/pad_cuda -> NVENC — so frames
    never round-trip to system memory. Any clip NVDEC/cuda-filters can't handle
    falls back to the CPU scale/pad path for that one segment.

    ``lead``/``tail`` are transition "handles" (seconds) — extra source pulled in
    before the in-point and after the out-point so the assembler can overlap this
    clip with its neighbours via xfade. They're 0 for plain hard cuts, which
    reproduces the original trim exactly. The planner guarantees the source has
    room for any handle it assigns, so no clamping is needed beyond ss>=0.

    ``fill`` is the framing: ``"fit"`` letterboxes/pillarboxes to keep the whole
    source frame (classic/moody); ``"cover"`` scales to fill and centre-crops so
    the output is edge-to-edge with no black bars (cinematic). Centre-cropping to
    the exact W×H first is also what lets the centred ``zoom`` push into *content*
    rather than into letterbox bars.

    ``zoom`` adds a per-shot push-in / on-beat punch (see ``_zoom_filter``). It
    uses the CPU-only ``zoompan`` filter, so a zoomed segment skips the GPU path;
    so does any ``cover`` segment, since stock ffmpeg has no GPU centre-crop
    (``scale_cuda`` can over-scale but there is no ``crop_cuda``).
    """
    ss = max(0.0, entry.in_point - lead)
    dur = entry.duration + (entry.in_point - ss) + tail
    zf = _zoom_filter(zoom, dur, fps, width, height)

    def _cpu_cmd() -> list[str]:
        if fill == "cover":
            vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                  f"crop={width}:{height},fps={fps},setsar=1")
        else:
            vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                  f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,fps={fps},setsar=1")
        if zf:
            vf += "," + zf
        return ["ffmpeg", "-y", "-v", "error",
                "-ss", f"{ss:.4f}", "-i", entry.src_path, "-t", f"{dur:.4f}",
                "-vf", vf, "-an", *video_encode_args, "-movflags", "+faststart", str(out)]

    def _gpu_cmd() -> list[str]:
        vf = (f"scale_cuda={width}:{height}:force_original_aspect_ratio=decrease,"
              f"pad_cuda={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,fps={fps}")
        return ["ffmpeg", "-y", "-v", "error", "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
                "-ss", f"{ss:.4f}", "-i", entry.src_path, "-t", f"{dur:.4f}",
                "-vf", vf, "-an", *video_encode_args, "-movflags", "+faststart", str(out)]

    # zoompan is CPU-only, and cover-framing needs a CPU centre-crop — either one
    # rules out the GPU path. Only a plain fit hard-cut shot can stay on the GPU.
    if gpu and not zf and fill != "cover":
        try:
            _run(_gpu_cmd(), f"render segment @ {entry.place_at:.2f}s (gpu)")
            return
        except RuntimeError:
            pass  # this clip won't decode/filter on the GPU — fall back to CPU
    _run(_cpu_cmd(), f"render segment @ {entry.place_at:.2f}s")


def assemble(segments: list[Path], song_path: Path, out_path: Path,
             *, mux_audio: bool = True) -> None:
    """Concat normalized segments and mux the song over them as the only audio.

    ``mux_audio=False`` emits a video-only file (no song) — used when the song is
    layered in afterwards with ducked dialogue (see ``_duck_and_mux``)."""
    listfile = out_path.parent / "concat.txt"
    lines = []
    for p in segments:
        # Absolute paths: the concat demuxer resolves entries relative to the
        # listfile's own directory, so a relative seg path would double up.
        safe = str(Path(p).resolve()).replace("'", "'\\''")
        lines.append(f"file '{safe}'\n")
    listfile.write_text("".join(lines), encoding="utf-8")
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listfile)]
    if mux_audio:
        cmd += ["-i", str(song_path), "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest"]
    else:
        cmd += ["-map", "0:v:0", "-c:v", "copy"]
    cmd += ["-movflags", "+faststart", str(out_path)]
    _run(cmd, "final assembly")


def assemble_transitions(segments: list[Path], durations: list[float],
                         joins: list[tuple[str, float]], song_path: Path,
                         out_path: Path, *, video_encode_args: list[str],
                         mux_audio: bool = True) -> None:
    """Assemble segments with xfade accent transitions, muxing the song over them.

    ``durations[i]`` is the rendered length of segment ``i`` (including its
    handles). ``joins[i]`` is the (transition_type, transition_dur) describing the
    join *into* segment ``i`` (``joins[0]`` is unused). Runs of consecutive hard
    cuts are first concatenated losslessly with the concat *demuxer* into one
    intermediate file each; only the genuine transition boundaries then meet in an
    ``xfade`` filtergraph. This keeps the number of simultaneously-open ffmpeg
    inputs equal to the number of runs (≈ transitions + 1) rather than the total
    segment count — a 256-clip timeline opens a few dozen decoders, not hundreds,
    which is the difference between fitting in RAM and being OOM-killed mid-render.
    Because each transition's handles add exactly its duration back across the two
    neighbours, the overlaps net out and the final video length equals the planned
    timeline, keeping the muxed song in sync.
    """
    n = len(segments)
    work_dir = out_path.parent
    # Partition into runs of consecutive hard cuts; a transition starts a new run.
    runs: list[list[int]] = []
    cur = [0]
    for i in range(1, n):
        if joins[i][1] > 0:
            runs.append(cur)
            cur = [i]
        else:
            cur.append(i)
    runs.append(cur)

    # Materialise each hard-cut run to a single file with the concat *demuxer*
    # (stream copy — every segment is rendered with identical encode params, so
    # this is lossless and uses near-zero memory, exactly like ``assemble``). A
    # length-1 run is already its own file and needs no copy.
    run_files: list[Path] = []
    run_durs: list[float] = []
    for ri, run in enumerate(runs):
        run_durs.append(sum(durations[i] for i in run))
        if len(run) == 1:
            run_files.append(segments[run[0]])
            continue
        listfile = work_dir / f"run_{ri:04d}.txt"
        lines = []
        for i in run:
            safe = str(Path(segments[i]).resolve()).replace("'", "'\\''")
            lines.append(f"file '{safe}'\n")
        listfile.write_text("".join(lines), encoding="utf-8")
        rf = work_dir / f"run_{ri:04d}.mp4"
        _run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
              "-i", str(listfile), "-c", "copy", "-movflags", "+faststart", str(rf)],
             f"transition run {ri} concat")
        run_files.append(rf)

    inputs: list[str] = []
    for p in run_files:
        inputs += ["-i", str(p)]

    # Every run is normalised to a common timebase (and pixel format / SAR) before
    # it reaches xfade. xfade outputs at AVTB (1/1000000) but raw inputs carry
    # their own container timebase, and xfade refuses to configure when a chained
    # xfade output meets a fresh input with a different timebase — so the whole
    # graph must speak one timebase end to end.
    fc: list[str] = []
    run_labels: list[str] = []
    norm = "settb=AVTB,format=yuv420p,setsar=1"
    for ri in range(len(runs)):
        lbl = f"run{ri}"
        fc.append(f"[{ri}:v]{norm}[{lbl}]")
        run_labels.append(lbl)

    cur_label = run_labels[0]
    running = run_durs[0]
    for ri in range(1, len(runs)):
        ttype, td = joins[runs[ri][0]]
        offset = max(0.0, running - td)
        out_label = f"x{ri}"
        fc.append(f"[{cur_label}][{run_labels[ri]}]"
                  f"xfade=transition={ttype}:duration={td:.4f}:offset={offset:.4f}[{out_label}]")
        running = running + run_durs[ri] - td
        cur_label = out_label

    nf = len(run_files)
    cmd = ["ffmpeg", "-y", "-v", "error", *inputs]
    if mux_audio:
        cmd += ["-i", str(song_path), "-filter_complex", ";".join(fc),
                "-map", f"[{cur_label}]", "-map", f"{nf}:a:0",
                *video_encode_args, "-c:a", "aac", "-b:a", "192k", "-shortest"]
    else:
        cmd += ["-filter_complex", ";".join(fc), "-map", f"[{cur_label}]",
                *video_encode_args]
    cmd += ["-movflags", "+faststart", str(out_path)]
    _run(cmd, "final assembly (transitions)")


def _duck_expr(a: float, p: float, b: float, c: float, gain: float) -> str:
    """A piecewise ``volume`` gain expression (eval=frame) for one spoken window:
    full → fade DOWN over [a,p] → hold ``gain`` over [p,b] → fade UP over [b,c] →
    full. ``c`` is beat-aligned by the caller so the music swells back in on a beat.
    """
    fd = max(1e-3, p - a)
    fu = max(1e-3, c - b)
    rise = 1.0 - gain
    return (f"if(lt(t,{a:.4f}),1,"
            f"if(lt(t,{p:.4f}),1-{rise:.4f}*(t-{a:.4f})/{fd:.4f},"
            f"if(lt(t,{b:.4f}),{gain:.4f},"
            f"if(lt(t,{c:.4f}),{gain:.4f}+{rise:.4f}*(t-{b:.4f})/{fu:.4f},1))))")


def _duck_and_mux(video_path: Path, song_path: Path, windows: list[dict],
                  out_path: Path) -> None:
    """Lay the song over a finished (audio-less) video, ducking it beneath each
    spoken line and dropping the clip's own dialogue on top.

    The song is the base track. For every window we (a) ride its level with a
    frame-evaluated ``volume`` envelope — fade down into the line, hold, fade back
    up to land on a beat — and (b) delay that clip's audio to the window's start,
    then ``amix`` with ``normalize=0`` so the song keeps its level and the line
    sits over the dip. Every source is forced to a common format so amix never
    bails on a sample-rate / channel-layout mismatch.

    Each ``windows`` entry: ``in_point``/``dur``/``src_path`` (the dialogue to pull
    and where it sits via ``place_at``), and ``a_start``/``place_at``/``up_start``/
    ``up_end`` marking the fade-down start, full-duck point, fade-up start and end.
    """
    inputs = ["-i", str(video_path), "-i", str(song_path)]
    for w in windows:
        inputs += ["-ss", f"{w['in_point']:.4f}", "-t", f"{w['dur']:.4f}", "-i", w["src_path"]]
    fmt = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
    fc: list[str] = []
    prev = "[1:a]"                                   # the song
    for i, w in enumerate(windows):
        expr = _duck_expr(w["a_start"], w["place_at"], w["up_start"], w["up_end"], DUCK_GAIN)
        lbl = f"[s{i}]"
        fc.append(f"{prev}volume=eval=frame:volume='{expr}'{lbl}")
        prev = lbl
    fc.append(f"{prev}{fmt}[song]")
    dl: list[str] = []
    for i, w in enumerate(windows):
        delay = int(round(w["place_at"] * 1000))
        lbl = f"[d{i}]"
        fc.append(f"[{2 + i}:a]{fmt},adelay={delay}|{delay}{lbl}")
        dl.append(lbl)
    fc.append(f"[song]{''.join(dl)}amix=inputs={1 + len(windows)}:normalize=0:duration=first[aout]")
    cmd = ["ffmpeg", "-y", "-v", "error", *inputs,
           "-filter_complex", ";".join(fc),
           "-map", "0:v:0", "-map", "[aout]", "-c:v", "copy",
           "-c:a", "aac", "-b:a", "192k", "-shortest",
           "-movflags", "+faststart", str(out_path)]
    _run(cmd, "final assembly (ducked dialogue)")


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
    preset = options.get("preset") or DEFAULT_PRESET
    cfg = PRESETS.get(preset, PRESETS[DEFAULT_PRESET])
    enhanced = bool(cfg["enhanced_analysis"])
    transitions = cfg["transitions"]
    rhythm = cfg.get("rhythm", "density")
    motion_weight = float(cfg.get("motion_weight", W_MOTION))
    fill = cfg.get("fill", "fit")
    push_in = float(cfg.get("push_in", 0.0))
    punch = float(cfg.get("punch", 0.0))
    # "Let clips speak" is preset-independent: a checkbox + how many moments.
    let_speak = bool(options.get("let_clips_speak"))
    speak_moments = options.get("speak_moments", "auto")

    # 1. audio --------------------------------------------------------------- #
    progress("analyzing_audio", _overall("analyzing_audio", 0.0), 0.0, "Analyzing audio…")
    grid = analyze_audio(song_path, enhanced=enhanced)
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

    # 2b. speech moments (optional, preset-independent) ---------------------- #
    # Read dialogue timings from each usable clip's subtitle sidecar, then pick a
    # few substantial lines to feature in the song's quiet pockets. Empty (a no-op)
    # when the box is off or no clip has a qualifying line.
    forced_speech: list[SpeechMoment] = []
    if let_speak:
        clip_dur = {c.clip_id: c.duration for c in curves}
        utterances: list[Utterance] = []
        for c in curves:
            utterances += _utterances_for_clip(int(c.clip_id), Path(c.src_path),
                                                clip_dur[c.clip_id])
        T = float(target or grid.duration)
        if str(speak_moments).lower() == "auto":
            want = max(1, min(4, round(T / 60.0)))
        else:
            try:
                want = max(0, int(speak_moments))
            except (TypeError, ValueError):
                want = 1
        forced_speech = _pick_speech_moments(grid, utterances, T, want)

    # 3. plan ---------------------------------------------------------------- #
    progress("planning", _overall("planning", 0.0), 0.0, "Planning cuts…")
    edl = plan_cuts(grid, curves, tightness=tightness, target_duration=target,
                    seed=seed, transitions=transitions, rhythm=rhythm,
                    motion_weight=motion_weight,
                    overuse_w=float(cfg.get("overuse_w", W_OVERUSE)),
                    overuse_p=float(cfg.get("overuse_p", 1.0)),
                    forced_speech=forced_speech)
    (work_dir / "edl.json").write_text(edl.to_json(), encoding="utf-8")
    n_trans = sum(1 for e in edl.entries if e.transition_dur > 0)
    n_speak = sum(1 for e in edl.entries if e.speak)
    speak_note = ""
    if let_speak:
        speak_note = (f", {n_speak} spoken line(s)" if n_speak
                      else ", no spoken lines found")
    progress("planning", _overall("planning", 1.0), 1.0,
             f"{len(edl.entries)} cuts"
             + (f", {n_trans} transitions" if n_trans else "") + speak_note)

    # 4. render + assemble --------------------------------------------------- #
    # Handles: each transition borrows transition_dur/2 of source from the clip on
    # either side of the join, so the overlaps net out to the planned timeline.
    # Drop beats (energy hits) get a centred zoom punch; resolve them once so the
    # render loop can match each shot's place_at against them.
    drop_times = ([t for t, is_drop in _accent_times(grid, edl.timeline_duration) if is_drop]
                  if punch > 0 else [])
    beat_period = 60.0 / grid.tempo if grid.tempo and grid.tempo > 0 else 0.5
    drop_tol = max(0.5 * beat_period, 0.15)
    n = len(edl.entries)
    segments: list[Path] = []
    durations: list[float] = []
    joins: list[tuple[str, float]] = []
    for i, entry in enumerate(edl.entries):
        sp = i / max(1, n)
        progress("rendering", _overall("rendering", sp * 0.9), sp,
                 f"Rendering segment {i + 1} of {n}")
        lead = entry.transition_dur / 2.0           # incoming transition (this join)
        nxt = edl.entries[i + 1] if i + 1 < n else None
        tail = (nxt.transition_dur / 2.0) if nxt else 0.0   # outgoing transition
        # Per-shot motion is a sparse, centred accent — never a pan. A shot landing
        # on a drop beat gets a zoom *punch* (the camera kick on the hit); a long
        # held shot gets a slow push-in so it breathes; everything else stays
        # locked off. The punch wins when a held shot also happens to be a drop.
        zoom = None
        on_drop = punch > 0 and any(abs(entry.place_at - dt) <= drop_tol for dt in drop_times)
        if on_drop and entry.duration >= 0.4:
            zoom = ("punch", punch)
        elif push_in > 0 and entry.duration >= HELD_MIN_S:
            zoom = ("push_in", push_in)
        seg = work_dir / f"seg_{i:04d}.mp4"
        _render_segment(entry, seg, width=width, height=height, fps=fps,
                        video_encode_args=video_encode_args, gpu=hwaccel_decode,
                        lead=lead, tail=tail, fill=fill, zoom=zoom)
        segments.append(seg)
        # Use the segment's *actual* rendered length, not the nominal target:
        # frame-rounding makes each clip a few ms short, and across a run that
        # accumulates enough to push an xfade offset past the real end of its
        # first input, which silently drops everything after the transition.
        durations.append(probe_duration(seg) if n_trans else entry.duration + lead + tail)
        joins.append((entry.transition, entry.transition_dur))

    progress("rendering", _overall("rendering", 0.95), 0.95, "Assembling final cut…")
    # Build the duck envelope for each spoken shot: fade the music DOWN just before
    # the line, hold, then fade it back UP to land on the next beat ("into the beat").
    beat_times = [b.time for b in grid.beats]
    duck_windows: list[dict] = []
    for e in edl.entries:
        if not e.speak:
            continue
        up_start = e.place_at + e.duration
        nb = next((t for t in beat_times if t > up_start + 0.05), None)
        fu = (_clamp(nb - up_start, SPEAK_UNDUCK_MIN, SPEAK_UNDUCK_MAX) if nb is not None
              else SPEAK_UNDUCK_MIN)
        duck_windows.append({
            "place_at": e.place_at, "dur": e.duration, "in_point": e.in_point,
            "src_path": e.src_path, "a_start": max(0.0, e.place_at - SPEAK_DUCK_FADE),
            "up_start": up_start, "up_end": up_start + fu,
        })
    if duck_windows:
        # Build the video first (no song), then lay the song under it — ducked with
        # a fade in/out — and mix each clip's own dialogue in over the dip.
        tmp_video = work_dir / "video_only.mp4"
        if n_trans:
            assemble_transitions(segments, durations, joins, song_path, tmp_video,
                                 video_encode_args=video_encode_args, mux_audio=False)
        else:
            assemble(segments, song_path, tmp_video, mux_audio=False)
        _duck_and_mux(tmp_video, song_path, duck_windows, out_path)
    elif n_trans:
        assemble_transitions(segments, durations, joins, song_path, out_path,
                             video_encode_args=video_encode_args)
    else:
        assemble(segments, song_path, out_path)

    size = out_path.stat().st_size
    return {
        "width": width, "height": height, "fps": fps,
        "duration": round(edl.timeline_duration, 3),
        "size_bytes": size, "cuts": len(edl.entries), "seed": seed,
        "preset": preset, "transitions": n_trans, "spoken": len(duck_windows),
    }


# --------------------------------------------------------------------------- #
# Subprocess worker entry
# --------------------------------------------------------------------------- #
# server.py runs generate() in a SEPARATE PROCESS (`python moviegen.py render`),
# not a thread, so the OS reclaims this job's whole memory footprint — the librosa
# arrays plus the numba JIT state (~2.5GB) — the instant the process exits. A render
# living in the long-lived server process could never give that memory back, because
# CPython/glibc keep freed heap arenas mapped. The job spec arrives as one JSON
# object on stdin; progress plus the final result/error leave as newline-delimited
# JSON on stdout for the parent to mirror into its job state. stderr carries
# librosa/numba chatter and any traceback, which the parent captures for diagnostics.

def _worker_main() -> int:
    import sys

    spec = json.loads(sys.stdin.read())

    def emit(obj: dict) -> None:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def progress(status, overall, stage_progress=0.0, detail="") -> None:
        emit({"t": "progress", "status": status, "overall": overall,
              "stage_progress": stage_progress, "detail": detail})

    try:
        result = generate(
            video_paths=[Path(p) for p in spec["video_paths"]],
            song_path=Path(spec["song_path"]),
            options=spec["options"],
            work_dir=Path(spec["work_dir"]),
            out_path=Path(spec["out_path"]),
            video_encode_args=list(spec["video_encode_args"]),
            hwaccel_decode=bool(spec["hwaccel_decode"]),
            progress=progress,
        )
        emit({"t": "result", "result": result})
        return 0
    except Exception as exc:
        emit({"t": "error", "error": str(exc)[:400]})
        return 1


if __name__ == "__main__":
    import sys

    # The only entry point: `python moviegen.py render` drives one job from a JSON
    # spec on stdin. Anything else is a misuse (this module is otherwise a library).
    raise SystemExit(_worker_main() if sys.argv[1:2] == ["render"] else 2)
