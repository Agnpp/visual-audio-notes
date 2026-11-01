"""List the selected connection's advertised model IDs without guessing abilities.

Discovery is read-only: it does not switch provider, run inference, or turn a
server's list of names into a capability or quality certification.
"""
from __future__ import annotations

import os
import urllib.error
import urllib.parse
import urllib.request

from .providers import ModelConnection, ProviderError, _NoRedirect, _strict_json


SCOPE = "available_models_not_capability_or_quality_validation"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def _models_endpoint(connection: ModelConnection) -> str:
    parsed = urllib.parse.urlsplit(connection.endpoint)
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[:-len("/chat/completions")]
    if path.endswith("/models"):
        pass
    elif connection.provider == "local_llamacpp" and not path.endswith("/v1"):
        path += "/v1/models"
    else:
        path += "/models"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _model_id(value: object, key: str) -> str | None:
    # Names are data, never instructions or inferred abilities. Preserve valid
    # local aliases, including Unicode and internal spaces, without rewriting ID.
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > 200 or not value.isprintable() or (key and key in value)):
        return None
    return value


def list_connection_models(connection: ModelConnection) -> dict:
    """Fetch exactly one configured /models endpoint; never retry or fall back.

    Standard DeepSeek and llama.cpp responses use ``{"data": [{"id": ...}]}``.
    Empty or entirely unusable lists are explicitly unavailable. Malformed
    envelopes, HTTP failures, and connection failures raise a sanitized error.
    """
    if not isinstance(connection, ModelConnection):
        raise ProviderError("Model discovery requires a model connection")
    headers = {"Accept": "application/json"}
    # For a local service this value is used only to suppress an accidental key
    # echo in a model ID; it is neither required, validated, nor transmitted.
    key = os.environ.get(connection.api_key_env, "")
    if connection.provider == "deepseek":
        if not key or not key.strip():
            raise ProviderError("Credential is missing from the configured environment variable")
        if key != key.strip() or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ProviderError("Credential environment value contains invalid whitespace")
        headers["Authorization"] = "Bearer " + key
    request = urllib.request.Request(_models_endpoint(connection), headers=headers, method="GET")
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=connection.timeout_seconds) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderError("Model list response exceeds the supported size")
        try:
            data = _strict_json(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise ProviderError("Provider returned an invalid model list") from None
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise ProviderError("Provider returned an invalid model list")
        models = []
        seen = set()
        for item in data["data"]:
            identifier = _model_id(item.get("id") if isinstance(item, dict) else None, key)
            if identifier is not None and identifier not in seen:
                models.append({"id": identifier, "label": identifier})
                seen.add(identifier)
        result = {"models": models, "status": "available" if models else "unavailable", "scope": SCOPE}
        if not models:
            result["reason"] = "empty_model_list" if not data["data"] else "no_usable_model_ids"
        return result
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise ProviderError(f"Model discovery HTTP error ({code}); no automatic retry or fallback") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ProviderError("Model discovery connection failed; no automatic retry or fallback") from None
