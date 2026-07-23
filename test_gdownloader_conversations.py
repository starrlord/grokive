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


if __name__ == "__main__":
    print("imagine conversation walking golden tests")
    for name, test in sorted(dict(globals()).items()):
        if name.startswith("test_") and callable(test):
            test()
            print(f"  {name[5:].replace('_', ' ')} OK")
    print("all passed")
