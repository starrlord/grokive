"""SQLite read-model for the gallery.

This is a *derived* index built from ``metadata.json`` (the download ledger) plus
what's on disk (thumbnail dimensions, subtitle sidecars). It is safe to delete and
rebuild at any time; the downloader never writes here. The web API queries this DB
instead of shipping the whole library to the browser, which gives fast paginated
results and real full-text prompt search (FTS5).

Favorites / archive / playlists still live in their JSON files for now and are
applied as filters by the API layer; a later phase can fold them into tables here.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Iterable

from mediautil import group_key_and_label, media_rel_path, tags_for_groups

try:
    from PIL import Image
except Exception:  # pragma: no cover - Pillow always present in practice
    Image = None  # type: ignore


SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
  id                TEXT PRIMARY KEY,
  media_type        TEXT,
  prompt            TEXT,
  normalized_prompt TEXT,
  model             TEXT,
  created_at        TEXT,
  parent_id         TEXT,
  source_url        TEXT,
  local_path        TEXT,
  href              TEXT,
  thumb             TEXT,
  subtitles         TEXT,
  canvas_id         TEXT,
  canvas_name       TEXT,
  thumb_w           INTEGER,
  thumb_h           INTEGER,
  media_w           INTEGER,
  media_h           INTEGER,
  has_subtitles     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_media_created ON media(created_at);
CREATE INDEX IF NOT EXISTS idx_media_type    ON media(media_type);
CREATE INDEX IF NOT EXISTS idx_media_canvas  ON media(canvas_id);
CREATE INDEX IF NOT EXISTS idx_media_model   ON media(model);

CREATE TABLE IF NOT EXISTS media_tags (
  media_id TEXT,
  tag      TEXT,
  PRIMARY KEY (media_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON media_tags(tag);

CREATE VIRTUAL TABLE IF NOT EXISTS media_fts USING fts5(
  id UNINDEXED, prompt, tags, model, filename, tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

MEDIA_COLUMNS = [
    "id", "media_type", "prompt", "normalized_prompt", "model", "created_at",
    "parent_id", "source_url", "local_path", "href", "thumb", "subtitles",
    "canvas_id", "canvas_name", "thumb_w", "thumb_h", "media_w", "media_h",
    "has_subtitles",
]


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _thumb_dims(path: Path) -> tuple[int | None, int | None]:
    if Image is None or not path.exists():
        return None, None
    try:
        with Image.open(path) as img:
            return img.width, img.height
    except Exception:
        return None, None


def _video_dims(path: Path) -> tuple[int | None, int | None]:
    """Source video pixel dimensions via ffprobe (None,None if unavailable)."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not path.exists():
        return None, None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        w, _, h = out.partition("x")
        return int(w), int(h)
    except Exception:
        return None, None


def _media_dims(media_type: str | None, path: Path) -> tuple[int | None, int | None]:
    """Real source dimensions of a media file: ffprobe for video, Pillow for images."""
    if media_type == "video":
        return _video_dims(path)
    return _thumb_dims(path)  # any image format Pillow can open


def _rel_href(local_path: str, gallery_dir: Path) -> str:
    """Browser-relative path to a media file, matching how the gallery serves it
    (``/media/...`` and ``/thumbnails/...`` under the gallery root)."""
    return local_path.replace("\\", "/")


def build_index(
    db_path: str | Path,
    metadata_path: str | Path,
    gallery_dir: str | Path,
    thumbnails_dir: str | Path | None = None,
) -> int:
    """(Re)build the index from metadata.json. Returns the number of rows written."""
    gallery_dir = Path(gallery_dir)
    thumbnails_dir = Path(thumbnails_dir) if thumbnails_dir else gallery_dir / "thumbnails"
    metadata_path = Path(metadata_path)
    items: list[dict[str, Any]] = []
    if metadata_path.exists():
        try:
            loaded = json.loads(metadata_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                items = [it for it in loaded if isinstance(it, dict) and it.get("id")]
        except Exception:
            items = []

    # Group by normalized prompt and compute group-level tags, exactly like the
    # static gallery, then assign each group's tags to its member media.
    buckets: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in items:
        key, label = group_key_and_label(item)
        if key not in buckets:
            buckets[key] = {"prompt": label, "ids": []}
            order.append(key)
        buckets[key]["ids"].append(item["id"])
    groups = [{"prompt": buckets[k]["prompt"]} for k in order]
    tags_for_groups(groups)
    tags_by_id: dict[str, list[str]] = {}
    for key, group in zip(order, groups):
        for mid in buckets[key]["ids"]:
            tags_by_id[mid] = group.get("tags", [])

    conn = _connect(db_path)
    try:
        conn.executescript(SCHEMA)
        # Migrate pre-existing DBs: CREATE TABLE IF NOT EXISTS won't add new columns.
        for col in ("media_w", "media_h"):
            try:
                conn.execute(f"ALTER TABLE media ADD COLUMN {col} INTEGER")
            except sqlite3.OperationalError:
                pass  # already present
        # Carry probed dimensions forward from the previous build so we only ffprobe
        # each video once (not on every startup/sync rebuild).
        try:
            dim_cache = {
                r[0]: (r[1], r[2]) for r in conn.execute(
                    "SELECT id, media_w, media_h FROM media WHERE media_w IS NOT NULL AND media_h IS NOT NULL"
                )
            }
        except sqlite3.OperationalError:
            dim_cache = {}
        conn.execute("DELETE FROM media")
        conn.execute("DELETE FROM media_tags")
        conn.execute("DELETE FROM media_fts")
        media_rows = []
        tag_rows = []
        fts_rows = []
        for item in items:
            mid = item["id"]
            rel = media_rel_path(item)
            href = _rel_href(rel, gallery_dir)
            thumb_path = thumbnails_dir / f"{mid}.jpg"
            tw, th = _thumb_dims(thumb_path)
            # Real source dimensions for the resolution badge: prefer values captured
            # from Grok at download time, else the previous build's cache, else probe
            # the file once (ffprobe/Pillow) and let the next build read it from cache.
            mw, mh = item.get("width"), item.get("height")
            if not (mw and mh):
                mw, mh = dim_cache.get(mid) or _media_dims(item.get("media_type"), gallery_dir / rel)
            thumb_href = f"thumbnails/{mid}.jpg" if thumb_path.exists() else None
            vtt = (gallery_dir / rel).with_suffix(".vtt")
            subtitles = href.rsplit(".", 1)[0] + ".vtt" if vtt.exists() else None
            tags = tags_by_id.get(mid, [])
            key, _ = group_key_and_label(item)
            media_rows.append((
                mid, item.get("media_type"), item.get("prompt") or "", key,
                item.get("model"), item.get("created_at"), item.get("parent_id"),
                item.get("source_url"), rel, href, thumb_href, subtitles,
                item.get("canvas_id"), item.get("canvas_name"), tw, th, mw, mh,
                1 if subtitles else 0,
            ))
            for tag in tags:
                tag_rows.append((mid, tag))
            fts_rows.append((
                mid, item.get("prompt") or "", " ".join(tags),
                item.get("model") or "", rel.rsplit("/", 1)[-1],
            ))
        conn.executemany(
            f"INSERT OR REPLACE INTO media ({','.join(MEDIA_COLUMNS)}) "
            f"VALUES ({','.join('?' for _ in MEDIA_COLUMNS)})",
            media_rows,
        )
        conn.executemany("INSERT OR IGNORE INTO media_tags (media_id, tag) VALUES (?, ?)", tag_rows)
        conn.executemany(
            "INSERT INTO media_fts (id, prompt, tags, model, filename) VALUES (?, ?, ?, ?, ?)",
            fts_rows,
        )
        conn.commit()
        return len(media_rows)
    finally:
        conn.close()


def _fts_query(raw: str) -> str:
    """Turn user text into a safe FTS5 prefix query: each term ANDed, prefix-matched."""
    terms = [t for t in "".join(c if c.isalnum() else " " for c in raw).split() if t]
    return " AND ".join(f'"{t}"*' for t in terms)


def _temp_id_table(conn: sqlite3.Connection, name: str, ids: Iterable[str]) -> bool:
    ids = list(ids)
    if not ids:
        return False
    conn.execute(f"CREATE TEMP TABLE {name} (id TEXT PRIMARY KEY)")
    conn.executemany(f"INSERT OR IGNORE INTO {name} (id) VALUES (?)", [(i,) for i in ids])
    return True


def query_media(
    db_path: str | Path,
    *,
    view: str = "files",
    q: str = "",
    tags: Iterable[str] = (),
    models: Iterable[str] = (),
    canvas: str | None = None,
    media_type: str = "all",
    resolutions: Iterable[int] = (),
    sort: str = "new",
    page: int = 1,
    page_size: int = 120,
    favorites: Iterable[str] = (),
    stashed: Iterable[str] = (),
    collection_ids: Iterable[str] = (),
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Paginated, filtered media query. ``favorites``/``stashed`` are id sets the
    caller supplies (from library.json); they drive Favorites/Archive and the
    global "hide archived from Recent" rule. ``all`` intentionally bypasses that
    hiding so users can find anything that still exists on disk."""
    conn = _connect(db_path)
    try:
        has_stash = _temp_id_table(conn, "_stash", stashed)
        has_fav = _temp_id_table(conn, "_fav", favorites)
        has_collection = _temp_id_table(conn, "_collection", collection_ids)

        where: list[str] = []
        params: list[Any] = []
        joins = ""

        if has_collection:
            where.append("m.id IN (SELECT id FROM _collection)")

        if q.strip():
            match = _fts_query(q)
            if match:
                joins += " JOIN media_fts f ON f.id = m.id"
                where.append("media_fts MATCH ?")
                params.append(match)

        view = "recent" if view == "files" else ("archive" if view == "stashed" else view)
        if view == "archive":
            where.append("m.id IN (SELECT id FROM _stash)" if has_stash else "0")
        else:
            if has_stash and view not in ("all", "favorites", "canvases", "collections"):
                where.append("m.id NOT IN (SELECT id FROM _stash)")
            if view == "favorites":
                where.append("m.id IN (SELECT id FROM _fav)" if has_fav else "0")

        for tag in tags:
            where.append("EXISTS (SELECT 1 FROM media_tags mt WHERE mt.media_id = m.id AND mt.tag = ?)")
            params.append(tag)

        model_list = list(models)
        if model_list:
            clauses = []
            for model in model_list:
                if model == "Unknown model":
                    clauses.append("m.model IS NULL OR m.model = ''")
                else:
                    clauses.append("m.model = ?")
                    params.append(model)
            where.append("(" + " OR ".join(clauses) + ")")

        if canvas:
            where.append("m.canvas_id = ?")
            params.append(canvas)
        res_list = [int(r) for r in resolutions if str(r).strip().lstrip("-").isdigit()]
        if res_list:
            # Match on the shorter side (portrait-safe), same value the badge shows.
            where.append(f"MIN(m.media_w, m.media_h) IN ({','.join('?' for _ in res_list)})")
            params.extend(res_list)
        if media_type in ("image", "video"):
            where.append("m.media_type = ?")
            params.append(media_type)
        # Date bounds compare lexicographically against ISO created_at (works for
        # both "YYYY-MM-DD" and full "YYYY-MM-DDT..." timestamps).
        if start:
            where.append("m.created_at >= ?")
            params.append(start)
        if end:
            where.append("m.created_at < ?")
            params.append(end)

        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        order_sql = {
            "old": "m.created_at ASC",
            "prompt": "m.prompt COLLATE NOCASE ASC",
            "model": "m.model COLLATE NOCASE ASC",
        }.get(sort, "m.created_at DESC")

        total = conn.execute(f"SELECT COUNT(*) FROM media m{joins}{where_sql}", params).fetchone()[0]
        page = max(1, int(page))
        page_size = max(1, min(500, int(page_size)))
        rows = conn.execute(
            f"SELECT m.* FROM media m{joins}{where_sql} "
            f"ORDER BY {order_sql} LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
        items = []
        for row in rows:
            d = dict(row)
            d["tags"] = [
                r[0] for r in conn.execute(
                    "SELECT tag FROM media_tags WHERE media_id = ? ORDER BY tag", (d["id"],)
                )
            ]
            d["has_subtitles"] = bool(d["has_subtitles"])
            items.append(d)
        return {"total": total, "page": page, "page_size": page_size, "items": items}
    finally:
        conn.close()


def media_by_ids(db_path: str | Path, ids: list[str]) -> list[dict[str, Any]]:
    """Resolve media rows for an explicit id list, preserving the given order
    (used to play/reorder/export a playlist)."""
    ids = [str(i) for i in ids]
    if not ids:
        return []
    conn = _connect(db_path)
    try:
        by_id: dict[str, dict] = {}
        CHUNK = 400
        for start in range(0, len(ids), CHUNK):
            chunk = ids[start:start + CHUNK]
            qmarks = ",".join("?" for _ in chunk)
            for row in conn.execute(f"SELECT * FROM media WHERE id IN ({qmarks})", chunk):
                d = dict(row)
                d["has_subtitles"] = bool(d["has_subtitles"])
                d["tags"] = [
                    r[0] for r in conn.execute("SELECT tag FROM media_tags WHERE media_id = ?", (d["id"],))
                ]
                by_id[d["id"]] = d
        return [by_id[i] for i in ids if i in by_id]
    finally:
        conn.close()


def delete_media(db_path: str | Path, ids: list[str]) -> int:
    """Remove media rows (and their tags / FTS entries) for the given ids. Returns
    the number of media rows deleted. The DB is derived, so this just keeps the
    read-model in sync after a hard delete without a full rebuild."""
    ids = [str(i) for i in ids]
    if not ids:
        return 0
    conn = _connect(db_path)
    try:
        deleted = 0
        CHUNK = 400
        for start in range(0, len(ids), CHUNK):
            chunk = ids[start:start + CHUNK]
            qmarks = ",".join("?" for _ in chunk)
            deleted += conn.execute(f"DELETE FROM media WHERE id IN ({qmarks})", chunk).rowcount
            conn.execute(f"DELETE FROM media_tags WHERE media_id IN ({qmarks})", chunk)
            conn.execute(f"DELETE FROM media_fts WHERE id IN ({qmarks})", chunk)
        conn.commit()
        return deleted
    finally:
        conn.close()


def facets(
    db_path: str | Path,
    *,
    view: str = "recent",
    q: str = "",
    canvas: str | None = None,
    media_type: str = "all",
    favorites: Iterable[str] = (),
    stashed: Iterable[str] = (),
    collection_ids: Iterable[str] = (),
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Tag / model / canvas / resolution counts for the current browsing scope."""
    conn = _connect(db_path)
    try:
        has_stash = _temp_id_table(conn, "_stash", stashed)
        has_fav = _temp_id_table(conn, "_fav", favorites)
        has_collection = _temp_id_table(conn, "_collection", collection_ids)

        where: list[str] = []
        params: list[Any] = []
        joins = ""

        if q.strip():
            match = _fts_query(q)
            if match:
                joins += " JOIN media_fts f ON f.id = m.id"
                where.append("media_fts MATCH ?")
                params.append(match)

        view = "recent" if view == "files" else ("archive" if view == "stashed" else view)
        if has_collection:
            where.append("m.id IN (SELECT id FROM _collection)")
        elif view == "archive":
            where.append("m.id IN (SELECT id FROM _stash)" if has_stash else "0")
        else:
            if has_stash and view not in ("all", "favorites", "canvases", "collections"):
                where.append("m.id NOT IN (SELECT id FROM _stash)")
            if view == "favorites":
                where.append("m.id IN (SELECT id FROM _fav)" if has_fav else "0")

        if canvas:
            where.append("m.canvas_id = ?")
            params.append(canvas)
        if media_type in ("image", "video"):
            where.append("m.media_type = ?")
            params.append(media_type)
        if start:
            where.append("m.created_at >= ?")
            params.append(start)
        if end:
            where.append("m.created_at < ?")
            params.append(end)

        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        tags = [
            {"name": r["tag"], "count": r["n"]}
            for r in conn.execute(
                f"SELECT mt.tag, COUNT(*) n FROM media_tags mt JOIN media m ON m.id = mt.media_id{joins}{where_sql} "
                f"GROUP BY mt.tag ORDER BY n DESC, mt.tag",
                params,
            )
        ]
        models = [
            {"name": r["model"] or "Unknown model", "count": r["n"]}
            for r in conn.execute(
                f"SELECT COALESCE(NULLIF(m.model,''), NULL) model, COUNT(*) n FROM media m{joins}{where_sql} "
                f"GROUP BY model ORDER BY n DESC",
                params,
            )
        ]
        canvases = [
            {"id": r["canvas_id"], "name": r["canvas_name"] or r["canvas_id"],
             "count": r["n"], "videos": r["v"], "images": r["i"], "cover": r["cover"]}
            for r in conn.execute(
                f"SELECT m.canvas_id, m.canvas_name, COUNT(*) n, "
                f"SUM(m.media_type='video') v, SUM(m.media_type='image') i, "
                f"MAX(m.thumb) cover FROM media m{joins}{where_sql}"
                f"{' AND' if where_sql else ' WHERE'} m.canvas_id IS NOT NULL "
                f"GROUP BY m.canvas_id ORDER BY n DESC",
                params,
            )
        ]
        res_where = (where_sql + " AND" if where_sql else " WHERE") + \
            " m.media_w IS NOT NULL AND m.media_h IS NOT NULL"
        resolutions = [
            {"height": r["h"], "count": r["n"]}
            for r in conn.execute(
                f"SELECT MIN(m.media_w, m.media_h) AS h, COUNT(*) n FROM media m{joins}{res_where} "
                f"GROUP BY h ORDER BY h DESC",
                params,
            )
        ]
        total = conn.execute(f"SELECT COUNT(*) FROM media m{joins}{where_sql}", params).fetchone()[0]
        return {"tags": tags, "models": models, "canvases": canvases,
                "resolutions": resolutions, "total": total}
    finally:
        conn.close()
