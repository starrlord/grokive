"""xAI Grok Imagine API client — image & video generation.

A small, dependency-light wrapper over the xAI REST API, mirroring the urllib
house style used by :mod:`promptstudio` (no `requests`; stdlib only). The server
holds the API key and calls these functions; nothing here touches settings or the
gallery — the caller wires that up.

Endpoints (base ``https://api.x.ai/v1``):
  * ``POST /images/generations``         — text -> image (and image edit/alter via
                                            an optional ``image`` data-URI field).
  * ``POST /videos/generations``         — text -> video, or image -> video when an
                                            ``image`` data-URI is supplied. Returns a
                                            ``request_id`` to poll.
  * ``GET  /videos/{request_id}``        — poll video status until ``done``.

All returned media URLs are short-lived, so callers must download immediately via
:func:`fetch_bytes`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

XAI_BASE = "https://api.x.ai/v1"

# Endpoint paths kept as constants: if xAI splits image editing onto a dedicated
# ``/images/edits`` route, only this needs to change (today the generations route
# accepts an optional ``image`` field to switch into edit/alter mode, matching the
# video endpoint's image-to-video behaviour).
IMAGE_GEN_PATH = "/images/generations"
IMAGE_EDIT_PATH = "/images/edits"  # editing/altering a source image is a distinct endpoint
VIDEO_GEN_PATH = "/videos/generations"
VIDEO_GET_PATH = "/videos/{request_id}"


class XaiError(Exception):
    """An xAI API failure with an optional HTTP ``status`` and API error ``code``."""

    def __init__(self, message: str, *, status: int | None = None, code: str = ""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def _headers(api_key: str) -> dict:
    headers = {"Content-Type": "application/json"}
    key = str(api_key or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _parse_error_body(raw: bytes) -> tuple[str, str]:
    """Pull ``(message, code)`` out of an xAI error response body."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return (raw.decode("utf-8", "replace")[:300] if raw else ""), ""
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or "")[:300], str(err.get("code") or "")
    if isinstance(err, str):
        return err[:300], ""
    return str(data)[:300], ""


def _request_json(method: str, url: str, api_key: str, payload: dict | None = None,
                  *, timeout: float = 180.0) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(api_key), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read()
        except Exception:
            pass
        message, code = _parse_error_body(body)
        raise XaiError(message or f"xAI HTTP {exc.code}", status=exc.code, code=code) from exc
    except urllib.error.URLError as exc:
        raise XaiError(f"Could not reach xAI: {exc.reason}") from exc


# --- Images ----------------------------------------------------------------- #

def generate_images(api_key: str, *, model: str, prompt: str, n: int = 1,
                    aspect_ratio: str = "1:1", resolution: str = "1k",
                    image_data_uri: str = "", response_format: str = "url",
                    timeout: float = 180.0) -> list[dict[str, Any]]:
    """Generate ``n`` images from ``prompt`` (text-to-image), or — when
    ``image_data_uri`` is given — edit/alter that source image via the distinct
    ``/images/edits`` endpoint. The source image is passed as an object
    ``{"url": <public URL or data: URI>}``; a single-image edit derives its aspect
    ratio from the source, so aspect_ratio/resolution aren't sent for edits.

    Returns the API's ``data`` list — each item has a ``url`` (or ``b64_json`` when
    ``response_format='b64_json'``). URLs are temporary; download promptly."""
    n = max(1, min(10, int(n or 1)))
    if image_data_uri:
        payload: dict[str, Any] = {
            "model": model,
            "prompt": str(prompt or ""),
            "image": {"url": image_data_uri, "type": "image_url"},
            "n": n,
            "response_format": response_format,
        }
        path = IMAGE_EDIT_PATH
    else:
        payload = {
            "model": model,
            "prompt": str(prompt or ""),
            "n": n,
            "response_format": response_format,
        }
        if aspect_ratio and aspect_ratio != "auto":
            payload["aspect_ratio"] = aspect_ratio
        if resolution:
            payload["resolution"] = resolution
        path = IMAGE_GEN_PATH
    data = _request_json("POST", XAI_BASE + path, api_key, payload, timeout=timeout)
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise XaiError("xAI returned no image data.")
    return items


# --- Video ------------------------------------------------------------------ #

def start_video(api_key: str, *, model: str, prompt: str, image_data_uri: str = "",
                duration: int = 6, aspect_ratio: str = "16:9", resolution: str = "480p",
                timeout: float = 60.0) -> str:
    """Kick off a video generation. With ``image_data_uri`` the source image is the
    starting frame (image-to-video); otherwise it's text-to-video. Returns the
    ``request_id`` to poll with :func:`poll_video`."""
    payload: dict[str, Any] = {
        "model": model,
        "prompt": str(prompt or ""),
        "duration": max(1, min(15, int(duration or 6))),
    }
    # "auto" (or empty) = let the API keep the input image's aspect ratio (image-to-
    # video). Sending an explicit ratio stretches the source to fit, which squishes it.
    if aspect_ratio and aspect_ratio != "auto":
        payload["aspect_ratio"] = aspect_ratio
    if resolution:
        payload["resolution"] = resolution
    if image_data_uri:
        # image-to-video: the source image is an object with a url (public URL or
        # base64 data: URI), not a bare string.
        payload["image"] = {"url": image_data_uri}
    data = _request_json("POST", XAI_BASE + VIDEO_GEN_PATH, api_key, payload, timeout=timeout)
    request_id = data.get("request_id") if isinstance(data, dict) else None
    if not request_id:
        raise XaiError("xAI did not return a request_id for the video job.")
    return str(request_id)


def poll_video(api_key: str, request_id: str, *, timeout: float = 60.0) -> dict[str, Any]:
    """Fetch the current status of a video job. Returns a dict with ``status`` in
    ``pending|done|expired|failed``; when ``done`` it carries a ``video`` object with
    a ``url`` and ``duration``; when ``failed`` it carries an ``error`` object."""
    url = XAI_BASE + VIDEO_GET_PATH.format(request_id=urllib.parse.quote(str(request_id)))
    return _request_json("GET", url, api_key, None, timeout=timeout)


# --- Download --------------------------------------------------------------- #

# A real browser User-Agent: xAI's media CDN 403s the default ``Python-urllib`` agent.
_DOWNLOAD_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def fetch_bytes(url: str, *, timeout: float = 180.0) -> tuple[bytes, str]:
    """Download a generated media URL immediately (xAI URLs expire). Returns
    ``(raw_bytes, content_type)``. Sends a browser User-Agent so the CDN/WAF doesn't
    403 the request (it rejects urllib's default agent)."""
    req = urllib.request.Request(url, headers={"Accept": "*/*", "User-Agent": _DOWNLOAD_UA}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "") or ""
            return resp.read(), content_type
    except urllib.error.HTTPError as exc:
        raise XaiError(f"Could not download generated media (HTTP {exc.code}).", status=exc.code) from exc
    except urllib.error.URLError as exc:
        raise XaiError(f"Could not download generated media: {exc.reason}") from exc
