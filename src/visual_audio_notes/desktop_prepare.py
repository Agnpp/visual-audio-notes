"""Stage recognition results into a new, source-bound prepared revision."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import shutil
import uuid

from .binding import create_binding, save_binding
from .core import Chunk, Unit, file_hash, read_json
from .review import create_review, load_review, refresh_review, save_review
from .task_runtime import TaskCancelled, atomic_json


def refresh_candidates(prepared: Path, previous_review: Path | None = None):
    from .desktop_matching import rank_candidates
    content = read_json(prepared / "features.json")
    if not content["units"] or not content["segments"]:
        return
    units = [Unit.from_wire(item) for item in content["units"]]
    chunks = [Chunk(**item) for item in content["segments"]]
    ranked = rank_candidates(units, chunks)
    atomic_json(prepared / "chunks.json", {"chunks": [asdict(c) for c in chunks]})
    review = refresh_review(load_review(previous_review), ranked, units, chunks) if previous_review and previous_review.is_file() else create_review(ranked, units, chunks)
    save_review(prepared / "review.json", review)
    atomic_json(prepared / "candidate_metrics.json", {"method": "lexical", "uses_ai": False,
                "candidates": sum(bool(row["candidates"]) for row in ranked), "chunks": len(chunks),
                "automatic_confirmations": 0})


def prepare_recognition(project: Path, settings, progress, *, cancel_event=None, request_guard=None):
    from .recognition import transcribe, recognize_pages, segments_to_vtt
    meta = read_json(project / "project.json")
    prepared = project / "prepared"
    content = read_json(prepared / "features.json")
    mapping = read_json(prepared / "source_map.json")
    need_asr = bool(meta["audio"] and not content["segments"])
    blank = {unit["unit_id"] for unit in content["units"] if not unit["text"].strip()
             and not mapping[unit["unit_id"]].get("ocr_completed") and not mapping[unit["unit_id"]].get("ocr_unavailable")}
    if not need_asr and not blank:
        refresh_candidates(prepared, prepared / "review.json")
        return {"changed": False, "uses_ai": False}
    for original in meta["files"]:
        if file_hash(project / original["path"]) != original["sha256"]:
            raise ValueError("原件与导入时不一致，请重新导入资料。")
    def check():
        if cancel_event is not None and cancel_event.is_set():
            raise TaskCancelled("已停止，识别缓存和原件已保留。")
    def callback(stage):
        return lambda message, fraction=None: progress(stage, message, fraction)
    report = {"version": "recognition-preparation-v1", "uses_ai": True,
              "asr": None, "ocr": None, "warnings": []}
    with settings.credentials():
        if need_asr:
            check()
            progress("asr", "正在识别录音，时间仍对应原始录音…", None)
            asr_config = settings.config("asr")
            if request_guard is not None:
                asr_config["_request_guard"] = request_guard
            result = transcribe(project / meta["audio"], asr_config, project / ".recognition-cache",
                                progress=callback("asr"), cancel_event=cancel_event)
            if not result["segments"]:
                raise ValueError("没有识别到可用语音，已保留原录音；请检查录音或更换语音识别方式。")
            content["segments"] = result["segments"]
            report["asr"] = {k: v for k, v in result.items() if k != "segments"}
            derived = project / "derived"
            derived.mkdir(exist_ok=True)
            transcript = derived / ("transcript-" + uuid.uuid4().hex[:12] + ".vtt")
            transcript.write_text(segments_to_vtt(result["segments"]), encoding="utf-8")
            meta["transcript"] = transcript.relative_to(project).as_posix()
            meta["transcript_origin"] = "generated_asr"
        if blank:
            check()
            progress("ocr", "只识别没有原生文字的图片或扫描页…", None)
            pages = []
            for unit in content["units"]:
                locator = mapping[unit["unit_id"]]
                if unit["unit_id"] not in blank:
                    continue
                image = prepared / locator["ocr_image"] if locator.get("ocr_image") else prepared / "pages" / locator["image"]
                pages.append({"unit_id": unit["unit_id"], "image": image, "text": unit["text"]})
            ocr_config = settings.config("ocr")
            if request_guard is not None:
                ocr_config["_request_guard"] = request_guard
            result = recognize_pages(pages, ocr_config, project / ".recognition-cache",
                                     progress=callback("ocr"), cancel_event=cancel_event)
            for unit in content["units"]:
                uid = unit["unit_id"]
                if uid in blank and result["texts"].get(uid):
                    unit["text"] = result["texts"][uid]
                    mapping[uid]["text_origin"] = "ocr_requires_verification"
            failed = {item["unit_id"] for item in result.get("failures", [])}
            for uid in blank - failed:
                mapping[uid]["ocr_completed"] = True
                if not result["texts"].get(uid):
                    mapping[uid]["text_origin"] = "ocr_no_text_detected"
            report["ocr"] = result
            for item in result.get("failures", []):
                report["warnings"].append("部分页面识别未完成，请核对原图或更换图片识别方式。")
            if any(not unit["text"].strip() for unit in content["units"]):
                report["warnings"].append("部分图片没有可读文字；OCR 不代表已经理解图表或流程关系，归纳仍需核对原图。")
    check()
    staging = project / ("preparing-" + uuid.uuid4().hex[:12])
    shutil.copytree(prepared, staging)
    # A new preparation revision never overwrites an old run's captured files.
    if (staging / "archive_binding.json").exists():
        (staging / "archive_binding.json").unlink()
    atomic_json(staging / "features.json", content)
    atomic_json(staging / "source_map.json", mapping)
    from .desktop_time import check_times
    time_checks = check_times(project / meta["audio"] if meta["audio"] else None, content["segments"])
    atomic_json(staging / "time_checks.json", time_checks)
    if time_checks.get("within_audio_bounds") is False:
        report["warnings"].append(time_checks["message"])
    previous_sources = read_json(staging / "recognition_sources.json") if (staging / "recognition_sources.json").is_file() else {}
    # A later OCR retry must not erase the ASR model/output provenance from an
    # earlier successful preparation. Keep each original stage report as well
    # as the latest available ASR/OCR summary; history never nests itself.
    previous_history = previous_sources.get("history", [])
    if previous_sources and not previous_history:
        previous_history = [previous_sources]
    source_record = {**report,
                     "asr": report["asr"] or previous_sources.get("asr"),
                     "ocr": report["ocr"] or previous_sources.get("ocr"),
                     "history": previous_history + [report]}
    atomic_json(staging / "recognition_sources.json", source_record)
    metrics = read_json(staging / "prepare_metrics.json") if (staging / "prepare_metrics.json").is_file() else {}
    metrics.update(uses_ai=True, asr_performed=need_asr or metrics.get("asr_performed", False),
                   ocr_performed=bool(blank), transcript_kind=meta.get("transcript_origin", "user_provided_transcript"))
    if meta["audio"] and meta["pdf"]:
        metrics["input_hashes"] = {"audio": file_hash(project / meta["audio"]), "pdf": file_hash(project / meta["pdf"])}
        if meta["transcript"]:
            metrics["input_hashes"]["transcript"] = file_hash(project / meta["transcript"])
    atomic_json(staging / "prepare_metrics.json", metrics)
    previous = project / "runs" / meta["completed_run"] / "candidate_review.json" if meta.get("completed_run") else prepared / "review.json"
    refresh_candidates(staging, previous)
    complete = bool(content["units"] and content["segments"] and meta["audio"] and meta["pdf"])
    if complete:
        save_binding(staging / "archive_binding.json", create_binding(staging, project / meta["audio"], project / meta["pdf"]))
    check()
    history = project / "prepared_history" / ("v_" + uuid.uuid4().hex[:12])
    history.parent.mkdir(exist_ok=True)
    prepared.rename(history)
    try:
        staging.rename(prepared)
    except BaseException:
        history.rename(prepared)
        raise
    # Source text changed: old AI drafts stay in runs, not shown as current notes.
    existing_warnings = meta.get("preparation_warnings", [])
    meta.update(full_prepared=complete, completed_run=None, preparation_warnings=list(dict.fromkeys(existing_warnings + report["warnings"])),
                preparation_uses_ai=True, preparation_revision=meta.get("preparation_revision", 0) + 1)
    atomic_json(project / "project.json", meta)
    return {"changed": True, **report}


def edit_source_text(project: Path, kind: str, source_id: str, text: str, *, start=None, end=None):
    """An explicit user correction preserves raw recognition and invalidates drafts."""
    from .core import Segment
    from .recognition import segments_to_vtt
    from .desktop_time import check_times
    if kind not in {"page", "transcript"} or not isinstance(text, str) or not text.strip() or len(text) > 200000:
        raise ValueError("修订文字不能为空或过长。")
    meta = read_json(project / "project.json")
    prepared = project / "prepared"
    content = read_json(prepared / "features.json")
    collection = content["units"] if kind == "page" else content["segments"]
    field = "unit_id" if kind == "page" else "segment_id"
    current = next((item for item in collection if item[field] == source_id), None)
    if current is None:
        raise ValueError("找不到当前文字，请重新打开资料后再修订。")
    previous = dict(current)
    current["text"] = text.strip()
    if kind == "transcript":
        if start is not None:
            current["start"] = start
        if end is not None:
            current["end"] = end
        for index, item in enumerate(content["segments"]):
            Segment.from_wire(item)
            if index and item["start"] < content["segments"][index - 1]["start"]:
                raise ValueError("开始时间不能早于前一段，请保留原录音的时间顺序。")
    else:
        Unit.from_wire(current)
    if current == previous:
        return
    staging = project / ("preparing-edit-" + uuid.uuid4().hex[:12])
    shutil.copytree(prepared, staging)
    (staging / "archive_binding.json").unlink(missing_ok=True)
    atomic_json(staging / "features.json", content)
    mapping = read_json(staging / "source_map.json")
    if kind == "page":
        mapping[source_id]["text_origin"] = "user_corrected"
        mapping[source_id]["ocr_completed"] = True
        atomic_json(staging / "source_map.json", mapping)
    changes = read_json(staging / "corrections.json") if (staging / "corrections.json").is_file() else {"events": []}
    from datetime import datetime, timezone
    changes["events"].append({"at_utc": datetime.now(timezone.utc).isoformat(), "actor": "desktop_user_explicit_edit",
                               "kind": kind, "source_id": source_id, "before": previous, "after": current})
    atomic_json(staging / "corrections.json", changes)
    if kind == "transcript":
        derived = project / "derived"
        derived.mkdir(exist_ok=True)
        subtitle = derived / ("revised-transcript-" + uuid.uuid4().hex[:12] + ".vtt")
        subtitle.write_text(segments_to_vtt(content["segments"]), encoding="utf-8")
        meta.update(transcript=subtitle.relative_to(project).as_posix(), transcript_origin="user_edited")
    time_checks = check_times(project / meta["audio"] if meta["audio"] else None, content["segments"])
    atomic_json(staging / "time_checks.json", time_checks)
    metrics = read_json(staging / "prepare_metrics.json") if (staging / "prepare_metrics.json").is_file() else {}
    metrics.update(user_corrected=True, transcript_kind=meta.get("transcript_origin"))
    if meta["audio"] and meta["pdf"]:
        metrics["input_hashes"] = {"audio": file_hash(project / meta["audio"]), "pdf": file_hash(project / meta["pdf"])}
        if meta.get("transcript"):
            metrics["input_hashes"]["transcript"] = file_hash(project / meta["transcript"])
    atomic_json(staging / "prepare_metrics.json", metrics)
    prior = project / "runs" / meta["completed_run"] / "candidate_review.json" if meta.get("completed_run") else prepared / "review.json"
    refresh_candidates(staging, prior)
    if meta["full_prepared"]:
        save_binding(staging / "archive_binding.json", create_binding(staging, project / meta["audio"], project / meta["pdf"]))
    history = project / "prepared_history" / ("v_" + uuid.uuid4().hex[:12])
    history.parent.mkdir(exist_ok=True)
    prepared.rename(history)
    try:
        staging.rename(prepared)
    except BaseException:
        history.rename(prepared)
        raise
    meta.update(status="ready", latest_run=None, completed_run=None, preparation_revision=meta.get("preparation_revision", 0) + 1)
    warnings = meta.get("preparation_warnings", []) + ["文字已经用户修订，原有对应关系需重新核对；原始资料及旧识别结果仍保留。"]
    if time_checks.get("within_audio_bounds") is False:
        warnings.append(time_checks["message"])
    meta["preparation_warnings"] = list(dict.fromkeys(warnings))
    atomic_json(project / "project.json", meta)
