"""Offline archive of native PDF, imported subtitles and optional manual notes.

No model is loaded, selected or called. Existing subtitle content is not an ASR
result produced by this application, and no audio/page relation is inferred.
"""
from __future__ import annotations

import base64
import hashlib
import html
from pathlib import Path
import shutil

from .archive import READER_JS, export_archive, markdown_text, validate_archive
from .basic import parse_transcript
from .binding import validate_binding
from .core import Chunk, file_hash, load_features, read_json, write_json


NOTICE = "仅整理原件及导入转写，未调用 AI，未自动建立对应关系。"


def export_basic_archive(out: Path, title: str, audio: Path, pdf: Path, prepared: Path,
                         manual_notes: str = "") -> dict:
    """Export portable source browsing, playback and user-supplied notes.

    Requires prepared/archive_binding.json captured immediately after basic
    preparation. It is never recreated at export time. Public archive metadata
    keeps basenames and relative resources, not the original absolute paths.
    """
    out, audio, pdf, prepared = map(Path, (out, audio, pdf, prepared))
    if out.exists() or out.is_symlink():
        raise ValueError("Archive destination exists; use a new folder")
    if not isinstance(title, str) or not title.strip() or not isinstance(manual_notes, str):
        raise ValueError("Archive title and manual notes must be text")
    binding_path = prepared / "archive_binding.json"
    if not binding_path.is_file():
        raise ValueError("Capture an archive binding during basic preparation before exporting")
    binding = read_json(binding_path)
    if "basic_sources.json" not in binding.get("prepared_hashes", {}):
        raise ValueError("The basic source manifest must be part of the captured binding")
    validate_binding(binding, prepared, audio, pdf)
    metrics = read_json(prepared / "prepare_metrics.json")
    manifest = read_json(prepared / "basic_sources.json")
    for source in (metrics, manifest):
        if (not isinstance(source, dict) or source.get("uses_ai") is not False
                or type(source.get("model_calls")) is not int or source["model_calls"] != 0
                or source.get("transcript_kind") != "user_provided_transcript"):
            raise ValueError("Basic export requires a model-free preparation with supplied subtitles")
    if metrics.get("asr_performed") is not False:
        raise ValueError("Basic export cannot label a generated ASR result as imported subtitles")
    originals = manifest.get("originals", {})
    try:
        transcript_info = originals["transcript"]
        transcript = Path(transcript_info["path"])
        expected_transcript = transcript_info["sha256"]
    except (KeyError, TypeError):
        raise ValueError("The bound source manifest lacks its original subtitle file") from None
    if (not transcript.is_file() or transcript.suffix.lower() not in {".srt", ".vtt"}
            or file_hash(transcript) != expected_transcript):
        raise ValueError("Original subtitle differs from the captured source manifest")
    actual_hashes = {"audio": file_hash(audio), "pdf": file_hash(pdf), "transcript": expected_transcript}
    if any(metrics.get("input_hashes", {}).get(name) != value for name, value in actual_hashes.items()):
        raise ValueError("Basic preparation input hashes disagree with the captured originals")
    units, segments = load_features(prepared / "features.json")
    if parse_transcript(transcript) != segments:
        raise ValueError("Prepared text/times differ from the supplied subtitle file")
    source_map = read_json(prepared / "source_map.json")
    # Keep every supplied cue rather than regrouping it for a model context.
    chunks = [Chunk(s.segment_id, s.start, s.end, s.text).__dict__ for s in segments]
    predictions = [{"segment_id": s.segment_id, "unit_id": None, "status": "unreviewed"} for s in segments]
    empty_pages = [source_map[u.unit_id]["page"] for u in units if not u.text.strip()]
    missing_text = ("第 " + "、".join(map(str, empty_pages)) +
                    " 页没有原生文字；如需识别图片文字，需要另选 OCR 模型，本次未执行 OCR。") if empty_pages else "已直接读取 PDF 原生文字，未执行 OCR。"
    notes = {"overview": NOTICE + missing_text,
             "topics": [], "review_questions": [],
             "uncertainties": ["转写为使用者已有资料，本次未验证识别准确性或音频时长。",
                               "手动笔记不附自动推断的课件页或录音对应关系。"]}
    if manual_notes.strip():
        notes["topics"].append({"title": "手动笔记", "key_idea": manual_notes,
                                "explanation": "", "example": "", "pitfall": "", "limitation": "",
                                "evidence_ids": []})
    credit = f"原课件：{pdf.name}；独立录音：{audio.name}；导入转写：{transcript.name}。" + NOTICE
    checked = export_archive(out, title, audio, pdf, prepared, source_map, chunks,
                             predictions, notes, credit)
    transcript_name = "sources/transcript" + transcript.suffix.lower()
    shutil.copy2(transcript, out / transcript_name)
    # Validate actual copied bytes too, not only the mutable sources read earlier.
    copied = {"audio": out / ("sources/recording" + audio.suffix.lower()),
              "pdf": out / "sources/materials.pdf", "transcript": out / transcript_name}
    if any(file_hash(path) != actual_hashes[name] for name, path in copied.items()):
        raise ValueError("An original file changed while the archive was being copied")
    validate_binding(binding, prepared, audio, pdf)
    if file_hash(transcript) != expected_transcript:
        raise ValueError("The supplied transcript changed during export")

    document = (out / "阅读.html").read_text(encoding="utf-8")
    document = document.replace("图音笔记 · 可离线归档的研究样例", "图音笔记 · 无需 AI 的基础归档", 1)
    document = document.replace('<div class="notice">重点由本地模型整理。点击来源核对原页或录音；自动页面对应关系仍需复核。</div>',
                                '<div class="notice">' + NOTICE + '</div>', 1)
    document = document.replace("<strong>核心重点</strong>", "<strong>手动记录</strong>", 1)
    document = document.replace("<h2>复习问题</h2><ul></ul><h2>待确认</h2>", "<h2>导入说明</h2>", 1)
    document = document.replace('<details class="extra"><summary>查看完整讲解与时间索引</summary>',
                                '<details class="extra" open><summary>导入转写与原始时间索引</summary>', 1)
    controls = ('<details class="extra" open><summary>浏览课件页面</summary>' +
                " ".join('<button class="ref" data-ref="' + html.escape(identifier, quote=True) + '">资料第 ' +
                         str(locator["page"]) + ' 页</button>' for identifier, locator in
                         sorted(source_map.items(), key=lambda item: item[1]["page"])) +
                '<p><a href="sources/materials.pdf">打开原课件</a> · <a href="' + transcript_name +
                '">原字幕文件</a> · <a href="转写稿.md">完整转写稿</a></p></details>')
    document = document.replace('<main class="layout"><section>', '<main class="layout"><section>' + controls, 1)
    basic_js = READER_JS.replace(
        "current.unit_id ? '播放中的资料对应关系为自动候选，仍需核对' : '此段资料对应关系待确认'",
        "'本段为导入转写，未自动建立课件对应关系'")
    old_hash = base64.b64encode(hashlib.sha256(READER_JS.encode()).digest()).decode()
    new_hash = base64.b64encode(hashlib.sha256(basic_js.encode()).digest()).decode()
    document = document.replace('<script>' + READER_JS + '</script>', '<script>' + basic_js + '</script>', 1)
    document = document.replace("sha256-" + old_hash, "sha256-" + new_hash, 1)
    (out / "阅读.html").write_text(document, encoding="utf-8")
    markdown = ["# " + markdown_text(title), "", NOTICE, "", "## 原件与转写", "",
                "[原课件](sources/materials.pdf) · [原字幕](" + transcript_name + ") · [完整转写稿](转写稿.md)", ""]
    if manual_notes.strip():
        markdown += ["## 手动笔记", "", markdown_text(manual_notes), ""]
    markdown += ["## 导入说明", "", missing_text, "",
                 "转写由使用者提供；本次未执行语音识别、语义匹配或重点归纳。", "",
                 "来源：" + markdown_text(credit), ""]
    (out / "笔记.md").write_text("\n".join(markdown), encoding="utf-8")
    (out / "转写稿.md").write_text("\n\n".join(
        f'[{s.start:.3f}–{s.end:.3f} 秒] ' + markdown_text(s.text) for s in segments), encoding="utf-8")

    public_sources = {**manifest, "originals": {name: {
        "filename": Path(info["path"]).name, "sha256": info["sha256"], "bytes": info["bytes"],
        "archive_path": (transcript_name if name == "transcript" else
                         "sources/materials.pdf" if name == "pdf" else "sources/recording" + audio.suffix.lower()),
    } for name, info in originals.items()}, "prepared_manifest_sha256": file_hash(prepared / "basic_sources.json")}
    write_json(out / "basic_sources.json", public_sources)
    project = read_json(out / "project.json")
    project.update(mode="basic_no_ai", uses_ai=False, model_calls=0,
                   transcript_kind="user_provided_transcript", automatic_correspondence=False,
                   needs_ocr=bool(empty_pages), ocr_not_run_pages=empty_pages,
                   manual_notes=manual_notes)
    project["sources"]["transcript"] = transcript_name
    write_json(out / "project.json", project)
    checked = {**checked, **validate_archive(out), "mode": "basic_no_ai", "uses_ai": False,
               "model_calls": 0, "automatic_correspondence": False,
               "transcript_kind": "user_provided_transcript", "subtitle_bytes_verified": True,
               "needs_ocr": bool(empty_pages)}
    write_json(out / "archive_checks.json", checked)
    return checked
