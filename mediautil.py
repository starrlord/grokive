"""Shared, dependency-light helpers for prompt grouping and tag extraction.

Extracted from the old gallery builders so the SQLite index (db.py) and any
other tooling can reuse them without importing the large legacy HTML template.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any


def media_shard(media_id: str) -> str:
    """Two-hex-char bucket for a media id, e.g. ``media/videos/<ab>/<id>.mp4``.

    Hashing the id (rather than slicing it) keeps the 256 buckets evenly filled
    no matter the id's shape — UUIDs, ``montage_*`` ids, and base64-of-URL
    filename stems would otherwise clump (every base64 stem starts ``aH...``)."""
    return hashlib.sha1(str(media_id).encode("utf-8")).hexdigest()[:2]


def file_content_hash(path: Path, _chunk: int = 1 << 20) -> str:
    """blake2b digest of a file's bytes, for exact-duplicate detection."""
    h = hashlib.blake2b(digest_size=20)
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(_chunk), b""):
            h.update(block)
    return h.hexdigest()


STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "by", "for", "from", "in", "into", "is", "it", "of",
    "on", "or", "the", "to", "with", "without", "this", "that", "these", "those", "be", "being",
    "style", "image", "video", "photo", "picture", "generate", "make", "create", "showing",
}


def normalize_prompt(prompt: str) -> str:
    return re.sub(r"\s+", " ", prompt.lower()).strip(" \t\r\n.!?;:")


def media_rel_path(item: dict[str, Any]) -> str:
    """local_path may have been written on Windows (backslashes); make it POSIX
    so it resolves on any platform."""
    return str(item.get("local_path", "")).replace("\\", "/")


def group_key_and_label(item: dict[str, Any]) -> tuple[str, str]:
    """Return (bucket_key, display_label) for grouping an item.

    Items group by prompt so each keeps its own prompt text. Canvas (Agent) media
    with no prompt falls back to one bucket per canvas, labelled with the canvas
    name, so it isn't dumped into the single global 'untitled' bucket.
    """
    prompt = item.get("prompt", "") or ""
    key = normalize_prompt(prompt)
    if key:
        return key, prompt
    canvas_id = item.get("canvas_id")
    canvas_name = item.get("canvas_name")
    if canvas_id and canvas_name:
        return f"canvas:{canvas_id}", canvas_name
    return "", ""


def tokens(prompt: str) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", prompt.lower())
    return [word for word in words if word not in STOPWORDS]


def tags_for_groups(groups: list[dict[str, Any]]) -> None:
    """Assign up to 8 representative tags to each group (in place), ranked by how
    distinctive each unigram/bigram is via TF-IDF (each group's prompt is one
    document). This surfaces what makes a clip *different* from the rest of the
    library rather than the boilerplate every prompt shares — the same scoring the
    Prompt Studio cluster tagger (_label_clusters) uses. A frequency rank would do
    the opposite: pick the most common library-wide words and drop the unique ones."""
    per_group: list[Counter] = []
    doc_freq: Counter = Counter()
    for group in groups:
        term_list = tokens(group["prompt"])
        bigrams = [f"{a} {b}" for a, b in zip(term_list, term_list[1:])]
        cnt: Counter = Counter(term_list + bigrams)
        per_group.append(cnt)
        doc_freq.update(cnt.keys())  # in how many groups each term appears

    n = max(1, len(groups))
    for group, cnt in zip(groups, per_group):
        scored = [
            (freq * (math.log((n + 1) / doc_freq[term]) + 1.0), freq, term)
            for term, freq in cnt.items()
        ]
        scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
        kept: list[str] = []
        covered: set[str] = set()
        for _, _, term in scored:
            words = term.split()
            if len(words) == 1 and words[0] in covered:
                continue  # already represented by a chosen bigram
            kept.append(term)
            if len(words) > 1:
                covered.update(words)
            if len(kept) >= 8:
                break
        group["tags"] = kept
