"""Imagine v2 conversation walking — gdownloader's replacement for the dead post chain.

Fixtures mirror the two real response shapes: the conversational edit flow (prompt and
inputs on the turn's ``mediaGenInput``) and agent mode (prompt on the asset's ``summary``,
inputs on ``auxKeys.input_assets``, several assets emitted per turn).
"""

import gdownloader as g


def asset(asset_id, *, key=None, mime="image/jpeg", response_id=None, summary="",
          created="2026-07-23T02:19:24.000Z", aux=None, deleted=False, width=1280, height=720):
    return {
        "assetId": asset_id,
        "mimeType": mime,
        "key": key if key is not None else f"users/u1/generated/{asset_id}/image.jpg",
        "summary": summary,
        "createTime": created,
        "auxKeys": aux or {},
        "responseId": response_id,
        "isDeleted": deleted,
        "width": width,
        "height": height,
    }


def turn(response_id, assets, *, sender="ASSISTANT", model=None, gen=None, message=""):
    return {
        "responseId": response_id,
        "sender": sender,
        "message": message,
        "model": model,
        "mediaGenInput": gen or {},
        "fileAttachmentAssetMetadata": assets,
    }


ROOT = "5e6c5555-8ba3-4de2-aa6e-22d79e1112c0"
EDIT = "2df3704d-1111-4111-8111-111111111111"
NEXT = "cb333c6e-2222-4222-8222-222222222222"
CLIP = "ffe56162-3333-4333-8333-333333333333"


def edit_flow():
    """Upload -> edit -> re-edit -> video, the way the chat endpoint returns it."""
    upload = asset(ROOT, key=f"users/u1/{ROOT}/content", mime="image/png",
                   created="2026-07-23T02:10:08.000Z", width=None, height=None)
    return {"responses": [
        turn("r1", [upload], sender="human", message="Different outfit",
             gen={"imageToImage": {"prompt": "Different outfit", "inputAssets": [ROOT],
                                   "modelName": "imagine-image-edit"}}),
        turn("r2", [asset(EDIT, response_id="r2")], model="imagine-image-edit",
             gen={"imageToImage": {"prompt": "Different outfit", "inputAssets": [ROOT],
                                   "modelName": "imagine-image-edit"}}),
        # The same asset rides along as INPUT on the next turn, with that turn's prompt.
        turn("r3", [asset(EDIT, response_id="r2")], sender="human",
             gen={"imageToImage": {"prompt": "Increase chest volume", "inputAssets": [EDIT],
                                   "modelName": "imagine-image-edit"}}),
        turn("r4", [asset(NEXT, response_id="r4")], model="imagine-image-edit",
             gen={"imageToImage": {"prompt": "Increase chest volume", "inputAssets": [EDIT],
                                   "modelName": "imagine-image-edit"}}),
        turn("r5", [asset(CLIP, mime="video/mp4", response_id="r5",
                          key=f"users/u1/generated/{CLIP}/generated_video.mp4")],
             model="imagine-video-gen",
             gen={"imageToVideo": {"prompt": "She walks", "inputAssets": [NEXT],
                                   "modelName": "imagine-video-gen"}}),
    ]}


def by_id(items):
    return {item["id"]: item for item in items}


def test_walks_the_whole_chain():
    items = by_id(g.extract_conversation_items(edit_flow()))
    assert set(items) == {ROOT, EDIT, NEXT, CLIP}


def test_producing_turn_wins_over_later_input_references():
    """EDIT is attached to three turns; only the one that made it describes it."""
    items = by_id(g.extract_conversation_items(edit_flow()))
    assert items[EDIT]["prompt"] == "Different outfit"
    assert items[EDIT]["parent_id"] == ROOT
    assert items[EDIT]["model"] == "imagine-image-edit"


def test_parents_rebuild_the_lineage():
    items = by_id(g.extract_conversation_items(edit_flow()))
    assert items[ROOT]["parent_id"] is None       # uploaded reference: chain starts here
    assert items[NEXT]["parent_id"] == EDIT
    assert items[CLIP]["parent_id"] == NEXT


def test_uploaded_reference_is_archived_without_a_prompt():
    """No turn produced it, so it must not inherit the prompt of the turn it fed."""
    items = by_id(g.extract_conversation_items(edit_flow()))
    assert items[ROOT]["prompt"] == ""
    assert items[ROOT]["model"] is None
    assert items[ROOT]["source_url"] == f"https://assets.grok.com/users/u1/{ROOT}/content"


def test_media_type_survives_an_extensionless_upload_url():
    items = by_id(g.extract_conversation_items(edit_flow()))
    assert g.resolve_media_type(items[ROOT]) == "image"   # /content, typed by mimeType
    assert g.resolve_media_type(items[CLIP]) == "video"


def test_records_normalize_like_any_other_source():
    from pathlib import Path
    items = by_id(g.extract_conversation_items(edit_flow()))
    record = g.normalize_record(items[CLIP], Path("media/videos/ab/clip.mp4"))
    assert record.id == CLIP
    assert record.media_type == "video"
    assert record.parent_id == NEXT
    assert record.created_at == "2026-07-23T02:19:24.000Z"
    assert record.width == 1280


def agent_flow():
    """Agent mode: one turn, several assets, prompt per asset, inputs in auxKeys."""
    made = [
        asset("a0000000-0000-4000-8000-00000000000%d" % n, response_id="r1",
              summary=f"keyframe {n}", aux={"input_assets": f'["{ROOT}"]',
                                            "generation_type": "image_to_image"})
        for n in (1, 2)
    ]
    return {"responses": [turn("r1", made, model="imagine-agent-mode-grok-4-5")]}


def test_agent_mode_reads_prompt_and_parent_off_the_asset():
    items = g.extract_conversation_items(agent_flow())
    assert len(items) == 2
    assert [item["prompt"] for item in items] == ["keyframe 1", "keyframe 2"]
    assert {item["parent_id"] for item in items} == {ROOT}
    assert {item["model"] for item in items} == {"imagine-agent-mode-grok-4-5"}


def test_per_asset_inputs_beat_the_turn_level_ones():
    """A turn that emits several assets can't describe them all; auxKeys can."""
    payload = {"responses": [turn(
        "r1",
        [asset("b0000000-0000-4000-8000-000000000001", response_id="r1",
               aux={"input_assets": f'["{NEXT}"]'})],
        gen={"imageToImage": {"prompt": "p", "inputAssets": [ROOT]}},
    )]}
    assert g.extract_conversation_items(payload)[0]["parent_id"] == NEXT


def test_parent_falls_back_to_a_reference_url():
    payload = {"responses": [turn(
        "r1",
        [asset("c0000000-0000-4000-8000-000000000001", response_id="r1",
               aux={"image_references": f'["https://assets.grok.com/users/u1/generated/{NEXT}/image.jpg"]'})],
    )]}
    assert g.extract_conversation_items(payload)[0]["parent_id"] == NEXT


USER = "b7813d5a-ed45-4ef7-b3d2-fa56dd8fa748"


def legacy_turn(urls, *, attachments):
    return {"responses": [{
        "responseId": "r1", "sender": "ASSISTANT", "model": "imagine-image-edit",
        "createTime": "2026-07-22T19:29:01.232Z",
        "generatedImageUrls": urls, "fileAttachments": attachments,
        "generatedImageWidth": 1280, "generatedImageHeight": 720,
        "mediaGenInput": {"imageToImage": {"prompt": "Remove blush", "inputAssets": [EDIT],
                                           "modelName": "imagine-image-edit"}},
    }]}


def test_older_turns_name_their_output_as_a_bare_storage_key():
    """Pre-asset-metadata turns only carry generatedImageUrls (a key, not a URL)."""
    item, = g.extract_conversation_items(
        legacy_turn([f"users/{USER}/generated/{NEXT}/image.jpg"], attachments=[NEXT]))
    assert item["id"] == NEXT   # NOT the user id the key starts with
    assert item["source_url"] == f"https://assets.grok.com/users/{USER}/generated/{NEXT}/image.jpg"
    assert item["prompt"] == "Remove blush"
    assert item["parent_id"] == EDIT
    assert (item["width"], item["height"]) == (1280, 720)
    assert g.resolve_media_type(item) == "image"


def test_moderated_turns_are_not_archived():
    """Grok reports a path for a generation it then threw away; that URL 404s forever."""
    assert g.extract_conversation_items(
        legacy_turn([f"users/{USER}/generated/{NEXT}/image.jpg"], attachments=[])) == []
    assert g.extract_conversation_items(legacy_turn([""], attachments=[NEXT])) == []


def test_asset_id_is_read_off_the_end_of_a_key_not_the_user_id():
    reference = f"https://assets.grok.com/users/{USER}/generated/{NEXT}/image.jpg"
    assert g._asset_id_in_url(reference) == NEXT
    assert g._asset_id_in_url(f"users/{USER}/{ROOT}/content") == ROOT
    assert g._asset_id_in_url("users/u1/generated/nope/image.jpg") is None
    payload = {"responses": [turn(
        "r1", [asset("e0000000-0000-4000-8000-000000000001", response_id="r1",
                     aux={"image_reference": reference})])]}
    assert g.extract_conversation_items(payload)[0]["parent_id"] == NEXT


def test_generated_urls_never_duplicate_an_attached_asset():
    """A modern turn carries both shapes; the asset metadata is the richer one."""
    payload = {"responses": [turn(
        "r1", [asset(NEXT, response_id="r1", summary="attached")],
        gen={"imageToImage": {"prompt": "gen", "inputAssets": [EDIT]}},
    )]}
    payload["responses"][0]["generatedImageUrls"] = [f"users/u1/generated/{NEXT}/image.jpg"]
    payload["responses"][0]["fileAttachments"] = [NEXT]
    item, = g.extract_conversation_items(payload)
    assert item["prompt"] == "gen" and item["width"] == 1280


def test_deleted_and_keyless_assets_are_skipped():
    payload = {"responses": [turn("r1", [
        asset("d0000000-0000-4000-8000-000000000001", response_id="r1", deleted=True),
        {"assetId": "d0000000-0000-4000-8000-000000000002", "mimeType": "image/jpeg"},
        {"mimeType": "image/jpeg", "key": "users/u1/generated/x/image.jpg"},
    ])]}
    assert g.extract_conversation_items(payload) == []


def test_malformed_payloads_return_nothing():
    for payload in ({}, {"responses": None}, {"responses": ["nope"]}, [], None):
        assert g.extract_conversation_items(payload) == []


def test_conversation_id_comes_from_the_query_not_the_path():
    """Grok's share link names the POST in the path and the conversation in the query."""
    url = f"https://grok.com/imagine/post/{ROOT}?conversation=555329a0-f108-4c39-94fd-bbc4d09e8f2e"
    assert g.normalize_conversation_id(url) == "555329a0-f108-4c39-94fd-bbc4d09e8f2e"
    assert g.normalize_conversation_id("  555329a0  ") == "555329a0"
    assert g.normalize_conversation_id("https://grok.com/chat/abc123/") == "abc123"


def test_conversation_specs_are_bodyless_gets_that_ask_for_imagine_chats():
    auth = g.RequestSpec("POST", "https://grok.com/rest/media/post/list",
                         {"Content-Type": "application/json", "User-Agent": "ua"},
                         {"sso": "token"}, "{}")
    listing = g.grok_conversation_list_spec(auth, 100)
    assert listing.method == "GET" and listing.body is None
    assert "kind=CONVERSATION_KIND_IMAGINE" in listing.url and "pageSize=100" in listing.url
    assert "Content-Type" not in listing.headers
    assert "sso=token" in listing.headers_with_cookies()["Cookie"]

    responses = g.grok_conversation_responses_spec(auth, "conv-1")
    assert responses.url.endswith("/rest/app-chat/conversations/conv-1/responses")
    assert responses.method == "GET" and responses.body is None


SD = "https://assets.grok.com/users/u1/generated/vid-1/generated_video.mp4"
HD = "https://assets.grok.com/users/u1/generated/vid-1/generated_video_1080_hd.mp4"


SPEC = g.RequestSpec("GET", "https://assets.grok.com/", {"User-Agent": "ua"}, {"sso": "t"}, None)


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code


def _fake_cdn(present, raises=False):
    """Stand in for the CDN at the TRANSPORT seam (curl_cffi), not at _probe_url — the
    per-run memo lives in prefer_hd1080 around it, so stubbing higher would hide what we're
    testing. `present` is the set of urls answering 200; calls records every request made."""
    calls = []

    def head(url, **_kwargs):
        calls.append(url)
        if raises:
            raise g.CffiRequestException("boom")
        return _Response(200 if url in present else 404)

    return head, calls


def _with_cdn(head, fn):
    original = g.cffi_requests.head
    g.cffi_requests.head = head
    g._HD1080_PROBES.clear()
    try:
        return fn()
    finally:
        g.cffi_requests.head = original
        g._HD1080_PROBES.clear()


def test_the_1080_sibling_is_derived_only_for_v2_generated_videos():
    """Grok never names the upscale, so we reconstruct its key — but only where one can exist."""
    assert g._hd1080_sibling_url(SD) == HD
    assert g._hd1080_sibling_url(SD + "?sig=tok") == HD      # signing query dropped
    assert g._hd1080_sibling_url(HD) == ""                   # already the upscale
    assert g._hd1080_sibling_url("https://assets.grok.com/users/u1/generated/i/image.jpg") == ""
    assert g._hd1080_sibling_url("https://assets.grok.com/x/content") == ""  # canvas, extensionless
    assert g._hd1080_sibling_url("") == ""


def test_a_video_with_a_1080_render_is_repointed_at_it():
    """The whole fix: rewriting source_url lifts the rank 1 -> 3, which is exactly what
    process_item already treats as an upgrade, so refresh_hd replaces the SD file in place."""
    head, calls = _fake_cdn({HD})
    items = [{"id": "vid-1", "source_url": SD, "width": 1280, "height": 720}]
    moved = _with_cdn(head, lambda: g.prefer_hd1080(SPEC, items, {}))
    assert moved == 1
    assert items[0]["source_url"] == HD
    assert calls == [HD]
    assert g._media_res_rank(items[0]["source_url"]) > g._media_res_rank(SD)
    # SD dimensions describe the file we're no longer taking; db.py re-ffprobes 1080 names.
    assert items[0]["width"] is None and items[0]["height"] is None


def test_a_video_without_one_keeps_its_sd_url():
    """The fallback. A 404 url written into the record would cost 5 backed-off download
    attempts on EVERY later sync, so a missing sibling must change nothing at all."""
    head, _ = _fake_cdn(set())
    items = [{"id": "vid-1", "source_url": SD, "width": 1280, "height": 720}]
    moved = _with_cdn(head, lambda: g.prefer_hd1080(SPEC, items, {}))
    assert moved == 0
    assert items[0]["source_url"] == SD
    assert items[0]["width"] == 1280 and items[0]["height"] == 720


def test_an_unreachable_cdn_keeps_the_sd_url():
    """A refused/errored probe is indistinguishable from absent, and guessing wrong costs
    5 backed-off download attempts per sync — so a transport failure must never swap."""
    head, calls = _fake_cdn({HD}, raises=True)
    items = [{"id": "vid-1", "source_url": SD, "width": 1280, "height": 720}]
    moved = _with_cdn(head, lambda: g.prefer_hd1080(SPEC, items, {}))
    assert moved == 0 and calls == [HD]
    assert items[0]["source_url"] == SD and items[0]["width"] == 1280


def test_an_item_already_held_at_1080_is_not_probed_again():
    head, calls = _fake_cdn({HD})
    items = [{"id": "vid-1", "source_url": SD}]
    moved = _with_cdn(head, lambda: g.prefer_hd1080(SPEC, items, {"vid-1": {"source_url": HD}}))
    assert moved == 0 and calls == []


def test_probe_results_are_memoised_within_a_run():
    head, calls = _fake_cdn({HD})

    def run():
        g.prefer_hd1080(SPEC, [{"id": "vid-1", "source_url": SD}], {})
        g.prefer_hd1080(SPEC, [{"id": "vid-1", "source_url": SD}], {})

    _with_cdn(head, run)
    assert calls == [HD]


def test_images_and_canvas_assets_are_left_alone():
    head, calls = _fake_cdn({HD})
    items = [
        {"id": "img-1", "source_url": "https://assets.grok.com/users/u1/generated/img-1/image.jpg"},
        {"id": "can-1", "source_url": "https://assets.grok.com/x/content"},
    ]
    moved = _with_cdn(head, lambda: g.prefer_hd1080(SPEC, items, {}))
    assert moved == 0 and calls == []
    assert items[0]["source_url"].endswith("image.jpg")


def test_probes_fan_out_but_each_url_is_asked_once():
    """Parallel probing must not turn a shared sibling into duplicate requests."""
    head, calls = _fake_cdn({HD})
    other_sd = "https://assets.grok.com/users/u1/generated/vid-2/generated_video.mp4"
    other_hd = g._hd1080_sibling_url(other_sd)
    items = [
        {"id": "vid-1", "source_url": SD},
        {"id": "vid-1-again", "source_url": SD},
        {"id": "vid-2", "source_url": other_sd},
    ]
    moved = _with_cdn(head, lambda: g.prefer_hd1080(SPEC, items, {}))
    assert sorted(calls) == sorted([HD, other_hd])
    assert moved == 2
    assert items[0]["source_url"] == HD and items[1]["source_url"] == HD
    assert items[2]["source_url"] == other_sd


def _run_download_batch(workers):
    """Drive process_items over a batch whose downloads finish in REVERSE order, and record
    which thread made every library write. Returns what the batch left behind."""
    import argparse
    import json
    import tempfile
    import threading
    import time
    from pathlib import Path

    ids = [f"vid-{n}" for n in range(8)]
    base = "https://assets.grok.com/users/u1/generated"
    items = [{"id": i, "prompt": f"prompt {i}", "createdAt": "2026-09-14T00:00:00Z",
              "source_url": f"{base}/{i}/generated_video.mp4"} for i in ids]
    items[6]["source_url"] = f"{base}/vid-6/generated_video_1080_hd.mp4"  # an upscale of a held clip
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}
    writers = []

    def fake_download(client, spec, url, dest):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.03 * (len(ids) - ids.index(dest.name)))  # the first item finishes LAST
        with lock:
            state["active"] -= 1
        if dest.name == "vid-3":
            raise RuntimeError("cdn said no")
        out = dest.with_suffix(".mp4")
        out.write_bytes(url.encode())
        return out

    saved_globals = (g.GALLERY_ROOT, g.DOWNLOAD_WORKERS, g.DELETED_IDS, g.REFRESHED,
                     g.download_media, g.save_metadata, g.append_failure)
    real_save, real_fail = g.save_metadata, g.append_failure
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "gallery"
        try:
            g.GALLERY_ROOT, g.DOWNLOAD_WORKERS, g.DELETED_IDS, g.REFRESHED = root, workers, set(), 0
            g.download_media = fake_download
            g.save_metadata = lambda path, recs: (writers.append(threading.get_ident()), real_save(path, recs))
            g.append_failure = lambda path, f: (writers.append(threading.get_ident()), real_fail(path, f))
            held_rel = f"media/videos/{g.media_shard('vid-6')}/vid-6.mp4"
            (root / held_rel).parent.mkdir(parents=True)
            (root / held_rel).write_bytes(b"sd")
            by_id = {"vid-6": {"id": "vid-6", "prompt": "kept", "media_type": "video",
                               "source_url": f"{base}/vid-6/generated_video.mp4", "local_path": held_rel}}
            on_disk = root / f"media/videos/{g.media_shard('vid-5')}/vid-5.mp4"  # indexed, not fetched
            on_disk.parent.mkdir(parents=True, exist_ok=True)
            on_disk.write_bytes(b"copied in")
            args = argparse.Namespace(metadata=Path(tmp) / "metadata.json",
                                      failures=Path(tmp) / "failed.json", quiet=True)
            saved = g.process_items(None, SPEC, items, by_id, args)
            return {
                "saved": saved, "peak": state["peak"], "writers": set(writers),
                "order": list(by_id), "by_id": by_id, "refreshed": g.REFRESHED,
                "metadata": json.loads(args.metadata.read_text(encoding="utf-8")),
                "failures": [f["id"] for f in json.loads(args.failures.read_text(encoding="utf-8"))],
            }
        finally:
            (g.GALLERY_ROOT, g.DOWNLOAD_WORKERS, g.DELETED_IDS, g.REFRESHED,
             g.download_media, g.save_metadata, g.append_failure) = saved_globals


def test_parallel_downloads_write_the_library_in_order_from_one_thread():
    """The metadata.json guarantee. Downloads overlap and finish in reverse order, yet every
    write — by_id, metadata.json, the failure log — lands on the main thread, in batch order."""
    import threading

    run = _run_download_batch(workers=4)
    assert run["peak"] > 1                                   # the downloads really overlapped
    assert run["writers"] == {threading.get_ident()}         # ...and nothing wrote off-thread
    assert run["saved"] == 5                                 # 8 items: 1 failed, 1 indexed, 1 upscaled
    assert run["order"] == ["vid-6", "vid-0", "vid-1", "vid-2", "vid-4", "vid-5", "vid-7"]
    assert run["metadata"] == list(run["by_id"].values())    # the file matches memory exactly
    assert run["failures"] == ["vid-3"]
    assert run["refreshed"] == 1
    assert run["by_id"]["vid-6"]["prompt"] == "kept"         # the upscale merged, didn't replace
    assert run["by_id"]["vid-6"]["source_url"].endswith("generated_video_1080_hd.mp4")


def test_parallel_batch_leaves_the_same_library_as_the_serial_loop():
    """GROK_DOWNLOAD_WORKERS=1 is the old serial loop; any worker count must end identical."""
    serial, parallel = _run_download_batch(workers=1), _run_download_batch(workers=4)
    assert serial["peak"] == 1
    for key in ("saved", "order", "metadata", "failures", "refreshed"):
        assert serial[key] == parallel[key], key


if __name__ == "__main__":
    print("imagine conversation walking golden tests")
    for name, test in sorted(dict(globals()).items()):
        if name.startswith("test_") and callable(test):
            test()
            print(f"  {name[5:].replace('_', ' ')} OK")
    print("all passed")
