"""Desktop-only orchestration without torch, sklearn, or research CLI imports."""
from __future__ import annotations

from dataclasses import asdict
import base64
import hashlib
import html
import json
from pathlib import Path
import re
import shutil
import urllib.parse

from .binding import validate_binding
from .core import Chunk, chunks_from_segments, file_hash, load_features, read_json
from .desktop_prepare import active_review_path
from .providers import create_json_model
from .quality import synthesize_grounded
from .review import create_review, load_review, refresh_review, save_review, source_fingerprint
from .task_runtime import TaskBudgetExceeded, TaskCancelled, TaskModel, atomic_json, usage_report
from .topic_notes import organize_topics, render_topic_markdown


def run_ai(project: Path, resources: Path, run_id: str, connection, refresh: bool, progress, *, cancel_event=None,
           max_calls=32, max_output_tokens=128000, language="zh-CN"):
    meta = read_json(project / "project.json")
    prepared = project / "prepared"
    audio, pdf = project / meta["audio"], project / meta["pdf"]
    binding = read_json(prepared / "archive_binding.json")
    validate_binding(binding, prepared, audio, pdf)
    units, segments = load_features(prepared / "features.json")
    config = read_json(resources / "benchmark/provider.v3.json")
    config.update(persistent_cache=True, cache_max_age_seconds=86400,
                  cache_policy_version="product-task-cache-v1", cache_force_refresh=refresh)
    from .desktop_locale import normalize_language
    language = normalize_language(language)
    prompt_suffix = ".en.txt" if language == "en" else ".txt"
    config["output_language"] = language
    # Desktop keeps the imported cue axis and IDs, so a human's manual links
    # survive adding AI organization. Text batching remains independently bounded.
    chunks = [Chunk(s.segment_id, s.start, s.end, s.text) for s in segments]
    run = project / "runs" / run_id
    run.mkdir(parents=True, exist_ok=False)
    runtime = TaskModel(create_json_model(connection), run / "task_runtime.json", stop_on_request_error=True,
                        cancel_event=cancel_event, max_calls=max_calls, max_requested_output_tokens=max_output_tokens)
    extraction, grouped = None, None
    try:
        atomic_json(run / "chunks.json", {"chunks": [asdict(c) for c in chunks]})
        # No score is invented and no page is automatically confirmed. The
        # review can still register an explicit user's page selection later.
        from .desktop_matching import rank_candidates
        runtime.stage("candidates", "running")
        progress("candidates", "正在按内容查找课件候选，不使用翻页时间…")
        candidates = rank_candidates(units, chunks)
        previous = active_review_path(project, meta)
        review = refresh_review(load_review(previous), candidates, units, chunks) if previous.is_file() else create_review(candidates, units, chunks)
        save_review(run / "candidate_review.json", review)
        runtime.stage("candidates", "complete")
        runtime.stage("evidence", "running")
        progress("evidence", "正在提取录音与课件的原文依据…")
        extraction = synthesize_grounded(units, chunks, runtime,
            (resources / ("prompts/quality_spans" + prompt_suffix)).read_text(encoding="utf-8"), project / ".van-cache", config)
        atomic_json(run / "quality_notes.json", extraction)
        if not extraction.get("ledger"):
            raise ValueError("未得到可以核对的证据，请检查转写和资料文字。")
        runtime.stage("evidence", "complete")
        # Reuse grounded bilingual draft terms for candidates; no new model call.
        candidates = rank_candidates(units, chunks, ledger=extraction["ledger"])
        review = refresh_review(review, candidates, units, chunks)
        save_review(run / "candidate_review.json", review)
        atomic_json(run / "candidate_metrics.json", {
            "method": "lexical_with_ai_derived_terms", "uses_ai_derived_terms": True,
            "chunks": len(chunks), "candidates": sum(bool(row["candidates"]) for row in candidates),
            "automatic_confirmations": 0})
        runtime.stage("topics", "running")
        progress("topics", "正在归纳重点，录音内容与课件补充分开保存…")
        grouped = organize_topics(extraction, runtime,
            (resources / ("prompts/topic_notes" + prompt_suffix)).read_text(encoding="utf-8"), project / ".van-cache", config)
        atomic_json(run / "topic_notes.json", grouped)
        (run / "主题笔记草稿.md").write_text(render_topic_markdown(grouped, extraction["ledger"]), encoding="utf-8")
        validate_binding(binding, prepared, audio, pdf)
        runtime.stage("topics", "complete")
        runtime.finish()
        atomic_json(run / "run_manifest.json", {
            "version": "desktop-product-run-v1", "complete": True,
            "feature_sha256": file_hash(prepared / "features.json"),
            "binding_sha256": file_hash(prepared / "archive_binding.json"),
            "outputs": {name: file_hash(run / name) for name in
                        ("quality_notes.json", "topic_notes.json", "chunks.json")},
            "connection": connection.public_dict(), "quality_status": "unverified_draft", "output_language": language})
    except BaseException as error:
        runtime.finish("cancelled" if isinstance(error, TaskCancelled) else "budget_stopped" if isinstance(error, TaskBudgetExceeded) else "failed")
        raise
    finally:
        batches = (extraction or {}).get("batches", []) + (grouped or {}).get("batches", [])
        atomic_json(run / "usage_report.json", usage_report(runtime.calls, batches))


def _append_manual(out, text):
    if not text.strip():
        return
    with (out / "笔记.md").open("a", encoding="utf-8") as stream:
        stream.write("\n\n## 我的笔记\n\n" + text + "\n")
    reader = out / "阅读.html"
    content = reader.read_text(encoding="utf-8")
    extra = '<section style="padding:24px;max-width:1100px;margin:auto"><h2>我的笔记</h2><div style="white-space:pre-wrap">' + html.escape(text) + '</div></section>'
    reader.write_text(content.replace("</body>", extra + "</body>"), encoding="utf-8")


def _apply_basic_review(out, project, meta, prepared, *, uses_ai=False):
    from .archive import validate_archive
    path = active_review_path(project, meta)
    if not path.is_file():
        return
    units, segments = load_features(prepared / "features.json")
    chunks = [Chunk(s.segment_id, s.start, s.end, s.text) for s in segments]
    review = load_review(path, expected_fingerprint=source_fingerprint(units, chunks))
    selected = {item["segment_id"]: item for item in review["items"]}
    manifest = read_json(out / "project.json")
    count = 0
    for cue in manifest["timeline"]:
        item = selected[cue["segment_id"]]
        single = item["status"] == "confirmed" and len(item["confirmed_unit_ids"]) == 1
        cue["unit_id"] = item["confirmed_unit_ids"][0] if single else None
        cue["status"] = item["status"]
        manifest["evidence"][cue["segment_id"]]["unit_id"] = cue["unit_id"]
        count += single
    manifest["human_confirmed_single_page_mappings"] = count
    atomic_json(out / "project.json", manifest)
    atomic_json(out / "审阅记录.json", review)
    reader = out / "阅读.html"
    document = reader.read_text(encoding="utf-8")
    payload = json.dumps({key: manifest[key] for key in ("units", "evidence", "timeline")}, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026")
    document = re.sub(r'(<script type="application/json" id="archive-data">).*?(</script>)',
                      lambda match: match[1] + payload + match[2], document, count=1, flags=re.S)
    found = re.search(r"<script>(.*?)</script>", document, re.S)
    if found:
        old = found[1]
        new = old.replace("'本段为导入转写，未自动建立课件对应关系'",
                          "current.unit_id ? '本段课件对应关系由使用者人工确认' : '本段尚未人工确认课件对应关系'")
        new = new.replace("current.unit_id ? '播放中的资料对应关系为自动候选，仍需核对' : '此段资料对应关系待确认'",
                          "current.unit_id ? '本段课件对应关系由使用者人工确认' : '本段尚未人工确认课件对应关系'")
        old_hash = base64.b64encode(hashlib.sha256(old.encode()).digest()).decode()
        new_hash = base64.b64encode(hashlib.sha256(new.encode()).digest()).decode()
        document = document.replace("<script>" + old + "</script>", "<script>" + new + "</script>").replace(old_hash, new_hash)
    reader.write_text(document, encoding="utf-8")
    checked = {**validate_archive(out), "mode": "recognized_sources_no_synthesis" if uses_ai else "basic_no_ai", "uses_ai": uses_ai,
               "model_calls": None if uses_ai else 0, "human_confirmed_single_page_mappings": count}
    atomic_json(out / "archive_checks.json", checked)
    return checked


def _append_derived(out, project, meta):
    """Preserve recognition and every original Office/image alongside previews."""
    prepared = project / "prepared"
    sources = out / "sources"
    sources.mkdir(exist_ok=True)
    retained = []
    existing = {file_hash(p): p for p in sources.rglob("*") if p.is_file()}
    for index, item in enumerate(meta["files"]):
        source = project / item["path"]
        target = existing.get(item["sha256"])
        if target is None:
            target = sources / "originals" / (str(index + 1) + "-" + source.name)
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(source, target)
            if file_hash(target) != item["sha256"]:
                raise ValueError("归档原件复制校验未通过，请重新保存。")
        retained.append({"filename": source.name, "sha256": item["sha256"],
                         "path": target.relative_to(out).as_posix()})
    atomic_json(out / "原件清单.json", {"originals": retained, "preparation_warnings": meta.get("preparation_warnings", [])})
    if meta.get("transcript_origin") in {"generated_asr", "user_edited"} and meta.get("transcript"):
        source = project / meta["transcript"]
        shutil.copy2(source, sources / ("revised-transcript.vtt" if meta["transcript_origin"] == "user_edited" else "generated-transcript.vtt"))
    transcripts = []
    for source in sorted((project / "derived").glob("*.vtt")):
        if not source.resolve().is_relative_to(project.resolve()):
            raise ValueError("派生转写路径不正确，请检查项目资料。")
        target = sources / "transcript_history" / source.name
        target.parent.mkdir(exist_ok=True)
        shutil.copy2(source, target)
        checksum = file_hash(source)
        if file_hash(target) != checksum:
            raise ValueError("历史转写复制校验未通过，请重新保存。")
        transcripts.append({"filename": source.name, "sha256": checksum,
                            "path": target.relative_to(out).as_posix(),
                            "current": source.relative_to(project).as_posix() == meta.get("transcript")})
    if transcripts:
        atomic_json(out / "派生转写清单.json", {"transcripts": transcripts})
    if (project / "append_history.json").is_file():
        shutil.copy2(project / "append_history.json", out / "资料补充记录.json")
    features = read_json(prepared / "features.json")
    if (prepared / "recognition_sources.json").is_file():
        shutil.copy2(prepared / "recognition_sources.json", out / "识别记录.json")
    attempts = sorted((project / ".recognition-cache" / "attempts").glob("*.json"))
    if attempts or (prepared / "recognition_sources.json").is_file():
        from .task_runtime import usage_report
        atomic_json(out / "识别用量.json", {
            "scope": "Recorded recognition API attempts over this project's history; includes failed attempts, excludes text synthesis and local model work.",
            "usage": usage_report([read_json(attempt) for attempt in attempts]),
        })
    for source, destination in (("corrections.json", "人工修订记录.json"), ("time_checks.json", "时间范围核对.json")):
        if (prepared / source).is_file():
            shutil.copy2(prepared / source, out / destination)
    if (prepared / "recognition_sources.json").is_file() or (prepared / "corrections.json").is_file():
        atomic_json(out / "识别文字.json", features)
        from .archive import markdown_text
        lines = ["# 识别与修订的文字", "", "转写、OCR 和用户修订都需要核对原音和原图；不是已经核实的讲者原话。", ""]
        for segment in features["segments"]:
            lines += [f"[{segment['start']:.3f}–{segment['end']:.3f} 秒] " + markdown_text(segment["text"]), ""]
        for unit in features["units"]:
            lines += ["## 资料 `" + unit["unit_id"] + "`", "", markdown_text(unit["text"]), ""]
        (out / "识别文字.md").write_text("\n".join(lines), encoding="utf-8")
    if (prepared / "office_sources.json").is_file():
        shutil.copy2(prepared / "office_sources.json", out / "文档转换记录.json")
    links = ["[" + item["filename"].replace("[", "\\[").replace("]", "\\]") + "](" + urllib.parse.quote(item["path"], safe="/") + ")" for item in retained]
    with (out / "笔记.md").open("a", encoding="utf-8") as stream:
        stream.write("\n\n## 导入原件\n\n" + " · ".join(links) + "\n")
        if meta.get("preparation_warnings"):
            stream.write("\n## 识别和预览待核对项\n\n" + "\n".join("- " + warning for warning in meta["preparation_warnings"]) + "\n")
    reader = out / "阅读.html"
    details = '<details style="max-width:1100px;margin:24px auto;padding:20px"><summary>导入原件与识别说明</summary><ul>'
    details += ''.join('<li><a href="' + html.escape(urllib.parse.quote(item["path"], safe="/"), quote=True) + '">' + html.escape(item["filename"]) + '</a></li>' for item in retained)
    details += '</ul>' + ''.join('<p>' + html.escape(warning) + '</p>' for warning in meta.get("preparation_warnings", []))
    if (out / "识别文字.md").is_file():
        details += '<p><a href="识别文字.md">识别与修订文字</a></p>'
    if (out / "识别记录.json").is_file():
        details += '<p><a href="识别记录.json">识别记录</a></p>'
    if (out / "识别用量.json").is_file():
        details += '<p><a href="识别用量.json">识别接口用量（含历史失败尝试）</a></p>'
    details += '</details>'
    reader.write_text(reader.read_text(encoding="utf-8").replace('</body>', details + '</body>'), encoding="utf-8")


def _export_recognized(project, out, meta, prepared, audio, pdf):
    from .archive import export_archive, validate_archive
    units, segments = load_features(prepared / "features.json")
    validate_binding(read_json(prepared / "archive_binding.json"), prepared, audio, pdf)
    chunks = [asdict(Chunk(s.segment_id, s.start, s.end, s.text)) for s in segments]
    predictions = [{"segment_id": c["segment_id"], "unit_id": None, "status": "unreviewed"} for c in chunks]
    uses_ai = bool(meta.get("preparation_uses_ai") or (prepared / "recognition_sources.json").is_file())
    mode = "recognized_sources_no_synthesis" if uses_ai else "corrected_sources_no_synthesis"
    notice = "原件、识别文字与手动笔记归档；已执行语音识别或 OCR，尚未生成重点归纳。" if uses_ai else "原件、用户修订文字与手动笔记归档；未调用 AI，修订内容仍需核对原件。"
    notes = {"overview": notice, "topics": [], "review_questions": [],
             "uncertainties": ["自动识别的文字仍需核对；浏览或播放不自动确认课件对应关系。"]}
    export_archive(out, meta["title"], audio, pdf, prepared, read_json(prepared / "source_map.json"),
                   chunks, predictions, notes, "使用者导入的原件；识别结果不是人工校对稿。", browse_pages=True)
    document = (out / "阅读.html").read_text(encoding="utf-8")
    document = document.replace("重点由本地模型整理。点击来源核对原页或录音；自动页面对应关系仍需复核。", notice)
    (out / "阅读.html").write_text(document, encoding="utf-8")
    manifest = read_json(out / "project.json")
    manifest.update(mode=mode, uses_ai=uses_ai, automatic_correspondence=False,
                    transcript_kind=meta.get("transcript_origin"), model_calls=None,
                    model_call_scope="识别记录中分别保留本机模型步骤与远程 API 尝试，未从缺失统计推断零用量")
    atomic_json(out / "project.json", manifest)
    _append_manual(out, meta["manual_notes"])
    checked = _apply_basic_review(out, project, meta, prepared, uses_ai=uses_ai) or validate_archive(out)
    _append_derived(out, project, meta)
    checked.update(mode=mode, uses_ai=uses_ai)
    atomic_json(out / "archive_checks.json", checked)
    return {"mode": mode, "checks": checked}


def export_project(project: Path, out: Path, *, language="zh-CN"):
    from .desktop_locale import normalize_language
    normalize_language(language)
    result = _export_project(project, out)
    if language == "en":
        from .desktop_english_archive import export_english_reader
        export_english_reader(project, out)
        result["reader_entry"] = "index.html"
    else:
        result["reader_entry"] = "阅读.html"
    return result


def _export_project(project: Path, out: Path):
    meta = read_json(project / "project.json")
    for source in meta["files"]:
        if file_hash(project / source["path"]) != source["sha256"]:
            raise ValueError("原件与导入时不一致，请重新导入。")
    prepared = project / "prepared"
    audio = project / meta["audio"] if meta["audio"] else None
    pdf = project / meta["pdf"] if meta["pdf"] else None
    active = meta.get("completed_run") or meta["latest_run"]
    run = project / "runs" / active if active else None
    if run and (run / "run_manifest.json").is_file():
        from .topic_output import export_topic_archive
        manifest = read_json(run / "run_manifest.json")
        if (manifest.get("complete") is not True
                or manifest.get("feature_sha256") != file_hash(prepared / "features.json")
                or manifest.get("binding_sha256") != file_hash(prepared / "archive_binding.json")
                or any(file_hash(run / name) != expected for name, expected in manifest["outputs"].items())):
            raise ValueError("整理结果与原件不一致，请重新整理后归档。")
        validate_binding(read_json(prepared / "archive_binding.json"), prepared, audio, pdf)
        checked = export_topic_archive(out, meta["title"], audio, pdf, prepared,
            load_review(run / "candidate_review.json"), read_json(run / "quality_notes.json"),
            read_json(run / "topic_notes.json"), "使用者导入的独立录音和课件；AI 主题草稿仍需核对。",
            chunks=read_json(run / "chunks.json")["chunks"])
        _append_manual(out, meta["manual_notes"])
        # Preserve subtitles, exact run accounting and the user's own notes too.
        if meta["transcript"]:
            source = project / meta["transcript"]
            shutil.copy2(source, out / "sources" / ("transcript" + source.suffix.lower()))
        shutil.copy2(run / "usage_report.json", out / "AI用量.json")
        _append_derived(out, project, meta)
        return {"mode": "ai_topics", "checks": checked}
    if meta["full_prepared"]:
        if (prepared / "recognition_sources.json").is_file() or (prepared / "corrections.json").is_file():
            return _export_recognized(project, out, meta, prepared, audio, pdf)
        from .basic_output import export_basic_archive
        checked = export_basic_archive(out, meta["title"], audio, pdf, prepared, meta["manual_notes"])
        checked = _apply_basic_review(out, project, meta, prepared) or checked
        _append_derived(out, project, meta)
        return {"mode": "basic_no_ai", "checks": checked}
    # Incomplete inputs still have a useful portable archive. No transcript or
    # AI success is manufactured just to satisfy the full-input reader format.
    out.mkdir(parents=True, exist_ok=False)
    sources = out / "sources"
    sources.mkdir()
    links = []
    for item in meta["files"]:
        source = project / item["path"]
        if file_hash(source) != item["sha256"]:
            raise ValueError("原件与导入时不一致，请重新导入。")
        target = sources / source.name
        shutil.copy2(source, target)
        links.append((source.name, target.relative_to(out).as_posix()))
    title = meta["title"]
    note = meta["manual_notes"]
    content = ["# " + title, "", "原件与手动笔记归档；未执行 AI 转写、匹配或主题归纳。", ""]
    content += ["- [" + name.replace("[", "\\[").replace("]", "\\]") + "](" + target.replace(" ", "%20") + ")" for name, target in links]
    if note.strip():
        content += ["", "## 我的笔记", "", note]
    (out / "笔记.md").write_text("\n".join(content), encoding="utf-8")
    controls = []
    for name, target in links:
        safe_target = html.escape(target, quote=True)
        controls.append('<li><a href="' + safe_target + '">' + html.escape(name) + '</a></li>')
        suffix = Path(name).suffix.lower()
        if suffix in {".wav", ".mp3", ".m4a"}:
            controls.append('<audio controls preload="metadata" src="' + safe_target + '"></audio>')
        elif suffix in {".png", ".jpg", ".jpeg"}:
            controls.append('<img alt="' + html.escape(name, quote=True) + '" src="' + safe_target + '">')
    document = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src \'self\' data:; media-src \'self\'; style-src \'unsafe-inline\'">'
        '<title>' + html.escape(title) + '</title><style>body{font-family:Segoe UI,Microsoft YaHei,sans-serif;max-width:1000px;margin:40px auto;padding:0 24px;color:#202124;background:#f6f8fc}main{background:white;padding:32px;border-radius:20px}a{color:#0b57d0}li{margin:12px 0}img{max-width:100%;display:block}audio{width:100%}.notes{white-space:pre-wrap;line-height:1.8}</style>'
        '<main><h1>' + html.escape(title) + '</h1><p>原件与手动笔记归档 · 未执行 AI</p><ul>' + ''.join(controls) + '</ul><h2>我的笔记</h2><div class="notes">' + html.escape(note) + '</div></main></html>')
    (out / "阅读.html").write_text(document, encoding="utf-8")
    atomic_json(out / "project.json", {"mode": "source_and_manual_no_ai", "uses_ai": False,
                                      "model_calls": 0, "files": [{"name": n, "path": p} for n, p in links]})
    if (prepared / "review.json").is_file():
        shutil.copy2(prepared / "review.json", out / "审阅记录.json")
    if meta.get("preparation_uses_ai"):
        manifest = read_json(out / "project.json")
        manifest.update(mode="recognized_sources_no_synthesis", uses_ai=True, model_calls=None)
        atomic_json(out / "project.json", manifest)
        for name in ("笔记.md", "阅读.html"):
            file = out / name
            file.write_text(file.read_text(encoding="utf-8").replace("未执行 AI 转写、匹配或主题归纳。", "已执行识别；尚未生成重点归纳，文字仍需核对。").replace("未执行 AI</p>", "已执行识别，尚未生成重点归纳</p>"), encoding="utf-8")
    _append_derived(out, project, meta)
    return {"mode": "recognized_sources_no_synthesis" if meta.get("preparation_uses_ai") else "source_and_manual_no_ai"}
