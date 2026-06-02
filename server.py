"""Web front-end for Grokive.

Serves the incremental gallery and exposes two actions used by the toolbar that is
injected into the page:

* **Sync**   -> runs ``grokive.py reindex`` -> ``download`` -> ``agents`` -> ``index``
               in a background thread, streaming output to a rolling log.
* **Config** -> stores the captured browser cURL (``grok_auth.txt``) on the
               persistent data volume.

All state lives under ``GROK_DATA_DIR`` (``/data`` in the container) so it survives
container recreation. Designed to run under waitress as a single process; sync
state is kept in-process and guarded by a lock.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

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

import db

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("GROK_DATA_DIR", ROOT / "data")).resolve()

GALLERY_DIR = DATA_DIR / "gallery"
INCREMENTAL_DIR = GALLERY_DIR / "gallery_incremental"
MEDIA_DIR = GALLERY_DIR / "media"
THUMBS_DIR = GALLERY_DIR / "thumbnails"
CURL_FILE = DATA_DIR / "grok_auth.txt"
# Legacy filename (pre-rename); still read so existing data volumes keep working.
LEGACY_CURL_FILE = DATA_DIR / "curl_samples.txt"
METADATA_FILE = DATA_DIR / "metadata.json"
DELETED_FILE = DATA_DIR / "deleted_ids.json"  # blocklist: ids the downloader must never re-pull
PLAYLISTS_FILE = DATA_DIR / "playlists.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
LIBRARY_FILE = DATA_DIR / "library.json"
DB_FILE = DATA_DIR / "index.db"
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


def _sync_worker() -> None:
    py = sys.executable
    cli = str(ROOT / "grokive.py")
    # Reindex FIRST so any media that exists on disk but is missing from
    # metadata.json is folded back in before the downloader runs (prevents
    # re-downloading files we already have).
    steps = [
        ("reindex", [py, cli, "reindex"]),
        ("download", [py, cli, "download"]),
        ("agents", [py, cli, "agents"]),
        ("index", [py, cli, "index"]),  # thumbnails + SQLite read-model
    ]
    rc = 0
    try:
        for label, args in steps:
            rc = _run_step(label, args)
            if rc != 0:
                _sync["step"] = "error"
                break
        else:
            _sync["step"] = "done"
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
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SETTINGS_FILE)


def _whisper_url() -> str:
    """Effective Whisper endpoint: env var wins, else the saved setting."""
    return WHISPER_ENV or str(_load_settings().get("whisper_server_url", "")).strip()


def _burn_enabled() -> bool:
    return bool(_load_settings().get("burn_subtitles"))


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


def _subtitles_worker() -> None:
    rc = 0
    try:
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
        for index, path in enumerate(todo, start=1):
            remaining = total - index
            _sync["step"] = f"{index}/{total}"
            _log(f"[{index}/{total}] {path.name} …")
            try:
                srt = _whisper_srt(path)
                path.with_suffix(".srt").write_text(srt, encoding="utf-8")
                vtt = _srt_to_vtt(srt)
                if vtt:
                    path.with_suffix(".vtt").write_text(vtt, encoding="utf-8")
                note = "no speech/audio" if not srt.strip() else "ok"
                _log(f"  {note} ({remaining} remaining)")
            except Exception as exc:
                _log(f"  failed: {exc}")
        # Rebuild the index so the new subtitle tracks show up in the UI.
        _log("rebuilding index...")
        rebuild_db()
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
        _sync["log"].clear()
    threading.Thread(target=_subtitles_worker, daemon=True).start()
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
    return send_from_directory(MEDIA_DIR, relpath, conditional=True)


@app.get("/thumbnails/<path:relpath>")
def thumbnails(relpath: str) -> Response:
    return send_from_directory(THUMBS_DIR, relpath, conditional=True)


@app.post("/api/sync")
def api_sync() -> Response:
    if start_sync():
        return jsonify(ok=True)
    return jsonify(ok=False, error="Sync already running"), 409


@app.get("/api/sync/status")
def api_sync_status() -> Response:
    return jsonify(
        running=_sync["running"],
        job=_sync["job"],
        step=_sync["step"],
        returncode=_sync["returncode"],
        started_at=_sync["started_at"],
        finished_at=_sync["finished_at"],
        auth_hint=_sync["auth_hint"],
        log=list(_sync["log"])[-60:],
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
    )


@app.post("/api/settings")
def api_settings_post() -> Response:
    payload = request.get_json(silent=True) or {}
    settings = _load_settings()
    if not WHISPER_ENV and "whisper_server_url" in payload:
        settings["whisper_server_url"] = str(payload.get("whisper_server_url") or "").strip()[:500]
    if "burn_subtitles" in payload:
        settings["burn_subtitles"] = bool(payload.get("burn_subtitles"))
    _save_settings(settings)
    return jsonify(
        ok=True,
        whisper_configured=bool(_whisper_url()),
        burn_subtitles=bool(settings.get("burn_subtitles")),
    )


@app.get("/api/config")
def api_config_get() -> Response:
    src = CURL_FILE if CURL_FILE.exists() else LEGACY_CURL_FILE
    if src.exists():
        mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(src.stat().st_mtime))
        return jsonify(configured=True, mtime=mtime)
    return jsonify(configured=False)


@app.post("/api/config")
def api_config_post() -> Response:
    body = request.get_data(as_text=True) or ""
    text = body.strip()
    if not text:
        return jsonify(ok=False, error="Empty config."), 400
    if "grok.com" not in text or "Cookie" not in text:
        return jsonify(
            ok=False,
            error="That doesn't look like a Grok cURL request (missing grok.com / Cookie).",
        ), 400
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CURL_FILE.with_suffix(".txt.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(CURL_FILE)
    # Saving migrates off the legacy file so there's only one source of truth.
    if LEGACY_CURL_FILE.exists():
        try:
            LEGACY_CURL_FILE.unlink()
        except OSError:
            pass
    return jsonify(ok=True)


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
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PLAYLISTS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PLAYLISTS_FILE)
    return jsonify(ok=True, count=len(clean))


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


def _video_paths_for_ids(ids: list) -> list[Path]:
    """Ordered, existing video files for a list of item ids (order preserved).
    Ignores images, missing files, and anything that would escape the gallery
    root (path traversal)."""
    index = _metadata_index()
    gallery_root = GALLERY_DIR.resolve()
    paths: list[Path] = []
    for raw_id in ids or []:
        item = index.get(str(raw_id))
        if not item or item.get("media_type") != "video":
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
# Burned-in subtitle font size; half of libass's default of 16 for SRT.
SUBTITLE_FONTSIZE = 8


def _merge_videos(paths: list[Path], out_path: Path) -> bool:
    """Merge clips in order into out_path. Returns True if the merge was a pure
    stream copy (zero quality loss), False if clips had to be re-encoded.

    When every clip shares codec/width/height/pix_fmt/fps the clips are
    concatenated with ``-c copy`` — no re-encode, audio untouched. When specs
    vary, each clip is re-encoded (CRF 10, preset slow) onto the largest input
    resolution by pixel count, padded to keep aspect, with audio re-encoded to
    AAC (a silent track is added to any clip lacking audio so audio is never
    dropped); the normalised clips are then losslessly concatenated.

    Raises RuntimeError if ffmpeg exits non-zero.
    """
    signatures = [_probe_signature(p) for p in paths]
    lossless = all(sig is not None for sig in signatures) and len(set(signatures)) == 1
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
        common_v = [
            "-vf", vf,
            "-c:v", "libx264", "-preset", PRESET, "-crf", CRF, "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", AUDIO_BPS, "-ar", AUDIO_AR, "-ac", "2",
            "-movflags", "+faststart", str(temp),
        ]
        if _has_audio(src):
            cmd = ["ffmpeg", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0"] + common_v
        else:
            # No audio stream -> mux in silence so every clip carries audio.
            cmd = [
                "ffmpeg", "-y", "-i", str(src),
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
    # libass renders SRT with a default Fontsize of 16 (at PlayResY 288, scaled to
    # the video). Force half that for smaller burned-in captions.
    try:
        _run_ffmpeg(
            ["ffmpeg", "-y", "-i", video_path.name,
             "-vf", f"subtitles=subs.srt:force_style='Fontsize={SUBTITLE_FONTSIZE}'",
             "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
             "-c:a", "copy", "-movflags", "+faststart", burned.name],
            "burn subtitles",
            cwd=str(workdir),
        )
    except Exception as exc:
        _log(f"burn: ffmpeg failed, exporting without subtitles ({exc})")
        return video_path
    return burned


def _export_response(paths: list[Path], name: str):
    """Merge ``paths`` (in order), optionally burn subtitles, and stream the result
    as an MP4 download. The temp dir is deleted after streaming — nothing persists.
    Shared by the saved-playlist and one-off (selection) export routes."""
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        return jsonify(ok=False, error="ffmpeg is not available on the server."), 500
    if not paths:
        return jsonify(ok=False, error="No videos to export are available on the server."), 400

    tmpdir = Path(tempfile.mkdtemp(prefix="ga-export-"))
    out_path = tmpdir / "merged.mp4"
    try:
        _merge_videos(paths, out_path)
        if _burn_enabled():
            out_path = _burn_subtitles_into(out_path)
    except Exception as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return jsonify(ok=False, error=f"Merge failed: {exc}"), 500

    size = out_path.stat().st_size
    safe_name = re.sub(r"[^\w.-]+", "_", name or "export").strip("_") or "export"

    def generate():
        try:
            with open(out_path, "rb") as fh:
                while True:
                    chunk = fh.read(262144)
                    if not chunk:
                        break
                    yield chunk
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

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


def rebuild_db() -> None:
    """Rebuild the SQLite read-model from metadata.json + on-disk thumbnails/subs.
    Safe to call anytime; the DB is purely derived."""
    try:
        rows = db.build_index(DB_FILE, METADATA_FILE, GALLERY_DIR)
        print(f"index.db rebuilt: {rows} media rows")
    except Exception as exc:  # pragma: no cover - defensive
        print(f"index.db rebuild failed: {exc}")


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
        rebuild_db()
    favorites, stashed = _library_sets()
    start, end = _period_range(request.args.get("period", "all"))
    result = db.query_media(
        DB_FILE,
        view=request.args.get("view", "files"),
        q=request.args.get("q", ""),
        tags=_multi_arg("tags"),
        models=_multi_arg("models"),
        canvas=request.args.get("canvas") or None,
        media_type=request.args.get("type", "all"),
        sort=request.args.get("sort", "new"),
        page=_int_arg("page", 1),
        page_size=_int_arg("page_size", 120),
        favorites=favorites,
        stashed=stashed,
        start=start,
        end=end,
    )
    return jsonify(result)


@app.get("/api/facets")
def api_facets() -> Response:
    if not DB_FILE.exists():
        rebuild_db()
    _, stashed = _library_sets()
    return jsonify(db.facets(DB_FILE, stashed=stashed))


@app.post("/api/media/by-ids")
def api_media_by_ids() -> Response:
    """Resolve an ordered id list to full media records (for playlist playback)."""
    if not DB_FILE.exists():
        rebuild_db()
    ids = (request.get_json(silent=True) or {}).get("ids")
    if not isinstance(ids, list):
        return jsonify(items=[])
    return jsonify(items=db.media_by_ids(DB_FILE, ids))


@app.get("/api/library")
def api_library_get() -> Response:
    """User library state: favorited and stashed item ids. Kept separate from
    metadata.json (the download ledger) so a sync never clobbers it."""
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
            key = str(item)[:128]
            if key and key not in seen:
                seen.add(key)
                out.append(key)
        return out

    data = {"favorites": _ids(payload.get("favorites")), "stashed": _ids(payload.get("stashed"))}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LIBRARY_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(LIBRARY_FILE)
    return jsonify(ok=True, favorites=len(data["favorites"]), stashed=len(data["stashed"]))


def _atomic_write_json(path: Path, data) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


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
            targets += [media, media.with_suffix(".srt"), media.with_suffix(".vtt")]
    mid = str(item.get("id") or "")
    if mid:
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

    items = []
    if METADATA_FILE.exists():
        try:
            loaded = json.loads(METADATA_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                items = loaded
        except Exception:
            items = []
    kept, removed = [], 0
    for item in items:
        if isinstance(item, dict) and str(item.get("id")) in ids:
            _delete_media_files(item)
            removed += 1
        else:
            kept.append(item)
    if removed:
        _atomic_write_json(METADATA_FILE, kept)

    # Blocklist every requested id — even ones not (yet) in metadata — so an item
    # that's mid-flight or appears on a later sync page can't slip back in.
    _atomic_write_json(DELETED_FILE, sorted(_load_deleted() | ids))

    _purge_ids_from_library(ids)
    _purge_ids_from_playlists(ids)
    try:
        db.delete_media(DB_FILE, list(ids))
    except Exception as exc:  # pragma: no cover - defensive
        print(f"index delete failed: {exc}")

    return jsonify(ok=True, deleted=removed, blocklisted=len(ids))


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


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    maybe_reindex()
    rebuild_db()
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
    # and subtitle jobs aren't cut off mid-request.
    serve(app, host="0.0.0.0", port=PORT, threads=8, channel_timeout=3600)


if __name__ == "__main__":
    main()
