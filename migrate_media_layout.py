"""One-time migration: move a flat media/ + thumbnails/ library into hashed
shard subdirectories (media/<type>/<ab>/<id>.<ext>, thumbnails/<ab>/<id>.jpg).

Idempotent and dry-run by default — re-running finishes an interrupted run and
reports "nothing to do" once everything is sharded. Move-only; never deletes.

    python migrate_media_layout.py [--data-dir DIR] [--apply]

Without --apply it only reports what it would move. The server rebuilds index.db
from metadata.json + disk on startup/sync, so no DB edits are needed here.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from mediautil import media_shard
from reindex import _relocate


def _desired_rel(local_path: str, media_id: str) -> str | None:
    """Sharded local_path for a record, or None if it doesn't look like media/*."""
    parts = local_path.replace("\\", "/").split("/")
    if len(parts) < 2 or parts[0] != "media":
        return None
    media_type, filename = parts[1], parts[-1]
    return f"media/{media_type}/{media_shard(media_id)}/{filename}"


def migrate(data_dir: Path, apply: bool) -> int:
    gallery = data_dir / "gallery"
    metadata_path = data_dir / "metadata.json"
    thumbs = gallery / "thumbnails"
    if not metadata_path.exists():
        print(f"no metadata.json at {metadata_path}")
        return 1
    records = json.loads(metadata_path.read_text(encoding="utf-8"))

    # When run as root (e.g. `docker exec`), any dir/file we create lands as
    # root:root, which the app (running as PUID:PGID) then can't write into. Mirror
    # the data dir's ownership onto everything we touch so the gallery stays
    # writable. No-op on Windows / when not root.
    owner = None
    if hasattr(os, "geteuid") and os.geteuid() == 0 and hasattr(os, "chown"):
        st = (gallery if gallery.exists() else data_dir).stat()
        owner = (st.st_uid, st.st_gid)

    def _own(p: Path) -> None:
        if owner and p.exists():
            try:
                os.chown(p, owner[0], owner[1])
            except OSError:
                pass

    moved = already = missing = thumbs_moved = sidecars = 0
    changed = False
    for rec in records:
        mid, lp = str(rec.get("id") or ""), str(rec.get("local_path") or "")
        if not mid or not lp:
            continue
        desired = _desired_rel(lp, mid)
        if desired is None:
            continue
        cur, dst = gallery / lp.replace("\\", "/"), gallery / desired
        if lp.replace("\\", "/") == desired and cur.is_file():
            already += 1
            continue
        if cur.is_file():
            # Count subtitle sidecars that will travel with it (for the report).
            sidecars += sum(1 for e in (".srt", ".vtt") if cur.with_suffix(e).is_file())
            if apply:
                _relocate(cur, dst.parent)            # moves media + sidecars
                _own(dst.parent); _own(dst)
                for e in (".srt", ".vtt"):
                    _own(dst.with_suffix(e))
                rec["local_path"] = desired
                changed = True
            moved += 1
        elif dst.is_file():
            # File already moved on a prior run; just heal the stale local_path.
            if rec.get("local_path") != desired:
                rec["local_path"] = desired
                changed = True
            already += 1
        else:
            missing += 1

        # Thumbnail: flat -> sharded (thumbnails aren't tracked in metadata).
        flat_thumb = thumbs / f"{mid}.jpg"
        shard_thumb = thumbs / media_shard(mid) / f"{mid}.jpg"
        if flat_thumb.is_file() and not shard_thumb.is_file():
            thumbs_moved += 1
            if apply:
                shard_thumb.parent.mkdir(parents=True, exist_ok=True)
                os.replace(flat_thumb, shard_thumb)
                _own(shard_thumb.parent); _own(shard_thumb)

    if apply and changed:
        tmp = metadata_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        tmp.replace(metadata_path)

    verb = "moved" if apply else "would move"
    print(f"{'APPLIED' if apply else 'DRY RUN'}: {len(records)} records | "
          f"{verb} {moved} media (+{sidecars} sidecars), {verb} {thumbs_moved} thumbnails | "
          f"{already} already sharded | {missing} missing on disk")
    if not apply and (moved or thumbs_moved):
        print("re-run with --apply to perform the migration.")
    elif apply:
        print("done — restart the server (or run Sync) to rebuild index.db.")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", type=Path,
                    default=Path(os.environ.get("GROK_DATA_DIR", "data")))
    ap.add_argument("--apply", action="store_true",
                    help="perform the moves (default is a dry run)")
    args = ap.parse_args()
    raise SystemExit(migrate(args.data_dir, args.apply))


if __name__ == "__main__":
    main()
