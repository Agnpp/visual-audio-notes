import pytest

from test_providers import server
from visual_audio_notes.providers import ModelConnection, ProviderError, create_json_model


@pytest.mark.parametrize("effort", ["low", "high", "max"])
def test_explicit_effort_reaches_request_and_cache_without_temperature(server, monkeypatch, effort):
    endpoint, requests = server({"choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}]})
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    model = create_json_model(ModelConnection("deepseek", endpoint=endpoint,
                                             thinking="enabled", reasoning_effort=effort))
    assert model.json("Return JSON", "connection probe") == {"ok": True}
    assert requests[0]["body"]["reasoning_effort"] == effort
    assert "temperature" not in requests[0]["body"]
    assert model.cache_identity["reasoning_effort"] == effort


def test_invalid_effort_is_not_silently_mapped():
    with pytest.raises(ProviderError):
        ModelConnection("deepseek", reasoning_effort="ultra")


def test_local_public_settings_do_not_claim_thinking_support():
    public = ModelConnection("local_llamacpp").public_dict()
    assert "thinking" not in public and "reasoning_effort" not in public
    assert public["reasoning_control"] == "configured_by_local_service"
