"""Saved-prompt folder tidy-up and the auto-tagger's folder guard (promptstudio)."""

from __future__ import annotations

from promptstudio import (FOLDER_SEP, _allowed_folders, _clean_folder, _clean_label_tag,
                          reorganize_saved)


def rec(n, folder, tags=None):
    return {"id": f"rs-{n}", "text": f"prompt {n}", "folder": folder, "tags": list(tags or [])}


def test_composites_fold_into_a_two_level_tree():
    records = [rec(i, "Actions / Motion - Energetic | Style / Look") for i in range(10)]
    records += [rec(100 + i, "Actions / Motion - Energ | Style / Lo") for i in range(3)]  # clipped mid-word
    records += [
        rec(200, "Actions / Motion - Calm | Pose / A"),  # a lone tone stays a tag, not a folder
        rec(300, "Extension - FF"),  # a person's own folders are left exactly as named
        rec(301, "Library"),
        rec(302, "Unfiled"),
    ]
    tidy, report = reorganize_saved(records)
    by = {r["id"]: r for r in tidy}
    energetic = "Actions / Motion" + FOLDER_SEP + "Energetic"
    assert by["rs-0"]["folder"] == energetic
    assert by["rs-100"]["folder"] == energetic  # "Energ" completed from the full spelling
    assert {"energetic", "style"} <= set(by["rs-100"]["tags"])
    assert by["rs-200"]["folder"] == "Actions / Motion" and {"calm", "pose"} <= set(by["rs-200"]["tags"])
    assert by["rs-300"]["folder"] == "Extension - FF" and by["rs-301"]["folder"] == "Library"
    assert by["rs-302"]["folder"] == ""
    assert (report["folders_before"], report["folders_after"], report["unfiled"]) == (6, 4, 1)
    top = {node["name"]: node for node in report["tree"]}
    assert top["Actions / Motion"]["count"] == 14
    assert top["Actions / Motion"]["children"] == [{"name": "Energetic", "count": 13}]
    again, second = reorganize_saved(tidy)
    assert again == tidy and second["changed"] == 0  # a tidy library comes back unchanged


def test_clipped_names_and_the_pose_family():
    clipped = "Character Descriptions - Energetic Walki"  # cut at the old 40-character cap
    assert len(clipped) == 40
    tidy, _ = reorganize_saved([rec(1, clipped), rec(2, "Pose / Calm"), rec(3, "Pose / Style / Look")])
    by = {r["id"]: r for r in tidy}
    assert by["rs-1"]["folder"] == "Character Descriptions"
    assert "energetic" in by["rs-1"]["tags"] and "energetic-walki" not in by["rs-1"]["tags"]
    assert by["rs-2"]["folder"] == "Pose / Action" and "calm" in by["rs-2"]["tags"]
    assert by["rs-3"]["folder"] == "Pose / Action" and "style" in by["rs-3"]["tags"]


def test_tag_spelling_variants_merge():
    records = [
        rec(1, "Library", ["close-up", "169"]),
        rec(2, "Library", ["closeup", "cu"]),
        rec(3, "Library", ["close-up", "boots"]),
        rec(4, "Library", ["boot"]),
        rec(5, "Library", ["boots"]),
    ]
    tidy, report = reorganize_saved(records)
    by = {r["id"]: r["tags"] for r in tidy}
    assert by["rs-1"] == ["close-up", "16x9"]
    assert by["rs-2"] == ["close-up"]  # punctuation variant + shorthand collapse into one tag
    assert by["rs-4"] == ["boots"]  # singular/plural: the more-used form wins
    assert report["retagged"] == 3
    assert _clean_label_tag("16:9 aspect") == "16x9-aspect"  # no more "169"


def test_auto_tagger_can_only_file_into_existing_folders():
    names = ["Actions / Motion", "Actions / Motion" + FOLDER_SEP + "Energetic", "Library",
             "Actions / Motion - Calm | Style / Lo"]
    allowed = _allowed_folders(names)
    assert "Actions / Motion - Calm | Style / Lo" not in allowed  # composites are never offered back
    assert "Character Descriptions" in allowed and "Library" in allowed
    nested = "Actions / Motion" + FOLDER_SEP + "Energetic"
    assert _clean_folder("actions / motion" + FOLDER_SEP + "energetic", allowed) == nested
    assert _clean_folder("Actions / Motion - Energetic | Pose / Action", allowed) == nested
    assert _clean_folder("Actions / Motion - Brand New Tone", allowed) == "Actions / Motion"
    assert _clean_folder("Totally New Folder", allowed) == ""
    assert _clean_folder("Unfiled", allowed) == ""


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("prompt folder tests passed")
