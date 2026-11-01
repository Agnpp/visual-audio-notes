import json

import pytest

from visual_audio_notes.model_selection import build_model_selector, deepseek_thinking_settings


MODELS = [{"id": "actual-fast", "label": "连接返回的快模型"}, {"id": "actual-large"}]
LEVELS = ["off", "low", "high", "max"]


def test_model_menu_uses_only_actual_connection_models_and_off_default():
    result = build_model_selector("connection-a", MODELS, LEVELS)
    assert [m["id"] for m in result["options"]] == ["actual-fast", "actual-large"]
    assert result["selected"] == {"model": "actual-fast", "effort": "off"}
    assert result["compact_label"] == "连接返回的快模型 · 关闭思考 ▾"
    assert result["reasoning_controls"]["adjustable"]
    assert result["selection_source"] == {"model": "default", "effort": "default"}
    assert "deepseek" not in json.dumps(result)


@pytest.mark.parametrize("effort", LEVELS)
def test_explicit_model_and_effort_are_preserved_without_default_overwrite(effort):
    saved = {"model": "actual-large", "effort": effort, "connection_id": "connection-a"}
    result = build_model_selector("connection-a", MODELS, LEVELS, saved, default_model="actual-fast")
    assert result["selected"] == {"model": "actual-large", "effort": effort}
    assert result["selection_source"] == {"model": "explicit", "effort": "explicit"}
    assert saved == {"model": "actual-large", "effort": effort, "connection_id": "connection-a"}


def test_defaults_apply_to_only_unset_fields_and_declared_capabilities():
    result = build_model_selector("c", ["small", "big"], {"small": ["off"], "big": ["low", "high"]},
                                  {"model": "big"})
    assert result["selected"] == {"model": "big", "effort": "high"}
    fixed = build_model_selector("c", ["small", "big"], {"small": ["off"]})
    assert fixed["selected"]["effort"] == "off"
    assert fixed["reasoning_controls"]["supported"]
    assert not fixed["reasoning_controls"]["adjustable"]
    assert fixed["reasoning_controls"]["options"] == [{"id": "off", "label": "关闭思考", "selected": True}]


def test_local_or_unknown_provider_without_declaration_does_not_fake_thinking_support():
    for declaration in (None, [], {}):
        result = build_model_selector("local-llamacpp", ["my-server-model"], declaration)
        assert result["selected"] == {"model": "my-server-model", "effort": None}
        assert not result["reasoning_controls"]["visible"]
        assert result["reasoning_controls"]["options"] == []
        assert "由服务端配置" in result["compact_label"]
        assert "thinking" not in result and "reasoning_effort" not in result
    with pytest.raises(ValueError, match="not supported"):
        build_model_selector("local-llamacpp", ["my-server-model"], None, {"effort": "high"})


def test_unsupported_model_or_level_rejected_instead_of_silent_fallback():
    with pytest.raises(ValueError, match="not available"):
        build_model_selector("c", MODELS, LEVELS, {"model": "not-in-model-list"})
    with pytest.raises(ValueError, match="not supported"):
        build_model_selector("c", MODELS, ["low"], {"effort": "high"})
    with pytest.raises(ValueError, match="different connection"):
        build_model_selector("new-c", MODELS, LEVELS, {"connection_id": "old-c"})
    with pytest.raises(ValueError, match="Default model"):
        build_model_selector("c", MODELS, LEVELS, default_model="not-returned")


def test_no_model_list_shows_connection_action_not_invented_default():
    result = build_model_selector("c", [], LEVELS)
    assert result["status"] == "model_list_empty" and result["enabled"] is False
    assert result["selected"] == {"model": None, "effort": None}
    assert result["compact_label"] == "选择模型 ▾"
    with pytest.raises(ValueError, match="not available"):
        build_model_selector("c", [], LEVELS, {"model": "saved-but-not-present"})
    with pytest.raises(ValueError, match="requires an available"):
        build_model_selector("c", [], LEVELS, {"effort": "high"})


def test_public_object_does_not_copy_private_connection_metadata():
    source = [{"id": "actual-model", "object": "model", "owned_by": "provider",
               "api_key": "must-not-leak", "endpoint": "https://unused.invalid/private"}]
    result = build_model_selector("public-connection", source, LEVELS)
    public = json.dumps(result)
    assert "must-not-leak" not in public and "unused.invalid" not in public
    assert result["options"][0] == {"id": "actual-model", "label": "actual-model", "selected": True}
    with pytest.raises(ValueError, match="public model and effort"):
        build_model_selector("c", MODELS, LEVELS, {"api_key": "not-allowed"})


@pytest.mark.parametrize("invalid_models", [["same", "same"], [{"id": ""}], [123], ["bad\nname"]])
def test_bad_connection_model_list_is_not_rendered(invalid_models):
    with pytest.raises(ValueError):
        build_model_selector("c", invalid_models, None)


@pytest.mark.parametrize("invalid_levels", [["medium"], ["high", "high"], [None], "high"])
def test_bad_reasoning_declaration_is_rejected(invalid_levels):
    with pytest.raises(ValueError):
        build_model_selector("c", MODELS, invalid_levels)


@pytest.mark.parametrize("effort", ["low", "high", "max"])
def test_deepseek_enabled_modes_have_explicit_effort_and_omit_temperature(effort):
    assert deepseek_thinking_settings(effort, temperature=0.2) == {
        "thinking": {"type": "enabled"}, "reasoning_effort": effort}


def test_deepseek_off_has_no_effort_and_can_use_temperature():
    assert deepseek_thinking_settings("off") == {"thinking": {"type": "disabled"}}
    assert deepseek_thinking_settings("off", temperature=0.2) == {
        "thinking": {"type": "disabled"}, "temperature": 0.2}


@pytest.mark.parametrize("bad", [True, float("nan"), -1, 3, "0.2"])
def test_invalid_deepseek_temperature_does_not_reach_request(bad):
    with pytest.raises(ValueError):
        deepseek_thinking_settings("off", temperature=bad)
