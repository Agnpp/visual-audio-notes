import json

import pytest

from visual_audio_notes.capabilities import list_capabilities, plan_workflow


def by_id(plan):
    return {row["id"]: row for row in plan["steps"]}


def connection(kind, location="local", **extras):
    return {kind: {"id": "chosen-model", "location": location, "capabilities": [kind], **extras}}


def test_catalog_separates_model_requirements_from_deployment():
    catalog = {row["id"]: row for row in list_capabilities()}
    assert len(catalog) == 13
    assert not catalog["native_text_extract"]["requires_ai"]
    assert not catalog["keyword_search"]["requires_ai"]
    assert not catalog["lexical_candidates"]["requires_ai"]
    assert catalog["transcription"]["required_model_kind"] == "speech_to_text"
    assert catalog["ocr"]["required_model_kind"] == "ocr"
    assert catalog["semantic_candidates"]["required_model_kind"] == "embedding"
    assert "不一定需要大语言模型" in catalog["ocr"]["explanation"]
    assert "location" not in catalog["grounded_synthesis"]
    first = list_capabilities()
    first[0]["label"] = "changed"
    assert list_capabilities()[0]["label"] != "changed"


def test_local_and_remote_models_have_the_same_ai_requirements():
    inputs = {"has_transcript": True, "requested_ai": ["grounded_synthesis"]}
    local = by_id(plan_workflow(inputs, connection("text_generation", "local")))["grounded_synthesis"]
    remote = by_id(plan_workflow(inputs, connection("text_generation", "remote")))["grounded_synthesis"]
    for key in ("requires_ai", "required_model_kind", "status", "connection_id"):
        assert local[key] == remote[key]
    assert local["requires_ai"] is True and local["status"] == "ready"
    assert local["connection_location"] == "local"
    assert remote["connection_location"] == "remote"


def test_existing_native_text_and_transcript_enable_a_no_ai_workflow():
    plan = plan_workflow({"has_native_text": True, "has_transcript": True, "has_audio": True})
    steps = by_id(plan)
    assert not plan["missing_connections"]
    assert {"file_import", "native_text_extract", "preview", "manual_edit", "keyword_search",
            "lexical_candidates", "export"} == set(plan["can_use_without_ai"])
    assert steps["transcription"]["status"] == "reuse"
    assert steps["ocr"]["status"] == "reuse"
    assert steps["grounded_synthesis"]["status"] == "optional"
    assert all(s["connection_id"] is None for s in steps.values())


@pytest.mark.parametrize("identifier,kind,inputs", [
    ("transcription", "speech_to_text", {"has_audio": True}),
    ("ocr", "ocr", {"has_images": True}),
    ("semantic_candidates", "embedding", {"has_native_text": True, "has_transcript": True}),
    ("image_understanding", "vision", {"has_images": True}),
])
def test_text_connection_does_not_implicitly_support_other_model_kinds(identifier, kind, inputs):
    inputs["requested_ai"] = [identifier]
    wrong_selection = {kind: {"id": "text-only", "location": "remote", "capabilities": ["text_generation"]}}
    plan = plan_workflow(inputs, wrong_selection)
    step = by_id(plan)[identifier]
    assert step["status"] == "needs_connection"
    assert step["missing_model_kind"] == kind
    assert plan["missing_connections"] == [kind]
    assert step["connection_id"] == "text-only"


def test_connections_are_explicitly_selected_not_automatically_routed():
    inputs = {"has_audio": True, "requested_ai": ["transcription"]}
    plan = plan_workflow(inputs, {"text_generation": {"id": "multi-capable", "location": "local",
                                                     "capabilities": ["text_generation", "speech_to_text"]}})
    assert by_id(plan)["transcription"]["status"] == "needs_connection"
    selected = {"speech_to_text": {"id": "multi-capable", "location": "local",
                                  "capabilities": ["text_generation", "speech_to_text"]}}
    assert by_id(plan_workflow(inputs, selected))["transcription"]["status"] == "ready"


def test_native_text_skips_ocr_but_images_still_need_their_own_recognition():
    plain = by_id(plan_workflow({"has_native_text": True, "requested_ai": ["ocr"]}))
    assert plain["ocr"]["status"] == "reuse"
    mixed = by_id(plan_workflow({"has_native_text": True, "has_images": True, "requested_ai": ["ocr"]}))
    assert mixed["ocr"]["status"] == "needs_connection"
    assert mixed["native_text_extract"]["status"] == "ready"


def test_existing_transcript_is_reused_even_if_transcription_was_selected():
    plan = plan_workflow({"has_audio": True, "has_transcript": True, "requested_ai": ["transcription"]})
    step = by_id(plan)["transcription"]
    assert step["requires_ai"] is True  # The function still needs a model when actually run.
    assert step["status"] == "reuse" and step["connection_id"] is None
    assert not plan["missing_connections"]


def test_choosing_a_model_does_not_silently_enable_an_ai_feature():
    plan = plan_workflow({"has_transcript": True}, connection("text_generation"))
    assert by_id(plan)["grounded_synthesis"]["status"] == "optional"
    assert plan["ai_requested"] == []


def test_requested_conversion_does_not_pretend_its_text_output_already_exists():
    plan = plan_workflow({"has_audio": True, "has_images": True,
                          "requested_ai": ["transcription", "ocr", "semantic_candidates"]},
                         {**connection("speech_to_text"), **connection("ocr"), **connection("embedding")})
    steps = by_id(plan)
    assert steps["transcription"]["status"] == "ready"
    assert steps["ocr"]["status"] == "ready"
    assert steps["semantic_candidates"]["status"] == "optional"
    assert steps["lexical_candidates"]["status"] == "optional"


def test_ui_plan_never_echoes_connection_secrets_or_urls():
    secret = "fixture-secret-do-not-display"
    conns = connection("text_generation", "remote", api_key=secret, endpoint="https://private.example/path")
    plan = plan_workflow({"has_transcript": True, "requested_ai": ["grounded_synthesis"]}, conns)
    encoded = json.dumps(plan, ensure_ascii=False)
    assert secret not in encoded and "private.example" not in encoded and "api_key" not in encoded
    assert "chosen-model" in encoded
    assert json.loads(encoded) == plan


@pytest.mark.parametrize("inputs", [
    {"has_audio": 1}, {"has_transcript": "true"}, {"unknown": True},
    {"requested_ai": ["file_import"]}, {"requested_ai": ["transcription", "transcription"]},
    {"requested_ai": ["invented"]}, {"requested_ai": "transcription"},
])
def test_bad_input_contracts_are_rejected(inputs):
    with pytest.raises(ValueError):
        plan_workflow(inputs)


@pytest.mark.parametrize("connections", [
    {"none": {}}, {"speech_to_text": "local"},
    {"text_generation": {"id": "chosen", "location": "guess", "capabilities": ["text_generation"]}},
    {"text_generation": {"id": "chosen", "location": ["local"], "capabilities": ["text_generation"]}},
    {"text_generation": {"id": "chosen", "location": "local", "capabilities": ["transcription"]}},
])
def test_invalid_connection_metadata_cannot_turn_into_ai_readiness(connections):
    with pytest.raises(ValueError):
        plan_workflow({}, connections)
