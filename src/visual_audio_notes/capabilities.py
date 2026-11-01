"""Model requirements are independent of whether a connection is local or remote.

This catalog/planner performs no inference, networking, credential lookup or file
processing. Connection capabilities are explicit user/provider declarations, not
inferred from a provider name or from an OpenAI-compatible endpoint.
"""
from __future__ import annotations

from copy import deepcopy
import re


MODEL_KINDS = frozenset({"speech_to_text", "ocr", "embedding", "text_generation", "vision"})
INPUT_FLAGS = frozenset({"has_documents", "has_audio", "has_images", "has_native_text", "has_transcript"})


def _capability(identifier: str, label: str, model_kind: str, explanation: str) -> dict:
    needs_model = model_kind != "none"
    return {"id": identifier, "label": label, "requires_ai": needs_model,
            "required_model_kind": model_kind,
            "requirement_label": "需要 AI" if needs_model else "无需 AI",
            "explanation": explanation}


_CATALOG = (
    _capability("file_import", "导入与整理文件", "none", "保存原件、分类与去重，不调用模型。"),
    _capability("native_text_extract", "读取文档原生文字", "none", "直接读取数字文档中已有的文字，不把 OCR 当作原生文字提取。"),
    _capability("preview", "预览与回听", "none", "显示原件、播放录音，不调用理解模型。"),
    _capability("manual_edit", "手动编辑与核对", "none", "可以手动补充转写、修改笔记和确认对应关系。"),
    _capability("keyword_search", "关键词搜索", "none", "在已有文字中按字词搜索，不使用语义向量。"),
    _capability("lexical_candidates", "文字候选匹配", "none", "按已有课件文字与转写的字词相似度列候选，不自动确认。"),
    _capability("export", "保存与离线归档", "none", "保存已有内容和附件，不重新调用生成模型。"),
    _capability("transcription", "录音转文字", "speech_to_text", "需要语音识别模型；已有转写可以直接导入复用。"),
    _capability("ocr", "识别图片中的文字", "ocr", "需要文字识别模型，不一定需要大语言模型；原生文字可直接读取。"),
    _capability("semantic_candidates", "按含义寻找候选", "embedding", "需要语义向量模型；与关键词匹配是两种功能。"),
    _capability("grounded_synthesis", "整理重点与解释", "text_generation", "需要语言模型，保留录音和课件来源，结果仍需质量核对。"),
    _capability("semantic_review", "复查改写是否有依据", "text_generation", "需要语言模型辅助复查；模型支持判断不等于事实确认。"),
    _capability("image_understanding", "理解图表与流程图", "vision", "需要视觉理解能力；只支持文字的模型不能据此读取图片。"),
)


def list_capabilities() -> list[dict]:
    """Return safe, editable display data without exposing shared catalog state."""
    return deepcopy(list(_CATALOG))


def _validate_inputs(inputs: dict) -> tuple[dict[str, bool], set[str]]:
    if not isinstance(inputs, dict) or set(inputs) - INPUT_FLAGS - {"requested_ai"}:
        raise ValueError("Unknown workflow input field")
    flags = {name: inputs.get(name, False) for name in INPUT_FLAGS}
    if any(type(value) is not bool for value in flags.values()):
        raise ValueError("Workflow input flags must be booleans")
    # Images and native document text are also documents when the caller does
    # not supply an explicit document inventory flag.
    if "has_documents" not in inputs:
        flags["has_documents"] = flags["has_images"] or flags["has_native_text"]
    requested = inputs.get("requested_ai", [])
    ai_ids = {row["id"] for row in _CATALOG if row["requires_ai"]}
    if (not isinstance(requested, list) or any(not isinstance(item, str) or item not in ai_ids for item in requested)
            or len(requested) != len(set(requested))):
        raise ValueError("requested_ai must contain unique known AI capability IDs")
    return flags, set(requested)


def _selected_connections(connections: dict) -> dict:
    if not isinstance(connections, dict) or set(connections) - MODEL_KINDS:
        raise ValueError("Connections must be selected by a known model kind")
    safe = {}
    for kind, value in connections.items():
        if not isinstance(value, dict):
            raise ValueError("Malformed selected connection")
        identifier = value.get("id")
        location = value.get("location")
        supported = value.get("capabilities")
        if (not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", identifier)
                or not isinstance(location, str) or location not in {"local", "remote"}
                or not isinstance(supported, list)
                or any(not isinstance(item, str) or item not in MODEL_KINDS for item in supported)
                or len(supported) != len(set(supported))):
            raise ValueError("Invalid selected connection metadata")
        # Do not copy endpoint URLs, API keys, provider-specific options, or any
        # unknown extra fields into the UI plan. This is not a credential store.
        safe[kind] = {"id": identifier, "location": location, "capabilities": list(supported)}
    return safe


def plan_workflow(inputs: dict, connections: dict | None = None) -> dict:
    """Describe available actions and explicitly requested model capabilities.

    ``inputs`` accepts boolean has_documents/has_audio/has_images/has_native_text/
    has_transcript and ``requested_ai`` (a list of capability IDs, default []).
    ``connections`` explicitly selects one connection per model kind, e.g.
    {"text_generation": {"id": "my-model", "location": "remote",
                          "capabilities": ["text_generation"]}}.

    ready means the supplied inputs and declared connection are available for
    that action; it is not a model-health/quality guarantee. This planner does
    not assume that a requested upstream conversion has already succeeded.
    Importing existing text/transcripts, browsing, manual edits and export never
    require a connection. It does not execute any step or silently enable AI.
    """
    flags, requested = _validate_inputs(inputs)
    selected = _selected_connections({} if connections is None else connections)
    document_text = flags["has_native_text"]
    any_text = document_text or flags["has_transcript"]
    pair_text = document_text and flags["has_transcript"]
    steps = []
    for capability in list_capabilities():
        identifier = capability["id"]
        kind = capability["required_model_kind"]
        step = {**capability, "status": "optional", "selected": identifier in requested,
                "connection_id": None, "connection_location": None,
                "missing_model_kind": None, "reason": ""}
        if not capability["requires_ai"]:
            ready = identifier in {"file_import", "manual_edit", "export"}
            if identifier == "native_text_extract":
                ready = flags["has_documents"] and document_text
            elif identifier == "preview":
                ready = flags["has_documents"] or flags["has_audio"]
            elif identifier == "keyword_search":
                ready = any_text
            elif identifier == "lexical_candidates":
                ready = pair_text
            step.update(status="ready" if ready else "optional",
                        reason="可直接使用，无需模型连接。" if ready else
                               "准备好相应原件或文字后可用；此功能本身不调用模型。")
        elif identifier == "transcription" and flags["has_transcript"]:
            step.update(status="reuse", reason="已有转写可直接复用，不调用语音识别模型。")
        elif identifier == "ocr" and document_text and not flags["has_images"]:
            step.update(status="reuse", reason="文档已有原生文字，直接读取即可，无需 OCR 调用。")
        else:
            applicable = {
                "transcription": flags["has_audio"], "ocr": flags["has_images"],
                "semantic_candidates": pair_text,
                "grounded_synthesis": any_text, "semantic_review": any_text,
                "image_understanding": flags["has_images"],
            }[identifier]
            if identifier not in requested:
                step["reason"] = "未启用；需要时再选择相应模型连接。"
            elif not applicable:
                step["reason"] = ("先准备课件文字和转写；可导入已有文本，或单独启用转写/OCR。"
                                  if identifier == "semantic_candidates" else
                                  "当前缺少此功能需要的输入；准备好输入后再执行。")
            else:
                connection = selected.get(kind)
                supported = connection is not None and kind in connection["capabilities"]
                step.update(status="ready" if supported else "needs_connection",
                            connection_id=connection["id"] if connection else None,
                            connection_location=connection["location"] if connection else None,
                            missing_model_kind=None if supported else kind,
                            reason=("已选择明确支持该能力的连接；执行与质量尚需验证。" if supported else
                                    "尚未选择支持该能力的连接；文字模型不会自动获得语音、OCR 或向量能力。"))
        steps.append(step)
    return {
        "schema_version": 1, "steps": steps,
        "ai_requested": sorted(requested),
        "missing_connections": sorted({step["missing_model_kind"] for step in steps
                                       if step["missing_model_kind"] is not None}),
        "can_use_without_ai": [step["id"] for step in steps
                               if not step["requires_ai"] and step["status"] == "ready"],
        "reused_inputs": [step["id"] for step in steps if step["status"] == "reuse"],
        "notice": "需不需要 AI 与连接在本地或线上是两个维度；连接能力是声明，不代表已验证质量。",
    }
