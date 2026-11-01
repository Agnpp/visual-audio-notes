import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import urllib.error

import pytest

from visual_audio_notes.provider_discovery import SCOPE, list_connection_models
from visual_audio_notes.providers import ModelConnection, ProviderError


KEY = "test-only-fake-model-discovery-credential"


@pytest.fixture
def servers(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", KEY)
    created = []

    def start(body=None, status=200, redirect=None):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requests.append({"path": self.path, "authorization": self.headers.get("Authorization")})
                self.send_response(status)
                if redirect:
                    self.send_header("Location", redirect)
                self.end_headers()
                value = {"data": [{"id": "deepseek-flash"}]} if body is None else body
                self.wfile.write(value if isinstance(value, bytes) else json.dumps(value).encode("utf-8"))

        http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=lambda: http.serve_forever(poll_interval=.01), daemon=True)
        thread.start()
        created.append((http, thread))
        return f"http://127.0.0.1:{http.server_port}", requests

    yield start
    for http, thread in created:
        http.shutdown()
        http.server_close()
        thread.join()


@pytest.mark.parametrize("provider,suffix,expected", [
    ("deepseek", "", "/models"),
    ("deepseek", "/v1", "/v1/models"),
    ("deepseek", "/chat/completions", "/models"),
    ("deepseek", "/v1/chat/completions", "/v1/models"),
    ("deepseek", "/v1/models", "/v1/models"),
    ("local_llamacpp", "", "/v1/models"),
    ("local_llamacpp", "/v1", "/v1/models"),
    ("local_llamacpp", "/chat/completions", "/v1/models"),
    ("local_llamacpp", "/v1/chat/completions", "/v1/models"),
    ("local_llamacpp", "/v1/models", "/v1/models"),
    ("deepseek", "/custom-prefix/v1/chat/completions/", "/custom-prefix/v1/models"),
    ("local_llamacpp", "/custom-prefix", "/custom-prefix/v1/models"),
])
def test_discovery_normalizes_endpoint_and_auth_only_for_selected_provider(servers, provider, suffix, expected):
    endpoint, requests = servers({"object": "list", "data": [
        {"id": "custom-model", "owned_by": "ignored", "description": "Do not show this"}]
    })
    connection = ModelConnection(provider, endpoint=endpoint + suffix)
    assert list_connection_models(connection) == {
        "models": [{"id": "custom-model", "label": "custom-model"}], "status": "available", "scope": SCOPE}
    assert requests == [{"path": expected, "authorization": "Bearer " + KEY if provider == "deepseek" else None}]


def test_only_usable_unique_ids_are_exposed_without_capability_guesses(servers, tmp_path, monkeypatch):
    endpoint, requests = servers({"data": [
        {"id": "deepseek-flash", "owned_by": KEY}, {"id": "deepseek-flash"},
        {"id": "vendor/模型文件.gguf:latest"}, {"id": "local model alias"},
        {"id": "prefix-" + KEY + "-suffix"}, {"id": ""}, {"id": " "},
        {"id": "with\nnewline"}, {"id": "\u202ehidden"}, {"id": "x" * 201},
        {"id": " trailing "}, {"id": 7}, {"name": "not-an-id"}, None, "not-an-object"]})
    monkeypatch.chdir(tmp_path)
    result = list_connection_models(ModelConnection("deepseek", endpoint=endpoint))
    assert result["models"] == [
        {"id": "deepseek-flash", "label": "deepseek-flash"},
        {"id": "vendor/模型文件.gguf:latest", "label": "vendor/模型文件.gguf:latest"},
        {"id": "local model alias", "label": "local model alias"}]
    assert KEY not in json.dumps(result)
    assert "capabilities" not in result and "quality" not in result
    assert not list(tmp_path.iterdir()) and len(requests) == 1


@pytest.mark.parametrize("data,reason", [([], "empty_model_list"), ([{"id": ""}], "no_usable_model_ids"), ([{"id": KEY}], "no_usable_model_ids")])
def test_empty_or_secret_only_model_list_is_explicitly_unavailable(servers, data, reason):
    endpoint, _ = servers({"data": data})
    assert list_connection_models(ModelConnection("deepseek", endpoint=endpoint)) == {
        "models": [], "status": "unavailable", "scope": SCOPE, "reason": reason}


@pytest.mark.parametrize("body", [b"not JSON " + KEY.encode(), b' {"data":[],"data":[]} ',
                                  [], {}, {"data": None}, {"models": [{"name": "not-standard-envelope"}]}])
def test_malformed_response_has_no_fallback_and_no_body_in_error(servers, body):
    endpoint, requests = servers(body)
    with pytest.raises(ProviderError, match="invalid model list") as caught:
        list_connection_models(ModelConnection("deepseek", endpoint=endpoint))
    assert KEY not in str(caught.value) and len(requests) == 1


def test_http_error_ignores_body_and_does_not_retry(servers):
    endpoint, requests = servers({"error": KEY}, status=401)
    with pytest.raises(ProviderError) as caught:
        list_connection_models(ModelConnection("deepseek", endpoint=endpoint))
    assert str(caught.value) == "Model discovery HTTP error (401); no automatic retry or fallback"
    assert KEY not in str(caught.value) and KEY not in repr(caught.value)
    assert len(requests) == 1


def test_redirect_does_not_forward_bearer_or_discover_a_different_server(servers):
    other, other_requests = servers()
    endpoint, requests = servers(status=302, redirect=other + "/steal")
    with pytest.raises(ProviderError, match="302"):
        list_connection_models(ModelConnection("deepseek", endpoint=endpoint))
    assert len(requests) == 1 and not other_requests


def test_missing_credential_prevents_remote_request(servers, monkeypatch):
    endpoint, requests = servers()
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    with pytest.raises(ProviderError, match="Credential is missing"):
        list_connection_models(ModelConnection("deepseek", endpoint=endpoint))
    assert not requests


def test_local_discovery_does_not_require_or_send_cloud_credential(servers, monkeypatch):
    endpoint, requests = servers({"data": [{"id": "user-whisper-named-llm"}]})
    monkeypatch.setenv("DEEPSEEK_API_KEY", "invalid key value ignored for local")
    result = list_connection_models(ModelConnection("local_llamacpp", endpoint=endpoint))
    assert result["scope"] == SCOPE
    assert result["models"][0]["id"] == "user-whisper-named-llm"
    assert requests[0]["authorization"] is None
    assert "speech_to_text" not in json.dumps(result)


def test_local_model_id_cannot_echo_the_configured_credential(servers):
    endpoint, requests = servers({"data": [{"id": KEY}, {"id": "safe-local-model"}]})
    result = list_connection_models(ModelConnection("local_llamacpp", endpoint=endpoint))
    assert result["models"] == [{"id": "safe-local-model", "label": "safe-local-model"}]
    assert requests[0]["authorization"] is None
    assert KEY not in json.dumps(result)


def test_connection_error_is_safe_and_honors_timeout(monkeypatch):
    seen = []

    class FailedOpener:
        def open(self, request, timeout):
            seen.append(timeout)
            raise urllib.error.URLError("unsafe server detail " + KEY)

    monkeypatch.setenv("DEEPSEEK_API_KEY", KEY)
    monkeypatch.setattr("visual_audio_notes.provider_discovery.urllib.request.build_opener", lambda *_: FailedOpener())
    with pytest.raises(ProviderError) as caught:
        list_connection_models(ModelConnection("deepseek", timeout_seconds=13))
    assert str(caught.value) == "Model discovery connection failed; no automatic retry or fallback"
    assert KEY not in str(caught.value) and seen == [13]


def test_invalid_connection_object_rejected_without_network():
    with pytest.raises(ProviderError, match="requires a model connection"):
        list_connection_models({"provider": "deepseek"})
