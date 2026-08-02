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
import gzip
import hashlib
import json
import os
import random
import re
import signal
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
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

# Segment renders are independent single-input ffmpeg jobs, so they run in a small
# worker pool. Sub-second Music Video segments are dominated by per-process spawn +
# input-open + seek overhead, not encode time, so parallelism is near-linear there.
# 6 leaves 2 NVENC sessions of the GeForce 8-session cap (driver 550+) free for
# whatever else the server encodes mid-render (Merge/Export, subtitle burn) — the
# CPU-filter segment path has NO libx264 fallback, so running AT the cap turns a
# concurrent encode into a dead render. MOVIE_RENDER_WORKERS overrides (1 = the
# old strictly-serial behaviour); the same pool drives motion-analysis decodes
# (NVDEC, uncapped) and the library warm-up.
try:
    RENDER_WORKERS = max(1, min(16, int(os.environ.get("MOVIE_RENDER_WORKERS", "") or 6)))
except ValueError:
    RENDER_WORKERS = 6

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
# Motion Match Cut has its own stages: no audio analysis (the song is optional and
# never sets the cut points), plus a "matching" stage that scores every ordered clip
# pair's best seam. Kept as a SEPARATE table — _overall() takes it as an argument
# rather than the module dicts being mutated, so the beat pipeline is untouched.
MATCHCUT_STAGE_WEIGHTS = {"analyzing_motion": 0.45, "matching": 0.15,
                          "planning": 0.05, "rendering": 0.35}
MATCHCUT_STAGE_ORDER = ["analyzing_motion", "matching", "planning", "rendering"]
# Auto Montage adds a "selecting" stage (score the candidate library against the
# song, pure cache reads) between audio and motion. Separate table, same rule as
# matchcut: never mutate the beat tables — that reweights every existing bar.
AUTOPICK_STAGE_WEIGHTS = {"analyzing_audio": 0.10, "selecting": 0.03,
                          "analyzing_motion": 0.42, "planning": 0.05,
                          "rendering": 0.40}
AUTOPICK_STAGE_ORDER = ["analyzing_audio", "selecting", "analyzing_motion",
                        "planning", "rendering"]

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
                  "overuse_w": 1.0, "overuse_p": 2.0,
                  # Reserve the most-cinematic clip for the strongest drop, and ramp the
                  # held shots leading into it down to 0.35x (tension -> release). Classic/
                  # moody never set these keys, so they + the golden tests are byte-for-byte
                  # unchanged. See hero_shot/breakdown_slowmo in plan_cuts.
                  "hero_shot": True, "breakdown_slowmo": 0.35},
    "moody":     {"enhanced_analysis": True,  "rhythm": "phrase",  "motion_weight": 0.0,
                  "transitions": "accents", "fill": "fit",   "push_in": 0.12, "punch": 0.0,
                  "overuse_w": W_OVERUSE, "overuse_p": 1.0},
    # "musicvideo" (UI: "Music Video") — the high-energy PMV/velocity-edit style:
    # cinematic's cover-framed, motion-favouring, library-spreading base PLUS sub-beat
    # "machine-gun" cutting (onset-driven flurries in loud passages, see machine_gun in
    # plan_cuts), harder + more frequent zoom punches, a white flash on loud downbeat
    # cuts, and a saturated neon grade. The most intense preset.
    "musicvideo": {"enhanced_analysis": True, "rhythm": "density", "motion_weight": W_MOTION,
                   "transitions": "accents", "fill": "cover", "push_in": 0.06, "punch": 0.22,
                   "overuse_w": 1.0, "overuse_p": 2.0, "scene_aware": True,
                   "machine_gun": True, "grade": "neon", "flash": 0.05,
                   "rgb_split": 0.006, "shake": 0.04,
                   # The PMV slow-mo-into-the-drop + hero slam, same ramp as cinematic.
                   # Only fires when the song has a quiet held pocket before the drop —
                   # the machine-gunned loud sections have no slowable held shots, so on a
                   # wall-to-wall banger it stays out of the way.
                   "hero_shot": True, "breakdown_slowmo": 0.35},
    # "picvideo" (UI: "Picture & Video") — the Music Video style, but STILL IMAGES may
    # ride along as beats (looped into their slot with a Ken-Burns push at render time).
    # Mirrors "musicvideo" EXCEPT two keys: `allow_stills` opens the gate for images (the
    # server resolver and the ingest branch both key off it — no other preset sets it, so
    # every other mode stays strictly video-only and the golden planner tests are
    # unaffected), and `motion_weight` drops to 0 so a motionless still competes evenly
    # with real footage for every slot instead of being starved of the high-energy ones.
    # Stills are letterboxed (the render forces fill="fit" for an image regardless of the
    # "cover" here) so a portrait photo keeps its whole frame.
    "picvideo":  {"enhanced_analysis": True, "rhythm": "density", "motion_weight": 0.0,
                  "transitions": "accents", "fill": "cover", "push_in": 0.06, "punch": 0.22,
                  "overuse_w": 1.0, "overuse_p": 2.0, "scene_aware": True,
                  "machine_gun": True, "grade": "neon", "flash": 0.05,
                  "rgb_split": 0.006, "shake": 0.04,
                  "hero_shot": True, "breakdown_slowmo": 0.35,
                  "allow_stills": True},
}
DEFAULT_PRESET = "classic"

# Named colour grades appended to the per-segment render filtergraph. CPU-only —
# eq/colorbalance have no CUDA equivalent, so a graded segment leaves the NVENC path.
# "neon": push saturation + contrast and lift the shadows cool/magenta — the
# over-cooked music-video look that fuses heterogeneous clips into one world.
GRADES: dict[str, str] = {
    "neon": "eq=saturation=1.35:contrast=1.10:gamma=0.96,colorbalance=bs=0.06:rh=0.04",
}

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

# Music Video preset — sub-beat "machine-gun" cutting + on-beat white flash.
MACHINE_GUN_ENERGY = 0.55   # only add sub-beat cuts where the smoothed energy is >= this
MACHINE_GUN_MIN_BEATS = 0.5 # keep every sub-beat cut at least this many beats from any other
FLASH_ENERGY = 0.50         # white-flash a downbeat cut only when that beat is at least this loud

# Scene-cut detection (PySceneDetect's content-diff idea, computed straight off the
# motion curve we already have — a hard cut is a sharp frame-diff spike, so no extra
# decode pass and no OpenCV). Deliberately conservative: single-shot AI clips yield
# none, so no spurious penalties. Used only by scene-aware presets (Music Video).
SCENE_SPIKE_ABS = 0.62     # a cut frame's peak-normalized motion must clear this
SCENE_SPIKE_REL = 3.5      # ...and be at least this many times the local baseline
SCENE_LOCAL_WIN = 6        # samples each side that form the local baseline
# Planner penalties when scene-aware (Music Video) — mugen-style segment rejection:
# drop candidate windows that straddle an internal scene cut or are near-static.
W_SCENE_SPAN = 0.6
W_LOWCONTRAST = 0.5
LOWCONTRAST_FLOOR = 0.045  # mean normalized motion below this reads as dead footage

# Character reference-sheet head trim. Grok prepends some generations with a
# "character sheet" card (a grid of poses + face crops) that then transitions into
# the real footage. Two shapes exist in the wild, both unmistakable on the RAW
# (unnormalized) frame-diff curve, so the montage analysis pass and the server's
# Merge/Export head scan detect them the same way (_head_trim_from_diffs):
#   - FLASH card (measured on real exports): the card lives on the very first
#     frame(s) and cross-fades out within ~0.25s — a huge first diff (~45-65 vs a
#     settled baseline of ~2-5), i.e. raw[0] towers over the post-dissolve median.
#   - HELD card: the card sits static for ~0.5-1.0s, then cuts/dissolves — a run
#     of near-zero diffs ended by a spike run.
# Thresholds are on the raw 0-255 mean-abs-diff scale: detection MUST run before
# peak normalization, because the card's exit spike is often the clip's peak — it
# would otherwise both survive as the normalization divisor (deflating every real
# motion value) and attract the planner's peak-anchor math to the card itself.
HEAD_STATIC_DIFF = 2.0     # raw diff below which a frame pair reads as "held still"
HEAD_SPIKE_DIFF = 12.0     # raw diff that reads as part of the cut/dissolve out of the card
HEAD_MIN_STATIC_S = 0.25   # a HELD card needs at least this much leading stillness
HEAD_FLASH_MIN = 20.0      # a FLASH card's first diff must be at least this big...
HEAD_FLASH_RATIO = 5.0     # ...and this many times the settled post-dissolve median
                           # (real cards measure 14-23x; opening motion stays ~1x)
HEAD_DISSOLVE_MAX_S = 0.5  # a card's exit spike-run can't run longer (that's real action)
HEAD_MAX_TRIM_S = 1.5      # a card exit later than this is content, not an intro card
HEAD_MIN_REMAIN_S = 1.0    # never trim a clip down below this much remaining footage
HEAD_SCAN_S = 3.0          # how much of the head detect_head_trim() decodes

# Picture & Video preset — still images allowed as beats. A still has no container
# duration and no decodable motion, so it never survives the normal video pipeline
# (motion analysis drops it, and the planner's length gate rejects a zero-duration
# clip). Two surgical accommodations, both gated so no other preset ever sees an image:
# at ingest a still gets a synthetic flat MotionCurve (see _still_curve), and at render
# it's looped into its slot with a Ken-Burns push (see _render_segment's image branch).
# A path is a path — no media-type field flows through the JSON spec — so IMAGE_EXTS is
# the discriminator at both the ingest and render branches.
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
STILL_HOLD_MAX = 12.0   # synthetic duration a still reports — long enough to clear the
                        # planner's `c.duration < d` length gate for any realistic beat
                        # slot, bounded so it doesn't distort max_clip-driven interval
                        # subdivision when a selection is all stills.
STILL_MOTION = 0.35     # synthetic motion for a still: in [LOWCONTRAST_FLOOR,
                        # SCENE_SPIKE_ABS) so it never reads as dead footage or a scene
                        # cut. picvideo runs motion_weight=0, so this only lightly
                        # colours the energy-match term — a still competes evenly.

# Accent-transition tunables (cinematic preset only).
TRANSITION_MIN = 0.30     # seconds; shorter is too quick to register as a transition
TRANSITION_MAX = 0.70     # seconds; longer steals too much of a fast shot
# Canonical (deterministic, seed=None) transition per context, plus the taste-set
# sampled from when a seed is supplied. xfade transition names are stock ffmpeg.
_TRANSITION_SECTION = "dissolve"            # at a structural section start
_TRANSITION_DROP = "fadeblack"              # on an energy drop / downbeat hit
_TRANSITION_VARIANTS_SECTION = ["dissolve", "fade", "smoothleft", "smoothright"]
_TRANSITION_VARIANTS_DROP = ["fadeblack", "fadewhite", "circleopen"]

# Cinematic "breakdown" slow-motion (cinematic preset only). Real setpts slow-mo on the
# 1-2 held shots leading INTO the strongest drop — a decel ramp, then the reserved hero
# shot SLAMS at full speed (tension -> release). Render-time only via
# EDLEntry.playback_speed, so the timeline still tiles exactly. Deliberately surgical so
# it adds a single dramatic moment without draining the energetic density pacing.
SLOWMO_MIN_SPEED = 0.3       # clamp floor for breakdown_slowmo (never crawl footage to a halt)
SLOWMO_MAX_SHOT_SPEED = 0.7  # a slowed shot must reach at least this slow, else skip (not worth it)
SLOWMO_MAX_ENTRIES = 2       # at most this many shots slowed (a 2-shot decel ramp into the drop)
SLOWMO_RAMP_S = 4.5          # ...and at most this many seconds of footage, total
SLOWMO_LOOKBACK_S = 6.0      # only slow shots within this many seconds before the drop
SLOWMO_MIN_SRC = 0.5         # min source seconds a slowed shot pulls (floors its speed, doesn't skip)

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
    # Which engine produced the beats/downbeats and the device it ran on — surfaced so
    # a render can report whether madmom actually ran.
    engine: str = "librosa"
    device: str = "cpu"
    # When madmom was requested but we fell back to librosa, WHY (exception text or
    # "not installed"). Empty when madmom ran or wasn't requested. Threaded into the
    # result so a silent fallback stays diagnosable after the scratch log is purged.
    engine_note: str = ""


@dataclass
class MotionCurve:
    clip_id: str
    src_path: str
    duration: float
    fps_analyzed: float
    samples: list[float] = field(default_factory=list)  # normalized 0..1
    # Seconds cut off the clip's head (a detected character-sheet intro card).
    # duration/samples and every window time below are in TRIMMED clip time; the
    # render adds this back when seeking the source (see _render_segment).
    head_offset: float = 0.0
    _prefix: list[float] = field(default_factory=list, repr=False)
    scene_cuts: list[float] = field(default_factory=list, repr=False)  # internal shot-change times

    def __post_init__(self) -> None:
        # Prefix sums for O(1) window means.
        acc = 0.0
        self._prefix = [0.0]
        for s in self.samples:
            acc += s
            self._prefix.append(acc)
        self.scene_cuts = self._detect_scene_cuts()

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

    def _detect_scene_cuts(self) -> list[float]:
        """Internal shot-change times (s): sharp, isolated frame-diff spikes. Empty
        for single-shot footage (the common case for AI clips). See SCENE_* tunables."""
        s, fps = self.samples, self.fps_analyzed
        n = len(s)
        if n < 3 or fps <= 0:
            return []
        w = SCENE_LOCAL_WIN
        cuts: list[float] = []
        for i in range(1, n):
            if s[i] < SCENE_SPIKE_ABS or s[i] < s[i - 1]:
                continue
            lo, hi = max(0, i - w), min(n, i + w + 1)
            neigh = [s[j] for j in range(lo, hi) if j != i]
            base = sum(neigh) / len(neigh) if neigh else 0.0
            if s[i] >= SCENE_SPIKE_REL * base:
                cuts.append(i / fps)
        return cuts

    def spans_scene_cut(self, start: float, length: float) -> bool:
        """True if an internal scene cut falls strictly inside the rendered window
        (so the shot would cut across a hidden shot change). 0.05s edge margin so a
        shot that merely STARTS/ENDS on a cut doesn't count."""
        a, b = start + 0.05, start + length - 0.05
        return any(a < c < b for c in self.scene_cuts)

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
    # Render-time slow-motion factor (ffmpeg setpts). 1.0 = real time; <1.0 = slow-mo.
    # Pure render metadata — NEVER alters duration/place_at/in_point/out_point, so the
    # clip fills the SAME timeline slot (it just consumes proportionally less source,
    # which setpts stretches back to fill). The duration-tiling invariant stays exact.
    playback_speed: float = 1.0
    # Source-time seconds trimmed off this clip's head (detected intro card).
    # in_point/out_point are in trimmed clip time; the render seek adds this back.
    head_offset: float = 0.0


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

_MADMOM_NOTE = ""  # why the last madmom attempt fell back to librosa (for the result readout)


def _madmom_beats(song_path: Path):
    """Steady beats + REAL downbeats from madmom's RNN + DBN downbeat tracker
    (madmom-modern — the Python-3.12 / NumPy-2 fork; the 2018 madmom won't build).
    madmom is the gold-standard downbeat tracker and gives a rock-steady grid
    (verified: ~143 BPM, inter-beat-interval CV 3%, correct bar phase) where beat_this's
    neural model octave-locked to a jittery half-tempo (73 BPM, CV 25%) on the same
    track — even with its DBN mode. CPU-only, ~tens of MB (no torch). Returns
    ``(beat_times, downbeat_times, tempo, "cpu")`` or ``None`` when madmom isn't
    installed (e.g. the local Python-3.10 venv) so the caller falls back to librosa — a
    pure upgrade with no hard dependency.

    The win over librosa: librosa has no downbeat tracker, so otherwise every preset
    fakes downbeats as every-4th-beat; madmom gives the real bar-ones that the
    Cinematic phrase/section structure needs.
    """
    global _MADMOM_NOTE
    _MADMOM_NOTE = ""
    import sys
    try:
        import statistics
        from madmom.features.downbeats import RNNDownBeatProcessor, DBNDownBeatTrackingProcessor
        act = RNNDownBeatProcessor()(str(song_path))                      # per-frame beat+downbeat activations
        res = DBNDownBeatTrackingProcessor(beats_per_bar=[3, 4], fps=100)(act)  # rows: [time, position-in-bar]
        if res is None or len(res) < 2:
            _MADMOM_NOTE = "madmom returned no beats"
            print("[moviegen] madmom returned no beats; using librosa", file=sys.stderr)
            return None
        beats = [float(t) for t in res[:, 0]]
        downbeats = [float(t) for t, pos in res if int(round(pos)) == 1]  # bar position 1 == downbeat
        diffs = [b - a for a, b in zip(beats, beats[1:]) if b > a]
        tempo = 60.0 / statistics.median(diffs) if diffs else 0.0
        print(f"[moviegen] madmom beats: {len(beats)} beats / {len(downbeats)} downbeats "
              f"@ {tempo:.0f} BPM", file=sys.stderr)
        return beats, downbeats, tempo, "cpu"
    except (ImportError, ModuleNotFoundError) as exc:  # not installed (e.g. local py3.10) -> expected
        _MADMOM_NOTE = "madmom not installed"
        print(f"[moviegen] madmom not installed ({exc}); using librosa", file=sys.stderr)
        return None
    except Exception as exc:  # installed but failed at runtime -> the case worth surfacing
        _MADMOM_NOTE = f"madmom error: {type(exc).__name__}: {exc}"
        print(f"[moviegen] madmom unavailable ({exc}); using librosa", file=sys.stderr)
        return None


def analyze_audio(song_path: Path, *, enhanced: bool = False,
                  beat_engine: str = "librosa") -> BeatGrid:
    """librosa beat grid + per-beat energy + sections.

    ``enhanced=False`` (classic) uses ``beat_track`` with median-energy-crossing
    sections and no onsets — the original v1 behaviour, kept byte-for-byte.

    ``enhanced=True`` (cinematic) drives ``beat_track`` from an onset-strength
    envelope (tighter beats), records transient onset times, falls back to PLP
    pulse peaks when beat tracking degenerates on tempo-varying material, and
    derives real structural sections via ``librosa.segment`` instead of the
    median heuristic. Downbeats are every-4th-beat in both librosa modes; set
    ``beat_engine="neural"`` (env ``BEAT_ENGINE``) to get REAL beats + downbeats
    from madmom instead (see ``_madmom_beats``; it falls back to librosa when
    madmom isn't installed or errors, recording why in ``BeatGrid.engine_note``).
    """
    import numpy as np
    import librosa

    y, sr = librosa.load(str(song_path), sr=None, mono=True)
    duration = float(len(y) / sr) if sr else 0.0

    onsets: list[float] = []
    downbeat_times: list[float] | None = None  # real downbeats (neural engine); else None
    engine, device = "librosa", "cpu"          # which beat engine actually ran (for the readout)
    neural = _madmom_beats(song_path) if beat_engine in ("neural", "madmom") else None
    engine_note = _MADMOM_NOTE if (neural is None and beat_engine in ("neural", "madmom")) else ""
    if neural is not None:
        # Real beats + downbeats from madmom; still derive onsets + structural sections
        # below (madmom gives neither). The neural engine always implies "enhanced".
        beat_list, downbeat_times, tempo, device = neural
        engine = "madmom"
        beat_times = np.asarray(beat_list, dtype=float)
        beat_frames = (librosa.time_to_frames(beat_times, sr=sr)
                       if len(beat_times) else np.asarray([], dtype=int))
        onset_env = librosa.onset.onset_strength(y=y, sr=sr)
        onset_frames = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr)
        onsets = [float(t) for t in librosa.frames_to_time(onset_frames, sr=sr)]
        enhanced = True
    elif enhanced:
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
        beat_times = librosa.frames_to_time(beat_frames, sr=sr)
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

    # Map each beat to its section + downbeat flag. Real downbeats from the neural
    # engine when present; otherwise the every-4th-beat 4/4 heuristic.
    db_round = {round(d, 2) for d in downbeat_times} if downbeat_times is not None else None
    beats: list[Beat] = []
    for i, t in enumerate(times):
        sid = next((s.id for s in sections if s.start <= t < s.end), sections[-1].id)
        is_db = (round(float(t), 2) in db_round) if db_round is not None else (i % 4 == 0)
        beats.append(Beat(time=float(t), is_downbeat=is_db,
                          energy=float(energies[i]), section_id=sid))

    return BeatGrid(duration=duration, tempo=tempo, beats=beats,
                    sections=sections, onsets=onsets, engine=engine, device=device,
                    engine_note=engine_note)


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
# Beat-grid cache — analyze_audio is deterministic per (song bytes, engine, mode),
# so a re-used track skips the whole madmom/librosa stage on later renders.
# --------------------------------------------------------------------------- #

BEAT_CACHE_VERSION = 1
BEAT_CACHE_MAX_ENTRIES = 200   # entries are a few KB each; LRU by mtime past this


def _madmom_available() -> bool:
    """Whether madmom is importable HERE — part of the cache key, so the local
    venv (no madmom) and the container (madmom) never share entries, and
    installing madmom later invalidates any librosa-fallback grids."""
    import importlib.util
    try:
        return importlib.util.find_spec("madmom") is not None
    except Exception:
        return False


def _beat_cache_key(song_path: Path, *, enhanced: bool, beat_engine: str) -> str:
    """Key for one analysis of one song. CONTENT-hashed, never path/mtime: the
    server re-saves the uploaded song into the render scratch dir (which every
    render wipes), so the same track arrives at the same path with a fresh mtime
    each time. Everything that changes the resulting grid is in the key."""
    h = hashlib.sha1()
    with open(song_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    neural = beat_engine in ("neural", "madmom")
    parts = [f"v{BEAT_CACHE_VERSION}", h.hexdigest(),
             f"enh{int(bool(enhanced))}", "engneural" if neural else "englibrosa"]
    if neural:
        parts.append(f"madmom{int(_madmom_available())}")
    return ":".join(parts)


def _beat_cache_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / f"{hashlib.sha1(key.encode()).hexdigest()}.json.gz"


def _grid_to_payload(grid: BeatGrid) -> dict:
    return {
        "duration": grid.duration, "tempo": grid.tempo,
        "beats": [[b.time, int(b.is_downbeat), b.energy, b.section_id]
                  for b in grid.beats],
        "sections": [[s.id, s.start, s.end, s.intensity] for s in grid.sections],
        "onsets": grid.onsets, "engine": grid.engine, "device": grid.device,
        "engine_note": grid.engine_note,
    }


def _grid_from_payload(d: dict) -> BeatGrid:
    return BeatGrid(
        duration=float(d["duration"]), tempo=float(d["tempo"]),
        beats=[Beat(time=float(t), is_downbeat=bool(db), energy=float(e),
                    section_id=int(sid)) for t, db, e, sid in d["beats"]],
        sections=[Section(id=int(i), start=float(s), end=float(e),
                          intensity=float(x)) for i, s, e, x in d["sections"]],
        onsets=[float(t) for t in d.get("onsets", [])],
        engine=d.get("engine", "librosa"), device=d.get("device", "cpu"),
        engine_note=d.get("engine_note", ""),
    )


def _prune_beat_cache(cache_dir: Path) -> None:
    """Drop the oldest entries past BEAT_CACHE_MAX_ENTRIES (LRU — a cache hit
    re-touches its file's mtime). Best-effort; never breaks a render."""
    try:
        entries = sorted(cache_dir.glob("*.json.gz"),
                         key=lambda f: f.stat().st_mtime, reverse=True)
        for f in entries[BEAT_CACHE_MAX_ENTRIES:]:
            f.unlink(missing_ok=True)
    except OSError:
        pass


def load_or_analyze_audio(song_path: Path, *, enhanced: bool, beat_engine: str,
                          cache_dir: Path | None) -> tuple[BeatGrid, bool]:
    """``analyze_audio`` with a content-keyed cache in front; returns
    ``(grid, hit)``. JSON round-trips Python floats exactly (repr-based), so a
    cached grid is identical to a fresh analysis. Any cache trouble — unreadable
    entry, unwritable dir — silently falls back to analyzing."""
    key = None
    if cache_dir is not None:
        try:
            key = _beat_cache_key(song_path, enhanced=enhanced, beat_engine=beat_engine)
            p = _beat_cache_path(cache_dir, key)
            if p.is_file():
                with gzip.open(p, "rt", encoding="utf-8") as fh:
                    grid = _grid_from_payload(json.load(fh))
                os.utime(p)                      # LRU touch
                return grid, True
        except Exception:
            key = None                           # bad entry/unhashable song -> analyze
    grid = analyze_audio(song_path, enhanced=enhanced, beat_engine=beat_engine)
    if cache_dir is not None and key is not None:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            p = _beat_cache_path(cache_dir, key)
            tmp = p.with_suffix(".tmp")
            with gzip.open(tmp, "wt", encoding="utf-8") as fh:
                json.dump(_grid_to_payload(grid), fh)
            tmp.replace(p)
            _prune_beat_cache(cache_dir)
        except Exception:
            pass                                 # cache write is best-effort
    return grid, False


# --------------------------------------------------------------------------- #
# Stage 2 — motion analysis -> MotionCurve per clip
# --------------------------------------------------------------------------- #

def _gray_diffs(src: Path, hwaccel_decode: bool, limit_s: float | None = None,
                timeout_s: float = 900.0) -> list[float]:
    """RAW mean-abs frame diffs (0-255 scale) at ANALYSIS_FPS over downscaled
    grayscale frames — the shared decode pass behind analyze_motion and
    detect_head_trim. ``limit_s`` caps how much of the clip is decoded (the
    head-only scan); None decodes the whole clip.

    ``timeout_s`` is a kill-watchdog on the decode: unlike every other ffmpeg
    call here (subprocess.run with a timeout), this one streams from a pipe, and
    a wedged read (stalled network/FUSE mount) would otherwise block forever.
    detect_head_trim runs on a server request thread holding an export slot, so
    it MUST eventually return — a killed decode just yields the samples read so
    far, which at worst means no trim for that clip."""
    import numpy as np

    frame_bytes = ANALYSIS_W * ANALYSIS_H
    cmd = ["ffmpeg", "-v", "error"]
    if hwaccel_decode:
        cmd += ["-hwaccel", "cuda"]
    cmd += ["-i", str(src)]
    if limit_s is not None:
        cmd += ["-t", f"{limit_s:.3f}"]
    cmd += [
        "-vf", f"fps={ANALYSIS_FPS},scale={ANALYSIS_W}:{ANALYSIS_H},format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert proc.stdout is not None
    watchdog = threading.Timer(timeout_s, proc.kill)
    watchdog.start()
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
        watchdog.cancel()
        proc.stdout.close()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    return samples


def _head_trim_from_diffs(raw: list[float], fps: float) -> float:
    """Seconds to cut off a clip's head when it opens on a character-sheet intro
    card. ``raw[i]`` is the diff between frames i and i+1; the shared shape is an
    optional leading static run (the held card), then a short spike-run (the
    cut/dissolve out of the card), then settled content — trim to the first frame
    after the spike-run. Returns 0.0 when neither card signature is present:
    immediate ordinary motion never clears the spike threshold from a standing
    start, a fade-in rises gradually with no spike, sustained fast action keeps
    the spike-run going past HEAD_DISSOLVE_MAX_S, and a late exit is content.

    HELD card: the static run alone is proof (real footage that still for 0.25s+
    does not then jump straight to a 12+ diff). FLASH card (no static run — the
    card lives inside frame 0 and dissolves immediately): the first diff must
    tower over the settled post-dissolve median (HEAD_FLASH_MIN/_RATIO), which
    ordinary opening motion can't do — motion doesn't stop instantly."""
    if fps <= 0 or len(raw) < 2:
        return 0.0
    i = 0
    while i < len(raw) and raw[i] < HEAD_STATIC_DIFF:
        i += 1
    j = i
    while j < len(raw) and raw[j] >= HEAD_SPIKE_DIFF:
        j += 1
    if j == i or j >= len(raw):
        return 0.0                      # no card exit / elevated to the end of the scan
    trim = j / fps
    if trim > HEAD_MAX_TRIM_S or (j - i) / fps > HEAD_DISSOLVE_MAX_S:
        return 0.0                      # exit too late, or "dissolve" is sustained action
    if i >= max(1, round(HEAD_MIN_STATIC_S * fps)):
        return trim                     # HELD card: static run, then the exit spike-run
    if i == 0 and raw[0] >= HEAD_FLASH_MIN:
        settled = sorted(raw[j:j + 8])
        med = settled[len(settled) // 2]
        if raw[0] >= HEAD_FLASH_RATIO * max(med, 1.0):
            return trim                 # FLASH card: frame 0 towers over settled content
    return 0.0


def detect_head_trim(src: Path, *, hwaccel_decode: bool = False) -> float:
    """Standalone head scan for callers outside the montage pipeline (the server's
    Merge/Export): decode only the first HEAD_SCAN_S seconds and return the seconds
    to trim off a detected character-sheet intro card, 0.0 when there's nothing to
    trim. Never raises — detection must not be able to break an export."""
    try:
        if _is_image(src):
            return 0.0
        trim = _head_trim_from_diffs(
            _gray_diffs(Path(src), hwaccel_decode, limit_s=HEAD_SCAN_S, timeout_s=120.0),
            float(ANALYSIS_FPS))
        if trim <= 0.0:
            return 0.0
        duration = probe_duration(Path(src))
        if duration > 0 and duration - trim < HEAD_MIN_REMAIN_S:
            return 0.0
        return trim
    except Exception:
        return 0.0


def _curve_from_raw(clip_id: str, src: Path, raw: list[float],
                    duration: float) -> MotionCurve:
    """Head-trim + peak-normalize RAW diffs into a MotionCurve — the pure-math
    tail of ``analyze_motion``, split out so cached raw diffs (see the motion-diff
    cache below) and a fresh decode produce bit-identical curves. Auto-pick uses
    it too, so its features come from exactly the pipeline's own processing."""
    samples = list(raw)
    head = _head_trim_from_diffs(samples, float(ANALYSIS_FPS))
    if head > 0 and duration - head >= HEAD_MIN_REMAIN_S:
        samples = samples[round(head * ANALYSIS_FPS):]
        duration -= head
    else:
        head = 0.0
    peak = max(samples) if samples else 0.0
    if peak > 0:
        samples = [s / peak for s in samples]
    return MotionCurve(clip_id=clip_id, src_path=str(src), duration=duration,
                       fps_analyzed=float(ANALYSIS_FPS), samples=samples,
                       head_offset=head)


def analyze_motion(clip_id: str, src: Path, hwaccel_decode: bool,
                   cache_dir: Path | None = None) -> MotionCurve:
    """Per-frame motion curve via CPU frame-differencing.

    Decodes the clip to a downscaled grayscale raw-video stream at ANALYSIS_FPS,
    then motion[t] = mean(|frame[t] - frame[t-1]|), normalized 0..1. No OpenCV.

    A detected character-sheet intro card is cut off the head FIRST, on the raw
    diffs (see _head_trim_from_diffs): the leading samples are dropped, duration
    shrinks, and the trim is recorded as MotionCurve.head_offset — so the curve,
    its windows/scene cuts, and everything the planner derives are all in trimmed
    clip time, and normalization is no longer poisoned by the card's cut spike.

    ``cache_dir`` (the shared motion_cache) short-circuits the decode with the
    clip's cached RAW diffs; head-trim + normalization always re-run on top, so a
    hit is bit-identical to a fresh analysis."""
    raw = duration = None
    if cache_dir is not None:
        hit = _load_motion_diffs(src, cache_dir)
        if hit is not None:
            raw, duration = hit
    if raw is None:
        duration = probe_duration(src)
        raw = _gray_diffs(src, hwaccel_decode)
        if cache_dir is not None:
            _save_motion_diffs(src, cache_dir, raw, duration)
    return _curve_from_raw(clip_id, src, raw, duration)


# --------------------------------------------------------------------------- #
# Motion-diff cache — the RAW frame diffs are the expensive part of stage 2 (a
# full decode of every clip); everything derived (head trim, normalization,
# windows, scene cuts) is pure math on top, so caching the diffs reproduces
# analyze_motion bit-exactly while staying valid if detection thresholds are
# ever retuned. Entries live under motion_cache/beat/ — a sibling namespace of
# Motion Match Cut's descriptors, sharing its LRU size/age budget (its
# prune_cache rglobs the whole motion_cache tree). Keyed by path+size+mtime
# like matchcut (library media is immutable once downloaded). Tiny: ~8 floats
# per second of footage, so a whole library is a few MB.
# --------------------------------------------------------------------------- #

MOTION_CACHE_VERSION = 1


def _motion_cache_key(src: Path) -> str:
    st = Path(src).stat()
    return (f"mv{MOTION_CACHE_VERSION}:{ANALYSIS_FPS}x{ANALYSIS_W}x{ANALYSIS_H}:"
            f"{Path(src).resolve()}:{st.st_size}:{int(st.st_mtime)}")


def _motion_cache_path(cache_dir: Path, key: str) -> Path:
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return cache_dir / "beat" / h[:2] / f"{h}.json.gz"


def motion_cache_has(src: Path, cache_dir: Path) -> bool:
    """Whether SRC has a cached analysis (no decode, two stats). Used by the
    server's coverage readout; never raises."""
    try:
        return _motion_cache_path(cache_dir, _motion_cache_key(src)).is_file()
    except Exception:
        return False


def _load_motion_diffs(src: Path, cache_dir: Path) -> tuple[list[float], float] | None:
    """Cached ``(raw_diffs, container_duration)`` for SRC, or None. A hit touches
    the entry's mtime so matchcut.prune_cache's LRU reflects real use."""
    try:
        p = _motion_cache_path(cache_dir, _motion_cache_key(src))
        if not p.is_file():
            return None
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            d = json.load(fh)
        os.utime(p)
        return [float(x) for x in d["raw"]], float(d["duration"])
    except Exception:
        return None


def _save_motion_diffs(src: Path, cache_dir: Path, raw: list[float],
                       duration: float) -> None:
    """Best-effort cache write; the payload keeps the source path so a future
    hygiene sweep can match entries to files without reversing the key hash."""
    try:
        p = _motion_cache_path(cache_dir, _motion_cache_key(src))
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump({"v": MOTION_CACHE_VERSION, "src": str(src),
                       "duration": duration, "fps": ANALYSIS_FPS,
                       "raw": raw}, fh)
        tmp.replace(p)
    except Exception:
        pass


def purge_motion_cache_for(src: Path, cache_dir: Path | None) -> None:
    """Drop SRC's cached analyses — the beat-mode diffs AND the Motion Match Cut
    descriptor — so deleting a clip doesn't strand cache data. MUST run while the
    file still exists (both keys include size+mtime). Never raises; an entry that
    can't be matched is left for the LRU prune to age out."""
    if cache_dir is None:
        return
    try:
        _motion_cache_path(cache_dir, _motion_cache_key(src)).unlink(missing_ok=True)
    except Exception:
        pass
    try:
        import matchcut
        matchcut._cache_path(cache_dir, matchcut._cache_key(Path(src))).unlink(missing_ok=True)
    except Exception:
        pass


def warm_motion_cache(paths: list, cache_dir: Path, *, hwaccel_decode: bool = False,
                      workers: int | None = None, log=print) -> dict:
    """Pre-analyze clips into the motion-diff cache (the library warm-up).

    Skips images and anything already cached; misses decode in a RENDER_WORKERS
    pool. ``log`` lines go to stdout by default — the CLI's output streams into
    the server's sync log panel, so progress is visible there. Returns
    ``{"videos", "cached", "analyzed", "failed"}``."""
    vids = [Path(p) for p in paths if not _is_image(p)]
    todo = [p for p in vids if not motion_cache_has(p, cache_dir)]
    cached = len(vids) - len(todo)
    stats = {"videos": len(vids), "cached": cached, "analyzed": 0, "failed": 0}
    if not todo:
        log(f"motion cache: all {len(vids)} clip(s) already analyzed")
        return stats
    log(f"motion cache: {cached} already analyzed, {len(todo)} to go")

    def _one(p: Path) -> bool:
        duration = probe_duration(p)
        raw = _gray_diffs(p, hwaccel_decode)
        if not raw:
            return False
        _save_motion_diffs(p, cache_dir, raw, duration)
        return True

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, min(workers or RENDER_WORKERS, len(todo)))) as pool:
        futures = {pool.submit(_one, p): p for p in todo}
        for fut in as_completed(futures):
            try:
                ok = fut.result()
            except Exception:
                ok = False
            stats["analyzed" if ok else "failed"] += 1
            done += 1
            if done % 25 == 0 or done == len(todo):
                log(f"motion cache: {done}/{len(todo)} analyzed")
    log(f"motion cache: done — {stats['analyzed']} new, {stats['failed']} failed, "
        f"{cached} were already analyzed")
    return stats


def _is_image(path) -> bool:
    """True if a source path is a still image (Picture & Video mode). The ingest and
    render branches key off this — no media-type field flows through the JSON spec, so
    the file extension is the discriminator."""
    return Path(path).suffix.lower() in IMAGE_EXTS


def _still_curve(clip_id: str, src: Path) -> MotionCurve:
    """A synthetic MotionCurve standing in for a still image (Picture & Video mode).

    An image has no container duration and no decodable motion, so rather than analysing
    it we hand the planner a flat curve: STILL_HOLD_MAX seconds long (so it clears
    plan_cuts' ``c.duration < d`` length gate for any realistic beat slot) with a
    constant, FULL-SPAN sample list at STILL_MOTION. Full-span (not a token 2s) keeps
    every MotionCurve window method in range and genuinely constant; STILL_MOTION sits in
    [LOWCONTRAST_FLOOR, SCENE_SPIKE_ABS) so the still never trips the scene-aware
    dead-footage penalty or registers an internal scene cut. The unmodified planner then
    places the still exactly like a video; the render loop loops it into its slot (and
    ignores its in_point, which is meaningless for a still) — see _render_segment."""
    samples = [STILL_MOTION] * max(2, int(STILL_HOLD_MAX * ANALYSIS_FPS))
    return MotionCurve(clip_id=clip_id, src_path=str(src), duration=STILL_HOLD_MAX,
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
                         min_words: int = MIN_SPEAK_WORDS,
                         head_offset: float = 0.0) -> list[Utterance]:
    """Merge a clip's cues into lines, trim each to whole sentences, and keep only
    the substantial ones.

    Consecutive cues less than ``SUB_MERGE_GAP`` apart are joined (a sentence split
    across cues becomes one line); each line is then cut to whole sentences up to
    ``SPEAK_MAX_DUR`` (never mid-sentence); finally a line must clear ``min_words``
    and fit inside the clip. Filters out the "I" / "are" one-word noise.

    Sidecar cues are in ORIGINAL clip time while the pipeline runs in trimmed time
    (``head_offset`` = a cut intro card), so each line is shifted by -head_offset
    and any line starting inside the trimmed head is dropped — nobody speaks over
    a static character sheet, and ``clip_dur`` is the trimmed duration.
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
        s, e2 = s - head_offset, e2 - head_offset
        if s < 0:
            continue
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


def _hero_clip(curves: list[MotionCurve], length: float) -> str | None:
    """The single most cinematic clip to reserve for the drop: the one whose best
    motion window (over a ``length``-second span) has the highest mean motion.
    Deterministic — iterates by ascending clip_id so ties resolve to the smallest id."""
    best: str | None = None
    best_v = -1.0
    for c in sorted(curves, key=lambda x: int(x.clip_id)):
        m = max((w[0] for w in c.top_windows(length, TOP_WINDOWS_K)), default=0.0)
        if m > best_v:
            best_v, best = m, c.clip_id
    return best


def _apply_breakdown_slowmo(entries: list[EDLEntry], drop: float | None,
                            speed: float) -> None:
    """Real slow-mo decel RAMP into ``drop`` — the held shot nearest the drop is slowed
    the most (to ``speed``), and the shot before it eases back toward real time, so motion
    visibly dilates into the slam (the drop entry itself stays full-speed). Mutates ONLY
    ``playback_speed`` on existing entries, so the duration-tiling invariant is untouched.
    Skips spoken shots (don't fight the duck), xfade-target shots, and shots too short to
    slow without judder; bounded to stay surgical."""
    if drop is None or len(entries) < 2:
        return
    speed = _clamp(speed, SLOWMO_MIN_SPEED, 1.0)
    if speed >= 1.0:
        return
    h = min(range(len(entries)), key=lambda k: abs(entries[k].place_at - drop))
    acc = 0.0
    n = 0
    prev = 0.0                               # previous (closer) shot's speed -> keeps the ramp monotonic
    for k in range(h - 1, 0, -1):           # never index 0 (keep a clean pre-roll); the
        e = entries[k]                       # drop entry h is left at 1.0x (the slam)
        if drop - e.place_at > SLOWMO_LOOKBACK_S:
            break                            # too far ahead of the drop to read as a ramp
        if e.speak:
            break                            # never override a ducked spoken line
        if e.transition_dur > 0:
            continue                         # leave xfade-target shots alone
        floor = SLOWMO_MIN_SRC / e.duration  # slowest this clip's source actually allows
        if floor > SLOWMO_MAX_SHOT_SPEED:
            continue                         # too short to slow meaningfully -> skip
        # Decel ramp: the nearest slowed shot (n=0) gets the full slow-down; each earlier
        # shot eases back toward 1.0, so the deceleration builds into the drop. A short shot
        # can't slow past its source (floor); `prev` keeps the ramp monotonic (closer=slower).
        desired = speed + (1.0 - speed) * (n / max(1, SLOWMO_MAX_ENTRIES))
        shot_speed = _clamp(max(desired, floor, prev), SLOWMO_MIN_SPEED, SLOWMO_MAX_SHOT_SPEED)
        e.playback_speed = round(shot_speed, 4)
        prev = shot_speed
        acc += e.duration
        n += 1
        if acc >= SLOWMO_RAMP_S or n >= SLOWMO_MAX_ENTRIES:
            break


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


# --------------------------------------------------------------------------- #
# Structural cadence (Cinematic) — follow the montage blueprint: vary the cut rate by
# section (the golden rule — never one constant speed), phrase-align to real bar
# downbeats (from madmom), accelerate through the build, hold the breakdown/outro.
# The existing accent-transition + zoom-punch machinery (driven by _accent_times)
# lands the accents on the energy drops / section changes.
# --------------------------------------------------------------------------- #

def _cadence_beats(intensity: float, tightness: float) -> int:
    """Beats per cut for a section of the given 0..1 energy — calm sections hold for
    bars, loud sections cut every beat. Tightness scales the whole curve."""
    if intensity >= 0.80:
        base = 1
    elif intensity >= 0.62:
        base = 2
    elif intensity >= 0.45:
        base = 4
    elif intensity >= 0.30:
        base = 8
    else:
        base = 16
    return max(1, round(base * _lerp(1.8, 0.6, _clamp(tightness, 0.0, 1.0))))


def _accel_ramp(s0: float, s1: float, beat: float,
                start_beats: float, end_beats: float) -> list[float]:
    """Interior cut times in [s0, s1) whose spacing shrinks geometrically from
    ``start_beats`` to ``end_beats`` — the accelerating 'build' that loads anticipation
    into the drop at s1. Normalised so the spacings exactly fill the span."""
    span = s1 - s0
    if span <= beat * end_beats * 1.5:
        return []
    n = max(2, int(round(span / (0.5 * (start_beats + end_beats) * beat))))
    r = (end_beats / start_beats) ** (1.0 / max(1, n - 1))
    lens = [start_beats * r ** i for i in range(n)]
    scale = span / (sum(lens) * beat)
    out: list[float] = []
    t = s0
    for L in lens[:-1]:
        t += L * beat * scale
        out.append(t)
    return out


def _structural_boundaries(grid: BeatGrid, beats: list, times: list[float],
                           energies: list[float], T: float, tightness: float) -> list[float]:
    """Section-aware cut grid for Cinematic's 'structural' rhythm (the blueprint):
    per-section cadence from energy, phrase-snapped section starts, an accelerating
    ramp through a build, long holds in calm/breakdown sections, a J-cut intro (first
    picture on the first downbeat) and an L-cut outro (one held shot to the end)."""
    beat_times = [b.time for b in beats]
    if not beat_times:
        return [0.0, T]
    tempo = grid.tempo if grid.tempo and grid.tempo > 0 else 120.0
    beat = 60.0 / tempo
    bar = 4.0 * beat
    downbeats = [b.time for b in beats if b.is_downbeat] or beat_times
    first_db = downbeats[0]
    sections = sorted(grid.sections, key=lambda s: s.start)

    def snap_beat(t: float) -> float:
        return min(beat_times, key=lambda x: abs(x - t))

    def snap_phrase(t: float, phrase_bars: int = 8) -> float:
        step = phrase_bars * bar
        return first_db + round((t - first_db) / step) * step if step > 0 else t

    bounds = [0.0]
    n = len(sections)
    peak_idx = max(range(n), key=lambda i: sections[i].intensity) if n else -1
    for si, sec in enumerate(sections):
        nxt = sections[si + 1] if si + 1 < n else None
        s0 = 0.0 if si == 0 else _clamp(snap_phrase(sec.start), bounds[-1], T)
        s1 = T if nxt is None else _clamp(snap_phrase(sec.end), s0, T)
        if s1 <= s0 + 1e-3:
            continue
        if s0 > bounds[-1] + 1e-3:
            bounds.append(snap_beat(s0))
        cursor = bounds[-1]
        # INTRO J-cut: first picture begins on the first downbeat (sound leads).
        if si == 0 and s0 + 1e-3 < first_db < s0 + 2 * bar:
            cb = snap_beat(first_db)
            if cb > cursor + 1e-3:
                bounds.append(cb)
                cursor = cb
        # OUTRO L-cut: a quiet, short final section holds a single shot to the end.
        if nxt is None and si > 0 and sec.intensity <= 0.5 and (s1 - s0) <= 4 * bar:
            continue
        # BUILD: only the section right before the loudest one (and rising into it) gets
        # the accelerating ramp — the single pre-drop that loads anticipation. (A looser
        # "any rising section" rule turned calm verses into ramps.)
        if (nxt is not None and si + 1 == peak_idx
                and nxt.intensity - sec.intensity >= 0.10 and sec.intensity < 0.6):
            for c in _accel_ramp(cursor, s1, beat, 4.0, 1.0):
                cb = snap_beat(c)
                if bounds[-1] + 1e-3 < cb < T:
                    bounds.append(cb)
            continue
        # else: even cadence chosen from this section's energy.
        cad = _cadence_beats(sec.intensity, tightness)
        t = cursor + cad * beat
        while t < s1 - 1e-3:
            cb = snap_beat(t)
            if bounds[-1] + 1e-3 < cb < T:
                bounds.append(cb)
            t += cad * beat
    bounds.append(T)
    return sorted(set(b for b in bounds if 0.0 <= b <= T))


def plan_cuts(grid: BeatGrid, curves: list[MotionCurve], *,
              tightness: float, target_duration: float | None,
              seed: int | None = None, transitions: str = "none",
              rhythm: str = "density", motion_weight: float = W_MOTION,
              machine_gun: bool = False, scene_aware: bool = False,
              overuse_w: float = W_OVERUSE, overuse_p: float = 1.0,
              forced_speech: list[SpeechMoment] | None = None,
              hero_shot: bool = False, breakdown_slowmo: float = 0.0) -> EDL:
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
    elif rhythm == "structural":
        boundaries = _structural_boundaries(grid, beats, times, energies, T, tightness)
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

    # Sub-beat "machine-gun" cutting (Music Video). In loud passages, add cuts at
    # transient onsets — the flurry that detonates on the drop. grid.onsets is
    # populated only by the enhanced analysis (empty otherwise -> no-op). Each onset
    # snaps to a half-beat candidate grid (beats + their midpoints) so a cut can land
    # BETWEEN beats instead of collapsing onto a beat we'd already cut on; it's gated
    # to loud beats and kept >= MACHINE_GUN_MIN_BEATS apart from every other boundary
    # so each slot stays a few frames long (sub-frame slots drift off the invariant).
    if machine_gun and grid.onsets:
        bp = 60.0 / grid.tempo if grid.tempo and grid.tempo > 0 else (T / max(1, len(beats)))
        min_sub = MACHINE_GUN_MIN_BEATS * bp
        cand: list[float] = []
        for j in range(len(times)):
            cand.append(times[j])
            if j + 1 < len(times):
                cand.append((times[j] + times[j + 1]) / 2.0)
        extra: list[float] = []
        for ot in grid.onsets:
            if not (1e-3 < ot < T - 1e-3) or energy_at(ot) < MACHINE_GUN_ENERGY:
                continue
            snapped = min(cand, key=lambda t: abs(t - ot)) if cand else ot
            if not (1e-3 < snapped < T - 1e-3):
                continue
            if any(abs(snapped - b) < min_sub for b in boundaries) or \
               any(abs(snapped - e) < min_sub for e in extra):
                continue
            extra.append(snapped)
        boundaries = sorted(set(boundaries + extra))

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
    head_by_id = {c.clip_id: c.head_offset for c in curves}
    n_intervals = max(1, len(boundaries) - 1)
    max_uses = max(1, -(-n_intervals // len(curves)))  # ceil

    # Cinematic-only (gated, default-off, density rhythm). Anchor BOTH features to the
    # single strongest drop: reserve the most-cinematic clip for the drop interval, and
    # slow the held shots leading into it. Other presets / golden tests pass neither
    # kwarg, so hero_drop/hero_cid stay None and every block below is skipped.
    hero_drop: float | None = None
    hero_cid: str | None = None
    if (hero_shot or breakdown_slowmo > 0.0) and rhythm == "density":
        drops = [t for t, is_drop in _accent_times(grid, T) if is_drop]
        if drops:
            # Anchor to the biggest quiet->loud CONTRAST (a real breakdown->drop), not the
            # loudest absolute drop: a drop deep in a loud section has a busy, fast pre-roll
            # with no held shots to slow, whereas a drop coming out of a quiet pocket gives
            # BOTH the slowable held shots AND the most dramatic slam.
            def _contrast(t: float) -> float:
                return energy_at(t) - min(energy_at(t - x) for x in (1.0, 2.0, 3.0, 4.0, 5.0))
            hero_drop = max(drops, key=lambda t: (_contrast(t), energy_at(t), -t))
        if hero_shot:
            median_len = (boundaries[-1] - boundaries[0]) / max(1, len(boundaries) - 1)
            hero_cid = _hero_clip(curves, median_len)

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
                duration=round(d, 4), place_at=round(b0, 4), speak=True,
                head_offset=head_by_id.get(cid, 0.0)))
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
                    if scene_aware:
                        # mugen-style rejection: avoid dead footage and shots that
                        # cut across a hidden internal scene change.
                        if actual_mean < LOWCONTRAST_FLOOR:
                            score -= W_LOWCONTRAST
                        if c.spans_scene_cut(in_point, d):
                            score -= W_SCENE_SPAN
                    scored.append((score, c, in_point))

        if not scored:  # no clip long enough; use the longest, full length
            c = max(curves, key=lambda x: x.duration)
            d = min(d, c.duration)
            scored = [(0.0, c, 0.0)]

        # Hero reservation: the drop belongs to the most-cinematic clip. Restrict this
        # interval's candidates to the hero clip's windows when it fits here (clips too
        # short were already dropped by the length filter, so an empty result degrades
        # cleanly to normal scoring). Its best window then wins the sort/exploration.
        if hero_cid is not None and hero_drop is not None and b0 <= hero_drop < b1:
            hero_cands = [s for s in scored if s[1].clip_id == hero_cid]
            if hero_cands:
                scored = hero_cands

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
            head_offset=c.head_offset,
        ))
        recent.append(c.clip_id)
        use_count[c.clip_id] += 1

    if transitions == "accents":
        _assign_accent_transitions(grid, curves, entries, explore, rng)

    # Slow-mo runs AFTER transitions are assigned (it reads each entry's transition_dur
    # to skip xfade-target shots) and BEFORE the invariant check (it touches only
    # playback_speed, never duration). Shares the drop anchor with the hero reservation.
    if breakdown_slowmo > 0.0 and rhythm == "density":
        _apply_breakdown_slowmo(entries, hero_drop, breakdown_slowmo)

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


def _rgb_split_filter(amt: float, width: int) -> str:
    """Static RGB channel split (chromatic aberration) as a drop accent — red and
    blue fringe opposite ways. ``amt`` is the shift as a fraction of frame width.
    CPU-only: rgbashift forces an RGB-plane conversion, so it leaves the GPU path."""
    if amt <= 0:
        return ""
    s = max(1, round(amt * width))
    return f"rgbashift=rh={s}:bh={-s}:edge=smear"


def _shake_filter(amt: float, width: int, height: int) -> str:
    """A short, decaying camera-shake rattle for a drop hit: over-scale to create
    crop margin, then jitter the crop window with a decaying sinusoid so the kick
    rattles the camera and settles within ~0.2s. ``amt`` is the over-scale fraction.
    The commas inside max() are safe — they sit in single-quoted x/y values, so the
    filtergraph parser doesn't read them as filter separators (same trick the zoom
    clause relies on). CPU-only (no crop_cuda)."""
    if amt <= 0:
        return ""
    osc = 1.0 + 2.0 * amt
    f1, f2, decay = 16.0, 11.0, 0.18
    return (f"scale=ceil(iw*{osc:.4f}/2)*2:ceil(ih*{osc:.4f}/2)*2,"
            f"crop={width}:{height}:"
            f"x='(iw-ow)/2+(iw-ow)*0.45*sin(2*PI*t*{f1})*max(0,1-t/{decay})':"
            f"y='(ih-oh)/2+(ih-oh)*0.45*sin(2*PI*t*{f2})*max(0,1-t/{decay})'")


def _render_segment(entry: EDLEntry, out: Path, *, width: int, height: int,
                    fps: int, video_encode_args: list[str], gpu: bool,
                    lead: float = 0.0, tail: float = 0.0,
                    fill: str = "fit",
                    zoom: tuple[str, float] | None = None,
                    grade: str = "", flash: float = 0.0,
                    rgb_split: float = 0.0, shake: float = 0.0,
                    playback_speed: float = 1.0) -> None:
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
    # Still image (Picture & Video mode): there is no timeline to seek into, so IGNORE
    # in_point entirely and LOOP the frame to fill exactly this slot plus its transition
    # handles — `dur = entry.duration + lead + tail`, the same length a trimmed video
    # segment renders to, so it tiles the concat identically (the pure hard-cut path
    # trusts this nominal length, and the transition path re-probes it). Letterboxed
    # (fill="fit") so a portrait keeps its whole frame; the push/punch zoom + accents
    # still apply. Always CPU (NVDEC can't decode a JPEG) and playback_speed is
    # meaningless on a held frame, so slow-mo is skipped.
    if _is_image(entry.src_path):
        dur = entry.duration + lead + tail
        vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
              f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,fps={fps},setsar=1")
        zf = _zoom_filter(zoom, dur, fps, width, height)
        if zf:
            # Smooth Ken Burns on a STILL: zoompan snaps its crop window to whole INPUT
            # pixels every frame, so on a static image the ~1px steps read as a juddery
            # shimmer (on video the moving content hides it). Pre-upscaling the frame makes
            # each step a fraction of an OUTPUT pixel, so the push/punch reads as smooth —
            # measured ~3x more even per-frame motion. 4x for <=1080p, 2x above (keeps the
            # intermediate within ~8K). Only stills that actually zoom pay this cost.
            up = 4 if (4 * width <= 8192 and 4 * height <= 8192) else 2
            vf += f",scale={up * width}:{up * height}:flags=bicubic,{zf}"
        sk = _shake_filter(shake, width, height)
        if sk:
            vf += "," + sk
        rs = _rgb_split_filter(rgb_split, width)
        if rs:
            vf += "," + rs
        if grade:
            vf += "," + grade
        if flash > 0:
            vf += f",fade=t=in:st=0:d={flash:.4f}:color=white"
        # Normalise to EXACTLY what the video segments are so the stream-copy concat
        # accepts the still: a JPEG decodes as full-range yuvj420p, and the shake/zoom
        # filters can perturb SAR — force limited-range yuv420p + SAR 1:1 (see the render
        # smoke test; without this the pix_fmt/SAR differ and -c copy mixes color ranges).
        vf += ",scale=out_range=tv,setsar=1,format=yuv420p"
        cmd = ["ffmpeg", "-y", "-v", "error",
               "-loop", "1", "-framerate", str(fps), "-t", f"{dur:.4f}",
               "-i", entry.src_path, "-vf", vf, "-an", *video_encode_args,
               "-movflags", "+faststart", str(out)]
        _run(cmd, f"render still @ {entry.place_at:.2f}s")
        return
    # Seek math runs in TRIMMED clip time (the planner's timeline), then head_offset
    # — a cut character-sheet intro card — is added back to reach source time. The
    # ss>=0 clamp therefore floors at the card's END, so a transition lead handle can
    # never reach back into the trimmed head.
    ss_rel = max(0.0, entry.in_point - lead)
    dur = entry.duration + (entry.in_point - ss_rel) + tail
    ss = entry.head_offset + ss_rel
    zf = _zoom_filter(zoom, dur, fps, width, height)

    def _cpu_cmd() -> list[str]:
        if fill == "cover":
            vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                  f"crop={width}:{height},fps={fps},setsar=1")
        else:
            vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                  f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,fps={fps},setsar=1")
        if playback_speed != 1.0:
            # Real retime: stretch/compress presentation timestamps, then the fps filter
            # in the base chain resamples to constant output fps. MUST precede that fps=
            # token, so prepend it to the whole chain. `-t {dur}` below caps the OUTPUT
            # at dur, so ffmpeg reads dur*speed of source and emits exactly dur seconds.
            # The timeline slot length is therefore unchanged; only the footage inside
            # it retimes.
            #
            # NOTE the fps filter DUPLICATES/DROPS frames — it does not interpolate. At
            # 24fps source into 30fps output the pulldown already holds 1 frame in 5;
            # measured duplicate fraction is exactly max(0, 1 - src_fps*speed/out_fps),
            # so 0.80 is the practical judder floor and out_fps/src_fps (1.25 here) is a
            # clean 1:1. Motion Match Cut's speed solve clamps to that measured band.
            #
            # speed > 1.0 reads MORE source than the slot length (slow-mo reads less, so
            # the original slow-mo path never needed a headroom check). Callers emitting
            # speed > 1.0 MUST ensure in_point + duration*speed <= clip duration —
            # matchcut.solve_speeds does this explicitly.
            vf = f"setpts=PTS/{playback_speed:.4f}," + vf
        if zf:
            vf += "," + zf
        sk = _shake_filter(shake, width, height)
        if sk:
            vf += "," + sk
        rs = _rgb_split_filter(rgb_split, width)
        if rs:
            vf += "," + rs
        if grade:
            vf += "," + grade
        if flash > 0:
            vf += f",fade=t=in:st=0:d={flash:.4f}:color=white"
        return ["ffmpeg", "-y", "-v", "error",
                "-ss", f"{ss:.4f}", "-i", entry.src_path, "-t", f"{dur:.4f}",
                "-vf", vf, "-an", *video_encode_args, "-movflags", "+faststart", str(out)]

    def _gpu_cmd() -> list[str]:
        vf = (f"scale_cuda={width}:{height}:force_original_aspect_ratio=decrease,"
              f"pad_cuda={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,fps={fps}")
        return ["ffmpeg", "-y", "-v", "error", "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
                "-ss", f"{ss:.4f}", "-i", entry.src_path, "-t", f"{dur:.4f}",
                "-vf", vf, "-an", *video_encode_args, "-movflags", "+faststart", str(out)]

    # zoompan is CPU-only, cover-framing needs a CPU centre-crop, and a grade/flash
    # is a CPU-only filter too — any of them rules out the GPU path. Only a plain fit
    # hard-cut shot with no extra filters can stay on the GPU. ANY retime must stay on
    # the CPU: _gpu_cmd has no setpts clause, so admitting speed >= 1.0 here used to
    # make a speed-up silently render at real time with no error at all.
    if (gpu and not zf and fill != "cover" and not grade and flash <= 0
            and shake <= 0 and rgb_split <= 0 and playback_speed == 1.0):
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
                         fps: int, mux_audio: bool = True) -> None:
    """Assemble segments with crossfade accent transitions, muxing the song over them.

    ``durations[i]`` is the rendered length of segment ``i`` (including its handles).
    ``joins[i]`` is the (transition_type, transition_dur) describing the join *into*
    segment ``i`` (``joins[0]`` is unused).

    Consecutive hard cuts are first concatenated losslessly (concat demuxer) into one
    run file each, so only genuine transition boundaries need a crossfade. Each
    crossfade is then rendered in ISOLATION (one reliable two-input ``xfade``): the
    td-second tail of a run is blended with the td-second head of the next into a
    td-second clip, and every run is trimmed to the body between its bordering
    transitions. Concatenating bodies + transition clips reproduces exactly the same
    overlap (length = sum(run durations) - sum(td) = the planned timeline) and blends
    the same frames, so it is visually identical to a correct xfade.

    This deliberately AVOIDS a single N-deep ``xfade`` filtergraph: past ~2 chained
    stages ffmpeg silently drops everything after an early transition even when every
    offset is in range, which collapsed dense cinematic montages to ~12s. Isolated
    single-stage xfades don't hit that.
    """
    n = len(segments)
    work_dir = out_path.parent

    def _safe(p) -> str:
        return str(Path(p).resolve()).replace("'", "'\\''")

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

    # Materialise each hard-cut run to a single file (concat demuxer, stream copy —
    # lossless and near-zero memory). A length-1 run is already its own file.
    run_files: list[Path] = []
    run_durs: list[float] = []
    for ri, run in enumerate(runs):
        if len(run) == 1:
            run_files.append(Path(segments[run[0]]))
            run_durs.append(durations[run[0]])
            continue
        listfile = work_dir / f"run_{ri:04d}.txt"
        listfile.write_text("".join(f"file '{_safe(segments[i])}'\n" for i in run), encoding="utf-8")
        rf = work_dir / f"run_{ri:04d}.mp4"
        _run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
              "-i", str(listfile), "-c", "copy", "-movflags", "+faststart", str(rf)],
             f"transition run {ri} concat")
        run_files.append(rf)
        run_durs.append(probe_duration(rf))

    # td consumed from each run's head (transition into it) and tail (out of it).
    K = len(runs)
    td_in = [0.0] * K
    for ri in range(1, K):
        td_in[ri] = joins[runs[ri][0]][1]
    td_out = [0.0] * K
    for ri in range(K - 1):
        td_out[ri] = td_in[ri + 1]

    # Every piece is re-encoded to the same W/H/fps/SAR/pixfmt so the final concat is a
    # lossless stream copy. xfade needs CFR inputs, so the trims force fps explicitly.
    norm = f"fps={fps},format=yuv420p,setsar=1"
    pieces: list[Path] = []
    for ri in range(K):
        src, d = run_files[ri], run_durs[ri]
        body_len = max(0.0, d - td_in[ri] - td_out[ri])
        if body_len > 1e-3:
            body = work_dir / f"body_{ri:04d}.mp4"
            _run(["ffmpeg", "-y", "-v", "error", "-i", str(src),
                  "-ss", f"{td_in[ri]:.4f}", "-t", f"{body_len:.4f}",
                  "-vf", norm, "-an", *video_encode_args, "-movflags", "+faststart", str(body)],
                 f"transition body {ri}")
            pieces.append(body)
        if ri < K - 1:
            ttype, td = joins[runs[ri + 1][0]]
            tail = work_dir / f"tail_{ri:04d}.mp4"
            head = work_dir / f"head_{ri:04d}.mp4"
            _run(["ffmpeg", "-y", "-v", "error", "-i", str(src),
                  "-ss", f"{max(0.0, d - td):.4f}", "-t", f"{td:.4f}",
                  "-vf", norm, "-an", *video_encode_args, "-movflags", "+faststart", str(tail)],
                 f"transition tail {ri}")
            _run(["ffmpeg", "-y", "-v", "error", "-i", str(run_files[ri + 1]),
                  "-t", f"{td:.4f}", "-vf", norm, "-an", *video_encode_args,
                  "-movflags", "+faststart", str(head)],
                 f"transition head {ri}")
            xc = work_dir / f"xfade_{ri:04d}.mp4"
            _run(["ffmpeg", "-y", "-v", "error", "-i", str(tail), "-i", str(head),
                  "-filter_complex",
                  f"[0:v]{norm}[a];[1:v]{norm}[b];"
                  f"[a][b]xfade=transition={ttype}:duration={td:.4f}:offset=0[v]",
                  "-map", "[v]", *video_encode_args, "-movflags", "+faststart", str(xc)],
                 f"transition xfade {ri}")
            pieces.append(xc)

    # Concat all bodies + transition clips (uniform encode params -> lossless copy).
    catlist = work_dir / "pieces.txt"
    catlist.write_text("".join(f"file '{_safe(p)}'\n" for p in pieces), encoding="utf-8")
    joined = work_dir / "video_joined.mp4" if mux_audio else out_path
    _run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(catlist),
          "-c", "copy", "-movflags", "+faststart", str(joined)], "concat transition pieces")

    if mux_audio:
        _run(["ffmpeg", "-y", "-v", "error", "-i", str(joined), "-i", str(song_path),
              "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
              "-shortest", "-movflags", "+faststart", str(out_path)],
             "mux song over transitions")


# Motion Match Cut with NO song: keep the source clips' own audio rather than
# rendering silent. Every shot contributes its own audio, in cut order.
AUDIO_EDGE_FADE = 0.04     # s; tiny fade each end so a hard cut doesn't click


def _clip_audio_track(entries: list[EDLEntry], work_dir: Path, out_path: Path,
                      *, ar: int = 48000) -> int:
    """Build one audio track exactly as long as the timeline, from each shot's OWN
    audio. Returns how many shots contributed real audio — 0 means nothing had any,
    so the caller should ship the video silent rather than mux a dead track.

    Each piece is forced to EXACTLY ``entry.duration`` via apad + an output ``-t``, so
    the track tiles the timeline with no drift. A retimed shot gets ``atempo`` at the
    same factor its video got from setpts, keeping picture and sound locked; the free
    speed band [0.80, 1.25] sits well inside atempo's [0.5, 2.0] range, so a single
    filter always suffices. A clip with no audio stream (or an undecodable one)
    silently contributes silence for its slot."""
    parts: list[Path] = []
    kept = 0
    for i, e in enumerate(entries):
        piece = work_dir / f"aud_{i:04d}.wav"
        af = []
        if abs(e.playback_speed - 1.0) > 1e-3:
            af.append(f"atempo={e.playback_speed:.4f}")
        af += [f"aresample={ar}", "apad",
               f"afade=t=in:st=0:d={AUDIO_EDGE_FADE}",
               f"afade=t=out:st={max(0.0, e.duration - AUDIO_EDGE_FADE):.4f}"
               f":d={AUDIO_EDGE_FADE}"]
        made = False
        try:
            _run(["ffmpeg", "-y", "-v", "error",
                  "-ss", f"{e.head_offset + e.in_point:.4f}", "-i", e.src_path,
                  "-vn", "-map", "0:a:0", "-af", ",".join(af),
                  "-t", f"{e.duration:.4f}", "-ac", "2", "-ar", str(ar),
                  "-c:a", "pcm_s16le", str(piece)], f"clip audio {i}")
            made = True
            kept += 1
        except RuntimeError:
            pass                      # no audio stream / undecodable -> silence below
        if not made:
            _run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                  "-i", f"anullsrc=channel_layout=stereo:sample_rate={ar}",
                  "-t", f"{e.duration:.4f}", "-c:a", "pcm_s16le", str(piece)],
                 f"silence {i}")
        parts.append(piece)

    if not kept:
        return 0
    listfile = work_dir / "audio_concat.txt"
    listfile.write_text("".join(
        f"file '{str(p.resolve()).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n"
        for p in parts), encoding="utf-8")
    _run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
          "-i", str(listfile), "-c:a", "pcm_s16le", str(out_path)], "clip audio concat")
    return kept


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

def _overall(stage: str, stage_progress: float, order: list[str] | None = None,
             weights: dict[str, float] | None = None) -> float:
    """Weighted overall progress so the bar advances smoothly across stages.

    ``order``/``weights`` are per-MODE so a pipeline with different stages (Motion
    Match Cut skips audio analysis and adds a matching stage) can report honest
    progress. They default to the beat-montage tables. NEVER mutate the module-level
    dicts to add a stage — that silently reweights every existing preset's bar."""
    order = order or _STAGE_ORDER
    weights = weights or STAGE_WEIGHTS
    done = sum(weights[s] for s in order[:order.index(stage)])
    return round(done + weights[stage] * _clamp(stage_progress, 0, 1), 4)


def generate(*, video_paths: list[Path], song_path: Path | None, options: dict,
             work_dir: Path, out_path: Path, video_encode_args: list[str],
             hwaccel_decode: bool, progress: ProgressFn,
             cache_dir: Path | None = None,
             beat_cache_dir: Path | None = None) -> dict:
    """Run the full pipeline. ``progress(status, overall, stage_progress, detail)``
    is called throughout. Returns result metadata for the finished file.

    Two modes, selected by ``options['mode']``:

      "beat" (default)  the beat-synced montage — analyse the song, plan cuts against
                        its grid. ``song_path`` is REQUIRED.
      "matchcut"        Motion Match Cut — splice where motion aligns across clips.
                        ``song_path`` is OPTIONAL (None renders silent); when present
                        it is muxed as a bed but never sets the cut points.

    Both modes share the render and assembly stages verbatim — those are EDL-driven
    and already audio-free."""
    mode = (options.get("mode") or "beat").strip().lower()
    if mode not in ("beat", "matchcut"):
        mode = "beat"
    if mode == "beat" and song_path is None:
        raise RuntimeError("a beat montage needs a song")
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
    machine_gun = bool(cfg.get("machine_gun", False))
    scene_aware = bool(cfg.get("scene_aware", False))
    grade = GRADES.get(cfg.get("grade") or "", "")
    flash = float(cfg.get("flash", 0.0))
    rgb_split = float(cfg.get("rgb_split", 0.0))
    shake = float(cfg.get("shake", 0.0))
    # Picture & Video: only this preset lets still images through as beats. Every other
    # preset leaves this falsey, so a stray image path (there shouldn't be one) would
    # fall to analyze_motion and be dropped — the pipeline stays video-only.
    allow_stills = bool(cfg.get("allow_stills"))
    # madmom (RNN+DBN) is the default beat engine for EVERY preset — a steady grid +
    # real downbeats; it falls back to librosa automatically when madmom isn't
    # installed (e.g. the local Python 3.10 venv). BEAT_ENGINE can override ("librosa").
    beat_engine = (options.get("beat_engine") or os.environ.get("BEAT_ENGINE") or "neural")
    # "Let clips speak" is preset-independent: a checkbox + how many moments.
    let_speak = bool(options.get("let_clips_speak"))
    speak_moments = options.get("speak_moments", "auto")
    # Auto Montage: video_paths is the CANDIDATE pool (whole library / chosen
    # collections); a "selecting" stage picks the clips that best serve this song
    # from cached analyses before the normal pipeline runs. Beat mode only.
    auto_pick = bool(options.get("auto_pick")) and mode == "beat"

    if mode == "matchcut":
        # Motion Match Cut owns its own look: the seam is the effect, so every beat
        # preset's decoration is off. Cover framing is mandatory — mixed aspect ratios
        # destroy a match cut. Stills have no motion to match, so they never enter.
        fill = "cover"
        push_in = punch = flash = rgb_split = shake = 0.0
        grade = ""
        allow_stills = False
        let_speak = False
        transitions = "none"
        stage_order, stage_weights = MATCHCUT_STAGE_ORDER, MATCHCUT_STAGE_WEIGHTS
    elif auto_pick:
        stage_order, stage_weights = AUTOPICK_STAGE_ORDER, AUTOPICK_STAGE_WEIGHTS
    else:
        stage_order, stage_weights = _STAGE_ORDER, STAGE_WEIGHTS

    def _ov(stage: str, sp: float) -> float:
        return _overall(stage, sp, stage_order, stage_weights)

    # 1. audio --------------------------------------------------------------- #
    # Match-cut mode has no beat grid at all: the song is optional and, when present,
    # is a bed muxed at assembly — it never sets the cut points (a seam forced onto a
    # beat loses the match; measured direction coherence lasts only ~350ms).
    grid = None
    if mode == "beat":
        progress("analyzing_audio", _ov("analyzing_audio", 0.0), 0.0, "Analyzing audio…")
        grid, grid_cached = load_or_analyze_audio(song_path, enhanced=enhanced,
                                                  beat_engine=beat_engine,
                                                  cache_dir=beat_cache_dir)
        eng = grid.engine + (f"/{grid.device}" if grid.engine != "librosa" else "")
        if grid.engine_note:
            eng += f" ({grid.engine_note})"   # e.g. "librosa (madmom error: …)" — never a silent drop
        if grid_cached:
            eng += " · cached"
        progress("analyzing_audio", _ov("analyzing_audio", 1.0), 1.0,
                 f"{len(grid.beats)} beats @ {grid.tempo:.0f} BPM · {eng}")

    # 1b. auto-pick (Auto Montage) ------------------------------------------- #
    # Score the candidate pool against this song's demand profile using ONLY
    # cached artifacts (beat grid + per-clip raw diffs) — zero decoding — and
    # narrow video_paths to the chosen clips. Uncached candidates are skipped
    # (the warm-up / sync keeps coverage converging to 100%).
    auto_stats: dict = {}
    if auto_pick:
        import autopick
        progress("selecting", _ov("selecting", 0.0), 0.0,
                 f"Choosing clips for this song ({len(video_paths)} candidates)…")
        video_paths, auto_stats = autopick.select_clips(
            [Path(p) for p in video_paths], grid, cache_dir,
            tightness=tightness, target_duration=target,
            scene_aware=scene_aware, allow_stills=allow_stills, seed=seed)
        detail = (f"Picked {auto_stats['picked']} of {auto_stats['analyzed']} "
                  f"analyzed clips (~{auto_stats['est_slots']} slots)")
        if auto_stats.get("skipped_uncached"):
            detail += f" · {auto_stats['skipped_uncached']} unanalyzed skipped"
        progress("selecting", _ov("selecting", 1.0), 1.0, detail)

    # 2. motion (the slow stage — surface per-clip progress) ----------------- #
    curves: list[MotionCurve] = []
    descs: list = []                 # matchcut.ClipDesc, match-cut mode only
    total = len(video_paths)
    if mode == "matchcut":
        import matchcut
        # The server names the cache dir (it owns the data layout); fall back to a
        # sibling of the scratch dir so a direct library call still caches.
        if cache_dir is None and work_dir.parent.exists():
            cache_dir = work_dir.parent / "motion_cache"
        for idx, src in enumerate(video_paths):
            sp = idx / total
            progress("analyzing_motion", _ov("analyzing_motion", sp), sp,
                     f"Analyzing motion: clip {idx + 1} of {total}")
            try:
                d = matchcut.load_or_analyze(str(idx), Path(src), cache_dir=cache_dir,
                                             hwaccel_decode=hwaccel_decode)
                if d is not None:
                    descs.append(d)
            except Exception:
                pass  # skip undecodable clips; fail below only if <2 remain
        if len(descs) < 2:
            raise RuntimeError("fewer than 2 usable clips after motion analysis")
        # Bound the descriptor cache now that this run's entries are freshly touched,
        # so they're the LAST things evicted. Best-effort — never breaks a render.
        try:
            matchcut.prune_cache(cache_dir)
        except Exception:
            pass
        n_trimmed = sum(1 for d in descs if d.head_offset > 0)
        progress("analyzing_motion", _ov("analyzing_motion", 1.0), 1.0,
                 f"Analyzed {len(descs)} clips"
                 + (f" · trimmed {n_trimmed} intro card(s)" if n_trimmed else ""))
    else:
        # Beat-mode analysis runs through the motion-diff cache (a hit skips the
        # decode entirely and reproduces the curve bit-exactly) and a worker pool
        # for the misses — decodes are independent single-input ffmpeg jobs, so
        # the pool changes wall-clock only. Results keep candidate order (clip_id
        # = original index; the planner's determinism depends on stable order).
        # progress() stays on this thread — see the render pool for why.
        results: list[MotionCurve | None] = [None] * total

        def _analyze_one(idx: int) -> None:
            src = video_paths[idx]
            try:
                # A still image (Picture & Video mode only) can't be decoded for motion —
                # substitute a synthetic flat curve so the planner can place it like a clip.
                if allow_stills and _is_image(src):
                    curve = _still_curve(str(idx), Path(src))
                else:
                    curve = analyze_motion(str(idx), Path(src), hwaccel_decode,
                                           cache_dir=cache_dir)
                if curve.duration > 0 and curve.samples:
                    results[idx] = curve
            except Exception:
                pass  # skip undecodable clips; fail later only if <2 remain

        done = 0
        with ThreadPoolExecutor(max_workers=max(1, min(RENDER_WORKERS, total))) as pool:
            for fut in as_completed([pool.submit(_analyze_one, i) for i in range(total)]):
                fut.result()          # _analyze_one never raises; keep the contract loud
                done += 1
                sp = done / total
                progress("analyzing_motion", _ov("analyzing_motion", sp), sp,
                         f"Analyzing motion: clip {done} of {total}")
        curves = [c for c in results if c is not None]
        if len(curves) < 2:
            raise RuntimeError("fewer than 2 usable clips after motion analysis")
        n_trimmed = sum(1 for c in curves if c.head_offset > 0)
        progress("analyzing_motion", _ov("analyzing_motion", 1.0), 1.0,
                 f"Analyzed {len(curves)} clips"
                 + (f" · trimmed {n_trimmed} intro card(s)" if n_trimmed else ""))

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
                                                clip_dur[c.clip_id],
                                                head_offset=c.head_offset)
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
    match_stats: dict = {}
    if mode == "matchcut":
        progress("matching", _ov("matching", 0.0), 0.0, "Matching motion across clips…")
        edl, match_stats = matchcut.plan_match_cuts(
            descs, target_duration=target, seed=seed,
            match_speed=bool(options.get("match_speed", True)),
            transition_frames=(3 if options.get("match_dissolve") else 0),
            fps=fps,
            progress=lambda f: progress("matching", _ov("matching", f), f,
                                        "Matching motion across clips…"))
        progress("planning", _ov("planning", 1.0), 1.0,
                 f"{match_stats['clips_used']} shots from "
                 f"{match_stats['clips_gated']} of {match_stats['clips_in']} clips"
                 + (f" · {match_stats['clips_in'] - match_stats['clips_gated']} too static"
                    if match_stats['clips_in'] > match_stats['clips_gated'] else "")
                 + f" · seam {match_stats['seam_mean_chain']}/{match_stats['seam_max_possible']}")
    else:
        progress("planning", _ov("planning", 0.0), 0.0, "Planning cuts…")
        edl = plan_cuts(grid, curves, tightness=tightness, target_duration=target,
                        seed=seed, transitions=transitions, rhythm=rhythm,
                        motion_weight=motion_weight, machine_gun=machine_gun,
                        scene_aware=scene_aware,
                        overuse_w=float(cfg.get("overuse_w", W_OVERUSE)),
                        overuse_p=float(cfg.get("overuse_p", 1.0)),
                        forced_speech=forced_speech,
                        hero_shot=bool(cfg.get("hero_shot", False)),
                        breakdown_slowmo=float(cfg.get("breakdown_slowmo", 0.0)))
    # Match Cut with NO song: the clips' own audio becomes the soundtrack. With a song
    # the music is the track instead (mixing both is a future call).
    keep_audio = (mode == "matchcut" and song_path is None
                  and bool(options.get("keep_audio", True)))
    n_audio = 0

    (work_dir / "edl.json").write_text(edl.to_json(), encoding="utf-8")
    n_trans = sum(1 for e in edl.entries if e.transition_dur > 0)
    n_speak = sum(1 for e in edl.entries if e.speak)
    if mode == "beat":
        speak_note = ""
        if let_speak:
            speak_note = (f", {n_speak} spoken line(s)" if n_speak
                          else ", no spoken lines found")
        progress("planning", _ov("planning", 1.0), 1.0,
                 f"{len(edl.entries)} cuts"
                 + (f", {n_trans} transitions" if n_trans else "") + speak_note)

    # 4. render + assemble --------------------------------------------------- #
    # Handles: each transition borrows transition_dur/2 of source from the clip on
    # either side of the join, so the overlaps net out to the planned timeline.
    # Drop beats (energy hits) get a centred zoom punch; resolve them once so the
    # render loop can match each shot's place_at against them.
    # Every beat-grid read below is guarded: match-cut mode has no grid, and all of
    # these are FX timing that mode switches off anyway (punch/flash/split/shake = 0).
    want_drop_fx = punch > 0 or rgb_split > 0 or shake > 0
    drop_times = ([t for t, is_drop in _accent_times(grid, edl.timeline_duration) if is_drop]
                  if (want_drop_fx and grid) else [])
    beat_period = 60.0 / grid.tempo if (grid and grid.tempo and grid.tempo > 0) else 0.5
    drop_tol = max(0.5 * beat_period, 0.15)
    # White flash on loud downbeat cuts (Music Video). Sparse by construction — only
    # every 4th beat, and only where that beat is loud — so it reads as an on-beat
    # strobe through the drops, not a seizure-inducing every-cut flicker.
    flash_times = ([b.time for b in grid.beats if b.is_downbeat and b.energy >= FLASH_ENERGY]
                   if (flash > 0 and grid) else [])
    n = len(edl.entries)
    # Per-segment parameters are resolved SEQUENTIALLY first (cheap, deterministic),
    # then the renders — independent single-input ffmpeg jobs writing distinct files
    # with everything they need precomputed — run in a small worker pool. The pool
    # changes wall-clock only: same commands, same outputs, same failure semantics
    # (first error cancels the queue and fails the job). Machine-gun Music Video
    # plans hundreds of sub-second segments whose cost is process spawn + input
    # open + seek, so the pool is near-linear there.
    specs: list[tuple] = []
    joins: list[tuple[str, float]] = []
    for i, entry in enumerate(edl.entries):
        lead = entry.transition_dur / 2.0           # incoming transition (this join)
        nxt = edl.entries[i + 1] if i + 1 < n else None
        tail = (nxt.transition_dur / 2.0) if nxt else 0.0   # outgoing transition
        # Per-shot motion is a sparse, centred accent — never a pan. A shot landing
        # on a drop beat gets a zoom *punch* (the camera kick on the hit); a long
        # held shot gets a slow push-in so it breathes; everything else stays
        # locked off. The punch wins when a held shot also happens to be a drop.
        zoom = None
        on_drop = bool(drop_times) and any(abs(entry.place_at - dt) <= drop_tol for dt in drop_times)
        big_drop = on_drop and entry.duration >= 0.4
        if big_drop and punch > 0:
            zoom = ("punch", punch)
        elif push_in > 0 and entry.duration >= HELD_MIN_S:
            zoom = ("push_in", push_in)
        seg_flash = (flash if flash > 0 and entry.duration >= 0.2
                     and any(abs(entry.place_at - ft) <= drop_tol for ft in flash_times)
                     else 0.0)
        # Stacked drop accents — punch + chroma split + shake detonate together on
        # the hit (the signature combo); held/quiet shots stay clean.
        seg_rgb = rgb_split if big_drop else 0.0
        seg_shake = shake if big_drop else 0.0
        specs.append((entry, work_dir / f"seg_{i:04d}.mp4", lead, tail, zoom,
                      seg_flash, seg_rgb, seg_shake))
        joins.append((entry.transition, entry.transition_dur))

    segments: list[Path] = [s[1] for s in specs]
    durations: list[float] = [0.0] * n

    def _render_one(i: int) -> None:
        entry, seg, lead, tail, zoom, seg_flash, seg_rgb, seg_shake = specs[i]
        _render_segment(entry, seg, width=width, height=height, fps=fps,
                        video_encode_args=video_encode_args, gpu=hwaccel_decode,
                        lead=lead, tail=tail, fill=fill, zoom=zoom,
                        grade=grade, flash=seg_flash, rgb_split=seg_rgb, shake=seg_shake,
                        playback_speed=entry.playback_speed)
        # Use the segment's *actual* rendered length, not the nominal target:
        # frame-rounding makes each clip a few ms short, and across a run that
        # accumulates enough to push an xfade offset past the real end of its
        # first input, which silently drops everything after the transition.
        durations[i] = probe_duration(seg) if n_trans else entry.duration + lead + tail

    # progress() must stay on this thread — the worker protocol is one JSON line
    # per write on stdout, and interleaved writes from pool threads could shear a
    # line — so completions are counted here as futures resolve.
    progress("rendering", _ov("rendering", 0.0), 0.0,
             f"Rendering {n} segments…")
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, min(RENDER_WORKERS, n))) as pool:
        futures = [pool.submit(_render_one, i) for i in range(n)]
        try:
            for fut in as_completed(futures):
                fut.result()                    # re-raise the first failed segment
                done += 1
                sp = done / max(1, n)
                progress("rendering", _ov("rendering", sp * 0.9), sp,
                         f"Rendering segment {done} of {n}")
        except BaseException:
            for f in futures:
                f.cancel()                      # in-flight ffmpeg jobs still drain
            raise

    progress("rendering", _ov("rendering", 0.95), 0.95, "Assembling final cut…")
    # Build the duck envelope for each spoken shot: fade the music DOWN just before
    # the line, hold, then fade it back UP to land on the next beat ("into the beat").
    beat_times = [b.time for b in grid.beats] if grid else []
    duck_windows: list[dict] = []
    for e in edl.entries:
        if not e.speak:
            continue
        up_start = e.place_at + e.duration
        nb = next((t for t in beat_times if t > up_start + 0.05), None)
        fu = (_clamp(nb - up_start, SPEAK_UNDUCK_MIN, SPEAK_UNDUCK_MAX) if nb is not None
              else SPEAK_UNDUCK_MIN)
        duck_windows.append({
            "place_at": e.place_at, "dur": e.duration,
            # _duck_and_mux seeks the RAW source file for the dialogue, so this
            # in_point must be in original clip time — add the trimmed head back.
            "in_point": e.in_point + e.head_offset,
            "src_path": e.src_path, "a_start": max(0.0, e.place_at - SPEAK_DUCK_FADE),
            "up_start": up_start, "up_end": up_start + fu,
        })
    if duck_windows:
        # Build the video first (no song), then lay the song under it — ducked with
        # a fade in/out — and mix each clip's own dialogue in over the dip.
        tmp_video = work_dir / "video_only.mp4"
        if n_trans:
            assemble_transitions(segments, durations, joins, song_path, tmp_video,
                                 video_encode_args=video_encode_args, fps=fps, mux_audio=False)
        else:
            assemble(segments, song_path, tmp_video, mux_audio=False)
        _duck_and_mux(tmp_video, song_path, duck_windows, out_path)
    elif keep_audio:
        # Songless match cut: assemble the video, build a timeline-length track from
        # the shots' own audio, then mux the two.
        tmp_video = work_dir / "video_only.mp4"
        if n_trans:
            assemble_transitions(segments, durations, joins, song_path, tmp_video,
                                 video_encode_args=video_encode_args, fps=fps, mux_audio=False)
        else:
            assemble(segments, song_path, tmp_video, mux_audio=False)
        track = work_dir / "clip_audio.wav"
        n_audio = _clip_audio_track(edl.entries, work_dir, track)
        if n_audio:
            _run(["ffmpeg", "-y", "-v", "error", "-i", str(tmp_video), "-i", str(track),
                  "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                  "-c:a", "aac", "-b:a", "192k", "-shortest",
                  "-movflags", "+faststart", str(out_path)], "mux clip audio")
        else:
            tmp_video.replace(out_path)   # no clip had audio — ship it silent
    elif n_trans:
        # song_path is None in match-cut mode with no song — the assemblers ignore it
        # entirely when mux_audio is False, yielding a valid silent MP4.
        assemble_transitions(segments, durations, joins, song_path, out_path,
                             video_encode_args=video_encode_args, fps=fps,
                             mux_audio=song_path is not None)
    else:
        assemble(segments, song_path, out_path, mux_audio=song_path is not None)

    # Safety net + honest duration: a silent xfade overshoot used to ship a stub
    # (e.g. 12s of a 240s plan). Probe the real file; fail loudly if it came out wildly
    # short rather than committing garbage, and report the ACTUAL length, not the plan.
    actual = probe_duration(out_path)
    if actual > 0 and actual < 0.5 * edl.timeline_duration:
        raise RuntimeError(
            f"assembled montage is {actual:.0f}s but the plan was "
            f"{edl.timeline_duration:.0f}s — transition assembly dropped clips")
    size = out_path.stat().st_size
    result = {
        "width": width, "height": height, "fps": fps,
        "duration": round(actual if actual > 0 else edl.timeline_duration, 3),
        "size_bytes": size, "cuts": len(edl.entries), "seed": seed,
        "preset": preset, "transitions": n_trans, "spoken": len(duck_windows),
        "intro_cards_trimmed": n_trimmed, "mode": mode,
    }
    if mode == "matchcut":
        # How many shots contributed their own audio (songless match cut only).
        result["audio_shots"] = n_audio
    if auto_stats:
        # What Auto Montage chose and from how much — the panel's readout.
        result["auto"] = auto_stats
    if grid is not None:
        # Beat-engine provenance only exists when a grid was built. The panel guards
        # this behind an #if, so omitting it in match-cut mode is safe.
        result.update(beat_engine=grid.engine, beat_device=grid.device,
                      beat_engine_note=grid.engine_note)
    if match_stats:
        # Surface what the matcher actually found — a weak run must be diagnosable
        # rather than reading as a success that merely looks like an ordinary montage.
        result["match"] = match_stats
    return result


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
            # Optional: Motion Match Cut renders with no song at all.
            song_path=(Path(spec["song_path"]) if spec.get("song_path") else None),
            options=spec["options"],
            work_dir=Path(spec["work_dir"]),
            out_path=Path(spec["out_path"]),
            video_encode_args=list(spec["video_encode_args"]),
            hwaccel_decode=bool(spec["hwaccel_decode"]),
            progress=progress,
            cache_dir=(Path(spec["motion_cache_dir"]) if spec.get("motion_cache_dir") else None),
            beat_cache_dir=(Path(spec["beat_cache_dir"]) if spec.get("beat_cache_dir") else None),
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
