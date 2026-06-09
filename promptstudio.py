"""Prompt Studio — corpus mining + structured prompt composition (Phase 0).

Pure, dependency-light helpers (stdlib + ``mediautil``) so they stay testable and
can run without Flask, mirroring how ``moviegen`` is a self-contained library that
``server.py`` drives. No model calls live here yet:

* ``parse_components`` splits one prompt into the eight authoring slots below using
  cheap heuristics (quoted spans + keyword lexicons). Powers the "Remix" button.
* ``compose`` reassembles slot values back into a single prompt string. Powers the
  composer's live preview. ``parse_components`` and ``compose`` are rough inverses.
* ``mine_vocabulary`` turns the whole prompt corpus into per-slot chip palettes
  (frequent phrases) plus a de-duplicated prompt list, so a freshly-opened composer
  already speaks the operator's own vocabulary — with zero AI.

Later phases (local embeddings, local LLM) layer on top without changing this file.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import re
import sqlite3
import urllib.request
from collections import Counter

from mediautil import STOPWORDS, normalize_prompt

# The eight authoring slots, in the order ``compose`` lays them out. Keep this list
# and the frontend composer's field order in sync.
SLOTS: list[tuple[str, str]] = [
    ("subject", "Subject / Persona"),
    ("wardrobe", "Wardrobe"),
    ("setting", "Setting"),
    ("action", "Action"),
    ("dialogue", "Dialogue"),
    ("voice", "Voice / Accent"),
    ("camera", "Camera"),
    ("lighting", "Lighting / Look"),
    ("continuity", "Continuity"),
]
# Grok Imagine is a two-stage pipeline: a detailed still (text-to-image) is then animated
# with a SHORT motion prompt (image-to-video). These groups split the slots accordingly so
# the composer can emit one prompt per stage.
IMAGE_SLOTS = ["subject", "wardrobe", "setting", "lighting"]
MOTION_SLOTS = ["action", "camera", "voice", "dialogue", "continuity"]
SLOT_KEYS = [k for k, _ in SLOTS]
SLOT_LABELS = dict(SLOTS)

SAVED_RESPONSE_FOLDERS = [
    "Character Descriptions",
    "Actions / Motion",
    "Dialogue / Voice",
    "Scene Beats",
    "Style / Look",
    "Instructions / Format",
    "Unfiled",
]
SAVED_RESPONSE_TAGS = [
    "base-image",
    "appearance",
    "wardrobe",
    "body-type",
    "pose",
    "setting",
    "lighting",
    "portrait",
    "reference",
    "style",
    "action",
    "motion",
    "dialogue",
    "voice",
    "accent",
    "camera",
]

# Keyword lexicons for clause classification. Substring match on a lowercased clause;
# order of evaluation (see ``_classify``) resolves overlaps. Deliberately conservative
# — a missed clause just falls through to "action", which Remix lets the user fix.
_CAMERA = (
    "camera", "close-up", "close up", "closeup", "wide shot", "medium shot",
    "low angle", "high angle", "overhead", "over the shoulder", "pov",
    "point of view", "panning", "zoom", "tracking", "dolly", "handheld",
    "slow motion", "slow-motion", "framing", "lens", "depth of field", "angle", "shot",
)
_LIGHTING = (
    "lighting", "backlit", "cinematic", "film grain", "grainy", "35mm", "16mm",
    "vhs", "neon", "golden hour", "sunset", "candlelit", "candlelight", "shadows",
    "moody", "noir", "silhouette", "soft light", "harsh light", "natural light",
    "bokeh", "overexposed", "color grade", "saturated", "desaturated", "dramatic light",
)
_WARDROBE = (
    "wearing", "outfit", "clothes", "uniform",
    # tops / one-piece (women's, men's, unisex)
    "dress", "tank top", "tanktop", "crop top", "blouse", "t-shirt", "tshirt", "shirt",
    "button-up", "button-down", "polo", "hoodie", "sweater", "sweatshirt", "turtleneck",
    "flannel", "vest", "suit", "tuxedo", "blazer", "jacket", "coat", "trench coat", "tie",
    "robe", "nightgown",
    # bottoms
    "jeans", "denim", "skirt", "shorts", "slacks", "trousers", "khakis", "cargo",
    "joggers", "sweatpants", "overalls", "leggings",
    # swim / fabrics / footwear / accessories
    "bikini", "swim trunks", "trunks", "stockings", "leather", "lace", "sheer", "heels",
    "boots", "sneakers", "loafers", "sandals", "suspenders", "shirtless",
)
_SETTING = (
    "bedroom", "kitchen", "bathroom", "living room", "trailer", "porch", "backyard",
    "front yard", "lawn chair", "lawn", "beach", "poolside", "pool", "motel",
    "nightclub", "couch", "sofa", "in bed", "on the bed", "outdoors", "outdoor",
    "indoor", "background", "barn", "garage", "alley", "bedroom", "room",
)
_PERSONA = (
    "woman", "girl", "lady", "grandma", "granny", "mother", "housewife", "wife", "female",
    "man", "guy", "boy", "dude", "gentleman", "father", "dad", "husband", "grandpa",
    "grandfather", "male", "teen", "redneck", "blonde", "brunette", "redhead", "bearded",
    "muscular", "petite", "curvy", "year old", "years old", "year-old",
)
# Voice / accent / delivery — how lines are *spoken* (Grok lip-syncs native audio, so this
# rides on the motion prompt next to the dialogue).
_VOICE = (
    "accent", "drawl", "slurred", "raspy", "husky", "breathy", "monotone", "nasally",
    "midwestern", "valley girl", "british", "baby voice", "whispering", "whispery",
    "speaks in", "talking in", "voice",
)

# Tokenizer for chip mining: keep all word-ish tokens (so "close up" survives — note
# ``mediautil.tokens`` drops sub-3-char words like "up"). N-gram filtering below
# handles stopword noise. Apostrophes kept so "don't" stays one token.
_WORD = re.compile(r"[a-z][a-z'’-]*")
# Extra filler beyond mediautil's STOPWORDS that clutters chips but isn't worth
# globally stopword-ing in the index.
_FILLER = {
    "her", "his", "she", "he", "him", "they", "them", "their", "its", "very",
    "while", "both", "so", "then", "also", "one", "two", "using", "use", "you",
    "your", "i", "me", "my", "we", "us", "out", "up", "down", "off", "over",
}
_STOP = set(STOPWORDS) | _FILLER

_QUOTE = re.compile(r"[\"“”‘’']{1}([^\"“”]{3,}?)[\"“”'’]")
_QUOTE_PAIRS = {'"': '"', "“": "”", "'": "'", "‘": "’"}


def _dialogue_spans(text: str) -> list[dict]:
    """Return quoted spans with delimiters so dialogue-only enhancement can preserve the prompt."""
    s = str(text or "")
    spans: list[dict] = []
    i = 0
    while i < len(s):
        opener = s[i]
        closer = _QUOTE_PAIRS.get(opener)
        if not closer:
            i += 1
            continue
        # Avoid treating contractions or possessives as single-quote dialogue.
        if opener == "'" and i > 0 and s[i - 1].isalnum():
            i += 1
            continue
        j = i + 1
        while j < len(s):
            if s[j] == closer:
                if closer == "'" and j + 1 < len(s) and s[j + 1].isalnum():
                    j += 1
                    continue
                content = s[i + 1:j].strip()
                if content:
                    spans.append({"start": i, "end": j + 1, "open": opener, "close": closer, "text": content})
                i = j + 1
                break
            j += 1
        else:
            i += 1
    return spans


def _classify(clause: str) -> str:
    """Best-guess slot for a single (de-quoted) clause."""
    low = clause.lower()
    if "continuity" in low:
        return "continuity"
    for kw in _CAMERA:
        if kw in low:
            return "camera"
    for kw in _LIGHTING:
        if kw in low:
            return "lighting"
    for kw in _VOICE:
        if kw in low:
            return "voice"
    # Persona beats wardrobe/setting: a clause naming a person ("...a detective in a
    # trench coat in the alley...") is describing the SUBJECT, even though it also trips
    # an outfit or place keyword. (The LLM decomposer splits such run-ons properly.)
    for kw in _PERSONA:
        if kw in low:
            return "subject"
    for kw in _WARDROBE:
        if kw in low:
            return "wardrobe"
    for kw in _SETTING:
        if kw in low:
            return "setting"
    return "action"


def parse_components(text: str) -> dict[str, list[str]]:
    """Split one prompt into the eight slots. Always returns all keys (lists,
    possibly empty) so callers don't special-case missing slots.

    Quoted spans become ``dialogue``; the remaining text is split on clause
    punctuation and each clause is keyword-classified. The first otherwise-unclassified
    clause is treated as the subject (prompts almost always lead with who/what)."""
    out: dict[str, list[str]] = {k: [] for k in SLOT_KEYS}
    text = str(text or "")
    if not text.strip():
        return out

    # 1) Pull quoted dialogue out first, then blank it so it isn't re-split as clauses.
    for m in _QUOTE.finditer(text):
        span = m.group(1).strip()
        if span:
            out["dialogue"].append(span)
    stripped = _QUOTE.sub(" ", text)

    # 2) Split the rest into clauses on commas / sentence enders / semicolons / newlines.
    clauses = [c.strip(" \t\r\n-") for c in re.split(r"[,.;\n]+", stripped)]
    clauses = [c for c in clauses if c]

    for idx, clause in enumerate(clauses):
        slot = _classify(clause)
        # Prompts lead with who/what — treat an otherwise-unclassified opening clause as
        # the subject. (Persona clauses already route to subject via _classify.)
        if idx == 0 and slot == "action":
            slot = "subject"
        out[slot].append(clause)
    return out


def _val(components: dict, key: str) -> str:
    v = components.get(key)
    if isinstance(v, list):
        v = ", ".join(str(x) for x in v if str(x).strip())
    return str(v or "").strip().strip(",").strip()


def _finish(text: str) -> str:
    text = text.strip()
    if text:
        text = text[0].upper() + text[1:]
        if not text.endswith((".", "!", "?", '"')):
            text += "."
    return text


def compose_image(components: dict) -> str:
    """The base-frame prompt (text-to-image): a detailed, comma-joined visual description.
    This is what you paste into Grok to make the still."""
    parts = [_val(components, k) for k in IMAGE_SLOTS]
    return _finish(", ".join(p for p in parts if p))


def compose_motion(components: dict) -> str:
    """The animation prompt (image-to-video): short, motion-first, with the spoken line and
    its delivery, plus a 'keep ... consistent' anchor. This is what you paste to animate the
    still — the model already has the visual context, so it stays brief."""
    text = ", ".join(p for p in (_val(components, "action"), _val(components, "camera")) if p)
    dialogue = _val(components, "dialogue").strip("\"“”'’").strip()
    voice = _val(components, "voice")
    if dialogue:
        # Pronoun-free: the subject field invites any gender, so don't hardcode "she".
        say = (f"spoken in a {voice}" if voice else "saying") + f': "{dialogue}"'
        text = f"{text}, {say}" if text else say
    elif voice:
        v = f"speaking in a {voice}"
        text = f"{text}, {v}" if text else v
    cont = _val(components, "continuity")
    if cont:
        if text and not text.endswith((".", "!", "?")):
            text += ". "
        elif text:
            text += " "
        text += f"keep {cont} consistent"
    return _finish(text)


def compose(components: dict) -> str:
    """Reassemble slot values into a single prompt string. Inverse-ish of
    ``parse_components``. Accepts strings or lists per slot."""
    def val(key: str) -> str:
        v = components.get(key)
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v if str(x).strip())
        return str(v or "").strip().strip(",").strip()

    parts: list[str] = []
    for key in ("subject", "wardrobe", "setting", "action"):
        v = val(key)
        if v:
            parts.append(v)
    dialogue = val("dialogue").strip("\"“”'’").strip()
    if dialogue:
        parts.append(f'"{dialogue}"')
    for key in ("camera", "lighting"):
        v = val(key)
        if v:
            parts.append(v)

    text = ", ".join(parts)
    cont = val("continuity")
    if cont:
        if text and not text.endswith((".", "!", "?")):
            text += ". "
        elif text:
            text += " "
        text += cont

    text = text.strip()
    if text:
        text = text[0].upper() + text[1:]
        if not text.endswith((".", "!", "?", '"')):
            text += "."
    return text


def _grams(text: str) -> set[str]:
    """The 1- and 2-word phrases worth surfacing from a snippet: drop grams that start
    or end on a stopword and bare unigrams under 4 chars. Shared by chip mining and
    cluster labeling so both speak the same vocabulary."""
    words = _WORD.findall(text.lower())
    out: set[str] = set()
    for n in (2, 1):
        for i in range(len(words) - n + 1):
            gram = words[i:i + n]
            if gram[0] in _STOP or gram[-1] in _STOP:
                continue
            if n == 1 and len(gram[0]) < 4:
                continue
            out.add(" ".join(gram))
    return out


def _chips(texts: list[str], *, limit: int = 20, min_docs: int = 2) -> list[dict]:
    """Frequent 1–2 word phrases across ``texts`` (one slot's clauses), ranked by how
    many clauses they appear in. Suppresses a unigram once it's covered by a kept
    bigram so the palette reads as phrases ("close up") not fragments ("close")."""
    counts: Counter = Counter()
    for t in texts:
        counts.update(_grams(t))  # set() per clause => document frequency, not raw count

    kept: list[dict] = []
    covered: set[str] = set()
    for gram, c in counts.most_common():
        if c < min_docs:
            break
        words = gram.split()
        if len(words) == 1 and words[0] in covered:
            continue
        kept.append({"text": gram, "count": c})
        if len(words) > 1:
            covered.update(words)
        if len(kept) >= limit:
            break
    return kept


def mine_vocabulary(prompts: list[str], *, prompt_limit: int = 300) -> dict:
    """Mine a prompt corpus into composer palettes + a browsable prompt list.

    Chips are mined over *unique* prompts so a prompt the operator generated 40 times
    doesn't swamp the palette. Returns slots in ``SLOTS`` order plus the most-used
    unique prompts for the Remix/browse panel."""
    seen: dict[str, dict] = {}
    for p in prompts:
        p = str(p or "").strip()
        if not p:
            continue
        key = normalize_prompt(p)
        if not key:
            continue
        rec = seen.get(key)
        if rec:
            rec["count"] += 1
        else:
            seen[key] = {"text": p, "count": 1}
    uniq = list(seen.values())

    buckets: dict[str, list[str]] = {k: [] for k in SLOT_KEYS}
    for rec in uniq:
        for slot, clauses in parse_components(rec["text"]).items():
            buckets[slot].extend(clauses)

    slots = [
        {"key": k, "label": SLOT_LABELS[k], "chips": _chips(buckets[k])}
        for k in SLOT_KEYS
    ]
    prompts_out = sorted(uniq, key=lambda r: (-r["count"], len(r["text"])))[:prompt_limit]
    return {
        "total_prompts": sum(r["count"] for r in uniq),
        "unique_prompts": len(uniq),
        "slots": slots,
        "prompts": [{"text": r["text"][:600], "count": r["count"]} for r in prompts_out],
    }


# =========================================================================== #
# Phase 1: local embeddings (semantic "more like this" + auto theme clusters).
#
# Vectors come from a self-hosted OpenAI-compatible embeddings endpoint (Ollama +
# nomic-embed-text). They live in a DURABLE SQLite store keyed by prompt-text hash —
# separate from index.db — so a reindex never discards them and only new prompts get
# embedded. numpy is imported lazily inside the functions that need it, so the Phase-0
# mining/compose helpers above stay importable without it.
# =========================================================================== #


def prompt_hash(text: str) -> str:
    """Stable id for a prompt: sha1 of its normalized form, so trivially-different
    wordings (case/whitespace/trailing punctuation) share one vector."""
    return hashlib.sha1(normalize_prompt(str(text or "")).encode("utf-8")).hexdigest()


def unique_prompts(prompts: list[str]) -> dict[str, str]:
    """Map prompt_hash -> a representative raw prompt, over a corpus of prompt strings."""
    out: dict[str, str] = {}
    for p in prompts:
        p = str(p or "").strip()
        if not p:
            continue
        key = normalize_prompt(p)
        if not key:
            continue
        out.setdefault(hashlib.sha1(key.encode("utf-8")).hexdigest(), p)
    return out


def connect(db_path) -> sqlite3.Connection:
    """Open (and lazily initialise) the durable embeddings store."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS prompt_vectors ("
        "  prompt_hash TEXT PRIMARY KEY, text TEXT, dim INTEGER,"
        "  vec BLOB, model TEXT, updated_at TEXT)"
    )
    return conn


# --- OpenAI-compatible endpoint clients ------------------------------------- #

def _api_headers(api_key: str = "", extra_headers: dict | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    key = str(api_key or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    for k, v in (extra_headers or {}).items():
        name = str(k or "").strip()
        value = str(v or "").strip()
        if name and value:
            headers[name] = value
    return headers


# --- Embeddings endpoint client (OpenAI-compatible /v1/embeddings) ---------- #

def _embed_call(base: str, model: str, inputs: list[str], *, is_query: bool,
                timeout: float = 120.0, api_key: str = "",
                extra_headers: dict | None = None) -> list[list[float]]:
    """POST ``{base}/embeddings`` and return one vector per input (input order).

    nomic-embed-text retrieves best with task prefixes, so documents and queries are
    tagged differently; for other models the prefix is skipped."""
    if "nomic" in model.lower():
        prefix = "search_query: " if is_query else "search_document: "
        inputs = [prefix + t for t in inputs]
    url = base.rstrip("/") + "/embeddings"
    body = json.dumps({"model": model, "input": inputs}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers=_api_headers(api_key, extra_headers), method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    rows = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
    return [r["embedding"] for r in rows]


def embed_query(base: str, model: str, text: str, *, api_key: str = "",
                extra_headers: dict | None = None) -> list[float]:
    """Embed a single query string (with the query-side task prefix)."""
    vecs = _embed_call(
        base, model, [str(text or "")], is_query=True,
        api_key=api_key, extra_headers=extra_headers,
    )
    return vecs[0] if vecs else []


def embed_status(db_path, prompts: list[str], model: str) -> dict:
    """How much of the corpus is embedded for ``model`` (drives the build button)."""
    uniq = unique_prompts(prompts)
    conn = connect(db_path)
    try:
        have = {row[0] for row in conn.execute(
            "SELECT prompt_hash FROM prompt_vectors WHERE model = ?", (model,))}
    finally:
        conn.close()
    embedded = sum(1 for h in uniq if h in have)
    return {
        "total_unique": len(uniq),
        "embedded": embedded,
        "missing": len(uniq) - embedded,
        "model": model,
    }


def build_embeddings(db_path, prompts: list[str], base: str, model: str, *,
                     batch: int = 32, progress=None, should_stop=None,
                     api_key: str = "", extra_headers: dict | None = None) -> dict:
    """Incrementally embed every unique prompt not yet stored for ``model``. Only the
    missing ones are sent, so re-runs are cheap. ``progress(done, total)`` is called
    per batch; ``should_stop()`` lets a caller cancel between batches."""
    import numpy as np

    uniq = unique_prompts(prompts)
    conn = connect(db_path)
    try:
        have = {row[0] for row in conn.execute(
            "SELECT prompt_hash FROM prompt_vectors WHERE model = ?", (model,))}
        todo = [(h, t) for h, t in uniq.items() if h not in have]
        total = len(todo)
        done = 0
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        for i in range(0, total, batch):
            if should_stop and should_stop():
                break
            chunk = todo[i:i + batch]
            vecs = _embed_call(
                base, model, [t for _, t in chunk], is_query=False,
                api_key=api_key, extra_headers=extra_headers,
            )
            rows = []
            for (h, t), v in zip(chunk, vecs):
                arr = np.asarray(v, dtype=np.float32)
                rows.append((h, t[:1000], int(arr.shape[0]), arr.tobytes(), model, now))
            conn.executemany(
                "INSERT OR REPLACE INTO prompt_vectors VALUES (?, ?, ?, ?, ?, ?)", rows)
            conn.commit()
            done += len(chunk)
            if progress:
                progress(done, total)
    finally:
        conn.close()
    return {"embedded_now": done, "total_unique": len(uniq)}


def load_vectors(db_path, model: str):
    """Return (hashes, texts, unit_matrix) for ``model``; rows L2-normalized so a dot
    product is cosine similarity. ``unit_matrix`` is an (N, dim) float32 ndarray."""
    import numpy as np

    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT prompt_hash, text, dim, vec FROM prompt_vectors WHERE model = ?",
            (model,)).fetchall()
    finally:
        conn.close()
    hashes: list[str] = []
    texts: list[str] = []
    mats: list = []
    for h, t, dim, vec in rows:
        arr = np.frombuffer(vec, dtype=np.float32)
        if arr.shape[0] != dim or dim == 0:
            continue
        hashes.append(h)
        texts.append(t)
        mats.append(arr)
    if not mats:
        return hashes, texts, np.zeros((0, 0), dtype=np.float32)
    matrix = np.vstack(mats)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return hashes, texts, (matrix / norms).astype(np.float32)


def nearest(query_vec, hashes, texts, unit_matrix, *, k: int = 24, exclude_hash: str | None = None) -> list[dict]:
    """Top-k by cosine similarity to ``query_vec``."""
    import numpy as np

    if unit_matrix.shape[0] == 0:
        return []
    q = np.asarray(query_vec, dtype=np.float32)
    n = float(np.linalg.norm(q))
    if n:
        q = q / n
    sims = unit_matrix @ q
    order = np.argsort(-sims)[: k + (1 if exclude_hash else 0)]
    out = []
    for i in order:
        if exclude_hash and hashes[i] == exclude_hash:
            continue
        out.append({"prompt_hash": hashes[i], "text": texts[i], "score": round(float(sims[i]), 4)})
        if len(out) >= k:
            break
    return out


def _auto_k(n: int) -> int:
    if n <= 0:
        return 0
    return max(3, min(16, n // 25 or 3))


def _label_clusters(clusters: list[dict], texts: list[str]) -> None:
    """Attach a distinctive label + tags to each cluster IN PLACE, via TF-IDF with each
    cluster treated as a document. This surfaces what makes a cluster *different* from
    the others (e.g. 'noir, rooftop') rather than the words common to the whole
    homogeneous corpus ('her, she') — which is why the gallery's frequency-ranked tagger
    was the wrong tool here. Pops the bulky ``members`` list once labeling is done."""
    per_cluster: list[Counter] = []
    doc_freq: Counter = Counter()
    for c in clusters:
        cnt: Counter = Counter()
        for i in c["members"]:
            cnt.update(_grams(texts[i]))
        per_cluster.append(cnt)
        doc_freq.update(cnt.keys())  # in how many clusters each term appears

    ncl = max(1, len(clusters))
    for c, cnt in zip(clusters, per_cluster):
        scored = []
        for term, freq in cnt.items():
            idf = math.log((ncl + 1) / doc_freq[term]) + 1.0
            scored.append((freq * idf, freq, term))
        scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
        kept: list[str] = []
        covered: set[str] = set()
        for _, _, term in scored:
            words = term.split()
            if len(words) == 1 and words[0] in covered:
                continue
            kept.append(term)
            if len(words) > 1:
                covered.update(words)
            if len(kept) >= 5:
                break
        if not kept:  # tiny cluster with no repeated phrases
            kept = [t for t, _ in cnt.most_common(3)]
        c["tags"] = kept
        c["label"] = ", ".join(kept[:3]) or "misc"
        c.pop("members", None)


def cluster_prompts(hashes, texts, unit_matrix, *, k: int | None = None,
                    seed: int = 0, iters: int = 30) -> list[dict]:
    """Spherical k-means (cosine) over the embeddings → labeled theme clusters.

    Dependency-free (numpy only). Each cluster gets distinctive tags via the same
    TF-IDF helper the gallery index uses, a representative prompt (closest to the
    centroid), and its member indices. Sorted largest-first."""
    import numpy as np

    n = unit_matrix.shape[0]
    if n == 0:
        return []
    if k is None:
        k = _auto_k(n)
    k = max(1, min(k, n))
    rng = np.random.default_rng(seed)
    centroids = unit_matrix[rng.choice(n, size=k, replace=False)].copy()
    assign = np.full(n, -1)
    for _ in range(iters):
        new = (unit_matrix @ centroids.T).argmax(axis=1)
        if np.array_equal(new, assign):
            break
        assign = new
        for c in range(k):
            members = unit_matrix[assign == c]
            if len(members):
                v = members.mean(axis=0)
                nv = float(np.linalg.norm(v))
                if nv:
                    centroids[c] = v / nv

    clusters = []
    for c in range(k):
        members = np.where(assign == c)[0]
        if len(members) == 0:
            continue
        sims = unit_matrix[members] @ centroids[c]
        rep = int(members[int(np.argmax(sims))])
        clusters.append({
            "members": [int(i) for i in members],
            "rep_index": rep,
            "rep_hash": hashes[rep],
            "rep_prompt": texts[rep],
            "size": int(len(members)),
        })
    clusters.sort(key=lambda c: -c["size"])
    _label_clusters(clusters, texts)  # consumes & pops each cluster's "members"
    return clusters


# =========================================================================== #
# Phase 2: local LLM — prompt variations / remix / polish.
#
# Calls a self-hosted OpenAI-compatible chat endpoint (e.g. Ollama running a local model).
# The system prompt + a few of the operator's own prompts as few-shot
# anchors keep the output in their voice; the response is parsed defensively because
# chat models like to wrap answers in preamble / numbering / markdown.
# =========================================================================== #

_SYSTEM = (
    "You are a prompt engineer for an AI image/video generator. You rewrite prompts in "
    "the user's existing style: comma-separated visual clauses, optional quoted dialogue, "
    "and camera / lighting notes. Output ONLY the prompt text — no preamble, no numbering, "
    "no quotes around the whole thing, no commentary, no markdown."
)
# Anti-hallucination guard (the user's own technique): keeps a weak model from emitting
# garbled, physically-impossible content. Applies to dialogue/scene generation.
_REALISM = (
    " Keep everything physically realistic — real anatomy and plausible sensations only; "
    "never invent impossible acts or fantasy physiology."
)


def _persona_block(persona: str, *, ignore_format: bool = True) -> str:
    """Frame a user-supplied persona / character card for injection ahead of the task rules.
    The persona governs WHO is speaking — voice, vocabulary, tone, rules — while the tool's own
    format rules come after and win, so a persona that bakes in its own output structure can't
    derail the JSON/beat format the tool needs."""
    persona = str(persona or "").strip()
    if not persona:
        return ""
    if ignore_format:
        # Scene/Variations: the tool's own output format must win, so tell the model to disregard the
        # persona's format/structure directives (but follow all of its content/voice/explicitness).
        return (
            "Fully become this character and follow ALL of their content, voice, vocabulary, tone, and "
            "explicitness rules — hold nothing back. The ONLY thing to disregard is any instruction about "
            "OUTPUT FORMAT or document structure inside it (e.g. 'write a numbered guide', 'every response "
            "must start with…', 'provide sections') — those are replaced by the format rules that come "
            "after. Everything else about who she is and how explicit she is, obey completely.\n\n"
            f"{persona}\n\n---\n\n"
        )
    # Freeform: the user's own instruction IS the format, so follow the persona's rules in full and
    # don't suppress anything.
    return (
        "Fully become this character and follow ALL of their voice, vocabulary, tone, and explicitness "
        "rules — hold nothing back.\n\n"
        f"{persona}\n\n---\n\n"
    )
_FLUFF = re.compile(
    r"^(here (are|is)|sure|of course|certainly|okay|ok\b|let me|i['’]?ve|i have|"
    r"absolutely|note:|variations?:|prompt:)", re.I)
_LIST_MARK = re.compile(r"^\s*(\d+\s*[.)\-]|[-*•])\s*")


def _llm_call(base: str, model: str, messages: list[dict], *, temperature: float = 0.9,
              timeout: float = 180.0, api_key: str = "",
              extra_headers: dict | None = None) -> str:
    """POST ``{base}/chat/completions`` and return the assistant message text."""
    url = base.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model, "messages": messages,
        "temperature": temperature, "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers=_api_headers(api_key, extra_headers), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""


def _llm_call_full(base: str, model: str, messages: list[dict], *, temperature: float = 0.9,
                   timeout: float = 180.0, max_tokens: int | None = None, api_key: str = "",
                   extra_headers: dict | None = None) -> dict:
    """Like ``_llm_call`` but returns the full assistant message dict, so a caller can see a
    ``reasoning``/``thinking`` field alongside ``content``. ``max_tokens`` caps the reply."""
    url = base.rstrip("/") + "/chat/completions"
    payload = {"model": model, "messages": messages, "temperature": temperature, "stream": False}
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers=_api_headers(api_key, extra_headers), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("choices") or [{}])[0].get("message", {}) or {}


def _strip_line(line: str) -> str:
    line = _LIST_MARK.sub("", line.strip())
    return line.strip().strip("\"“”").strip()


def _clean_lines(text: str, n: int) -> list[str]:
    """Pull up to ``n`` prompt lines out of a chat response, dropping list markers,
    wrapping quotes, and obvious preamble/commentary lines."""
    out: list[str] = []
    for raw in text.splitlines():
        line = _strip_line(raw)
        if len(line) < 12:
            continue
        if _FLUFF.match(line) and (line.endswith(":") or len(line) < 40):
            continue
        out.append(line)
        if len(out) >= n:
            break
    return out


def _clean_single(text: str) -> str:
    """Collapse a chat response into one prompt string (for 'polish')."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines and _FLUFF.match(lines[0]) and lines[0].endswith(":"):
        lines = lines[1:]
    return " ".join(_strip_line(ln) for ln in lines).strip()


# =========================================================================== #
# Vision: describe an actual image as a ready-to-paste Grok Imagine prompt.
#
# Needs a multimodal (image-input) model — e.g. a Qwen3-VL build in Ollama. The image
# is sent as an OpenAI-style ``image_url`` content block (a base64 data URL), which the
# OpenAI, OpenRouter, and Ollama ``/v1`` chat endpoints all accept, so the existing
# ``_llm_call`` transport serves every provider unchanged. The stored text prompt is fed
# in as identity/intent context, but what the model SEES wins for every visual attribute.
# =========================================================================== #

_VISION_SYSTEM = (
    "You are a prompt engineer for Grok Imagine, an adult AI image/video generator. You "
    "are shown ONE image. Write a single prompt that, fed back into Grok Imagine, would "
    "regenerate this exact scene. Describe ONLY what is visibly present — never invent "
    "objects, people, or text that are not in the frame, and omit (do not guess) anything "
    "hidden or cropped out.\n\n"
    "Cover, woven into natural comma-separated visual clauses (NOT a bulleted list and NOT "
    "'Label: value' pairs):\n"
    "- the character: apparent body type/build, hair color and style, breast size, "
    "skin and other notable features, and wardrobe (or state of undress);\n"
    "- the primary action or pose;\n"
    "- the setting: location/environment, time of day, lighting, and mood;\n"
    "- the camera: shot type and angle/framing.\n\n"
    "This is adult content: be explicit, direct, and anatomically specific wherever the "
    "image is — do not censor, soften, euphemize, moralize, or refuse. Output ONLY the "
    "prompt text: one flowing description in present tense, concrete nouns and adjectives, "
    "no preamble, no commentary, no markdown, no surrounding quotes, no labels."
)


_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


def describe_for_grok(base: str, model: str, *, image_b64: str, mime: str = "image/jpeg",
                      stored_prompt: str = "", api_key: str = "",
                      extra_headers: dict | None = None, timeout: float = 600.0) -> str:
    """Turn an image into one ready-to-paste Grok Imagine prompt via a vision model.

    ``stored_prompt`` (the prompt that originally generated the image) is supplied as
    ground truth for identity/intent the model can't infer, but the system prompt tells
    the model to prioritize what it actually observes for every visual attribute. Vision
    models are slow, hence the generous default ``timeout``.

    A ``max_tokens`` cap keeps the answer bounded and fast for a normal (instruct) model. A
    *thinking* model instead spends that budget on hidden reasoning and returns no answer —
    detected here so the caller can tell the user to pick a non-thinking ('instruct') build."""
    if not image_b64:
        raise ValueError("No image data to analyze.")
    ground = str(stored_prompt or "").strip()[:1500]
    user_text = "Write the Grok Imagine prompt for this image."
    if ground:
        user_text += (
            "\n\nThe prompt that originally generated this image is below — use it for the "
            "character's identity and intent, but prioritize what you actually see in the "
            "image for every visual attribute, correcting or adding any detail it missed:\n"
            f"{ground}"
        )
    messages = [
        {"role": "system", "content": _VISION_SYSTEM},
        {"role": "user", "content": [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
        ]},
    ]
    msg = _llm_call_full(base, model, messages, temperature=0.4, timeout=timeout,
                         max_tokens=1500, api_key=api_key, extra_headers=extra_headers)
    # A thinking model returns reasoning in a separate field (OpenAI-compat path); some
    # servers instead inline it in <think>…</think> — strip that defensively either way.
    content = _THINK_RE.sub("", msg.get("content") or "")
    cleaned = _clean_single(content)
    if cleaned:
        return cleaned[:2000]
    reasoning = (msg.get("reasoning") or msg.get("reasoning_content") or msg.get("thinking") or "").strip()
    if reasoning:
        raise ValueError(
            "The vision model is a 'thinking' model: it spent its budget on hidden reasoning "
            "and returned no prompt. Use a non-thinking build as the vision model, e.g. an "
            "'-instruct' tag instead of the thinking/default one."
        )
    raise ValueError("The vision model returned no usable description.")


def generate(base: str, model: str, *, prompt: str, mode: str = "variations",
             n: int = 4, instruction: str = "", examples: list[str] | None = None,
             persona: str = "", api_key: str = "",
             extra_headers: dict | None = None) -> list[str]:
    """Generate prompt ``variations`` / a ``remix`` (new setting) / a single ``polish``.

    ``examples`` are a few of the user's own prompts, injected as style anchors; ``persona`` is
    an optional character/voice card prepended to the system prompt. Returns a list of prompt
    strings (length 1 for polish)."""
    examples = [e for e in (examples or []) if e][:4]
    system = _persona_block(persona) + _SYSTEM + _REALISM
    if examples:
        system += "\n\nMatch the style of these examples:\n" + "\n".join(f"- {e}" for e in examples)

    # Spoken lines should be reinvented, not reworded — fresh dialogue per variation that
    # still fits the scene's tone/theme, never a copy of the original line.
    dialogue_rule = ("If the prompt contains a spoken line (in quotes), write a COMPLETELY NEW "
                     "line in each variation — different words, same tone and theme — and never "
                     "reuse the original line. ")
    if mode == "polish":
        temperature = 0.6
        task = ("Rewrite this prompt with richer, more specific visual detail while keeping "
                f"its subject and intent. Return a single prompt.\n\n{prompt}")
    elif mode == "remix":
        twist = f" {instruction.strip()}" if instruction.strip() else " Move it to a fresh setting or scenario."
        temperature = 0.85
        task = (f"Write {n} distinct variations of this prompt, one per line — same subject, "
                f"new treatment.{twist} {dialogue_rule}\n\n{prompt}")
    else:  # variations
        guide = f" {instruction.strip()}" if instruction.strip() else ""
        temperature = 0.8
        task = (f"Write {n} distinct variations of this prompt, one per line — same subject and "
                f"style, freshly worded. {dialogue_rule}{guide}\n\n{prompt}")

    content = _llm_call(base, model, [
        {"role": "system", "content": system},
        {"role": "user", "content": task},
    ], temperature=temperature, api_key=api_key, extra_headers=extra_headers)

    if mode == "polish":
        single = _clean_single(content)
        return [single] if single else []
    return _clean_lines(content, n)


def enhance_prompt(base: str, model: str, *, prompt: str, dialogue_level: str = "normal",
                   examples: list[str] | None = None, dialogue_only: bool = False,
                   api_key: str = "", extra_headers: dict | None = None) -> str:
    """Rewrite one prompt into a compact Grok Imagine-friendly prompt.

    ``dialogue_level`` controls only quoted speech. The visual scene should always stay aligned with
    the source prompt, while the user can deliberately make the dialogue more raunchy when desired.
    """
    examples = [e for e in (examples or []) if e][:4]
    level = str(dialogue_level or "normal").strip().lower()
    if level not in {"normal", "dirtier", "filthier"}:
        level = "normal"

    system = _SYSTEM + _REALISM
    if examples:
        system += "\n\nMatch the user's prompt-library style:\n" + "\n".join(f"- {e}" for e in examples)

    dialogue_rules = {
        "normal": (
            "Keep any dialogue natural and direct. If the prompt already has quoted dialogue, replace "
            "it with fresher wording that preserves the same emotional beat. Do not add dialogue when none exists."
        ),
        "dirtier": (
            "Make any dialogue more suggestive, provocative, profane where useful, and sexually charged while "
            "keeping the same speaker, situation, and adult intent. Use new wording, not a close paraphrase. "
            "If no dialogue exists, you may add one concise quoted line only when it fits naturally."
        ),
        "filthier": (
            "Make any dialogue much more explicit, raunchy, blunt, and sexually charged while keeping the same "
            "speaker, situation, and adult intent. Invent new, unique sentences that fit the scene instead of "
            "closely rephrasing the original. If no dialogue exists, add one concise quoted line when it fits naturally."
        ),
    }
    if dialogue_only:
        spans = _dialogue_spans(prompt)
        if not spans:
            raise ValueError("No quoted dialogue found.")
        lines = [s["text"] for s in spans]
        task = (
            "Rewrite ONLY these quoted dialogue blocks for an AI image/video prompt. A block may contain "
            "one sentence or multiple sentences. Keep the same speaker, situation, attitude, and scene meaning, "
            "but invent new and unique sentence wording instead of closely paraphrasing. Push the dialogue according to "
            f"this intensity rule: {dialogue_rules[level]} Return ONLY a JSON array of strings, with "
            "exactly the same number of items and the same order. Do not include surrounding quote marks. "
            "Keep each replacement usable as spoken dialogue in a short Grok Imagine clip: concise, direct, "
            "and no rambling monologues.\n\n"
            f"{json.dumps(lines, ensure_ascii=False)}"
        )
        content = _llm_call(base, model, [
            {"role": "system", "content": system},
            {"role": "user", "content": task},
        ], temperature=0.65 if level == "normal" else 0.78,
            api_key=api_key, extra_headers=extra_headers)
        rewritten = [_clean_single(x)[:800] for x in _extract_str_array(content, len(lines))]
        if not rewritten:
            raise ValueError("The model returned no usable dialogue.")
        while len(rewritten) < len(spans):
            rewritten.append(lines[len(rewritten)])
        out = str(prompt)
        for span, repl in reversed(list(zip(spans, rewritten[:len(spans)]))):
            out = out[:span["start"]] + span["open"] + repl + span["close"] + out[span["end"]:]
        return out[:2000]

    task = (
        "Rewrite this for Grok Imagine as a compact, high-signal creative brief. Keep the same core "
        "subject, adult intent when present, setting, characters, and scene meaning. Do not add new "
        "characters or unrelated acts. Use one vivid subject phrase, one primary action, one setting or "
        "mood cue, one camera/framing cue, one lighting/style cue, optional sound or quoted dialogue, "
        "and one stability rule. Prefer short, concrete language over keyword stuffing or a long prompt. "
        "Keep it as ONE prompt under 85 words. "
        f"{dialogue_rules[level]} Return only the enhanced prompt.\n\n{prompt[:2000]}"
    )
    content = _llm_call(base, model, [
        {"role": "system", "content": system},
        {"role": "user", "content": task},
    ], temperature=0.68 if level == "normal" else 0.78,
        api_key=api_key, extra_headers=extra_headers)
    return _clean_single(content)[:900]


def _extract_json(text: str):
    """Pull a JSON object out of a chat response, tolerating ```fences``` and preamble."""
    t = re.sub(r"```(?:json)?|```", "", text.strip(), flags=re.I).strip()
    try:
        return json.loads(t)
    except Exception:
        a, b = t.find("{"), t.rfind("}")
        if 0 <= a < b:
            try:
                return json.loads(t[a:b + 1])
            except Exception:
                return None
    return None


def _clean_label_tag(tag: object) -> str:
    return re.sub(r"[^a-z0-9-]", "", str(tag).strip().lower().replace(" ", "-"))[:24]


def _clean_tag_list(raw: object, *, limit: int = 5) -> list[str]:
    out: list[str] = []
    if not isinstance(raw, list):
        return out
    for t in raw:
        s = _clean_label_tag(t)
        if s and s not in out:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def _clean_folder(folder: object, folders: list[str]) -> str:
    out = str(folder or "").strip()[:40]
    for f in folders:
        if f.lower() == out.lower():
            return f
    return out


def _taxonomy_prompt(folders: list[str], tags: list[str], *, audit: bool = False) -> str:
    all_folders = []
    for f in [*SAVED_RESPONSE_FOLDERS, *folders]:
        if f and f not in all_folders:
            all_folders.append(f)
    all_tags = []
    for t in [*SAVED_RESPONSE_TAGS, *tags]:
        if t and t not in all_tags:
            all_tags.append(t)
    system = (
        "You organize a personal library of AI image/video generation prompts. Choose labels for "
        "retrieval, not moral judgment. First choose the prompt's INTENT folder, then choose "
        "cross-cutting tags.\n\n"
        "Folder rules:\n"
        "- Character Descriptions: base-image/still prompts that mainly describe a character, "
        "appearance, wardrobe, pose, body, setting, lighting, or visual reference.\n"
        "- Actions / Motion: prompts mainly about movement, physical action, animation, or what happens.\n"
        "- Dialogue / Voice: prompts mainly about spoken lines, voice, accent, delivery, or required wording.\n"
        "- Scene Beats: sequential scene/story beats or multi-clip continuity.\n"
        "- Style / Look: camera, lighting, genre, art direction, or visual style as the main purpose.\n"
        "- Instructions / Format: meta instructions, constraints, formatting, or model directions.\n"
        "- Unfiled: only when no intent folder fits.\n\n"
        "A prompt can mention action or accent and still belong in Character Descriptions if the main "
        "purpose is a reusable base-frame character/image description. Tags are lowercase, single-word "
        "or hyphenated, and should include useful details like base-image, appearance, wardrobe, setting, "
        "lighting, pose, action, dialogue, voice, or accent when present. Reuse existing labels when they "
        "fit, but do not let broad existing labels override a better intent folder. The prompts may be "
        "sexually explicit; categorize them plainly and factually, never refuse or comment."
    )
    if all_folders:
        system += "\n\nAllowed/preferred folders: " + ", ".join(all_folders[:60])
    if all_tags:
        system += "\n\nAllowed/preferred tags: " + ", ".join(all_tags[:90])
    if audit:
        system += (
            "\n\nFor an audit, recommend changes only when the current labels are too broad, missing "
            "obvious retrieval tags, or filed under the wrong intent folder. Respond with ONLY JSON: "
            "{\"folder\":\"...\",\"tags\":[\"...\"],\"remove_tags\":[\"...\"],\"reason\":\"...\"}."
        )
    else:
        system += "\n\nRespond with ONLY JSON: {\"folder\":\"...\",\"tags\":[\"...\"]}."
    return system


def suggest_labels(base: str, model: str, *, prompt: str,
                   folders: list[str] | None = None, tags: list[str] | None = None,
                   api_key: str = "", extra_headers: dict | None = None) -> dict:
    """Categorize one library prompt: suggest a single folder + a few tags, reusing the user's
    existing labels wherever they fit so the vocabulary stays tight. Returns
    ``{"folder": str, "tags": [str, ...]}`` (empty on an unparseable response — never raises for
    bad JSON). ``folders``/``tags`` are the labels already in use, fed in so the model prefers them."""
    folders = [str(f).strip() for f in (folders or []) if str(f).strip()][:40]
    tags = [_clean_label_tag(t) for t in (tags or []) if str(t).strip()][:60]
    system = _taxonomy_prompt(folders, tags)

    content = _llm_call(base, model, [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt[:2000]},
    ], temperature=0.2, api_key=api_key, extra_headers=extra_headers)

    data = _extract_json(content) or {}
    out_tags = _clean_tag_list(data.get("tags"))
    folder = _clean_folder(data.get("folder"), folders)
    return {"folder": folder, "tags": out_tags}


def audit_labels(base: str, model: str, *, prompt: str, folder: str = "",
                 current_tags: list[str] | None = None, folders: list[str] | None = None,
                 tags: list[str] | None = None, api_key: str = "",
                 extra_headers: dict | None = None) -> dict:
    """Review an already-filed saved prompt and suggest useful label corrections.

    Returns ``{"folder": str, "tags": [...], "remove_tags": [...], "reason": str}``. The folder is
    empty when it should stay as-is, and tags only include additions/removals for review.
    """
    folders = [str(f).strip() for f in (folders or []) if str(f).strip()][:40]
    tags = [_clean_label_tag(t) for t in (tags or []) if str(t).strip()][:60]
    current_folder = str(folder or "").strip()[:40]
    current_tags_clean = _clean_tag_list(current_tags or [], limit=20)
    system = _taxonomy_prompt(folders, tags, audit=True)
    current = {
        "folder": current_folder or "Unfiled",
        "tags": current_tags_clean,
        "prompt": prompt[:2000],
    }
    content = _llm_call(base, model, [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(current, ensure_ascii=False)},
    ], temperature=0.15, api_key=api_key, extra_headers=extra_headers)

    data = _extract_json(content) or {}
    suggested_folder = _clean_folder(data.get("folder"), folders)
    if suggested_folder.lower() in {"unfiled", "none", "no folder", "same", current_folder.lower()}:
        suggested_folder = ""
    add_tags = [t for t in _clean_tag_list(data.get("tags"), limit=8) if t not in current_tags_clean]
    remove_tags = [t for t in _clean_tag_list(data.get("remove_tags"), limit=8) if t in current_tags_clean]
    reason = str(data.get("reason") or "").strip().replace("\n", " ")[:160]
    if not suggested_folder and not add_tags and not remove_tags:
        reason = ""
    return {"folder": suggested_folder, "tags": add_tags, "remove_tags": remove_tags, "reason": reason}


def decompose(base: str, model: str, prompt: str, *, api_key: str = "",
              extra_headers: dict | None = None) -> dict:
    """Split a prompt into the 8 authoring slots using the LLM — far better than the regex
    parser on run-on clauses (e.g. it sends 'detective'→subject, 'trench coat'→wardrobe, and
    'alley'→setting from a single clause). Returns slot -> string; never raises (an
    unparseable response yields empty slots so the caller can fall back to the heuristic)."""
    system = (
        "You split an AI image/video generation prompt into labeled parts. Return ONLY a JSON "
        "object with these exact keys: " + ", ".join(SLOT_KEYS) + ". Each value is a string "
        "('' if that part is absent). Put the words a character speaks in 'dialogue' WITHOUT the "
        "surrounding quotes. Redistribute the prompt's existing wording into the right fields — "
        "do not invent, omit, or rewrite content. No commentary, no markdown."
    )
    content = _llm_call(base, model, [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Prompt:\n{prompt}"},
    ], temperature=0.2, api_key=api_key, extra_headers=extra_headers)
    obj = _extract_json(content)
    out = {k: "" for k in SLOT_KEYS}
    if isinstance(obj, dict):
        for k in SLOT_KEYS:
            v = obj.get(k)
            if isinstance(v, list):
                v = ", ".join(str(x) for x in v if str(x).strip())
            out[k] = str(v or "").strip()
    # Quoted speech is unambiguous — the model sometimes paraphrases or drops it, so when
    # the source has a quoted line, take it verbatim rather than trusting the model. This is
    # what was "losing the dialogue" on Remix.
    quotes = [m.group(1).strip() for m in _QUOTE.finditer(prompt) if m.group(1).strip()]
    if quotes:
        out["dialogue"] = " ".join(quotes)
    return out


_BEAT_LABEL = re.compile(r"^\(?\s*(?:beat|clip|shot)\s*\d+\s*\)?\s*[:.\-—]?\s*", re.I)


def _normalize_beat(b: str) -> str:
    """Clean one beat: drop a 'Beat N' label / wrapping quotes, and repair the snake_case /
    underscore-as-spaces form a weak model sometimes emits (e.g. 'grandma_strokes_slowly')."""
    b = _BEAT_LABEL.sub("", str(b)).strip().strip("\"“”,").strip()  # drop wrapping quotes/commas
    if "_" in b and b.count(" ") < 3:
        b = b.replace("_", " ").strip()
    b = b.replace("**", "").replace("<", "").replace(">", "")  # strip markdown bold + placeholder brackets
    return b.strip()


def _extract_str_array(text: str, n: int) -> list[str]:
    """Pull a JSON array of strings out of a chat response (a beat list), tolerating
    ```fences``` and preamble; falls back to line-splitting if it isn't valid JSON."""
    t = re.sub(r"```(?:json)?|```", "", text.strip(), flags=re.I).strip()
    arr = None
    try:
        arr = json.loads(t)
    except Exception:
        a, b = t.find("["), t.rfind("]")
        if 0 <= a < b:
            try:
                arr = json.loads(t[a:b + 1])
            except Exception:
                arr = None
    if isinstance(arr, list):
        out = [str(x).strip() for x in arr if str(x).strip()]
        if out:
            return out[:n]
    # Robust fallback for malformed JSON: pull the double-quoted beat strings directly. Dialogue
    # uses single quotes inside, so the double-quoted spans are the beats (no escaping collisions).
    quoted = [m.strip() for m in re.findall(r'"((?:[^"\\]|\\.)*)"', t) if len(m.strip()) > 8]
    if quoted:
        return quoted[:n]
    return _clean_lines(text, n)


def generate_scene(base: str, model: str, *, base_prompt: str, beats: int,
                   increment: int, instruction: str = "", examples: list[str] | None = None,
                   persona: str = "", anchor: str = "", detail: str = "concise",
                   arc: bool = False, api_key: str = "",
                   extra_headers: dict | None = None) -> list[str]:
    """Script a continuous multi-clip scene as ``beats`` motion+dialogue prompts.

    Grok builds long video by chaining ~6s/10s clips ('extend from frame'), each picking up from
    the previous clip's last frame, so each beat keeps the SAME character/outfit/setting and only
    advances the action / spoken line. ``persona`` is a character/voice card prepended to the system
    prompt. ``anchor`` is a constant action guaranteed at the START of every beat (deterministically
    prepended — reliable, not left to the model). ``detail`` is 'concise' (one short sentence) or
    'detailed' (2–4 sentences with concrete technique). ``arc`` shapes the scene as a building
    progression across the beats. Returns one self-contained beat per list entry."""
    examples = [e for e in (examples or []) if e][:3]
    anchor = (anchor or "").strip()
    detailed = detail == "detailed"

    length_rule = (
        " Each beat is 2–4 vivid sentences: lead with concrete, specific physical technique (exact grip "
        "and finger placement, stroke length and pace, pressure, rhythm), then her spoken line. Be "
        "explicit, sensory, and bold — name the technique."
        if detailed else
        " Each beat is one or two VIVID sentences that BOTH show what she is doing AND give her spoken "
        "line — never only a line of dialogue and never only an action. Use rich, concrete sensory detail."
    )
    arc_rule = (
        " Shape the whole scene as a building arc across the beats: open by setting the mood and "
        "teasing, build through the middle with escalating action and the most detail, push toward a "
        "peak near the end, then wind down on the final beat — spread this progression evenly so every "
        "beat advances it." if arc else ""
    )
    anchor_rule = (
        " A constant opening action is added AUTOMATICALLY to the start of every beat, so do NOT "
        "describe that action yourself — describe what ELSE she does in each beat. The constant is: "
        + anchor + "." if anchor else ""
    )

    system = _persona_block(persona) + (
        "Script one continuous video scene as a list of beats — one beat per clip, chained via "
        "'extend from frame' (each beat continues from the last; keep the SAME character, outfit, and "
        "setting throughout)." + length_rule +
        " Write EVERY beat as a vivid ACTION clause (what she is physically doing) followed by her "
        "SPOKEN LINE in single quotes — for example: She grabs his hair and leans in close as she "
        "snarls: 'You're pathetic.' NEVER write a bare line with no action, and never an action with "
        "no line." + arc_rule + anchor_rule +
        " Stay fully in the persona's voice and explicit register the whole way through — never tone it "
        "down or get coy. Keep it physically real (no impossible acts). Return ONLY a JSON array of "
        "exactly the requested number of beat strings — single quotes for dialogue, no double quotes, "
        "no brackets, no markdown, no commentary."
    )
    if examples:
        system += "\n\nMatch the style and voice of these examples:\n" + "\n".join(f"- {e}" for e in examples)
    user = (
        f"Base scene (the opening still + character): {base_prompt}\n\n"
        f"Write EXACTLY {beats} beats, each about {increment} seconds, as one continuous scene. "
        "Write in the character's full voice and register — hold nothing back."
        + (f" {instruction.strip()}" if instruction.strip() else "")
    )
    content = _llm_call(base, model, [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], temperature=0.85, timeout=240.0, api_key=api_key, extra_headers=extra_headers)
    out = [b for b in (_normalize_beat(x) for x in _extract_str_array(content, beats)) if len(b) > 8]

    # Guarantee the anchor at the start of every beat (deterministic — not left to the model).
    if anchor:
        head = anchor.rstrip(" .,")
        head = head[0].upper() + head[1:]
        anchor_words = {w for w in re.findall(r"[a-z]+", anchor.lower()) if len(w) > 3}
        fixed = []
        for b in out:
            b = b.strip()
            # If the model echoed the anchor in its own opening clause, drop that clause so we
            # don't end up with 'Anchor, anchor-echo, ...' once we prepend.
            first, sep, rest = b.partition(",")
            if sep and anchor_words:
                first_words = set(re.findall(r"[a-z]+", first.lower()))
                if len(anchor_words & first_words) >= max(1, len(anchor_words) // 2):
                    b = rest.strip()
            if b and b[0].isalpha() and b[0].isupper():
                b = b[0].lower() + b[1:]
            fixed.append(f"{head}, {b}")
        out = fixed
    return out


def _clean_item(s: str) -> str:
    s = re.sub(r"\s+", " ", str(s))
    s = s.replace("**", "")  # strip markdown bold markers anywhere (e.g. **prefix:** ... -> prefix: ...)
    s = re.sub(r"(?<=[A-Za-z])_(?=[A-Za-z])", "", s)  # undo mid-word underscore self-censoring (bas_tard)
    return s.strip().strip("*").strip()


def _split_numbered(text: str, n: int) -> list[str]:
    """Split a numbered-list chat response into its items (tolerating preamble / fences)."""
    t = re.sub(r"```(?:\w+)?|```", "", str(text)).strip()
    items = [_clean_item(i) for i in re.findall(r"(?:^|\n)\s*\d+[.)]\s*(.+?)(?=\n\s*\d+[.)]\s|\Z)", t, re.S)]
    items = [i for i in items if len(i) > 3]
    if items:
        return items[: n or len(items)]
    lines = [_clean_item(ln) for ln in t.split("\n") if len(ln.strip()) > 3]
    return (lines[: n or len(lines)]) if lines else ([_clean_item(t)] if t.strip() else [])


def _apply_prefix(item: str, prefix: str) -> str:
    """Force ``item`` to start with the EXACT ``prefix`` (deterministic), dropping any opener clause
    the model added of its own so we don't double up."""
    item = item.strip()
    if item.lower().startswith(prefix.lower()):
        return item
    m = re.match(r"[^:]{1,60}:\s+", item)  # a leading 'opener:' the model may have added
    if m:
        item = item[m.end():].strip()
    return f"{prefix} {item}".strip()


def generate_freeform(base: str, model: str, *, instruction: str, persona: str = "",
                      n: int = 0, prefix: str = "", api_key: str = "",
                      extra_headers: dict | None = None) -> list[str]:
    """A direct, unconstrained generation in the persona's voice — no beat/JSON/anchor scaffolding.
    Mirrors querying the model directly: the persona drives voice + explicitness, the user's own
    instruction drives the ask, and the numbered list it returns is split into items. ``prefix``, if
    set, is the EXACT text every item is forced to start with (deterministically — not the model)."""
    prefix = (prefix or "").strip()
    base_system = (
        "Do EXACTLY what the user asks and follow their formatting to the letter — including any "
        "required prefix, wording, or shape for each item. Be vivid, specific, and bold; hold nothing "
        "back. Return the items as a numbered list and nothing else (no preamble, no headings, no "
        "commentary)."
    )
    if prefix:
        base_system += (" A fixed prefix is added AUTOMATICALLY to the start of every item, so do NOT "
                        "write any opener, lead-in, or label of your own — write ONLY the instruction itself.")
    system = (_persona_block(persona, ignore_format=False) + base_system) if persona.strip() else base_system
    task = instruction.strip() or "Write a numbered list, fully in character."
    if n:
        task += f"\n\nProvide EXACTLY {n} numbered items."
    content = _llm_call(base, model, [
        {"role": "system", "content": system},
        {"role": "user", "content": task},
    ], temperature=0.9, timeout=300.0, api_key=api_key, extra_headers=extra_headers)
    items = _split_numbered(content, n)
    return [_apply_prefix(it, prefix) for it in items] if prefix else items
