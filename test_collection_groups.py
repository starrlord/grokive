from __future__ import annotations

import io
import json
import os
import tempfile
import zipfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp())
os.environ["GROK_DATA_DIR"] = str(_tmp)
os.environ["ADMIN_PASSWORD"] = "admin-secret"

import server  # noqa: E402


def _reset_state() -> None:
    server.DATA_DIR.mkdir(parents=True, exist_ok=True)
    for path in (
        server.COLLECTIONS_FILE,
        server.COLLECTION_GROUPS_FILE,
        server.METADATA_FILE,
        server.LIBRARY_FILE,
        server.DB_FILE,
    ):
        path.unlink(missing_ok=True)
    server.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    server.THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    server._locked_cache["mtime"] = None
    server._locked_cache["collections"] = {}
    server._locked_cache["groups"] = {}
    server._locked_cache["group_collections"] = {}


def _write_fixture() -> None:
    _reset_state()
    media_dir = server.MEDIA_DIR / "videos" / "aa"
    thumb_dir = server.THUMBS_DIR / "aa"
    media_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir.mkdir(parents=True, exist_ok=True)
    for mid in ("g1", "g2", "a1", "u1"):
        (media_dir / f"{mid}.mp4").write_bytes(b"x")
        (thumb_dir / f"{mid}.jpg").write_bytes(b"x")
    server.METADATA_FILE.write_text(json.dumps([
        {"id": "g1", "media_type": "video", "prompt": 'secret root prompt, she says "the secret root line"', "tags": ["secret-root"],
         "created_at": "2026-07-01", "local_path": "media/videos/aa/g1.mp4"},
        {"id": "g2", "media_type": "video", "prompt": "secret sally prompt", "tags": ["secret-sally"],
         "created_at": "2026-07-02", "local_path": "media/videos/aa/g2.mp4"},
        {"id": "a1", "media_type": "image", "prompt": "secret audrey prompt", "tags": ["secret-audrey"],
         "created_at": "2026-07-03", "local_path": "media/videos/aa/a1.mp4"},
        {"id": "u1", "media_type": "video", "prompt": 'public prompt, she says "a public line for everyone"', "tags": ["public"],
         "created_at": "2026-07-04", "local_path": "media/videos/aa/u1.mp4"},
    ]), encoding="utf-8")
    server.COLLECTIONS_FILE.write_text(json.dumps([
        {"id": "root", "name": "Identity Sheets", "ids": ["g1"], "created_at": "2026-07-01", "updated_at": "2026-07-01"},
        {"id": "aud", "name": "Identity Sheets - Audrey", "ids": ["a1"], "created_at": "2026-07-01",
         "updated_at": "2026-07-01", "locked": True, "pass_hash": server.generate_password_hash("audpw"),
         "locked_at": "2026-07-01 10:00:00"},
        {"id": "sal", "name": "Identity Sheets - Sally", "ids": ["g2"], "created_at": "2026-07-01", "updated_at": "2026-07-01"},
        {"id": "duo", "name": "Duos - One", "ids": ["u1"], "created_at": "2026-07-01", "updated_at": "2026-07-01"},
        {"id": "other", "name": "Other", "ids": ["u1"], "created_at": "2026-07-01", "updated_at": "2026-07-01"},
    ]), encoding="utf-8")
    server.rebuild_db(wait=True)


def _client():
    c = server.app.test_client()
    with c.session_transaction() as sess:
        sess["authed"] = True
    return c


def test_seed_migration_groups_only_multi_member_prefixes():
    _write_fixture()
    server._seed_collection_groups_once()
    first = server._load_collections(strict=True)
    by_id = {c["id"]: c for c in first}
    assert by_id["root"]["name"] == "Identity Sheets"
    assert by_id["root"]["group"] == "Identity Sheets"
    assert by_id["aud"]["name"] == "Audrey"
    assert by_id["aud"]["group"] == "Identity Sheets"
    assert by_id["sal"]["name"] == "Sally"
    assert by_id["sal"]["group"] == "Identity Sheets"
    assert "group" not in by_id["duo"]
    server._seed_collection_groups_once()
    assert server._load_collections(strict=True) == first
    print("  migration: multi-member prefix grouped, singleton prefix untouched, idempotent OK")


def test_group_lock_hides_members_and_preserves_absent_rows():
    _write_fixture()
    server._seed_collection_groups_once()
    c = _client()
    assert c.get("/api/collections").json.keys() == {"collections", "groups"}
    assert c.post("/api/collections/groups/lock", json={"name": "Identity Sheets", "password": "charpw"}).status_code == 200

    resp = c.get("/api/collections")
    body = resp.get_data(as_text=True)
    assert "Audrey" not in body and "Sally" not in body
    data = resp.json
    assert [g for g in data["groups"] if g["name"] == "Identity Sheets"][0]["collection_count"] == 3
    assert {x["id"] for x in data["collections"]} == {"duo", "other"}

    media = c.get("/api/media?view=all&page_size=20").json
    assert [it["id"] for it in media["items"]] == ["u1"]
    assert c.post("/api/media/by-ids", json={"ids": ["g1", "g2", "a1", "u1"]}).json["items"][0]["id"] == "u1"
    facets = c.get("/api/facets?view=all").get_data(as_text=True)
    # Tags are phrases lifted from prompts: the sealed member's spoken line must not leak.
    assert "the secret root line" not in facets and "a public line for everyone" in facets
    assert c.get("/media/videos/aa/g1.mp4").status_code == 404
    assert c.get("/thumbnails/aa/g1.jpg").status_code == 404

    visible_payload = {"collections": data["collections"]}
    assert c.post("/api/collections", json=visible_payload).status_code == 200
    assert {x["id"] for x in server._load_collections(strict=True)} == {"root", "aud", "sal", "duo", "other"}

    bad = {"collections": data["collections"] + [{"id": "other", "name": "Other", "ids": ["u1"], "group": "Identity Sheets"}]}
    assert c.post("/api/collections", json=bad).status_code == 403
    print("  lock: members hidden everywhere checked, save reinjects absent rows, anti-kidnap OK")


def test_group_unlock_layers_with_collection_lock_and_bulk_paths():
    _write_fixture()
    server._seed_collection_groups_once()
    c = _client()
    c.post("/api/collections/groups/lock", json={"name": "Identity Sheets", "password": "charpw"})
    assert c.post("/api/collections/groups/unlock", json={"name": "Identity Sheets", "password": "charpw"}).status_code == 200
    data = c.get("/api/collections").json
    by_id = {x["id"]: x for x in data["collections"]}
    assert by_id["root"]["ids"] == ["g1"]
    assert by_id["aud"]["locked"] is True and by_id["aud"]["unlocked"] is False and by_id["aud"]["ids"] == []

    assert c.post("/api/collections/relock-all", json={}).status_code == 200
    assert {x["id"] for x in c.get("/api/collections").json["collections"]} == {"duo", "other"}
    assert c.post("/api/collections/unlock-all", json={"password": "charpw"}).json["unlocked"] == 1
    assert "root" in {x["id"] for x in c.get("/api/collections").json["collections"]}
    assert c.post("/api/collections/groups/relock", json={"name": "Identity Sheets"}).status_code == 200
    assert c.post("/api/collections/groups/force-unlock", json={"name": "Identity Sheets", "admin_password": "admin-secret"}).status_code == 200
    assert c.post("/api/collections/groups/remove-lock", json={"name": "Identity Sheets", "password": ""}).status_code == 200
    assert c.get("/api/collections").json["groups"] == []
    print("  unlock: group unlock, layered collection lock, relock/unlock all, force/remove OK")


def test_purge_and_backup_include_group_state_and_validate_restore():
    _write_fixture()
    server._seed_collection_groups_once()
    c = _client()
    c.post("/api/collections/groups/lock", json={"name": "Identity Sheets", "password": "charpw"})
    server._purge_ids_from_collections({"g1"})
    by_id = {x["id"]: x for x in server._load_collections(strict=True)}
    assert by_id["root"]["group"] == "Identity Sheets"
    assert by_id["root"]["ids"] == []

    exported = c.get("/api/backup/export").data
    with zipfile.ZipFile(io.BytesIO(exported)) as zf:
        assert "collection_groups.json" in zf.namelist()
        original_collections = server.COLLECTIONS_FILE.read_text(encoding="utf-8")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as bad:
            for info in zf.infolist():
                payload = b"{bad json" if info.filename == "collection_groups.json" else zf.read(info.filename)
                bad.writestr(info.filename, payload)
    buf.seek(0)
    res = c.post("/api/backup/import", data={"file": (buf, "bad.zip")}, content_type="multipart/form-data")
    assert res.status_code == 400
    assert server.COLLECTIONS_FILE.read_text(encoding="utf-8") == original_collections
    print("  backup: collection_groups exported, corrupt JSON aborts before writes OK")


def test_parent_cover_draws_on_visible_children():
    _write_fixture()
    # Covers need indexed thumbs, and the index looks in the id's own shard.
    for mid in ("g1", "g2", "a1"):
        shard_dir = server.THUMBS_DIR / server.db.media_shard(mid)
        shard_dir.mkdir(parents=True, exist_ok=True)
        (shard_dir / f"{mid}.jpg").write_bytes(b"x")
    server.rebuild_db(wait=True)
    cols = server._load_collections(strict=True)
    for coll in cols:
        if coll["id"] in ("sal", "aud"):
            coll["parent_id"] = "root"
    server.COLLECTIONS_FILE.write_text(json.dumps(cols), encoding="utf-8")
    c = _client()
    root = next(x for x in c.get("/api/collections").json["collections"] if x["id"] == "root")
    assert root["ids"] == ["g1"] and root["item_count"] == 1  # counts stay the parent's own
    # Open child Sally's g2 (newer) joins the mosaic; locked child Audrey's a1 stays out.
    assert [it["id"] for it in root["cover_items"]] == ["g2", "g1"]
    assert root["cover_items"][0]["created_at"]
    assert c.post("/api/collections/aud/unlock", json={"password": "audpw"}).status_code == 200
    root = next(x for x in c.get("/api/collections").json["collections"] if x["id"] == "root")
    assert [it["id"] for it in root["cover_items"]] == ["a1", "g2", "g1"]
    print("  covers: parent mosaic includes visible children only, counts unchanged OK")


def test_group_cover_pin_lives_only_in_its_group():
    _write_fixture()
    c = _client()
    pin = "2026-09-14T00:00:00.000Z"

    def save(mutate):
        rows = c.get("/api/collections").json["collections"]
        for row in rows:
            mutate(row)
        assert c.post("/api/collections", json={"collections": rows}).status_code == 200
        return {x["id"]: x for x in server._load_collections(strict=True)}

    save(lambda r: r.update(group="Duos") if r["id"] == "duo" else r.update(parent_id="root") if r["id"] == "sal" else None)
    stored = save(lambda r: r.update(group_cover_at=pin) if r["id"] in ("duo", "other", "sal") else None)
    assert stored["duo"]["group_cover_at"] == pin
    assert "group_cover_at" not in stored["other"]  # ungrouped: nothing to be the cover of
    assert "group_cover_at" not in stored["sal"]  # nested child: no group, so no pin
    stored = save(lambda r: r.update(group="Elsewhere") if r["id"] == "duo" else None)
    assert stored["duo"]["group"] == "Elsewhere" and "group_cover_at" not in stored["duo"]
    print("  pins: kept on grouped members, dropped when ungrouped/nested/moved OK")


def test_sub_collection_cover_pin():
    _write_fixture()
    for mid in ("g1", "g2", "a1"):
        shard_dir = server.THUMBS_DIR / server.db.media_shard(mid)
        shard_dir.mkdir(parents=True, exist_ok=True)
        (shard_dir / f"{mid}.jpg").write_bytes(b"x")
    server.rebuild_db(wait=True)
    cols = server._load_collections(strict=True)
    for coll in cols:
        if coll["id"] in ("sal", "aud"):
            coll["parent_id"] = "root"
        if coll["id"] == "root":
            coll["cover_id"] = "g1"
    server.COLLECTIONS_FILE.write_text(json.dumps(cols), encoding="utf-8")
    c = _client()

    def root_row():
        return next(x for x in c.get("/api/collections").json["collections"] if x["id"] == "root")

    def save_root(**patch):
        rows = c.get("/api/collections").json["collections"]
        for row in rows:
            if row["id"] == "root":
                row.update(patch)
        assert c.post("/api/collections", json={"collections": rows}).status_code == 200

    save_root(cover_child_id="sal")
    root = root_row()
    assert [it["id"] for it in root["cover_items"]] == ["g2"]
    assert root["cover_peek"]["id"] == "g2"
    assert root["cover_id"] == "g1"  # the parent's own cover_id survives the round-trip
    save_root(cover_child_id="aud")  # sealed child: can't show, so the pooled mosaic stands in
    assert [it["id"] for it in root_row()["cover_items"]] == ["g2", "g1"]
    assert c.post("/api/collections/aud/unlock", json={"password": "audpw"}).status_code == 200
    assert [it["id"] for it in root_row()["cover_items"]] == ["a1"]
    save_root(cover_child_id="duo")  # not a child of root: dropped
    assert "cover_child_id" not in {x["id"]: x for x in server._load_collections(strict=True)}["root"]
    print("  sub-collection pin: pinned child's covers, sealed pin falls back, bad pin dropped OK")


if __name__ == "__main__":
    print("collection group tests")
    test_seed_migration_groups_only_multi_member_prefixes()
    test_group_lock_hides_members_and_preserves_absent_rows()
    test_group_unlock_layers_with_collection_lock_and_bulk_paths()
    test_purge_and_backup_include_group_state_and_validate_restore()
    test_parent_cover_draws_on_visible_children()
    test_group_cover_pin_lives_only_in_its_group()
    test_sub_collection_cover_pin()
    print("all passed")
