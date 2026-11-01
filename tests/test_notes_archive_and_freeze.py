import copy
from pathlib import Path
import shutil

import pytest

from visual_audio_notes.archive import export_archive, safe_member, validate_archive
from visual_audio_notes.cli import freeze, verify_freeze
from visual_audio_notes.core import write_json
from visual_audio_notes.notes import LocalModel, validate_notes


def notes():
    return {"overview": "A grounded overview", "topics": [{
        "title": "Main concept", "key_idea": "Supported concept", "explanation": "Why it matters",
        "example": "", "pitfall": "", "limitation": "", "evidence_ids": ["u_one", "t_one"],
    }], "review_questions": ["Why?"], "uncertainties": []}


def test_fabricated_citations_are_not_accepted():
    value = notes()
    value["topics"][0]["evidence_ids"] = ["invented"]
    with pytest.raises(ValueError, match="Unknown"):
        validate_notes(value, {"u_one", "t_one"})


def test_inference_client_cannot_silently_send_to_external_provider():
    with pytest.raises(ValueError, match="local"):
        LocalModel("https://example.com", "model")


def test_schema_is_sent_in_the_servers_actual_nested_transport_contract(tmp_path, monkeypatch):
    import json
    from visual_audio_notes import notes as module
    captured = {}
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return json.dumps({"choices": [{"finish_reason": "stop",
                              "message": {"content": '{"choice":"allowed"}'}}]}).encode()
    def open_request(request, timeout):
        captured.update(json.loads(request.data))
        return Response()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module.urllib.request, "urlopen", open_request)
    schema = {"type": "object", "properties": {"choice": {"enum": ["allowed"]}}}
    result = LocalModel("http://127.0.0.1:1", "fixture").json("s", "u", schema=schema)
    assert result["choice"] == "allowed"
    assert captured["response_format"]["json_schema"]["schema"] == schema
    assert captured["response_format"]["json_schema"]["strict"] is True


def test_archive_works_after_move_and_treats_model_output_as_data(tmp_path):
    prepared = tmp_path / "prepared"
    (prepared / "pages").mkdir(parents=True)
    (prepared / "pages/u_one.png").write_bytes(b"local-image-placeholder")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio-placeholder")
    pdf = tmp_path / "slides.pdf"
    pdf.write_bytes(b"%PDF-placeholder")
    value = notes()
    value["topics"][0]["key_idea"] = '<script>alert(1)</script> ![remote](https://example.com/x)'
    chunk = {"segment_id": "t_one", "start": 120, "end": 130,
             "text": "</script><script>alert(2)</script>"}
    destination = tmp_path / "archive"
    export_archive(destination, "中文 空格 <title>", audio, pdf, prepared,
                   {"u_one": {"page": 1, "image": "u_one.png"}},
                   [chunk], [{**chunk, "unit_id": None, "status": "review"}], value,
                   "Local research fixture")
    moved = tmp_path / "中文 移动目录"
    shutil.move(destination, moved)
    assert validate_archive(moved)["valid"]
    document = (moved / "阅读.html").read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in document
    assert "<script>alert(2)</script>" not in document
    assert "\\u003c/script>" in document
    assert "connect-src" in document
    assert str(tmp_path) not in document
    markdown = (moved / "笔记.md").read_text(encoding="utf-8")
    assert "![remote]" not in markdown


@pytest.mark.parametrize("relative", ["../escape", "/outside", "C:/private", "a\\b"])
def test_resource_cannot_escape_archive(tmp_path, relative):
    with pytest.raises(ValueError):
        safe_member(tmp_path, relative)


def test_freeze_records_uninstalled_optional_model_libraries_without_requiring_them(tmp_path, monkeypatch):
    from visual_audio_notes import cli
    original = cli.importlib.metadata.version
    def available(name):
        if name in {'sentence-transformers', 'torch'}:
            raise cli.importlib.metadata.PackageNotFoundError(name)
        return original(name)
    monkeypatch.setattr(cli.importlib.metadata, 'version', available)
    config, features, run = tmp_path/'protocol.json', tmp_path/'features.json', tmp_path/'run'
    run.mkdir()
    write_json(config, {'version':'optional-library-fixture'})
    write_json(features, {'units':[], 'segments':[]})
    write_json(run/'predictions.json', {'predictions':['preserved']})
    freeze(run, config, features)
    result=verify_freeze(run)
    assert result['software']['packages']['torch'] is None
    assert result['software']['packages']['sentence-transformers'] is None
    assert result['software']['packages']['pypdfium2']


def test_frozen_prediction_tampering_is_detected(tmp_path):
    config = tmp_path / "protocol.json"
    features = tmp_path / "features.json"
    run = tmp_path / "run"
    run.mkdir()
    write_json(config, {"version": "test"})
    write_json(features, {"units": [], "segments": []})
    write_json(run / "predictions.json", {"predictions": ["original"]})
    freeze(run, config, features)
    assert verify_freeze(run)
    write_json(run / "predictions.json", {"predictions": ["changed after scoring"]})
    with pytest.raises(ValueError, match="changed"):
        verify_freeze(run)


def test_evaluator_requires_freeze_before_reading_answers(tmp_path, monkeypatch):
    from visual_audio_notes import evaluation
    read_paths = []
    monkeypatch.setattr(evaluation, "read_json", lambda p: read_paths.append(p))
    with pytest.raises(FileNotFoundError):
        evaluation.evaluate(tmp_path / "missing-run", tmp_path / "gold.json",
                            tmp_path / "source_map.json", tmp_path / "result.json")
    assert read_paths == []
