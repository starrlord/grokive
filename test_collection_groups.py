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
        {"id": "g1", "media_type": "video", "prompt": "secret root prompt", "tags": ["secret-root"],
         "created_at": "2026-07-01", "local_path": "media/videos/aa/g1.mp4"},
        {"id": "g2", "media_type": "video", "prompt": "secret sally prompt", "tags": ["secret-sally"],
         "created_at": "2026-07-02", "local_path": "media/videos/aa/g2.mp4"},
        {"id": "a1", "media_type": "image", "prompt": "secret audrey prompt", "tags": ["secret-audrey"],
         "created_at": "2026-07-03", "local_path": "media/videos/aa/a1.mp4"},
        {"id": "u1", "media_type": "video", "prompt": "public prompt", "tags": ["public"],
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
    assert "secret-root" not in facets and "public" in facets
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


if __name__ == "__main__":
    print("collection group tests")
    test_seed_migration_groups_only_multi_member_prefixes()
    test_group_lock_hides_members_and_preserves_absent_rows()
    test_group_unlock_layers_with_collection_lock_and_bulk_paths()
    test_purge_and_backup_include_group_state_and_validate_restore()
    print("all passed")
