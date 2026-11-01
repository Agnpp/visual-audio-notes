"""Public model/thinking selector contract, independent of GUI and credentials.

The caller supplies models actually returned by its selected connection and the
reasoning levels explicitly implemented by that connection's adapter. This
module neither fetches a model list nor assumes local and remote APIs share a
thinking protocol. Selecting a model is not a content-quality certificate.
"""
from __future__ import annotations

import math


EFFORTS = ("off", "low", "high", "max")
EFFORT_LABELS = {"off": "关闭思考", "low": "低", "high": "高", "max": "最高"}


def _public_text(value: object, label: str, limit: int = 200) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(c) < 32 for c in value)):
        raise ValueError("Invalid selector " + label)
    return value.strip()


def _models(values: object) -> list[dict]:
    if not isinstance(values, list):
        raise ValueError("Available models must be a list from the selected connection")
    result, seen = [], set()
    for item in values:
        if isinstance(item, str):
            identifier = _public_text(item, "model ID")
            label = identifier
        elif isinstance(item, dict):
            identifier = _public_text(item.get("id"), "model ID")
            label = _public_text(item.get("label", item.get("display_name", identifier)), "model label")
        else:
            raise ValueError("Malformed available-model entry")
        if identifier in seen:
            raise ValueError("Duplicate available model ID")
        seen.add(identifier)
        # Deliberately do not copy unknown metadata: a model-list response may
        # carry endpoint or credential fields which are not public UI content.
        result.append({"id": identifier, "label": label})
    return result


def _efforts(supported: object, model: str) -> list[str]:
    values = supported.get(model, []) if isinstance(supported, dict) else supported
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise ValueError("Supported thinking levels must be explicitly declared as a list")
    if (any(not isinstance(v, str) or v not in EFFORTS for v in values)
            or len(values) != len(set(values))):
        raise ValueError("Unknown or duplicate supported thinking level")
    # A canonical order in the menu is presentation only. It does not confer a
    # capability on models which the caller did not explicitly declare.
    return [v for v in EFFORTS if v in values]


def build_model_selector(connection_id: str, available_models: list,
                         supported_efforts: list | dict | None,
                         selection: dict | None = None, *, default_model: str | None = None) -> dict:
    """Build a compact public selector and validate an optional saved choice.

    ``selection`` accepts ``model`` and ``effort`` (plus an optional connection_id
    for stale-connection detection). Model IDs are never invented. When unset,
    the model defaults to default_model or the first actual model, and effort to
    off if supported, then high, otherwise the first declared level. An empty declaration
    means thinking is controlled by the server, with no selectable fake levels.
    """
    connection_id = _public_text(connection_id, "connection ID")
    models = _models(available_models)
    if selection is None:
        selection = {}
    if not isinstance(selection, dict) or set(selection) - {"model", "effort", "connection_id"}:
        raise ValueError("Selection accepts public model and effort fields only")
    if selection.get("connection_id") not in (None, connection_id):
        raise ValueError("Selection belongs to a different connection")
    if supported_efforts is not None and not isinstance(supported_efforts, (list, tuple, dict)):
        raise ValueError("Unsupported thinking capability declaration")
    explicit_model = selection.get("model") is not None
    explicit_effort = selection.get("effort") is not None
    ids = {m["id"] for m in models}
    if explicit_model:
        model = _public_text(selection["model"], "selected model")
        if model not in ids:
            raise ValueError("Selected model is not available on this connection")
    elif models:
        model = _public_text(default_model, "default model") if default_model is not None else models[0]["id"]
        if model not in ids:
            raise ValueError("Default model is not available on this connection")
    else:
        if explicit_effort:
            raise ValueError("A thinking level requires an available selected model")
        return {"connection_id": connection_id, "status": "model_list_empty", "enabled": False,
                "compact_label": "选择模型 ▾", "options": [],
                "selected": {"model": None, "effort": None},
                "reasoning_controls": {"supported": False, "visible": False, "adjustable": False,
                                       "options": [], "selected": None,
                                       "notice": "尚未获取此连接的可用模型，请先测试连接。"},
                "selection_source": {"model": "unset", "effort": "unset"}}
    levels = _efforts(supported_efforts, model)
    if explicit_effort:
        effort = selection["effort"]
        if not isinstance(effort, str) or effort not in levels:
            raise ValueError("Selected thinking level is not supported by this model connection")
    else:
        effort = ("off" if "off" in levels else
                  ("high" if "high" in levels else (levels[0] if levels else None)))
    label = next(m["label"] for m in models if m["id"] == model)
    effort_label = EFFORT_LABELS[effort] if effort is not None else "由服务端配置"
    return {
        "connection_id": connection_id, "status": "ready", "enabled": True,
        "compact_label": f"{label} · {effort_label} ▾",
        "options": [{**m, "selected": m["id"] == model} for m in models],
        "selected": {"model": model, "effort": effort},
        "reasoning_controls": {
            "supported": bool(levels), "visible": bool(levels), "adjustable": len(levels) > 1,
            "options": [{"id": v, "label": EFFORT_LABELS[v], "selected": v == effort} for v in levels],
            "selected": effort,
            "notice": ("思考等级由此连接的适配器明确支持。" if levels else
                       "此连接未声明可调思考等级，由本地服务或提供方配置。"),
        },
        "selection_source": {"model": "explicit" if explicit_model else "default",
                             "effort": "explicit" if explicit_effort else ("default" if levels else "server")},
    }


def deepseek_thinking_settings(effort: str, *, temperature: float | None = None) -> dict:
    """Map an explicitly selected DeepSeek level, not a universal provider API.

    Off sends no reasoning_effort. Thinking modes omit temperature because that
    field does not control sampling in the documented DeepSeek thinking mode.
    No credentials, model choice, or network request is handled here.
    """
    if not isinstance(effort, str) or effort not in EFFORTS:
        raise ValueError("Unknown DeepSeek thinking level")
    if temperature is not None and (isinstance(temperature, bool)
            or not isinstance(temperature, (int, float)) or not math.isfinite(temperature)
            or not 0 <= temperature <= 2):
        raise ValueError("Invalid DeepSeek temperature")
    if effort == "off":
        result = {"thinking": {"type": "disabled"}}
        if temperature is not None:
            result["temperature"] = temperature
        return result
    return {"thinking": {"type": "enabled"}, "reasoning_effort": effort}
