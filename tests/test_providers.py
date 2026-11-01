import json
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from visual_audio_notes.notes import LocalModel
from visual_audio_notes.providers import (DeepSeekJsonModel, JsonModel, ModelConnection,
                                          ProviderError, create_json_model)


KEY = "test-not-a-real-credential-7d31"
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["choice"],
          "properties": {"choice": {"type": "string", "enum": ["allowed"]}}}


def envelope(content='{"choice":"allowed"}', finish="stop"):
    return {"id": "test-response-1", "model": "deepseek-flash-test-revision", "choices": [
        {"finish_reason": finish, "message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", KEY)
    resources = []

    def start(body=None, status=200, redirect=None):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                wire = self.rfile.read(int(self.headers["Content-Length"]))
                received.append({"path": self.path, "authorization": self.headers.get("Authorization"),
                                 "body": json.loads(wire), "wire": wire})
                self.send_response(status)
                if redirect:
                    self.send_header("Location", redirect)
                self.end_headers()
                data = envelope() if body is None else body
                self.wfile.write(data if isinstance(data, bytes) else json.dumps(data).encode())

        http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        resources.append((http, thread))
        return f"http://127.0.0.1:{http.server_port}", received

    yield start
    for http, thread in resources:
        http.shutdown()
        http.server_close()
        thread.join()


def client(endpoint):
    return create_json_model(ModelConnection("deepseek", endpoint=endpoint))


def test_actual_deepseek_json_mode_payload_and_local_schema_validation(server, tmp_path, monkeypatch):
    endpoint, requests = server()
    monkeypatch.chdir(tmp_path)
    model = client(endpoint)
    assert isinstance(model, JsonModel)
    before = dict(model.cache_identity)
    assert model.json("从原文选择证据", "private transcript never logged", schema=SCHEMA) == {"choice": "allowed"}
    request = requests[0]
    assert request["path"] == "/chat/completions"
    assert request["authorization"] == "Bearer " + KEY
    payload = request["body"]
    assert payload["model"] == "deepseek-flash"
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["temperature"] == 0
    assert "reasoning_effort" not in payload
    assert "seed" not in payload
    assert "JSON structure guide only" in payload["messages"][0]["content"]
    assert "Example JSON shape" in payload["messages"][0]["content"]
    assert "allowed" not in payload["messages"][0]["content"]
    schema_suffix = payload["messages"][1]["content"].split("all constraints apply):\n", 1)[1]
    assert json.loads(schema_suffix) == SCHEMA
    assert model.calls[0]["actual_model"] == "deepseek-flash-test-revision"
    assert model.calls[0]["response_id"] == "test-response-1"
    assert model.calls[0]["usage"]["total_tokens"] == 15
    assert model.calls[0]["status"] == "validated"
    assert model.calls[0]["system_prefix_sha256"] == hashlib.sha256(
        payload["messages"][0]["content"].encode("utf-8")).hexdigest()
    assert model.observed_models == {"deepseek-flash-test-revision"}
    assert model.cache_identity == before
    assert not list(tmp_path.iterdir())
    records = json.dumps(model.calls)
    assert "private transcript" not in records and KEY not in records
    assert "private transcript" not in repr(model) and KEY not in repr(model)
    assert model.capabilities == {"text_generation", "json_object"}


@pytest.mark.parametrize("body", [
    envelope("   "), envelope('{"choice":"allowed"}', "length"),
    envelope('{"choice":"allowed"}', "content_filter"),
    envelope("not json"), envelope("[]"), envelope('{"choice":"not_allowed"}'),
    envelope('{"choice":"allowed","unexpected":true}'), envelope('{"choice":"allowed","choice":"allowed"}'),
    envelope('{"choice":NaN}'), {"choices": []}, {"choices": None}, b"not-json-envelope",
])
def test_bad_output_never_passes_or_becomes_successful_cache(server, body):
    endpoint, requests = server(body)
    model = client(endpoint)
    with pytest.raises(ProviderError):
        model.json("Return JSON", "source", schema=SCHEMA)
    assert len(requests) == 1
    assert model.calls[-1]["status"] == "invalid_response"


@pytest.mark.parametrize("choices", ["missing", None, [], ["invalid-choice"], [{}, {}]])
def test_invalid_choices_preserve_known_billable_usage_and_safe_metadata(server, choices):
    body = envelope()
    body["usage"].update(prompt_cache_hit_tokens=8, prompt_cache_miss_tokens=2)
    body["model"] = "revision-" + KEY
    body["id"] = "response-" + KEY
    if choices == "missing":
        body.pop("choices")
    else:
        body["choices"] = choices
    endpoint, requests = server(body)
    model = client(endpoint)
    with pytest.raises(ProviderError, match="invalid JSON response"):
        model.json("JSON", "source", schema=SCHEMA)
    assert len(requests) == 1
    call = model.calls[-1]
    assert call["status"] == "invalid_response"
    assert call["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                             "prompt_cache_hit_tokens": 8, "prompt_cache_miss_tokens": 2}
    assert call["actual_model"] == "revision-[redacted]"
    assert call["response_id"] == "response-[redacted]"
    assert KEY not in json.dumps(model.calls)


def test_schema_format_pattern_ranges_and_local_ref_are_enforced(server):
    schema = {"type": "object", "$defs": {"code": {"type": "string", "pattern": "^A[0-9]+$"}},
              "properties": {"code": {"$ref": "#/$defs/code"}, "n": {"type": "integer", "minimum": 2}},
              "required": ["code", "n"], "additionalProperties": False}
    endpoint, _ = server(envelope('{"code":"B7","n":1}'))
    with pytest.raises(ProviderError, match="local schema validation"):
        client(endpoint).json("JSON", "source", schema=schema)


def test_external_schema_reference_cannot_fetch_resources(server):
    endpoint, requests = server(envelope('{"choice":"allowed"}'))
    schema = {"$ref": endpoint + "/malicious-schema"}
    with pytest.raises(ProviderError, match="local schema validation"):
        client(endpoint).json("JSON", "source", schema=schema)
    assert len(requests) == 1 and requests[0]["path"] == "/chat/completions"


def test_http_error_cannot_leak_server_body_or_credential(server):
    endpoint, _ = server({"error": "Server echoes " + KEY}, status=401)
    model = client(endpoint)
    with pytest.raises(ProviderError) as caught:
        model.json("JSON", "source", schema=SCHEMA)
    assert str(caught.value) == "Provider HTTP error (401); no automatic retry or fallback"
    assert KEY not in str(caught.value) and KEY not in repr(caught.value)
    assert KEY not in json.dumps(model.calls)
    assert model.calls[0]["http_status"] == 401


def test_redirect_does_not_forward_authorization_or_follow(server):
    other, other_requests = server()
    endpoint, requests = server(status=307, redirect=other + "/steal")
    with pytest.raises(ProviderError, match="307"):
        client(endpoint).json("JSON", "source")
    assert len(requests) == 1
    assert not other_requests


def test_no_secret_in_public_config_or_malicious_metadata(server):
    payload = envelope()
    payload["model"] = KEY
    payload["id"] = "prefix " + KEY
    endpoint, _ = server(payload)
    connection = ModelConnection("deepseek", endpoint=endpoint)
    model = create_json_model(connection)
    model.json("JSON", "source", purpose=KEY)
    assert KEY not in json.dumps(connection.public_dict())
    assert KEY not in repr(connection)
    assert KEY not in json.dumps(model.calls)
    assert KEY not in json.dumps(model.cache_identity)


def test_missing_key_does_not_send_request(server, monkeypatch):
    endpoint, requests = server()
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    model = client(endpoint)
    with pytest.raises(ProviderError, match="Credential is missing"):
        model.json("JSON", "source")
    assert not requests and not model.calls


@pytest.mark.parametrize("endpoint", ["http://remote.invalid", "https://user:password@example.com", "https://example.com?key=secret", "https://example.com/#fragment", "https://example.com:invalid"])
def test_bad_remote_endpoint_is_rejected_without_echoing_it(endpoint):
    with pytest.raises(ProviderError) as caught:
        ModelConnection("deepseek", endpoint=endpoint)
    assert endpoint not in str(caught.value)


def test_explicit_local_selection_reuses_existing_client_without_cloud_env(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    local = create_json_model({"provider": "local_llamacpp", "model": "user-local-model",
                               "endpoint": "http://127.0.0.1:38802", "fingerprint": "test-model-hash"})
    assert isinstance(local, LocalModel)
    assert local.cache_identity["provider"] == "local_llamacpp"
    assert local.cache_identity["endpoint"] == "http://127.0.0.1:38802"
    assert local.cache_identity["fingerprint"] == "test-model-hash"
    with pytest.raises(ProviderError, match="loopback"):
        ModelConnection("local_llamacpp", endpoint="https://api.deepseek.com")


def test_unknown_provider_does_not_fallback():
    with pytest.raises(ProviderError, match="Unknown model provider"):
        create_json_model({"provider": "automatic", "model": "whatever"})
    with pytest.raises(ProviderError, match="configuration fields"):
        create_json_model({"provider": "deepseek", "api_key": "never-store-key"})


def test_thinking_mode_and_endpoint_v1_keep_explicit_request_settings(server):
    endpoint, requests = server()
    connection = ModelConnection("deepseek", endpoint=endpoint + "/v1", thinking="enabled")
    model = create_json_model(connection)
    model.json("JSON", "source")
    assert requests[0]["path"] == "/v1/chat/completions"
    assert requests[0]["body"]["thinking"] == {"type": "enabled"}
    assert requests[0]["body"]["reasoning_effort"] == "high"
    assert "temperature" not in requests[0]["body"]


def test_explicit_non_thinking_mode_keeps_temperature(server):
    endpoint, requests = server()
    model = create_json_model(ModelConnection("deepseek", endpoint=endpoint, thinking="disabled"))
    model.json("JSON", "source")
    assert requests[0]["body"]["thinking"] == {"type": "disabled"}
    assert requests[0]["body"]["temperature"] == 0
    assert "reasoning_effort" not in requests[0]["body"]


def test_invalid_schema_is_detected_before_any_request(server):
    endpoint, requests = server()
    with pytest.raises(ProviderError, match="Invalid output JSON Schema"):
        client(endpoint).json("JSON", "source", schema={"type": "not-a-type"})
    assert not requests


def test_batch_values_limits_and_annotations_do_not_change_common_system_prefix(server):
    endpoint, requests = server()
    model = client(endpoint)

    def batch_schema(source_id, count):
        return {"type": "object", "additionalProperties": False, "required": ["choice"],
                "properties": {
                    "choice": {"type": "string", "enum": ["allowed", source_id]},
                    "batch": {"type": "object", "const": {"source_id": source_id}},
                    "ids": {"type": "array", "minItems": count, "maxItems": count,
                            "items": {"type": "string", "enum": [source_id]},
                            "description": source_id, "examples": [[source_id]], "default": [source_id]},
                }}

    schemas = [batch_schema("PRIVATE_SOURCE_A", 1), batch_schema("PRIVATE_SOURCE_B", 5)]
    for schema in schemas:
        assert model.json("Choose only supported identifiers.", "task evidence", schema=schema) == {"choice": "allowed"}
    first, second = [r["body"]["messages"] for r in requests]
    assert first[0] == second[0]
    assert "PRIVATE_SOURCE" not in first[0]["content"]
    assert model.calls[0]["system_prefix_sha256"] == model.calls[1]["system_prefix_sha256"]
    assert model.calls[0]["request_sha256"] != model.calls[1]["request_sha256"]
    for messages, schema in zip((first, second), schemas):
        assert messages[1]["content"].startswith("task evidence\n\nAuthoritative JSON Schema")
        assert json.loads(messages[1]["content"].split("all constraints apply):\n", 1)[1]) == schema
    assert all(c["transport_version"] == model.cache_identity["transport_version"] for c in model.calls)
    assert "PRIVATE_SOURCE" not in json.dumps(model.calls)


def test_semantically_identical_dictionary_order_has_identical_request_text_and_hash(server):
    endpoint, requests = server()
    model = client(endpoint)
    reordered = {"properties": {"choice": {"enum": ["allowed"], "type": "string"}},
                 "required": ["choice"], "additionalProperties": False, "type": "object"}
    model.json("JSON", "same evidence", schema=SCHEMA)
    model.json("JSON", "same evidence", schema=reordered)
    assert requests[0]["body"] == requests[1]["body"]
    assert requests[0]["wire"] == requests[1]["wire"]
    assert model.calls[0]["request_sha256"] == model.calls[1]["request_sha256"]
    assert model.calls[0]["system_prefix_sha256"] == model.calls[1]["system_prefix_sha256"]


@pytest.mark.parametrize("field,value", [("source", "OTHER_SOURCE"), ("spans", ["OTHER_SPAN"]),
                                         ("spans", ["SPAN_A", "SPAN_A"])])
def test_dynamic_constraints_in_task_suffix_are_still_enforced_locally(server, field, value):
    schema = {"type": "object", "additionalProperties": False, "required": ["source", "spans"],
              "properties": {"source": {"type": "string", "const": "SOURCE_A"},
                             "spans": {"type": "array", "minItems": 1, "maxItems": 1, "uniqueItems": True,
                                       "items": {"type": "string", "enum": ["SPAN_A"]}}}}
    response = {"source": "SOURCE_A", "spans": ["SPAN_A"], field: value}
    endpoint, requests = server(envelope(json.dumps(response)))
    model = client(endpoint)
    with pytest.raises(ProviderError, match="local schema validation"):
        model.json("Respect source boundaries.", "source evidence", schema=schema)
    assert len(requests) == 1
    assert "SOURCE_A" not in requests[0]["body"]["messages"][0]["content"]
    assert "SPAN_A" not in requests[0]["body"]["messages"][0]["content"]
    assert model.calls[-1]["status"] == "invalid_response"


def test_schema_property_named_enum_or_const_keeps_its_structure(server):
    schema = {"type": "object", "required": ["enum", "const"], "additionalProperties": False,
              "properties": {"enum": {"type": "string", "enum": ["source_value"]},
                             "const": {"type": "boolean"}}}
    endpoint, requests = server(envelope('{"enum":"source_value","const":true}'))
    assert client(endpoint).json("JSON", "source", schema=schema) == {"enum": "source_value", "const": True}
    prefix = requests[0]["body"]["messages"][0]["content"]
    assert '"enum":{"type":"string"}' in prefix
    assert '"const":{"type":"boolean"}' in prefix
    assert "source_value" not in prefix


def test_boolean_property_schema_can_be_shown_without_bypassing_full_validation(server):
    schema = {"type": "object", "required": ["choice"], "properties": {"choice": True, "blocked": False}}
    endpoint, _ = server()
    assert client(endpoint).json("JSON", "source", schema=schema) == {"choice": "allowed"}
