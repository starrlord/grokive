"""Shared, dependency-light helpers for prompt grouping and tag extraction.

Extracted from the old gallery builders so the SQLite index (db.py) and any
other tooling can reuse them without importing the large legacy HTML template.
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple


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


# --- Tags -------------------------------------------------------------------------------
# A media tag is a PHRASE lifted from its prompt, never a keyword fragment:
#   * spoken lines — quoted ("…"), or unquoted after a speech cue (`She says: …`,
#     `Sound lip-synced: …`) up to the sentence end;
#   * reported speech — a clause on a speech verb that says WHAT is said ("they tell the
#     ticket agent what they'll pay that the others won't");
#   * talk as a noun — "polite small talk";
#   * camera / capture style — a curated vocabulary, one canonical label per concept
#     ("iPhone selfie", "DSLR", "DoP camera work", "multi-angle"), skipping negated uses;
#   * recurring action & descriptor phrases mined across the whole library ("hanging out
#     with friends", "hanging out with friends and family", "brown leather sofa"), minus
#     production directives (camera, framing, fidelity, layout, audio).
# The word lists here are deliberately neutral: a library adds its own subject-matter
# vocabulary through `tag_words.json` in its data dir (TagWords / load_tag_words), which is
# kept out of the repo. This replaced a TF-IDF unigram/bigram ranker whose output was mostly
# fragments and slices of @mention ids: ~90k distinct tags on a 19k-prompt library.

TAG_MAX_LEN = 160
MAX_SPOKEN_TAGS = 6
MAX_SPEECH_TAGS = 4
MAX_STYLE_TAGS = 8
MAX_MINED_TAGS = 6
MAX_PHRASE_TAGS = 4
PHRASE_MIN_PROMPTS = 3  # a whole short clause is a tag once this many distinct prompts use it
TAG_WORDS_FILE = "tag_words.json"  # a library's word-list additions, in its data dir


@dataclass(frozen=True)
class TagWords:
    """Library-specific additions to the tagger's word lists. Each one ADDS to a neutral
    built-in list, and the defaults add nothing:

    * talk_adjectives — words that make "<adjective> talk" a talk-noun tag;
    * speech_content  — words that, after a speech verb, show what is being said;
    * delivery_words  — how something is said, trimmed off the end of a speech tag;
    * hanging_words   — modifiers a mined phrase may not end on;
    * blocked_words   — words that disqualify a mined phrase;
    * split_compounds — joined spellings split before mining ("hangout" -> "hang out").
    """

    talk_adjectives: frozenset[str] = frozenset()
    speech_content: frozenset[str] = frozenset()
    delivery_words: frozenset[str] = frozenset()
    hanging_words: frozenset[str] = frozenset()
    blocked_words: frozenset[str] = frozenset()
    split_compounds: tuple[tuple[str, str], ...] = ()


def load_tag_words(path: str | Path) -> TagWords:
    """TagWords from a JSON object of word lists, `split_compounds` as {"joined": "split"}.
    A missing or unreadable file, an unknown key or a malformed entry just means no additions:
    tagging still runs on the built-ins, only less precisely."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return TagWords()
    if not isinstance(data, dict):
        return TagWords()

    def words(key: str) -> frozenset[str]:
        raw = data.get(key)
        if not isinstance(raw, list):
            return frozenset()
        return frozenset(w for w in (str(x).strip().lower() for x in raw) if w)

    raw_compounds = data.get("split_compounds")
    compounds = tuple(sorted(
        (str(k).strip().lower(), str(v).strip().lower())
        for k, v in (raw_compounds.items() if isinstance(raw_compounds, dict) else ())
        if str(k).strip() and str(v).strip()
    ))
    return TagWords(words("talk_adjectives"), words("speech_content"), words("delivery_words"),
                    words("hanging_words"), words("blocked_words"), compounds)


_MARK = chr(31)  # stands in for a lifted spoken line, so no clause ever spans across one
_UUID = r"@?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# A run of @mention ids ("@a and @b", "@a,@b", "…23ffand @b" — the typed-together "and").
_MENTIONS_RE = re.compile(rf"{_UUID}(?:\s*(?:,|&|and|or)?\s*{_UUID})*(?:and|or)?", re.I)
_QUOTE_RE = re.compile(r'"([^"\n]+)"')
_SPEECH_CUE_RE = re.compile(
    r"\b(?:say|says|said|saying|whisper\w*|speak\w*|tell|tells|ask\w*|dialog\w*|lip[- ]?sync\w*|"
    r"mouth(?:s|ing)|lines?|voice|voiceover|narrat\w*|confess\w*|talk\w*|quote|script|monologue)\b",
    re.I,
)
# Hyphen-guarded so "talk-show" isn't speech.
_SPEECH_VERB_RE = re.compile(
    r"(?<![\w-])(?:say|says|said|saying|tell|tells|telling|told|talk|talks|talking|speak|speaks|"
    r"speaking|whisper|whispers|whispering|ask|asks|asking|beg|begs|begging|mock|mocks|mocking|berate|"
    r"berates|berating|brag|brags|bragging|confess|confesses|confessing|describe|describes|describing|"
    r"narrate|narrates|narrating|mouthing|taunt|taunts|taunting|instruct|instructs|instructing)(?![\w-])",
    re.I,
)
_TALK_ADJECTIVES = frozenset("small sweet trash smack friendly idle polite casual shop pep baby".split())
_LEADING_CONJ_RE = re.compile(r"^(?:(?:and|but|then|while|whilst|as|if|so|also|before|after|until|when)\s+)+", re.I)
_MID_CONJ_RE = re.compile(r"\b(?:while|whilst|as|before|after|until|when)\s+", re.I)
_SUBJECT_RE = re.compile(r"\b(?:she|he|they|we|i|you|both)\s+$", re.I)
_WORD_RE = re.compile(r"[a-z][a-z'-]*")
# After a speech verb, one of these shows WHAT is said; without one the clause is only a
# delivery direction ("speaks aloud to camera with clear lip-sync").
_SPEECH_CONTENT = frozenset("""
what how about that things thing ways way where why who everything anything something nothing stories story
secrets secret list lines line words me him them us you
""".split())
_AUDIENCE = frozenset("the a an camera lens viewer viewers audience each other one her his their".split())
_FIRST_SECOND = frozenset("i i'm i'll i've i'd me my mine you you're you'll you've your yours we we're us our".split())
_SPEECH_TRAIL = frozenset("""
with lip-sync lip-synced lipsync lip sync synced voice voices aloud accent twang southern husky to the a an in on
at camera lens such as like including saying says said
""".split())
_PHRASE_STOP = frozenset("""
a an and are as at by for from in into is it of on or the to with without this that these those be being her
his their she he they him them you your me my i we us its our same exact one only very while then also just now
so like each every both all no not than too more most some any other another again even still up down out over
off really
""".split())
# Any of these marks a short phrase as a production directive rather than content —
# "face fully unobstructed", "shallow depth of field", "top row", "strict single take".
_DIRECTIVE = frozenset("""
unobscured unobstructed obscured obscuring covering covered visible visibility clear clearly identity lock locked
strict strictly never disappears disappear vanish vanishes vanishing ref refs reference pores text overlays
overlay watermark row rows panel panels separation spacing view views facing profile depth field dof lighting
cinematic realistic realism real push-ins push-in pull-back switches multi-angle angle angles shot shots framing
framed anatomy anatomical silhouettes silhouette a-pose t-pose stronger precise accurate accurately consistent
consistency uniform exact exactly always only quality filmic lip-sync lip-synced lipsync sync synced sound audio
dialogue dialog voice voiceover fingers returns frame frames camera dop lens fps resolution aspect ratio extra
limbs duplicate hands hand arms arm keyframe still image video clip animation animate animated render rendering
style detailed detail details sharp focus bokeh dolly orbit tracking pan tilt zoom slider gimbal handheld static
crane vertical horizontal mcu ms ws cu ecu close-up closeup shallow cinematography pov composition background
foreground centered center cropped crop scale proportions proportion match matching seamless seamlessly
transition transitions loop looping continuity portrait backlit backlight volumetric studio version silent
throughout texture seconds second
""".split())
_HANGING = frozenset("""
lit deep thick long dark bright soft firm heavy multiple full tight high low slow fast quick big small large huge
light warm cool smoky husky glossy shiny sheer white black tan brown orange red pink blue green purple gold silver
grey gray natural thin dense wet messy cozy modern early late old young new classic standard single double
perfect fresh hot sweet slight subtle gentle strong wide intense continuous constant steady short curly straight
wavy loose tiny little extra plump round flat toned visible open parted half partially slightly knowing relaxed
immediate larger bigger smaller tied colored confident playful wooden neutral mid lower upper
""".split())
_MINED_BLOCK = frozenset("""
definition scene start cycles cycle right left forbidden significantly identical but maximum minimum finale rack
medium uv same exact entire unchanged width distinct degrees rim close once twice remains
""".split())


class _Lexicon(NamedTuple):
    talk_noun_re: re.Pattern
    speech_content: frozenset
    speech_trail: frozenset
    hanging: frozenset
    blocked: frozenset
    compounds_re: re.Pattern | None
    compounds: dict


@functools.lru_cache(maxsize=8)
def _lexicon(words: TagWords) -> _Lexicon:
    """The built-in word lists merged with a library's additions, compiled once per TagWords."""
    adjectives = "|".join(map(re.escape, sorted(_TALK_ADJECTIVES | words.talk_adjectives, key=len, reverse=True)))
    compounds = dict(words.split_compounds)
    joined = "|".join(map(re.escape, sorted(compounds, key=len, reverse=True)))
    return _Lexicon(
        talk_noun_re=re.compile(
            rf"(?<![\w-])((?:(?:{adjectives})\s+){{1,3}}(?:talk|talking|dialogue|dialog))(?![\w-])", re.I),
        speech_content=_SPEECH_CONTENT | words.speech_content,
        speech_trail=_SPEECH_TRAIL | words.delivery_words,
        hanging=_HANGING | words.hanging_words,
        blocked=_MINED_BLOCK | words.blocked_words,
        compounds_re=re.compile(rf"\b(?:{joined})\b", re.I) if compounds else None,
        compounds=compounds,
    )


_CLAUSE_BREAK_RE = re.compile(r"\n|" + re.escape(_MARK) + r"|[,;:](?=\s|$)|(?<!\.)[.!?](?=\s|$)|[—–]|(?<= )-(?= )")


def _split_clauses(text: str) -> list[str]:
    """Clauses at sentence ends, commas, semicolons, colons, dashes and newlines — never
    inside parentheses ("a list of snacks (chips, fruit, nuts)" stays whole)."""
    if "(" not in text:  # the common case: one regex split, not a character walk
        return [c.strip() for c in _CLAUSE_BREAK_RE.split(text) if c.strip()]
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        nxt = text[i + 1] if i + 1 < len(text) else " "
        prev = text[i - 1] if i else " "
        if ch == _MARK or ch == "\n" or (depth == 0 and (
            (ch in ",;:" and nxt.isspace())
            or (ch in ".!?" and nxt.isspace() and prev != ".")
            or ch in "—–"
            or (ch == "-" and prev == " " and nxt == " ")
        )):
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return [c.strip() for c in out if c.strip()]


_SENTENCE_END_RE = re.compile(r"(?<!\.)[.!?](?!\.)(?:\s|$)|\n|" + re.escape(_MARK) + r"|$")


def _sentence_end(text: str) -> int:
    """Index of the first sentence break in text (an ellipsis doesn't end a sentence)."""
    return _SENTENCE_END_RE.search(text).start()


def _lift_spoken_lines(text: str) -> tuple[list[str], str]:
    """Spoken lines (quoted, or after a speech cue + colon), and the text with each line
    — and each cue — replaced by _MARK so they don't also become clause/phrase tags."""
    lines: list[str] = []

    def quoted(m: re.Match) -> str:
        if len(m.group(1).split()) >= 3:  # not `the "exact" woman` or `a sign reading "OPEN"`
            lines.append(m.group(1))
            return _MARK
        return m.group(0)

    text = _QUOTE_RE.sub(quoted, text)
    out: list[str] = []
    pos = 0
    for m in re.finditer(r":[ \t]+", text):
        if m.start() < pos:
            continue
        start = max(text.rfind(ch, 0, m.start()) for ch in (".", ",", "\n", _MARK)) + 1
        cue = text[start:m.start()]
        if len(cue) > 60 or not _SPEECH_CUE_RE.search(cue) or text[m.end():m.end() + 1] in ('"', _MARK):
            continue
        end = m.end() + _sentence_end(text[m.end():])
        # An unquoted line can run on ("First coffee is in. You smell that."): keep taking
        # sentences while they still talk in first/second person and carry no timings.
        while end < len(text) and text[end] in ".!?":
            nxt = end + 1 + _sentence_end(text[end + 1:])
            sentence = text[end + 1:nxt]
            if (not sentence.strip() or re.search(r"\d", sentence)
                    or not _FIRST_SECOND & set(_WORD_RE.findall(sentence.lower()))):
                break
            end = nxt
        if len(text[m.end():end].split()) >= 3:
            lines.append(text[m.end():end])
            out += [text[pos:start], _MARK]
            pos = end
    out.append(text[pos:])
    return lines, "".join(out)


def _speech_clause(clause: str, lex: _Lexicon) -> str | None:
    """The reported-speech tag in one clause, or None. Starts at the speaker named right
    before the verb, else after a subordinating conjunction ("… while saying X" -> "saying
    X"), else after a joining "and"; trailing delivery words are dropped."""
    m = _SPEECH_VERB_RE.search(clause)
    if not m or "→" in clause or " + " in clause:
        return None
    after = _WORD_RE.findall(clause[m.end():].lower())
    if not any(w in lex.speech_content or (w == "to" and i + 1 < len(after) and after[i + 1] not in _AUDIENCE)
               for i, w in enumerate(after)):
        return None
    head = clause[:m.start()]
    subject = _SUBJECT_RE.search(head)
    mids = list(_MID_CONJ_RE.finditer(head))
    if subject and len(head.split()) > 1:
        clause = clause[subject.start():]
    elif mids:
        clause = clause[mids[-1].end():]
    else:
        ands = list(re.finditer(r"\band\s+", head, re.I))
        if ands and not head[ands[-1].end():].strip() and len(head.split()) > 1:
            clause = clause[ands[-1].end():]
    words = _LEADING_CONJ_RE.sub("", clause.strip()).split()
    while words and words[-1].lower().strip(".,;:") in lex.speech_trail:
        words.pop()
    if (not 3 <= len(words) <= 40 or words[0].lower() == "only"
            or sum(w.lower() in _DIRECTIVE for w in words) >= 3):  # a stage direction that mentions a line
        return None
    return " ".join(words)


def _clean_tag(text: str) -> str:
    tag = re.sub(r"\s+", " ", text.replace(_MARK, " ")).strip().lower().strip(" .,;:!?-—–'\"")
    if tag.count("(") > tag.count(")"):
        tag += ")"
    if len(tag) > TAG_MAX_LEN:
        tag = tag[:TAG_MAX_LEN].rsplit(" ", 1)[0].rstrip(" .,;:-") + "…"
    return tag


# Camera / capture style: (label, pattern), specific before generic. Matches resolve the way
# one alternation would: at any position the first listed pattern that matches claims the
# span ("extreme close-up" before "close-up", "35mm film" before "35mm lens"). Abbreviations
# are case-sensitive via (?-i:…): "Ms Davis" isn't a medium shot.
_STYLE_TAGS: tuple[tuple[str, str], ...] = (
    ("iPhone selfie", r"\biphone\b[^.,;\n]{0,25}\bselfies?\b|\bselfies?\b[^.,;\n]{0,25}\biphone\b"),
    ("mirror selfie", r"\bmirror selfies?\b"),
    ("iPhone", r"\biphone\b"),
    ("smartphone photo", r"\bsmart ?phone\b|\bphone camera\b|\bcell ?phone (?:photo|camera|video)\b"),
    ("selfie", r"(?<!source )(?<!reference )(?<!original )(?<!input )\bselfies?\b"),
    ("DSLR", r"\bdslr\b"),
    ("Canon", r"\bcanon\b"),
    ("ARRI Alexa", r"\barri\b|\balexa (?:mini|lf|35|look)\b"),
    ("webcam", r"\bweb ?cam"),
    ("35mm film", r"\b35 ?mm film\b|\bfilm camera\b|\bkodak\b|\bportra\b"),
    ("film grain", r"\bfilm grain\b|\bgrainy\b|\b35 ?mm grain\b"),
    ("24mm lens", r"\b24 ?mm\b"),
    ("35mm lens", r"\b35 ?mm\b"),
    ("50mm lens", r"\b50 ?mm\b"),
    ("85mm lens", r"\b85 ?mm\b"),
    ("POV", r"\bpov\b|\bpoint[- ]of[- ]view\b|\bfirst[- ]person\b"),
    ("candid", r"\bcandid\b"),
    ("amateur", r"\bamateur\b"),
    ("DoP camera work", r"\bdop\b|\bd\.o\.p\b|\bdirector of photography\b|\bcinematographer\b"),
    ("multi-angle", r"\bmulti[- ]?angles?\b|\bmultiple (?:camera )?angles\b|\bmulti[- ]?cam(?:era)?\b|\bangle changes\b"),
    ("handheld", r"\bhand[- ]?held\b"),
    ("gimbal", r"\bgimbal\b"),
    ("steadicam", r"\bsteadicam\b"),
    ("push-in", r"\bpush(?:es|ing)?[- ]?ins?\b|\bdolly[- ]in\b"),
    ("dolly", r"\bdolly\b"),
    ("orbit shot", r"\borbit(?:s|al|ing)?\b|\barc(?:s|ing)? (?:around|shot|move)\b"),
    ("crane shot", r"\bcrane[- ](?:up|down|shot|move|rise|lift)\b"),
    ("tracking shot", r"\btracking(?: shot)?\b|\blateral track\b"),
    ("slider", r"\bslider\b"),
    ("rack focus", r"\brack focus\b"),
    ("slow motion", r"\bslow[- ]?mo(?:tion)?\b"),
    ("Dutch angle", r"\bdutch[- ]?(?:angle|tilt)\b"),
    ("top-down shot", r"\btop[- ]down\b|\bbird'?s[- ]eye\b|\boverhead shot\b"),
    ("over-the-shoulder", r"\bover[- ]the[- ]shoulder\b|\bover-shoulder\b|(?-i:\bOTS\b)"),
    ("low angle", r"\blow[- ]angle\b"),
    ("high angle", r"\bhigh[- ]angle\b"),
    ("three-quarter view", r"\bthree[- ]quarters?\b"),
    ("profile view", r"\bside profile\b|\bprofile (?:view|shot)\b|\bside view\b"),
    ("extreme close-up", r"\bextreme close[- ]?ups?\b|\bmacro (?:shot|close[- ]?up)\b|(?-i:\bECU\b)"),
    ("medium close-up", r"\bmedium close[- ]?ups?\b|(?-i:\bMCU\b)"),
    ("close-up", r"\bclose[- ]?ups?\b|(?-i:\bCU\b)"),
    ("medium shot", r"\bmedium(?:[- ]wide)? shots?\b|\bmid[- ]shot\b|(?-i:\bMS\b)"),
    ("wide shot", r"\bwide shots?\b|\bwide[- ]angle\b|\bestablishing shot\b|(?-i:\bWS\b)"),
    ("full-body shot", r"\bfull[- ]body (?:shot|view|framing)\b"),
    ("shallow depth of field", r"\bshallow (?:depth of field|dof)\b"),
    ("bokeh", r"\bbokeh\b"),
    ("lens flare", r"\blens flares?\b|\banamorphic flares?\b"),
    ("anamorphic", r"\banamorphic\b"),
    ("golden hour", r"\bgolden hour\b"),
    ("natural light", r"\bnatural (?:light|lighting|daylight)\b|\bwindow light\b"),
    ("studio lighting", r"\bstudio (?:light|lighting)\b"),
    ("glamour lighting", r"\bglamou?r lighting\b"),
    ("ring light", r"\bring ?light\b"),
    ("backlit", r"\bback ?lit\b|\bbacklight(?:ing|s)?\b|\brim light\b"),
    ("volumetric light", r"\bvolumetric\b"),
    ("moody lighting", r"\bmoody\b|\blow[- ]key (?:light|lighting)\b"),
    ("documentary style", r"\bdocumentary\b"),
    ("editorial", r"\beditorial\b"),
    ("glamour", r"\bglamou?r\b"),
    ("boudoir", r"\bboudoir\b"),
    ("photoreal", r"\bphoto[- ]?real(?:istic|ism)?\b|\bhyper[- ]?real(?:istic|ism)?\b"),
    ("cinematic", r"\bcinematic\b"),
    ("16:9 widescreen", r"\b16:9\b"),
    ("9:16 vertical", r"\b9:16\b|\bvertical (?:video|format|frame)\b"),
    ("lip-sync", r"\blip[- ]?sync\w*"),
    ("voiceover", r"\bvoice[- ]?over\b"),
    ("montage", r"\bmontage\b|\bjump cuts?\b"),
    ("split screen", r"\bsplit[- ]?screen\b"),
    ("character sheet", r"\bcharacter (?:reference |model )?sheet\b|\breference sheet\b|\bmodel sheet\b|\bturnaround sheet\b"),
)
_STYLE_ORDER = {label: i for i, (label, _) in enumerate(_STYLE_TAGS)}
_LEADING_ASSERTIONS_RE = re.compile(r"^(?:\\b|\(\?<!.*?\)|\(\?-i:)+")


def _top_level_alternatives(pattern: str) -> list[str]:
    """A regex split at its top-level `|` (not inside groups or character classes)."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            buf.append(pattern[i:i + 2])
            i += 2
            continue
        depth += (ch in "([") - (ch in ")]")
        if ch == "|" and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def _required_prefix(alternative: str) -> str:
    """The literal text every match of one regex alternative starts with, lowercased:
    `\\bselfies?\\b` -> "selfie", `\\b35 ?mm\\b` -> "35", `(?-i:\\bMCU\\b)` -> "mcu"."""
    s = _LEADING_ASSERTIONS_RE.sub("", alternative)
    out: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and s[i + 1:i + 2] in (".", ":", "'", "-"):
            out.append(s[i + 1])
            i += 2
        elif ch.isalnum() or ch in " :'-":
            out.append(ch)
            i += 1
        else:
            if ch in "?*{" and out:
                out.pop()  # an optional character isn't required
            break
    return "".join(out).strip().lower()


_STYLE_ENTRIES = tuple(
    (order, label, re.compile(rx, re.I), tuple(_required_prefix(alt) for alt in _top_level_alternatives(rx)))
    for order, (label, rx) in enumerate(_STYLE_TAGS)
)
# A negation counts only RIGHT before the term, at most two harmless modifiers between ("no
# ECU", "NOT dutch angle", "no extreme face close-up"). A looser window negated the wrong term
# in run-on prompts: "NO extras 15s DoP", "never stops multi-angle".
_STYLE_NEGATION_RE = re.compile(
    r"\b(?:no|not|never|without|avoid|avoiding|zero|forbidden|don'?t)\s+"
    r"(?:(?:extreme|harsh|dramatic|tight|any|a|an|the|too|overly|face|show|showing|use|using|visible|obvious)\s+){0,2}$",
    re.I,
)


def _style_matches(text: str):
    """(start, end, label) per camera / style term in text. A term is only run when its
    required literal text appears (one 69-way alternation tried at every character took
    ~20 s over 19k prompts); overlaps then resolve left to right, first-listed term winning."""
    low = text.lower()
    hits = []
    for order, label, rx, prefixes in _STYLE_ENTRIES:
        if any(not p or p in low for p in prefixes):
            hits.extend((m.start(), order, m.end(), label) for m in rx.finditer(text))
    hits.sort()
    claimed = 0
    for start, _, end, label in hits:
        if start >= claimed:
            claimed = end
            yield start, end, label


def _has_style_term(text: str) -> bool:
    return next(_style_matches(text), None) is not None


def _style_tags(text: str) -> tuple[str, ...]:
    """Canonical camera / capture style labels used (not negated) in text, in vocabulary order."""
    found = {label for start, _, label in _style_matches(text)
             if not _STYLE_NEGATION_RE.search(text, max(0, start - 48), start)}
    return tuple(sorted(found, key=_STYLE_ORDER.__getitem__)[:MAX_STYLE_TAGS])


@functools.lru_cache(maxsize=65536)
def _prompt_parts(prompt: str, words: TagWords) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], str]:
    """(always-tags: spoken lines, speech clauses, talk nouns; candidate short phrases; camera
    style labels; the prose left once spoken lines are lifted — what phrase mining reads).
    Cached: the server rebuilds the index often, and a prompt's parts never change."""
    lex = _lexicon(words)
    text = prompt
    for curly, straight in (("“", '"'), ("”", '"'), ("„", '"'), ("″", '"'),
                            ("‘", "'"), ("’", "'")):
        text = text.replace(curly, straight)
    text = re.sub(r"(\w);(ll|re|ve|d|s|t|m)\b", r"\1'\2", text)  # "they;ll" -> "they'll"
    text = _MENTIONS_RE.sub(" ", text)
    spoken, rest = _lift_spoken_lines(text)
    speech: list[str] = []
    nouns: list[str] = []
    phrases: list[str] = []
    for clause in _split_clauses(rest):
        nouns += [m.group(1) for m in lex.talk_noun_re.finditer(clause)]
        said = _speech_clause(lex.talk_noun_re.sub(" ", clause), lex)
        if said:
            speech.append(said)
            continue
        body = _LEADING_CONJ_RE.sub("", clause)
        found = _WORD_RE.findall(body.lower())
        if (2 <= len(found) <= 4 and not re.search(r"\d", body)
                and not lex.talk_noun_re.search(body) and not _SPEECH_VERB_RE.search(body)
                and not _has_style_term(body)
                and found[0] not in _PHRASE_STOP and found[-1] not in _PHRASE_STOP
                and not any(w in _DIRECTIVE or w.startswith(("photoreal", "ultra-photo", "hyperreal"))
                            for w in found)):
            phrases.append(" ".join(found))
    always = tuple(spoken[:MAX_SPOKEN_TAGS] + speech[:MAX_SPEECH_TAGS] + nouns)
    return always, tuple(phrases), _style_tags(rest), rest


# --- Mined phrases ---------------------------------------------------------------------
# Word sequences (2–7 words) that recur across the library inside clauses, like "hanging out
# with friends". Word forms count together via a crude stem ("hang out" / "hanging out" are
# one phrase), and a phrase must look like a complete unit: used in varied surroundings, not
# nearly always continued by the same word ("face completely" → "unobscured"), not ending on
# a dangling modifier, and not a directive, speech or camera term (those have their own layers).
MINED_MAX_WORDS = 7
PHRASE_REMINE_SECONDS = 6 * 3600
_EDGES = ("<s>", "</s>")
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'-]*")
_STEM_IRREGULAR = {"made": "mak", "told": "tel", "said": "sai", "took": "tak", "gave": "giv", "held": "hold"}
# A phrasal verb reads as an activity, so its label takes a gerund ("hanging out with friends").
_PARTICLES = frozenset("out up off down over back around with into on in at through away".split())
_MINED_EDGE_STOP = _PHRASE_STOP | frozenset("""
against behind toward towards near beyond across along onto upon under between how what where when why who which
whose around through about after before during until via per plus
""".split())
_HANGING_SUFFIXES = ("-length", "-sized", "-waisted", "-style", "-up", "-level")
# Words that legitimately join two parts of one phrase ("hanging out WITH friends AND family").
_CONNECTORS = frozenset("and or with of in on into onto from to at by for through around over under near".split())


def _min_prompts(n: int) -> int:
    """Distinct prompts a mined phrase of n words must appear in — a longer phrase is more
    specific, so it earns a tag sooner."""
    return 8 if n == 2 else 5 if n == 3 else 4


@functools.lru_cache(maxsize=None)
def _stem(word: str) -> str:
    """A crude grouping key so word forms count together (walk/walks/walking, drag/dragging)
    — never shown to anyone."""
    if word in _STEM_IRREGULAR:
        return _STEM_IRREGULAR[word]
    s = word
    for suffix in ("ing", "ed", "es", "s"):
        if s.endswith(suffix) and len(s) > len(suffix) + 2 and not (suffix == "s" and s.endswith("ss")):
            s = s[: -len(suffix)]
            break
    if len(s) > 3 and s[-1] == s[-2] and s[-1] not in "aeiou":
        s = s[:-1]
    if len(s) > 3 and s.endswith("e"):
        s = s[:-1]
    return s


_STOP_STEMS = frozenset(_stem(w) for w in _PHRASE_STOP)


def _segments(rest: str, lex: _Lexicon) -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]:
    """(surface words, stems) runs a phrase may lie in: the prose's clauses (with a library's
    joined spellings split apart), cut again at any token holding a digit ("1990s", "720p")
    so none glues numbers into words. Hyphenated words are NOT split generally — that turned
    camera terms like "slow push-in" into bogus "slow push" phrases."""
    text = rest
    if lex.compounds_re is not None:
        text = lex.compounds_re.sub(lambda m: lex.compounds[m.group(0).lower()], text)
    runs = []
    for clause in _split_clauses(text):
        run: list[str] = []
        for tok in (*_TOKEN_RE.findall(clause.lower()), "0"):  # "0" flushes the last run
            if any(ch.isdigit() for ch in tok):
                if len(run) > 1:
                    runs.append((tuple(run), tuple(_stem(w) for w in run)))
                run = []
            else:
                run.append(tok)
    return tuple(runs)


def _phrase_label(gram: tuple[str, ...], surface: dict) -> str:
    """The phrase's most common spelling — except a phrasal verb takes a gerund when one is
    used for it or for its shortest prefix ("hang out with friends and family" borrows
    "hanging" from "hanging out with friends"). Nouns never get one invented: "coffee cups" stays."""
    forms = surface[gram]
    best = max((c, f) for f, c in forms.items())[1]
    words = best.split()
    gerunds = [(c, f) for f, c in forms.items() if f.split()[0].endswith("ing")]
    if len(words) < 2 or words[1] not in _PARTICLES or (gerunds and max(gerunds)[1] == best):
        return best
    if gerunds:
        return max(gerunds)[1]
    for k in range(2, len(gram)):
        prefix = surface.get(gram[:k])
        pre_gerunds = [(c, f) for f, c in (prefix or {}).items() if f.split()[0].endswith("ing")]
        if pre_gerunds:
            return " ".join([max(pre_gerunds)[1].split()[0]] + words[1:])
    return best


def _is_adverb(word: str, unigram_df: Counter) -> bool:
    """An -ly word whose base is itself used in the library ("slowly" <- "slow", "gently" <-
    "gentle", "happily" <- "happy"). A bare "-ly" test also threw out nouns: "family", "belly"."""
    if not word.endswith("ly") or len(word) < 5:
        return False
    base = word[:-2]
    return any(unigram_df.get(_stem(b)) for b in (base, base + "le", base + "l", base[:-1] + "y"))


def _keep_phrase(gram, label, df, unigram_df, occurrences, left, right, lex: _Lexicon) -> bool:
    words = label.split()
    if any(a == b for a, b in zip(gram, gram[1:])):  # "walking walk"
        return False
    # Keyword salad: two phrases run together with nothing joining them ("red scarf brown
    # boots") appear together far less often than apart. Judged against the geometric mean of
    # the halves' counts — the rarer half alone lets salad through when that half is itself
    # salad ("long hair red scarf" + "brown boots").
    need = 0.02 if len(gram) == 2 else 0.04
    for k in range(1, len(gram)):
        if words[k - 1] in _CONNECTORS or words[k] in _CONNECTORS:
            continue
        a, b = (df.get(part, 0) if len(part) > 1 else unigram_df[part[0]] for part in (gram[:k], gram[k:]))
        if df[gram] < need * (a * b) ** 0.5:
            return False
    if (words[0] in _MINED_EDGE_STOP or words[-1] in _MINED_EDGE_STOP or words[-1] in lex.hanging
            or words[-1].endswith(_HANGING_SUFFIXES) or _is_adverb(words[-1], unigram_df)):
        return False
    if any(w in _DIRECTIVE or w in lex.blocked or w.startswith(("photoreal", "ultra-photo", "hyperreal"))
           for w in words):
        return False
    if _SPEECH_VERB_RE.search(label) or lex.talk_noun_re.search(label) or _has_style_term(label):
        return False
    for side in (left[gram], right[gram]):
        # Copy-pasted prompt families repeat a phrase in one fixed spot; a real phrase shows
        # up in at least three different surroundings on each side.
        if len(side) < 3:
            return False
        # A fragment is nearly always continued by the same CONTENT word; a linking word
        # after it ("… with friends and …") is no evidence of that.
        top = max((c for k, c in side.items() if k not in _EDGES and k not in _STOP_STEMS), default=0)
        if top > 0.5 * occurrences[gram]:
            return False
    for w in right[gram]:
        if w not in _EDGES and w not in _STOP_STEMS and df.get(gram + (w,), 0) >= 0.7 * df[gram]:
            return False
    for w in left[gram]:
        if w not in _EDGES and w not in _STOP_STEMS and df.get((w,) + gram, 0) >= 0.7 * df[gram]:
            return False
    return True


def _mine_phrases(segmented: list, lex: _Lexicon) -> dict[tuple[str, ...], tuple[str, int]]:
    """{stems: (label, prompts using it)} across a corpus of _segments() results. Apriori:
    an n-word candidate is only counted when both its (n-1)-word halves are frequent."""
    floor = min(_min_prompts(n) for n in range(2, MINED_MAX_WORDS + 1))
    unigram_df: Counter = Counter()
    for runs in segmented:
        unigram_df.update({s for _, stems in runs for s in stems})
    df: dict[tuple[str, ...], int] = {}
    occurrences: Counter = Counter()
    left: dict = defaultdict(Counter)
    right: dict = defaultdict(Counter)
    surface: dict = defaultdict(Counter)
    # Per run, alive[i] says the (n-1)-word gram starting at i is frequent: an n-gram is only
    # built where both halves are alive, so dead stretches cost nothing on later passes.
    live = [[(words, stems, [True] * len(stems)) for words, stems in runs] for runs in segmented]
    for n in range(2, MINED_MAX_WORDS + 1):
        counts: Counter = Counter()
        for runs in live:
            seen = set()
            for _, stems, alive in runs:
                for i in range(len(stems) - n + 1):
                    if alive[i] and alive[i + 1]:
                        seen.add(stems[i:i + n])
            counts.update(seen)
        frequent = {g for g, c in counts.items() if c >= floor}
        if not frequent:
            break
        df.update((g, counts[g]) for g in frequent)
        need = _min_prompts(n)
        survivors = []
        for runs in live:
            kept_runs = []
            for words, stems, alive in runs:
                size = len(stems)
                flags = []
                for i in range(size - n + 1):
                    gram = stems[i:i + n] if alive[i] and alive[i + 1] else None
                    hit = gram is not None and gram in frequent
                    flags.append(hit)
                    if hit and df[gram] >= need:  # neighbour stats only for tag candidates
                        occurrences[gram] += 1
                        left[gram][stems[i - 1] if i else "<s>"] += 1
                        right[gram][stems[i + n] if i + n < size else "</s>"] += 1
                        surface[gram][" ".join(words[i:i + n])] += 1
                if any(flags):
                    kept_runs.append((words, stems, flags))
            survivors.append(kept_runs)
        live = survivors
    vocab = {}
    for gram in occurrences:
        label = _phrase_label(gram, surface)
        if _keep_phrase(gram, label, df, unigram_df, occurrences, left, right, lex):
            vocab[gram] = (label, df[gram])
    return vocab


def _match_phrases(runs, vocab) -> tuple[str, ...]:
    """The mined phrases one prompt uses, in reading order."""
    flat: list = []
    found = []
    for _, stems in runs:
        base = len(flat)
        flat.extend(stems)
        flat.append(None)
        for n in range(min(MINED_MAX_WORDS, len(stems)), 1, -1):
            for i in range(len(stems) - n + 1):
                hit = vocab.get(stems[i:i + n])
                if hit:
                    found.append((base + i, n, hit[0], hit[1]))
    chosen: list = []
    for start, n, label, count in sorted(found, key=lambda f: -f[1]):
        # Inside a longer chosen phrase, keep it only as the broader tag it genuinely is: the
        # longer one adds a coordinated part ("hanging out with friends" + "… and family"), or
        # it's far more common on its own — not as nested boilerplate ("holds a mug" inside
        # "holds a mug of hot coffee").
        if all((c_start == start and flat[start + n] in ("and", "or")) or count >= 3 * c_count
               for c_start, c_n, _, c_count in chosen if c_start <= start and start + n <= c_start + c_n):
            chosen.append((start, n, label, count))
    labels: list[str] = []
    for _, _, label, _ in sorted(chosen):
        if label not in labels:
            labels.append(label)
    return tuple(labels[:MAX_MINED_TAGS])


_PHRASES: dict[str, Any] = {"vocab": None, "words": None, "corpus": frozenset(), "at": 0.0, "tags": {}}


def _mined_phrase_tags(rests: list[str], words: TagWords) -> list[tuple[str, ...]]:
    """Mined-phrase tags per prompt prose. The vocabulary is mined once and reused across index
    rebuilds until the corpus drifts (over 2% of prompts new or gone), the word lists change,
    or it's PHRASE_REMINE_SECONDS old: a sync's few new prompts are matched against it rather
    than re-mining the library (~10 s at 19k prompts) on every rebuild."""
    memo = _PHRASES
    lex = _lexicon(words)
    corpus = frozenset(rests)
    drift = len(corpus ^ memo["corpus"]) / max(1, len(corpus | memo["corpus"]))
    if (memo["vocab"] is None or memo["words"] != words or drift > 0.02
            or time.monotonic() - memo["at"] > PHRASE_REMINE_SECONDS):
        segmented = {rest: _segments(rest, lex) for rest in corpus}
        vocab = _mine_phrases(list(segmented.values()), lex)
        memo.update(vocab=vocab, words=words, corpus=corpus, at=time.monotonic(),
                    tags={rest: _match_phrases(runs, vocab) for rest, runs in segmented.items()})
    tags = memo["tags"]
    for rest in rests:
        if rest not in tags:
            tags[rest] = _match_phrases(_segments(rest, lex), memo["vocab"])
    return [tags[rest] for rest in rests]


def tags_for_groups(groups: list[dict[str, Any]], words: TagWords | None = None) -> None:
    """Assign tags to each group (in place) — see the Tags notes above. Each group's prompt is
    one document: a whole short clause becomes a tag once PHRASE_MIN_PROMPTS distinct prompts
    use it, and mined phrases come from a vocabulary built across the whole corpus. `words`
    adds a library's own vocabulary (load_tag_words); None uses only the neutral built-ins."""
    words = words if words is not None else TagWords()
    parts = [_prompt_parts(group.get("prompt") or "", words) for group in groups]
    mined = _mined_phrase_tags([rest for *_, rest in parts], words)
    phrase_df: Counter = Counter()
    for _, phrases, _, _ in parts:
        phrase_df.update(set(phrases))
    for group, (always, phrases, styles, _), found in zip(groups, parts, mined):
        recurring = [p for p in phrases if phrase_df[p] >= PHRASE_MIN_PROMPTS][:MAX_PHRASE_TAGS]
        tags: list[str] = []
        seen: set[str] = set()
        for tag in (*(_clean_tag(t) for t in always), *styles, *found, *(_clean_tag(t) for t in recurring)):
            if tag and tag.lower() not in seen:
                seen.add(tag.lower())
                tags.append(tag)
        group["tags"] = tags
