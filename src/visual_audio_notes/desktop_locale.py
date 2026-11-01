"""Shared desktop labels. Source text is never passed through localization."""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import re
import sys

LANGUAGES = {"zh-CN", "en"}


def normalize_language(value):
    if value not in LANGUAGES:
        raise ValueError("请选择简体中文或英文。")
    return value


@lru_cache(maxsize=1)
def catalog():
    paths = [Path(__file__).parent / "desktop_ui/en.json"]
    if getattr(sys, "frozen", False):
        paths.append(Path(sys._MEIPASS) / "src/visual_audio_notes/desktop_ui/en.json")
    for path in paths:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    raise ValueError("Language resources are missing. Extract the entire app folder again.")


@lru_cache(maxsize=1)
def _templates():
    patterns = []
    ordered = sorted(catalog().items(), key=lambda pair: len(re.sub(r"\{v\d+\}", "", pair[0])), reverse=True)
    for source, target in ordered:
        if "{v" not in source:
            continue
        expression = re.escape(source)
        for name in re.findall(r"\{(v\d+)\}", source):
            expression = expression.replace(re.escape("{" + name + "}"), "(?P<" + name + ">.+?)")
        patterns.append((re.compile(expression, re.S), target))
    return patterns


def translate(text, language="zh-CN"):
    if language != "en" or not isinstance(text, str):
        return text
    known = catalog().get(text)
    if known is not None:
        return known
    for pattern, target in _templates():
        match = pattern.fullmatch(text)
        if match:
            return target.format(**match.groupdict())
    prefix, suffix = "本次读取或识别已完成；请用添加资料补齐", "，再归纳重点。"
    if text.startswith(prefix) and text.endswith(suffix):
        missing = text[len(prefix):-len(suffix)].split("、")
        return catalog()[prefix] + ", ".join(catalog().get(part, part) for part in missing) + catalog()[suffix]
    return text


def localize_payload(value, language):
    """Only known application metadata fields, never titles, notes or quotes."""
    labels = {"status_label", "summary", "message", "error"}
    warnings = {"preparation_warnings", "preview_warnings"}
    if isinstance(value, list):
        return [localize_payload(item, language) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in labels and isinstance(item, str):
            result[key] = translate(item, language)
        elif key in warnings and isinstance(item, list):
            result[key] = [translate(warning, language) for warning in item]
        elif key in {"topics", "segments", "chunks", "review_items", "manual_notes"}:
            result[key] = item
        else:
            result[key] = localize_payload(item, language)
    return result
