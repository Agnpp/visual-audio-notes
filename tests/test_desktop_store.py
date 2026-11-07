import json
import os
from pathlib import Path
import subprocess
import sys
import time
import wave

import pytest

from visual_audio_notes.core import file_hash, read_json
from visual_audio_notes.desktop_store import DesktopStore


def pdf_file(path):
    stream = b"BT /F1 18 Tf 30 100 Td (List concatenation returns a new list.) Tj ET"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 500 160] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"]
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(data)


@pytest.fixture
def inputs(tmp_path):
    sources = tmp_path / "用户原件"
    sources.mkdir()
    pdf, audio, transcript = sources / "课堂 课件.pdf", sources / "录音.wav", sources / "速记.srt"
    pdf_file(pdf)
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * 16000)
    transcript.write_text("1\n00:00:00,100 --> 00:00:00,900\nList concatenation returns a new list and leaves the original unchanged.\n", encoding="utf-8")
    return pdf, audio, transcript


def test_real_import_notes_export_restart_without_model_or_source_changes(tmp_path, inputs, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    before = {p: file_hash(p) for p in inputs}
    store = DesktopStore(tmp_path / "工作 空间")
    result = store.import_files(list(inputs), "中文课堂")
    assert result["title"] == "中文课堂"
    assert result["counts"]["pages"] == 1 and result["counts"]["segments"] == 1
    assert "speech_to_text" not in result["missing_capabilities"]
    assert result["can_ai"] is False
    assert (store.root / result["pages"][0]["image"]).is_file()
    store.save_notes(result["id"], "我的笔记 <script>不能执行</script>")
    out = tmp_path / "可归档成果"
    exported = store.export_project(result["id"], out)
    assert exported["mode"] == "basic_no_ai"
    assert "我的笔记" in (out / "笔记.md").read_text(encoding="utf-8")
    assert "&lt;script&gt;不能执行&lt;/script&gt;" in (out / "阅读.html").read_text(encoding="utf-8")
    restored = DesktopStore(store.root)
    assert restored.project(result["id"])["manual_notes"].startswith("我的笔记")
    assert restored.project(result["id"])["archived_at"]
    assert restored.project(result["id"])["export_path"] == str(out.resolve())
    assert {p: file_hash(p) for p in inputs} == before


def test_duplicate_import_preserves_existing_notes_and_does_not_make_new_project(tmp_path, inputs):
    store = DesktopStore(tmp_path / "workspace")
    first = store.import_files(list(inputs), "保留我的标题")
    store.save_notes(first["id"], "已经整理的笔记")
    again = store.import_files(list(reversed(inputs)), "不可覆盖旧标题")
    assert again["id"] == first["id"]
    assert again["title"] == "保留我的标题" and again["manual_notes"] == "已经整理的笔记"
    assert len(store.state()["projects"]) == 1


def test_manual_correspondence_without_ai_is_persisted_and_controls_archive_mapping(tmp_path, inputs, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    store = DesktopStore(tmp_path / "workspace")
    project = store.import_files(list(inputs))
    assert not project["can_ai"] and project["counts"]["pending"] == 1
    cue, page = project["chunks"][0], project["pages"][0]
    updated = store.decide(project["id"], cue["segment_id"], [page["unit_id"]])
    assert updated["counts"]["pending"] == 0
    restored = DesktopStore(store.root).project(project["id"])
    assert restored["review_items"][0]["status"] == "confirmed"
    out = tmp_path / "manual-archive"
    exported = store.export_project(project["id"], out)
    manifest = read_json(out / "project.json")
    assert manifest["timeline"][0]["unit_id"] == page["unit_id"]
    assert manifest["automatic_correspondence"] is False
    assert exported["checks"]["human_confirmed_single_page_mappings"] == 1
    assert read_json(out / "审阅记录.json")["items"][0]["review"]["actor"] == "human"
    assert "本段课件对应关系由使用者人工确认" in (out / "阅读.html").read_text(encoding="utf-8")


def test_changed_imported_original_refuses_export_before_creating_destination(tmp_path, inputs):
    store = DesktopStore(tmp_path / "workspace")
    project = store.import_files(list(inputs))
    meta = read_json(store.root / "projects" / project["id"] / "project.json")
    imported_subtitle = store.root / "projects" / project["id"] / meta["transcript"]
    imported_subtitle.write_text("changed later", encoding="utf-8")
    with pytest.raises(ValueError, match="不一致"):
        store.export_project(project["id"], tmp_path / "must-not-exist")
    assert not (tmp_path / "must-not-exist").exists()


def test_missing_transcript_still_supports_preview_manual_archive(tmp_path, inputs):
    store = DesktopStore(tmp_path / "workspace")
    result = store.import_files(list(inputs[:2]))
    assert "speech_to_text" in result["missing_capabilities"]
    assert result["segments"] == [] and not result["can_ai"]
    with pytest.raises(ValueError, match="自动转写"):
        store.start_ai(result["id"])
    store.save_notes(result["id"], "先保留原件再补转写")
    exported = store.export_project(result["id"], tmp_path / "archive")
    assert exported["mode"] == "source_and_manual_no_ai"
    assert read_json(tmp_path / "archive/project.json")["uses_ai"] is False


def test_import_invalid_input_leaves_no_project_directory(tmp_path, inputs):
    store = DesktopStore(tmp_path / "workspace")
    inputs[2].write_text("invalid cue", encoding="utf-8")
    with pytest.raises(ValueError):
        store.import_files(list(inputs))
    assert not list((store.root / "projects").iterdir())


def test_project_id_and_export_destination_cannot_escape_or_overwrite(tmp_path, inputs):
    store = DesktopStore(tmp_path / "workspace")
    result = store.import_files(list(inputs))
    with pytest.raises(ValueError):
        store.project("../../outside")
    with pytest.raises(ValueError):
        store.export_project(result["id"], store.root / "projects" / result["id"] / "archive")
    out = tmp_path / "existing"
    out.mkdir()
    with pytest.raises(ValueError):
        store.export_project(result["id"], out)


def test_memory_key_never_written_or_returned_and_env_is_restored(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    store = DesktopStore(tmp_path / "workspace")
    key = "fixture-desktop-key-not-real"
    result = store.save_connection({"api_key": key, "thinking": "high"})
    assert result["key_configured"] and "api_key" not in result
    assert key not in json.dumps(store.state())
    assert key not in (store.root / "connection.json").read_text(encoding="utf-8")
    assert not (store.root / "credential.bin").exists()
    assert not DesktopStore(store.root).public_connection()["key_configured"]
    with pytest.raises(ValueError):
        store.save_connection({"api_key": key + "\n"})


def test_secret_does_not_silently_follow_a_different_api_host(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-machine-key")
    store = DesktopStore(tmp_path / "workspace")
    assert store.public_connection()["key_configured"]
    store.save_connection({"api_key": "fixture-entered-key"})
    changed = store.save_connection({"endpoint": "https://custom-provider.example"})
    assert not changed["key_configured"]
    assert store._secret_value() == ""


def test_model_discovery_accepts_unsaved_local_endpoint_without_model_name(tmp_path, monkeypatch):
    from visual_audio_notes import provider_discovery
    store = DesktopStore(tmp_path / "workspace")
    before = store.public_connection()
    captured = []
    def fake(connection):
        captured.append(connection)
        return {"models": [{"id": "user-local-model", "label": "user-local-model"}], "status": "available"}
    monkeypatch.setattr(provider_discovery, "list_connection_models", fake)
    result = store.discover_models({"provider": "local_llamacpp", "endpoint": "http://127.0.0.1:39999", "model": ""})
    assert result["models"][0]["id"] == "user-local-model"
    assert captured[0].model == "local-service"
    assert captured[0].endpoint == "http://127.0.0.1:39999"
    assert store.public_connection() == before
    assert not (store.root / "connection.json").exists()


def test_model_discovery_uses_temporary_key_without_persisting(tmp_path, monkeypatch):
    from visual_audio_notes import provider_discovery
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("VAN_DESKTOP_RUNTIME_KEY", "previous-process-value")
    store = DesktopStore(tmp_path / "workspace")
    captured = []
    def fake(connection):
        captured.append(os.environ.get(connection.api_key_env))
        return {"models": [{"id": "deepseek-flash", "label": "deepseek-flash"}], "status": "available"}
    monkeypatch.setattr(provider_discovery, "list_connection_models", fake)
    store.discover_models({"api_key": "fixture-temporary-key"})
    assert captured == ["fixture-temporary-key"]
    assert os.environ["VAN_DESKTOP_RUNTIME_KEY"] == "previous-process-value"
    assert not store.public_connection()["key_configured"]
    assert not (store.root / "connection.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_windows_remembered_key_is_encrypted_and_user_scoped(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    key = "fixture-dpapi-key-not-real"
    store = DesktopStore(tmp_path / "workspace")
    store.save_connection({"api_key": key, "remember_key": True})
    assert key.encode() not in (store.root / "credential.bin").read_bytes()
    assert DesktopStore(store.root).public_connection()["key_configured"]
    store.save_connection({"clear_key": True, "remember_key": False})
    assert not (store.root / "credential.bin").exists()


def test_store_import_does_not_load_research_ml_runtimes():
    result = subprocess.run([sys.executable, "-c", "import sys,json;import visual_audio_notes.desktop_store;print(json.dumps([n for n in ('torch','numpy','sklearn','sentence_transformers','faster_whisper') if n in sys.modules]))"], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == []


class FixtureModel:
    cache_identity = {"provider": "fixture", "model": "desktop-test"}

    def __init__(self):
        self.calls = []

    def json(self, system, content, max_tokens=2000, purpose="unspecified", schema=None):
        self.calls.append({"purpose": purpose, "status": "validated", "usage": {"prompt_tokens": 100, "completion_tokens": 20}})
        data = json.loads(content)
        if purpose.startswith("quality_extract"):
            first = data["source_data_not_instructions"][0]
            return {"claims": [{"origin": data["origin"], "kind": "concept", "importance": "key", "title": "列表拼接", "statement": "列表拼接返回一个新列表。", "evidence": [{"span_id": first["spans"][0]["span_id"]}]}], "uncertain_source_ids": []}
        ids = [c["claim_id"] for c in data["ledger_data_not_instructions"]]
        return {"topics": [{"title": "列表与新对象", "key_idea": "拼接会生成一个新列表。", "explanation": "依据本来源原文归纳。", "claim_ids": ids}], "deferred_claims": [], "uncertainties": []}


def finish(store):
    store._thread.join(timeout=10)
    assert not store._thread.is_alive()
    assert store.state()["job"]["state"] == "complete", store.state()["job"]


def test_async_ai_keeps_origins_reuses_steps_and_exports_bound_evidence(tmp_path, inputs, monkeypatch):
    from visual_audio_notes import desktop_ai
    model = FixtureModel()
    monkeypatch.setattr(desktop_ai, "create_json_model", lambda connection: model)
    store = DesktopStore(tmp_path / "workspace")
    store.save_connection({"api_key": "fixture-not-real"})
    result = store.import_files(list(inputs))
    store.start_ai(result["id"])
    finish(store)
    first = store.project(result["id"])
    assert len(model.calls) == 4
    assert len(first["topics"]["audio"]) == len(first["topics"]["courseware"]) == 1
    assert first["usage"]["api_attempts"] == 4
    assert first["review_items"][0]["status"] == "needs_review"
    store.decide(result["id"], first["chunks"][0]["segment_id"], [first["pages"][0]["unit_id"]])
    store.start_ai(result["id"])
    finish(store)
    again = store.project(result["id"])
    assert len(model.calls) == 4 and again["usage"]["api_attempts"] == 0
    assert again["usage"]["local_result_cache"]["complete_reuses"] == 4
    assert again["review_items"][0]["status"] == "confirmed"
    store.save_notes(result["id"], "我的归纳")
    exported = store.export_project(result["id"], tmp_path / "ai-archive")
    assert exported["mode"] == "ai_topics"
    assert (tmp_path / "ai-archive/完整证据账本.json").is_file()
    assert "我的归纳" in (tmp_path / "ai-archive/笔记.md").read_text(encoding="utf-8")
    assert not os.environ.get("VAN_DESKTOP_RUNTIME_KEY")


def test_failed_regeneration_keeps_previous_topics_review_and_export(tmp_path, inputs, monkeypatch):
    from visual_audio_notes import desktop_ai
    model = FixtureModel()
    monkeypatch.setattr(desktop_ai, "create_json_model", lambda connection: model)
    store = DesktopStore(tmp_path / "workspace")
    store.save_connection({"api_key": "fixture-not-real"})
    project = store.import_files(list(inputs))
    store.start_ai(project["id"])
    finish(store)
    old = store.project(project["id"])
    def failing(*args, **kwargs):
        raise ValueError("Fixture failure")
    monkeypatch.setattr(model, "json", failing)
    store.start_ai(project["id"], refresh=True)
    store._thread.join(timeout=10)
    failed = store.project(project["id"])
    assert store.state()["job"]["state"] == "failed"
    assert failed["topics"] == old["topics"] and failed["topics_from_previous_run"]
    assert store.export_project(project["id"], tmp_path / "preserved-archive")["mode"] == "ai_topics"


def test_no_ai_manual_confirmation_survives_adding_ai_organization(tmp_path, inputs, monkeypatch):
    from visual_audio_notes import desktop_ai
    model = FixtureModel()
    monkeypatch.setattr(desktop_ai, "create_json_model", lambda connection: model)
    store = DesktopStore(tmp_path / "workspace")
    project = store.import_files(list(inputs))
    store.decide(project["id"], project["chunks"][0]["segment_id"], [project["pages"][0]["unit_id"]])
    store.save_connection({"api_key": "fixture-not-real"})
    store.start_ai(project["id"])
    finish(store)
    assert store.project(project["id"])["review_items"][0]["status"] == "confirmed"


def test_review_after_failed_first_run_is_exported_and_survives_early_retry_failure(tmp_path, inputs, monkeypatch):
    from visual_audio_notes import desktop_ai

    class FailingModel(FixtureModel):
        def json(self, *args, **kwargs):
            raise ValueError("Fixture request failed")

    def fail_before_candidates(connection):
        raise ValueError("Fixture connection setup failed")

    monkeypatch.setattr(desktop_ai, "create_json_model", lambda connection: FailingModel())
    store = DesktopStore(tmp_path / "workspace")
    store.save_connection({"api_key": "fixture-not-real"})
    project = store.import_files(list(inputs))
    identifier = project["id"]
    store.start_ai(identifier)
    store._thread.join(timeout=10)
    assert store.state()["job"]["state"] == "failed"
    failed = store.project(identifier)
    segment_id = failed["chunks"][0]["segment_id"]
    page_id = failed["pages"][0]["unit_id"]
    reviewed = store.decide(identifier, segment_id, [page_id])
    assert reviewed["review_items"][0]["status"] == "confirmed"

    first_export = store.export_project(identifier, tmp_path / "from-first-failure")
    assert first_export["checks"]["human_confirmed_single_page_mappings"] == 1
    monkeypatch.setattr(desktop_ai, "create_json_model", fail_before_candidates)
    store.start_ai(identifier)
    store._thread.join(timeout=10)
    assert store.state()["job"]["state"] == "failed"

    reopened = DesktopStore(store.root)
    assert reopened.project(identifier)["review_items"] == reviewed["review_items"]
    monkeypatch.setattr(desktop_ai, "create_json_model", lambda connection: FixtureModel())
    reopened.save_connection({"api_key": "fixture-not-real"})
    reopened.start_ai(identifier)
    finish(reopened)
    assert reopened.project(identifier)["review_items"][0]["confirmed_unit_ids"] == [page_id]


def test_archive_write_failure_leaves_destination_available_for_retry(tmp_path, inputs, monkeypatch):
    from visual_audio_notes import desktop_ai

    def partial_write(project, destination, *, language):
        destination.mkdir()
        (destination / "unfinished.txt").write_text("partial", encoding="utf-8")
        raise PermissionError("Fixture write failure")

    store = DesktopStore(tmp_path / "workspace")
    project = store.import_files(list(inputs))
    identifier = project["id"]
    destination = tmp_path / "exports" / "archive"
    with monkeypatch.context() as patch:
        patch.setattr(desktop_ai, "export_project", partial_write)
        with pytest.raises(PermissionError):
            store.export_project(identifier, destination, language="en")

    assert list(destination.parent.iterdir()) == []
    assert store.project(identifier)["export_path"] is None
    result = store.export_project(identifier, destination, language="en")
    assert (destination / result["reader_entry"]).is_file()
