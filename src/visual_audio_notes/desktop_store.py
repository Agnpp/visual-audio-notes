"""Small desktop workspace over the model-free and source-grounded workflows.

The UI gets only public settings and contained workspace resource paths. Long
AI work runs in one background task; nothing imports research ML runtimes.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory
import threading
import urllib.parse
import uuid

from .basic import parse_transcript, prepare_basic
from .binding import create_binding, save_binding, validate_binding
from .core import Chunk, Segment, Unit, chunks_from_segments, file_hash, load_features, read_json
from .desktop_credentials import load_secret, save_secret
from .desktop_prepare import active_review_path, refresh_candidates
from .desktop_recognition import RecognitionSettings
from .ingest import extract_pdf
from .providers import ModelConnection, create_json_model
from .review import StaleReviewError, apply_decision, create_review, load_review, save_review, source_fingerprint
from .task_runtime import TaskBudgetExceeded, TaskCancelled, TaskModel, TaskRequestFailed, atomic_json, usage_report


_ENV_LOCK = threading.RLock()
_SECRET_ENV = "VAN_DESKTOP_RUNTIME_KEY"
_KINDS = {".pdf": "document", ".png": "image", ".jpg": "image", ".jpeg": "image",
          ".wav": "audio", ".mp3": "audio", ".m4a": "audio", ".srt": "transcript", ".vtt": "transcript",
          ".pptx": "office", ".docx": "office", ".xlsx": "office", ".vsdx": "office",
          ".ppt": "office", ".doc": "office", ".xls": "office", ".vsd": "office"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _review_version(meta, review):
    return f'{meta.get("preparation_revision", 0)}:{review["source_revision"]}:{review["source_fingerprint"]}'


def _safe_error(error):
    if isinstance(error, TaskCancelled):
        return "已停止整理，原件和已完成步骤已保留，可以继续。"
    if isinstance(error, TaskBudgetExceeded):
        return "已达到本轮调用上限，已完成的步骤已保存，可以继续整理。"
    if isinstance(error, TaskRequestFailed) and error.response_error:
        if error.response_error == "output_limit":
            return "模型输出达到上限。已保存完成的步骤，可分批整理或调整模型设置后再试。"
        return "模型未返回可用的归纳结果。已保存完成的步骤，可检查模型设置后再试。"
    from .recognition import RecognitionError
    if isinstance(error, RecognitionError):
        return str(error)
    return "整理未完成，已保留原件和已完成步骤。请检查连接、密钥及模型设置后再试。"


@contextmanager
def _credential(secret):
    with _ENV_LOCK:
        previous = os.environ.get(_SECRET_ENV)
        try:
            if secret:
                os.environ[_SECRET_ENV] = secret
            else:
                os.environ.pop(_SECRET_ENV, None)
            yield
        finally:
            if previous is None:
                os.environ.pop(_SECRET_ENV, None)
            else:
                os.environ[_SECRET_ENV] = previous


class DesktopStore:
    def __init__(self, root: Path, resource_root: Path | None = None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "projects").mkdir(exist_ok=True)
        from .desktop_append import recover_pending_appends
        recover_pending_appends(self.root)
        self.resource_root = Path(resource_root or Path(__file__).resolve().parents[2])
        self._lock = threading.RLock()
        self._thread = None
        self._secret = ""
        self._models = []
        self._cancel = threading.Event()
        self.recognition = RecognitionSettings(self.root)
        self._preferences = read_json(self.root / "preferences.json") if (self.root / "preferences.json").is_file() else {}
        self._settings = {"provider": "deepseek", "model": "deepseek-flash",
                          "endpoint": "https://api.deepseek.com", "thinking": "off", "remember_key": False}
        if (self.root / "connection.json").is_file():
            self._settings.update(read_json(self.root / "connection.json"))
        if self._settings.get("remember_key"):
            try:
                self._secret = load_secret(self.root / "credential.bin")
            except (ValueError, OSError, UnicodeError):
                self._settings["remember_key"] = False
        self._job = read_json(self.root / "job.json") if (self.root / "job.json").is_file() else None
        if self._job and self._job.get("state") == "running":
            self._job.update(state="failed", stage="interrupted", message="上次整理已中断，已完成步骤保留，点击整理可以继续。")
            self._save_job()
            try:
                interrupted = self._path(self._job["project_id"]) / "project.json"
                meta = read_json(interrupted)
                meta["status"] = "failed"
                atomic_json(interrupted, meta)
            except (ValueError, OSError, KeyError):
                pass

    def _path(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"p_[a-f0-9]{24}", identifier):
            raise ValueError("找不到这份资料，请刷新列表。")
        path = self.root / "projects" / identifier
        if not (path / "project.json").is_file():
            raise ValueError("找不到这份资料，请刷新列表。")
        return path

    def _secret_value(self):
        if self._secret:
            return self._secret
        # A machine-wide DeepSeek credential must not silently follow a user
        # changing this connection to a different host or a local service.
        if (self._settings["provider"] == "deepseek"
                and urllib.parse.urlsplit(self._connection().endpoint).hostname == "api.deepseek.com"):
            return os.environ.get("DEEPSEEK_API_KEY", "")
        return ""

    def _connection(self, values=None):
        values = values or self._settings
        thinking = values.get("thinking", "off")
        if thinking not in {"off", "low", "high", "max"}:
            raise ValueError("请选择关闭思考、低、高或最高。")
        return ModelConnection(provider=values["provider"], model=values["model"],
            endpoint=values.get("endpoint") or None, api_key_env=_SECRET_ENV,
            thinking="disabled" if thinking == "off" else "enabled",
            reasoning_effort="high" if thinking == "off" else thinking,
            timeout_seconds=600)

    def public_connection(self):
        return {**self._settings, "key_present": bool(self._secret_value()),
                "key_configured": bool(self._secret_value()),
                "configured": self._settings["provider"] == "local_llamacpp" or bool(self._secret_value()),
                "models": deepcopy(self._models), "capabilities": ["text_generation"],
                "thinking_options": ["off", "low", "high", "max"] if self._settings["provider"] == "deepseek" else []}

    def save_connection(self, values):
        if not isinstance(values, dict) or set(values) - {
                "provider", "model", "endpoint", "thinking", "api_key", "remember_key", "clear_key"}:
            raise ValueError("连接设置格式不正确。")
        with self._lock:
            if self._job and self._job["state"] == "running":
                raise ValueError("正在整理，请在本次任务结束后修改连接。")
            selected = {**self._settings, **{k: values[k] for k in ("provider", "model", "endpoint", "thinking", "remember_key") if k in values}}
            if type(selected["remember_key"]) is not bool:
                raise ValueError("记住密钥选项无效。")
            normalized = self._connection(selected)  # Validate before writing anything.
            previous = self._connection()
            changed_target = (normalized.provider, normalized.endpoint) != (previous.provider, previous.endpoint)
            secret = "" if values.get("clear_key") else values.get("api_key") or ("" if changed_target else self._secret)
            if not isinstance(secret, str) or (secret and (secret != secret.strip() or any(ord(c) < 33 or ord(c) > 126 for c in secret))):
                raise ValueError("密钥不能包含空格或换行。")
            if selected["remember_key"] and secret:
                save_secret(self.root / "credential.bin", secret)
            elif (self.root / "credential.bin").exists():
                (self.root / "credential.bin").unlink()
            self._secret = secret
            self._settings = selected
            self._models = []
            atomic_json(self.root / "connection.json", selected)
            return self.public_connection()

    def discover_models(self, values=None):
        from .provider_discovery import list_connection_models
        with self._lock:
            if values is not None and (not isinstance(values, dict) or set(values) - {
                    "provider", "model", "endpoint", "thinking", "api_key", "remember_key", "clear_key"}):
                raise ValueError("连接设置格式不正确。")
            values = values or {}
            selected = {**self._settings, **{key: values[key] for key in
                        ("provider", "model", "endpoint", "thinking") if key in values}}
            if not selected["model"]:
                # Model discovery does not send this field; the constructor
                # still requires a valid public connection shape.
                selected["model"] = "local-service" if selected["provider"] == "local_llamacpp" else "deepseek-flash"
            connection = self._connection(selected)
            current = self._connection()
            same_target = (connection.provider, connection.endpoint) == (current.provider, current.endpoint)
            secret = "" if values.get("clear_key") else values.get("api_key") or (self._secret_value() if same_target else "")
            if not isinstance(secret, str) or (secret and (secret != secret.strip() or any(ord(c) < 33 or ord(c) > 126 for c in secret))):
                raise ValueError("密钥不能包含空格或换行。")
        with _credential(secret):
            try:
                result = list_connection_models(connection)
            except ValueError:
                raise ValueError("无法读取模型列表，请检查连接地址和密钥。") from None
        with self._lock:
            if same_target:
                self._models = result["models"]
        return result

    def state(self):
        with self._lock:
            projects = []
            for path in sorted((self.root / "projects").glob("p_*/project.json"), reverse=True):
                try:
                    projects.append(self.project(path.parent.name))
                except (ValueError, OSError, KeyError):
                    continue
            projects.sort(key=lambda p: p["created_at"], reverse=True)
            return {"projects": projects, "connection": self.public_connection(), "preferences": deepcopy(self._preferences),
                    "recognition": self.recognition.public(), "job": deepcopy(self._job)}

    def save_preferences(self, values):
        from .desktop_locale import normalize_language
        if not isinstance(values, dict) or set(values) != {"language"}:
            raise ValueError("请选择简体中文或英文。")
        language = normalize_language(values["language"])
        with self._lock:
            if self._job and self._job["state"] == "running":
                raise ValueError("请先结束当前任务，再切换语言。")
            self._preferences = {"language": language}
            atomic_json(self.root / "preferences.json", self._preferences)
            return deepcopy(self._preferences)

    def save_recognition(self, values):
        with self._lock:
            if self._job and self._job["state"] == "running":
                raise ValueError("正在整理，请结束本次任务后修改识别方式。")
            return self.recognition.save(values)

    def download_model(self):
        with self._lock:
            if self._job and self._job["state"] == "running":
                raise ValueError("已有任务进行中，请完成后再准备模型。")
            if not self.recognition.public()["asr"]["local_engine_available"]:
                raise ValueError("当前轻量包没有本机语音引擎，请选择语音 API 或使用完整识别版。")
            self._cancel.clear()
            self._job = {"id": "download_" + uuid.uuid4().hex[:12], "project_id": None,
                         "state": "running", "stage": "download_model", "message": "正在准备本机语音识别模型…", "progress": 0}
            self._save_job()
            self._thread = threading.Thread(target=self._download_model, daemon=True)
            self._thread.start()
            return deepcopy(self._job)

    def _download_model(self):
        from .model_assets import download_asr_model
        try:
            download_asr_model(self.root / "models", progress=lambda message, fraction=None:
                self._progress(None, "download_model", message, fraction), cancel_event=self._cancel)
            state, message = "complete", "语音识别模型已准备好，以后无需重复下载。"
        except BaseException:
            state = "cancelled" if self._cancel.is_set() else "failed"
            message = "已停止下载，下次可以继续。" if self._cancel.is_set() else "模型准备未完成，请检查网络后继续；已下载部分会保留。"
        with self._lock:
            self._job.update(state=state, message=message)
            self._save_job()

    def cancel(self):
        with self._lock:
            if self._job and self._job["state"] == "running":
                self._cancel.set()
                self._job["cancel_requested"] = True
                self._job["message"] = "正在停止；当前识别或请求结束后不再新增调用，已完成步骤会保留。"
                self._save_job()
            return deepcopy(self._job)

    def import_files(self, paths, title="", *, language="zh-CN"):
        if not isinstance(paths, list) or not paths or len(paths) > 200:
            raise ValueError("请选择课件、图片、录音或已有转写文件。")
        paths = [Path(p).resolve() for p in paths]
        if len({p.name.casefold() for p in paths}) != len(paths):
            raise ValueError("同一次导入的文件名不能重复，请先区分名称。")
        kinds = {}
        total = 0
        for path in paths:
            kind = _KINDS.get(path.suffix.lower())
            if not kind or not path.is_file() or path.stat().st_size == 0:
                raise ValueError("请导入 PDF、图片、Office 文档、录音或已有 SRT/VTT 转写。")
            total += path.stat().st_size
            kinds.setdefault(kind, []).append(path)
        if total > 2 * 1024 ** 3:
            raise ValueError("这批资料超过 2 GB，请分批导入。")
        if any(len(kinds.get(k, [])) > 1 for k in ("audio", "transcript")):
            raise ValueError("一份笔记先使用一段录音和一份已有转写；课件和图片可以一起导入。")
        # Format validation happens before a workspace folder is created.
        if kinds.get("transcript"):
            parse_transcript(kinds["transcript"][0])
        for document_path in kinds.get("document", []):
            import pypdfium2 as pdfium
            try:
                with pdfium.PdfDocument(document_path) as document:
                    if not len(document):
                        raise ValueError()
            except Exception:
                raise ValueError("PDF 无法读取，可能已损坏或需要密码。") from None
        if kinds.get("image"):
            from PIL import Image
            for path in kinds["image"]:
                try:
                    with Image.open(path) as image:
                        image.verify()
                except Exception:
                    raise ValueError("有一张图片无法读取，请检查文件。") from None
        hashes = {path: file_hash(path) for path in paths}
        identity = sorted((p.name, _KINDS[p.suffix.lower()], hashes[p]) for p in paths)
        with self._lock:
            for marker in (self.root / "projects").glob("p_*/project.json"):
                existing = read_json(marker)
                if sorted((f["name"], f["kind"], f["sha256"]) for f in existing["files"]) == identity:
                    return self.project(existing["id"])
            return self._import_new(paths, kinds, hashes, title, language=language)

    def add_files(self, identifier, paths):
        from .desktop_append import append_files
        with self._lock:
            if self._job and self._job["state"] == "running":
                raise ValueError("正在整理，请停止或等待完成后再添加资料。")
            return append_files(self, identifier, paths)

    def _import_new(self, paths, kinds, hashes, title, *, language="zh-CN"):
        identifier = "p_" + uuid.uuid4().hex[:24]
        project_dir = self.root / "projects" / identifier
        originals = project_dir / "originals"
        originals.mkdir(parents=True)
        files = []
        for path in paths:
            copied = originals / path.name
            shutil.copy2(path, copied)
            if file_hash(copied) != hashes[path]:
                raise ValueError("导入时原文件发生变化，请重新导入。")
            files.append({"name": path.name, "kind": _KINDS[path.suffix.lower()],
                          "path": copied.relative_to(project_dir).as_posix(), "sha256": hashes[path]})
        from .desktop_documents import assemble_documents, apply_document_text
        assembled = assemble_documents(project_dir, files, language=language)
        pdf = assembled["pdf"]
        audio = next((project_dir / f["path"] for f in files if f["kind"] == "audio"), None)
        transcript = next((project_dir / f["path"] for f in files if f["kind"] == "transcript"), None)
        prepared = project_dir / "prepared"
        full = bool(pdf and audio and transcript)
        if full:
            prepare_basic(pdf, audio, transcript, prepared)
        else:
            units, source_map = extract_pdf(pdf, prepared / "pages", 20261101) if pdf else ([], {})
            segments = parse_transcript(transcript) if transcript else []
            atomic_json(prepared / "features.json", {"units": [asdict(u) for u in units], "segments": [asdict(s) for s in segments]})
            atomic_json(prepared / "source_map.json", source_map)
        apply_document_text(prepared, assembled)
        from .desktop_time import check_times
        imported_content = read_json(prepared / "features.json")
        time_checks = check_times(audio, imported_content["segments"])
        atomic_json(prepared / "time_checks.json", time_checks)
        if full:
            save_binding(prepared / "archive_binding.json", create_binding(prepared, audio, pdf))
        content = read_json(prepared / "features.json")
        if content["units"] and content["segments"]:
            from .desktop_prepare import refresh_candidates
            refresh_candidates(prepared)
        meta = {"id": identifier, "title": str(title).strip()[:120] or paths[0].stem, "document_preview_language": language,
                "created_at": _now(), "status": "ready", "files": files, "manual_notes": "",
                "full_prepared": full, "pdf": pdf.relative_to(project_dir).as_posix() if pdf else None,
                "audio": audio.relative_to(project_dir).as_posix() if audio else None,
                "transcript": transcript.relative_to(project_dir).as_posix() if transcript else None,
                "latest_run": None, "completed_run": None, "archived_at": None, "export_path": None,
                "transcript_origin": "user_provided_transcript" if transcript else None,
                "preparation_warnings": [warning for item in assembled["conversions"] for warning in item["warnings"]] +
                    ([time_checks["message"]] if time_checks.get("within_audio_bounds") is False else [])}
        atomic_json(project_dir / "project.json", meta)
        return self.project(identifier)

    def project(self, identifier):
        path = self._path(identifier)
        meta = read_json(path / "project.json")
        features = read_json(path / "prepared/features.json")
        source_map = read_json(path / "prepared/source_map.json")
        text_by_id = {unit["unit_id"]: unit["text"] for unit in features["units"]}
        pages = [{"unit_id": uid, "page": locator["page"], "text": text_by_id[uid],
                  **{key: locator[key] for key in ("original_filename", "original_page", "preview_kind", "preview_warnings", "text_origin") if key in locator},
                  "image": f"projects/{identifier}/prepared/pages/{locator['image']}"}
                 for uid, locator in sorted(source_map.items(), key=lambda pair: pair[1]["page"])]
        missing = []
        if meta["audio"] and not features["segments"]:
            missing.append("speech_to_text")
        if any(not text_by_id[uid].strip() and not locator.get("ocr_completed") and not locator.get("ocr_unavailable") for uid, locator in source_map.items()):
            missing.append("ocr")
        if not self.public_connection()["configured"]:
            missing.append("text_generation")
        topics = {"audio": [], "courseware": []}
        usage = None
        review_items, chunks, review_version = [], [], None
        run = path / "runs" / meta["latest_run"] if meta["latest_run"] else None
        completed = path / "runs" / meta["completed_run"] if meta.get("completed_run") else None
        topics_run = completed or run
        if topics_run and (topics_run / "topic_notes.json").is_file():
            result = read_json(topics_run / "topic_notes.json")
            topics = {origin: result[origin + "_topics"] for origin in topics}
        if run and (run / "usage_report.json").is_file():
            usage = read_json(run / "usage_report.json")
        preparation_report = path / "task_preparations" / ((meta["latest_run"] or "none") + ".json")
        if preparation_report.is_file():
            preparation_usage = read_json(preparation_report).get("usage", {})
            usage = {**(usage or {}), "recognition": preparation_usage,
                     "total_api_attempts": (usage or {}).get("api_attempts", 0) + preparation_usage.get("api_attempts", 0)}
        review_path = active_review_path(path, meta)
        if review_path.is_file():
            review = load_review(review_path)
            review_items = review["items"]
            chunks = read_json(review_path.parent / "chunks.json")["chunks"]
            review_version = _review_version(meta, review)
        recognition = self.recognition.public()
        needs_asr = "speech_to_text" in missing
        needs_ocr = "ocr" in missing
        needs_download = needs_asr and recognition["asr"]["provider"] == "local_whisper" and not recognition["asr"]["local_model"]["ready"]
        labels = {"ready": "资料已就绪", "running": "正在整理", "ai_ready": "主题草稿待核对", "prepared": "识别与候选已整理", "failed": "待继续整理"}
        candidate_metrics = read_json(path / "prepared/candidate_metrics.json") if (path / "prepared/candidate_metrics.json").is_file() else {}
        if topics_run and (topics_run / "candidate_metrics.json").is_file():
            candidate_metrics = read_json(topics_run / "candidate_metrics.json")
        return {"id": identifier, "title": meta["title"], "created_at": meta["created_at"],
                "status": meta["status"], "status_label": labels.get(meta["status"], "资料已就绪"),
                "summary": "已整理主题，保留原文来源" if any(topics.values()) else "原件已保留，可阅读、回听和手动记录",
                "files": [{"name": f["name"], "kind": f["kind"]} for f in meta["files"]],
                "pages": pages, "segments": features["segments"], "manual_notes": meta["manual_notes"],
                "topics": topics, "counts": {"pages": len(pages), "segments": len(features["segments"]),
                    "pending": sum(item["status"] == "needs_review" for item in review_items) if review_items else len(features["segments"])},
                "missing_capabilities": missing, "can_ai": bool(meta["full_prepared"] and self.public_connection()["configured"]),
                "can_process": bool(meta["audio"] or pages), "needs_model_download": needs_download,
                "pipeline_needs": {"asr": needs_asr, "ocr": needs_ocr},
                "preparation_warnings": meta.get("preparation_warnings", []),
                "transcript_origin": meta.get("transcript_origin") or "user_provided_transcript",
                "candidate_method": candidate_metrics.get("method", "lexical"),
                "candidate_uses_ai_derived_terms": candidate_metrics.get("uses_ai_derived_terms", False),
                "usage": usage, "audio_path": f"projects/{identifier}/{meta['audio']}" if meta["audio"] else None,
                "pdf_path": f"projects/{identifier}/{meta['pdf']}" if meta["pdf"] else None,
                "archived_at": meta.get("archived_at"), "has_ai": any(topics.values()),
                "export_path": meta.get("export_path"),
                "topics_from_previous_run": bool(completed and meta.get("completed_run") != meta.get("latest_run")),
                "review_items": review_items, "review_version": review_version, "chunks": chunks}

    def save_notes(self, identifier, text):
        if not isinstance(text, str) or len(text) > 1000000:
            raise ValueError("笔记内容过长或格式不正确。")
        with self._lock:
            path = self._path(identifier)
            meta = read_json(path / "project.json")
            meta["manual_notes"] = text
            atomic_json(path / "project.json", meta)
            return self.project(identifier)

    def edit_text(self, identifier, kind, source_id, text, *, start=None, end=None):
        from .desktop_prepare import edit_source_text
        with self._lock:
            if self._job and self._job["state"] == "running":
                raise ValueError("请先停止本次整理，再修订文字。")
            path = self._path(identifier)
            edit_source_text(path, kind, source_id, text, start=start, end=end)
            return self.project(identifier)

    def _save_job(self):
        atomic_json(self.root / "job.json", self._job)

    def _progress(self, identifier, stage, message, fraction=None):
        with self._lock:
            if self._job and self._job["project_id"] == identifier:
                self._job.update(stage=stage, message=message)
                if isinstance(fraction, (int, float)) and not isinstance(fraction, bool):
                    self._job["progress"] = max(0, min(1, fraction))
                self._save_job()

    def start_ai(self, identifier, refresh=False, *, language="zh-CN"):
        from .desktop_locale import normalize_language
        language = normalize_language(language)
        with self._lock:
            if self._job and self._job["state"] == "running":
                raise ValueError("已有一份资料正在整理，完成后再开始下一份。")
            detail = self.project(identifier)
            if not detail["can_process"]:
                raise ValueError("请先导入课件、图片或录音。")
            capabilities = self.recognition.public()
            if detail["pipeline_needs"]["asr"] and capabilities["asr"]["provider"] == "local_whisper" and not capabilities["asr"]["local_engine_available"]:
                raise ValueError("当前软件包没有本机语音引擎，请使用完整识别版，或在语音识别设置中选择 API。")
            if detail["needs_model_download"]:
                raise ValueError("自动转写需要先准备本机语音模型，也可以在语音识别设置中选择 API。")
            if detail["pipeline_needs"]["ocr"] and capabilities["ocr"]["provider"] == "local_rapidocr" and not capabilities["ocr"]["local_engine_available"]:
                raise ValueError("当前软件包没有本机 OCR 引擎，请使用完整识别版，或在图片识字设置中选择视觉 API。")
            if detail["pipeline_needs"]["asr"] and capabilities["asr"]["provider"] == "openai_compatible_asr" and not capabilities["asr"]["key_configured"]:
                raise ValueError("请在语音识别设置中填写你所选语音服务的密钥。")
            if detail["pipeline_needs"]["ocr"] and capabilities["ocr"]["provider"] == "deepseek_vision" and not capabilities["ocr"]["key_configured"]:
                raise ValueError("请在图片识字设置中填写所选视觉连接的密钥。")
            if type(refresh) is not bool:
                raise ValueError("重新生成选项无效。")
            path = self._path(identifier)
            meta = read_json(path / "project.json")
            previous_review = active_review_path(path, meta)
            prepared = path / "prepared"
            if not meta.get("completed_run") and previous_review.parent != prepared:
                # A failed first run can still contain human decisions. Keep
                # them reachable if a retry stops before producing candidates.
                refresh_candidates(prepared, previous_review)
            run_id = "r_" + uuid.uuid4().hex[:24]
            self._cancel.clear()
            meta.update(status="running", latest_run=run_id)
            atomic_json(path / "project.json", meta)
            self._job = {"id": run_id, "project_id": identifier, "state": "running", "stage": "prepare", "message": "正在准备资料，已有文字和转写会复用…"}
            self._save_job()
            self._thread = threading.Thread(target=self._run_ai, args=(identifier, run_id, self._connection(), self._secret_value(), refresh, language), daemon=True)
            self._thread.start()
            return deepcopy(self._job)

    def _run_ai(self, identifier, run_id, connection, secret, refresh, language="zh-CN"):
        from .desktop_ai import run_ai
        from .desktop_prepare import prepare_recognition
        path = self._path(identifier)
        generated_notes = False
        reserved_calls = reserved_output = 0
        def reserve_request(purpose, maximum_output):
            nonlocal reserved_calls, reserved_output
            if self._cancel.is_set():
                raise TaskCancelled("已停止后续请求。")
            if reserved_calls >= 32 or reserved_output + maximum_output > 128000:
                raise TaskBudgetExceeded("识别与归纳合计达到本轮调用上限。")
            reserved_calls += 1
            reserved_output += maximum_output
        attempts_before = set((path / ".recognition-cache/attempts").glob("*.json"))
        preparation_path = path / "task_preparations" / (run_id + ".json")
        try:
            preparation = prepare_recognition(path, self.recognition,
                lambda stage, message, fraction=None: self._progress(identifier, stage, message, fraction),
                cancel_event=self._cancel, request_guard=reserve_request)
            calls = (preparation.get("asr") or {}).get("api_calls", []) + (preparation.get("ocr") or {}).get("api_calls", [])
            atomic_json(preparation_path,
                        {"report": preparation, "usage": usage_report(calls)})
            if self._cancel.is_set():
                raise TaskCancelled("已停止整理。")
            current = read_json(path / "project.json")
            if current["full_prepared"] and self.public_connection()["configured"]:
                if reserved_calls >= 32 or reserved_output >= 128000:
                    raise TaskBudgetExceeded("识别阶段达到本轮调用上限，已完成识别会复用。")
                with _credential(secret):
                    run_ai(path, self.resource_root, run_id, connection, refresh,
                           lambda stage, message: self._progress(identifier, stage, message), cancel_event=self._cancel,
                            max_calls=32 - reserved_calls, max_output_tokens=128000 - reserved_output, language=language)
                generated_notes = True
            state = "complete"
            if generated_notes:
                message = "主题草稿已整理，请核对录音原话与课件补充。"
            elif not current["full_prepared"]:
                missing_sources = []
                if not current.get("pdf"):
                    missing_sources.append("课件或图片")
                if not current.get("audio"):
                    missing_sources.append("原录音")
                if not current.get("transcript"):
                    missing_sources.append("可用转写")
                message = "本次读取或识别已完成；请用添加资料补齐" + "、".join(missing_sources) + "，再归纳重点。"
            else:
                message = "识别与内容候选已整理；配置文字 AI 后可以继续归纳重点。"
        except BaseException as error:
            state = "cancelled" if self._cancel.is_set() else "failed"
            message = "已停止整理，原件和已完成步骤已保留，可以继续。" if state == "cancelled" else _safe_error(error)
        finally:
            # Failed or cancelled ASR/OCR may already be billed. The adapter
            # journals sanitized attempts independently of a successful result.
            new_attempts = sorted(set((path / ".recognition-cache/attempts").glob("*.json")) - attempts_before)
            if new_attempts:
                payload = read_json(preparation_path) if preparation_path.is_file() else {"report": {"complete": False}}
                payload["usage"] = usage_report([read_json(marker) for marker in new_attempts])
                atomic_json(preparation_path, payload)
        with self._lock:
            meta = read_json(path / "project.json")
            meta["status"] = "ai_ready" if generated_notes else "prepared" if state == "complete" else "failed"
            if state == "complete" and generated_notes:
                meta["completed_run"] = run_id
            atomic_json(path / "project.json", meta)
            self._job.update(state=state, stage=state, message=message)
            self._save_job()

    def decide(self, identifier, segment_id, unit_ids, decision="match", *,
               expected_version=None, expected_revision=None):
        with self._lock:
            if self._job and self._job["project_id"] == identifier and self._job["state"] == "running":
                raise StaleReviewError()
            path = self._path(identifier)
            meta = read_json(path / "project.json")
            review_path = active_review_path(path, meta)
            if not review_path.is_file():
                if expected_version is not None:
                    raise StaleReviewError()
                raise ValueError("手动对应需要课件和已有转写；无需配置 AI。")
            features = read_json(path / "prepared/features.json")
            chunks_path = review_path.parent / "chunks.json"
            fingerprint = source_fingerprint([Unit.from_wire(u) for u in features["units"]],
                [Chunk(**chunk) for chunk in read_json(chunks_path)["chunks"]])
            review = load_review(review_path, expected_fingerprint=fingerprint)
            if expected_version is not None:
                item = next((item for item in review["items"] if item["segment_id"] == segment_id), None)
                if (expected_version != _review_version(meta, review) or item is None
                        or expected_revision != item["revision"]):
                    raise StaleReviewError()
            reviewed = apply_decision(review, segment_id, unit_ids, decision=decision,
                reviewer="desktop-user", expected_fingerprint=review["source_fingerprint"])
            save_review(review_path, reviewed)
            return self.project(identifier)

    def export_project(self, identifier, destination, *, language="zh-CN"):
        from .desktop_ai import export_project
        from .desktop_locale import normalize_language
        language = normalize_language(language)
        with self._lock:
            path = self._path(identifier)
            if self._job and self._job["state"] == "running" and self._job["project_id"] == identifier:
                raise ValueError("正在整理，请完成后再保存归档。")
            destination = Path(destination).resolve()
            if destination.exists() or destination.is_relative_to(path) or path.is_relative_to(destination):
                raise ValueError("请选择一个新的归档文件夹，不能覆盖原资料或已有文件夹。")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(prefix=".van-", dir=destination.parent) as temporary:
                staging = Path(temporary) / "archive"
                result = export_project(path, staging, language=language)
                if destination.exists():
                    raise ValueError("请选择一个新的归档文件夹，不能覆盖原资料或已有文件夹。")
                staging.rename(destination)
            meta = read_json(path / "project.json")
            meta["archived_at"] = _now()
            meta["export_path"] = str(destination)
            atomic_json(path / "project.json", meta)
            return {"path": str(destination), **result}
