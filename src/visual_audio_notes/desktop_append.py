"""Append originals in an isolated revision without running any model.

Current source identities, recognized text, explicit corrections and transcript
IDs survive additions. A new document changes candidates and invalidates the
current AI draft; previous runs and preparations remain available as history.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import shutil
import re
import uuid

from .basic import parse_transcript, prepare_basic
from .binding import create_binding, save_binding, validate_binding
from .core import Segment, Unit, file_hash, read_json
from .desktop_documents import apply_document_text, assemble_documents
from .desktop_prepare import active_review_path, refresh_candidates
from .desktop_time import check_times
from .ingest import extract_pdf
from .task_runtime import atomic_json


def _contained_file(project: Path, relative: str) -> Path:
    path = (project / relative).resolve()
    if not path.is_relative_to(project.resolve()) or not path.is_file():
        raise ValueError("原件或派生转写缺失，请先恢复资料再补充文件。")
    return path


def _verify_originals(project: Path, files: list[dict]):
    for item in files:
        if file_hash(_contained_file(project, item["path"])) != item["sha256"]:
            raise ValueError("原件与导入时不一致，未补充文件；请先恢复或重新导入。")


def _remove_owned_tree(path: Path, owner: Path):
    """Only remove the exact task-created staging tree inside its owner."""
    target, root = path.resolve(), owner.resolve()
    if target == root or not target.is_relative_to(root):
        raise ValueError("补充文件暂存路径不正确。")
    if path.exists():
        shutil.rmtree(path)


def recover_pending_appends(root: Path):
    """Recover the tiny directory-swap window before a desktop store opens.

    Only our exact transaction names and contained, validated backups qualify.
    An untouched staging copy without a backup may belong to another running
    process and is left alone. Invalid markers never authorize deleting data.
    """
    root = Path(root).resolve()
    recovered = []
    for transaction in root.glob(".append-*"):
        if (not re.fullmatch(r"\.append-(?:[a-f0-9]{12}|[a-f0-9]{32})", transaction.name)
                or transaction.is_symlink() or not transaction.is_dir()):
            continue
        marker = transaction / "transaction.json"
        if not marker.is_file():
            continue
        try:
            record = read_json(marker)
            identifier = record.get("project_id") if isinstance(record, dict) else None
            if not isinstance(identifier, str) or not re.fullmatch(r"p_[a-f0-9]{24}", identifier):
                continue
            # Fixed layouts, never marker-supplied paths. Keep old interrupted
            # transactions recoverable after shortening the Windows staging path.
            version = record.get("version")
            if type(version) is not int:
                continue
            if version == 2 and re.fullmatch(r"\.append-[a-f0-9]{12}", transaction.name):
                backup = transaction / "b"
            elif version == 1 and re.fullmatch(r"\.append-[a-f0-9]{32}", transaction.name):
                backup = transaction / "previous-project"
            else:
                continue
            project = root / "projects" / identifier
            if not backup.is_dir() or backup.is_symlink():
                continue
            backup_meta = read_json(backup / "project.json")
            if backup_meta.get("id") != identifier:
                continue
            _verify_originals(backup, backup_meta["files"])
            if project.exists():
                current = read_json(project / "project.json")
                if current.get("id") != identifier:
                    continue
                _verify_originals(project, current["files"])
            else:
                backup.rename(project)
                recovered.append(identifier)
            _remove_owned_tree(transaction, root)
        except (OSError, ValueError, KeyError, TypeError):
            # Leave questionable folders intact for inspection. One malformed
            # abandoned transaction must not prevent all projects from opening.
            continue
    return recovered


def _restore_pages(prepared: Path, old: Path, old_files: list[dict]):
    current = read_json(prepared / "features.json")
    mapping = read_json(prepared / "source_map.json")
    previous = read_json(old / "features.json")
    previous_map = read_json(old / "source_map.json")
    old_by_id = {unit["unit_id"]: unit for unit in previous["units"]}
    hashes = {item["name"].casefold(): item["sha256"] for item in old_files}
    old_documents = [item for item in old_files if item["kind"] in {"document", "office", "image"}]

    def key(locator, *, legacy=False):
        name, page = locator.get("original_filename"), locator.get("original_page")
        if name is None and legacy and len(old_documents) == 1:
            name, page = old_documents[0]["name"], locator["page"]
        if not isinstance(name, str) or name.casefold() not in hashes or type(page) is not int:
            return None
        return name.casefold(), hashes[name.casefold()], page

    old_keys = {}
    for uid, locator in previous_map.items():
        identity = key(locator, legacy=True)
        if identity is None or identity in old_keys:
            raise ValueError("旧资料缺少唯一的原件页定位，未补充文件；请重新组合导入。")
        old_keys[identity] = uid
    matched = set()
    remapped, units = {}, []
    for unit in current["units"]:
        new_id = unit["unit_id"]
        locator = mapping[new_id]
        old_id = old_keys.get(key(locator))
        if old_id is None:
            units.append(unit)
            remapped[new_id] = locator
            continue
        matched.add(old_id)
        old_locator = previous_map[old_id]
        # Fresh preview resources belong to the new combined PDF. Provenance,
        # recognized text and explicit edits belong to the unchanged original.
        locator = {**locator, **{k: deepcopy(v) for k, v in old_locator.items()
                   if k not in {"page", "image", "ocr_image"}}}
        if old_locator.get("ocr_image"):
            source = _contained_file(old, old_locator["ocr_image"])
            target = prepared / "ocr_inputs" / (old_id + source.suffix.lower())
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(source, target)
            locator["ocr_image"] = target.relative_to(prepared).as_posix()
        units.append(deepcopy(old_by_id[old_id]))
        remapped[old_id] = locator
    if matched != set(old_by_id):
        raise ValueError("原件页数或来源定位发生变化，未补充文件；请核对原资料。")
    if len({unit["unit_id"] for unit in units}) != len(units):
        raise ValueError("补充资料的来源标识冲突，未修改原项目。")
    current["units"] = units
    atomic_json(prepared / "features.json", current)
    atomic_json(prepared / "source_map.json", remapped)
    for name in ("recognition_sources.json", "corrections.json"):
        if (old / name).is_file():
            shutil.copy2(old / name, prepared / name)


def _prepare_revision(stage: Path, final: Path, meta: dict, appended: list[dict]):
    previous_meta = deepcopy(meta)
    old = stage / "prepared_history" / ("v_" + uuid.uuid4().hex[:12])
    old.parent.mkdir(exist_ok=True)
    (stage / "prepared").rename(old)
    old_content = read_json(old / "features.json")
    old_metrics = read_json(old / "prepare_metrics.json") if (old / "prepare_metrics.json").is_file() else {}
    # Office converters require a fresh destination. Keep the old derived PDF
    # and conversion files with the old preparation instead of overwriting them.
    previous_pdf = stage / meta["pdf"] if meta.get("pdf") else None
    previous_pdf_hash = file_hash(previous_pdf) if previous_pdf else None
    snapshot_pdf = None
    if (stage / "converted").exists():
        (stage / "converted").rename(old / "converted")
        if previous_pdf and previous_pdf.is_relative_to(stage / "converted"):
            snapshot_pdf = old / previous_pdf.relative_to(stage)
    if previous_pdf and previous_pdf.exists() and not previous_pdf.is_relative_to(stage / "originals"):
        snapshot_pdf = old / "previous-materials.pdf"
        previous_pdf.rename(snapshot_pdf)
    if snapshot_pdf:
        atomic_json(old / "document_snapshot.json", {
            "pdf": snapshot_pdf.relative_to(old).as_posix(), "sha256": previous_pdf_hash})
    assembled = assemble_documents(stage, meta["files"], language=meta.get("document_preview_language", "zh-CN"))
    pdf = assembled["pdf"]
    audio = stage / meta["audio"] if meta.get("audio") else None
    user_subtitle = next((item for item in appended if item["kind"] == "transcript"), None)
    if user_subtitle:
        meta["transcript"] = user_subtitle["path"]
        meta["transcript_origin"] = "user_provided_transcript"
    if not audio:
        audio_item = next((item for item in appended if item["kind"] == "audio"), None)
        if audio_item:
            meta["audio"] = audio_item["path"]
            audio = stage / audio_item["path"]
    transcript = stage / meta["transcript"] if meta.get("transcript") else None
    prepared = stage / "prepared"
    full = bool(pdf and audio and transcript)
    if full:
        prepare_basic(pdf, audio, transcript, prepared)
    else:
        units, mapping = extract_pdf(pdf, prepared / "pages", 20261101) if pdf else ([], {})
        segments = parse_transcript(transcript) if transcript else []
        atomic_json(prepared / "features.json", {"units": [asdict(u) for u in units],
                    "segments": [asdict(s) for s in segments]})
        atomic_json(prepared / "source_map.json", mapping)
    apply_document_text(prepared, assembled)
    _restore_pages(prepared, old, previous_meta["files"][:-len(appended)])
    content = read_json(prepared / "features.json")
    if not user_subtitle:
        # Parsing an ASR/revised VTT would assign new opaque IDs. Keep the exact
        # current cue text, time and identity rather than recreating that source.
        content["segments"] = old_content["segments"]
    for unit in content["units"]:
        Unit.from_wire(unit)
    for segment in content["segments"]:
        Segment.from_wire(segment)
    atomic_json(prepared / "features.json", content)
    metrics = read_json(prepared / "prepare_metrics.json") if (prepared / "prepare_metrics.json").is_file() else {}
    metrics.update({k: v for k, v in old_metrics.items() if k not in {
        "input_hashes", "pages", "transcript_segments", "empty_text_unit_ids", "needs_ocr", "source_manifest"}})
    metrics.update(pages=len(content["units"]), transcript_segments=len(content["segments"]),
                   transcript_kind=meta.get("transcript_origin"), appended_without_models=True)
    if pdf and audio:
        metrics["input_hashes"] = {"audio": file_hash(audio), "pdf": file_hash(pdf)}
        if transcript:
            metrics["input_hashes"]["transcript"] = file_hash(transcript)
    atomic_json(prepared / "prepare_metrics.json", metrics)
    # Basic archive manifests require the final persistent paths, not this
    # isolated staging path which disappears after commit.
    manifest_path = prepared / "basic_sources.json"
    if manifest_path.is_file():
        manifest = read_json(manifest_path)
        for name, value in manifest["originals"].items():
            value["path"] = str(final / Path(value["path"]).relative_to(stage))
        atomic_json(manifest_path, manifest)
    time_checks = check_times(audio, content["segments"])
    atomic_json(prepared / "time_checks.json", time_checks)
    refresh_candidates(prepared, active_review_path(stage, previous_meta, old), previous_prepared=old)
    full = bool(pdf and audio and content["units"] and content["segments"])
    if full:
        save_binding(prepared / "archive_binding.json", create_binding(prepared, audio, pdf))
        validate_binding(read_json(prepared / "archive_binding.json"), prepared, audio, pdf)
    warnings = meta.get("preparation_warnings", []) + [warning for row in assembled["conversions"] for warning in row["warnings"]]
    warnings.append("已补充资料。请核对受影响的对应关系与重点草稿；原件、识别结果和历史版本均保留。")
    if user_subtitle and previous_meta.get("transcript"):
        warnings.append("新提供的字幕已替代当前转写；旧转写文件、识别记录和人工修订仍保留在项目历史中。")
    if time_checks.get("within_audio_bounds") is False:
        warnings.append(time_checks["message"])
    meta.update(full_prepared=full, pdf=pdf.relative_to(stage).as_posix() if pdf else None,
                status="ready", latest_run=None, completed_run=None, archived_at=None, export_path=None,
                preparation_revision=meta.get("preparation_revision", 0) + 1,
                preparation_warnings=list(dict.fromkeys(warnings)))
    events = read_json(stage / "append_history.json") if (stage / "append_history.json").is_file() else {"events": []}
    events["events"].append({"at_utc": datetime.now(timezone.utc).isoformat(), "uses_ai": False,
        "added": [{k: row[k] for k in ("name", "kind", "sha256")} for row in appended],
        "previous_transcript": previous_meta.get("transcript"), "current_transcript": meta.get("transcript"),
        "previous_transcript_origin": previous_meta.get("transcript_origin"), "prepared_history": old.relative_to(stage).as_posix()})
    atomic_json(stage / "append_history.json", events)
    atomic_json(stage / "project.json", meta)


def append_files(store, identifier: str, paths: list) -> dict:
    """Add files to one existing project; no network/model work is permitted."""
    from .desktop_store import _KINDS
    with store._lock:
        if store._job and store._job.get("state") == "running":
            raise ValueError("请先停止本次整理，再补充资料。")
        project = store._path(identifier)
        meta = read_json(project / "project.json")
        _verify_originals(project, meta["files"])
        if (project / "prepared/archive_binding.json").is_file() and meta.get("audio") and meta.get("pdf"):
            validate_binding(read_json(project / "prepared/archive_binding.json"), project / "prepared",
                             _contained_file(project, meta["audio"]), _contained_file(project, meta["pdf"]))
        if not isinstance(paths, list) or not paths or len(paths) > 200:
            raise ValueError("请选择要补充的课件、图片、录音或已有转写。")
        old_names = {item["name"].casefold(): item for item in meta["files"]}
        selected, seen = [], {}
        for value in paths:
            if not isinstance(value, (str, Path)):
                raise ValueError("补充文件路径不正确。")
            path = Path(value).resolve()
            kind = _KINDS.get(path.suffix.lower())
            if not kind or not path.is_file() or path.stat().st_size == 0:
                raise ValueError("请选择有效的课件、图片、录音或 SRT/VTT 文件。")
            checksum, folded = file_hash(path), path.name.casefold()
            previous = old_names.get(folded) or seen.get(folded)
            if previous:
                if previous["sha256"] != checksum:
                    raise ValueError("同名文件内容不同，未覆盖原件；请先为补充文件改名。")
                continue
            row = {"name": path.name, "kind": kind, "path": "originals/" + path.name, "sha256": checksum}
            selected.append((path, row))
            seen[folded] = row
        if not selected:
            return store.project(identifier)
        combined = meta["files"] + [row for _, row in selected]
        if len(combined) > 200 or sum((project / row["path"]).stat().st_size for row in meta["files"]) + sum(path.stat().st_size for path, _ in selected) > 2 * 1024 ** 3:
            raise ValueError("一份资料最多 200 个原件、合计 2 GB，请分成另一份笔记。")
        if any(sum(row["kind"] == kind for row in combined) > 1 for kind in ("audio", "transcript")):
            raise ValueError("一份笔记先使用一段原录音和一份用户字幕，不能追加第二份同类文件。")
        for path, row in selected:
            if row["kind"] == "transcript":
                parse_transcript(path)
        # Both roots are shorter than projects/p_<24hex>, so copying a working
        # Windows cache path cannot itself push it past the legacy MAX_PATH.
        transaction = store.root / (".append-" + uuid.uuid4().hex[:12])
        stage, backup = transaction / "n", transaction / "b"
        transaction.mkdir()
        committed = False
        try:
            shutil.copytree(project, stage)
            for path, row in selected:
                shutil.copy2(path, stage / row["path"])
                if file_hash(stage / row["path"]) != row["sha256"]:
                    raise ValueError("补充时原文件发生变化，请重新选择。")
            staged_meta = deepcopy(meta)
            staged_meta["files"] = combined
            _prepare_revision(stage, project, staged_meta, [row for _, row in selected])
            _verify_originals(stage, combined)
            # Check old sources again immediately before commit, too.
            _verify_originals(project, meta["files"])
            atomic_json(transaction / "transaction.json", {"version": 2, "project_id": identifier})
            project.rename(backup)
            try:
                stage.rename(project)
                committed = True
            except BaseException:
                backup.rename(project)
                raise
            return store.project(identifier)
        finally:
            # Before commit, only isolated copies are removed. After commit,
            # previous-project is a verified duplicate of the retained history.
            if committed or project.exists():
                _remove_owned_tree(transaction, store.root)
