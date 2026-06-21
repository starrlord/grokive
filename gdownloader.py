from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from curl_cffi import requests as cffi_requests
from curl_cffi.requests.exceptions import RequestException as CffiRequestException

from mediautil import file_content_hash, media_shard


DEFAULT_METADATA = Path("metadata.json")
DEFAULT_FAILURES = Path("failed_downloads.json")
DEFAULT_DELETED = Path("deleted_ids.json")
# Root holding media/, thumbnails/ and the built galleries. Files are written under
# here, but local_path is stored relative to this root so the galleries resolve it
# the same way regardless of where the repo lives.
GALLERY_ROOT = Path("gallery")
# IDs the user deleted via the web UI. Populated in main(); process_item refuses to
# (re)download anything in here, so a deleted item never comes back on the next sync.
DELETED_IDS: set[str] = set()
# Count of items re-downloaded in place this run because Grok began serving an HD
# (upscaled) variant for an id we already had — surfaced in the run summary.
REFRESHED: int = 0
GROK_FAVORITES_ENDPOINT = "https://grok.com/rest/media/post/list"
GROK_FAVORITES_FILTER = "MEDIA_POST_SOURCE_LIKED"
GROK_CANVAS_LIST_ENDPOINT = "https://grok.com/rest/media/canvas/list"
GROK_CANVAS_GET_ENDPOINT = "https://grok.com/rest/media/canvas/get"
GROK_POST_GET_ENDPOINT = "https://grok.com/rest/media/post/get"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v", ".mpeg", ".mpg"}
KNOWN_MEDIA_HOSTS = {"assets.grok.com", "imagine-public.x.ai"}
MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
}
PROMPT_KEYS = {"prompt", "text", "query", "user_prompt", "input_prompt", "caption"}
ID_KEYS = {"id", "generation_id", "media_id", "asset_id", "uuid"}
DATE_KEYS = {"created_at", "createdAt", "createTime", "createdTime", "timestamp", "time", "date"}
NEXT_KEYS = {"next", "next_cursor", "nextCursor", "cursor", "pagination_token", "continuation"}
MODEL_KEYS = {"model", "modelName", "modelId"}
MIME_KEYS = {"mime_type", "mimeType", "contentType", "content_type"}
MEDIA_TYPE_KEYS = {"media_type", "mediaType", "type"}


@dataclass
class RequestSpec:
    method: str
    url: str
    headers: dict[str, str]
    cookies: dict[str, str]
    body: str | None

    def headers_with_cookies(self) -> dict[str, str]:
        """Headers plus a folded-in Cookie header.

        httpx deprecated per-request ``cookies=``; fold them into the Cookie
        header instead (unless one is already present from the captured cURL).
        """
        headers = dict(self.headers)
        if self.cookies and not any(k.lower() == "cookie" for k in headers):
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        return headers


@dataclass
class MediaItem:
    id: str
    prompt: str
    created_at: str | None
    media_type: str
    model: str | None
    parent_id: str | None
    source_url: str
    local_path: str
    canvas_id: str | None = None
    canvas_name: str | None = None
    width: int | None = None
    height: int | None = None


def parse_curl_samples(path: Path) -> list[RequestSpec]:
    text = path.read_text(encoding="utf-8")
    commands = re.split(r"(?=^curl(?:\.exe)?\s)", text, flags=re.MULTILINE)
    specs = []
    for command in commands:
        command = command.strip()
        if not re.match(r"^curl(?:\.exe)?\s", command):
            continue
        try:
            specs.append(parse_curl(command))
        except ValueError as exc:
            print(f"skipping cURL block: {exc}")
    if not specs:
        raise SystemExit(f"No curl commands found in {path}")
    return specs


def parse_curl(command: str) -> RequestSpec:
    command = command.replace("\\\n", " ")
    parts = shlex.split(command, posix=True)
    if not parts or parts[0] not in {"curl", "curl.exe"}:
        raise ValueError("curl command must start with curl")

    method = "GET"
    url = ""
    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    body: str | None = None
    i = 1

    while i < len(parts):
        part = parts[i]
        if part in {"-X", "--request"}:
            i += 1
            method = parts[i].upper()
        elif part in {"-H", "--header"}:
            i += 1
            name, _, value = parts[i].partition(":")
            if name and _:
                if name.lower() == "cookie":
                    cookies.update(parse_cookie_header(value.strip()))
                else:
                    headers[name.strip()] = value.strip()
        elif part in {"-b", "--cookie", "--cookie-jar"}:
            i += 1
            cookies.update(parse_cookie_header(parts[i]))
        elif part in {"--data", "--data-raw", "--data-binary", "-d"}:
            i += 1
            body = parts[i]
            if method == "GET":
                method = "POST"
        elif part.startswith("http://") or part.startswith("https://"):
            url = part
        i += 1

    if not url:
        match = re.search(r"""['"]?(https?://[^'"\s\\;]+)""", command)
        if match:
            url = match.group(1)
        else:
            raise ValueError("Could not find URL in curl command")
    return RequestSpec(method=method, url=url, headers=headers, cookies=cookies, body=body)


def parse_cookie_header(value: str) -> dict[str, str]:
    cookies = {}
    for chunk in value.split(";"):
        name, sep, val = chunk.strip().partition("=")
        if name and sep:
            cookies[name] = val
    return cookies


def grok_favorites_spec(auth_spec: RequestSpec, page_size: int) -> RequestSpec:
    headers = dict(auth_spec.headers)
    headers["Content-Type"] = "application/json"
    return RequestSpec(
        method="POST",
        url=GROK_FAVORITES_ENDPOINT,
        headers=headers,
        cookies=auth_spec.cookies,
        body=json.dumps(
            {"limit": page_size, "filter": {"source": GROK_FAVORITES_FILTER}},
            separators=(",", ":"),
        ),
    )


def grok_canvas_list_spec(auth_spec: RequestSpec) -> RequestSpec:
    headers = dict(auth_spec.headers)
    headers["Content-Type"] = "application/json"
    return RequestSpec(
        method="POST",
        url=GROK_CANVAS_LIST_ENDPOINT,
        headers=headers,
        cookies=auth_spec.cookies,
        body="{}",
    )


def grok_canvas_get_spec(auth_spec: RequestSpec, canvas_id: str) -> RequestSpec:
    headers = dict(auth_spec.headers)
    headers["Content-Type"] = "application/json"
    return RequestSpec(
        method="POST",
        url=GROK_CANVAS_GET_ENDPOINT,
        headers=headers,
        cookies=auth_spec.cookies,
        body=json.dumps({"id": canvas_id}, separators=(",", ":")),
    )


def grok_post_get_spec(auth_spec: RequestSpec, post_id: str) -> RequestSpec:
    """Fetch a single post (/imagine/post/<id>) — root media plus its child posts.
    Mirrors canvas/get: POST {"id": <post_id>} to /rest/media/post/get."""
    headers = dict(auth_spec.headers)
    headers["Content-Type"] = "application/json"
    return RequestSpec(
        method="POST",
        url=GROK_POST_GET_ENDPOINT,
        headers=headers,
        cookies=auth_spec.cookies,
        body=json.dumps({"id": post_id}, separators=(",", ":")),
    )


def list_grok_canvases(client: httpx.Client, auth_spec: RequestSpec) -> list[tuple[str, str]]:
    """Return (id, name) for every Agent canvas/document on /imagine/saved."""
    data = request_json_with_backoff(client, grok_canvas_list_spec(auth_spec))
    documents = data.get("documents", []) if isinstance(data, dict) else []
    canvases = []
    for doc in documents:
        if isinstance(doc, dict) and doc.get("id"):
            canvases.append((str(doc["id"]), str(doc.get("name") or doc["id"])))
    return canvases


def extract_canvas_items(canvas_json: Any, canvas_id: str, canvas_name: str) -> list[dict[str, Any]]:
    """Pull one media record per node in an Agent canvas (canvas/get response).

    Canvas media lives on assets.grok.com as extension-less /content URLs, so the
    media type and file extension come from the asset's mimeType, not the URL.
    """
    nodes = canvas_json.get("nodes", []) if isinstance(canvas_json, dict) else []
    out: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        asset = node.get("asset") if isinstance(node.get("asset"), dict) else {}
        meta = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        url = asset.get("mediaUrl") or meta.get("mediaUrl")
        if not isinstance(url, str) or not url:
            continue
        mime = asset.get("mimeType") or ""
        item_id = str(
            asset.get("assetId")
            or meta.get("assetId")
            or node.get("id")
            or stable_id(url)
        )
        out[item_id] = {
            "id": item_id,
            "prompt": asset.get("prompt") or meta.get("prompt") or "",
            "created_at": asset.get("createTime"),
            "createdAt": asset.get("createTime"),
            "mime_type": mime,
            "model": asset.get("generationType"),
            "parent_id": canvas_id,
            "canvas_id": canvas_id,
            "canvas_name": canvas_name,
            "source_url": url,
        }
    return list(out.values())


def choose_grok_auth_spec(specs: list[RequestSpec]) -> RequestSpec:
    for spec in specs:
        if urlparse(spec.url).hostname == "grok.com" and spec.cookies:
            return spec
    for spec in specs:
        if urlparse(spec.url).hostname == "grok.com":
            return spec
    return specs[0]


def load_metadata(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def load_deleted_ids(path: Path) -> set[str]:
    """IDs the user deleted via the web UI; these are never (re)downloaded."""
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    return {str(x) for x in data} if isinstance(data, list) else set()


def _atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file + os.replace so a crash/OOM mid-write can never leave
    a truncated, unparseable file behind. metadata.json is the library's single
    source of truth and is rewritten after every downloaded item, so a partial
    write here is the most likely real-world corruption path."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save_metadata(path: Path, items: list[dict[str, Any]]) -> None:
    _atomic_write_text(path, json.dumps(items, indent=2, ensure_ascii=False))


def append_failure(path: Path, failure: dict[str, Any]) -> None:
    failures = []
    if path.exists():
        failures = json.loads(path.read_text(encoding="utf-8"))
    failures.append(failure)
    _atomic_write_text(path, json.dumps(failures, indent=2, ensure_ascii=False))


def request_json(client: httpx.Client, spec: RequestSpec) -> Any:
    response = client.request(
        spec.method,
        spec.url,
        headers=spec.headers_with_cookies(),
        content=spec.body,
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def iter_pages(client: httpx.Client, list_spec: RequestSpec, max_pages: int | None) -> Any:
    seen_urls: set[str] = set()
    page = 0
    spec = list_spec

    while True:
        if max_pages is not None and page >= max_pages:
            return
        if spec.url in seen_urls:
            return
        seen_urls.add(spec.url)
        page += 1

        data = request_json_with_backoff(client, spec)
        yield data

        cursor = find_next_cursor(data)
        if not cursor:
            return
        spec = RequestSpec(
            method=spec.method,
            url=with_cursor(spec.url, cursor),
            headers=spec.headers,
            cookies=spec.cookies,
            body=with_cursor_in_body(spec.body, cursor),
        )
        time.sleep(1)


def request_json_with_backoff(client: httpx.Client, spec: RequestSpec) -> Any:
    delay = 2.0
    for attempt in range(6):
        try:
            return request_json(client, spec)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 429 or attempt == 5:
                raise
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


def with_cursor(url: str, cursor: str) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    cursor_keys = [key for key in query if "cursor" in key.lower() or "token" in key.lower()]
    query[cursor_keys[0] if cursor_keys else "cursor"] = cursor
    return urlunparse(parsed._replace(query=urlencode(query)))


def with_cursor_in_body(body: str | None, cursor: str) -> str | None:
    if not body:
        return body
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body
    if isinstance(data, dict):
        key = next((k for k in data if "cursor" in k.lower() or "token" in k.lower()), "cursor")
        data[key] = cursor
        return json.dumps(data, separators=(",", ":"))
    return body


def find_next_cursor(data: Any) -> str | None:
    if isinstance(data, dict):
        for key, value in data.items():
            if key in NEXT_KEYS and isinstance(value, str) and value:
                return value
        for value in data.values():
            found = find_next_cursor(value)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = find_next_cursor(value)
            if found:
                return found
    return None


def extract_media_items(data: Any) -> list[dict[str, Any]]:
    concrete = extract_grok_media_items(data)
    if concrete:
        return concrete

    candidates: list[dict[str, Any]] = []

    def visit(node: Any, ancestors: list[dict[str, Any]]) -> None:
        if isinstance(node, dict):
            urls = media_urls_in(node)
            if urls:
                context = merge_ancestors(ancestors + [node])
                for url in urls:
                    item = dict(context)
                    item["source_url"] = url
                    candidates.append(item)
            for value in node.values():
                visit(value, ancestors + [node])
        elif isinstance(node, list):
            for value in node:
                visit(value, ancestors)

    visit(data, [])
    deduped: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        item_id = str(first_value(candidate, ID_KEYS) or stable_id(candidate.get("source_url", "")))
        candidate["id"] = item_id
        deduped[item_id] = candidate
    return list(deduped.values())


def extract_grok_media_items(data: Any) -> list[dict[str, Any]]:
    raw = []
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        post = data.get("post")
        if isinstance(post, dict):
            raw = [post]
        else:
            for key in ("mediaPosts", "items", "posts", "results", "data", "media", "list", "generations"):
                value = data.get(key)
                if isinstance(value, list):
                    raw = value
                    break
    if not raw:
        return []

    out: dict[str, dict[str, Any]] = {}
    for post in raw:
        if not isinstance(post, dict):
            continue
        for item, parent in grok_post_and_children(post):
            record = harvest_grok_item(item, parent)
            if record:
                out[record["id"]] = record
    return list(out.values())


def grok_post_and_children(post: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    items: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    seen_objects: set[int] = set()
    seen_ids: set[str] = set()

    def visit(node: dict[str, Any], parent: dict[str, Any] | None) -> None:
        object_id = id(node)
        if object_id in seen_objects:
            return
        seen_objects.add(object_id)

        node_id = first_value(node, ID_KEYS)
        if node_id:
            node_id = str(node_id)
            if node_id in seen_ids:
                return
            seen_ids.add(node_id)

        items.append((node, parent))

        for key in ("childPosts", "children", "mediaList", "media", "images", "videos", "inputMediaItems"):
            value = node.get(key)
            if isinstance(value, list):
                for child in value:
                    if isinstance(child, dict):
                        visit(child, node)
            elif isinstance(value, dict):
                visit(value, node)

        # Grok stores the base image for generated videos here. It is an ancestor
        # of the current post, so do not make its parent_id point at the video.
        for key in ("originalPost", "parentPost", "sourcePost"):
            value = node.get(key)
            if isinstance(value, dict):
                visit(value, None)

    visit(post, None)
    return items


def _best_media_url(item: dict[str, Any]) -> Any:
    """Pick the highest-resolution media URL from an item's OWN fields. Grok exposes upscales as
    ``hd1080MediaUrl`` (1080p) and ``hdMediaUrl`` (720p) alongside the SD ``mediaUrl``.

    We deliberately do NOT descend into nested ``videos[]``: a post's video children are SEPARATE
    items with their own ids and are visited independently by grok_post_and_children, so reaching
    in would stamp a child video's URL onto the parent image's record (which mis-typed videos as
    images and broke their thumbnails). Each node already carries its own hd* fields."""
    for field in ("hd1080MediaUrl", "hdMediaUrl"):
        candidate = item.get(field)
        if isinstance(candidate, str) and candidate:
            return candidate
    return (
        item.get("mediaUrl")
        or item.get("imageUrl")
        or item.get("videoUrl")
        or item.get("url")
        or nested_url(item.get("media"))
        or item.get("fileUrl")
        or item.get("sourceUrl")
    )


def harvest_grok_item(item: dict[str, Any], parent: dict[str, Any] | None) -> dict[str, Any] | None:
    url = _best_media_url(item)
    if not isinstance(url, str) or not is_media_url(url):
        return None

    item_id = str(
        item.get("id")
        or item.get("postId")
        or item.get("mediaId")
        or item.get("generationId")
        or stable_id(url)
    )
    prompt = item.get("prompt") or item.get("caption") or (parent or {}).get("prompt") or (parent or {}).get("caption") or ""
    created_at = (
        item.get("createTime")
        or item.get("createdAt")
        or item.get("createdTime")
        or item.get("timestamp")
        or (parent or {}).get("createTime")
        or (parent or {}).get("createdAt")
    )
    model = item.get("modelName") or item.get("model") or item.get("modelId") or (parent or {}).get("modelName")
    original_post_id = item.get("originalPostId") or item.get("original_post_id")
    parent_item_id = (parent or {}).get("id") or (parent or {}).get("postId")
    parent_original_id = (parent or {}).get("originalPostId") or (parent or {}).get("original_post_id")
    if original_post_id and str(original_post_id) != item_id:
        parent_id = original_post_id
    elif parent_item_id and str(parent_item_id) != item_id and str(parent_original_id) != item_id:
        parent_id = parent_item_id
    else:
        parent_id = normalize_prompt(str(prompt))
    res = item.get("resolution") or (parent or {}).get("resolution") or {}
    width = res.get("width") if isinstance(res, dict) else None
    height = res.get("height") if isinstance(res, dict) else None
    return {
        "id": item_id,
        "prompt": prompt,
        "created_at": created_at,
        "createdAt": created_at,
        "model": model,
        "parent_id": parent_id,
        "source_url": url,
        "mime_type": first_value(item, MIME_KEYS),
        "media_type": first_value(item, MEDIA_TYPE_KEYS),
        "width": width,
        "height": height,
    }


def nested_url(value: Any) -> str | None:
    if isinstance(value, dict):
        url = value.get("url") or value.get("src")
        return url if isinstance(url, str) else None
    return None


def media_urls_in(node: dict[str, Any]) -> list[str]:
    urls = []
    for key, value in node.items():
        if isinstance(value, str) and is_media_url(value):
            urls.append(value)
        elif isinstance(value, list):
            urls.extend(v for v in value if isinstance(v, str) and is_media_url(v))
        elif isinstance(value, dict):
            url = value.get("url") or value.get("src")
            if isinstance(url, str) and is_media_url(url):
                urls.append(url)
    return urls


def is_media_url(value: str) -> bool:
    parsed = urlparse(value)
    ext = Path(parsed.path).suffix.lower()
    if parsed.scheme not in {"http", "https"}:
        return False
    if ext in IMAGE_EXTS.union(VIDEO_EXTS):
        return True
    return parsed.netloc.lower() in KNOWN_MEDIA_HOSTS


def merge_ancestors(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for node in nodes:
        for key, value in node.items():
            if isinstance(value, (str, int, float, bool)) and key not in merged:
                merged[key] = value
    return merged


def first_value(data: dict[str, Any], keys: set[str]) -> Any:
    for key in keys:
        if data.get(key):
            return data[key]
    for key, value in data.items():
        if key.lower() in {k.lower() for k in keys} and value:
            return value
    return None


def stable_id(value: str) -> str:
    readable = re.sub(r"[^a-zA-Z0-9_-]+", "_", value)[-60:].strip("_")
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]
    return f"{readable}_{digest}" if readable else digest


def media_type_for(url: str) -> str:
    ext = Path(urlparse(url).path).suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    return "image"


def resolve_media_type(raw: dict[str, Any]) -> str:
    """Prefer an explicit mimeType (canvas assets have extension-less URLs)."""
    mime = first_value(raw, MIME_KEYS)
    if isinstance(mime, str) and mime:
        mime = mime.lower()
        if mime.startswith("video"):
            return "video"
        if mime.startswith("image"):
            return "image"
    media_type = first_value(raw, MEDIA_TYPE_KEYS)
    if isinstance(media_type, str) and media_type:
        media_type = media_type.lower()
        if "video" in media_type:
            return "video"
        if "image" in media_type:
            return "image"
    return media_type_for(str(raw.get("source_url", "")))


def extension_for(url: str, content_type: str | None) -> str:
    ext = Path(urlparse(url).path).suffix.lower()
    if ext:
        return ext
    if content_type:
        mime = content_type.split(";")[0].strip().lower()
        if mime in MIME_EXT:
            return MIME_EXT[mime]
        guessed = mimetypes.guess_extension(mime)
        if guessed:
            return guessed
    return ".bin"


def download_media(client: httpx.Client, spec: RequestSpec, url: str, dest_without_ext: Path) -> Path:
    # Media lives behind Cloudflare bot protection (e.g. imagine-public.x.ai), which
    # blocks based on TLS/HTTP fingerprint, not just headers — httpx is rejected with 403
    # even with a browser User-Agent. curl_cffi impersonates a real browser's TLS handshake
    # so the CDN serves the file. The httpx `client` is unused here but kept for signature
    # compatibility with the rest of the pipeline.
    delay = 2.0
    last_exc: Exception | None = None
    for attempt in range(5):
        try:
            response = cffi_requests.get(
                url,
                headers=spec.headers,
                cookies=spec.cookies,
                impersonate="firefox",
                allow_redirects=True,
                timeout=300,
            )
            response.raise_for_status()
            ext = extension_for(url, response.headers.get("content-type"))
            dest = dest_without_ext.with_suffix(ext)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(response.content)
            return dest
        except CffiRequestException as exc:
            last_exc = exc
            if attempt == 4:
                break
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"failed downloading {url}: {last_exc}")


def normalize_record(raw: dict[str, Any], local_path: Path) -> MediaItem:
    prompt = str(first_value(raw, PROMPT_KEYS) or "")
    created_at = first_value(raw, DATE_KEYS)
    parent_id = raw.get("parent_id") or raw.get("parentId") or normalize_prompt(prompt)
    source_url = str(raw["source_url"])
    return MediaItem(
        id=str(raw["id"]),
        prompt=prompt,
        created_at=str(created_at) if created_at else None,
        media_type=resolve_media_type(raw),
        model=str(first_value(raw, MODEL_KEYS)) if first_value(raw, MODEL_KEYS) else None,
        parent_id=str(parent_id) if parent_id else None,
        source_url=source_url,
        # Always store POSIX-style ("media/images/x.jpg") so metadata written on
        # Windows still resolves when the gallery is served from Linux/Docker.
        local_path=local_path.as_posix(),
        canvas_id=str(raw["canvas_id"]) if raw.get("canvas_id") else None,
        canvas_name=str(raw["canvas_name"]) if raw.get("canvas_name") else None,
        width=int(raw["width"]) if raw.get("width") else None,
        height=int(raw["height"]) if raw.get("height") else None,
    )


def normalize_prompt(prompt: str) -> str:
    return re.sub(r"\s+", " ", prompt.lower()).strip(" \t\r\n.!?;:")


def _asset_path(url: str) -> str:
    """The path part of an asset URL (drops scheme/host and any signing query), so two URLs
    are compared by what file they point at, not by transient query tokens."""
    try:
        return urlparse(url).path
    except Exception:
        return url or ""


def _media_res_rank(url: str) -> int:
    """Resolution tier of a Grok asset URL, inferred from its filename, so an upscale can be
    told apart from a downgrade. 3 = 1080p (generated_video_1080_hd.mp4), 2 = HD/720p
    (…_hd.mp4), 1 = base/SD (generated_video.mp4) or anything else. Grok exposes these tiers
    as hd1080MediaUrl / hdMediaUrl / mediaUrl respectively (see _best_media_url)."""
    name = _asset_path(url).rsplit("/", 1)[-1].lower()
    if "1080" in name:
        return 3
    if name.rsplit(".", 1)[0].endswith("_hd"):
        return 2
    return 1


def _record_mistyped(record: dict[str, Any]) -> bool:
    """True if a stored record's media_type disagrees with its source_url's actual file type —
    the signature of the earlier bug that stamped a child video's URL onto an image record
    (video bytes saved under media/images, so its thumbnail couldn't be made). Only judged when
    the URL carries a definitive extension; canvas /content URLs are extensionless and trusted."""
    url = str(record.get("source_url") or "")
    if not Path(urlparse(url).path).suffix:
        return False
    return media_type_for(url) != str(record.get("media_type") or "")


def _drop_thumbnail(item_id: str) -> None:
    """Delete the cached thumbnail for an id so the index step regenerates it from the refreshed
    media (generate_missing only makes MISSING ones). Mirrors thumbnails.thumb_path layout."""
    try:
        thumb = GALLERY_ROOT / "thumbnails" / media_shard(item_id) / f"{item_id}.jpg"
        if thumb.is_file():
            thumb.unlink()
    except OSError:
        pass


def refresh_hd(
    client: httpx.Client,
    media_spec: RequestSpec,
    raw: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    args: argparse.Namespace,
) -> bool:
    """Re-download an item we already have because Grok now offers a higher-resolution variant
    (an upscale — e.g. SD→1080p), same id, new asset URL. Overwrites the file in place and
    refreshes ONLY the changed fields on the existing record — source_url, local_path,
    dimensions, content_hash — preserving everything else (subtitles, flags, etc.). Drops the
    stale thumbnail so the index step rebuilds it. Returns True on success."""
    global REFRESHED
    item_id = str(raw["id"])
    new_url = str(raw["source_url"])
    media_type = resolve_media_type(raw)
    base = Path("media/videos" if media_type == "video" else "media/images")
    folder = base / media_shard(item_id)
    old = by_id[item_id]
    try:
        (GALLERY_ROOT / folder).mkdir(parents=True, exist_ok=True)
        local = download_media(client, media_spec, new_url, GALLERY_ROOT / folder / item_id)
    except Exception as exc:
        append_failure(args.failures, {"id": item_id, "source_url": new_url, "error": str(exc)})
        print(f"failed HD refresh {item_id}: {exc}")
        return False
    # Everything past the download is wrapped so a single malformed item can't throw out
    # of process_item and abort the whole sync (the worker's outer catch would). On failure
    # the record keeps its old SD URL, so the next sync simply retries.
    try:
        new_rel = local.relative_to(GALLERY_ROOT).as_posix()
        # If the refreshed file landed at a different path (ext change, or old legacy/flat
        # layout), remove the now-orphaned old file so we don't leave the stale small copy.
        old_rel = str(old.get("local_path") or "").replace("\\", "/")
        if old_rel and old_rel != new_rel:
            try:
                old_file = GALLERY_ROOT / old_rel
                if old_file.is_file():
                    old_file.unlink()
            except OSError:
                pass
        # Merge: keep the existing record (subtitles/flags/etc.) and update only what changed.
        # Each field is taken from the fresh download only when present, so a sparse list item
        # never nulls out good existing metadata (e.g. a canvas_id).
        fresh = normalize_record(raw, local.relative_to(GALLERY_ROOT)).__dict__
        record = dict(old)
        record["source_url"] = new_url
        record["local_path"] = new_rel
        record["content_hash"] = file_content_hash(local)
        for key in ("width", "height", "media_type", "model", "canvas_id", "canvas_name"):
            if fresh.get(key) is not None:
                record[key] = fresh[key]
        by_id[item_id] = record
        save_metadata(args.metadata, list(by_id.values()))
        _drop_thumbnail(item_id)
    except Exception as exc:  # noqa: BLE001 - never let one item abort the sync
        print(f"failed HD refresh record {item_id}: {exc}")
        return False
    REFRESHED += 1
    if not args.quiet:
        print(f"upgraded {item_id} -> {local} (upscaled)")
    return True


def process_item(
    client: httpx.Client,
    media_spec: RequestSpec,
    raw: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    args: argparse.Namespace,
) -> bool:
    """Download one media item and record it. Returns True if a new file was saved."""
    item_id = str(raw["id"])
    if item_id in DELETED_IDS:
        return False  # user deleted it; never bring it back
    if item_id in by_id:
        # Self-heal upscales: if Grok now offers a HIGHER-resolution variant for an id we
        # already have (e.g. SD→1080p, or 720p→1080p), re-download and replace it in place.
        # Strictly-higher tier guard means a transient lower-res listing can never downgrade
        # a file we already upgraded. Also re-fetch records left mis-typed by the earlier
        # nested-videos bug (a video URL saved onto an image record) so they self-correct.
        old = by_id[item_id]
        new_url = str(raw.get("source_url") or "")
        upgrade = bool(new_url) and _media_res_rank(new_url) > _media_res_rank(str(old.get("source_url") or ""))
        if upgrade or _record_mistyped(old):
            refresh_hd(client, media_spec, raw, by_id, args)
        return False
    source_url = str(raw["source_url"])
    media_type = resolve_media_type(raw)
    base = Path("media/videos" if media_type == "video" else "media/images")
    folder = base / media_shard(item_id)
    # If the media file is already on disk (e.g. copied in from another machine)
    # but missing from metadata, index it instead of re-downloading. Check the
    # sharded location first, then the legacy flat one.
    existing_file = (next(iter((GALLERY_ROOT / folder).glob(f"{item_id}.*")), None)
                     or next(iter((GALLERY_ROOT / base).glob(f"{item_id}.*")), None))
    if existing_file is not None and existing_file.is_file():
        record = normalize_record(raw, existing_file.relative_to(GALLERY_ROOT))
        by_id[item_id] = record.__dict__
        save_metadata(args.metadata, list(by_id.values()))
        if not args.quiet:
            print(f"indexed existing {item_id} -> {existing_file}")
        return False
    try:
        (GALLERY_ROOT / folder).mkdir(parents=True, exist_ok=True)
        local = download_media(client, media_spec, source_url, GALLERY_ROOT / folder / item_id)
    except Exception as exc:
        append_failure(
            args.failures,
            {"id": item_id, "source_url": source_url, "error": str(exc)},
        )
        print(f"failed {item_id}: {exc}")
        return False
    # Store local_path relative to GALLERY_ROOT (e.g. media/images/<id>.jpg).
    record = normalize_record(raw, local.relative_to(GALLERY_ROOT))
    by_id[item_id] = record.__dict__
    save_metadata(args.metadata, list(by_id.values()))
    if not args.quiet:
        print(f"saved {item_id} -> {local}")
    return True


def patch_existing_record(raw: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> bool:
    """Refresh metadata-only fields on an already-downloaded record. Returns True if changed.

    Used by --refresh-metadata to backfill fields (notably created_at) onto records
    that were saved before the field was captured, without re-downloading the media.
    """
    record = by_id.get(str(raw["id"]))
    if record is None:
        return False
    changed = False
    created_at = first_value(raw, DATE_KEYS)
    if created_at and not record.get("created_at"):
        record["created_at"] = str(created_at)
        changed = True
    model = first_value(raw, MODEL_KEYS)
    if model and not record.get("model"):
        record["model"] = str(model)
        changed = True
    prompt = first_value(raw, PROMPT_KEYS)
    if prompt and not record.get("prompt"):
        record["prompt"] = str(prompt)
        changed = True
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description="Download media and prompt metadata from browser cURL samples.")
    parser.add_argument("--curl", type=Path, default=Path("grok_auth.txt"))
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--failures", type=Path, default=DEFAULT_FAILURES)
    parser.add_argument("--deleted", type=Path, default=DEFAULT_DELETED)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--page-size", type=int, default=40)
    parser.add_argument("--quiet", action="store_true", help="Print compact progress instead of one line per file.")
    parser.add_argument(
        "--grok-favorites",
        action="store_true",
        help="Use Grok's known favorites endpoint and use grok_auth.txt only for auth cookies/headers.",
    )
    parser.add_argument(
        "--grok-agents",
        nargs="*",
        metavar="CANVAS_ID",
        default=None,
        help=(
            "Download media from Grok Agent canvases (/imagine/agent/<id>). "
            "With no IDs, archives every canvas on /imagine/saved; otherwise only the given canvas IDs/URLs."
        ),
    )
    parser.add_argument(
        "--grok-posts",
        nargs="+",
        metavar="POST_ID",
        default=None,
        help=(
            "Download specific Grok Imagine posts by id or /imagine/post/<id> URL "
            "(root media, original/base media, and child posts)."
        ),
    )
    parser.add_argument(
        "--early-stop-existing-page",
        action="store_true",
        help="Stop when a fetched page contains only IDs already present in metadata.json.",
    )
    parser.add_argument(
        "--refresh-metadata",
        action="store_true",
        help="Re-fetch the list(s) and backfill metadata (e.g. created_at) on existing records without downloading media.",
    )
    args = parser.parse_args()

    curl_path = args.curl
    if not curl_path.exists():
        legacy = curl_path.with_name("curl_samples.txt")  # pre-rename filename
        if legacy.exists():
            curl_path = legacy
    specs = parse_curl_samples(curl_path)
    auth_spec = choose_grok_auth_spec(specs)
    media_spec = specs[-1]
    existing = load_metadata(args.metadata)
    by_id = {str(item["id"]): item for item in existing if "id" in item}
    global DELETED_IDS
    DELETED_IDS = load_deleted_ids(args.deleted)

    with httpx.Client(follow_redirects=True) as client:
        saved_count = 0

        def report() -> None:
            if args.quiet and saved_count and saved_count % 100 == 0:
                print(f"saved {saved_count} new files; metadata records: {len(by_id)}")

        if args.grok_posts is not None:
            saved_count = archive_posts(client, auth_spec, media_spec, by_id, args)
        elif args.grok_agents is not None:
            saved_count = archive_agent_canvases(client, auth_spec, media_spec, by_id, args)
        else:
            list_spec = grok_favorites_spec(auth_spec, args.page_size) if args.grok_favorites else specs[0]
            for page_data in iter_pages(client, list_spec, args.max_pages):
                page_items = extract_media_items(page_data)
                if args.refresh_metadata:
                    for raw in page_items:
                        saved_count += patch_existing_record(raw, by_id)
                    continue
                if args.early_stop_existing_page and page_items and all(str(raw["id"]) in by_id for raw in page_items):
                    print("page contained only existing IDs; stopping early")
                    break
                for raw in page_items:
                    if process_item(client, media_spec, raw, by_id, args):
                        saved_count += 1
                        report()

    save_metadata(args.metadata, list(by_id.values()))
    if args.refresh_metadata:
        print(f"refreshed {saved_count} record(s); metadata records: {len(by_id)}")
    else:
        print(f"metadata records: {len(by_id)}")
    if REFRESHED:
        print(f"HD upgrades: {REFRESHED}")


def normalize_canvas_id(value: str) -> str:
    """Accept either a bare canvas id or a full /imagine/agent/<id> URL."""
    value = value.strip()
    if "/" in value:
        return value.rstrip("/").rsplit("/", 1)[-1]
    return value


def normalize_post_id(value: str) -> str:
    """Accept a bare post id or a full /imagine/post/<id> URL (with optional query/fragment)."""
    value = value.strip()
    if "/" in value:
        path = urlparse(value).path or value  # drops ?query and #fragment
        value = path.rstrip("/").rsplit("/", 1)[-1]
    return value


def archive_posts(
    client: httpx.Client,
    auth_spec: RequestSpec,
    media_spec: RequestSpec,
    by_id: dict[str, dict[str, Any]],
    args: argparse.Namespace,
) -> int:
    """Download specific posts by id/URL, reusing the same per-post extraction
    the favorites list uses."""
    requested = [normalize_post_id(v) for v in args.grok_posts]
    print(f"fetching {len(requested)} post(s)")
    saved_count = 0
    for post_id in requested:
        post_json = request_json_with_backoff(client, grok_post_get_spec(auth_spec, post_id))
        post = post_json.get("post") if isinstance(post_json, dict) else None
        if not isinstance(post, dict):
            print(f"post {post_id}: not found or unexpected response")
            continue
        items = extract_grok_media_items({"posts": [post]})
        print(f"post {post_id}: {len(items)} media item(s)")
        for raw in items:
            if args.refresh_metadata:
                saved_count += patch_existing_record(raw, by_id)
                continue
            if process_item(client, media_spec, raw, by_id, args):
                saved_count += 1
                if args.quiet and saved_count % 100 == 0:
                    print(f"saved {saved_count} new files; metadata records: {len(by_id)}")
    return saved_count


def archive_agent_canvases(
    client: httpx.Client,
    auth_spec: RequestSpec,
    media_spec: RequestSpec,
    by_id: dict[str, dict[str, Any]],
    args: argparse.Namespace,
) -> int:
    if args.grok_agents:
        requested = [normalize_canvas_id(v) for v in args.grok_agents]
        names = {cid: name for cid, name in list_grok_canvases(client, auth_spec)}
        canvases = [(cid, names.get(cid, cid)) for cid in requested]
    else:
        canvases = list_grok_canvases(client, auth_spec)

    print(f"found {len(canvases)} agent canvas(es)")
    saved_count = 0
    for canvas_id, canvas_name in canvases:
        canvas_json = request_json_with_backoff(client, grok_canvas_get_spec(auth_spec, canvas_id))
        items = extract_canvas_items(canvas_json, canvas_id, canvas_name)
        print(f"canvas {canvas_id} '{canvas_name}': {len(items)} media items")
        for raw in items:
            if args.refresh_metadata:
                saved_count += patch_existing_record(raw, by_id)
                continue
            if process_item(client, media_spec, raw, by_id, args):
                saved_count += 1
                if args.quiet and saved_count % 100 == 0:
                    print(f"saved {saved_count} new files; metadata records: {len(by_id)}")
    return saved_count


if __name__ == "__main__":
    main()
