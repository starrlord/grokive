"""Web front-end for Grokive.

Serves the incremental gallery and exposes two actions used by the toolbar that is
injected into the page:

* **Sync**   -> runs ``grokive.py reindex`` -> ``download`` -> ``agents`` ->
               ``conversations`` -> ``index`` in a background thread, streaming output
               to a rolling log.
* **Config** -> stores one or more named Grok accounts, each a captured browser
               cURL (``grok_accounts.json`` registry + per-account session files),
               on the persistent data volume.

All state lives under ``GROK_DATA_DIR`` (``/data`` in the container) so it survives
container recreation. Designed to run under waitress as a single process; sync
state is kept in-process and guarded by a lock.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    request,
    send_file,
    send_from_directory,
    session,
)
from werkzeug.security import check_password_hash, generate_password_hash

import db
import gdownloader
import moviegen
import promptstudio
import xai_imagine
from mediautil import media_shard
# Aliased: the `/thumbnails/...` route function below is also named `thumbnails`
# and would otherwise shadow this module at module scope.
import thumbnails as thumbgen

# Bound image decoding (thumbnails, upload transcode) so a decompression-bomb image
# can't pin CPU/RAM — well above any real photo, well under Pillow's ~178 MP default.
from PIL import Image as _PILImage
_PILImage.MAX_IMAGE_PIXELS = 64_000_000

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("GROK_DATA_DIR", ROOT / "data")).resolve()

GALLERY_DIR = DATA_DIR / "gallery"
INCREMENTAL_DIR = GALLERY_DIR / "gallery_incremental"
MEDIA_DIR = GALLERY_DIR / "media"
THUMBS_DIR = GALLERY_DIR / "thumbnails"
CURL_FILE = DATA_DIR / "grok_auth.txt"
# Legacy filename (pre-rename); still read so existing data volumes keep working.
LEGACY_CURL_FILE = DATA_DIR / "curl_samples.txt"
# Multi-account Grok sessions: the registry (names / active flags) lives in
# grok_accounts.json; each account's pasted cURL lives in grok_accounts/<id>.txt.
# The migrated "default" account keeps using grok_auth.txt so from-source CLI runs
# and old backups keep lining up unchanged.
ACCOUNTS_FILE = DATA_DIR / "grok_accounts.json"
ACCOUNTS_DIR = DATA_DIR / "grok_accounts"
METADATA_FILE = DATA_DIR / "metadata.json"
DELETED_FILE = DATA_DIR / "deleted_ids.json"  # blocklist: ids the downloader must never re-pull
PLAYLISTS_FILE = DATA_DIR / "playlists.json"
COLLECTIONS_FILE = DATA_DIR / "collections.json"
COLLECTION_GROUPS_FILE = DATA_DIR / "collection_groups.json"
SCENES_FILE = DATA_DIR / "scenes.json"  # saved Prompt Studio Scene Builder scenes
RESPONSES_FILE = DATA_DIR / "saved_responses.json"  # Prompt Studio responses the user starred
PERSONAS_FILE = DATA_DIR / "personas.json"  # Prompt Studio persona / voice cards
FREEFORM_PRESETS_FILE = DATA_DIR / "freeform_presets.json"  # saved Freeform request + required text presets
SAVED_PROMPT_TEXT_LIMIT = 100_000
SETTINGS_FILE = DATA_DIR / "settings.json"
LIBRARY_FILE = DATA_DIR / "library.json"
DB_FILE = DATA_DIR / "index.db"
# Prompt Studio embeddings — a DURABLE store, separate from index.db (which is a
# disposable read-model rebuilt from metadata.json). Keyed by prompt-text hash so a
# reindex never discards the embedding work and only new prompts get embedded.
PROMPT_DB_FILE = DATA_DIR / "prompt_studio.db"
MOVIE_DIR = DATA_DIR / "movie_tmp"  # working dir + last "Generate Movie" output
# Motion Match Cut's per-clip descriptor cache. DELIBERATELY a sibling of MOVIE_DIR,
# not inside it: every render wipes MOVIE_DIR, and this must survive so a second
# render over the same clips skips analysis (measured ~150x faster on a hit). Purely
# derived data — safe to delete at any time; bounded by matchcut.prune_cache.
MOTION_CACHE_DIR = DATA_DIR / "motion_cache"
# Beat-montage song-analysis cache (madmom/librosa beat grids). Same rationale as
# the motion cache: MOVIE_DIR is wiped per render and the uploaded song re-saved,
# so entries are keyed by song CONTENT hash (see moviegen.load_or_analyze_audio).
# Purely derived, safe to delete; bounded by moviegen._prune_beat_cache.
BEAT_CACHE_DIR = DATA_DIR / "beat_cache"
# Built SvelteKit SPA (produced by `web/` -> adapter-static), served at "/".
SPA_DIR = Path(os.environ.get("SPA_DIR", ROOT / "web" / "build")).resolve()

PORT = int(os.environ.get("PORT", "8080"))


def _envbool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if not v:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# --- Authentication configuration ------------------------------------------ #
# Auth is REQUIRED BY DEFAULT. Set AUTH_DISABLED=true only on a fully trusted,
# isolated LAN. Precedence: disabled -> basic (legacy) -> login (themed screen).
AUTH_DISABLED = _envbool("AUTH_DISABLED")
BASIC_AUTH_USER = os.environ.get("BASIC_AUTH_USER", "").strip()
BASIC_AUTH_PASS = os.environ.get("BASIC_AUTH_PASS", "")
ADMIN_USER = os.environ.get("ADMIN_USER", "").strip() or "admin"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ADMIN_PW_FILE = DATA_DIR / "admin_password.txt"
ADMIN_PASSWORD_GENERATED = False
# Trust X-Forwarded-* from a reverse proxy (real client IPs for rate-limiting +
# HTTPS detection for secure cookies). Only enable when actually behind a proxy.
TRUST_PROXY = _envbool("TRUST_PROXY")
WHISPER_ENV = os.environ.get("WHISPER_SERVER_URL", "").strip()
# Prompt Studio AI engine — optional, self-hosted, gated exactly like Whisper. Point
# these at an Ollama / OpenAI-compatible server (its /v1 base). Feature degrades to the
# Phase-0 offline composer when unset. Env vars win over the saved settings.
EMBED_ENV = os.environ.get("EMBED_SERVER_URL", "").strip()
EMBED_MODEL_ENV = os.environ.get("EMBED_MODEL", "").strip()
LLM_ENV = os.environ.get("LLM_SERVER_URL", "").strip()
LLM_MODEL_ENV = os.environ.get("LLM_MODEL", "").strip()
LLM_VISION_MODEL_ENV = os.environ.get("LLM_VISION_MODEL", "").strip()
EMBED_API_KEY_ENV = os.environ.get("EMBED_API_KEY", "").strip()
LLM_API_KEY_ENV = os.environ.get("LLM_API_KEY", "").strip()
OPENAI_API_KEY_ENV = os.environ.get("OPENAI_API_KEY", "").strip()
OPENROUTER_API_KEY_ENV = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_HTTP_REFERER = os.environ.get("OPENROUTER_HTTP_REFERER", "").strip()
OPENROUTER_APP_TITLE = os.environ.get("OPENROUTER_APP_TITLE", "").strip() or "Grokive"
EMBED_MODEL_DEFAULT = "nomic-embed-text"
LLM_MODEL_DEFAULT = "dolphin3"
OPENAI_LLM_MODEL_DEFAULT = "gpt-5.4-mini"
OPENROUTER_LLM_MODEL_DEFAULT = "openai/gpt-5.4-mini"
# Grok Imagine API (xAI) — image & video generation. The API key is the only secret;
# the base URL is fixed (unlike the user-pointable Prompt Studio endpoints). Env vars
# win over the saved setting, exactly like the LLM_* keys.
XAI_BASE = "https://api.x.ai/v1"
XAI_API_KEY_ENV = os.environ.get("XAI_API_KEY", "").strip()
XAI_IMAGE_MODEL_ENV = os.environ.get("XAI_IMAGE_MODEL", "").strip()
XAI_VIDEO_MODEL_ENV = os.environ.get("XAI_VIDEO_MODEL", "").strip()
XAI_IMAGE_MODEL_DEFAULT = "grok-imagine-image-quality"
XAI_VIDEO_MODEL_DEFAULT = "grok-imagine-video"


def _auth_mode() -> str:
    if AUTH_DISABLED:
        return "off"
    if BASIC_AUTH_USER:
        return "basic"
    return "login"


def _resolve_admin_password() -> tuple[str, bool]:
    """(password, generated). With login auth on and no ADMIN_PASSWORD set, generate
    a strong password once and persist it under /data so it's stable across restarts
    (printed to the log on startup)."""
    if ADMIN_PASSWORD:
        return ADMIN_PASSWORD, False
    try:
        if ADMIN_PW_FILE.exists():
            pw = ADMIN_PW_FILE.read_text(encoding="utf-8").strip()
            if pw:
                return pw, True
    except Exception:
        pass
    pw = secrets.token_urlsafe(15)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ADMIN_PW_FILE.write_text(pw, encoding="utf-8")
    except Exception:
        pass
    return pw, True


if _auth_mode() == "login":
    ADMIN_PASSWORD, ADMIN_PASSWORD_GENERATED = _resolve_admin_password()

app = Flask(__name__)
if TRUST_PROXY:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Session secret: stable across restarts (derived from the admin creds so changing
# the password invalidates old sessions), overridable via SESSION_SECRET.
_session_secret = os.environ.get("SESSION_SECRET", "").strip()
if not _session_secret:
    if _auth_mode() == "login":
        _session_secret = hashlib.sha256(f"ga-session:{ADMIN_USER}:{ADMIN_PASSWORD}".encode()).hexdigest()
    else:
        _session_secret = secrets.token_hex(32)
app.secret_key = _session_secret
app.config["PERMANENT_SESSION_LIFETIME"] = datetime.timedelta(days=30)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Hard cap on any request body. 5 GB so the folder Import can take large source videos
# (it streams each upload straight to disk, one file per request, never buffering it in
# memory). Stricter per-route limits still apply where wanted (e.g. the Imagine image
# upload caps itself at 30 MB) — this is only the outer backstop. Set to None to uncap.
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024 * 1024
# SESSION_COOKIE_SECURE: true | false | auto (default). 'auto' = secure when behind an
# HTTPS-terminating proxy (TRUST_PROXY on). Never forced on plain HTTP, or the login
# cookie would be dropped and you could never stay signed in.
_secure_env = os.environ.get("SESSION_COOKIE_SECURE", "auto").strip().lower()
if _secure_env in ("1", "true", "yes", "on"):
    _cookie_secure = True
elif _secure_env in ("0", "false", "no", "off"):
    _cookie_secure = False
else:
    _cookie_secure = TRUST_PROXY
app.config["SESSION_COOKIE_SECURE"] = _cookie_secure


# --------------------------------------------------------------------------- #
# Grok accounts — one or more named sessions, each a pasted browser cURL. The
# registry holds names/active flags only; the secrets stay in per-account files.
# --------------------------------------------------------------------------- #

# New account ids are secrets.token_hex(4); "default" is the migrated legacy slot.
_ACCOUNT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,31}$")
MAX_ACCOUNTS = 20
# Serialises the registry's read-modify-write cycles (waitress is multithreaded, so
# two overlapping saves would otherwise be last-write-wins and silently drop one
# request's toggle/rename). Endpoints take this around load->mutate->save;
# _load_accounts itself must stay lock-free (it's called inside these sections).
_accounts_lock = threading.Lock()


def _utc_stamp() -> str:
    """Sortable full-precision UTC timestamp. Lexicographically compatible with the
    date-only stamps written before it existed ('2026-07-08' < '2026-07-08T09:00:00Z')."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _account_curl_path(acct_id: str) -> Path:
    return CURL_FILE if acct_id == "default" else ACCOUNTS_DIR / f"{acct_id}.txt"


def _account_curl_rel(acct_id: str) -> str:
    """The --curl path handed to the CLI, relative to DATA_DIR (the CLI's cwd)."""
    return CURL_FILE.name if acct_id == "default" else f"{ACCOUNTS_DIR.name}/{acct_id}.txt"


def _account_configured(acct_id: str) -> bool:
    if acct_id == "default":
        # gdownloader itself falls back to the legacy filename, so honour it here too.
        return CURL_FILE.exists() or LEGACY_CURL_FILE.exists()
    return _account_curl_path(acct_id).exists()


def _curl_error(text: str) -> str | None:
    """Validation shared by every route that accepts a pasted cURL; None when it looks right."""
    text = (text or "").strip()
    if not text:
        return "Empty config."
    if "grok.com" not in text or "Cookie" not in text:
        return "That doesn't look like a Grok cURL request (missing grok.com / Cookie)."
    return None


def _write_account_curl(acct_id: str, body: str) -> None:
    dest = _account_curl_path(acct_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".txt.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(dest)
    # Saving the default slot migrates off the legacy file so there's one source of truth.
    if acct_id == "default" and LEGACY_CURL_FILE.exists():
        try:
            LEGACY_CURL_FILE.unlink()
        except OSError:
            pass


def _save_accounts(accounts: list[dict]) -> None:
    _atomic_write_json(ACCOUNTS_FILE, accounts)


def _load_accounts() -> list[dict]:
    """The sanitised account registry. A pre-registry grok_auth.txt is migrated into a
    single always-on 'default' entry on first read (kept at that filename so CLI runs
    and old backups still line up)."""
    data: list = []
    if ACCOUNTS_FILE.exists():
        try:
            loaded = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                data = loaded
        except Exception:
            data = []
    clean: list[dict] = []
    seen: set[str] = set()
    for entry in data[:MAX_ACCOUNTS]:
        if not isinstance(entry, dict):
            continue
        acct_id = str(entry.get("id") or "").strip()
        name = str(entry.get("name") or "").strip()[:60]
        if not name or acct_id in seen:
            continue
        # The id doubles as the session filename — regex-gate it so a hand-edited
        # registry can never point outside grok_accounts/.
        if acct_id != "default" and not _ACCOUNT_ID_RE.fullmatch(acct_id):
            continue
        seen.add(acct_id)
        clean.append({
            "id": acct_id,
            "name": name,
            "active": bool(entry.get("active", True)),
            "created_at": str(entry.get("created_at") or "")[:32],
            "updated_at": str(entry.get("updated_at") or "")[:32],
        })
    if not clean and not ACCOUNTS_FILE.exists() and (CURL_FILE.exists() or LEGACY_CURL_FILE.exists()):
        stamp = _utc_stamp()
        clean = [{"id": "default", "name": "Account 1", "active": True,
                  "created_at": stamp, "updated_at": stamp}]
        _save_accounts(clean)
    return clean


# --------------------------------------------------------------------------- #
# Sync job state (single in-process job, guarded by a lock)
# --------------------------------------------------------------------------- #

_sync_lock = threading.Lock()
_sync = {
    "running": False,
    "job": "sync",           # sync | subtitles
    "step": "idle",          # download | agents | gallery | done | error | idle
    "returncode": None,
    "started_at": None,
    "finished_at": None,
    # Set the instant the core download/index steps finish — before the optional
    # (and slow) Autonomous Mode post-steps. Lets the UI show new media right away
    # instead of waiting for prompt autotagging to complete. None until reached.
    "media_ready_at": None,
    "log": deque(maxlen=400),
    "auth_hint": False,       # heuristic: looks like an auth/cookie problem
}

_AUTH_MARKERS = ("401", "403", "forbidden", "unauthor", "cloudflare", "cf_clearance", "expired")


def _log(line: str) -> None:
    line = line.rstrip()
    if not line:
        return
    _sync["log"].append(line)
    low = line.lower()
    if any(marker in low for marker in _AUTH_MARKERS):
        _sync["auth_hint"] = True


def _run_step(label: str, args: list[str]) -> int:
    """Run a subcommand, streaming combined output into the rolling log."""
    _sync["step"] = label
    _log(f"=== {label}: {' '.join(args)} ===")
    env = dict(os.environ, GROK_DATA_DIR=str(DATA_DIR), PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        args,
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        _log(line)
    proc.wait()
    _log(f"--- {label} exited with code {proc.returncode} ---")
    return proc.returncode or 0


def _inproc_step(label: str, fn) -> int:
    """Run an in-process post-sync step, framed in the rolling log exactly like _run_step
    (=== header === / --- footer ---) so the status pill and parsed Summary view treat it the
    same as a CLI step. Best-effort: a failure is logged and returns 1 but never raises — one
    optional step can't fail the whole sync."""
    _sync["step"] = label
    _log(f"=== {label}: autonomous ===")
    code = 0
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - surfaced in the log, never fatal
        code = 1
        _log(f"{label} error: {str(exc)[:200]}")
    _log(f"--- {label} exited with code {code} ---")
    return code


def _run_autonomous_steps() -> None:
    """Autonomous Mode post-sync automation: update the prompt index, import new library prompts
    (excluding locked/hidden collections), generate subtitles for any videos still missing them
    (when a subtitle endpoint is configured), then AI-tag ONLY the freshly imported prompts. Each
    step is independently gated by its own config (a missing endpoint logs a skip, never an error)
    and is best-effort, so it can't fail the sync that just succeeded."""
    new_records: list = []

    def _do_import() -> None:
        nonlocal new_records
        merged, new_records, _ = _import_library_into_saved(exclude_hidden=True)
        _log(f"imported {len(new_records)} new prompt(s) — library now {len(merged)}")

    def _do_subtitles() -> None:
        if not _whisper_url():
            _log("skipped — no subtitle endpoint configured")
            return
        done, total = _generate_missing_subtitles()
        _log("no videos need subtitles" if not total else f"subtitled {done}/{total} video(s)")

    def _do_autotag() -> None:
        if not _llm_url():
            _log("skipped — no LLM endpoint configured")
            return
        if not new_records:
            _log("no new prompts to tag")
            return
        tagged = _autotag_records(new_records, log=_log)
        _log(f"tagged {tagged}/{len(new_records)} new prompt(s)")

    _inproc_step("embed", lambda: _run_embed_build_inline(_log))
    _inproc_step("library", _do_import)
    _inproc_step("subtitles", _do_subtitles)
    _inproc_step("autotag", _do_autotag)


def _sync_worker() -> None:
    py = sys.executable
    cli = str(ROOT / "grokive.py")
    active = [a for a in _load_accounts() if a.get("active")]
    # With several accounts each step label carries the account name — " [Name]" —
    # which the UI splits back apart for the status pill and the Summary view. The
    # name is kept out of the "label: command" framing separator (no colons).
    multi = len(active) > 1
    rc = 0
    try:
        # Reindex FIRST so any media that exists on disk but is missing from
        # metadata.json is folded back in before the downloader runs (prevents
        # re-downloading files we already have).
        rc = _run_step("reindex", [py, cli, "reindex"])
        if rc != 0:
            _sync["step"] = "error"
            return
        # Download each active account in turn, exactly like a single-account sync.
        # A failing account is logged and skipped so the remaining accounts still
        # sync; the whole job then finishes as an error to surface the failure.
        failures = 0
        if not active:
            _log("no active Grok accounts — skipping download/agents (add one in Config)")
        for acct in active:
            tag = f" [{acct['name'].replace(':', ' ')}]" if multi else ""
            if not _account_configured(acct["id"]):
                _log(f"=== download{tag}: skipped ===")
                _log(f"account '{acct['name']}' has no saved cURL session — edit it in Config")
                _log(f"--- download{tag} exited with code 1 ---")
                failures += 1
                continue
            curl_rel = _account_curl_rel(acct["id"])
            acct_rc = _run_step(f"download{tag}", [py, cli, "download", "--curl", curl_rel])
            if acct_rc == 0:
                acct_rc = _run_step(f"agents{tag}", [py, cli, "agents", "--curl", curl_rel])
            if acct_rc == 0:
                # Imagine v2 media never reaches the favorites list "download" reads, and
                # its chain isn't on the post — it only exists in the conversation.
                acct_rc = _run_step(f"conversations{tag}", [py, cli, "conversations", "--curl", curl_rel])
            if acct_rc != 0:
                failures += 1
                rc = acct_rc
        if failures and rc == 0:
            rc = 1  # skipped-unconfigured accounts must fail the job too
        # Index once at the end so media from the accounts that DID succeed becomes
        # visible even when another account failed.
        idx_rc = _run_step("index", [py, cli, "index"])  # thumbnails + SQLite read-model
        if idx_rc != 0:
            rc = idx_rc
            _sync["step"] = "error"
            return
        # Media is now indexed and visible to the API. Mark the milestone so the UI
        # can refresh the gallery NOW, before the optional (and potentially slow)
        # autotagging post-steps run.
        _sync["media_ready_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        # Warm the montage motion cache for whatever just landed, so Beat Montage
        # analysis and Auto Montage clip picking hit the cache instead of decoding.
        # Best-effort: a failed warm-up never flips the sync to error (montages
        # just analyze those clips lazily at render time).
        _run_step("motioncache", [py, cli, "motioncache"])
        # Run optional post-sync automation (best-effort; never flips the sync to
        # error). Off unless the toggle is set.
        if _autonomous_enabled():
            _run_autonomous_steps()
        _sync["step"] = "error" if failures else "done"
    except Exception as exc:  # pragma: no cover - defensive
        rc = 1
        _sync["step"] = "error"
        _log(f"sync crashed: {exc}")
    finally:
        _sync["returncode"] = rc
        _sync["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _sync["running"] = False


def start_sync() -> bool:
    with _sync_lock:
        if _sync["running"]:
            return False
        _sync["running"] = True
        _sync["job"] = "sync"
        _sync["step"] = "download"
        _sync["returncode"] = None
        _sync["auth_hint"] = False
        _sync["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _sync["finished_at"] = None
        _sync["media_ready_at"] = None
        _sync["log"].clear()
    threading.Thread(target=_sync_worker, daemon=True).start()
    return True


# --------------------------------------------------------------------------- #
# Settings (Whisper URL + burn-subtitles toggle), persisted on the data volume
# --------------------------------------------------------------------------- #

def _load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _save_settings(data: dict) -> None:
    _atomic_write_json(SETTINGS_FILE, data)


def _whisper_url() -> str:
    """Effective Whisper endpoint: env var wins, else the saved setting."""
    return WHISPER_ENV or str(_load_settings().get("whisper_server_url", "")).strip()


def _burn_enabled() -> bool:
    return bool(_load_settings().get("burn_subtitles"))


def _autonomous_enabled() -> bool:
    """When on, a finished Sync auto-runs post-steps: update the prompt index, import new
    prompts into the library, and AI-tag the freshly imported ones. See _run_autonomous_steps."""
    return bool(_load_settings().get("autonomous_mode"))


# Subtitle display style — shared by the player's ::cue rendering (read via
# /api/settings) and the burned-in export (mapped to libass below). Curated font
# keys must stay in sync with SUBTITLE_FONTS in web/src/lib/state.js; each maps to
# a family fontconfig can resolve in the container (fonts-dejavu-core, see Dockerfile).
_SUB_FONT_LIBASS = {
    "system": "DejaVu Sans",
    "sans": "DejaVu Sans",
    "serif": "DejaVu Serif",
    "mono": "DejaVu Sans Mono",
}


def _sub_size(v) -> int:
    """Subtitle size as a percentage of the renderer default, clamped 25–400."""
    try:
        return max(25, min(400, int(round(float(v)))))
    except (TypeError, ValueError):
        return 170


def _sub_opacity(v) -> float:
    """Background-box opacity 0.0–1.0."""
    try:
        return max(0.0, min(1.0, round(float(v), 3)))
    except (TypeError, ValueError):
        return 0.25


def _sub_color(v) -> str:
    """A #RRGGBB hex colour, or white if malformed."""
    s = str(v or "").strip()
    return s.lower() if re.fullmatch(r"#[0-9a-fA-F]{6}", s) else "#ffffff"


def _embed_url() -> str:
    """Effective embeddings endpoint (OpenAI-compatible base, e.g. .../v1). Env wins."""
    return EMBED_ENV or str(_load_settings().get("embed_server_url", "")).strip()


def _embed_model() -> str:
    return EMBED_MODEL_ENV or str(_load_settings().get("embed_model", "")).strip() or EMBED_MODEL_DEFAULT


def _llm_url() -> str:
    """Effective chat endpoint (OpenAI-compatible base, e.g. .../v1). Env wins."""
    return LLM_ENV or str(_load_settings().get("llm_server_url", "")).strip()


def _llm_model() -> str:
    return LLM_MODEL_ENV or str(_load_settings().get("llm_model", "")).strip() or LLM_MODEL_DEFAULT


def _llm_vision_model() -> str:
    """Effective multimodal (image-input) chat model for the lightbox "Describe for Grok"
    feature. Falls back to the text model so a single vision-capable model still works
    without setting this separately. Env wins, then settings, then the chat model."""
    return (
        LLM_VISION_MODEL_ENV
        or str(_load_settings().get("llm_vision_model", "")).strip()
        or _llm_model()
    )


def _provider_from_url(url: str) -> str:
    host = urllib.parse.urlparse(str(url or "")).netloc.lower()
    if "openrouter.ai" in host:
        return "openrouter"
    if "openai.com" in host:
        return "openai"
    return "custom" if url else "local"


def _provider_key(url: str) -> str:
    provider = _provider_from_url(url)
    if provider == "openrouter":
        return OPENROUTER_API_KEY_ENV
    if provider == "openai":
        return OPENAI_API_KEY_ENV
    return ""


def _provider_display(provider: str) -> str:
    return {"openai": "OpenAI", "openrouter": "OpenRouter"}.get(provider, provider.title())


def _llm_api_key() -> str:
    url = _llm_url()
    settings = _load_settings()
    return (
        LLM_API_KEY_ENV
        or str(settings.get("llm_api_key", "")).strip()
        or _provider_key(url)
    )


def _embed_api_key() -> str:
    url = _embed_url()
    settings = _load_settings()
    return (
        EMBED_API_KEY_ENV
        or str(settings.get("embed_api_key", "")).strip()
        or _provider_key(url)
    )


def _llm_api_key_env_locked() -> bool:
    return bool(LLM_API_KEY_ENV or _provider_key(_llm_url()))


def _embed_api_key_env_locked() -> bool:
    return bool(EMBED_API_KEY_ENV or _provider_key(_embed_url()))


def _xai_api_key() -> str:
    """Effective xAI Imagine API key: env var wins, else the saved setting."""
    return XAI_API_KEY_ENV or str(_load_settings().get("xai_api_key", "")).strip()


def _xai_api_key_env_locked() -> bool:
    return bool(XAI_API_KEY_ENV)


def _xai_image_model() -> str:
    return XAI_IMAGE_MODEL_ENV or str(_load_settings().get("xai_image_model", "")).strip() or XAI_IMAGE_MODEL_DEFAULT


def _xai_video_model() -> str:
    return XAI_VIDEO_MODEL_ENV or str(_load_settings().get("xai_video_model", "")).strip() or XAI_VIDEO_MODEL_DEFAULT


def _xai_image_resolution() -> str:
    return str(_load_settings().get("xai_image_resolution", "")).strip() or "1k"


def _xai_image_aspect() -> str:
    return str(_load_settings().get("xai_image_aspect_ratio", "")).strip() or "1:1"


def _xai_video_resolution() -> str:
    return str(_load_settings().get("xai_video_resolution", "")).strip() or "480p"


def _xai_video_aspect() -> str:
    return str(_load_settings().get("xai_video_aspect_ratio", "")).strip() or "16:9"


def _xai_video_duration() -> int:
    try:
        d = int(_load_settings().get("xai_video_duration") or 6)
    except (TypeError, ValueError):
        d = 6
    return max(1, min(15, d))


def _provider_headers(url: str) -> dict:
    if _provider_from_url(url) != "openrouter":
        return {}
    headers = {"X-Title": OPENROUTER_APP_TITLE}
    if OPENROUTER_HTTP_REFERER:
        headers["HTTP-Referer"] = OPENROUTER_HTTP_REFERER
    return headers


def _model_list_url(base_url: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    return f"{base}/models"


def _looks_like_embedding_model(model: dict) -> bool:
    model_id = str(model.get("id") or "").lower()
    architecture = model.get("architecture") if isinstance(model.get("architecture"), dict) else {}
    modalities = [
        *(architecture.get("input_modalities") or []),
        *(architecture.get("output_modalities") or []),
    ]
    modality_text = " ".join(str(m).lower() for m in modalities)
    return "embed" in model_id or "embedding" in modality_text


def _looks_like_chat_model(model: dict) -> bool:
    model_id = str(model.get("id") or "").lower()
    if _looks_like_embedding_model(model):
        return False
    blocked = ("moderation", "whisper", "tts", "dall-e", "image", "audio", "realtime", "transcribe")
    return not any(part in model_id for part in blocked)


def _fetch_provider_models(provider: str, base_url: str, api_key: str, kind: str) -> list[dict]:
    url = _model_list_url(base_url)
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    headers.update(_provider_headers(base_url))
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw or "{}")
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    models = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = str(row.get("id") or "").strip()
        if not model_id:
            continue
        if kind == "embed" and not _looks_like_embedding_model(row):
            continue
        if kind == "llm" and not _looks_like_chat_model(row):
            continue
        name = str(row.get("name") or row.get("owned_by") or "").strip()
        models.append({"id": model_id, "name": name})
    return sorted(models, key=lambda m: m["id"].lower())


def _ollama_native_root(base_url: str) -> str:
    """Ollama's native API lives at the host root; its OpenAI-compatible base ends in /v1."""
    base = str(base_url or "").strip().rstrip("/")
    if base.lower().endswith("/v1"):
        base = base[:-3].rstrip("/")
    return base


def _fetch_ollama_vision_models(base_url: str, *, timeout: float = 20.0) -> list[dict]:
    """Installed Ollama models that can accept images, via the native ``/api/tags`` (which
    reports per-model ``capabilities`` — the OpenAI ``/v1/models`` list doesn't). Raises if
    the endpoint isn't an Ollama server, so the caller can fall back to a generic list. If
    this Ollama build doesn't report capabilities at all, returns every model (can't filter)
    rather than an empty list."""
    url = _ollama_native_root(base_url) + "/api/tags"
    req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
    rows = [r for r in (data.get("models") or []) if isinstance(r, dict)]
    any_caps = False
    vision: list[dict] = []
    for row in rows:
        caps = [str(c).lower() for c in (row.get("capabilities") or [])]
        if caps:
            any_caps = True
        name = str(row.get("name") or row.get("model") or "").strip()
        if name and "vision" in caps:
            vision.append({"id": name, "name": "vision"})
    if not any_caps:  # capability data unavailable — offer everything rather than nothing
        vision = [{"id": str(r.get("name") or r.get("model") or "").strip(), "name": ""}
                  for r in rows if (r.get("name") or r.get("model"))]
    return sorted(vision, key=lambda m: m["id"].lower())


def _llm_extra_headers() -> dict:
    return _provider_headers(_llm_url())


def _embed_extra_headers() -> dict:
    return _provider_headers(_embed_url())


# --------------------------------------------------------------------------- #
# Whisper transcription (whisper-asr-webservice: POST /asr?output=srt)
# --------------------------------------------------------------------------- #

def _http_post_file(url: str, field: str, filename: str, content: bytes,
                    content_type: str, timeout: float = 3600.0) -> str:
    """Minimal multipart/form-data POST using the stdlib (no requests dep)."""
    boundary = "----grokArchive" + secrets.token_hex(12)
    body = b"".join([
        f"--{boundary}\r\n".encode("utf-8"),
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode("utf-8"),
        f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
        content,
        b"\r\n",
        f"--{boundary}--\r\n".encode("utf-8"),
    ])
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _whisper_srt(media_path: Path) -> str:
    """Extract 16 kHz mono audio and ask the Whisper server for SRT text.
    Returns '' when the clip has no audio stream (nothing to transcribe)."""
    url = _whisper_url()
    if not url:
        raise RuntimeError("No Whisper server URL configured.")
    if not _has_audio(media_path):
        return ""
    with tempfile.TemporaryDirectory(prefix="ga-asr-") as tmp:
        wav = Path(tmp) / "audio.wav"
        _run_ffmpeg(
            ["ffmpeg", "-y", "-i", str(media_path), "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", str(wav)],
            "extract audio",
        )
        sep = "&" if "?" in url else "?"
        endpoint = f"{url}{sep}output=srt&task=transcribe&encode=true"
        return _http_post_file(endpoint, "audio_file", "audio.wav", wav.read_bytes(), "audio/wav")


def _srt_to_vtt(srt: str) -> str:
    """Convert SRT text to WebVTT (header + comma->dot in timestamps)."""
    body = (srt or "").strip()
    if not body:
        return ""
    body = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", body)
    return "WEBVTT\n\n" + body + "\n"


def _video_items() -> list[dict]:
    """All video records from metadata.json (id + local_path)."""
    if not METADATA_FILE.exists():
        return []
    try:
        items = json.loads(METADATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("media_type") == "video" and item.get("local_path"):
            out.append(item)
    return out


def _generate_missing_subtitles(progress=None) -> tuple[int, int]:
    """Generate sidecar .srt/.vtt for every video that lacks them, then rebuild index.db
    (only when something was produced) so the new tracks surface in the UI. Returns
    (done, total): total = videos that needed subtitles, done = those that finished without
    raising (a clip with no speech still counts as done). `progress(index, total)` is invoked
    before each file — the manual job uses it to drive the status pill; the autonomous step
    leaves it None and relies on the log. Shared by the manual job and the post-sync step."""
    videos = _video_items()
    todo = []
    for item in videos:
        rel = str(item.get("local_path", "")).replace("\\", "/")
        path = (GALLERY_DIR / rel).resolve()
        if path.exists() and not path.with_suffix(".srt").exists():
            todo.append(path)
    total = len(todo)
    _log(f"{total} video(s) need subtitles ({len(videos)} total, "
         f"{len(videos) - total} already done)")
    done = 0
    for index, path in enumerate(todo, start=1):
        remaining = total - index
        if progress:
            progress(index, total)
        _log(f"[{index}/{total}] {path.name} …")
        try:
            srt = _whisper_srt(path)
            path.with_suffix(".srt").write_text(srt, encoding="utf-8")
            vtt = _srt_to_vtt(srt)
            if vtt:
                path.with_suffix(".vtt").write_text(vtt, encoding="utf-8")
            note = "no speech/audio" if not srt.strip() else "ok"
            _log(f"  {note} ({remaining} remaining)")
            done += 1
        except Exception as exc:
            _log(f"  failed: {exc}")
    if total:
        # Rebuild the index so the new subtitle tracks show up in the UI.
        _log("rebuilding index...")
        rebuild_db()
    return done, total


def _subtitles_worker() -> None:
    rc = 0
    try:
        def _pill(index: int, total: int) -> None:
            _sync["step"] = f"{index}/{total}"
        _generate_missing_subtitles(progress=_pill)
        _sync["step"] = "done"
    except Exception as exc:  # pragma: no cover - defensive
        rc = 1
        _sync["step"] = "error"
        _log(f"subtitles crashed: {exc}")
    finally:
        _sync["returncode"] = rc
        _sync["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _sync["running"] = False


def start_subtitles() -> bool:
    if not _whisper_url():
        return False
    with _sync_lock:
        if _sync["running"]:
            return False
        _sync["running"] = True
        _sync["job"] = "subtitles"
        _sync["step"] = "scanning"
        _sync["returncode"] = None
        _sync["auth_hint"] = False
        _sync["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _sync["finished_at"] = None
        _sync["media_ready_at"] = None
        _sync["log"].clear()
    threading.Thread(target=_subtitles_worker, daemon=True).start()
    return True


def _motioncache_worker() -> None:
    """One-step job: run the CLI motion-cache warm-up (`grokive.py motioncache`).
    Shares the sync job slot + log, so the existing log panel shows its progress."""
    rc = 1
    try:
        rc = _run_step("motioncache",
                       [sys.executable, str(ROOT / "grokive.py"), "motioncache"])
        _sync["step"] = "done" if rc == 0 else "error"
    except Exception as exc:  # pragma: no cover - defensive
        _sync["step"] = "error"
        _log(f"motioncache crashed: {exc}")
    finally:
        _sync["returncode"] = rc
        _sync["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _sync["running"] = False


def start_motioncache() -> bool:
    """Library motion warm-up on demand (the Generate Movie panel's Analyze
    Library button). Same single job slot as sync/subtitles."""
    with _sync_lock:
        if _sync["running"]:
            return False
        _sync["running"] = True
        _sync["job"] = "motioncache"
        _sync["step"] = "motioncache"
        _sync["returncode"] = None
        _sync["auth_hint"] = False
        _sync["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _sync["finished_at"] = None
        _sync["media_ready_at"] = None
        _sync["log"].clear()
    threading.Thread(target=_motioncache_worker, daemon=True).start()
    return True


# --------------------------------------------------------------------------- #
# Auth: themed login (ADMIN_USER/ADMIN_PASSWORD) with a Basic-auth fallback
# --------------------------------------------------------------------------- #

def _admin_configured() -> bool:
    return _auth_mode() == "login"


def _basic_ok() -> bool:
    auth = request.authorization
    if not auth:
        return False
    return (secrets.compare_digest(auth.username or "", BASIC_AUTH_USER)
            and secrets.compare_digest(auth.password or "", BASIC_AUTH_PASS))


# --- Login rate limiting (per client IP, in-process) ----------------------- #
LOGIN_MAX_FAILS = 5         # lock after this many consecutive failures
LOGIN_LOCK_SECONDS = 300    # 5 minute lockout
_login_fails: dict[str, dict] = {}
_login_lock = threading.Lock()


def _login_retry_after(ip: str) -> int:
    with _login_lock:
        rec = _login_fails.get(ip)
        if rec and rec["until"] > time.time():
            return int(rec["until"] - time.time()) + 1
    return 0


def _record_login_fail(ip: str) -> None:
    with _login_lock:
        rec = _login_fails.setdefault(ip, {"count": 0, "until": 0.0})
        rec["count"] += 1
        if rec["count"] >= LOGIN_MAX_FAILS:
            rec["until"] = time.time() + LOGIN_LOCK_SECONDS
            rec["count"] = 0


def _clear_login_fails(ip: str) -> None:
    with _login_lock:
        _login_fails.pop(ip, None)


# Only the data is protected; the SPA shell + assets stay public so the login
# screen can load. These API endpoints are always reachable (to log in/out/check).
_OPEN_API = {"/api/auth/status", "/api/login", "/api/logout"}


@app.before_request
def _require_auth():
    if _admin_configured():
        path = request.path
        if path in _OPEN_API:
            return None
        if not path.startswith(("/api/", "/media/", "/thumbnails/")):
            return None  # SPA shell, assets, manifest, icons
        if session.get("authed"):
            return None
        return jsonify(ok=False, error="Authentication required."), 401
    # Legacy: HTTP Basic auth across everything.
    if _auth_mode() == "basic" and not _basic_ok():
        return Response("Authentication required.", 401, {"WWW-Authenticate": 'Basic realm="Grokive"'})
    return None


@app.get("/api/auth/status")
def api_auth_status() -> Response:
    required = _admin_configured()
    return jsonify(auth_required=required, authed=(bool(session.get("authed")) if required else True))


@app.post("/api/login")
def api_login() -> Response:
    if not _admin_configured():
        return jsonify(ok=True)
    ip = request.remote_addr or "?"
    wait = _login_retry_after(ip)
    if wait:
        return jsonify(ok=False, error=f"Too many attempts. Try again in {wait} s."), 429
    data = request.get_json(silent=True) or {}
    user_ok = secrets.compare_digest(str(data.get("username", "")), ADMIN_USER)
    pass_ok = secrets.compare_digest(str(data.get("password", "")), ADMIN_PASSWORD)
    if user_ok and pass_ok:
        _clear_login_fails(ip)
        session["authed"] = True
        session.permanent = True
        return jsonify(ok=True)
    _record_login_fail(ip)
    return jsonify(ok=False, error="Incorrect username or password."), 401


@app.post("/api/logout")
def api_logout() -> Response:
    session.clear()
    return jsonify(ok=True)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

def _spa_available() -> bool:
    return (SPA_DIR / "index.html").exists()


@app.get("/")
def index() -> Response:
    if _spa_available():
        return send_file(SPA_DIR / "index.html")
    return Response(
        "Web UI not built. Use the Docker image, or run `cd web && npm run build`.",
        503,
        mimetype="text/plain",
    )


@app.get("/media/<path:relpath>")
def media(relpath: str) -> Response:
    # Locked-collection media must not be reachable by direct URL either (the filename
    # stem is the media id). 404 keeps a locked id indistinguishable from a missing one.
    if _is_media_hidden(Path(relpath).stem):
        abort(404)
    return send_from_directory(MEDIA_DIR, relpath, conditional=True)


@app.get("/thumbnails/<path:relpath>")
def thumbnails(relpath: str) -> Response:
    if _is_media_hidden(Path(relpath).stem):
        abort(404)
    return send_from_directory(THUMBS_DIR, relpath, conditional=True)


@app.post("/api/sync")
def api_sync() -> Response:
    if start_sync():
        return jsonify(ok=True)
    return jsonify(ok=False, error="Sync already running"), 409


@app.get("/api/sync/status")
def api_sync_status() -> Response:
    # Snapshot under the lock so the response reflects one consistent moment rather
    # than a torn mix while a worker thread is updating the job.
    with _sync_lock:
        snap = dict(_sync)
        # Keep enough tail that the client can reconstruct every step (header +
        # output + footer) for the parsed Summary view, not just the last few lines.
        log_tail = list(_sync["log"])[-200:]
    return jsonify(
        running=snap["running"],
        job=snap["job"],
        step=snap["step"],
        returncode=snap["returncode"],
        started_at=snap["started_at"],
        finished_at=snap["finished_at"],
        media_ready_at=snap["media_ready_at"],
        auth_hint=snap["auth_hint"],
        log=log_tail,
    )


@app.post("/api/subtitles")
def api_subtitles() -> Response:
    if not _whisper_url():
        return jsonify(ok=False, error="No Whisper server URL configured."), 400
    if start_subtitles():
        return jsonify(ok=True)
    return jsonify(ok=False, error="A job is already running."), 409


@app.get("/api/settings")
def api_settings_get() -> Response:
    settings = _load_settings()
    return jsonify(
        whisper_server_url=_whisper_url(),
        whisper_configured=bool(_whisper_url()),
        whisper_env_locked=bool(WHISPER_ENV),
        burn_subtitles=bool(settings.get("burn_subtitles")),
        autonomous_mode=bool(settings.get("autonomous_mode")),
        subtitle_font=str(settings.get("subtitle_font") or "system"),
        subtitle_size=_sub_size(settings.get("subtitle_size")),
        subtitle_color=_sub_color(settings.get("subtitle_color")),
        subtitle_bg_opacity=_sub_opacity(settings.get("subtitle_bg_opacity")),
        embed_server_url=_embed_url(),
        embed_model=_embed_model(),
        embed_configured=bool(_embed_url()),
        embed_env_locked=bool(EMBED_ENV),
        embed_model_env_locked=bool(EMBED_MODEL_ENV),
        embed_provider=str(settings.get("embed_provider") or _provider_from_url(_embed_url()) or "local"),
        embed_api_key_configured=bool(_embed_api_key()),
        embed_api_key_env_locked=_embed_api_key_env_locked(),
        llm_server_url=_llm_url(),
        llm_model=_llm_model(),
        llm_vision_model=(LLM_VISION_MODEL_ENV or str(settings.get("llm_vision_model", "")).strip()),
        llm_vision_model_env_locked=bool(LLM_VISION_MODEL_ENV),
        llm_configured=bool(_llm_url()),
        llm_env_locked=bool(LLM_ENV),
        llm_model_env_locked=bool(LLM_MODEL_ENV),
        llm_provider=str(settings.get("llm_provider") or _provider_from_url(_llm_url()) or "local"),
        llm_api_key_configured=bool(_llm_api_key()),
        llm_api_key_env_locked=_llm_api_key_env_locked(),
        xai_api_key_configured=bool(_xai_api_key()),
        xai_api_key_env_locked=_xai_api_key_env_locked(),
        xai_image_model=_xai_image_model(),
        xai_image_model_env_locked=bool(XAI_IMAGE_MODEL_ENV),
        xai_video_model=_xai_video_model(),
        xai_video_model_env_locked=bool(XAI_VIDEO_MODEL_ENV),
        xai_image_resolution=_xai_image_resolution(),
        xai_image_aspect_ratio=_xai_image_aspect(),
        xai_video_resolution=_xai_video_resolution(),
        xai_video_aspect_ratio=_xai_video_aspect(),
        xai_video_duration=_xai_video_duration(),
    )


@app.post("/api/settings")
def api_settings_post() -> Response:
    payload = request.get_json(silent=True) or {}
    # Strict load so a corrupt settings.json doesn't get silently rewritten as {},
    # dropping whichever key this request didn't include.
    try:
        loaded = _load_json_strict(SETTINGS_FILE, {})
    except CorruptStateError as exc:
        return jsonify(ok=False, error=str(exc)), 503
    settings = loaded if isinstance(loaded, dict) else {}
    if not WHISPER_ENV and "whisper_server_url" in payload:
        settings["whisper_server_url"] = str(payload.get("whisper_server_url") or "").strip()[:500]
    if "burn_subtitles" in payload:
        settings["burn_subtitles"] = bool(payload.get("burn_subtitles"))
    if "autonomous_mode" in payload:
        settings["autonomous_mode"] = bool(payload.get("autonomous_mode"))
    # Subtitle display style (player ::cue + burned-in export). Font is restricted
    # to the curated keys; size/colour/opacity are clamped to safe ranges.
    if "subtitle_font" in payload:
        font = str(payload.get("subtitle_font") or "").strip()
        if font in _SUB_FONT_LIBASS:
            settings["subtitle_font"] = font
    if "subtitle_size" in payload:
        settings["subtitle_size"] = _sub_size(payload.get("subtitle_size"))
    if "subtitle_color" in payload:
        settings["subtitle_color"] = _sub_color(payload.get("subtitle_color"))
    if "subtitle_bg_opacity" in payload:
        settings["subtitle_bg_opacity"] = _sub_opacity(payload.get("subtitle_bg_opacity"))
    # Prompt Studio endpoints — only writable when not pinned by an env var.
    if not EMBED_ENV and "embed_server_url" in payload:
        settings["embed_server_url"] = str(payload.get("embed_server_url") or "").strip()[:500]
    if not EMBED_MODEL_ENV and "embed_model" in payload:
        settings["embed_model"] = str(payload.get("embed_model") or "").strip()[:120]
    if "embed_provider" in payload:
        settings["embed_provider"] = str(payload.get("embed_provider") or "").strip()[:40]
    if not _embed_api_key_env_locked():
        if str(payload.get("embed_api_key") or "").strip():
            settings["embed_api_key"] = str(payload.get("embed_api_key") or "").strip()[:500]
        elif payload.get("embed_api_key_clear"):
            settings.pop("embed_api_key", None)
    if not LLM_ENV and "llm_server_url" in payload:
        settings["llm_server_url"] = str(payload.get("llm_server_url") or "").strip()[:500]
    if not LLM_MODEL_ENV and "llm_model" in payload:
        settings["llm_model"] = str(payload.get("llm_model") or "").strip()[:120]
    if not LLM_VISION_MODEL_ENV and "llm_vision_model" in payload:
        settings["llm_vision_model"] = str(payload.get("llm_vision_model") or "").strip()[:120]
    if "llm_provider" in payload:
        settings["llm_provider"] = str(payload.get("llm_provider") or "").strip()[:40]
    if not _llm_api_key_env_locked():
        if str(payload.get("llm_api_key") or "").strip():
            settings["llm_api_key"] = str(payload.get("llm_api_key") or "").strip()[:500]
        elif payload.get("llm_api_key_clear"):
            settings.pop("llm_api_key", None)
    # Grok Imagine API (xAI) — key is write-only from the browser (same as llm_api_key);
    # the generation defaults are plain knobs.
    if not _xai_api_key_env_locked():
        if str(payload.get("xai_api_key") or "").strip():
            settings["xai_api_key"] = str(payload.get("xai_api_key") or "").strip()[:500]
        elif payload.get("xai_api_key_clear"):
            settings.pop("xai_api_key", None)
    if not XAI_IMAGE_MODEL_ENV and "xai_image_model" in payload:
        settings["xai_image_model"] = str(payload.get("xai_image_model") or "").strip()[:120]
    if not XAI_VIDEO_MODEL_ENV and "xai_video_model" in payload:
        settings["xai_video_model"] = str(payload.get("xai_video_model") or "").strip()[:120]
    if "xai_image_resolution" in payload:
        settings["xai_image_resolution"] = str(payload.get("xai_image_resolution") or "").strip()[:16]
    if "xai_image_aspect_ratio" in payload:
        settings["xai_image_aspect_ratio"] = str(payload.get("xai_image_aspect_ratio") or "").strip()[:16]
    if "xai_video_resolution" in payload:
        settings["xai_video_resolution"] = str(payload.get("xai_video_resolution") or "").strip()[:16]
    if "xai_video_aspect_ratio" in payload:
        settings["xai_video_aspect_ratio"] = str(payload.get("xai_video_aspect_ratio") or "").strip()[:16]
    if "xai_video_duration" in payload:
        try:
            settings["xai_video_duration"] = max(1, min(15, int(payload.get("xai_video_duration"))))
        except (TypeError, ValueError):
            pass
    _save_settings(settings)
    return jsonify(
        ok=True,
        whisper_configured=bool(_whisper_url()),
        burn_subtitles=bool(settings.get("burn_subtitles")),
        embed_configured=bool(_embed_url()),
        embed_api_key_configured=bool(_embed_api_key()),
        llm_configured=bool(_llm_url()),
        llm_api_key_configured=bool(_llm_api_key()),
        xai_api_key_configured=bool(_xai_api_key()),
    )


@app.post("/api/settings/models")
def api_settings_models() -> Response:
    payload = request.get_json(silent=True) or {}
    raw_kind = str(payload.get("kind") or "").strip().lower()
    kind = raw_kind if raw_kind in {"embed", "vision"} else "llm"
    provider = str(payload.get("provider") or "").strip().lower()
    # Vision shares the chat (LLM) endpoint/provider/key — it's just a different model.
    if kind == "embed":
        base_url = str(payload.get("url") or "").strip() or _embed_url()
        api_key = str(payload.get("api_key") or "").strip() or _embed_api_key()
    else:
        base_url = str(payload.get("url") or "").strip() or _llm_url()
        api_key = str(payload.get("api_key") or "").strip() or _llm_api_key()
    effective_provider = provider or _provider_from_url(base_url)
    # A URL is all that's required: a local Ollama lists its installed models via
    # /v1/models (and /api/tags for capabilities) just like a remote provider does — so
    # don't reject the "local" provider here (that was the "set a provider URL" bug).
    if not base_url:
        return jsonify(ok=False, error="Set a provider URL before loading models."), 400
    if effective_provider in {"openai", "openrouter"} and not api_key:
        return jsonify(ok=False, error=f"Add a {_provider_display(effective_provider)} API key before loading models."), 400
    try:
        if kind == "vision":
            # Prefer a true vision-only list from a local Ollama (its native /api/tags
            # reports per-model capabilities; the OpenAI /v1/models list doesn't). If the
            # endpoint isn't Ollama, fall back to the full chat-model list to pick from.
            models = None
            if effective_provider in {"local", "custom"}:
                try:
                    models = _fetch_ollama_vision_models(base_url)
                except Exception:
                    models = None
            if models is None:
                models = _fetch_provider_models(effective_provider, base_url, api_key, "llm")
                note = "Showing all chat models — pick a vision-capable one."
            else:
                note = "" if models else "No vision-capable models installed — pull one (e.g. a Qwen3-VL build)."
            return jsonify(ok=True, provider=effective_provider, kind=kind, models=models, note=note)
        models = _fetch_provider_models(effective_provider, base_url, api_key, kind)
    except urllib.error.HTTPError as exc:
        return jsonify(ok=False, error=f"Model list failed ({exc.code})."), 502
    except Exception as exc:
        return jsonify(ok=False, error=f"Model list failed: {exc}"), 502
    return jsonify(ok=True, provider=effective_provider, kind=kind, models=models)


@app.get("/api/config")
def api_config_get() -> Response:
    """Legacy single-account view — reports the 'default' slot (grok_auth.txt)."""
    src = CURL_FILE if CURL_FILE.exists() else LEGACY_CURL_FILE
    if src.exists():
        mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(src.stat().st_mtime))
        return jsonify(configured=True, mtime=mtime)
    return jsonify(configured=False)


@app.post("/api/config")
def api_config_post() -> Response:
    """Legacy single-account route: writes the 'default' account (grok_auth.txt) and
    makes sure it's registered, so older clients keep working alongside /api/accounts."""
    body = request.get_data(as_text=True) or ""
    err = _curl_error(body)
    if err:
        return jsonify(ok=False, error=err), 400
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _accounts_lock:
        _write_account_curl("default", body)
        accounts = _load_accounts()
        if not any(a["id"] == "default" for a in accounts):
            stamp = _utc_stamp()
            accounts.append({"id": "default", "name": "Account 1", "active": True,
                             "created_at": stamp, "updated_at": stamp})
            _save_accounts(accounts)
    return jsonify(ok=True)


def _account_summary(acct: dict) -> dict:
    """Public shape for one account: registry fields plus whether a session is saved
    (and when) — never the cURL itself, which is write-only like the API keys."""
    path = _account_curl_path(acct["id"])
    if acct["id"] == "default" and not path.exists() and LEGACY_CURL_FILE.exists():
        path = LEGACY_CURL_FILE
    configured = path.exists()
    mtime = ""
    if configured:
        try:
            mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime))
        except OSError:
            pass
    return {"id": acct["id"], "name": acct["name"], "active": acct["active"],
            "configured": configured, "mtime": mtime}


@app.get("/api/accounts")
def api_accounts_get() -> Response:
    return jsonify(accounts=[_account_summary(a) for a in _load_accounts()])


@app.post("/api/accounts")
def api_accounts_create() -> Response:
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()[:60]
    if not name:
        return jsonify(ok=False, error="Give the account a name."), 400
    curl = str(payload.get("curl") or "")
    err = _curl_error(curl)
    if err:
        return jsonify(ok=False, error=err), 400
    with _accounts_lock:
        accounts = _load_accounts()
        if len(accounts) >= MAX_ACCOUNTS:
            return jsonify(ok=False, error=f"Account limit reached ({MAX_ACCOUNTS})."), 400
        # The very first account takes the legacy 'default' slot (grok_auth.txt) so a
        # from-source CLI run keeps working with no flags; later ones get their own file.
        if not accounts and not _account_configured("default"):
            acct_id = "default"
        else:
            acct_id = secrets.token_hex(4)
            while any(a["id"] == acct_id for a in accounts):  # vanishingly unlikely
                acct_id = secrets.token_hex(4)
        stamp = _utc_stamp()
        acct = {"id": acct_id, "name": name, "active": bool(payload.get("active", True)),
                "created_at": stamp, "updated_at": stamp}
        _write_account_curl(acct_id, curl)
        accounts.append(acct)
        _save_accounts(accounts)
    return jsonify(ok=True, account=_account_summary(acct))


@app.post("/api/accounts/<acct_id>")
def api_accounts_update(acct_id: str) -> Response:
    """Partial update: any of name / active / curl. A blank curl keeps the saved session."""
    payload = request.get_json(silent=True) or {}
    curl = str(payload.get("curl") or "")
    if curl.strip():
        err = _curl_error(curl)
        if err:
            return jsonify(ok=False, error=err), 400
    with _accounts_lock:
        accounts = _load_accounts()
        acct = next((a for a in accounts if a["id"] == acct_id), None)
        if acct is None:
            return jsonify(ok=False, error="No such account."), 404
        if "name" in payload:
            name = str(payload.get("name") or "").strip()[:60]
            if not name:
                return jsonify(ok=False, error="Name can't be empty."), 400
            acct["name"] = name
        if "active" in payload:
            acct["active"] = bool(payload.get("active"))
        if curl.strip():
            _write_account_curl(acct_id, curl)
        acct["updated_at"] = _utc_stamp()
        _save_accounts(accounts)
    return jsonify(ok=True, account=_account_summary(acct))


@app.delete("/api/accounts/<acct_id>")
def api_accounts_delete(acct_id: str) -> Response:
    with _accounts_lock:
        accounts = _load_accounts()
        keep = [a for a in accounts if a["id"] != acct_id]
        if len(keep) == len(accounts):
            return jsonify(ok=False, error="No such account."), 404
        # The session file is the account's secret — it goes with the record.
        doomed = (CURL_FILE, LEGACY_CURL_FILE) if acct_id == "default" else (_account_curl_path(acct_id),)
        for path in doomed:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        _save_accounts(keep)
    return jsonify(ok=True)


# --------------------------------------------------------------------------- #
# Grok weekly usage (credits) — per-account quota readout for the top-bar bolts.
# Grok serves usage as a gRPC-Web unary RPC (proto-encoded, no JSON variant), so we
# frame the request and walk the response bytes by hand — a small, flat message.
# This mirrors the Firefox extension's decoder (firefox/content/grok.js); field
# numbers come from the service's proto descriptor, verified against live responses.
# --------------------------------------------------------------------------- #

# Overridable so tests can point at a local mock; production always uses grok.com.
GROK_CREDITS_URL = os.environ.get(
    "GROK_CREDITS_URL",
    "https://grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig",
)
# billing_product.Product enum -> (key, label). Imagine first in fallback ordering.
_CREDIT_PRODUCTS = {
    5: ("imagine", "Imagine"),
    4: ("chat", "Chat"),
    6: ("voice", "Voice"),
    2: ("build", "Build"),
    3: ("plugins", "Plugins"),
    1: ("api", "API"),
}
_CREDIT_PERIODS = {1: "monthly", 2: "weekly"}
QUOTA_TTL = 240  # seconds; weekly numbers move slowly (the extension polls at 5 min)
_quota_lock = threading.Lock()
_quota_cache: dict[str, tuple[float, dict]] = {}  # account id -> (fetched_at, entry)


def _pb_fields(data: bytes):
    """Minimal protobuf wire walker: yields (field, wire_type, value) — varint ->
    int, 64-bit -> float, length-delimited -> bytes, 32-bit -> float. Bails on
    malformed/unknown input instead of raising (a non-quota body decodes to nothing)."""
    i, n = 0, len(data)

    def varint() -> int:
        nonlocal i
        shift = result = 0
        while i < n:
            b = data[i]
            i += 1
            result |= (b & 0x7F) << shift
            if not b & 0x80:
                return result
            shift += 7
        raise ValueError("truncated varint")

    try:
        while i < n:
            tag = varint()
            field, wt = tag >> 3, tag & 7
            if wt == 0:
                yield field, wt, varint()
            elif wt == 1:
                if i + 8 > n:
                    return
                val = struct.unpack_from("<d", data, i)[0]
                i += 8
                yield field, wt, val
            elif wt == 2:
                ln = varint()
                if i + ln > n:
                    return
                val = data[i:i + ln]
                i += ln
                yield field, wt, val
            elif wt == 5:
                if i + 4 > n:
                    return
                val = struct.unpack_from("<f", data, i)[0]
                i += 4
                yield field, wt, val
            else:
                return
    except ValueError:
        return


def _pb_first_varint(msg: bytes) -> int:
    """Field 1 as a varint — covers both prod_charger.Cent {1: cents} and
    google.protobuf.Timestamp {1: seconds}."""
    return next((v for f, wt, v in _pb_fields(msg) if f == 1 and wt == 0), 0)


def _decode_credits_config(cfg: bytes) -> dict:
    """GrokCreditsConfig -> view model. Fields: 1 credit_usage_percent(float),
    2 on_demand_cap(Cent), 3 on_demand_used(Cent), 7 product_usage(repeated),
    8 current_period, 12 prepaid_balance(Cent)."""
    out = {
        "used_percent": None, "period_type": "unspecified", "reset_at": None,
        "products": [], "prepaid_cents": 0, "on_demand_cap_cents": 0, "on_demand_used_cents": 0,
    }
    for f, wt, v in _pb_fields(cfg):
        if f == 1 and wt == 5:
            out["used_percent"] = round(v, 2)
        elif f == 2 and wt == 2:
            out["on_demand_cap_cents"] = _pb_first_varint(v)
        elif f == 3 and wt == 2:
            out["on_demand_used_cents"] = _pb_first_varint(v)
        elif f == 7 and wt == 2:  # ProductUsage { 1: product, 2: usage_percent }
            product, pct = 0, 0.0
            for pf, pwt, pv in _pb_fields(v):
                if pf == 1 and pwt == 0:
                    product = pv
                elif pf == 2 and pwt == 5:
                    pct = pv
            meta = _CREDIT_PRODUCTS.get(product)
            if meta:
                out["products"].append({"key": meta[0], "label": meta[1], "percent": round(pct, 2)})
        elif f == 8 and wt == 2:  # UsagePeriod { 1: type, 3: end(Timestamp) }
            for uf, uwt, uv in _pb_fields(v):
                if uf == 1 and uwt == 0:
                    out["period_type"] = _CREDIT_PERIODS.get(uv, "unspecified")
                elif uf == 3 and uwt == 2:
                    out["reset_at"] = _pb_first_varint(uv) * 1000  # ms epoch
        elif f == 12 and wt == 2:
            out["prepaid_cents"] = _pb_first_varint(v)
    return out


def _decode_grpc_web(body: bytes) -> dict | None:
    """Split a gRPC-Web response (5-byte-prefixed frames) into the data frame and the
    trailer (flag 0x80, carries grpc-status); returns the decoded config or None."""
    i, message, status_ok = 0, None, True
    while i + 5 <= len(body):
        flag = body[i]
        ln = int.from_bytes(body[i + 1:i + 5], "big")
        i += 5
        payload = body[i:i + ln]
        i += ln
        if flag & 0x80:
            m = re.search(rb"grpc-status:\s*(\d+)", payload, re.IGNORECASE)
            if m and m.group(1) != b"0":
                status_ok = False
        elif not flag & 0x01:
            message = payload  # uncompressed data frame
    if not status_ok or message is None:
        return None
    cfg = next((v for f, wt, v in _pb_fields(message) if f == 1 and wt == 2), None)
    return _decode_credits_config(cfg) if cfg is not None else None


def _account_auth_spec(acct_id: str):
    """The account's parsed browser auth (headers + cookies) from its stored cURL,
    or None when there's no readable session."""
    path = _account_curl_path(acct_id)
    if acct_id == "default" and not path.exists():
        path = LEGACY_CURL_FILE
    if not path.exists():
        return None
    try:
        specs = gdownloader.parse_curl_samples(path)
        return gdownloader.choose_grok_auth_spec(specs)
    except (Exception, SystemExit):  # parse_curl_samples SystemExits on an empty file
        return None


def _fetch_account_quota(acct: dict) -> dict:
    """One account's weekly usage, shaped for the UI. Never raises: failures come
    back as {ok: False, error} — 'auth' marks a dead/expired session specifically."""
    base = {"id": acct["id"], "name": acct["name"]}
    spec = _account_auth_spec(acct["id"])
    if spec is None:
        return {**base, "ok": False, "error": "no-session"}
    # Reuse the captured browser headers (UA & co. keep Cloudflare happy) but swap
    # the content negotiation over to gRPC-Web proto. accept-encoding is dropped so
    # httpx negotiates only codings it can decode (a captured 'zstd' would arrive
    # as bytes we can't parse).
    headers = {k: v for k, v in spec.headers_with_cookies().items()
               if k.lower() not in ("content-type", "accept", "accept-encoding",
                                    "content-length", "x-grpc-web", "x-user-agent")}
    headers.update({
        "Content-Type": "application/grpc-web+proto",
        "Accept": "application/grpc-web+proto",
        "X-Grpc-Web": "1",
        "x-user-agent": "grpc-web-javascript/0.1",
    })
    try:
        # The request message sets no fields -> body is one empty frame.
        resp = httpx.post(GROK_CREDITS_URL, headers=headers,
                          content=b"\x00\x00\x00\x00\x00", timeout=15)
    except Exception:
        return {**base, "ok": False, "error": "network"}
    if resp.status_code in (401, 403):
        return {**base, "ok": False, "error": "auth"}
    if resp.status_code != 200:
        return {**base, "ok": False, "error": f"http-{resp.status_code}"}
    decoded = _decode_grpc_web(resp.content)
    if not decoded or not isinstance(decoded.get("used_percent"), (int, float)):
        return {**base, "ok": False, "error": "unavailable"}
    return {**base, "ok": True, **decoded}


@app.get("/api/accounts/quota")
def api_accounts_quota() -> Response:
    """Weekly usage for every ACTIVE account, cached ~4 min per account so the UI
    can poll freely; ?refresh=1 forces a refetch. Accounts are fetched concurrently."""
    refresh = request.args.get("refresh", "").strip().lower() in ("1", "true", "yes", "on")
    active = [a for a in _load_accounts() if a.get("active")]
    now = time.time()
    out: list = [None] * len(active)
    to_fetch: list[tuple[int, dict]] = []
    with _quota_lock:
        # Cache hygiene: drop entries for accounts that were deleted or paused.
        keep = {a["id"] for a in active}
        for stale in [k for k in _quota_cache if k not in keep]:
            _quota_cache.pop(stale, None)
        for idx, acct in enumerate(active):
            hit = _quota_cache.get(acct["id"])
            if not refresh and hit and now - hit[0] < QUOTA_TTL:
                out[idx] = {**hit[1], "name": acct["name"]}  # honour a rename
            else:
                to_fetch.append((idx, acct))
    if to_fetch:
        with ThreadPoolExecutor(max_workers=min(4, len(to_fetch))) as pool:
            fetched = list(pool.map(lambda pair: _fetch_account_quota(pair[1]), to_fetch))
        with _quota_lock:
            for (idx, acct), entry in zip(to_fetch, fetched):
                _quota_cache[acct["id"]] = (time.time(), entry)
                out[idx] = entry
    return jsonify(accounts=out, ttl=QUOTA_TTL)


@app.get("/api/playlists")
def api_playlists_get() -> Response:
    """Return the saved playlists ([] if none/unreadable). Stored on the data volume
    so they persist across container recreation and are shared by every client."""
    data: list = []
    if PLAYLISTS_FILE.exists():
        try:
            loaded = json.loads(PLAYLISTS_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                data = loaded
        except Exception:
            data = []
    return jsonify(playlists=data)


@app.post("/api/playlists")
def api_playlists_post() -> Response:
    """Replace the whole playlist collection. The client owns the list and sends it
    in full; we validate, normalise, and atomically write it."""
    payload = request.get_json(silent=True) or {}
    incoming = payload.get("playlists")
    if not isinstance(incoming, list):
        return jsonify(ok=False, error="Expected a 'playlists' array."), 400
    clean = []
    for entry in incoming[:500]:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()[:120]
        ids = entry.get("ids")
        if not name or not isinstance(ids, list):
            continue
        clean.append(
            {
                "id": str(entry.get("id") or "")[:64] or name,
                "name": name,
                "ids": [str(i) for i in ids][:2000],
                "created_at": str(entry.get("created_at", ""))[:32],
            }
        )
    _atomic_write_json(PLAYLISTS_FILE, clean)
    return jsonify(ok=True, count=len(clean))


# Upper bound for a media id reference stored in library.json / collections.json.
# Media ids can be the file's name stem (see reindex.py), and a filesystem allows
# up to 255-char names — manually-imported files (e.g. base64-URL names) routinely
# exceed the old 128 cap, which silently truncated the id so archive/collection
# membership never matched and the item could not leave Recent. 255 covers any
# real on-disk id while still bounding abuse.
MAX_MEDIA_ID_LEN = 255


def _collection_id_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    seen, out = set(), []
    for item in value[:100000]:
        key = str(item)[:MAX_MEDIA_ID_LEN]
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _clean_group_name(value) -> str:
    return str(value or "").strip()[:120]


def _clean_collection(entry: dict) -> dict | None:
    name = str(entry.get("name", "")).strip()[:120]
    ids = _collection_id_list(entry.get("ids"))
    if not name:
        return None
    # Full timestamp (not date-only) so "Recently updated" can order collections
    # touched on the same day; legacy date-only stamps still compare correctly.
    now = _utc_stamp()
    out = {
        "id": str(entry.get("id") or "")[:64] or name,
        "name": name,
        "ids": ids,
        "cover_id": str(entry.get("cover_id") or "")[:MAX_MEDIA_ID_LEN],
        "created_at": str(entry.get("created_at") or now)[:32],
        "updated_at": str(entry.get("updated_at") or now)[:32],
    }
    group = _clean_group_name(entry.get("group"))
    if group:
        out["group"] = group
    # Password-lock state rides through every write. It's server-authoritative: the
    # bulk /api/collections POST re-supplies it from disk, and the dedicated lock
    # endpoints set it — so a normal client save can never forge or clear a lock here.
    if entry.get("locked") and entry.get("pass_hash"):
        out["locked"] = True
        out["pass_hash"] = str(entry.get("pass_hash"))[:512]
        if entry.get("locked_at"):
            out["locked_at"] = str(entry.get("locked_at"))[:32]
    return out


def _load_collections(strict: bool = False) -> list[dict]:
    # strict=True (used by mutation paths that rewrite collections.json) raises on a
    # present-but-unreadable file instead of silently treating it as empty.
    data: list = []
    if COLLECTIONS_FILE.exists():
        try:
            loaded = json.loads(COLLECTIONS_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                data = loaded
        except Exception as exc:
            if strict:
                raise CorruptStateError(f"collections.json exists but could not be read: {exc}") from exc
            data = []
    clean: list[dict] = []
    for entry in data[:1000]:
        if isinstance(entry, dict):
            c = _clean_collection(entry)
            if c:
                clean.append(c)
    return clean


def _clean_group_record(entry: dict) -> dict | None:
    name = _clean_group_name(entry.get("name"))
    if not name:
        return None
    if not (entry.get("locked") and entry.get("pass_hash")):
        return None
    out = {
        "name": name,
        "locked": True,
        "pass_hash": str(entry.get("pass_hash"))[:512],
    }
    if entry.get("locked_at"):
        out["locked_at"] = str(entry.get("locked_at"))[:32]
    return out


def _load_groups(strict: bool = False) -> dict:
    data = {"seeded": False, "groups": []}
    if COLLECTION_GROUPS_FILE.exists():
        try:
            loaded = json.loads(COLLECTION_GROUPS_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception as exc:
            if strict:
                raise CorruptStateError(f"collection_groups.json exists but could not be read: {exc}") from exc
            data = {"seeded": False, "groups": []}
    groups = []
    seen = set()
    raw_groups = data.get("groups") if isinstance(data, dict) else []
    if isinstance(raw_groups, list):
        for entry in raw_groups[:1000]:
            if not isinstance(entry, dict):
                continue
            rec = _clean_group_record(entry)
            if not rec:
                continue
            key = rec["name"].casefold()
            if key in seen:
                continue
            seen.add(key)
            groups.append(rec)
    return {"seeded": bool(data.get("seeded")) if isinstance(data, dict) else False, "groups": groups}


def _save_groups(state: dict) -> None:
    clean = _load_groups_from_value(state)
    _atomic_write_json(COLLECTION_GROUPS_FILE, clean)


def _load_groups_from_value(value: dict) -> dict:
    if not isinstance(value, dict):
        value = {}
    groups = []
    seen = set()
    raw = value.get("groups")
    if isinstance(raw, list):
        for entry in raw[:1000]:
            if not isinstance(entry, dict):
                continue
            rec = _clean_group_record(entry)
            if not rec:
                continue
            key = rec["name"].casefold()
            if key in seen:
                continue
            seen.add(key)
            groups.append(rec)
    return {"seeded": bool(value.get("seeded")), "groups": groups}


def _group_record(name: str, state: dict | None = None) -> dict | None:
    target = _clean_group_name(name)
    if not target:
        return None
    data = state if state is not None else _load_groups()
    for rec in data.get("groups", []):
        if str(rec.get("name", "")).casefold() == target.casefold():
            return rec
    return None


def _canonical_group_name(name: str, collections: list[dict] | None = None, groups_state: dict | None = None) -> str:
    clean = _clean_group_name(name)
    if not clean:
        return ""
    key = clean.casefold()
    if groups_state is not None:
        rec = _group_record(clean, groups_state)
        if rec:
            return str(rec.get("name"))
    if collections is not None:
        for coll in collections:
            group = _clean_group_name(coll.get("group"))
            if group and group.casefold() == key:
                return group
    return clean


# --------------------------------------------------------------------------- #
# Collection locks — password-gate a collection so its media disappear from every
# listing (All Media / Archive / search / facets), the collection card's cover, and
# direct file/thumbnail URLs until unlocked. Unlock is per-browser-session and lasts
# 24 h. This gates ACCESS within the already-authenticated app — it is NOT encryption;
# the files stay on disk in the clear.
# --------------------------------------------------------------------------- #

UNLOCK_TTL = 24 * 3600  # seconds a successful unlock lasts (per session)
_locked_cache: dict = {"mtime": None, "collections": {}, "groups": {}, "group_collections": {}}
_locked_cache_lock = threading.Lock()


def _lock_mtime_key() -> tuple[float, float]:
    try:
        collections_mtime = COLLECTIONS_FILE.stat().st_mtime if COLLECTIONS_FILE.exists() else 0.0
    except OSError:
        collections_mtime = 0.0
    try:
        groups_mtime = COLLECTION_GROUPS_FILE.stat().st_mtime if COLLECTION_GROUPS_FILE.exists() else 0.0
    except OSError:
        groups_mtime = 0.0
    return collections_mtime, groups_mtime


def _locked_maps() -> tuple[dict[str, frozenset], dict[str, frozenset], dict[str, frozenset]]:
    """Return cached collection locks, group media locks, and group member-cid locks."""
    mtime = _lock_mtime_key()
    with _locked_cache_lock:
        if _locked_cache["mtime"] == mtime:
            return (_locked_cache["collections"],
                    _locked_cache["groups"],
                    _locked_cache["group_collections"])
    collections = _load_collections()
    collection_locks: dict[str, frozenset] = {}
    for coll in collections:
        if coll.get("locked") and coll.get("pass_hash"):
            collection_locks[str(coll.get("id"))] = frozenset(str(i) for i in coll.get("ids", []))
    lock_records = {str(rec.get("name")): rec for rec in _load_groups().get("groups", [])
                    if rec.get("locked") and rec.get("pass_hash")}
    group_locks: dict[str, frozenset] = {}
    group_collections: dict[str, frozenset] = {}
    for name in lock_records:
        ids: set[str] = set()
        cids: set[str] = set()
        for coll in collections:
            if _clean_group_name(coll.get("group")).casefold() == name.casefold():
                cids.add(str(coll.get("id")))
                ids.update(str(i) for i in coll.get("ids", []))
        group_locks[name] = frozenset(ids)
        group_collections[name] = frozenset(cids)
    with _locked_cache_lock:
        _locked_cache["mtime"] = mtime
        _locked_cache["collections"] = collection_locks
        _locked_cache["groups"] = group_locks
        _locked_cache["group_collections"] = group_collections
    return collection_locks, group_locks, group_collections


def _locked_collections_map() -> dict[str, frozenset]:
    """{collection_id: frozenset(media_ids)} for every password-locked collection."""
    return _locked_maps()[0]


def _locked_groups_map() -> dict[str, frozenset]:
    """{group_name: frozenset(media_ids of ALL member collections)} for locked groups."""
    return _locked_maps()[1]


def _locked_group_collections_map() -> dict[str, frozenset]:
    """{group_name: frozenset(collection_ids)} for locked groups."""
    return _locked_maps()[2]


def _session_unlocked(prune: bool = True) -> set[str]:
    """Collection ids unlocked in THIS session, expired grants dropped. ``prune`` is
    False on the hot file-serving path so it never rewrites the session cookie."""
    grants = session.get("unlocked")
    if not isinstance(grants, dict) or not grants:
        return set()
    now = time.time()
    valid = {cid: exp for cid, exp in grants.items() if isinstance(exp, (int, float)) and exp > now}
    if prune and len(valid) != len(grants):
        session["unlocked"] = valid
    return set(valid)


def _session_unlocked_groups(prune: bool = True) -> set[str]:
    """Group names unlocked in THIS session. Older grants are invalidated when a
    group is re-locked/re-passworded by comparing the grant's locked_at snapshot."""
    grants = session.get("unlocked_groups")
    if not isinstance(grants, dict) or not grants:
        return set()
    now = time.time()
    records = {str(rec.get("name")): rec for rec in _load_groups().get("groups", [])}
    valid = {}
    out = set()
    for name, grant in grants.items():
        exp = None
        locked_at = None
        if isinstance(grant, dict):
            exp = grant.get("expires")
            locked_at = grant.get("locked_at")
        elif isinstance(grant, (int, float)):
            exp = grant
        if not isinstance(exp, (int, float)) or exp <= now:
            continue
        rec = next((r for n, r in records.items() if n.casefold() == str(name).casefold()), None)
        if rec and locked_at is not None and locked_at != rec.get("locked_at"):
            continue
        canonical = str(rec.get("name")) if rec else str(name)
        valid[canonical] = {"expires": exp, "locked_at": rec.get("locked_at") if rec else locked_at}
        out.add(canonical)
    if prune and valid != grants:
        session["unlocked_groups"] = valid
    return out


def _grant_unlock(cid: str) -> None:
    grants = dict(session.get("unlocked") or {})
    grants[str(cid)] = time.time() + UNLOCK_TTL
    session["unlocked"] = grants
    session.permanent = True


def _grant_group_unlock(name: str) -> None:
    clean = _clean_group_name(name)
    if not clean:
        return
    rec = _group_record(clean)
    canonical = str(rec.get("name")) if rec else clean
    grants = dict(session.get("unlocked_groups") or {})
    grants[canonical] = {"expires": time.time() + UNLOCK_TTL, "locked_at": rec.get("locked_at") if rec else None}
    session["unlocked_groups"] = grants
    session.permanent = True


def _drop_unlock(cid: str) -> None:
    grants = dict(session.get("unlocked") or {})
    if grants.pop(str(cid), None) is not None:
        session["unlocked"] = grants


def _drop_group_unlock(name: str) -> None:
    clean = _clean_group_name(name)
    grants = dict(session.get("unlocked_groups") or {})
    removed = False
    for key in list(grants.keys()):
        if str(key).casefold() == clean.casefold():
            grants.pop(key, None)
            removed = True
    if removed:
        session["unlocked_groups"] = grants


def _hidden_media_ids(reveal_ids=None) -> set[str]:
    """Media ids the session must not see: every locked collection's media EXCEPT collections
    unlocked this session. Used by the per-id / file paths (by-ids, related, /media, /thumbnails)
    where unlocking grants file access, AND by global browse (All Media / facets / search) —
    unlocking a collection surfaces its media in the library too, while a still-locked
    collection's media stays hidden entirely (no rows, no placeholder). ``reveal_ids`` (the ids
    of a collection the request is scoped to) are exempted so that collection's own grid still
    shows items that also live in another, still-locked collection. Requires a request/session
    context."""
    locked = _locked_collections_map()
    locked_groups = _locked_groups_map()
    if not locked and not locked_groups:
        return set()
    unlocked = _session_unlocked()
    unlocked_groups = _session_unlocked_groups()
    out: set[str] = set()
    for cid, ids in locked.items():
        if cid not in unlocked:
            out |= ids
    for name, ids in locked_groups.items():
        if name not in unlocked_groups:
            out |= ids
    if reveal_ids:
        out -= {str(i) for i in reveal_ids}
    return out


def _list_hidden_media_ids() -> set[str]:
    """Session-AGNOSTIC hidden set: EVERY locked collection's media, whether or not it's
    unlocked this session. Used by surfaces that must not depend on a session (and may run
    outside a request context): the Prompt Studio discovery views (themes / find-similar) and
    the background library-prompt import. All Media / facets / search instead use the
    session-aware _hidden_media_ids(), so unlocking a collection surfaces its media there."""
    locked = _locked_collections_map()
    locked_groups = _locked_groups_map()
    if not locked and not locked_groups:
        return set()
    out: set[str] = set()
    for ids in locked.values():
        out |= ids
    for ids in locked_groups.values():
        out |= ids
    return out


def _is_media_hidden(media_id: str) -> bool:
    """Per-file membership check for the /media + /thumbnails gates (no session write)."""
    locked = _locked_collections_map()
    locked_groups = _locked_groups_map()
    if not locked and not locked_groups:
        return False
    unlocked = _session_unlocked(prune=False)
    unlocked_groups = _session_unlocked_groups(prune=False)
    mid = str(media_id)
    return (any(cid not in unlocked and mid in ids for cid, ids in locked.items())
            or any(name not in unlocked_groups and mid in ids for name, ids in locked_groups.items()))


def _collection_summaries(collections: list[dict]) -> list[dict]:
    if not DB_FILE.exists():
        rebuild_db(wait=True)
    unlocked = _session_unlocked()
    grants = session.get("unlocked") or {}
    summaries = []
    for coll in collections:
        cid = str(coll.get("id"))
        is_locked = bool(coll.get("locked") and coll.get("pass_hash"))
        is_unlocked = cid in unlocked
        media = db.media_by_ids(DB_FILE, coll.get("ids", []))
        # Never leak the password hash to the client.
        base = {k: v for k, v in coll.items() if k != "pass_hash"}
        if is_locked and not is_unlocked:
            # Sealed card: size counts only — no thumbnails, no member ids.
            summaries.append({
                **base, "ids": [], "cover_id": "", "cover": None, "covers": [],
                "cover_items": [], "cover_peek": None,
                "item_count": len(media),
                "video_count": sum(1 for it in media if it.get("media_type") == "video"),
                "image_count": sum(1 for it in media if it.get("media_type") == "image"),
                "locked": True, "unlocked": False, "unlock_expires": None,
            })
            continue
        ids = [it["id"] for it in media]
        # Covers reflect the collection's MOST-RECENT content, not the first items
        # ever added: media_by_ids preserves insertion order (oldest-first), so sort
        # by created_at desc just for the cover/mosaic. `ids` stays in insertion order
        # (it drives playback / montage). An explicitly-set cover_id still wins.
        recent = sorted(media, key=lambda it: it.get("created_at") or "", reverse=True)
        cover_id = coll.get("cover_id") if coll.get("cover_id") in ids else (recent[0]["id"] if recent else "")
        cover_item = next((it for it in media if it["id"] == cover_id), None)
        # Long-press peek needs the full-media href behind each cover thumb.
        # `cover_items` mirrors `covers` (same items, same order); `cover_peek` is the
        # explicit/primary cover, which may be older than the recent-4 mosaic.
        peek = lambda it: {"thumb": it.get("thumb"), "href": it.get("href"), "media_type": it.get("media_type")}
        cover_pool = [it for it in recent if it.get("thumb")][:4]
        covers = [it.get("thumb") for it in cover_pool]
        videos = sum(1 for it in media if it.get("media_type") == "video")
        images = sum(1 for it in media if it.get("media_type") == "image")
        summaries.append({
            **base,
            "ids": ids,
            "cover_id": cover_id,
            "cover": cover_item.get("thumb") if cover_item else (covers[0] if covers else None),
            "covers": covers,
            "cover_items": [peek(it) for it in cover_pool],
            "cover_peek": peek(cover_item) if cover_item else (peek(cover_pool[0]) if cover_pool else None),
            "item_count": len(media),
            "video_count": videos,
            "image_count": images,
            "locked": is_locked,
            "unlocked": is_unlocked if is_locked else False,
            "unlock_expires": (grants.get(cid) if (is_locked and is_unlocked) else None),
        })
    return summaries


def _sealed_group_names() -> set[str]:
    unlocked = _session_unlocked_groups()
    return {name for name in _locked_groups_map() if name not in unlocked}


def _sealed_group_collection_ids() -> set[str]:
    sealed = {name.casefold() for name in _sealed_group_names()}
    out: set[str] = set()
    for name, cids in _locked_group_collections_map().items():
        if name.casefold() in sealed:
            out.update(cids)
    return out


def _collection_group_summaries(collections: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for coll in collections:
        group = _clean_group_name(coll.get("group"))
        if group:
            rec = _group_record(group)
            name = str(rec.get("name")) if rec else group
            counts[name] = counts.get(name, 0) + 1
    grants = session.get("unlocked_groups") or {}
    unlocked = _session_unlocked_groups()
    out = []
    for rec in _load_groups().get("groups", []):
        name = str(rec.get("name"))
        if not (rec.get("locked") and rec.get("pass_hash")):
            continue
        grant = grants.get(name)
        expires = grant.get("expires") if isinstance(grant, dict) else (grant if isinstance(grant, (int, float)) else None)
        out.append({
            "name": name,
            "locked": True,
            "unlocked": name in unlocked,
            "collection_count": counts.get(name, 0),
            "unlock_expires": expires if name in unlocked else None,
        })
    return out


@app.get("/api/collections")
def api_collections_get() -> Response:
    """Return saved mixed-media collections with lightweight display summaries."""
    collections = _load_collections()
    sealed_cids = _sealed_group_collection_ids()
    visible = [c for c in collections if str(c.get("id")) not in sealed_cids]
    return jsonify(collections=_collection_summaries(visible), groups=_collection_group_summaries(collections))


@app.post("/api/collections")
def api_collections_post() -> Response:
    """Replace the whole collection list, mirroring the playlist persistence model."""
    payload = request.get_json(silent=True) or {}
    incoming = payload.get("collections")
    if not isinstance(incoming, list):
        return jsonify(ok=False, error="Expected a 'collections' array."), 400
    # Lock state is server-authoritative: a bulk client save can never set or clear it.
    # CRITICAL: a SEALED collection (locked + not unlocked this session) has its ids and
    # cover suppressed in the summary the client holds, so the client's copy is a hollow
    # placeholder. For such a collection we keep the ENTIRE server record and ignore the
    # client's version — otherwise a routine save (e.g. editing some other collection)
    # would write the empty ids back and silently wipe it. A collection unlocked in this
    # session keeps its lock fields server-side but may have its (real, client-held) ids
    # edited. Unlocked/normal collections are handled as before.
    existing_list = _load_collections()
    existing = {str(c.get("id")): c for c in existing_list}
    unlocked = _session_unlocked()
    sealed_group_names = {name.casefold() for name in _sealed_group_names()}
    sealed_group_cids = _sealed_group_collection_ids()
    clean = []
    seen_payload_ids: set[str] = set()
    for entry in incoming[:1000]:
        if not isinstance(entry, dict):
            continue
        cid = str(entry.get("id") or entry.get("name") or "")[:64]
        if cid:
            seen_payload_ids.add(cid)
        prior = existing.get(cid)
        incoming_group = _clean_group_name(entry.get("group"))
        prior_group = _clean_group_name(prior.get("group")) if prior else ""
        incoming_group_key = incoming_group.casefold()
        prior_group_key = prior_group.casefold()
        if ((prior_group_key in sealed_group_names and incoming_group_key != prior_group_key)
                or (incoming_group_key in sealed_group_names and incoming_group_key != prior_group_key)):
            return jsonify(ok=False, error="Unlock the collection group before changing its members."), 403
        if incoming_group:
            entry = {**entry, "group": _canonical_group_name(incoming_group, existing_list, _load_groups())}
        if prior and cid in sealed_group_cids:
            entry = prior
        elif prior and prior.get("locked") and prior.get("pass_hash"):
            if cid in unlocked:
                entry = {**entry, "locked": True, "pass_hash": prior["pass_hash"], "locked_at": prior.get("locked_at")}
            else:
                entry = prior  # sealed for this client — trust nothing it sends
        else:
            entry = {k: v for k, v in entry.items() if k not in ("locked", "pass_hash", "locked_at")}
        coll = _clean_collection(entry)
        if coll:
            clean.append(coll)
    reinjected = [c for c in existing_list
                  if str(c.get("id")) in sealed_group_cids and str(c.get("id")) not in seen_payload_ids]
    if len(clean) + len(reinjected) > 1000:
        return jsonify(ok=False, error="Too many collections to preserve hidden group members safely."), 400
    clean.extend(reinjected)
    _atomic_write_json(COLLECTIONS_FILE, clean)
    return jsonify(ok=True, count=len(clean))


@app.post("/api/collections/<cid>/lock")
def api_collection_lock(cid: str) -> Response:
    """Set (or change) a collection's password and lock it. Allowed only when the
    collection is currently accessible (not locked, or already unlocked this session),
    so a locked collection can't be silently re-passworded by someone without access."""
    pw = str((request.get_json(silent=True) or {}).get("password") or "")
    if not pw:
        return jsonify(ok=False, error="A password is required."), 400
    collections = _load_collections(strict=True)
    coll = next((c for c in collections if str(c.get("id")) == cid), None)
    if coll is None:
        return jsonify(ok=False, error="Collection not found."), 404
    if (coll.get("locked") and coll.get("pass_hash")) and cid not in _session_unlocked():
        return jsonify(ok=False, error="Unlock the collection before changing its password."), 403
    coll["locked"] = True
    coll["pass_hash"] = generate_password_hash(pw)
    coll["locked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _atomic_write_json(COLLECTIONS_FILE, [c for c in (_clean_collection(c) for c in collections) if c])
    _drop_unlock(cid)  # locking takes effect immediately
    return jsonify(ok=True, locked=True)


@app.post("/api/collections/<cid>/unlock")
def api_collection_unlock(cid: str) -> Response:
    """Verify a collection's password; on success unlock it for this session for 24 h."""
    ip = request.remote_addr or "?"
    wait = _login_retry_after(ip)
    if wait:
        return jsonify(ok=False, error=f"Too many attempts. Try again in {wait} s."), 429
    pw = str((request.get_json(silent=True) or {}).get("password") or "")
    coll = next((c for c in _load_collections() if str(c.get("id")) == cid), None)
    if coll is None or not (coll.get("locked") and coll.get("pass_hash")):
        return jsonify(ok=False, error="Collection is not locked."), 400
    if not check_password_hash(coll["pass_hash"], pw):
        _record_login_fail(ip)
        return jsonify(ok=False, error="Incorrect password."), 403
    _clear_login_fails(ip)
    _grant_unlock(cid)
    return jsonify(ok=True, unlocked=True, expires=(session.get("unlocked") or {}).get(cid))


@app.post("/api/collections/<cid>/relock")
def api_collection_relock(cid: str) -> Response:
    """Re-lock now: revoke this session's unlock grant for the collection."""
    _drop_unlock(cid)
    return jsonify(ok=True, locked=True)


@app.post("/api/collections/relock-all")
def api_collections_relock_all() -> Response:
    """Panic re-lock: revoke every active unlock grant in this session."""
    session["unlocked"] = {}
    session["unlocked_groups"] = {}
    return jsonify(ok=True)


@app.post("/api/collections/unlock-all")
def api_collections_unlock_all() -> Response:
    """Bulk unlock: try one password against EVERY locked collection and grant a 24 h
    session unlock to each that matches. Collections locked with a different password
    stay sealed. Shares the single-unlock brute-force limiter so it can't be used to
    bypass it; a run that matches nothing counts as one failed attempt."""
    ip = request.remote_addr or "?"
    wait = _login_retry_after(ip)
    if wait:
        return jsonify(ok=False, error=f"Too many attempts. Try again in {wait} s."), 429
    pw = str((request.get_json(silent=True) or {}).get("password") or "")
    if not pw:
        return jsonify(ok=False, error="A password is required."), 400
    matched = 0
    for coll in _load_collections():
        if coll.get("locked") and coll.get("pass_hash") and check_password_hash(coll["pass_hash"], pw):
            _grant_unlock(str(coll.get("id")))
            matched += 1
    for rec in _load_groups().get("groups", []):
        if rec.get("locked") and rec.get("pass_hash") and check_password_hash(rec["pass_hash"], pw):
            _grant_group_unlock(str(rec.get("name")))
            matched += 1
    if not matched:
        _record_login_fail(ip)
        return jsonify(ok=False, error="No collections match that password."), 403
    _clear_login_fails(ip)
    return jsonify(ok=True, unlocked=matched)


def _group_member_count(name: str, collections: list[dict] | None = None) -> int:
    target = _clean_group_name(name).casefold()
    if not target:
        return 0
    return sum(1 for c in (collections if collections is not None else _load_collections())
               if _clean_group_name(c.get("group")).casefold() == target)


def _group_unlock_expires(name: str):
    grants = session.get("unlocked_groups") or {}
    for key, grant in grants.items():
        if str(key).casefold() != _clean_group_name(name).casefold():
            continue
        if isinstance(grant, dict):
            return grant.get("expires")
        if isinstance(grant, (int, float)):
            return grant
    return None


@app.post("/api/collections/groups/lock")
def api_collection_group_lock() -> Response:
    payload = request.get_json(silent=True) or {}
    name = _clean_group_name(payload.get("name"))
    pw = str(payload.get("password") or "")
    if not name:
        return jsonify(ok=False, error="A group name is required."), 400
    if not pw:
        return jsonify(ok=False, error="A password is required."), 400
    collections = _load_collections(strict=True)
    state = _load_groups(strict=True)
    rec = _group_record(name, state)
    canonical = _canonical_group_name(name, collections, state)
    if _group_member_count(canonical, collections) == 0 and rec is None:
        return jsonify(ok=False, error="Collection group not found."), 404
    if rec and rec.get("locked") and rec.get("pass_hash") and canonical not in _session_unlocked_groups():
        return jsonify(ok=False, error="Unlock the collection group before changing its password."), 403
    record = {
        "name": canonical,
        "locked": True,
        "pass_hash": generate_password_hash(pw),
        "locked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    groups = [g for g in state.get("groups", []) if str(g.get("name", "")).casefold() != canonical.casefold()]
    groups.append(record)
    _save_groups({"seeded": bool(state.get("seeded")), "groups": groups})
    _drop_group_unlock(canonical)
    return jsonify(ok=True, locked=True)


@app.post("/api/collections/groups/unlock")
def api_collection_group_unlock() -> Response:
    ip = request.remote_addr or "?"
    wait = _login_retry_after(ip)
    if wait:
        return jsonify(ok=False, error=f"Too many attempts. Try again in {wait} s."), 429
    payload = request.get_json(silent=True) or {}
    name = _clean_group_name(payload.get("name"))
    pw = str(payload.get("password") or "")
    rec = _group_record(name)
    if rec is None or not (rec.get("locked") and rec.get("pass_hash")):
        return jsonify(ok=False, error="Collection group is not locked."), 400
    if not check_password_hash(rec["pass_hash"], pw):
        _record_login_fail(ip)
        return jsonify(ok=False, error="Incorrect password."), 403
    _clear_login_fails(ip)
    _grant_group_unlock(str(rec.get("name")))
    return jsonify(ok=True, unlocked=True, expires=_group_unlock_expires(str(rec.get("name"))))


@app.post("/api/collections/groups/relock")
def api_collection_group_relock() -> Response:
    name = _clean_group_name((request.get_json(silent=True) or {}).get("name"))
    _drop_group_unlock(name)
    return jsonify(ok=True, locked=True)


@app.post("/api/collections/groups/remove-lock")
def api_collection_group_remove_lock() -> Response:
    payload = request.get_json(silent=True) or {}
    name = _clean_group_name(payload.get("name"))
    pw = str(payload.get("password") or "")
    state = _load_groups(strict=True)
    rec = _group_record(name, state)
    if rec is None:
        return jsonify(ok=False, error="Collection group is not locked."), 400
    canonical = str(rec.get("name"))
    if canonical not in _session_unlocked_groups() and not check_password_hash(rec["pass_hash"], pw):
        return jsonify(ok=False, error="Incorrect password."), 403
    groups = [g for g in state.get("groups", []) if str(g.get("name", "")).casefold() != canonical.casefold()]
    _save_groups({"seeded": bool(state.get("seeded")), "groups": groups})
    _drop_group_unlock(canonical)
    return jsonify(ok=True, locked=False)


@app.post("/api/collections/groups/force-unlock")
def api_collection_group_force_unlock() -> Response:
    if not _admin_configured():
        return jsonify(ok=False, error="No admin password is configured for recovery."), 400
    ip = request.remote_addr or "?"
    wait = _login_retry_after(ip)
    if wait:
        return jsonify(ok=False, error=f"Too many attempts. Try again in {wait} s."), 429
    payload = request.get_json(silent=True) or {}
    if not secrets.compare_digest(str(payload.get("admin_password") or ""), ADMIN_PASSWORD):
        _record_login_fail(ip)
        return jsonify(ok=False, error="Incorrect admin password."), 403
    name = _clean_group_name(payload.get("name"))
    rec = _group_record(name)
    if rec is None and _group_member_count(name) == 0:
        return jsonify(ok=False, error="Collection group not found."), 404
    _clear_login_fails(ip)
    _grant_group_unlock(str(rec.get("name")) if rec else name)
    return jsonify(ok=True, unlocked=True, expires=_group_unlock_expires(str(rec.get("name")) if rec else name))


@app.post("/api/collections/<cid>/remove-lock")
def api_collection_remove_lock(cid: str) -> Response:
    """Remove password protection entirely. Requires the collection's current password
    (forgotten? use force-unlock with the admin password, then remove)."""
    pw = str((request.get_json(silent=True) or {}).get("password") or "")
    collections = _load_collections(strict=True)
    coll = next((c for c in collections if str(c.get("id")) == cid), None)
    if coll is None:
        return jsonify(ok=False, error="Collection not found."), 404
    # Already unlocked in this session (incl. via admin force-unlock) ⇒ no need to retype
    # the password to remove protection; otherwise the current password is required.
    if coll.get("locked") and coll.get("pass_hash"):
        if cid not in _session_unlocked() and not check_password_hash(coll["pass_hash"], pw):
            return jsonify(ok=False, error="Incorrect password."), 403
    for k in ("locked", "pass_hash", "locked_at"):
        coll.pop(k, None)
    _atomic_write_json(COLLECTIONS_FILE, [c for c in (_clean_collection(c) for c in collections) if c])
    _drop_unlock(cid)
    return jsonify(ok=True, locked=False)


@app.post("/api/collections/<cid>/force-unlock")
def api_collection_force_unlock(cid: str) -> Response:
    """Recovery for a forgotten collection password: re-enter the MAIN admin password to
    unlock the collection for this session. Re-asking for the master secret means an
    over-the-shoulder viewer on a logged-in session still can't bypass a collection lock."""
    if not _admin_configured():
        return jsonify(ok=False, error="No admin password is configured for recovery."), 400
    ip = request.remote_addr or "?"
    wait = _login_retry_after(ip)
    if wait:
        return jsonify(ok=False, error=f"Too many attempts. Try again in {wait} s."), 429
    if not secrets.compare_digest(str((request.get_json(silent=True) or {}).get("admin_password") or ""), ADMIN_PASSWORD):
        _record_login_fail(ip)
        return jsonify(ok=False, error="Incorrect admin password."), 403
    _clear_login_fails(ip)
    if not any(str(c.get("id")) == cid for c in _load_collections()):
        return jsonify(ok=False, error="Collection not found."), 404
    _grant_unlock(cid)
    return jsonify(ok=True, unlocked=True, expires=(session.get("unlocked") or {}).get(cid))


# --------------------------------------------------------------------------- #
# Folder Import — upload a local folder of videos/images into a new collection
# --------------------------------------------------------------------------- #
# The browser reads a chosen folder (webkitdirectory) and POSTs each file to
# /api/import/file, which streams it into the gallery, makes a thumbnail and buffers
# a metadata record under an import id. /api/import/commit then writes all records at
# once, creates a new collection named after the top folder and rebuilds the index.
# /api/import/cancel discards a buffered, never-committed run. Each file gets a fresh
# random id (never the original filename), so duplicate names across imports can't
# collide on disk or in the index.

_IMPORT_VIDEO_EXT = {"mp4", "webm", "m4v", "mov"}
_IMPORT_IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "gif", "avif"}
_imports_lock = threading.Lock()
_imports: dict[str, list[dict]] = {}   # import_id -> buffered records (with disk paths)
_IMPORT_MAX = 5000                      # per-import file ceiling (runaway guard)


def _import_kind(ext: str) -> str | None:
    ext = ext.lower().lstrip(".")
    if ext in _IMPORT_VIDEO_EXT:
        return "video"
    if ext in _IMPORT_IMAGE_EXT:
        return "image"
    return None


@app.post("/api/import/file")
def api_import_file() -> Response:
    """Stream one uploaded media file into the gallery and buffer its record under
    ``import_id``. Multipart: ``import_id``, ``file``, optional ``rel`` (the file's
    path within the picked folder, for display) and ``mtime`` (ms epoch)."""
    import_id = str(request.form.get("import_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", import_id):
        return jsonify(ok=False, error="Bad import id."), 400
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify(ok=False, error="No file uploaded."), 400
    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    kind = _import_kind(ext)
    if kind is None:
        print(f"import: skipped '{f.filename}' — unsupported type .{ext[:10]}")
        return jsonify(ok=False, error=f"Unsupported file type: .{ext[:10]}"), 415
    if ext == "jpeg":
        ext = "jpg"
    with _imports_lock:
        if len(_imports.get(import_id, [])) >= _IMPORT_MAX:
            return jsonify(ok=False, error="Too many files in one import."), 413

    mid = "import_" + secrets.token_hex(8)
    sub = "videos" if kind == "video" else "images"
    rel = f"media/{sub}/{media_shard(mid)}/{mid}.{ext}"
    dest = GALLERY_DIR / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    f.save(str(dest))  # streams to disk — never reads the (up to 2 GB) file into memory

    try:
        thumb_dest = thumbgen.thumb_path({"id": mid}, THUMBS_DIR)
        thumb_dest.parent.mkdir(parents=True, exist_ok=True)
        if kind == "video":
            thumbgen.make_video_thumb(dest, thumb_dest)
        else:
            thumbgen.make_image_thumb(dest, thumb_dest)
    except Exception as exc:  # pragma: no cover - thumbnail is best-effort
        print(f"import thumbnail failed: {exc}")

    # created_at from the file's own modified time (so imports sort by their real date),
    # falling back to now. Original name is kept for search + display.
    orig = Path(str(request.form.get("rel") or f.filename)).name
    try:
        ts = float(request.form.get("mtime") or 0) / 1000.0
        created = (datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
                   if ts > 0 else datetime.datetime.now(datetime.timezone.utc))
    except (TypeError, ValueError, OSError, OverflowError):
        created = datetime.datetime.now(datetime.timezone.utc)
    record = {
        "id": mid,
        "media_type": kind,
        "local_path": rel,
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%S"),
        "prompt": orig.rsplit(".", 1)[0][:200],
        "model": "Imported",
        "imported": True,
        "orig_filename": orig[:255],
    }
    with _imports_lock:
        _imports.setdefault(import_id, []).append(record)
    return jsonify(ok=True, id=mid, media_type=kind,
                   thumb=f"/thumbnails/{media_shard(mid)}/{mid}.jpg")


@app.post("/api/import/commit")
def api_import_commit() -> Response:
    """Finalize an import: append all buffered records to metadata.json, file them into a
    collection (a new one named after the folder, OR an existing one when ``collection_id``
    is given), and rebuild the index once. Body: {import_id, name, collection_id?}."""
    payload = request.get_json(silent=True) or {}
    import_id = str(payload.get("import_id") or "").strip()
    name = (str(payload.get("name") or "").strip() or "Imported")[:80]
    target_id = str(payload.get("collection_id") or "").strip()

    # Validate an existing-collection target BEFORE consuming the buffered upload, so a
    # bad request (deleted or sealed target) leaves the files recoverable — the client
    # can retry with another target or cancel (which deletes them).
    collections = _load_collections(strict=True)
    target = None
    if target_id:
        target = next((c for c in collections if str(c.get("id")) == target_id), None)
        if target is None:
            return jsonify(ok=False, error="That collection no longer exists."), 404
        # Refuse a sealed (locked + not unlocked this session) target: the client never
        # offers it, and an import must not mutate a collection the user can't see.
        if target.get("locked") and target.get("pass_hash") and target_id not in _session_unlocked():
            return jsonify(ok=False, error="Unlock this collection before importing into it."), 403
        if _clean_group_name(target.get("group")).casefold() in {g.casefold() for g in _sealed_group_names()}:
            return jsonify(ok=False, error="Unlock this collection's group before importing into it."), 403

    with _imports_lock:
        records = _imports.pop(import_id, None)
    if not records:
        return jsonify(ok=False, error="Nothing to import."), 400

    ids = [r["id"] for r in records]
    today = _utc_stamp()

    if target is not None:
        seen = set(target.get("ids", []))
        target["ids"] = list(target.get("ids", [])) + [i for i in ids if i not in seen]
        if not target.get("cover_id") and ids:
            target["cover_id"] = ids[0]
        target["updated_at"] = today
        coll_id, coll_name = target_id, target.get("name") or name
    else:
        coll_id, coll_name = "col-" + secrets.token_hex(8), name
        collections.insert(0, {
            "id": coll_id, "name": name, "ids": ids,
            "cover_id": ids[0] if ids else "", "created_at": today, "updated_at": today,
        })

    loaded = _load_json_strict(METADATA_FILE, [])
    items = loaded if isinstance(loaded, list) else []
    items.extend(records)

    # Publish the import. Write the collection membership BEFORE metadata.json (which is
    # what rebuild_db indexes from): if the second write fails, the worst durable state is
    # a collection holding ids not yet in metadata (they simply render nothing) rather than
    # archived media orphaned out of every collection. On any write failure, restore the
    # buffer so the client's cancel can reclaim the uploaded files instead of stranding them.
    try:
        _atomic_write_json(COLLECTIONS_FILE, [c for c in (_clean_collection(c) for c in collections) if c])
        _atomic_write_json(METADATA_FILE, items)
    except Exception:
        with _imports_lock:
            _imports.setdefault(import_id, []).extend(records)
        raise

    _archive_ids(ids)  # imported media start archived (kept out of Recent), per design
    rebuild_db()
    return jsonify(ok=True, collection_id=coll_id, name=coll_name, count=len(ids))


@app.post("/api/import/cancel")
def api_import_cancel() -> Response:
    """Discard a buffered, never-committed import: delete its on-disk files + thumbs."""
    payload = request.get_json(silent=True) or {}
    import_id = str(payload.get("import_id") or "").strip()
    with _imports_lock:
        records = _imports.pop(import_id, None) or []
    for r in records:
        try:
            (GALLERY_DIR / r["local_path"]).unlink(missing_ok=True)
        except OSError:
            pass
        try:
            thumbgen.thumb_path({"id": r["id"]}, THUMBS_DIR).unlink(missing_ok=True)
        except OSError:
            pass
    return jsonify(ok=True, removed=len(records))


@app.errorhandler(413)
def _payload_too_large(_e):
    """An upload exceeded MAX_CONTENT_LENGTH — log it (so import skips are diagnosable
    from the container logs) and return a clear reason for the client to surface."""
    size = request.content_length
    limit = app.config.get("MAX_CONTENT_LENGTH")
    pretty = f"{size / 1048576:.0f} MB" if size else "unknown size"
    print(f"413 payload too large on {request.path}: {pretty} (limit {limit and limit // 1048576} MB)")
    return jsonify(ok=False, error=f"File is larger than the {limit and limit // (1024**3)} GB upload limit."), 413


# --------------------------------------------------------------------------- #
# Playlist export (ffmpeg merge -> streamed MP4, nothing persisted on disk)
# --------------------------------------------------------------------------- #

def _metadata_index() -> dict:
    """Map item id -> metadata record so a playlist's ids resolve to media files."""
    if not METADATA_FILE.exists():
        return {}
    try:
        items = json.loads(METADATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    index: dict = {}
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("id"):
            index[str(item["id"])] = item
    return index


def _video_paths_for_ids(ids: list, *, exclude_montages: bool = False) -> list[Path]:
    """Ordered, existing video files for a list of item ids (order preserved).
    Ignores images, missing files, and anything that would escape the gallery
    root (path traversal). With ``exclude_montages`` it also skips beat-montage
    outputs (``model == "Beat Montage"``) so a montage can't be fed back into a new
    montage — used by the montage generator, not by playlist/MP4 export."""
    index = _metadata_index()
    gallery_root = GALLERY_DIR.resolve()
    paths: list[Path] = []
    for raw_id in ids or []:
        item = index.get(str(raw_id))
        if not item or item.get("media_type") != "video":
            continue
        if exclude_montages and item.get("model") == "Beat Montage":
            continue
        rel = str(item.get("local_path", "")).replace("\\", "/")
        if not rel:
            continue
        candidate = (GALLERY_DIR / rel).resolve()
        if candidate != gallery_root and gallery_root not in candidate.parents:
            continue
        if candidate.exists():
            paths.append(candidate)
    return paths


def _montage_source_paths_for_ids(ids: list) -> list[Path]:
    """Ordered, existing source files — videos AND still images — for the Picture &
    Video montage mode (order preserved). Same guards as ``_video_paths_for_ids`` (skips
    missing files, beat-montage outputs so a montage can't feed itself, and anything that
    would escape the gallery root) but also admits ``media_type == 'image'``. Kept a
    SEPARATE resolver so ``_video_paths_for_ids`` stays strictly video-only for
    playlist / MP4 export — images flow only through the montage preset that opts in."""
    index = _metadata_index()
    gallery_root = GALLERY_DIR.resolve()
    paths: list[Path] = []
    for raw_id in ids or []:
        item = index.get(str(raw_id))
        if not item or item.get("media_type") not in ("video", "image"):
            continue
        if item.get("model") == "Beat Montage":
            continue
        rel = str(item.get("local_path", "")).replace("\\", "/")
        if not rel:
            continue
        candidate = (GALLERY_DIR / rel).resolve()
        if candidate != gallery_root and gallery_root not in candidate.parents:
            continue
        if candidate.exists():
            paths.append(candidate)
    return paths


def _playlist_video_paths(playlist: dict) -> list[Path]:
    return _video_paths_for_ids(playlist.get("ids", []))


def _image_path_for_id(media_id: str):
    """Resolve ``(path, record)`` for an IMAGE media id, guarding against path traversal
    out of the gallery root. ``path`` is None when the record is missing, is a video, has
    no file on disk, or would escape the gallery; ``record`` is the metadata dict (or None
    when the id is unknown) so the caller can distinguish those cases."""
    item = _metadata_index().get(str(media_id))
    if not item or item.get("media_type") == "video":
        return None, item
    rel = str(item.get("local_path", "")).replace("\\", "/")
    if not rel:
        return None, item
    gallery_root = GALLERY_DIR.resolve()
    candidate = (GALLERY_DIR / rel).resolve()
    if candidate != gallery_root and gallery_root not in candidate.parents:
        return None, item
    return (candidate if candidate.exists() else None), item


def _image_b64_for_vision(path: Path, *, max_edge: int = 1024) -> tuple[str, str]:
    """Base64-encode an image for a vision model, downscaled to ``max_edge`` px on the long
    edge via ffmpeg (already a hard dependency) to cap tokens/latency without upscaling
    small images. Returns ``(b64, mime)``; falls back to the raw bytes (mime guessed from
    the suffix) if ffmpeg is unavailable or fails."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path),
             "-vf", f"scale='min({max_edge},iw)':'min({max_edge},ih)':force_original_aspect_ratio=decrease",
             "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "3", "pipe:1"],
            capture_output=True, timeout=60,
        )
        if out.returncode == 0 and out.stdout:
            return base64.b64encode(out.stdout).decode("ascii"), "image/jpeg"
    except Exception:
        pass
    raw = path.read_bytes()
    ext = path.suffix.lower().lstrip(".")
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "webp": "image/webp", "gif": "image/gif"}.get(ext, "image/jpeg")
    return base64.b64encode(raw).decode("ascii"), mime


def _probe_signature(path: Path):
    """(codec, width, height, pix_fmt, fps) of the first video stream, or None.

    Two clips with an identical signature can be concatenated with ``-c copy``
    losslessly; any difference forces a re-encode.
    """
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,width,height,pix_fmt,r_frame_rate",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        streams = (json.loads(out.stdout or "{}").get("streams") or [])
        if not streams:
            return None
        s = streams[0]
        return (s.get("codec_name"), s.get("width"), s.get("height"), s.get("pix_fmt"), s.get("r_frame_rate"))
    except Exception:
        return None


def _has_audio(path: Path) -> bool:
    """True if the file has at least one audio stream."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return bool((out.stdout or "").strip())
    except Exception:
        return False


def _run_ffmpeg(cmd: list[str], what: str, cwd: str | None = None) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600, cwd=cwd)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "").strip()[-400:] or f"ffmpeg failed ({what})")


def _concat_line(path: Path) -> str:
    # concat demuxer escapes a single quote in a path as '\'' .
    safe = str(path).replace("'", "'\\''")
    return f"file '{safe}'\n"


CRF = "10"
PRESET = "slow"
AUDIO_BPS = "320k"
AUDIO_AR = "48000"
FALLBACK_W = 1920
FALLBACK_H = 1080
# Video re-encoder selection. "auto" uses NVIDIA NVENC (GPU) when it can actually
# initialise on this host, else falls back to CPU libx264. "nvenc"/"cpu" force it.
VIDEO_ENCODER = os.environ.get("VIDEO_ENCODER", "auto").strip().lower()
NVENC_PRESET = "p5"   # NVENC quality/speed balance (p1 fastest … p7 slowest)
NVENC_CQ = "19"       # constant-quality target; ~matches libx264 CRF 10 for the merge
# Baseline burned-in subtitle font size (= "100%"); half of libass's default of 16
# for SRT. The saved subtitle_size percentage scales this in _ass_force_style().
SUBTITLE_FONTSIZE = 8


_nvenc_cache: bool | None = None


def _probe_nvenc() -> bool:
    """True if h264_nvenc can actually initialise here (NVIDIA driver + GPU present
    in the container). A tiny one-frame encode to a null sink is the only reliable
    test — the encoder is always *compiled in*, but only works with a visible GPU.

    The probe frame must clear NVENC's minimum dimensions — newer drivers/SDKs
    reject very small frames ("Frame Dimension less than the minimum supported
    value"), so a 64x64 probe yields a false negative and silently drops every
    encode onto the CPU. 256x256 is comfortably above the floor for all codecs."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=black:s=256x256:r=1", "-frames:v", "1",
             "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True, text=True, timeout=30,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _use_nvenc() -> bool:
    """Whether to encode on the GPU. VIDEO_ENCODER forces it (nvenc/cpu); the
    default (auto) probes once and caches the result."""
    global _nvenc_cache
    if VIDEO_ENCODER == "cpu":
        return False
    if VIDEO_ENCODER == "nvenc":
        return True
    if _nvenc_cache is None:
        _nvenc_cache = _probe_nvenc()
        app.logger.info(
            "video encoder: %s", "NVENC (GPU)" if _nvenc_cache else "libx264 (CPU)"
        )
    return _nvenc_cache


def _video_encode_args(crf: str, cpu_preset: str = PRESET, nvenc_cq: str = NVENC_CQ) -> list[str]:
    """Video-codec args for a re-encode: NVENC on the GPU when available, else CPU
    libx264. Quality knobs differ — libx264 uses -crf, NVENC uses -cq (both: lower
    = better). Decoding and the scale/pad filters stay on the CPU; only the encode
    moves to the GPU, which is where the time goes."""
    if _use_nvenc():
        return ["-c:v", "h264_nvenc", "-preset", NVENC_PRESET,
                "-rc", "vbr", "-cq", nvenc_cq, "-b:v", "0", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", cpu_preset, "-crf", crf, "-pix_fmt", "yuv420p"]


def _merge_videos(paths: list[Path], out_path: Path) -> bool:
    """Merge clips in order into out_path. Returns True if the merge was a pure
    stream copy (zero quality loss), False if clips had to be re-encoded.

    When every clip shares codec/width/height/pix_fmt/fps the clips are
    concatenated with ``-c copy`` — no re-encode, audio untouched. When specs
    vary, each clip is re-encoded (CRF 10, preset slow) onto the largest input
    resolution by pixel count, padded to keep aspect, with audio re-encoded to
    AAC (a silent track is added to any clip lacking audio so audio is never
    dropped); the normalised clips are then losslessly concatenated.

    A clip that opens on a Grok character-sheet intro card (a static held frame,
    then a hard cut, inside the first ~1.5s — see moviegen.detect_head_trim) gets
    that head cut off. A frame-accurate trim can't be stream-copied (the cut isn't
    a keyframe), so any detected card forces the re-encode path for the whole
    merge — near-lossless at CRF 10, and the only way to keep the cards out.

    Raises RuntimeError if ffmpeg exits non-zero.
    """
    signatures = [_probe_signature(p) for p in paths]
    trims = [moviegen.detect_head_trim(p) for p in paths]
    if any(t > 0 for t in trims):
        _log(f"export: trimming intro card from {sum(1 for t in trims if t > 0)} "
             f"of {len(paths)} clip(s)")
    lossless = (all(sig is not None for sig in signatures)
                and len(set(signatures)) == 1 and not any(t > 0 for t in trims))
    listfile = out_path.parent / "concat.txt"

    if lossless:
        listfile.write_text("".join(_concat_line(p) for p in paths), encoding="utf-8")
        _run_ffmpeg(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
             "-c", "copy", "-movflags", "+faststart", str(out_path)],
            "concat copy",
        )
        return True

    # Re-encode target = the input resolution with the most pixels.
    best_pixels = 0
    target_w, target_h = FALLBACK_W, FALLBACK_H
    for sig in signatures:
        if sig and sig[1] and sig[2]:
            pixels = sig[1] * sig[2]
            if pixels > best_pixels:
                best_pixels = pixels
                target_w, target_h = sig[1], sig[2]
    target_w += target_w % 2
    target_h += target_h % 2
    vf = (
        f"scale=iw*sar:ih,scale={target_w}:{target_h}:force_original_aspect_ratio=decrease:"
        f"force_divisible_by=2,pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
    )

    normalised: list[Path] = []
    for index, src in enumerate(paths):
        temp = out_path.parent / f"norm_{index}.mp4"
        # Input-side -ss + re-encode = a frame-accurate cut of the intro card
        # (video and this input's audio both trimmed; the lavfi silence isn't).
        seek = ["-ss", f"{trims[index]:.3f}"] if trims[index] > 0 else []
        common_v = [
            "-vf", vf,
            *_video_encode_args(CRF),
            "-c:a", "aac", "-b:a", AUDIO_BPS, "-ar", AUDIO_AR, "-ac", "2",
            "-movflags", "+faststart", str(temp),
        ]
        if _has_audio(src):
            cmd = ["ffmpeg", "-y", *seek, "-i", str(src), "-map", "0:v:0", "-map", "0:a:0"] + common_v
        else:
            # No audio stream -> mux in silence so every clip carries audio.
            cmd = [
                "ffmpeg", "-y", *seek, "-i", str(src),
                "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-map", "0:v:0", "-map", "1:a:0", "-shortest",
            ] + common_v
        _run_ffmpeg(cmd, f"re-encode clip {index + 1}")
        normalised.append(temp)

    listfile.write_text("".join(_concat_line(p) for p in normalised), encoding="utf-8")
    _run_ffmpeg(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
         "-c", "copy", "-movflags", "+faststart", str(out_path)],
        "final concat",
    )
    return False


def _hex_to_ass_bgr(hex_color: str) -> str:
    """#RRGGBB → libass BGR hex (no alpha), e.g. '#ff8800' → '0088FF'. ASS colours
    are little-endian (BGR), the reverse of CSS's RRGGBB."""
    h = str(hex_color or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return "FFFFFF"
    return (h[4:6] + h[2:4] + h[0:2]).upper()


def _ass_force_style() -> str:
    """Map the saved subtitle display settings to a libass force_style string.

    ASS colours are &HAABBGGRR with an INVERTED alpha byte (00 = opaque, FF =
    fully transparent). Background opacity is rendered as an opaque box
    (BorderStyle=3) whose BackColour carries the alpha; Outline/Shadow are off so
    the box hugs the text, matching the player's ::cue look.
    """
    s = _load_settings()
    font = _SUB_FONT_LIBASS.get(str(s.get("subtitle_font") or "system"), "DejaVu Sans")
    fontsize = max(1, round(SUBTITLE_FONTSIZE * _sub_size(s.get("subtitle_size")) / 100))
    color_bgr = _hex_to_ass_bgr(_sub_color(s.get("subtitle_color")))
    back_alpha = format(round((1.0 - _sub_opacity(s.get("subtitle_bg_opacity"))) * 255), "02X")
    return ",".join([
        f"Fontname={font}",
        f"Fontsize={fontsize}",
        f"PrimaryColour=&H00{color_bgr}",
        "BorderStyle=3",
        f"BackColour=&H{back_alpha}000000",
        "Outline=0",
        "Shadow=0",
    ])


def _burn_subtitles_into(video_path: Path) -> Path:
    """Transcribe video_path via Whisper and burn the captions in (re-encode,
    CRF 18 / medium, audio copied through). Returns the burned file, or the
    original path unchanged if there's no Whisper URL, no speech, or any failure
    — burning must never break the download.

    ffmpeg runs with cwd set to the temp dir and references the SRT by basename,
    which avoids the subtitles= filter's path-escaping pitfalls (Windows drive
    colons, spaces, quotes) on every platform.
    """
    if not _whisper_url():
        return video_path
    try:
        srt = _whisper_srt(video_path)
    except Exception as exc:
        _log(f"burn: transcription failed, exporting without subtitles ({exc})")
        return video_path
    if not srt.strip():
        return video_path
    workdir = video_path.parent
    (workdir / "subs.srt").write_text(srt, encoding="utf-8")
    burned = workdir / "burned.mp4"
    # Style the burned-in captions from the saved subtitle display settings
    # (font / size / colour / background opacity), mapped to libass force_style.
    try:
        _run_ffmpeg(
            ["ffmpeg", "-y", "-i", video_path.name,
             "-vf", f"subtitles=subs.srt:force_style='{_ass_force_style()}'",
             *_video_encode_args("18", cpu_preset="medium", nvenc_cq="23"),
             "-c:a", "copy", "-movflags", "+faststart", burned.name],
            "burn subtitles",
            cwd=str(workdir),
        )
    except Exception as exc:
        _log(f"burn: ffmpeg failed, exporting without subtitles ({exc})")
        return video_path
    return burned


# Cap concurrent exports. Each export runs a full ffmpeg merge synchronously on its
# waitress worker thread (and holds it through the streamed download), so an
# unbounded burst could occupy every worker and freeze the whole UI. With 8 worker
# threads, 2 export slots leaves plenty for normal browsing; excess requests get a
# quick 503 instead of tying up a thread waiting.
_EXPORT_SLOTS = threading.BoundedSemaphore(2)


def _export_response(paths: list[Path], name: str):
    """Merge ``paths`` (in order), optionally burn subtitles, and stream the result
    as an MP4 download. The temp dir is deleted after streaming — nothing persists.
    Shared by the saved-playlist and one-off (selection) export routes."""
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        return jsonify(ok=False, error="ffmpeg is not available on the server."), 500
    if not paths:
        return jsonify(ok=False, error="No videos to export are available on the server."), 400

    if not _EXPORT_SLOTS.acquire(blocking=False):
        return jsonify(ok=False, error="The server is busy with other exports — please try again in a moment."), 503

    tmpdir = Path(tempfile.mkdtemp(prefix="ga-export-"))
    out_path = tmpdir / "merged.mp4"
    try:
        _merge_videos(paths, out_path)
        if _burn_enabled():
            out_path = _burn_subtitles_into(out_path)
    except Exception as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        _EXPORT_SLOTS.release()
        return jsonify(ok=False, error=f"Merge failed: {exc}"), 500

    try:
        size = out_path.stat().st_size
        safe_name = re.sub(r"[^\w.-]+", "_", name or "export").strip("_") or "export"
    except Exception as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        _EXPORT_SLOTS.release()
        return jsonify(ok=False, error=f"Merge failed: {exc}"), 500

    def generate():
        # The export slot is held for the whole stream (the worker thread is busy
        # until the client finishes downloading), then released here.
        try:
            with open(out_path, "rb") as fh:
                while True:
                    chunk = fh.read(262144)
                    if not chunk:
                        break
                    yield chunk
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            _EXPORT_SLOTS.release()

    return Response(
        generate(),
        mimetype="video/mp4",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}.mp4"',
            "Content-Length": str(size),
        },
    )


@app.get("/api/playlists/<pid>/export")
def api_playlist_export(pid: str) -> Response:
    playlists: list = []
    if PLAYLISTS_FILE.exists():
        try:
            loaded = json.loads(PLAYLISTS_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                playlists = loaded
        except Exception:
            playlists = []
    playlist = next((p for p in playlists if isinstance(p, dict) and str(p.get("id")) == pid), None)
    if not playlist:
        return jsonify(ok=False, error="Playlist not found."), 404
    return _export_response(_playlist_video_paths(playlist), playlist.get("name") or "playlist")


@app.post("/api/export")
def api_export() -> Response:
    """One-off export of an arbitrary ordered list of item ids (e.g. the current
    selection), without saving a playlist first."""
    payload = request.get_json(silent=True) or {}
    ids = payload.get("ids")
    if not isinstance(ids, list) or not ids:
        return jsonify(ok=False, error="No items selected to export."), 400
    return _export_response(_video_paths_for_ids(ids), payload.get("name") or "export")


def _image_paths_for_ids(ids: list) -> list[Path]:
    """Ordered, existing IMAGE files for a list of item ids (order preserved). The
    list form of _image_path_for_id: ignores videos, missing files, and anything that
    would escape the gallery root (path traversal). Used by the images-only ZIP export."""
    index = _metadata_index()
    gallery_root = GALLERY_DIR.resolve()
    paths: list[Path] = []
    for raw_id in ids or []:
        item = index.get(str(raw_id))
        if not item or item.get("media_type") != "image":
            continue
        rel = str(item.get("local_path", "")).replace("\\", "/")
        if not rel:
            continue
        candidate = (GALLERY_DIR / rel).resolve()
        if candidate != gallery_root and gallery_root not in candidate.parents:
            continue
        if candidate.exists():
            paths.append(candidate)
    return paths


@app.post("/api/export/images")
def api_export_images() -> Response:
    """Bundle the selected IMAGE ids into a store-only .zip and stream it. Store-only
    (ZIP_STORED) because JPG/PNG/WebP are already compressed — deflate would burn CPU
    for ~0 gain. Videos are never included here; mixed/video exports keep using
    /api/export (the MP4 merge). The temp file is deleted after streaming."""
    payload = request.get_json(silent=True) or {}
    ids = payload.get("ids")
    if not isinstance(ids, list) or not ids:
        return jsonify(ok=False, error="No images selected to export."), 400

    paths = _image_paths_for_ids(ids)
    if not paths:
        return jsonify(ok=False, error="No image files to export are available on the server."), 400

    # Name: the caller's name (collection) or the first image's filename stem, + a
    # datetimestamp. The server owns the final filename so the client just honours
    # Content-Disposition (same pattern as the backup export).
    base = str(payload.get("name") or "").strip() or paths[0].stem
    safe = re.sub(r"[^\w.-]+", "_", base).strip("_") or "images"
    filename = f"{safe}_{time.strftime('%Y%m%d-%H%M%S')}.zip"

    tmpdir = Path(tempfile.mkdtemp(prefix="ga-zip-"))
    zip_path = tmpdir / "export.zip"
    try:
        used: set[str] = set()
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
            for path in paths:
                arc = path.name
                while arc in used:  # de-dupe colliding basenames across folders
                    stem, ext = os.path.splitext(arc)
                    arc = f"{stem}_{len(used)}{ext}"
                used.add(arc)
                zf.write(path, arcname=arc)
        size = zip_path.stat().st_size
    except Exception as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return jsonify(ok=False, error=f"Zip failed: {exc}"), 500

    def generate():
        try:
            with open(zip_path, "rb") as fh:
                for chunk in iter(lambda: fh.read(262144), b""):
                    yield chunk
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    return Response(
        generate(),
        mimetype="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(size),
        },
    )


# --------------------------------------------------------------------------- #
# Generate Movie (beat-synced montage from selected videos + an uploaded song).
# Runs as a single background job — same threading+poll model as Sync, with its
# own slot so a render and a sync don't block each other. See moviegen.py.
# --------------------------------------------------------------------------- #

_movie_lock = threading.Lock()
_movie = {
    "running": False,
    "job_id": None,
    "status": "idle",      # idle|queued|analyzing_audio|analyzing_motion|planning|rendering|done|error
    "progress": 0.0,       # overall 0..1
    "stage_progress": 0.0,
    "detail": "",
    "error": None,
    "started_at": None,
    "finished_at": None,
    "result": None,        # {path, filename, width, height, fps, duration, size_bytes, cuts}
    "commit_meta": None,   # provenance captured at submit (song, seed, source ids…)
    "committed": False,    # already added to the gallery?
    "committing": False,   # a commit is in-flight (guards against double-commit)
    "committed_id": None,  # the gallery media id once committed
    "acknowledged": False, # user dealt with the finished result (dismissed/committed/discarded)
}

_SONG_EXTS = {"mp3", "wav", "flac", "m4a", "aac", "ogg", "opus"}
BEAT_MONTAGE_COLLECTION = "beat-montage"


def _movie_progress(status: str, overall: float, stage_progress: float = 0.0, detail: str = "") -> None:
    # Update the progress fields as one burst under the lock so /status never reads
    # a half-updated mix (e.g. new status with stale progress) across waitress threads.
    with _movie_lock:
        _movie["status"] = status
        _movie["progress"] = round(float(overall), 4)
        _movie["stage_progress"] = round(float(stage_progress), 4)
        _movie["detail"] = detail


# Absolute path to this repo's moviegen.py. The render runs as `python moviegen.py
# render` in a CHILD PROCESS (see moviegen._worker_main), not a thread, so the OS
# reclaims the job's full memory footprint (librosa arrays + numba JIT, ~2.5GB) the
# instant it exits — a long-lived server thread could never hand that back.
_MOVIEGEN_PY = str(Path(moviegen.__file__).resolve())


def _purge_movie_scratch(keep: Path) -> None:
    """Drop everything in MOVIE_DIR except ``keep`` (the finished movie) — the per-cut
    segments, video_only.mp4, edl.json, the uploaded song and the stderr log. Called
    after a successful render so the bulk of the scratch (the segments) is freed at
    once instead of lingering until the next generation wipes the dir. Best-effort:
    never raises into the worker, and only the finished movie is preserved (still
    needed for preview / download / commit)."""
    try:
        keep = keep.resolve()
        for entry in MOVIE_DIR.iterdir():
            if entry.resolve() == keep:
                continue
            try:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _movie_worker(job_id: str, paths: list[Path], song_path: Path | None, options: dict) -> None:
    """Drive one montage render in a separate process and mirror its streamed
    progress into the shared job state. The child reads a JSON spec on stdin and
    emits newline-delimited JSON (progress / result / error) on stdout; its stderr
    is captured to a log so an OOM/segfault before any message still yields a reason."""
    out_path = MOVIE_DIR / f"movie_{job_id}.mp4"
    spec = {
        "video_paths": [str(p) for p in paths],
        # None in match-cut mode with no song. MUST stay None rather than str(None),
        # which would reach the child as the literal path "None".
        "song_path": (str(song_path) if song_path else None),
        "options": options,
        "work_dir": str(MOVIE_DIR),
        "out_path": str(out_path),
        "video_encode_args": _video_encode_args(CRF),
        "hwaccel_decode": _use_nvenc(),
        # The server owns the data layout, so it names the cache dirs rather than
        # letting the child infer them relative to its scratch dir.
        "motion_cache_dir": str(MOTION_CACHE_DIR),
        "beat_cache_dir": str(BEAT_CACHE_DIR),
    }
    stderr_log = MOVIE_DIR / "worker_stderr.log"
    result = None
    error = None
    returncode = None
    try:
        with open(stderr_log, "wb") as errf:
            proc = subprocess.Popen(
                [sys.executable, "-u", _MOVIEGEN_PY, "render"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errf,
                text=True, bufsize=1,
            )
            proc.stdin.write(json.dumps(spec))
            proc.stdin.close()
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue  # ignore any non-JSON noise on stdout
                kind = msg.get("t")
                if kind == "progress":
                    _movie_progress(msg.get("status", ""), msg.get("overall", 0.0),
                                    msg.get("stage_progress", 0.0), msg.get("detail", ""))
                elif kind == "result":
                    result = msg.get("result")
                elif kind == "error":
                    error = msg.get("error")
            returncode = proc.wait()

        if result is not None:
            with _movie_lock:
                _movie["result"] = {
                    "path": str(out_path),
                    "filename": f"{options.get('name') or 'movie'}.mp4",
                    **result,
                }
            _movie_progress("done", 1.0, 1.0, "Done")
            # The final movie is assembled — the per-cut segments and other intermediates
            # are now dead weight, so reclaim that space immediately (keep only the movie
            # for preview/download/commit). On error we fall through and keep everything,
            # including the stderr log, for diagnostics.
            _purge_movie_scratch(keep=out_path)
        else:
            # No result line: the child raised (error set) or was killed before it
            # could report (OOM/segfault). Prefer its own message, then the kill
            # signal, then the tail of its stderr.
            if error is None:
                if returncode is not None and returncode < 0:
                    sig = -returncode
                    hint = " — likely out of memory" if sig == 9 else ""
                    error = f"montage worker killed by signal {sig}{hint}"
                else:
                    try:
                        tail = stderr_log.read_text(encoding="utf-8", errors="replace")[-400:].strip()
                    except OSError:
                        tail = ""
                    error = tail or f"montage worker exited with code {returncode}"
            # Also write it to the SERVER log. The job dict is the only other place
            # this lives, and it's in-memory — so a failure the user reports after a
            # restart is otherwise undiagnosable (the scratch dir keeps stderr, but a
            # clean RuntimeError from the planner never reaches stderr at all).
            _log(f"montage render failed ({options.get('mode') or 'beat'}, "
                 f"{len(paths)} clips): {str(error)[:300]}")
            with _movie_lock:
                _movie["status"] = "error"
                _movie["error"] = str(error)[:400]
                _movie["detail"] = "Generation failed"
    except Exception as exc:  # pragma: no cover - defensive
        with _movie_lock:
            _movie["status"] = "error"
            _movie["error"] = str(exc)[:400]
            _movie["detail"] = "Generation failed"
    finally:
        with _movie_lock:
            _movie["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _movie["running"] = False


def _auto_candidate_ids(coll_ids: list) -> list[str]:
    """Candidate media ids for an Auto Montage pool: the union of the given
    collections in order (locked collections stay private — their members never
    enter a pool; the UI doesn't offer them either), or every library video when
    none are given. Shared by the generate endpoint and the resolution histogram
    so the picker's counts describe exactly the pool a render would use."""
    if coll_ids:
        wanted = {str(c) for c in coll_ids}
        ids, seen = [], set()
        for coll in _load_collections():
            if coll["id"] in wanted and not coll.get("locked"):
                for mid in coll["ids"]:
                    if mid not in seen:
                        seen.add(mid)
                        ids.append(mid)
        return ids
    return [mid for mid, rec in _metadata_index().items()
            if rec.get("media_type") == "video"]


def _auto_canvas_from_pool(ids: list) -> tuple[int, int]:
    """Auto canvas for an auto-pick pool: the DOMINANT (most common) source
    resolution rather than the largest — most clips then render native and only
    the minority upscales, where largest-of-thousands would upscale nearly
    everything. Falls back to the largest-source rule (over a capped sample)
    when the index carries no dimensions."""
    try:
        stats = [s for s in db.video_resolution_stats(DB_FILE, [str(i) for i in ids])
                 if s.get("w")]
    except Exception:
        stats = []
    if not stats:
        return _auto_canvas_from_sources(ids[:200])
    w, h = int(stats[0]["w"]), int(stats[0]["h"])   # sorted by count desc
    scale = min(1.0, 3840 / w, 2160 / h)
    w, h = round(w * scale), round(h * scale)
    w = max(160, min(3840, w)); w -= w % 2
    h = max(160, min(2160, h)); h -= h % 2
    return (w, h)


def _auto_canvas_from_sources(ids: list, default_wh: tuple[int, int] = (1920, 1080)) -> tuple[int, int]:
    """Canvas size matching the largest (by pixel area) source video, so clips of that
    shape render edge-to-edge instead of being cropped/letterboxed to a mismatched
    frame. Montages and rows without known dimensions are ignored; the result is scaled
    down (never up) to fit the renderer's 3840x2160 ceiling, clamped to its floor, and
    forced to even dimensions (H.264). Falls back to ``default_wh`` when nothing is
    probeable (e.g. an index that predates dimension capture)."""
    try:
        rows = db.media_by_ids(DB_FILE, [str(i) for i in ids])
    except Exception:
        rows = []
    best = None  # (area, w, h)
    for r in rows:
        if r.get("media_type") != "video" or r.get("model") == "Beat Montage":
            continue
        w, h = r.get("media_w"), r.get("media_h")
        if not (w and h):
            continue
        area = int(w) * int(h)
        if best is None or area > best[0]:
            best = (area, int(w), int(h))
    if not best:
        return default_wh
    _, w, h = best
    scale = min(1.0, 3840 / w, 2160 / h)  # shrink to fit the ceiling; never upscale
    w, h = round(w * scale), round(h * scale)
    w = max(160, min(3840, w)); w -= w % 2
    h = max(160, min(2160, h)); h -= h % 2
    return (w, h)


@app.post("/api/movie/generate")
def api_movie_generate() -> Response:
    """Start a beat-synced montage render. Multipart: ``video_ids`` (JSON array),
    ``song`` (uploaded audio file), plus option fields. Returns a ``job_id`` to
    poll; the heavy work runs in a background thread."""
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        return jsonify(ok=False, error="ffmpeg is not available on the server."), 500
    try:
        ids = json.loads(request.form.get("video_ids", "[]"))
    except Exception:
        ids = []
    if not isinstance(ids, list):
        ids = []
    # Render mode. "beat" is the beat-synced montage (song REQUIRED); "matchcut" is
    # Motion Match Cut, which cuts on motion continuity and takes an OPTIONAL song.
    mode = (request.form.get("mode") or "beat").strip().lower()
    if mode not in ("beat", "matchcut"):
        mode = "beat"
    # Resolve the preset first: a preset flagged allow_stills (Picture & Video) admits
    # still images as sources; every other preset stays strictly video-only. Match-cut
    # is always video-only — a still has no motion to match.
    preset = (request.form.get("preset") or moviegen.DEFAULT_PRESET).strip()
    if preset not in moviegen.PRESETS:
        preset = moviegen.DEFAULT_PRESET
    allow_stills = (bool(moviegen.PRESETS.get(preset, {}).get("allow_stills"))
                    and mode != "matchcut")
    # Auto Montage: instead of a hand-picked selection, the CANDIDATE pool is the
    # whole library or the chosen collections; the render child's selection stage
    # picks the clips that best serve the song from cached motion analyses.
    auto_pick = (request.form.get("auto_pick") or "").strip().lower() in ("1", "true", "on", "yes")
    if auto_pick and mode != "beat":
        return jsonify(ok=False, error="Auto-pick works with beat montages only."), 400
    aspect = (request.form.get("aspect") or "").strip().lower()
    if auto_pick:
        try:
            coll_ids = json.loads(request.form.get("auto_collections", "[]"))
        except Exception:
            coll_ids = []
        if not isinstance(coll_ids, list):
            coll_ids = []
        ids = _auto_candidate_ids(coll_ids)
        # Aspect-matched pools: a vertical montage is cut from vertical clips —
        # cropping landscape footage to 9:16 loses most of the frame, so the
        # canvas orientation FILTERS the pool rather than mutilating it. Clips
        # the index has no dimensions for never match (shape unverifiable).
        if aspect in ("landscape", "portrait", "square"):
            ids = db.filter_video_ids_by_orientation(
                DB_FILE, ids if coll_ids else None, aspect)
    # A montage is built from source clips, never from other montages — exclude
    # them so a beat-montage can't be fed back into a new one.
    paths = (_montage_source_paths_for_ids(ids) if allow_stills and not auto_pick
             else _video_paths_for_ids(ids, exclude_montages=True))
    if len(paths) < 2:
        if auto_pick and aspect:
            err = (f"Auto-pick found fewer than 2 {aspect} videos in the pool — "
                   f"try a different aspect or more collections.")
        elif auto_pick:
            err = "Auto-pick found fewer than 2 videos in the chosen collections."
        elif allow_stills:
            err = "Select at least 2 videos or images that exist on the server."
        else:
            err = "Select at least 2 non-montage videos that exist on the server."
        return jsonify(ok=False, error=err), 400

    # The song sets the beat grid, so it is mandatory for a beat montage and merely a
    # bed for a match cut (which renders silent without one).
    song = request.files.get("song")
    ext = ""
    if song and song.filename:
        ext = (song.filename.rsplit(".", 1)[-1] if "." in song.filename else "").lower()
        if ext not in _SONG_EXTS:
            return jsonify(ok=False, error=f"Unsupported audio format '.{ext}'."), 400
    elif mode == "beat":
        return jsonify(ok=False, error="Choose a song to set the beat."), 400
    else:
        song = None

    def _num(name, default, lo, hi, cast=float):
        try:
            return max(lo, min(hi, cast(request.form.get(name, default))))
        except (TypeError, ValueError):
            return default

    target = request.form.get("target_duration", "").strip()
    seed_raw = request.form.get("seed", "").strip()
    # (preset already resolved above, to pick the video-only vs image-inclusive resolver)
    # "Let clips speak": preset-independent. The count is "auto" or a small int.
    let_speak = (request.form.get("let_clips_speak") or "").strip().lower() in ("1", "true", "on", "yes")
    speak_raw = (request.form.get("speak_moments") or "auto").strip().lower()
    speak_moments = speak_raw if speak_raw == "auto" else (
        str(max(1, min(6, int(speak_raw)))) if speak_raw.isdigit() else "auto")
    # Resolution: "auto" (or no explicit width sent) sizes the canvas to the largest
    # source clip so footage isn't cropped/letterboxed to a mismatched frame; an
    # explicit width/height from the manual picker overrides it.
    if (request.form.get("resolution") or "").strip().lower() == "auto" or not request.form.get("width"):
        # Auto-pick: dominant resolution of the (aspect-filtered) pool; manual
        # selection keeps the largest-source rule.
        canvas_w, canvas_h = (_auto_canvas_from_pool(ids) if auto_pick
                              else _auto_canvas_from_sources(ids))
    else:
        canvas_w = int(_num("width", 1920, 160, 3840, int))
        canvas_h = int(_num("height", 1080, 160, 2160, int))
    try:
        target_duration = max(1.0, float(target)) if target else None
    except ValueError:
        target_duration = None   # a non-numeric length is "auto", not a 500
    options = {
        "name": (request.form.get("name") or "movie")[:80],
        "mode": mode,
        "preset": preset,
        "tightness": _num("tightness", 0.5, 0.0, 1.0),
        "width": canvas_w,
        "height": canvas_h,
        "fps": int(_num("fps", 30, 12, 60, int)),
        "target_duration": target_duration,
        # Each run sends a fresh seed so successive renders differ; absent/invalid
        # -> deterministic (strict best).
        "seed": (int(seed_raw) if seed_raw.lstrip("-").isdigit() else None),
        "let_clips_speak": let_speak,
        "speak_moments": speak_moments,
        # Match-cut only: retime shots into the measured free speed band so apparent
        # velocity is continuous across a seam, and optionally blend the seam over a
        # few frames (a dissolve on already-matched motion reads as a morph).
        "match_speed": (request.form.get("match_speed") or "1").strip().lower()
                       in ("1", "true", "on", "yes"),
        "match_dissolve": (request.form.get("match_dissolve") or "").strip().lower()
                          in ("1", "true", "on", "yes"),
        # Songless match cut: use the source clips' own audio as the soundtrack.
        # Defaults ON — otherwise the render is silent and usable audio is thrown away.
        "keep_audio": (request.form.get("keep_audio") or "1").strip().lower()
                      in ("1", "true", "on", "yes"),
        "auto_pick": auto_pick,
    }

    with _movie_lock:
        if _movie["running"]:
            return jsonify(ok=False, error="A movie is already being generated."), 409
        job_id = secrets.token_hex(8)
        # Fresh working dir so a previous render's segments/output can't leak in.
        shutil.rmtree(MOVIE_DIR, ignore_errors=True)
        MOVIE_DIR.mkdir(parents=True, exist_ok=True)
        # No song is legitimate in match-cut mode — the render is then silent.
        song_path = None
        if song is not None:
            song_path = MOVIE_DIR / f"song.{ext}"
            song.save(str(song_path))
        _movie.update(running=True, job_id=job_id, status="queued", progress=0.0,
                      stage_progress=0.0, detail="Queued…", error=None, result=None,
                      started_at=time.strftime("%Y-%m-%d %H:%M:%S"), finished_at=None,
                      committed=False, committing=False, committed_id=None, acknowledged=False,
                      commit_meta={
                          "name": options["name"],
                          "song": (song.filename if song is not None else None),
                          "mode": mode,
                          "seed": options["seed"], "tightness": options["tightness"],
                          "fps": options["fps"], "preset": options["preset"],
                          "let_clips_speak": options["let_clips_speak"],
                          "speak_moments": options["speak_moments"],
                          # Auto-pick pools can be thousands of ids; the status
                          # endpoint echoes source_ids every poll, so keep them
                          # out and flag the mode instead.
                          "source_ids": [] if auto_pick else [str(i) for i in ids],
                          "auto_pick": auto_pick,
                      })
    threading.Thread(target=_movie_worker, args=(job_id, paths, song_path, options), daemon=True).start()
    return jsonify(ok=True, job_id=job_id), 202


@app.post("/api/movie/motioncache")
def api_movie_motioncache() -> Response:
    """Start the library motion warm-up (the panel's Analyze Library button).
    Shares the sync/subtitles job slot; progress streams into the same log panel."""
    if start_motioncache():
        return jsonify(ok=True)
    return jsonify(ok=False, error="Another job is already running."), 409


@app.post("/api/movie/resolutions")
def api_movie_resolutions() -> Response:
    """Resolution histogram for a montage candidate pool — drives the panel's
    aspect/resolution picker. Body: ``{collections?: [ids], ids?: [media ids]}``;
    explicit ``ids`` (a manual selection) win, else the auto-pick pool for the
    given collections (none = whole library). Returns ``sizes`` ([{w, h,
    orientation, count}], biggest count first), ``total`` (dimensioned videos)
    and ``unknown`` (videos the index has no dimensions for — old index rows)."""
    body = request.get_json(silent=True) or {}
    ids = body.get("ids")
    try:
        if isinstance(ids, list) and ids:
            stats = db.video_resolution_stats(DB_FILE, [str(i) for i in ids])
        else:
            coll = body.get("collections")
            coll = coll if isinstance(coll, list) else []
            # Whole library skips the id plumbing entirely (one GROUP BY).
            stats = db.video_resolution_stats(
                DB_FILE, _auto_candidate_ids(coll) if coll else None)
    except Exception:
        stats = []
    known = [s for s in stats if s.get("w")]
    unknown = sum(s["count"] for s in stats if not s.get("w"))
    return jsonify(sizes=known, total=sum(s["count"] for s in known), unknown=unknown)


@app.get("/api/movie/motion_coverage")
def api_movie_motion_coverage() -> Response:
    """How much of the library the montage motion cache covers — drives the
    Generate Movie panel's Analyze Library readout and gates Auto-pick. Two
    stats per clip (media file + cache entry), no decoding."""
    videos = cached = 0
    gallery_root = GALLERY_DIR.resolve()
    for item in _metadata_index().values():
        if item.get("media_type") != "video" or item.get("model") == "Beat Montage":
            continue
        rel = str(item.get("local_path", "")).replace("\\", "/")
        if not rel:
            continue
        p = (GALLERY_DIR / rel).resolve()
        if p != gallery_root and gallery_root not in p.parents:
            continue
        if not p.exists():
            continue
        videos += 1
        if moviegen.motion_cache_has(p, MOTION_CACHE_DIR):
            cached += 1
    with _sync_lock:
        running = bool(_sync["running"] and _sync.get("job") == "motioncache")
    return jsonify(videos=videos, cached=cached, running=running)


@app.get("/api/movie/status")
def api_movie_status() -> Response:
    # Copy the whole job under the lock so the response is a single consistent
    # snapshot, never a mix of fields from before and after a worker update. The
    # worker only ever *reassigns* result/commit_meta (never mutates them in place),
    # so reading those nested dicts after the shallow copy is safe.
    with _movie_lock:
        snap = dict(_movie)
    r = snap["result"]
    meta = snap["commit_meta"] or {}
    return jsonify(
        running=snap["running"],
        job_id=snap["job_id"],
        status=snap["status"],
        progress=snap["progress"],
        stage_progress=snap["stage_progress"],
        detail=snap["detail"],
        error=snap["error"],
        started_at=snap["started_at"],
        finished_at=snap["finished_at"],
        # Submit-time provenance so the panel header is correct even when reopened
        # from the status chip with no live selection (and mid-render, before a
        # result exists): the source videos that went in, and which preset. The
        # ids let "Make another" re-render the same clips after the live selection
        # is gone.
        sources=len(meta.get("source_ids") or []),
        source_ids=meta.get("source_ids") or [],
        preset=meta.get("preset"),
        # Auto Montage: the panel header/labels say "auto-picked" instead of a
        # selection count (source_ids is deliberately empty for auto jobs).
        auto_pick=bool(meta.get("auto_pick")),
        # Which pipeline produced this job. The panel is destroyed on close, so on
        # reopen this is the ONLY signal of the job's true mode — without it a live
        # match cut is labelled as a beat montage.
        mode=meta.get("mode") or "beat",
        # Whether the finished result was already added to the gallery and whether
        # the user has dealt with it. These survive reloads (unlike the client's
        # in-memory ack), so the floating chip can stay dismissed across sessions.
        committed=snap["committed"],
        committed_id=snap["committed_id"],
        acknowledged=snap["acknowledged"],
        # Don't leak the absolute server path to the client.
        result=({k: v for k, v in r.items() if k != "path"} if r else None),
    )


@app.get("/api/movie/result")
def api_movie_result() -> Response:
    """Serve the finished movie — inline (range-enabled) so the SPA can preview it
    in a <video>, or as a download with ``?download=1``."""
    r = _movie["result"]
    if not r or not Path(r["path"]).exists():
        return jsonify(ok=False, error="No finished movie available."), 409
    download = request.args.get("download") in ("1", "true", "yes")
    resp = send_file(r["path"], mimetype="video/mp4", conditional=True,
                     as_attachment=download, download_name=r.get("filename", "movie.mp4"))
    # Single result slot reusing one URL — don't let the browser cache a stale render.
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _commit_montage() -> dict:
    """Persist the finished montage into the gallery: copy the file under a unique
    id, make a thumbnail, append a metadata record with provenance, drop it into
    the 'Beat Montage' collection, and rebuild the index. Returns {id, collection_id}."""
    r = _movie.get("result")
    if not r or not Path(r["path"]).exists():
        raise RuntimeError("No finished movie to add.")
    meta = _movie.get("commit_meta") or {}

    mid = "montage_" + secrets.token_hex(8)  # unique; never collides with Grok UUIDs
    rel = f"media/montages/{media_shard(mid)}/{mid}.mp4"
    dest = GALLERY_DIR / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(r["path"], dest)
    try:
        thumb_dest = thumbgen.thumb_path({"id": mid}, THUMBS_DIR)
        thumb_dest.parent.mkdir(parents=True, exist_ok=True)
        thumbgen.make_video_thumb(dest, thumb_dest)
    except Exception as exc:  # pragma: no cover - thumbnail is best-effort
        print(f"montage thumbnail failed: {exc}")

    song = str(meta.get("song") or "").replace("\\", "/").rsplit("/", 1)[-1]
    is_match = (meta.get("mode") or "beat") == "matchcut"
    # The TITLE distinguishes the two modes, but `model` below stays "Beat Montage"
    # for both. That exact string is the only thing stopping a rendered montage being
    # selected as a source for another one, and it is load-bearing in 3 server sites
    # and 5 Svelte sites — see specs/match-cut-spec.md §7.1.
    title = ["Motion Match Cut" if is_match else "Beat Montage"]
    if meta.get("name") and meta["name"] != "movie":
        title.append(str(meta["name"]))
    if song:
        title.append(song)
    record = {
        "id": mid,
        "media_type": "video",
        "local_path": rel,
        "created_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "prompt": " · ".join(title) + f" · {r.get('cuts', '?')} cuts",
        "model": "Beat Montage",
        "width": r.get("width"),
        "height": r.get("height"),
        # Provenance — ignored by the index, kept in metadata.json for reference.
        "montage": True,
        "mode": meta.get("mode") or "beat",
        "preset": meta.get("preset") or r.get("preset"),
        "fps": r.get("fps"),
        "duration": r.get("duration"),
        "seed": meta.get("seed"),
        "tightness": meta.get("tightness"),
        "source_ids": meta.get("source_ids") or [],
        "song": song,
        # What the matcher actually found (clips gated, seam quality) — keeps a weak
        # render diagnosable long after the scratch dir is purged.
        **({"match": r["match"]} if r.get("match") else {}),
    }
    # Strict load: if metadata.json exists but is unreadable, abort rather than
    # rewrite it with only this montage (which would erase the whole library).
    loaded = _load_json_strict(METADATA_FILE, [])
    items: list = loaded if isinstance(loaded, list) else []
    items.append(record)
    _atomic_write_json(METADATA_FILE, items)

    # Add to the 'Beat Montage' collection (create on first use). Strict load for
    # the same reason — a corrupt read must not wipe the user's other collections.
    collections = _load_collections(strict=True)
    coll = next((c for c in collections
                 if c.get("id") == BEAT_MONTAGE_COLLECTION or str(c.get("name", "")).lower() == "beat montage"), None)
    today = _utc_stamp()
    if coll is None:
        collections.insert(0, {
            "id": BEAT_MONTAGE_COLLECTION, "name": "Beat Montage", "ids": [mid],
            "cover_id": mid, "created_at": today, "updated_at": today,
        })
        coll_id = BEAT_MONTAGE_COLLECTION
    else:
        if mid not in coll["ids"]:
            coll["ids"].append(mid)
        coll["cover_id"] = coll.get("cover_id") or mid
        coll["updated_at"] = today
        coll_id = coll["id"]
    _atomic_write_json(COLLECTIONS_FILE, [c for c in (_clean_collection(c) for c in collections) if c])

    rebuild_db()
    return {"id": mid, "collection_id": coll_id}


@app.post("/api/movie/commit")
def api_movie_commit() -> Response:
    """Add the finished montage to the gallery + the 'Beat Montage' collection."""
    # Claim the commit atomically: validate and flip `committing` under the lock so
    # two near-simultaneous clicks can't both pass the checks and double-commit.
    with _movie_lock:
        if _movie.get("running"):
            return jsonify(ok=False, error="Still rendering."), 409
        r = _movie.get("result")
        if not r or not Path(r["path"]).exists():
            return jsonify(ok=False, error="No finished movie to add."), 409
        if _movie.get("committed"):
            return jsonify(ok=True, id=_movie.get("committed_id"),
                           collection_id=BEAT_MONTAGE_COLLECTION, already=True)
        if _movie.get("committing"):
            return jsonify(ok=False, error="Commit already in progress."), 409
        _movie["committing"] = True
    try:
        res = _commit_montage()
    except Exception as exc:
        with _movie_lock:
            _movie["committing"] = False
        return jsonify(ok=False, error=str(exc)[:300]), 500
    with _movie_lock:
        _movie["committed"] = True
        _movie["committed_id"] = res["id"]
        _movie["acknowledged"] = True  # committing is dealing with it — chip stays gone
        _movie["committing"] = False
    return jsonify(ok=True, **res)


@app.post("/api/movie/dismiss")
def api_movie_dismiss() -> Response:
    """Mark the finished montage as dealt with so the floating status chip stays
    hidden — durably, across reloads/tabs (the client's ack is memory-only). No-op
    while a render is still running."""
    with _movie_lock:
        if not _movie.get("running"):
            _movie["acknowledged"] = True
        acknowledged = _movie["acknowledged"]
    return jsonify(ok=True, acknowledged=acknowledged)


_rebuild_lock = threading.Lock()
_rebuild_running = False
_rebuild_dirty = False


def _do_rebuild() -> None:
    try:
        rows = db.build_index(DB_FILE, METADATA_FILE, GALLERY_DIR)
        print(f"index.db rebuilt: {rows} media rows")
    except Exception as exc:  # pragma: no cover - defensive
        print(f"index.db rebuild failed: {exc}")


def _rebuild_worker() -> None:
    # Trailing-run loop: anything that dirtied the index while a rebuild was in
    # flight is picked up by exactly one more pass (a rebuild snapshots
    # metadata.json at its start, so mid-flight changes need that second pass).
    global _rebuild_running, _rebuild_dirty
    while True:
        with _rebuild_lock:
            _rebuild_dirty = False
        _do_rebuild()
        with _rebuild_lock:
            if not _rebuild_dirty:
                _rebuild_running = False
                return


def rebuild_db(wait: bool = False) -> None:
    """Rebuild the SQLite read-model from metadata.json + on-disk thumbnails/subs.
    Safe to call anytime; the DB is purely derived.

    By default the rebuild is COALESCED onto a single background thread: a burst
    of calls (heavy generation nights fire one per finished item) folds into at
    most one in-flight rebuild plus one trailing pass, and no request thread
    stalls behind the ~O(library) scan. The new item shows up when the pass
    lands — seconds — instead of blocking its request. ``wait=True`` runs inline
    for callers that must query the fresh index immediately (startup before
    serving, backup restore, and the DB-file-missing bootstrap guards)."""
    global _rebuild_running, _rebuild_dirty
    if wait:
        _do_rebuild()
        return
    with _rebuild_lock:
        _rebuild_dirty = True
        if _rebuild_running:
            return
        _rebuild_running = True
    threading.Thread(target=_rebuild_worker, daemon=True, name="rebuild-db").start()


# --------------------------------------------------------------------------- #
# Grok Imagine API (xAI) — image & video generation into the gallery.
#
# Generated media is ingested exactly like downloaded media (file + thumbnail +
# metadata record + index rebuild), with an `api_generated` provenance flag and,
# for source-image generations, a `parent_id` link back to the gallery image so
# the existing related-media UI surfaces it.
# --------------------------------------------------------------------------- #

_CT_EXT = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
    "image/webp": "webp", "image/gif": "gif",
    "video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov",
}
_MEDIA_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "mp4", "webm", "mov", "m4v"}


def _ext_for_content_type(content_type: str, url: str = "", *, default: str = "jpg") -> str:
    """Best file extension for a downloaded media payload: prefer the Content-Type,
    fall back to the URL suffix, else ``default``."""
    ct = str(content_type or "").split(";")[0].strip().lower()
    if ct in _CT_EXT:
        return _CT_EXT[ct]
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower().lstrip(".")
    if suffix in _MEDIA_EXTS:
        return "jpg" if suffix == "jpeg" else suffix
    return default


def _image_data_uri_for_generation(path: Path) -> str:
    """A base64 data-URI for a gallery image used as a generation source. The
    self-hosted media server isn't reachable by xAI, so the image must be inlined
    (not passed as a URL). Encoded at a generous size to preserve source quality."""
    b64, mime = _image_b64_for_vision(path, max_edge=2048)
    return f"data:{mime};base64,{b64}"


def _sniff_image_ext(raw: bytes, *, default: str = "jpg") -> str:
    """File extension from an image's magic bytes (base64 results carry no mime)."""
    if raw[:3] == b"\xff\xd8\xff":
        return "jpg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return default


def _xai_image_payload_bytes(item: dict) -> tuple[bytes, str]:
    """Resolve an xAI image result item to ``(raw_bytes, ext)``, decoding inline
    base64 (preferred) or, as a fallback, downloading the temporary URL."""
    b64 = item.get("b64_json")
    if b64:
        raw = base64.b64decode(b64)
        return raw, _sniff_image_ext(raw)
    url = item.get("url")
    if not url:
        raise xai_imagine.XaiError("Image result had no data (it may have been filtered by moderation).")
    raw, ctype = xai_imagine.fetch_bytes(url)
    return raw, _ext_for_content_type(ctype, url, default="jpg")


def _ingest_generated_media(*, raw_bytes: bytes, ext: str, media_type: str, prompt: str,
                            model: str, provenance: dict, parent_id: str | None = None,
                            width=None, height=None, duration=None,
                            api_generated: bool = True, reindex: bool = True) -> dict:
    """Persist a generated image/video into the gallery: write the file under a
    unique ``imagine_*`` id, make a thumbnail, append a metadata record with
    provenance, and (optionally) rebuild the index. Returns the metadata record.

    ``reindex=False`` lets a batch (n>1) defer to a single rebuild by the caller."""
    mid = "imagine_" + secrets.token_hex(8)  # unique; never collides with Grok UUIDs
    sub = "videos" if media_type == "video" else "images"
    rel = f"media/{sub}/{media_shard(mid)}/{mid}.{ext}"
    dest = GALLERY_DIR / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw_bytes)
    try:
        thumb_dest = thumbgen.thumb_path({"id": mid}, THUMBS_DIR)
        thumb_dest.parent.mkdir(parents=True, exist_ok=True)
        if media_type == "video":
            thumbgen.make_video_thumb(dest, thumb_dest)
        else:
            thumbgen.make_image_thumb(dest, thumb_dest)
    except Exception as exc:  # pragma: no cover - thumbnail is best-effort
        print(f"imagine thumbnail failed: {exc}")

    record = {
        "id": mid,
        "media_type": media_type,
        "local_path": rel,
        "created_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "prompt": prompt or "",
        "model": model,
        "parent_id": parent_id,
        "width": width,
        "height": height,
        # Provenance — `api_generated` is also indexed (badge); the rest rides along
        # in metadata.json for reference (ignored by the index). An uploaded original
        # isn't AI-generated, so it carries no badge/provider.
        "api_generated": bool(api_generated),
        "api_provider": "xai" if api_generated else "",
        "api_params": provenance or {},
    }
    if duration is not None:
        record["duration"] = duration
    # Strict load: if metadata.json exists but is unreadable, abort rather than
    # rewrite it with only this record (which would erase the whole library).
    loaded = _load_json_strict(METADATA_FILE, [])
    items: list = loaded if isinstance(loaded, list) else []
    items.append(record)
    _atomic_write_json(METADATA_FILE, items)
    if reindex:
        rebuild_db()
    return record


def _resolve_generation_source(source_id: str):
    """Resolve a gallery image id to ``(data_uri, error_response)`` for use as a
    generation source. Exactly one of the two is non-None."""
    path, rec = _image_path_for_id(source_id)
    if rec is None:
        return None, (jsonify(ok=False, error="Source image not found."), 404)
    if path is None:
        return None, (jsonify(ok=False, error="Source must be an image with a file on disk."), 400)
    return _image_data_uri_for_generation(path), None


# --- Generation staging workspace ------------------------------------------- #
# Generations land in a per-source "session" (rooted on the gallery image you sent
# as a source, or one "scratch" session for text-only) — staged files + a sessions
# index live under GROK_DATA_DIR/imagine_staging and are NOT in the gallery. Saving a
# generation runs the normal ingest; clearing a session deletes its staged files
# (saved gallery items are independent and untouched).

STAGING_DIR = DATA_DIR / "imagine_staging"
STAGING_THUMBS_DIR = STAGING_DIR / "thumbs"
SESSIONS_FILE = DATA_DIR / "imagine_sessions.json"
_sessions_lock = threading.Lock()


def _load_sessions() -> dict:
    """All Imagine sessions keyed by session_id. Strict load: an unreadable file
    raises CorruptStateError rather than letting us blind-overwrite real history."""
    loaded = _load_json_strict(SESSIONS_FILE, {})
    return loaded if isinstance(loaded, dict) else {}


def _save_sessions(data: dict) -> None:
    _atomic_write_json(SESSIONS_FILE, data)


def _staged_media_path(gen_id: str, ext: str) -> Path:
    return STAGING_DIR / f"{gen_id}.{ext}"


def _staged_thumb_path(gen_id: str) -> Path:
    return STAGING_THUMBS_DIR / f"{gen_id}.jpg"


def _session_id_for(source_gallery_id: str) -> str:
    return f"src:{source_gallery_id}" if source_gallery_id else "scratch"


def _source_info(gallery_id: str):
    """The {id, thumb, prompt} of a gallery image used as a session root, or None."""
    if not gallery_id:
        return None
    rows = db.media_by_ids(DB_FILE, [gallery_id])
    r = rows[0] if rows else None
    return {
        "id": gallery_id,
        "thumb": (r.get("thumb") or r.get("href") or "") if r else "",
        "prompt": (r.get("prompt") or "") if r else "",
        "media_w": (r.get("media_w") if r else None),
        "media_h": (r.get("media_h") if r else None),
    }


def _ensure_session(sessions: dict, session_id: str) -> dict:
    sess = sessions.get(session_id)
    if sess is None:
        gid = session_id[4:] if session_id.startswith("src:") else ""
        sess = {"session_id": session_id, "source": _source_info(gid), "generations": []}
        sessions[session_id] = sess
    return sess


def _find_gen(sessions: dict, gen_id: str):
    for sess in sessions.values():
        for g in sess.get("generations", []):
            if g.get("gen_id") == gen_id:
                return sess, g
    return None, None


def _gen_public(rec: dict) -> dict:
    gid = rec.get("gen_id")
    return {**rec, "staged_url": f"/api/imagine/staged/{gid}",
            "thumb_url": f"/api/imagine/staged/{gid}/thumb"}


def _session_public(sess: dict) -> dict:
    return {
        "session_id": sess.get("session_id"),
        "source": sess.get("source"),
        "generations": [_gen_public(g) for g in sess.get("generations", [])],
    }


def _resolve_imagine_source(sessions: dict, source: str):
    """Resolve a per-generation source to ``(data_uri, parent_gen_id, error)``.
    ``source`` is a staged ``gen_*`` id (branch off a child), a gallery image id, or
    empty (text-only). ``error`` is a Flask response tuple when it can't be used."""
    source = str(source or "").strip()
    if not source:
        return "", None, None
    if source.startswith("gen_"):
        _, gen = _find_gen(sessions, source)
        if not gen:
            return "", None, (jsonify(ok=False, error="Source generation not found."), 404)
        if gen.get("media_type") != "image":
            return "", None, (jsonify(ok=False, error="Only an image can be used as a source."), 400)
        p = _staged_media_path(source, gen.get("ext") or "jpg")
        if not p.exists():
            return "", None, (jsonify(ok=False, error="Source generation file is missing."), 410)
        return _image_data_uri_for_generation(p), source, None
    data_uri, err = _resolve_generation_source(source)
    if err:
        return "", None, err
    return data_uri, None, None


def _write_staged_record(*, raw_bytes: bytes, ext: str, media_type: str, prompt: str,
                         model: str, params: dict, session_id: str,
                         source_gallery_id: str | None, parent_gen_id: str | None,
                         width=None, height=None, duration=None) -> dict:
    """Write a staged media file + thumbnail and build its record. No sessions I/O —
    the caller appends it under the lock (keeps the lock off slow downloads)."""
    gen_id = "gen_" + secrets.token_hex(8)
    media_path = _staged_media_path(gen_id, ext)
    media_path.parent.mkdir(parents=True, exist_ok=True)
    media_path.write_bytes(raw_bytes)
    # Capture image dimensions so the UI can size things (e.g. a match-source preview).
    if media_type == "image" and not (width and height):
        try:
            import io as _io
            from PIL import Image as _Image
            with _Image.open(_io.BytesIO(raw_bytes)) as _im:
                width, height = _im.size
        except Exception:
            pass
    try:
        thumb = _staged_thumb_path(gen_id)
        thumb.parent.mkdir(parents=True, exist_ok=True)
        if media_type == "video":
            thumbgen.make_video_thumb(media_path, thumb)
        else:
            thumbgen.make_image_thumb(media_path, thumb)
    except Exception as exc:  # pragma: no cover - thumbnail is best-effort
        print(f"imagine staging thumbnail failed: {exc}")
    return {
        "gen_id": gen_id,
        "session_id": session_id,
        "media_type": media_type,
        "ext": ext,
        "prompt": prompt or "",
        "model": model,
        "params": params or {},
        "source_gallery_id": source_gallery_id or None,
        "parent_gen_id": parent_gen_id or None,
        "created_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "width": width, "height": height, "duration": duration,
        "saved": False, "saved_media_id": None,
    }


def _append_generations(session_id: str, records: list) -> None:
    if not records:
        return
    with _sessions_lock:
        sessions = _load_sessions()
        sess = _ensure_session(sessions, session_id)
        sess["generations"].extend(records)
        _save_sessions(sessions)


def _save_parent_for(sessions: dict, gen: dict):
    """Gallery parent_id for a saved generation: the nearest already-saved ancestor,
    else the session's root gallery source, else None."""
    pid = gen.get("parent_gen_id")
    if pid:
        _, parent = _find_gen(sessions, pid)
        if parent and parent.get("saved_media_id"):
            return parent["saved_media_id"]
    return gen.get("source_gallery_id") or None


@app.post("/api/imagine/image")
def api_imagine_image() -> Response:
    """Generate image(s) via Grok Imagine into the active session's staging history
    (NOT the gallery — Save promotes a generation). With a ``source`` (a gallery image
    id, or a staged ``gen_*`` id to branch off) it's an edit/alteration."""
    api_key = _xai_api_key()
    if not api_key:
        return jsonify(ok=False, error="No xAI API key configured. Add one in Config → Grok Imagine API."), 400
    payload = request.get_json(silent=True) or {}
    prompt = str(payload.get("prompt") or "").strip()
    source = str(payload.get("source") or "").strip()
    if not prompt and not source:
        return jsonify(ok=False, error="A prompt is required."), 400
    session_id = str(payload.get("session_id") or "").strip() or (
        "scratch" if source.startswith("gen_") else _session_id_for(source))
    try:
        n = max(1, min(4, int(payload.get("n") or 1)))
    except (TypeError, ValueError):
        n = 1
    aspect_ratio = str(payload.get("aspect_ratio") or "").strip() or _xai_image_aspect()
    resolution = str(payload.get("resolution") or "").strip() or _xai_image_resolution()
    model = _xai_image_model()
    source_gallery_id = session_id[4:] if session_id.startswith("src:") else ""

    try:
        with _sessions_lock:
            sessions = _load_sessions()
            data_uri, parent_gen_id, err = _resolve_imagine_source(sessions, source)
    except CorruptStateError as exc:
        return jsonify(ok=False, error=str(exc)), 503
    if err:
        return err

    try:
        # Ask for inline base64 so the image comes back in the API response itself —
        # no second request to a CDN URL that can 403 / expire.
        results = xai_imagine.generate_images(
            api_key, model=model, prompt=prompt, n=n,
            aspect_ratio=aspect_ratio, resolution=resolution,
            image_data_uri=data_uri, response_format="b64_json",
        )
    except xai_imagine.XaiError as exc:
        return jsonify(ok=False, error=exc.message or "Image generation failed.", code=exc.code), 502

    params = {"mode": "edit" if data_uri else "text", "aspect_ratio": aspect_ratio,
              "resolution": resolution, "n": n, "source": source or None}
    records: list = []
    skipped = 0
    for item in results:
        try:
            raw, ext = _xai_image_payload_bytes(item)
        except xai_imagine.XaiError:
            # One image had no usable data (often moderation) — skip it and keep the
            # rest of the batch rather than failing the whole request. This is why
            # n>1 could silently yield nothing if any single image came back empty.
            skipped += 1
            continue
        records.append(_write_staged_record(
            raw_bytes=raw, ext=ext, media_type="image", prompt=prompt, model=model,
            params=params, session_id=session_id, source_gallery_id=source_gallery_id,
            parent_gen_id=parent_gen_id))
    print(f"imagine image: requested n={n}, xAI returned {len(results)}, staged {len(records)}, skipped {skipped}")
    if not records:
        return jsonify(ok=False, code="empty", error=(
            "No images came back — they may have been filtered by moderation. "
            "Try a different prompt, or generate fewer at once.")), 502
    _append_generations(session_id, records)
    return jsonify(ok=True, session_id=session_id, skipped=skipped,
                   generations=[_gen_public(r) for r in records])


@app.post("/api/imagine/upload")
def api_imagine_upload() -> Response:
    """Stage an uploaded image into a workspace as if it were generated, so it can be
    edited or animated like any other generation. Multipart: ``session_id`` + ``file``."""
    session_id = str(request.form.get("session_id") or "").strip() or "scratch"
    f = request.files.get("file")
    if f is None:
        return jsonify(ok=False, error="No file uploaded."), 400
    raw = f.read()
    if not raw:
        return jsonify(ok=False, error="The uploaded file was empty."), 400
    if len(raw) > 30 * 1024 * 1024:
        return jsonify(ok=False, error="Image is too large (max 30 MB)."), 413
    ext = _sniff_image_ext(raw, default="")
    if ext not in ("jpg", "png", "webp", "gif"):
        # Transcode anything else Pillow can read (bmp/tiff/heic/…) to JPEG.
        try:
            import io as _io
            from PIL import Image as _Image
            with _Image.open(_io.BytesIO(raw)) as _im:
                buf = _io.BytesIO()
                _im.convert("RGB").save(buf, "JPEG", quality=92)
                raw, ext = buf.getvalue(), "jpg"
        except Exception:
            return jsonify(ok=False, error="That doesn't look like an image. Use a JPEG, PNG, or WebP."), 415
    rec = _write_staged_record(
        raw_bytes=raw, ext=ext, media_type="image", prompt="", model="",
        params={"mode": "upload", "source": None},
        # An uploaded original isn't derived from anything — no parent link (so saving it
        # never mislinks it as a "generated" child of the workspace's source image).
        session_id=session_id, source_gallery_id=None, parent_gen_id=None)
    _append_generations(session_id, [rec])
    return jsonify(ok=True, session_id=session_id, generation=_gen_public(rec))


# Per-session video jobs: a registry keyed by session_id so several workspaces can
# render at once. A semaphore caps concurrent renders; each session is single-flight.
IMAGINE_VIDEO_CONCURRENCY = max(1, int(os.environ.get("IMAGINE_VIDEO_CONCURRENCY", "5") or 5))
_imagine_lock = threading.Lock()
_imagine_jobs: dict[str, dict] = {}
_imagine_video_sem = threading.Semaphore(IMAGINE_VIDEO_CONCURRENCY)
_IMAGINE_JOB_KEYS = ("session_id", "running", "job_id", "status", "detail",
                     "progress", "error", "result", "acknowledged",
                     "started_at", "finished_at")


def _imagine_job_default(session_id: str) -> dict:
    return {"session_id": session_id, "running": False, "job_id": "", "status": "idle",
            "detail": "", "progress": 0.0, "error": "", "result": None,
            "acknowledged": True, "started_at": "", "finished_at": ""}


def _imagine_job_set(session_id: str, **kw) -> None:
    with _imagine_lock:
        job = _imagine_jobs.setdefault(session_id, _imagine_job_default(session_id))
        job.update(kw)


def _imagine_video_worker(*, session_id: str, api_key: str, model: str, prompt: str,
                          image_data_uri: str, source_gallery_id: str, parent_gen_id: str | None,
                          duration: int, aspect_ratio: str, resolution: str, source: str) -> None:
    try:
        # Wait for a concurrency slot (status stays "queued" while blocked here).
        with _imagine_video_sem:
            _imagine_job_set(session_id, status="submitting", detail="Submitting to xAI…", progress=0.05)
            request_id = xai_imagine.start_video(
                api_key, model=model, prompt=prompt, image_data_uri=image_data_uri,
                duration=duration, aspect_ratio=aspect_ratio, resolution=resolution,
            )
            _imagine_job_set(session_id, status="pending",
                             detail="Generating… this can take a few minutes.", progress=0.1)
            start = time.monotonic()
            deadline = start + 15 * 60  # xAI's default poll timeout
            video_url = None
            vid_duration = None
            while True:
                if time.monotonic() > deadline:
                    raise xai_imagine.XaiError("Timed out waiting for the video (15 min).")
                data = xai_imagine.poll_video(api_key, request_id)
                status = str(data.get("status") or "").lower()
                if status == "done":
                    video = data.get("video") or {}
                    video_url = video.get("url")
                    vid_duration = video.get("duration")
                    break
                if status in ("failed", "expired"):
                    err = data.get("error") or {}
                    raise xai_imagine.XaiError(
                        err.get("message") or f"Video generation {status}.",
                        code=err.get("code") or status,
                    )
                elapsed = int(time.monotonic() - start)
                _imagine_job_set(session_id, status="pending",
                                 detail=f"Generating… {elapsed // 60}:{elapsed % 60:02d} elapsed",
                                 progress=min(0.9, 0.1 + elapsed / 600.0))
                time.sleep(5)
            if not video_url:
                raise xai_imagine.XaiError("xAI reported done but returned no video URL.")
            _imagine_job_set(session_id, status="saving", detail="Downloading…", progress=0.95)
            raw, ctype = xai_imagine.fetch_bytes(video_url)
            ext = _ext_for_content_type(ctype, video_url, default="mp4")
            rec = _write_staged_record(
                raw_bytes=raw, ext=ext, media_type="video", prompt=prompt, model=model,
                params={
                    "mode": "image-to-video" if image_data_uri else "text-to-video",
                    "duration": vid_duration or duration, "aspect_ratio": aspect_ratio,
                    "resolution": resolution, "source": source or None, "request_id": request_id,
                },
                session_id=session_id, source_gallery_id=source_gallery_id,
                parent_gen_id=parent_gen_id, duration=vid_duration or duration)
            _append_generations(session_id, [rec])
            _imagine_job_set(session_id, status="done", detail="Done", progress=1.0,
                             result=_gen_public(rec), error="", acknowledged=False)
    except xai_imagine.XaiError as exc:
        _imagine_job_set(session_id, status="error", error=exc.message[:400], detail="Generation failed")
    except Exception as exc:  # pragma: no cover - defensive
        _imagine_job_set(session_id, status="error", error=str(exc)[:400], detail="Generation failed")
    finally:
        with _imagine_lock:
            job = _imagine_jobs.get(session_id)
            if job:
                job["running"] = False
                job["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")


@app.post("/api/imagine/video")
def api_imagine_video() -> Response:
    """Start an async Grok Imagine video into the session's staging history. Per-session
    single-flight (409 if THIS workspace is already rendering); up to
    IMAGINE_VIDEO_CONCURRENCY workspaces render at once. Poll /api/imagine/video/status."""
    api_key = _xai_api_key()
    if not api_key:
        return jsonify(ok=False, error="No xAI API key configured. Add one in Config → Grok Imagine API."), 400
    payload = request.get_json(silent=True) or {}
    prompt = str(payload.get("prompt") or "").strip()
    source = str(payload.get("source") or "").strip()
    if not prompt and not source:
        return jsonify(ok=False, error="A prompt is required."), 400
    session_id = str(payload.get("session_id") or "").strip() or (
        "scratch" if source.startswith("gen_") else _session_id_for(source))
    try:
        duration = max(1, min(15, int(payload.get("duration") or _xai_video_duration())))
    except (TypeError, ValueError):
        duration = _xai_video_duration()
    aspect_ratio = str(payload.get("aspect_ratio") or "").strip() or _xai_video_aspect()
    resolution = str(payload.get("resolution") or "").strip() or _xai_video_resolution()
    model = _xai_video_model()
    source_gallery_id = session_id[4:] if session_id.startswith("src:") else ""

    try:
        with _sessions_lock:
            sessions = _load_sessions()
            data_uri, parent_gen_id, err = _resolve_imagine_source(sessions, source)
    except CorruptStateError as exc:
        return jsonify(ok=False, error=str(exc)), 503
    if err:
        return err

    with _imagine_lock:
        job = _imagine_jobs.get(session_id)
        if job and job.get("running"):
            return jsonify(ok=False, error="This workspace already has a video generating."), 409
        job_id = secrets.token_hex(8)
        _imagine_jobs[session_id] = {
            **_imagine_job_default(session_id), "running": True, "job_id": job_id,
            "status": "queued", "detail": "Queued…", "acknowledged": False,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    try:
        threading.Thread(
            target=_imagine_video_worker, daemon=True,
            kwargs=dict(session_id=session_id, api_key=api_key, model=model, prompt=prompt,
                        image_data_uri=data_uri, source_gallery_id=source_gallery_id,
                        parent_gen_id=parent_gen_id, duration=duration, aspect_ratio=aspect_ratio,
                        resolution=resolution, source=source),
        ).start()
    except Exception:
        # Thread exhaustion etc. — don't leave the session stuck "running" (409 forever).
        with _imagine_lock:
            j = _imagine_jobs.get(session_id)
            if j and j.get("job_id") == job_id:
                _imagine_jobs.pop(session_id, None)
        return jsonify(ok=False, error="Could not start the render. Try again."), 503
    return jsonify(ok=True, job_id=job_id, session_id=session_id), 202


@app.get("/api/imagine/video/status")
def api_imagine_video_status() -> Response:
    session_id = str(request.args.get("session") or "").strip()
    with _imagine_lock:
        job = dict(_imagine_jobs.get(session_id) or _imagine_job_default(session_id))
    return jsonify(**{k: job.get(k) for k in _IMAGINE_JOB_KEYS})


@app.get("/api/imagine/jobs")
def api_imagine_jobs() -> Response:
    """All known video jobs (running + finished-unacked), for the workspace switcher."""
    with _imagine_lock:
        jobs = [{k: j.get(k) for k in _IMAGINE_JOB_KEYS} for j in _imagine_jobs.values()]
    return jsonify(jobs=jobs)


@app.post("/api/imagine/video/ack")
def api_imagine_video_ack() -> Response:
    """Acknowledge (and drop) a finished video job so its result stops being
    re-delivered by /jobs and the registry doesn't grow without bound."""
    payload = request.get_json(silent=True) or {}
    session_id = str(payload.get("session_id") or "").strip()
    with _imagine_lock:
        job = _imagine_jobs.get(session_id)
        if job and not job.get("running"):
            _imagine_jobs.pop(session_id, None)
    return jsonify(ok=True)


@app.get("/api/imagine/sessions")
def api_imagine_sessions() -> Response:
    """Summaries of every staging session with content (for the workspace switcher)."""
    try:
        with _sessions_lock:
            sessions = _load_sessions()
    except CorruptStateError as exc:
        return jsonify(ok=False, error=str(exc)), 503
    with _imagine_lock:
        running = {sid for sid, j in _imagine_jobs.items() if j.get("running")}
    out = []
    for sid, sess in sessions.items():
        gens = sess.get("generations") or []
        if not gens:
            continue
        out.append({
            "session_id": sid,
            "source": sess.get("source"),
            "count": len(gens),
            "updated_at": max((g.get("created_at") or "") for g in gens),
            "rendering": sid in running,
            "cover": f"/api/imagine/staged/{gens[-1].get('gen_id')}/thumb",
        })
    out.sort(key=lambda s: s["updated_at"], reverse=True)
    return jsonify(ok=True, sessions=out)


@app.get("/api/imagine/session")
def api_imagine_session() -> Response:
    """Load one staging session. ``?id=<session_id>`` loads any session (scratch, ws:*,
    src:*); ``?source=<gallery id>`` is shorthand for that image's ``src:`` session."""
    sid = str(request.args.get("id") or "").strip()
    source_id = str(request.args.get("source") or "").strip()
    session_id = sid or _session_id_for(source_id)
    try:
        with _sessions_lock:
            sessions = _load_sessions()
            if session_id in sessions:
                sess = sessions[session_id]
            else:
                gid = session_id[4:] if session_id.startswith("src:") else ""
                sess = {"session_id": session_id, "source": _source_info(gid), "generations": []}
    except CorruptStateError as exc:
        return jsonify(ok=False, error=str(exc)), 503
    return jsonify(ok=True, session=_session_public(sess))


@app.post("/api/imagine/save")
def api_imagine_save() -> Response:
    """Promote one staged generation into the Grokive gallery (normal ingest +
    parent_id link), leaving the staging history intact (now flagged saved)."""
    payload = request.get_json(silent=True) or {}
    gen_id = str(payload.get("gen_id") or "").strip()
    if not gen_id:
        return jsonify(ok=False, error="No generation specified."), 400
    try:
        with _sessions_lock:
            sessions = _load_sessions()
            _, gen = _find_gen(sessions, gen_id)
            if not gen:
                return jsonify(ok=False, error="Generation not found."), 404
            if gen.get("saved") and gen.get("saved_media_id"):
                rows = db.media_by_ids(DB_FILE, [gen["saved_media_id"]])
                return jsonify(ok=True, item=(rows[0] if rows else None), already=True)
            snap = dict(gen)
            parent_id = _save_parent_for(sessions, gen)
    except CorruptStateError as exc:
        return jsonify(ok=False, error=str(exc)), 503

    ext = snap.get("ext") or ("mp4" if snap.get("media_type") == "video" else "jpg")
    path = _staged_media_path(gen_id, ext)
    if not path.exists():
        return jsonify(ok=False, error="The staged file is no longer available."), 410
    # An uploaded *original* isn't AI-generated — don't badge it. Anything derived
    # from it (edit / video) is a real generation and stays badged.
    is_upload = (snap.get("params") or {}).get("mode") == "upload"
    record = _ingest_generated_media(
        raw_bytes=path.read_bytes(), ext=ext,
        media_type=snap.get("media_type") or "image", prompt=snap.get("prompt") or "",
        model=snap.get("model") or "",
        provenance={**(snap.get("params") or {}), "staged_from": gen_id},
        parent_id=parent_id, duration=snap.get("duration"),
        api_generated=not is_upload)
    with _sessions_lock:
        sessions = _load_sessions()
        _, g2 = _find_gen(sessions, gen_id)
        if g2:
            g2["saved"] = True
            g2["saved_media_id"] = record["id"]
            _save_sessions(sessions)
    rows = db.media_by_ids(DB_FILE, [record["id"]])
    return jsonify(ok=True, item=(rows[0] if rows else record))


@app.post("/api/imagine/discard")
def api_imagine_discard() -> Response:
    """Delete a single staged generation (file + thumbnail + record). A saved gallery
    copy, if any, is independent and left intact."""
    payload = request.get_json(silent=True) or {}
    gen_id = str(payload.get("gen_id") or "").strip()
    if not gen_id:
        return jsonify(ok=False, error="No generation specified."), 400
    ext = ""
    try:
        with _sessions_lock:
            sessions = _load_sessions()
            sess, gen = _find_gen(sessions, gen_id)
            if not gen:
                return jsonify(ok=False, error="Generation not found."), 404
            ext = gen.get("ext") or ""
            sess["generations"] = [g for g in sess.get("generations", []) if g.get("gen_id") != gen_id]
            _save_sessions(sessions)
    except CorruptStateError as exc:
        return jsonify(ok=False, error=str(exc)), 503
    try:
        _staged_media_path(gen_id, ext).unlink(missing_ok=True)
    except OSError:
        pass
    try:
        _staged_thumb_path(gen_id).unlink(missing_ok=True)
    except OSError:
        pass
    return jsonify(ok=True)


@app.post("/api/imagine/session/clear")
def api_imagine_session_clear() -> Response:
    """Delete a session's staged files + history. Saved gallery items are untouched."""
    payload = request.get_json(silent=True) or {}
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return jsonify(ok=False, error="No session specified."), 400
    try:
        with _sessions_lock:
            sessions = _load_sessions()
            sess = sessions.pop(session_id, None)
            if sess:
                for g in sess.get("generations", []):
                    gid = g.get("gen_id") or ""
                    if not gid:
                        continue
                    try:
                        _staged_media_path(gid, g.get("ext") or "").unlink(missing_ok=True)
                    except OSError:
                        pass
                    try:
                        _staged_thumb_path(gid).unlink(missing_ok=True)
                    except OSError:
                        pass
                _save_sessions(sessions)
    except CorruptStateError as exc:
        return jsonify(ok=False, error=str(exc)), 503
    # Drop any finished job for this session so it can't be re-delivered into the
    # now-empty workspace (a running render is left alone).
    with _imagine_lock:
        job = _imagine_jobs.get(session_id)
        if job and not job.get("running"):
            _imagine_jobs.pop(session_id, None)
    return jsonify(ok=True)


@app.get("/api/imagine/staged/<gen_id>")
def api_imagine_staged(gen_id: str) -> Response:
    """Serve a staged generation's media file (range-enabled for video playback)."""
    if not str(gen_id).startswith("gen_"):
        return jsonify(error="not found"), 404
    try:
        with _sessions_lock:
            _, gen = _find_gen(_load_sessions(), gen_id)
    except CorruptStateError:
        return jsonify(error="not found"), 404
    if not gen:
        return jsonify(error="not found"), 404
    path = _staged_media_path(gen_id, gen.get("ext") or "jpg")
    if not path.exists():
        return jsonify(error="not found"), 404
    return send_file(path, conditional=True)


@app.get("/api/imagine/staged/<gen_id>/thumb")
def api_imagine_staged_thumb(gen_id: str) -> Response:
    if not str(gen_id).startswith("gen_"):
        return jsonify(error="not found"), 404
    path = _staged_thumb_path(gen_id)
    if not path.exists():
        return jsonify(error="not found"), 404
    return send_file(path, conditional=True)


def _archive_ids(ids) -> None:
    """Add ids to the archive (stashed) set in library.json, leaving favorites intact.
    Used to auto-archive freshly imported media so it doesn't land in Recent."""
    add = {str(i) for i in ids}
    if not add:
        return
    fav, stash = _library_sets()
    _atomic_write_json(LIBRARY_FILE, {"favorites": sorted(fav), "stashed": sorted(stash | add)})


def _library_sets() -> tuple[set, set]:
    favorites: set = set()
    stashed: set = set()
    if LIBRARY_FILE.exists():
        try:
            data = json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                favorites = {str(x) for x in (data.get("favorites") or [])}
                stashed = {str(x) for x in (data.get("stashed") or [])}
        except Exception:
            pass
    return favorites, stashed


def _multi_arg(name: str) -> list[str]:
    values: list[str] = []
    for raw in request.args.getlist(name):
        values.extend(part for part in raw.split(",") if part)
    return values


def _int_arg(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


def _period_range(period: str) -> tuple[str | None, str | None]:
    """Map a named period to (start, end) ISO-date bounds (end exclusive), using
    the server's current date. Rolling windows for last-N; calendar for month/year."""
    today = datetime.date.today()
    td = datetime.timedelta
    tomorrow = (today + td(days=1)).isoformat()
    # Sub-day windows: created_at is stored as UTC ISO (Grok's createTime), so
    # compute the lower bound in UTC for accurate hour math; upper bound is open.
    hours = {"hour1": 1, "hour4": 4, "hour8": 8}.get(period)
    if hours is not None:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        return (now_utc - td(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S"), None
    if period == "today":
        return today.isoformat(), tomorrow
    if period == "yesterday":
        return (today - td(days=1)).isoformat(), today.isoformat()
    if period == "last7":
        return (today - td(days=6)).isoformat(), tomorrow
    if period == "last14":
        return (today - td(days=13)).isoformat(), tomorrow
    if period == "last30":
        return (today - td(days=29)).isoformat(), tomorrow
    if period == "month":
        return today.replace(day=1).isoformat(), tomorrow
    if period == "year":
        return today.replace(month=1, day=1).isoformat(), tomorrow
    return None, None


@app.get("/api/media")
def api_media() -> Response:
    """Paginated, filtered, full-text-searchable media for the new SPA."""
    if not DB_FILE.exists():
        rebuild_db(wait=True)
    favorites, stashed = _library_sets()
    collection_id = request.args.get("collection") or ""
    collection_ids: list[str] = []
    if collection_id:
        collection = next((c for c in _load_collections() if c.get("id") == collection_id), None)
        if collection:
            collection_ids = collection.get("ids", []) or ["__grokive_empty_collection__"]
        else:
            collection_ids = ["__grokive_missing_collection__"]
    start, end = _period_range(request.args.get("period", "all"))
    result = db.query_media(
        DB_FILE,
        view=request.args.get("view", "recent"),
        q=request.args.get("q", ""),
        tags=_multi_arg("tags"),
        models=_multi_arg("models"),
        canvas=request.args.get("canvas") or None,
        media_type=request.args.get("type", "all"),
        resolutions=_multi_arg("res"),
        sort=request.args.get("sort", "new"),
        page=_int_arg("page", 1),
        page_size=_int_arg("page_size", 120),
        favorites=favorites,
        stashed=stashed,
        collection_ids=collection_ids,
        hidden=_hidden_media_ids(collection_ids if (collection_id and collection_id in _session_unlocked()) else None),
        start=start,
        end=end,
    )
    return jsonify(result)


@app.get("/api/facets")
def api_facets() -> Response:
    if not DB_FILE.exists():
        rebuild_db(wait=True)
    favorites, stashed = _library_sets()
    collection_id = request.args.get("collection") or ""
    collection_ids: list[str] = []
    if collection_id:
        collection = next((c for c in _load_collections() if c.get("id") == collection_id), None)
        if collection:
            collection_ids = collection.get("ids", []) or ["__grokive_empty_collection__"]
        else:
            collection_ids = ["__grokive_missing_collection__"]
    start, end = _period_range(request.args.get("period", "all"))
    return jsonify(db.facets(
        DB_FILE,
        view=request.args.get("view", "recent"),
        q=request.args.get("q", ""),
        tags=_multi_arg("tags"),
        models=_multi_arg("models"),
        resolutions=_multi_arg("res"),
        canvas=request.args.get("canvas") or None,
        media_type=request.args.get("type", "all"),
        favorites=favorites,
        stashed=stashed,
        collection_ids=collection_ids,
        hidden=_hidden_media_ids(collection_ids if (collection_id and collection_id in _session_unlocked()) else None),
        start=start,
        end=end,
    ))


@app.get("/api/stats")
def api_stats() -> Response:
    """Library totals (video/image counts + summed size) plus today's and the
    current month's creation counts for the Stats panel.

    ``tz_offset`` is the viewer's minutes east of UTC (the browser sends
    ``-getTimezoneOffset()``); the day/month windows are cut on THAT clock, since
    the container itself runs UTC. Clamped to the real -12..+14 range, and junk
    falls back to 0 (UTC) rather than 400ing a read-only panel."""
    if not DB_FILE.exists():
        rebuild_db(wait=True)
    try:
        tz_offset = int(request.args.get("tz_offset", 0))
    except (TypeError, ValueError):
        tz_offset = 0
    return jsonify(db.stats(DB_FILE, max(-12 * 60, min(14 * 60, tz_offset))))


@app.post("/api/media/by-ids")
def api_media_by_ids() -> Response:
    """Resolve an ordered id list to full media records (for playlist playback)."""
    if not DB_FILE.exists():
        rebuild_db(wait=True)
    ids = (request.get_json(silent=True) or {}).get("ids")
    if not isinstance(ids, list):
        return jsonify(items=[])
    hidden = _hidden_media_ids()
    items = [it for it in db.media_by_ids(DB_FILE, ids) if str(it.get("id")) not in hidden]
    return jsonify(items=items)


@app.get("/api/media/related")
def api_media_related() -> Response:
    """Local parent/child media links for the lightbox info panel."""
    if not DB_FILE.exists():
        rebuild_db(wait=True)
    media_id = str(request.args.get("id") or "").strip()
    if not media_id:
        return jsonify(base=None, generated=[])
    data = db.media_related(DB_FILE, media_id)
    hidden = _hidden_media_ids()
    if hidden and isinstance(data, dict):
        if data.get("base") and str(data["base"].get("id")) in hidden:
            data["base"] = None
        data["generated"] = [g for g in (data.get("generated") or []) if str(g.get("id")) not in hidden]
    return jsonify(data)


# --------------------------------------------------------------------------- #
# Prompt Studio (Phase 0: corpus vocabulary mining + structured composer).
# Pure, offline — no model calls. Embeddings / LLM arrive in later phases behind
# their own optional endpoints, gated like WHISPER_SERVER_URL.
# --------------------------------------------------------------------------- #

@app.get("/api/prompts/vocabulary")
def api_prompts_vocabulary() -> Response:
    """Composer chip palettes + a browsable prompt list, mined from metadata.json.
    Cheap enough (~hundreds of prompts) to recompute per request."""
    prompts = [str(it.get("prompt") or "") for it in _metadata_index().values()]
    return jsonify(promptstudio.mine_vocabulary(prompts))


@app.post("/api/prompts/parse")
def api_prompts_parse() -> Response:
    """Split a prompt into the eight authoring slots (powers 'Remix'). With ``llm: true``
    and an LLM endpoint configured, uses the model for a cleaner split (it can break up a
    run-on clause that the regex parser can't), falling back to the heuristic on any error."""
    payload = request.get_json(silent=True) or {}
    text = str(payload.get("text") or "")
    if payload.get("llm") and _llm_url() and text.strip():
        try:
            comps = promptstudio.decompose(
                _llm_url(), _llm_model(), text,
                api_key=_llm_api_key(), extra_headers=_llm_extra_headers(),
            )
            if any(comps.values()):
                return jsonify(components=comps, via="llm")
        except Exception:
            pass  # fall through to the heuristic parser
    return jsonify(components=promptstudio.parse_components(text), via="heuristic")


@app.post("/api/prompts/compose")
def api_prompts_compose() -> Response:
    """Assemble the two Grok-Imagine prompts from slot values: the detailed ``image`` (base
    frame) prompt and the short ``motion`` (animate) prompt. Powers the live preview."""
    components = (request.get_json(silent=True) or {}).get("components")
    if not isinstance(components, dict):
        components = {}
    return jsonify(
        image=promptstudio.compose_image(components),
        motion=promptstudio.compose_motion(components),
    )


# --------------------------------------------------------------------------- #
# Prompt Studio Phase 1: local embeddings — semantic "more like this" + auto
# theme clusters. Vectors live in the durable PROMPT_DB_FILE; the build runs in a
# background thread (its own job slot) and only embeds prompts not already stored.
# --------------------------------------------------------------------------- #

_embed_lock = threading.Lock()
_embed = {"running": False, "done": 0, "total": 0, "error": None}


def _corpus_prompts() -> list[str]:
    return [str(it.get("prompt") or "") for it in _metadata_index().values()]


def _prompt_media_ids() -> dict:
    """Map prompt_hash -> representative media ids, so a neighbor/cluster prompt
    resolves to something with a thumbnail. First id per prompt wins."""
    out: dict = {}
    for item in _metadata_index().values():
        p = str(item.get("prompt") or "")
        if not p.strip():
            continue
        out.setdefault(promptstudio.prompt_hash(p), []).append(str(item.get("id")))
    return out


def _embed_worker() -> None:
    base, model = _embed_url(), _embed_model()

    def progress(done: int, total: int) -> None:
        with _embed_lock:
            _embed["done"], _embed["total"] = done, total

    try:
        promptstudio.build_embeddings(
            PROMPT_DB_FILE, _corpus_prompts(), base, model, progress=progress,
            api_key=_embed_api_key(), extra_headers=_embed_extra_headers(),
        )
        err = None
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        err = str(exc)[:300]
    with _embed_lock:
        _embed["running"] = False
        _embed["error"] = err


def _run_embed_build_inline(log) -> None:
    """Build/refresh the prompt embedding index synchronously, for Autonomous Mode's post-sync
    step. Reuses the _embed job slot (lock + state) so the Studio's build button reflects progress
    and a manual build can't run at the same time. Logs a one-line skip when not configured or
    already running; raises on a build error so the caller's step is marked failed."""
    if not _embed_url():
        log("skipped — no embeddings endpoint configured")
        return
    with _embed_lock:
        if _embed["running"]:
            log("skipped — an index build is already running")
            return
        _embed.update(running=True, done=0, total=0, error=None)

    def progress(done: int, total: int) -> None:
        with _embed_lock:
            _embed["done"], _embed["total"] = done, total

    base, model = _embed_url(), _embed_model()
    err = None
    try:
        promptstudio.build_embeddings(
            PROMPT_DB_FILE, _corpus_prompts(), base, model, progress=progress,
            api_key=_embed_api_key(), extra_headers=_embed_extra_headers(),
        )
    except Exception as exc:  # noqa: BLE001
        err = str(exc)[:300]
    finally:
        with _embed_lock:
            _embed["running"] = False
            _embed["error"] = err
            done, total = _embed["done"], _embed["total"]
    if err:
        raise RuntimeError(err)
    log(f"embedded {done}/{total} prompt(s)")


@app.post("/api/prompts/embed")
def api_prompts_embed() -> Response:
    """Kick (or no-op if already running) the incremental embedding build."""
    if not _embed_url():
        return jsonify(ok=False, error="No embeddings endpoint configured."), 400
    with _embed_lock:
        if _embed["running"]:
            return jsonify(ok=True, running=True)
        _embed.update(running=True, done=0, total=0, error=None)
    threading.Thread(target=_embed_worker, daemon=True).start()
    return jsonify(ok=True, running=True)


@app.get("/api/prompts/status")
def api_prompts_status() -> Response:
    """Embedding coverage + build job state — drives the Studio's build button."""
    model = _embed_model()
    try:
        st = promptstudio.embed_status(PROMPT_DB_FILE, _corpus_prompts(), model)
    except Exception:
        st = {"total_unique": 0, "embedded": 0, "missing": 0, "model": model}
    with _embed_lock:
        job = dict(_embed)
    return jsonify(
        embed_configured=bool(_embed_url()),
        llm_configured=bool(_llm_url()),
        running=job["running"], done=job["done"], total=job["total"], error=job["error"],
        **st,
    )


@app.get("/api/prompts/similar")
def api_prompts_similar() -> Response:
    """Prompts most semantically similar to ?text= or to the prompt of ?id=, returned
    as representative media records (one per neighbouring prompt) for the grid."""
    base, model = _embed_url(), _embed_model()
    if not base:
        return jsonify(ok=False, error="No embeddings endpoint configured.", results=[]), 400
    if not DB_FILE.exists():
        rebuild_db(wait=True)
    text = request.args.get("text") or ""
    exclude = None
    if not text and request.args.get("id"):
        item = _metadata_index().get(str(request.args.get("id")))
        if item:
            text = str(item.get("prompt") or "")
            exclude = promptstudio.prompt_hash(text)
    if not text.strip():
        return jsonify(ok=True, query="", results=[])
    hidden = _list_hidden_media_ids()
    pmap = _prompt_media_ids()
    try:
        qvec = promptstudio.embed_query(
            base, model, text,
            api_key=_embed_api_key(), extra_headers=_embed_extra_headers(),
        )
        hashes, texts, matrix = promptstudio.load_vectors(PROMPT_DB_FILE, model)
        # Drop prompts that live ONLY in hidden/locked collections before ranking — "find
        # similar" must never score against or surface locked content, regardless of any
        # session unlock. Same policy as theme clusters: a prompt shared with any visible
        # media stays (its visible representative is chosen below).
        if hidden:
            keep = [
                i for i, h in enumerate(hashes)
                if not pmap.get(h) or any(mid not in hidden for mid in pmap[h])
            ]
            if len(keep) != len(hashes):
                hashes = [hashes[i] for i in keep]
                texts = [texts[i] for i in keep]
                matrix = matrix[keep] if matrix.shape[0] else matrix
        neighbors = promptstudio.nearest(qvec, hashes, texts, matrix,
                                         k=_int_arg("k", 24), exclude_hash=exclude)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc)[:200], results=[]), 502
    # Representative media id must be VISIBLE, so a prompt shared with a hidden collection
    # never surfaces a hidden thumbnail.
    pairs = [(nb, next((mid for mid in (pmap.get(nb["prompt_hash"]) or []) if mid not in hidden), None))
             for nb in neighbors]
    pairs = [(nb, mid) for nb, mid in pairs if mid]
    records = {str(r["id"]): r for r in db.media_by_ids(DB_FILE, [mid for _, mid in pairs])}
    results = []
    for nb, mid in pairs:
        rec = records.get(str(mid))
        if rec:
            results.append({**rec, "_score": nb["score"]})
    return jsonify(ok=True, query=text[:300], results=results)


@app.get("/api/prompts/themes")
def api_prompts_themes() -> Response:
    """Auto-discovered theme/persona clusters of the prompt corpus (k-means over the
    embeddings), each with a distinctive label and a cover thumbnail."""
    base, model = _embed_url(), _embed_model()
    if not base:
        return jsonify(ok=False, error="No embeddings endpoint configured.", themes=[]), 400
    if not DB_FILE.exists():
        rebuild_db(wait=True)
    k_arg = request.args.get("k")
    k = int(k_arg) if (k_arg and k_arg.isdigit()) else None
    hidden = _list_hidden_media_ids()
    pmap = _prompt_media_ids()
    try:
        hashes, texts, matrix = promptstudio.load_vectors(PROMPT_DB_FILE, model)
        # Drop prompts that exist only in hidden collections from the corpus before
        # clustering — unconditionally, regardless of any session unlock. This is a
        # global discovery surface (like Recent / All Media / facets), where unlocking
        # a collection lets you open it but never surfaces its contents here. A prompt
        # shared with any visible media stays.
        if hidden:
            keep = [
                i for i, h in enumerate(hashes)
                if not pmap.get(h) or any(mid not in hidden for mid in pmap[h])
            ]
            if len(keep) != len(hashes):
                hashes = [hashes[i] for i in keep]
                texts = [texts[i] for i in keep]
                matrix = matrix[keep] if matrix.shape[0] else matrix
        clusters = promptstudio.cluster_prompts(hashes, texts, matrix, k=k)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc)[:200], themes=[]), 502
    for c in clusters:
        # Cover from a VISIBLE media id, so a prompt shared with a hidden collection
        # never surfaces a hidden thumbnail.
        ids = pmap.get(c["rep_hash"]) or []
        c["_cover_id"] = next((mid for mid in ids if mid not in hidden), None)
    cover_ids = [c["_cover_id"] for c in clusters if c["_cover_id"]]
    records = {str(r["id"]): r for r in db.media_by_ids(DB_FILE, cover_ids)}
    themes = []
    for c in clusters:
        rec = records.get(str(c["_cover_id"])) if c["_cover_id"] else None
        themes.append({
            "label": c["label"],
            "tags": c["tags"],
            "size": c["size"],
            "rep_prompt": c["rep_prompt"][:400],
            "rep_id": c["_cover_id"],
            "cover": (rec or {}).get("thumb"),
            "media_type": (rec or {}).get("media_type"),
        })
    total = int(matrix.shape[0]) if getattr(matrix, "shape", None) else 0
    return jsonify(ok=True, themes=themes, total=total)


def _style_examples(k: int = 4) -> list[str]:
    """A few of the operator's own prompts, used as few-shot style anchors for the LLM.
    Prefers recent, moderate-length prompts that contain a quoted line, so the model sees real
    spoken dialogue to imitate (the strongest anti-garble anchor) before falling back to plain."""
    items = sorted(_metadata_index().values(),
                   key=lambda it: str(it.get("created_at") or ""), reverse=True)
    with_dialogue: list[str] = []
    plain: list[str] = []
    for it in items:
        p = str(it.get("prompt") or "").strip()
        if not (60 <= len(p) <= 500):
            continue
        (with_dialogue if ('"' in p or "“" in p) else plain).append(p)
    out = with_dialogue[:k]
    if len(out) < k:
        out += plain[: k - len(out)]
    return out


@app.post("/api/prompts/generate")
def api_prompts_generate() -> Response:
    """LLM prompt variations / remix / polish (Phase 2). Needs LLM_SERVER_URL."""
    base, model = _llm_url(), _llm_model()
    if not base:
        return jsonify(ok=False, error="No LLM endpoint configured.", variations=[]), 400
    payload = request.get_json(silent=True) or {}
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return jsonify(ok=False, error="No prompt provided.", variations=[]), 400
    mode = str(payload.get("mode") or "variations")
    if mode not in ("variations", "remix", "polish"):
        mode = "variations"
    try:
        n = int(payload.get("n", 4))
    except (TypeError, ValueError):
        n = 4
    n = max(1, min(8, n))
    instruction = str(payload.get("instruction") or "")[:300]
    persona = str(payload.get("persona") or "")[:4000]
    try:
        variations = promptstudio.generate(
            base, model, prompt=prompt, mode=mode, n=n,
            instruction=instruction, examples=_style_examples(), persona=persona,
            api_key=_llm_api_key(), extra_headers=_llm_extra_headers(),
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc)[:200], variations=[]), 502
    return jsonify(ok=True, variations=variations, model=model)


@app.post("/api/prompts/from-image")
def api_prompts_from_image() -> Response:
    """Vision: describe one image as a ready-to-paste Grok Imagine prompt (the lightbox
    lightning-bolt). Needs a multimodal LLM — set ``llm_vision_model`` / ``LLM_VISION_MODEL``
    to a VLM such as a Qwen3-VL build; it falls back to the chat model. Body: ``{"id": ...}``."""
    base = _llm_url()
    if not base:
        return jsonify(ok=False, error="No LLM endpoint configured.", prompt=""), 400
    payload = request.get_json(silent=True) or {}
    media_id = str(payload.get("id") or "").strip()
    if not media_id:
        return jsonify(ok=False, error="No image id provided.", prompt=""), 400
    path, item = _image_path_for_id(media_id)
    if item is None:
        return jsonify(ok=False, error="Unknown media id.", prompt=""), 404
    if item.get("media_type") == "video":
        return jsonify(ok=False, error="This works on images, not videos.", prompt=""), 400
    if path is None:
        return jsonify(ok=False, error="Image file not found on disk.", prompt=""), 404
    try:
        image_b64, mime = _image_b64_for_vision(path)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=f"Could not read the image: {exc}"[:200], prompt=""), 500
    model = _llm_vision_model()
    try:
        prompt = promptstudio.describe_for_grok(
            base, model, image_b64=image_b64, mime=mime,
            stored_prompt=str(item.get("prompt") or ""),
            api_key=_llm_api_key(), extra_headers=_llm_extra_headers(),
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc)[:200], prompt=""), 502
    if not prompt:
        return jsonify(ok=False, error="The vision model returned no usable description.", prompt=""), 502
    return jsonify(ok=True, prompt=prompt, model=model)


@app.post("/api/prompts/enhance")
def api_prompts_enhance() -> Response:
    """Enhance one saved prompt into a richer prompt. Needs LLM_SERVER_URL."""
    base, model = _llm_url(), _llm_model()
    if not base:
        return jsonify(ok=False, error="No LLM endpoint configured.", prompt=""), 400
    payload = request.get_json(silent=True) or {}
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return jsonify(ok=False, error="No prompt provided.", prompt=""), 400
    dialogue_level = str(payload.get("dialogue_level") or "normal").strip().lower()
    if dialogue_level not in ("normal", "dirtier", "filthier"):
        dialogue_level = "normal"
    dialogue_only = bool(payload.get("dialogue_only"))
    try:
        enhanced = promptstudio.enhance_prompt(
            base, model, prompt=prompt, dialogue_level=dialogue_level, examples=_style_examples(),
            dialogue_only=dialogue_only,
            api_key=_llm_api_key(), extra_headers=_llm_extra_headers(),
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc)[:200], prompt=""), 502
    return jsonify(ok=True, prompt=enhanced, dialogue_level=dialogue_level, dialogue_only=dialogue_only, model=model)


@app.post("/api/prompts/autotag")
def api_prompts_autotag() -> Response:
    """Suggest a folder + tags for one saved prompt via the local LLM. Needs LLM_SERVER_URL.
    The caller passes its existing folders/tags so suggestions reuse them and stay consistent."""
    base, model = _llm_url(), _llm_model()
    if not base:
        return jsonify(ok=False, error="No LLM endpoint configured.", folder="", tags=[]), 400
    payload = request.get_json(silent=True) or {}
    text = str(payload.get("text") or "").strip()
    if not text:
        return jsonify(ok=False, error="No prompt text provided.", folder="", tags=[]), 400
    folders = payload.get("folders") if isinstance(payload.get("folders"), list) else []
    tags = payload.get("tags") if isinstance(payload.get("tags"), list) else []
    try:
        out = promptstudio.suggest_labels(
            base, model, prompt=text, folders=folders, tags=tags,
            api_key=_llm_api_key(), extra_headers=_llm_extra_headers(),
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc)[:200], folder="", tags=[]), 502
    return jsonify(ok=True, folder=out.get("folder", ""), tags=out.get("tags", []), model=model)


@app.post("/api/prompts/audit-labels")
def api_prompts_audit_labels() -> Response:
    """Review one saved prompt's existing folder/tags and suggest corrections via the local LLM."""
    base, model = _llm_url(), _llm_model()
    if not base:
        return jsonify(ok=False, error="No LLM endpoint configured.", folder="", tags=[], remove_tags=[]), 400
    payload = request.get_json(silent=True) or {}
    text = str(payload.get("text") or "").strip()
    if not text:
        return jsonify(ok=False, error="No prompt text provided.", folder="", tags=[], remove_tags=[]), 400
    folders = payload.get("folders") if isinstance(payload.get("folders"), list) else []
    tags = payload.get("tags") if isinstance(payload.get("tags"), list) else []
    current_tags = payload.get("current_tags") if isinstance(payload.get("current_tags"), list) else []
    folder = str(payload.get("folder") or "").strip()
    try:
        out = promptstudio.audit_labels(
            base, model, prompt=text, folder=folder, current_tags=current_tags, folders=folders, tags=tags,
            api_key=_llm_api_key(), extra_headers=_llm_extra_headers(),
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc)[:200], folder="", tags=[], remove_tags=[]), 502
    return jsonify(
        ok=True,
        folder=out.get("folder", ""),
        tags=out.get("tags", []),
        remove_tags=out.get("remove_tags", []),
        reason=out.get("reason", ""),
        model=model,
    )


@app.post("/api/prompts/scene")
def api_prompts_scene() -> Response:
    """Script a continuous multi-clip scene from a base. Grok chains ~6s/10s clips, so the
    target length is divided by the chosen increment to size the beat count."""
    base, model = _llm_url(), _llm_model()
    if not base:
        return jsonify(ok=False, error="No LLM endpoint configured.", beats=[]), 400
    payload = request.get_json(silent=True) or {}
    base_prompt = str(payload.get("base") or "").strip()
    if not base_prompt:
        return jsonify(ok=False, error="No base scene provided.", beats=[]), 400
    try:
        length = int(payload.get("length_seconds", 60))
    except (TypeError, ValueError):
        length = 60
    length = max(6, min(600, length))
    increment = 10 if int(payload.get("increment", 10) or 10) == 10 else 6
    beats = max(1, min(24, -(-length // increment)))  # ceil(length / increment), capped
    instruction = str(payload.get("instruction") or "")[:300]
    persona = str(payload.get("persona") or "")[:4000]
    anchor = str(payload.get("anchor") or "")[:200]
    detail = "detailed" if str(payload.get("detail") or "").lower() == "detailed" else "concise"
    arc = bool(payload.get("arc"))
    try:
        out = promptstudio.generate_scene(
            base, model, base_prompt=base_prompt, beats=beats,
            increment=increment, instruction=instruction, examples=_style_examples(), persona=persona,
            anchor=anchor, detail=detail, arc=arc,
            api_key=_llm_api_key(), extra_headers=_llm_extra_headers(),
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc)[:200], beats=[]), 502
    return jsonify(ok=True, beats=out, clips=beats, increment=increment, length_seconds=length, model=model)


@app.get("/api/prompts/scenes")
def api_prompts_scenes_get() -> Response:
    """Saved Scene Builder scenes ([] if none/unreadable). Stored on the data volume so they
    persist across container recreation and are shared by every device."""
    data: list = []
    if SCENES_FILE.exists():
        try:
            loaded = json.loads(SCENES_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                data = loaded
        except Exception:
            data = []
    return jsonify(scenes=data)


@app.post("/api/prompts/scenes")
def api_prompts_scenes_post() -> Response:
    """Replace the whole saved-scenes list (the client owns it and sends it in full). Validate,
    normalise, atomic write."""
    incoming = (request.get_json(silent=True) or {}).get("scenes")
    if not isinstance(incoming, list):
        return jsonify(ok=False, error="Expected a 'scenes' array."), 400
    clean = []
    for entry in incoming[:200]:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()[:120]
        beats = entry.get("beats")
        if not name or not isinstance(beats, list):
            continue
        try:
            length = int(entry.get("length_seconds") or 0)
        except (TypeError, ValueError):
            length = 0
        clean.append({
            "id": str(entry.get("id") or "")[:64] or name,
            "name": name,
            "base": str(entry.get("base", ""))[:4000],
            "instruction": str(entry.get("instruction", ""))[:300],
            "anchor": str(entry.get("anchor", ""))[:200],
            "detail": "detailed" if str(entry.get("detail", "")).lower() == "detailed" else "concise",
            "arc": bool(entry.get("arc")),
            "length_seconds": length,
            "increment": 6 if int(entry.get("increment") or 10) == 6 else 10,
            "beats": [str(b)[:1000] for b in beats][:64],
            "created_at": str(entry.get("created_at", ""))[:32],
        })
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(SCENES_FILE, clean)
    return jsonify(ok=True, count=len(clean))


@app.post("/api/prompts/freeform")
def api_prompts_freeform() -> Response:
    """Direct, unconstrained generation in the active persona's voice — no beat/JSON scaffolding."""
    base, model = _llm_url(), _llm_model()
    if not base:
        return jsonify(ok=False, error="No LLM endpoint configured.", items=[]), 400
    payload = request.get_json(silent=True) or {}
    instruction = str(payload.get("instruction") or "").strip()[:1000]
    if not instruction:
        return jsonify(ok=False, error="No instruction provided.", items=[]), 400
    persona = str(payload.get("persona") or "")[:4000]
    prefix = str(payload.get("prefix") or "")[:200]
    try:
        n = int(payload.get("n", 0))
    except (TypeError, ValueError):
        n = 0
    n = max(0, min(30, n))
    try:
        items = promptstudio.generate_freeform(
            base, model, instruction=instruction, persona=persona, n=n, prefix=prefix,
            api_key=_llm_api_key(), extra_headers=_llm_extra_headers(),
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc)[:200], items=[]), 502
    return jsonify(ok=True, items=items, model=model)


@app.get("/api/prompts/freeform-presets")
def api_prompts_freeform_presets_get() -> Response:
    """Saved Freeform request presets ([] if none/unreadable). Stores the request, the required
    repeated text/prefix, and the count so common asks can be reused across devices."""
    data: list = []
    if FREEFORM_PRESETS_FILE.exists():
        try:
            loaded = json.loads(FREEFORM_PRESETS_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                data = loaded
        except Exception:
            data = []
    return jsonify(presets=data)


@app.post("/api/prompts/freeform-presets")
def api_prompts_freeform_presets_post() -> Response:
    """Replace the whole Freeform preset list (client owns it). Validate, normalise, atomic write."""
    incoming = (request.get_json(silent=True) or {}).get("presets")
    if not isinstance(incoming, list):
        return jsonify(ok=False, error="Expected a 'presets' array."), 400
    clean = []
    for entry in incoming[:200]:
        if not isinstance(entry, dict):
            continue
        instruction = str(entry.get("instruction", "")).strip()[:1000]
        prefix = str(entry.get("prefix", "")).strip()[:200]
        name = str(entry.get("name", "")).strip()[:80]
        if not instruction and not prefix:
            continue
        try:
            count = int(entry.get("count") or 10)
        except (TypeError, ValueError):
            count = 10
        count = max(1, min(30, count))
        clean.append({
            "id": str(entry.get("id") or "")[:64] or str(len(clean)),
            "name": name or (instruction[:60] if instruction else prefix[:60]),
            "instruction": instruction,
            "prefix": prefix,
            "count": count,
            "created_at": str(entry.get("created_at", ""))[:32],
        })
    _atomic_write_json(FREEFORM_PRESETS_FILE, clean)
    return jsonify(ok=True, count=len(clean))


@app.get("/api/prompts/responses")
def api_prompts_responses_get() -> Response:
    """Saved Prompt Studio responses ([] if none/unreadable). On the data volume, shared across devices.

    There is no server-side search, so this ships the WHOLE library in one body — several MB once you
    have tens of thousands of saved prompts. The Firefox extension re-reads it whenever its in-memory
    copy has lapsed (its MV2 background page is non-persistent, so that happens often), which made a
    single 🎲 roll cost a full download. An ETag keyed on the file's mtime+size lets the browser's own
    HTTP cache answer those re-reads with a 304 and no body.

    ``no-cache`` means "stored, but revalidate every time" — never serve blind. That keeps the client
    correct against the writers it cannot observe (autonomous tagging, /import-library, a backup
    restore), because every one of them goes through ``_atomic_write_json`` and so moves mtime+size.

    The stat→read→stat sandwich is what makes the validator honest: ``_atomic_write_json`` publishes
    by rename, so a read always returns one complete version, but if a write lands mid-read we cannot
    tell WHICH version we just served — so we ship it with no validator rather than pin the wrong one.
    """
    data: list = []
    etag: str | None = None
    if RESPONSES_FILE.exists():
        try:
            before = RESPONSES_FILE.stat()
            loaded = json.loads(RESPONSES_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                data = loaded
                after = RESPONSES_FILE.stat()
                if (before.st_mtime_ns, before.st_size) == (after.st_mtime_ns, after.st_size):
                    etag = f"{before.st_mtime_ns:x}-{before.st_size:x}"
        except Exception:
            data = []          # unreadable/corrupt: serve the empty fallback, never pin it
            etag = None
    resp = jsonify(responses=data)
    resp.headers["Cache-Control"] = "private, no-cache"
    if etag:
        resp.set_etag(etag, weak=True)
        return resp.make_conditional(request)
    return resp


# Serializes EVERY read-modify-write of saved_responses.json. waitress is multithreaded, so
# without this two concurrent writers (e.g. autonomous tagging + a user save) interleave between
# their read and their write and silently lose updates. Hold it across the WHOLE read→write, not
# just the write — _atomic_write_json only prevents torn files, not lost updates.
_responses_lock = threading.Lock()


@app.post("/api/prompts/responses")
def api_prompts_responses_post() -> Response:
    """Apply a client's saved-responses list as an UPSERT that can never shrink the file.

    Records are updated/inserted by id in the order given; any record currently on disk whose id
    the client did NOT send is preserved (appended). This makes a stale or truncated client POST
    harmless — it can add or reorder, but it can no longer wipe records it didn't know about. (The
    old behaviour blindly replaced the file AND capped it at 2000 entries, so any edit made with
    >2000 saved silently destroyed everything past 2000.) Deletion has its own by-id endpoint,
    since an upsert intentionally never removes."""
    incoming = (request.get_json(silent=True) or {}).get("responses")
    if not isinstance(incoming, list):
        return jsonify(ok=False, error="Expected a 'responses' array."), 400
    clean = []
    seen_ids = set()
    for entry in incoming:  # no cap — the client holds the full list and round-trips it whole
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text", "")).strip()[:SAVED_PROMPT_TEXT_LIMIT]
        if not text:
            continue
        # Tags: a deduped list of short lowercase labels (cross-cutting). Folder: a single bucket.
        tags: list = []
        raw_tags = entry.get("tags")
        if isinstance(raw_tags, list):
            for t in raw_tags:
                s = str(t).strip().lower()[:24]
                if s and s not in tags:
                    tags.append(s)
                if len(tags) >= 20:
                    break
        rid = str(entry.get("id") or "")[:64] or ("rs-" + secrets.token_hex(6))
        clean.append({
            "id": rid,
            "text": text,
            "created_at": str(entry.get("created_at", ""))[:32],
            "folder": str(entry.get("folder", "")).strip()[:40],
            "tags": tags,
            "starred": bool(entry.get("starred")),
        })
        seen_ids.add(rid)
    with _responses_lock:
        current = _read_responses()
        # Never shrink: keep any on-disk record the client didn't send (a stale/truncated client,
        # or a row another writer appended since the client last loaded).
        preserved = [r for r in current if str(r.get("id")) not in seen_ids]
        merged = clean + preserved
        _atomic_write_json(RESPONSES_FILE, merged)
    return jsonify(ok=True, count=len(merged))


@app.post("/api/prompts/responses/add")
def api_prompts_responses_add() -> Response:
    """Append ONE saved response server-side (read-modify-write) and return the full list.

    Unlike the full-list POST above, this NEVER overwrites the file with the caller's view —
    so a context that hasn't loaded the list (e.g. the lightbox 'Describe for Grok') can add a
    prompt without wiping the others. Deduplicates by exact text."""
    payload = request.get_json(silent=True) or {}
    text = str(payload.get("text") or "").strip()[:SAVED_PROMPT_TEXT_LIMIT]
    if not text:
        return jsonify(ok=False, error="No text provided.", responses=[]), 400
    folder = str(payload.get("folder") or "").strip()[:40]
    starred = bool(payload.get("starred"))
    with _responses_lock:
        current = _read_responses()
        existing = next((x for x in current if str(x.get("text", "")).strip() == text), None)
        if existing is not None:
            # already saved — leave folder/tags as-is, but UPGRADE to starred if requested.
            if starred and not existing.get("starred"):
                existing["starred"] = True
                _atomic_write_json(RESPONSES_FILE, current)
                return jsonify(ok=True, added=False, starred=True, responses=current)
            return jsonify(ok=True, added=False, responses=current)
        entry = {
            "id": "rs-" + secrets.token_hex(6),
            "text": text,
            "created_at": datetime.date.today().isoformat(),
            "folder": folder,
            "tags": [],
            "starred": starred,
        }
        current = [entry] + current
        _atomic_write_json(RESPONSES_FILE, current)
        return jsonify(ok=True, added=True, responses=current)


@app.post("/api/prompts/responses/star")
def api_prompts_responses_star() -> Response:
    """Set the ``starred`` flag on ONE saved response by id (read-modify-write) and return the
    full list. Starring is independent of folder — only the boolean changes. Backward compatible:
    a missing ``starred`` on a record reads as False."""
    payload = request.get_json(silent=True) or {}
    rid = str(payload.get("id") or "").strip()
    if not rid:
        return jsonify(ok=False, error="No prompt id provided.", responses=[]), 400
    with _responses_lock:
        current = _read_responses()
        record = next((x for x in current if str(x.get("id", "")) == rid), None)
        if record is None:
            return jsonify(ok=False, error="Unknown prompt id.", responses=current), 404
        record["starred"] = bool(payload.get("starred"))
        _atomic_write_json(RESPONSES_FILE, current)
        return jsonify(ok=True, responses=current)


@app.post("/api/prompts/responses/delete")
def api_prompts_responses_delete() -> Response:
    """Delete ONE saved response by id (read-modify-write) and return the full list. Separate from
    the full-list upsert, which intentionally never removes — so deletes go through here."""
    rid = str((request.get_json(silent=True) or {}).get("id") or "").strip()
    if not rid:
        return jsonify(ok=False, error="No prompt id provided.", responses=[]), 400
    with _responses_lock:
        current = _read_responses()
        remaining = [r for r in current if str(r.get("id")) != rid]
        if len(remaining) != len(current):
            _atomic_write_json(RESPONSES_FILE, remaining)
        return jsonify(ok=True, responses=remaining)


def _library_unique_prompts(exclude_hidden: bool = False) -> dict:
    """Map normalized-prompt hash -> {text, created, visible} over every media prompt in
    metadata.json, deduped (trivially-different wordings collapse, matching Prompt Studio's
    unique count). When ``exclude_hidden`` is set, prompts that live ONLY in locked/hidden
    collections are dropped before returning — a prompt shared with any visible media stays
    (the same policy as the discovery surfaces: themes, find-similar)."""
    uniq: dict = {}
    if not METADATA_FILE.exists():
        return uniq
    try:
        items = json.loads(METADATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return uniq
    hidden = _list_hidden_media_ids() if exclude_hidden else set()
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        # Only import prompts from media that came from Grok (synced/downloaded) or
        # was generated in-app — never from items UPLOADED to Grokive, whose "prompt"
        # is just a filename (folder import) or empty (uploaded original). Folder
        # imports carry `imported: True`; uploaded originals carry `api_generated: False`
        # (use `is False`, not a bare `not` — Grok records have no api_generated key).
        if it.get("imported") or it.get("api_generated") is False:
            continue
        text = str(it.get("prompt") or "").strip()
        if not text:
            continue
        h = promptstudio.prompt_hash(text)
        if not h:
            continue
        created = str(it.get("created_at") or "")
        visible = str(it.get("id")) not in hidden  # always True when exclude_hidden is off
        row = uniq.get(h)
        if row is None:
            uniq[h] = {"text": text, "created": created, "visible": visible}
        else:
            if created > row["created"]:
                row["created"] = created
            if visible:
                row["visible"] = True
    if exclude_hidden:
        return {h: r for h, r in uniq.items() if r.get("visible")}
    return uniq


def _read_responses() -> list:
    """Saved Prompt Studio responses as a list of dicts ([] if none/unreadable)."""
    if RESPONSES_FILE.exists():
        try:
            loaded = json.loads(RESPONSES_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                return [x for x in loaded if isinstance(x, dict)]
        except Exception:
            pass
    return []


def _import_library_into_saved(folder: str = "Library", *, exclude_hidden: bool = False) -> tuple[list, list, str]:
    """Merge the media library's prompts (metadata.json) into Saved responses, adding only those
    not already saved. The dedup key is the normalized-prompt hash of the text AS IT IS STORED —
    i.e. after the saved-prompt storage cap — because that's the only form a future run can see in
    the file. Hashing the FULL library text instead made every over-limit prompt permanently
    "new": its stored truncation hashes differently, so each sync re-imported (and re-tagged)
    the same prompts forever, one duplicate copy per sync.

    Exact-duplicate records that accumulated that way (identical stored-text hash) are collapsed
    on the way through: the FIRST copy is kept (the original import — already tagged), later
    copies' tags/starred are unioned into it. Only records whose normalized text is identical
    ever merge, so this can't collapse two genuinely different prompts. Backs up the current
    list before any write. Returns ``(merged, new_entries, backup_path)`` — ``new_entries`` is
    the delta added this call (empty list + no write when nothing is new and nothing collapsed).
    Shared by the manual import endpoint and Autonomous Mode's post-sync step."""
    with _responses_lock:
        current = _read_responses()
        seen: dict = {}   # stored-text hash -> the kept (first) record
        deduped: list = []
        for x in current:
            t = str(x.get("text", "")).strip()
            if not t:
                deduped.append(x)  # blank text: not ours to judge, keep as-is
                continue
            h = promptstudio.prompt_hash(t)
            kept = seen.get(h)
            if kept is None:
                seen[h] = x
                deduped.append(x)
                continue
            if not isinstance(kept.get("tags"), list):
                kept["tags"] = []
            for tag in (x.get("tags") or []):
                if isinstance(tag, str) and tag and tag not in kept["tags"]:
                    kept["tags"].append(tag)
            if x.get("starred") and not kept.get("starred"):
                kept["starred"] = True
            if not str(kept.get("folder") or "").strip() and str(x.get("folder") or "").strip():
                kept["folder"] = x["folder"]
        collapsed = len(current) - len(deduped)
        uniq = _library_unique_prompts(exclude_hidden=exclude_hidden)
        # Membership test + in-batch dedup both keyed by the hash of the CAPPED text —
        # the exact string the entry below stores, so the next run's file matches it.
        missing: dict = {}
        for r in uniq.values():
            stored_text = r["text"][:SAVED_PROMPT_TEXT_LIMIT]
            stored_h = promptstudio.prompt_hash(stored_text)
            if stored_h not in seen and stored_h not in missing:
                missing[stored_h] = r
        if not missing and not collapsed:
            return current, [], ""
        rows = sorted(missing.values(), key=lambda r: r["created"], reverse=True)  # newest first
        today = datetime.date.today().isoformat()
        new_entries = [{
            "id": "rs-" + secrets.token_hex(6),
            "text": r["text"][:SAVED_PROMPT_TEXT_LIMIT],
            "created_at": (r["created"][:10] or today),
            "folder": folder,
            "tags": [],
        } for r in rows]
        merged = deduped + new_entries  # keep existing/curated on top; imports appended
        backup = _atomic_write_json(RESPONSES_FILE, merged)
        return merged, new_entries, backup


def _autotag_records(records: list, *, log=None) -> int:
    """AI-tag a set of saved-response records in place on disk (Autonomous Mode's post-sync step).
    For each record still present in saved_responses.json, asks the LLM for a folder + tags
    (reusing the existing vocabulary, exactly like the per-item autotag endpoint) and applies them.
    Tags only the records passed in — never re-tags the rest of the library. Best-effort per item:
    an LLM error on one is logged and skipped. Writes once at the end. Returns how many changed."""
    base, model = _llm_url(), _llm_model()
    if not base or not records:
        return 0
    # Do the SLOW LLM work on a snapshot WITHOUT holding the lock (it can run for minutes),
    # collecting per-id patches. We re-read and apply them under the lock at the very end, so a
    # concurrent user save/add during the loop is never clobbered by a stale full-file rewrite.
    snapshot = _read_responses()
    by_id = {str(r.get("id")): r for r in snapshot}
    folders = sorted({str(r.get("folder")).strip() for r in snapshot if str(r.get("folder")).strip()})
    vocab = sorted({t for r in snapshot for t in (r.get("tags") or []) if isinstance(t, str)})
    api_key, headers = _llm_api_key(), _llm_extra_headers()
    total = len(records)
    patches: dict = {}  # id -> {"folder": str, "add_tags": [str]}
    for i, rec in enumerate(records, 1):
        target = by_id.get(str(rec.get("id")))
        if target is None:
            continue  # vanished from the file between import and now
        text = str(target.get("text") or "").strip()
        if not text:
            continue
        if log and (i == 1 or i == total or i % 5 == 0):
            log(f"tagging {i}/{total}…")
        try:
            out = promptstudio.suggest_labels(
                base, model, prompt=text, folders=folders, tags=vocab,
                api_key=api_key, extra_headers=headers,
            )
        except Exception as exc:  # noqa: BLE001
            if log:
                log(f"tag failed for {target.get('id')}: {str(exc)[:120]}")
            continue
        new_tags = [t for t in (out.get("tags") or []) if isinstance(t, str) and t]
        folder = str(out.get("folder") or "").strip()
        patch: dict = {}
        if new_tags:
            patch["add_tags"] = new_tags
            for t in new_tags:
                if t not in vocab:
                    vocab.append(t)  # keep new vocab available to later items in this batch
        if folder:
            patch["folder"] = folder[:40]
            if folder not in folders:
                folders.append(folder)
        if patch:
            patches[str(rec.get("id"))] = patch
    if not patches:
        return 0
    # Apply the patches by id onto the LATEST file under the lock (NOT the pre-loop snapshot),
    # so anything added/edited during the long LLM loop survives.
    with _responses_lock:
        current = _read_responses()
        cur_by_id = {str(r.get("id")): r for r in current}
        tagged = 0
        for rid, patch in patches.items():
            target = cur_by_id.get(rid)
            if target is None:
                continue  # deleted/changed since the snapshot — skip
            changed = False
            add_tags = patch.get("add_tags")
            if add_tags:
                merged_tags = list(target.get("tags") or [])
                for t in add_tags:
                    if t not in merged_tags:
                        merged_tags.append(t)
                target["tags"] = merged_tags[:20]
                changed = True
            if patch.get("folder"):
                target["folder"] = patch["folder"]
                changed = True
            if changed:
                tagged += 1
        if tagged:
            _atomic_write_json(RESPONSES_FILE, current)
    return tagged


@app.post("/api/prompts/responses/import-library")
def api_prompts_responses_import_library() -> Response:
    """Merge the media library's prompts (from metadata.json) into Saved responses: add any that
    aren't already saved (deduped by normalized prompt hash). ``{"preview": true}`` only COUNTS
    what would be added (for the button label) and writes nothing. The real import backs up the
    current list first and can only grow it. Returns the full updated list."""
    payload = request.get_json(silent=True) or {}
    preview = bool(payload.get("preview"))
    folder = str(payload.get("folder") or "Library").strip()[:40]
    if preview:
        current = _read_responses()
        have = {promptstudio.prompt_hash(t) for t in
                (str(x.get("text", "")).strip() for x in current) if t}
        uniq = _library_unique_prompts()
        missing = sum(1 for h in uniq if h not in have)
        return jsonify(ok=True, missing=missing, library_unique=len(uniq), saved=len(current))
    merged, new_entries, backup = _import_library_into_saved(folder)
    if not new_entries:
        return jsonify(ok=True, added=0, total=len(merged), backup="", responses=merged)
    return jsonify(ok=True, added=len(new_entries), total=len(merged), backup=backup, responses=merged)


@app.get("/api/prompts/personas")
def api_prompts_personas_get() -> Response:
    """Saved Prompt Studio persona cards ([] if none/unreadable). On the data volume so they
    persist and are shared across every device."""
    data: list = []
    if PERSONAS_FILE.exists():
        try:
            loaded = json.loads(PERSONAS_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                data = loaded
        except Exception:
            data = []
    return jsonify(personas=data)


@app.post("/api/prompts/personas")
def api_prompts_personas_post() -> Response:
    """Replace the whole persona-card list (client owns it). Validate, normalise, atomic write."""
    incoming = (request.get_json(silent=True) or {}).get("personas")
    if not isinstance(incoming, list):
        return jsonify(ok=False, error="Expected a 'personas' array."), 400
    clean = []
    for entry in incoming[:200]:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()[:80]
        text = str(entry.get("text", ""))[:8000]
        if not name and not text.strip():
            continue
        clean.append({
            "id": str(entry.get("id") or "")[:64] or str(len(clean)),
            "name": name,
            "text": text,
            "anchor": str(entry.get("anchor", "")).strip()[:200],
        })
    _atomic_write_json(PERSONAS_FILE, clean)
    return jsonify(ok=True, count=len(clean))


@app.get("/api/library")
def api_library_get() -> Response:
    """User library state: favorited and archived item ids. The archive key stays
    named ``stashed`` on disk so existing data volumes keep working."""
    favorites: list = []
    stashed: list = []
    if LIBRARY_FILE.exists():
        try:
            data = json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                favorites = data.get("favorites") if isinstance(data.get("favorites"), list) else []
                stashed = data.get("stashed") if isinstance(data.get("stashed"), list) else []
        except Exception:
            pass
    return jsonify(favorites=favorites, stashed=stashed)


@app.post("/api/library")
def api_library_post() -> Response:
    payload = request.get_json(silent=True) or {}

    def _ids(value) -> list:
        if not isinstance(value, list):
            return []
        seen, out = set(), []
        for item in value[:100000]:
            key = str(item)[:MAX_MEDIA_ID_LEN]
            if key and key not in seen:
                seen.add(key)
                out.append(key)
        return out

    data = {"favorites": _ids(payload.get("favorites")), "stashed": _ids(payload.get("stashed"))}
    _atomic_write_json(LIBRARY_FILE, data)
    return jsonify(ok=True, favorites=len(data["favorites"]), stashed=len(data["stashed"]))


class CorruptStateError(RuntimeError):
    """A state file exists but is unparseable. Mutation paths raise this instead of
    treating the file as empty, so a partial/corrupt read can never cause us to
    overwrite or blocklist over real data that is only momentarily unreadable."""


def _load_json_strict(path: Path, default):
    """Parse a state file that the caller is about to REWRITE. Returns `default`
    when the file is absent (a legitimately empty starting point) but raises
    CorruptStateError when it exists yet cannot be parsed. Read-only callers that
    can safely degrade to empty should keep using the tolerant inline json.loads."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CorruptStateError(f"{path.name} exists but could not be read: {exc}") from exc


BACKUPS_DIR = DATA_DIR / "backups"  # rolling backups of state files, capped per file


def _backup_state_file(path: Path, *, keep: int = 10) -> str:
    """Copy an existing, non-empty state file into ``/data/backups/<stem>-<ts><suffix>`` before
    it's overwritten, pruning that file's backups to the newest ``keep``. Best-effort — never
    raises (a backup failure must not block the write). Returns the backup filename, or ""."""
    try:
        if not path.exists() or path.stat().st_size == 0:
            return ""
        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        dest = BACKUPS_DIR / f"{path.stem}-{ts}{path.suffix}"
        if dest.exists():  # more than one write in the same second
            dest = BACKUPS_DIR / f"{path.stem}-{ts}-{secrets.token_hex(2)}{path.suffix}"
        shutil.copy2(path, dest)
        kept = sorted(BACKUPS_DIR.glob(f"{path.stem}-*{path.suffix}"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        for old in kept[keep:]:
            try:
                old.unlink()
            except OSError:
                pass
        return dest.name
    except Exception:
        return ""


def _atomic_write_json(path: Path, data, *, backup: bool = True) -> str:
    """Atomically write ``data`` as JSON (tmp + replace). Unless ``backup`` is False, the file's
    prior contents are copied into ``/data/backups`` first (capped per file) so a bad or
    unintended write is always recoverable. Returns the backup filename (or "")."""
    bak = _backup_state_file(path) if backup else ""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return bak


def _atomic_write_bytes(path: Path, data: bytes, *, backup: bool = True) -> str:
    """Atomically write raw bytes (tmp + replace), copying the prior file into
    ``/data/backups`` first (capped per file) unless ``backup`` is False. The bytes
    counterpart of ``_atomic_write_json`` — used by restore, where the source is a
    backup zip's raw member (JSON text already serialized, ``prompt_studio.db``, or a
    secret text file) rather than a Python object. Returns the backup filename (or "")."""
    bak = _backup_state_file(path) if backup else ""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return bak


def _load_deleted() -> set:
    if not DELETED_FILE.exists():
        return set()
    try:
        data = json.loads(DELETED_FILE.read_text(encoding="utf-8"))
        return {str(x) for x in data} if isinstance(data, list) else set()
    except Exception:
        return set()


def _delete_media_files(item: dict) -> None:
    """Delete a record's media file, its subtitle sidecars, and its thumbnail.
    Guards against paths escaping the gallery root (traversal)."""
    gallery_root = GALLERY_DIR.resolve()
    targets: list[Path] = []
    rel = str(item.get("local_path", "")).replace("\\", "/")
    if rel:
        media = (GALLERY_DIR / rel).resolve()
        if media == gallery_root or gallery_root in media.parents:
            # Drop the clip's cached motion analyses (beat diffs + match-cut
            # descriptor) BEFORE the file goes — both cache keys need its stat.
            if item.get("media_type") == "video" and media.exists():
                moviegen.purge_motion_cache_for(media, MOTION_CACHE_DIR)
            targets += [media, media.with_suffix(".srt"), media.with_suffix(".vtt")]
    mid = str(item.get("id") or "")
    if mid:
        # Thumbnails are stored sharded (thumbnails/<shard>/<id>.jpg); delete that, and
        # keep the legacy flat path as a fallback so older thumbnails are cleaned too.
        targets.append(thumbgen.thumb_path({"id": mid}, THUMBS_DIR))
        targets.append(THUMBS_DIR / f"{mid}.jpg")
    for p in targets:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def _purge_ids_from_library(ids: set) -> None:
    if not LIBRARY_FILE.exists():
        return
    try:
        data = json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(data, dict):
        return
    _atomic_write_json(LIBRARY_FILE, {
        "favorites": [x for x in (data.get("favorites") or []) if str(x) not in ids],
        "stashed": [x for x in (data.get("stashed") or []) if str(x) not in ids],
    })


def _purge_ids_from_playlists(ids: set) -> None:
    if not PLAYLISTS_FILE.exists():
        return
    try:
        data = json.loads(PLAYLISTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(data, list):
        return
    changed = False
    for pl in data:
        if isinstance(pl, dict) and isinstance(pl.get("ids"), list):
            kept = [i for i in pl["ids"] if str(i) not in ids]
            if len(kept) != len(pl["ids"]):
                pl["ids"] = kept
                changed = True
    if changed:
        _atomic_write_json(PLAYLISTS_FILE, data)


def _purge_ids_from_collections(ids: set) -> None:
    if not COLLECTIONS_FILE.exists():
        return
    data = _load_collections()
    changed = False
    for coll in data:
        kept = [i for i in coll.get("ids", []) if str(i) not in ids]
        if len(kept) != len(coll.get("ids", [])):
            coll["ids"] = kept
            if coll.get("cover_id") and str(coll["cover_id"]) in ids:
                coll["cover_id"] = kept[0] if kept else ""
            coll["updated_at"] = _utc_stamp()
            changed = True
    if changed:
        _atomic_write_json(COLLECTIONS_FILE, data)


def _delete_ids(ids: set) -> int:
    """Hard-delete the given media ids: remove files from disk, drop from metadata +
    index, purge from library/playlists/collections, and blocklist the synced ones so
    future syncs never re-pull them. Returns the number of metadata records removed.

    Strict-loads metadata.json and raises CorruptStateError if it's present but
    unreadable — the caller MUST surface that (503) before anything is blocklisted, so a
    transient read error can't permanently blocklist items that were never removed."""
    loaded = _load_json_strict(METADATA_FILE, [])
    items = loaded if isinstance(loaded, list) else []
    kept, removed = [], 0
    for item in items:
        if isinstance(item, dict) and str(item.get("id")) in ids:
            _delete_media_files(item)
            removed += 1
        else:
            kept.append(item)
    if removed:
        _atomic_write_json(METADATA_FILE, kept)

    # deleted_ids.json only stops the Grok downloader from re-pulling synced media.
    # Locally-originated items (imports, montages) have no Grok source, so blocklisting
    # them is pointless bloat — skip those prefixes. Unknown / not-yet-in-metadata ids
    # are still blocklisted, so a Grok item mid-sync can't slip back in on a later page.
    blocklist_ids = {i for i in ids if not i.startswith(("import_", "montage_"))}
    _atomic_write_json(DELETED_FILE, sorted(_load_deleted() | blocklist_ids))

    _purge_ids_from_library(ids)
    _purge_ids_from_playlists(ids)
    _purge_ids_from_collections(ids)
    try:
        db.delete_media(DB_FILE, list(ids))
    except Exception as exc:  # pragma: no cover - defensive
        print(f"index delete failed: {exc}")
    return removed


@app.post("/api/media/delete")
def api_media_delete() -> Response:
    """Hard-delete media: remove files from disk, drop from metadata + index, purge
    from library/playlists, and blocklist the ids so future syncs never re-pull them."""
    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get("ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        return jsonify(ok=False, error="No items to delete."), 400
    ids = {str(i) for i in raw_ids if str(i)}
    if not ids:
        return jsonify(ok=False, error="No items to delete."), 400
    try:
        removed = _delete_ids(ids)
    except CorruptStateError as exc:
        return jsonify(ok=False, error=str(exc)), 503
    blocklisted = len({i for i in ids if not i.startswith(("import_", "montage_"))})
    return jsonify(ok=True, deleted=removed, blocklisted=blocklisted)


@app.post("/api/canvas/<cid>/rename")
def api_canvas_rename(cid: str) -> Response:
    """Rename an agent canvas. canvas_name is denormalized onto every member record, so
    this rewrites it on ALL metadata records sharing this canvas_id (the stable grouping
    key) and rebuilds the derived index so the new name surfaces in the canvas list and
    headers. The HD-upscale re-download path deliberately no longer re-applies Grok's
    canvas_name (see gdownloader.refresh_hd), so a rename survives future syncs."""
    cid = str(cid)
    name = str((request.get_json(silent=True) or {}).get("name", "")).strip()[:120]
    if not name:
        return jsonify(ok=False, error="Name cannot be empty."), 400
    try:
        loaded = _load_json_strict(METADATA_FILE, [])
    except CorruptStateError as exc:
        return jsonify(ok=False, error=str(exc)), 503
    items = loaded if isinstance(loaded, list) else []
    found, changed = 0, 0
    for item in items:
        if isinstance(item, dict) and str(item.get("canvas_id") or "") == cid:
            found += 1
            if item.get("canvas_name") != name:
                item["canvas_name"] = name
                changed += 1
    if not found:
        return jsonify(ok=False, error="Canvas not found."), 404
    if changed:
        _atomic_write_json(METADATA_FILE, items)
        rebuild_db()
    return jsonify(ok=True, renamed=changed, name=name)


@app.post("/api/canvas/<cid>/delete")
def api_canvas_delete(cid: str) -> Response:
    """Hard-delete a whole agent canvas: delete every media item belonging to it (files,
    metadata, index) and blocklist the synced ones so a future sync won't re-pull the
    canvas. Reuses the same machinery as /api/media/delete."""
    cid = str(cid)
    try:
        loaded = _load_json_strict(METADATA_FILE, [])
    except CorruptStateError as exc:
        return jsonify(ok=False, error=str(exc)), 503
    items = loaded if isinstance(loaded, list) else []
    ids = {str(it.get("id")) for it in items
           if isinstance(it, dict) and str(it.get("canvas_id") or "") == cid and it.get("id")}
    if not ids:
        return jsonify(ok=False, error="Canvas not found."), 404
    try:
        removed = _delete_ids(ids)
    except CorruptStateError as exc:
        return jsonify(ok=False, error=str(exc)), 503
    return jsonify(ok=True, deleted=removed)


# --------------------------------------------------------------------------- #
# Backup / Restore — a portable, point-in-time bundle of the DURABLE state
# (records + config) as a single .zip, so the whole archive can be exported and
# re-imported on this or another machine. Deliberately EXCLUDES the media blobs
# (gallery/, gigabytes, re-syncable) and the derived index.db (rebuilt from
# metadata.json on restore). Secrets — the API keys in settings.json, the Grok
# session (grok_auth.txt) and the admin password — are bundled ONLY when explicitly
# requested (?secrets=1); off by default, so the downloaded file is safe to store
# anywhere. Restore validates every member up front and snapshots the pre-restore
# state into /data/backups before overwriting, so a bad backup is always recoverable.
# --------------------------------------------------------------------------- #

BACKUP_VERSION = 1
_SETTINGS_SECRET_KEYS = ("embed_api_key", "llm_api_key", "xai_api_key")
# Zip-bomb backstop: an honest backup is a few MB; refuse to extract anything wildly
# larger so a crafted archive can't exhaust memory on restore.
_RESTORE_MAX_TOTAL = 600 * 1024 * 1024

# arcname -> destination path. This is also the strict allowlist for restore: only
# these exact names are ever written, so a crafted zip can never escape DATA_DIR.
_BACKUP_TARGETS = {
    "metadata.json": METADATA_FILE,
    "library.json": LIBRARY_FILE,
    "collections.json": COLLECTIONS_FILE,
    "collection_groups.json": COLLECTION_GROUPS_FILE,
    "playlists.json": PLAYLISTS_FILE,
    "saved_responses.json": RESPONSES_FILE,
    "scenes.json": SCENES_FILE,
    "personas.json": PERSONAS_FILE,
    "freeform_presets.json": FREEFORM_PRESETS_FILE,
    "deleted_ids.json": DELETED_FILE,
    "settings.json": SETTINGS_FILE,
    "prompt_studio.db": PROMPT_DB_FILE,
}
# imagine_sessions.json is deliberately NOT backed up: it only indexes un-saved Grok
# Imagine generations whose blobs live in the ephemeral imagine_staging/ scratch dir
# (not bundled), so restoring it elsewhere would recreate sessions of broken tiles.
# Keepers are promoted into the gallery (metadata.json), which IS backed up.
# Secret files — included on export and accepted on restore ONLY in secrets mode.
_BACKUP_SECRET_TARGETS = {
    "grok_auth.txt": CURL_FILE,
    "grok_accounts.json": ACCOUNTS_FILE,
    "admin_password.txt": ADMIN_PW_FILE,
}
# Per-account session files ride along in secrets mode under grok_accounts/<id>.txt.
# Dynamic names, so they can't live in the static allowlist above — restore instead
# accepts exactly this pattern, whose id segment is the same regex the registry
# enforces, so a crafted member can never escape grok_accounts/.
_ACCOUNT_ARC_RE = re.compile(r"^grok_accounts/([a-z0-9][a-z0-9-]{2,31})\.txt$")
# Members that must parse as JSON before being written (validated up front so a
# corrupt entry aborts the whole restore rather than half-overwriting live data).
_BACKUP_JSON_NAMES = {
    "metadata.json", "library.json", "collections.json", "playlists.json",
    "collection_groups.json",
    "saved_responses.json", "scenes.json", "personas.json",
    "freeform_presets.json", "deleted_ids.json", "settings.json",
    "grok_accounts.json",
}


def _settings_without_secrets() -> dict:
    """settings.json with the API-key fields stripped, for a no-secrets export."""
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    for key in _SETTINGS_SECRET_KEYS:
        data.pop(key, None)
    return data


def _count_records(path: Path) -> int:
    """Best-effort record count for the manifest/summary: list length, the single
    record-list inside a wrapper dict, or the sum of all list values (library)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        lists = [v for v in data.values() if isinstance(v, list)]
        if len(lists) == 1:
            return len(lists[0])
        return sum(len(v) for v in lists)
    return 0


def _count_favorites() -> int:
    """library.json holds two lists (favorites + stashed); count only favorites so the
    summary label is honest (the generic _count_records would sum both)."""
    try:
        data = json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
        fav = data.get("favorites") if isinstance(data, dict) else None
        return len(fav) if isinstance(fav, list) else 0
    except Exception:
        return 0


def _backup_counts() -> dict:
    return {
        "media": _count_records(METADATA_FILE),
        "collections": _count_records(COLLECTIONS_FILE),
        "collection_groups": _count_records(COLLECTION_GROUPS_FILE),
        "playlists": _count_records(PLAYLISTS_FILE),
        "saved_responses": _count_records(RESPONSES_FILE),
        "scenes": _count_records(SCENES_FILE),
        "personas": _count_records(PERSONAS_FILE),
        "favorites": _count_favorites(),
    }


@app.get("/api/backup/export")
def api_backup_export() -> Response:
    """Stream a .zip bundle of the durable state. ?secrets=1 also bundles the API
    keys, Grok session and admin password (off by default)."""
    include_secrets = request.args.get("secrets", "").strip().lower() in ("1", "true", "yes", "on")
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    fname = f"grokive-backup-{ts}.zip"

    targets = dict(_BACKUP_TARGETS)
    if include_secrets:
        targets.update(_BACKUP_SECRET_TARGETS)

    manifest = {
        "app": "grokive",
        "kind": "backup",
        "version": BACKUP_VERSION,
        "created_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "includes_secrets": include_secrets,
        "counts": _backup_counts(),
        "files": [],
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, path in targets.items():
            if name == "settings.json" and not include_secrets:
                if not path.exists():
                    continue
                payload = json.dumps(_settings_without_secrets(), ensure_ascii=False, indent=2).encode("utf-8")
            else:
                if not path.exists():
                    continue
                try:
                    payload = path.read_bytes()
                except OSError:
                    continue
            zf.writestr(name, payload)
            manifest["files"].append({"name": name, "bytes": len(payload)})
        if include_secrets:
            # Per-account sessions (the 'default' account is grok_auth.txt, already above).
            for acct in _load_accounts():
                path = _account_curl_path(acct["id"])
                if acct["id"] == "default" or not path.exists():
                    continue
                try:
                    payload = path.read_bytes()
                except OSError:
                    continue
                arc = f"{ACCOUNTS_DIR.name}/{acct['id']}.txt"
                zf.writestr(arc, payload)
                manifest["files"].append({"name": arc, "bytes": len(payload)})
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))

    data = buf.getvalue()
    return Response(
        data,
        mimetype="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Content-Length": str(len(data)),
        },
    )


@app.post("/api/backup/import")
def api_backup_import() -> Response:
    """Restore a backup .zip produced by /api/backup/export. Validates every member
    first, then atomically replaces the live state files (snapshotting the prior
    versions into /data/backups), and rebuilds the derived index. Destructive."""
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify(ok=False, error="No backup file uploaded."), 400
    if not f.filename.lower().endswith(".zip"):
        return jsonify(ok=False, error="Expected a .zip backup file."), 400

    # Restore replaces prompt_studio.db, which a running embed build holds open for
    # minutes — racing it would fail the swap (Windows) or lose the build's in-flight
    # writes (Linux). Refuse rather than race.
    with _embed_lock:
        if _embed.get("running"):
            return jsonify(ok=False, error="A Prompt Studio embedding build is running — wait for it to finish, then restore."), 409

    tmpdir = Path(tempfile.mkdtemp(prefix="ga-restore-"))
    try:
        zpath = tmpdir / "backup.zip"
        f.save(str(zpath))  # streams to disk — never buffers the upload in memory
        try:
            archive = zipfile.ZipFile(zpath)
        except zipfile.BadZipFile:
            return jsonify(ok=False, error="That file isn't a valid .zip backup."), 400
        with archive as zf:
            names = set(zf.namelist())
            if "manifest.json" not in names:
                return jsonify(ok=False, error="Not a Grokive backup (no manifest.json)."), 400
            if sum(i.file_size for i in zf.infolist()) > _RESTORE_MAX_TOTAL:
                return jsonify(ok=False, error="Backup is implausibly large; refusing to extract."), 400
            try:
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            except Exception:
                return jsonify(ok=False, error="Backup manifest is unreadable."), 400
            if not (isinstance(manifest, dict)
                    and manifest.get("app") == "grokive" and manifest.get("kind") == "backup"):
                return jsonify(ok=False, error="That .zip isn't a Grokive backup."), 400
            includes_secrets = bool(manifest.get("includes_secrets"))

            # Phase 1: read + validate every recognised member BEFORE writing anything,
            # so a corrupt entry aborts the restore instead of half-overwriting live data.
            all_targets = {**_BACKUP_TARGETS, **_BACKUP_SECRET_TARGETS}
            pending: list[tuple[Path, bytes]] = []
            for arcname, dest in all_targets.items():
                if arcname not in names:
                    continue
                raw = zf.read(arcname)
                if arcname in _BACKUP_JSON_NAMES:
                    try:
                        parsed = json.loads(raw.decode("utf-8"))
                    except Exception as exc:
                        return jsonify(ok=False, error=f"'{arcname}' in the backup is corrupt: {exc}"), 400
                    # settings.json merges OVER the current settings so restoring a
                    # no-secrets backup never wipes existing API keys (the keys are
                    # simply absent from it); a secrets backup carries — and overrides
                    # with — its own keys.
                    if arcname == "settings.json" and isinstance(parsed, dict):
                        try:
                            current = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                        except Exception:
                            current = {}
                        if not isinstance(current, dict):
                            current = {}
                        merged = {**current, **parsed}
                        raw = json.dumps(merged, ensure_ascii=False, indent=2).encode("utf-8")
                pending.append((dest, raw))

            # Per-account session files: dynamic names, gated by the same id regex
            # the registry enforces (see _ACCOUNT_ARC_RE) so they can only land as
            # grok_accounts/<id>.txt.
            for arcname in sorted(names):
                m = _ACCOUNT_ARC_RE.fullmatch(arcname)
                if m:
                    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
                    pending.append((ACCOUNTS_DIR / f"{m.group(1)}.txt", zf.read(arcname)))

            if not pending:
                return jsonify(ok=False, error="Backup contained no recognised state files."), 400

            # Phase 2: write each file atomically (prior version snapshotted to
            # /data/backups). Each _atomic_write_bytes is per-file atomic (tmp + replace),
            # but the SET isn't — so if a write fails mid-way (e.g. disk full), roll the
            # already-written files back from their snapshots, making the restore
            # all-or-nothing rather than a half-applied mix of old and new state.
            done: list[tuple[Path, str]] = []  # (dest, snapshot filename or "")
            try:
                for dest, data in pending:
                    bak = _atomic_write_bytes(dest, data)
                    done.append((dest, bak))
            except OSError as exc:
                rolled = 0
                for dest, bak in reversed(done):
                    if not bak:
                        continue  # no snapshot (file was absent / backup failed) — leave as-is
                    try:
                        shutil.copy2(BACKUPS_DIR / bak, dest)
                        rolled += 1
                    except OSError:
                        pass
                return jsonify(
                    ok=False,
                    error=f"Restore failed while writing files ({exc}). Rolled back "
                          f"{rolled} of {len(done)} change(s) from /data/backups; "
                          f"your prior snapshots are kept there.",
                ), 500
            restored = [dest.name for dest, _ in done]

        # index.db is purely derived — rebuild it from the restored metadata.json so the
        # gallery reflects the restore immediately (inline: the restored library must be
        # queryable the moment this response lands, and a stale index would show items
        # the restore just replaced). (The locked-collection cache keys on
        # collections.json's mtime, so it refreshes itself on the next request.)
        rebuild_db(wait=True)

        return jsonify(
            ok=True,
            restored=sorted(restored),
            includes_secrets=includes_secrets,
            counts=_backup_counts(),
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.get("/healthz")
def healthz() -> Response:
    return jsonify(ok=True)


@app.get("/<path:path>")
def spa_assets(path: str) -> Response:
    """Serve built SPA assets (/_app/..., /manifest.webmanifest, /icons/...) and
    fall back to the SPA shell for client-side routes. Registered last so the
    explicit /api, /media, /thumbnails, /data.js routes always win."""
    if not _spa_available():
        abort(404)
    candidate = (SPA_DIR / path)
    if candidate.is_file() and SPA_DIR in candidate.resolve().parents:
        return send_from_directory(SPA_DIR, path)
    return send_file(SPA_DIR / "index.html")


def maybe_reindex() -> None:
    """Heal a lost metadata.json on startup: if it's missing/empty but we still
    have data.js or media on disk, rebuild it so Sync won't re-download everything."""
    if METADATA_FILE.exists() and METADATA_FILE.stat().st_size > 2:
        return
    data_js = INCREMENTAL_DIR / "data.js"
    has_data = data_js.exists() and data_js.stat().st_size > 40
    has_media = MEDIA_DIR.exists() and any(MEDIA_DIR.rglob("*"))
    if not (has_data or has_media):
        return
    print("metadata.json missing/empty — reconstructing from data.js / existing media…")
    try:
        from reindex import reindex

        reindex(GALLERY_DIR, METADATA_FILE, data_js)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"auto-reindex failed: {exc}")


def _seed_collection_groups_once() -> None:
    """One-time migration from '<Group> - <Name>' flat names to collection group labels."""
    try:
        groups_state = _load_groups(strict=True)
    except CorruptStateError:
        return
    if COLLECTION_GROUPS_FILE.exists() and groups_state.get("seeded"):
        return
    try:
        collections = _load_collections(strict=True)
    except CorruptStateError:
        return
    if any(_clean_group_name(c.get("group")) for c in collections):
        return

    prefix_counts: dict[str, int] = {}
    for coll in collections:
        name = str(coll.get("name") or "")
        if " - " not in name:
            continue
        prefix, rest = name.split(" - ", 1)
        prefix = prefix.strip()
        if prefix and rest.strip():
            prefix_counts[prefix] = prefix_counts.get(prefix, 0) + 1
    families = {prefix for prefix, count in prefix_counts.items() if count >= 2}
    if families:
        for coll in collections:
            name = str(coll.get("name") or "")
            if name in families:
                coll["group"] = name
                continue
            if " - " not in name:
                continue
            prefix, rest = name.split(" - ", 1)
            prefix = prefix.strip()
            rest = rest.strip()
            if prefix in families and rest:
                coll["group"] = prefix
                coll["name"] = rest
        _atomic_write_json(COLLECTIONS_FILE, [c for c in (_clean_collection(c) for c in collections) if c])
    _save_groups({"seeded": True, "groups": groups_state.get("groups", [])})


def _prune_motion_cache() -> None:
    """Bound Motion Match Cut's descriptor cache at boot. A render prunes it too, but
    doing it here also reclaims space for someone who has stopped using the mode
    entirely (and after a library move strands every entry). Stat-only, so it costs
    nothing on an empty or small cache; never fatal to startup."""
    try:
        import matchcut
        res = matchcut.prune_cache(MOTION_CACHE_DIR)
        if res["removed"]:
            _log(f"motion cache: removed {res['removed']} stale entr"
                 f"{'y' if res['removed'] == 1 else 'ies'} "
                 f"({res['freed'] / 1048576:.1f} MB), {res['kept']} kept")
    except Exception as exc:  # pragma: no cover - never block startup
        print(f"motion cache prune skipped: {exc}")


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _seed_collection_groups_once()
    _prune_motion_cache()
    maybe_reindex()
    rebuild_db(wait=True)  # inline: schema + rows must exist before the first request
    mode = _auth_mode()
    auth = {"login": "login screen", "basic": "HTTP basic", "off": "DISABLED"}[mode]
    print(f"Grokive server")
    print(f"  data dir : {DATA_DIR}")
    print(f"  auth     : {auth}")
    print(f"  cookie secure: {app.config['SESSION_COOKIE_SECURE']} | trust proxy: {TRUST_PROXY}")
    if mode == "login" and ADMIN_PASSWORD_GENERATED:
        print("  " + "-" * 60)
        print(f"  LOGIN: user '{ADMIN_USER}'  password '{ADMIN_PASSWORD}'")
        print("  (auto-generated; set ADMIN_USER/ADMIN_PASSWORD to choose your own,")
        print("   or AUTH_DISABLED=true to turn auth off. Stored in admin_password.txt.)")
        print("  " + "-" * 60)
    elif mode == "off":
        print("  WARNING: auth is disabled — anyone who can reach this port has full access.")
    print(f"  listening : http://0.0.0.0:{PORT}")
    from waitress import serve

    # channel_timeout is generous so long exports (merge + optional burn re-encode)
    # and subtitle jobs aren't cut off mid-request. max_request_body_size MUST track
    # MAX_CONTENT_LENGTH: waitress enforces its OWN body cap (its default is 1 GB) and
    # rejects an oversized upload with a bare 413 BEFORE Flask sees it — so a folder
    # import of large videos fails unless this matches the app-level limit.
    serve(app, host="0.0.0.0", port=PORT, threads=8, channel_timeout=3600,
          max_request_body_size=app.config["MAX_CONTENT_LENGTH"])


if __name__ == "__main__":
    main()
