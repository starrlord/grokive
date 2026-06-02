"""Thumbnail generation, extracted from the old gallery builders.

Image thumbnails use Pillow; video thumbnails grab a frame via ffmpeg. The web
API (db.py) reads these thumbnails for display and aspect ratios, so generating
them is a normal step of a sync.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from mediautil import media_rel_path


def thumb_path(item: dict[str, Any], thumbnails_dir: Path) -> Path:
    return thumbnails_dir / f"{item['id']}.jpg"


def make_image_thumb(source: Path, dest: Path) -> bool:
    try:
        with Image.open(source) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((400, 400))
            if img.mode in {"RGBA", "LA"}:
                background = Image.new("RGB", img.size, (20, 20, 20))
                background.paste(img, mask=img.getchannel("A"))
                img = background
            else:
                img = img.convert("RGB")
            dest.parent.mkdir(parents=True, exist_ok=True)
            img.save(dest, "JPEG", quality=84)
        return True
    except Exception as exc:
        print(f"thumbnail failed for {source}: {exc}")
        return False


def make_video_thumb(source: Path, dest: Path) -> bool:
    if not shutil.which("ffmpeg"):
        print("ffmpeg not found; video thumbnails skipped")
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg", "-y", "-ss", "0.5", "-i", str(source), "-vframes", "1", "-vf",
        "scale='min(400,iw)':-1", str(dest),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        print(f"thumbnail timed out for {source}")
        return False
    if result.returncode != 0:
        print(f"thumbnail failed for {source}: {result.stderr.strip()}")
        return False
    return True


def make_thumb(item: dict[str, Any], thumbnails_dir: Path, media_root: Path) -> bool:
    source = media_root / media_rel_path(item)
    dest = thumb_path(item, thumbnails_dir)
    if dest.exists():
        return True
    if item.get("media_type") == "video":
        return make_video_thumb(source, dest)
    return make_image_thumb(source, dest)


def generate_missing(
    metadata_path: str | Path,
    gallery_dir: str | Path,
    thumbnails_dir: str | Path | None = None,
) -> int:
    """Create thumbnails for any media in metadata.json that lacks one.
    Returns the number of thumbnails newly generated."""
    metadata_path = Path(metadata_path)
    gallery_dir = Path(gallery_dir)
    thumbnails_dir = Path(thumbnails_dir) if thumbnails_dir else gallery_dir / "thumbnails"
    if not metadata_path.exists():
        return 0
    try:
        items = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    processed = 0
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        if thumb_path(item, thumbnails_dir).exists():
            continue
        if make_thumb(item, thumbnails_dir, gallery_dir):
            processed += 1
    return processed
