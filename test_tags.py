"""Tags lifted from prompts (mediautil.tags_for_groups)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from mediautil import TagWords, load_tag_words, tags_for_groups


def tags_of(*prompts: str, words: TagWords | None = None) -> list[list[str]]:
    groups = [{"prompt": p} for p in prompts]
    tags_for_groups(groups, words)
    return [g["tags"] for g in groups]


def test_reported_speech_and_talk_noun_are_whole_tags():
    [tags] = tags_of(
        "@4c900cb7-0389-4194-aca7-cda55871ebed @b3870702-359a-4778-95d8-40b14e8ac5cd are all waiting "
        "in line, they tell the ticket agent what they;ll pay that the others won't in between sips, "
        "polite small talk"
    )
    assert "they tell the ticket agent what they'll pay that the others won't in between sips" in tags
    assert "polite small talk" in tags
    assert not any("4c900cb7" in t or "b3870702" in t for t in tags)


def test_quoted_and_cued_lines_are_tags_but_cues_are_not():
    [tags] = tags_of(
        "Kitchen at dusk. She leans in and whispers: “Meet me by the pool, at noon.” "
        "He says: bring the good glasses tonight. A sign reading \"OPEN\". Camera slow push-in."
    )
    assert tags == ["meet me by the pool, at noon", "bring the good glasses tonight", "push-in"]


def test_unquoted_line_runs_on_while_it_is_still_speech():
    [tags] = tags_of("She says: First coffee is in. You smell that. Three sips at 0-3s, slow push-in.")
    assert tags == ["first coffee is in. you smell that", "push-in"]


def test_delivery_only_speech_is_not_a_tag():
    [tags] = tags_of(
        "She speaks aloud to camera with clear lip-sync, talk-show segment, only one woman speaking at a time"
    )
    assert tags == ["lip-sync"]  # the camera/style tag stays; the delivery direction isn't speech


def test_speech_tag_starts_at_the_speech():
    [tags] = tags_of("She pulls her hair back and paces the room while saying everything she plans for the weekend")
    assert tags == ["saying everything she plans for the weekend"]
    [tags] = tags_of("She sips slowly and tells all of the ways she likes her coffee (hot, black, strong), soft light")
    assert tags == ["tells all of the ways she likes her coffee (hot, black, strong)"]


def test_short_phrases_must_recur_and_directives_never_count():
    shared = [
        "Take one, brown leather sofa, face fully unobstructed, shallow depth of field",
        "Take two, brown leather sofa, face fully unobstructed",
        "Another angle entirely, brown leather sofa, top row",
    ]
    assert tags_of(*shared) == [["shallow depth of field", "brown leather sofa"], ["brown leather sofa"], ["brown leather sofa"]]
    assert tags_of("Quiet morning, brown leather sofa") == [[]]  # used once: not a tag yet


def test_camera_style_tags_are_canonical_and_respect_negation():
    [tags] = tags_of(
        "Handheld iPhone selfie style footage, DoP camera work and angles, multiple angles, MCU no ECU, "
        "not golden hour, no orbit photoreal, Ms Davis smiles, 9:16"
    )
    # One canonical label per concept, in vocabulary order; negated terms (ECU, golden hour,
    # orbit) are dropped, and a negation doesn't leak onto the next term ("no orbit photoreal").
    # Abbreviations are case-sensitive: "Ms Davis" is not a medium shot.
    assert tags == ["iPhone selfie", "DoP camera work", "multi-angle", "handheld", "medium close-up",
                    "photoreal", "9:16 vertical"]


def test_every_style_pattern_has_a_prefilter_literal():
    # Style matching skips a term unless its required literal text appears in the prompt; an
    # alternative without one would silently never be tried.
    from mediautil import _STYLE_TAGS, _required_prefix, _top_level_alternatives
    for label, rx in _STYLE_TAGS:
        for alt in _top_level_alternatives(rx):
            assert len(_required_prefix(alt)) >= 2, (label, alt)


def test_recurring_action_phrases_are_mined_under_one_label():
    corpus = [
        "They pause to hang out with friends and family on the porch",
        "The two sisters hangout with friends and family by the pool",
        "In the kitchen they are hanging out with friends and family until the pie cools",
        "Both cousins hang out with friends and family in the yard",
        "Later they hang out with friends on the dock",
        "The couple hang out with friends under the stars",
        "Giggling, the pair hang out with friends for a long time",
        "After that they hang out with friends and laugh",
        "Quiet dinner scene, warm candles",
    ]
    tags = tags_of(*corpus, words=TagWords(split_compounds=(("hangout", "hang out"),)))
    # Every spelling (hang out / hangout / hanging out) counts toward one phrase, labelled as the
    # activity; the "… and family" extension is its own tag without hiding the broader one.
    assert {"hanging out with friends", "hanging out with friends and family"} <= set(tags[0])
    assert "hanging out with friends and family" in tags[1]
    assert "hanging out with friends" in tags[4] and "hanging out with friends and family" not in tags[4]
    assert tags[8] == []
    assert not any(t in ("out with friends", "friends and family", "hang out with") for ts in tags for t in ts)


def test_tag_words_file_extends_the_built_in_lists():
    prompt = "Two old friends trade cheesy corny talk at the diner"
    assert tags_of(prompt) == [[]]  # "cheesy" / "corny" aren't built-in talk words
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tag_words.json"
        path.write_text(json.dumps({"talk_adjectives": ["Cheesy", "corny"], "hanging_words": "not a list",
                                    "surprise": 1}), encoding="utf-8")
        words = load_tag_words(path)
        assert "cheesy" in words.talk_adjectives and not words.hanging_words  # bad entries ignored
        assert tags_of(prompt, words=words) == [["cheesy corny talk"]]
        path.write_text("{not json", encoding="utf-8")
        assert load_tag_words(path) == TagWords()  # unreadable file: no additions, no crash
        assert load_tag_words(Path(tmp) / "missing.json") == TagWords()


def test_long_lines_are_capped():
    [tags] = tags_of('She says "' + " ".join(["word"] * 80) + '"')
    assert len(tags) == 1 and len(tags[0]) <= 161 and tags[0].endswith("…")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("tag tests passed")
