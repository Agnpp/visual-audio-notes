import json
from pathlib import Path
import shutil
import subprocess
import sys
import wave

import pytest

from visual_audio_notes.archive import validate_archive
from visual_audio_notes.basic import prepare_basic
from visual_audio_notes.basic_output import export_basic_archive
from visual_audio_notes.binding import create_binding, save_binding
from visual_audio_notes.core import file_hash, read_json, write_json


@pytest.fixture
def f(tmp_path):
    import pypdfium2 as pdfium

    pdf, audio = tmp_path / "课件 原件.pdf", tmp_path / "独立 录音.wav"
    document = pdfium.PdfDocument.new()
    page = document.new_page(100, 100)
    page.close()
    document.save(pdf)
    document.close()
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\x00\x00" * 16000)
    transcript = tmp_path / "已有 转写.srt"
    transcript.write_text("1\n00:00:00,100 --> 00:00:00,500\nExisting subtitle text.\n", encoding="utf-8")
    prepared = tmp_path / "准备 资料"
    prepare_basic(pdf, audio, transcript, prepared)
    save_binding(prepared / "archive_binding.json", create_binding(prepared, audio, pdf))
    return {"audio": audio, "pdf": pdf, "prepared": prepared, "title": "无需 AI 的归档"}


def test_basic_archive_keeps_imported_text_and_originals_without_ai_labels(tmp_path, f):
    out = tmp_path / "归档"
    before = {name: file_hash(f[name]) for name in ("audio", "pdf")}
    checks = export_basic_archive(out, **f)
    manifest = read_json(out / "project.json")
    assert checks["uses_ai"] is False and checks["model_calls"] == 0
    assert manifest["mode"] == "basic_no_ai" and manifest["transcript_kind"] == "user_provided_transcript"
    assert manifest["notes"]["topics"] == []
    assert manifest["timeline"][0]["start"] == .1
    assert manifest["timeline"][0]["end"] == .5
    assert manifest["timeline"][0]["unit_id"] is None and manifest["timeline"][0]["status"] == "unreviewed"
    for name in ("阅读.html", "笔记.md"):
        text = (out / name).read_text(encoding="utf-8")
        assert "未调用 AI" in text
        assert "本地模型整理" not in text and "本地模型生成" not in text and "自动候选" not in text
    assert "Existing subtitle text." in (out / "转写稿.md").read_text(encoding="utf-8")
    assert (out / "sources/transcript.srt").read_bytes() == (tmp_path / "已有 转写.srt").read_bytes()
    assert before == {name: file_hash(f[name]) for name in before}
    assert validate_archive(out)["valid"]


def test_manual_notes_are_escaped_and_never_create_a_correspondence(tmp_path, f):
    manual = '<script>alert(1)</script> ![remote](https://example.invalid/pixel.png)\n手动内容。'
    out = tmp_path / "notes"
    export_basic_archive(out, **f, manual_notes=manual)
    html = (out / "阅读.html").read_text(encoding="utf-8")
    md = (out / "笔记.md").read_text(encoding="utf-8")
    assert '<script>alert(1)</script>' not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "![remote]" not in md and "&lt;script&gt;" in md
    project = read_json(out / "project.json")
    assert project["manual_notes"] == manual
    assert project["notes"]["topics"][0]["evidence_ids"] == []
    assert not any(row["unit_id"] for row in project["timeline"])


def test_all_preview_pages_can_be_selected_and_no_ocr_success_is_claimed(tmp_path, f):
    out = tmp_path / "previews"
    checks = export_basic_archive(out, **f)
    html = (out / "阅读.html").read_text(encoding="utf-8")
    project = read_json(out / "project.json")
    assert checks["needs_ocr"] and project["ocr_not_run_pages"] == [1]
    assert "本次未执行 OCR" in html and "需要另选 OCR 模型" in html
    for identifier in project["units"]:
        assert f'data-ref="{identifier}"' in html
    assert "导入转写与原始时间索引" in html


@pytest.mark.parametrize("target", ["audio", "pdf", "page", "source_manifest", "transcript"])
def test_replaced_original_or_bound_resource_is_rejected_before_export(tmp_path, f, target):
    if target in ("audio", "pdf"):
        changed = f[target]
    elif target == "page":
        changed = next((f["prepared"] / "pages").glob("*.png"))
    elif target == "source_manifest":
        changed = f["prepared"] / "basic_sources.json"
    else:
        changed = tmp_path / "已有 转写.srt"
    changed.write_bytes(changed.read_bytes() + b"changed")
    out = tmp_path / "blocked"
    with pytest.raises(ValueError):
        export_basic_archive(out, **f)
    assert not out.exists()


def test_current_metrics_cannot_hide_audio_replacement(tmp_path, f):
    f["audio"].write_bytes(b"replacement")
    path = f["prepared"] / "prepare_metrics.json"
    metrics = read_json(path)
    metrics["input_hashes"]["audio"] = file_hash(f["audio"])
    write_json(path, metrics)
    with pytest.raises(ValueError):
        export_basic_archive(tmp_path / "blocked", **f)


def test_export_requires_a_binding_captured_earlier(tmp_path, f):
    (f["prepared"] / "archive_binding.json").unlink()
    with pytest.raises(ValueError, match="Capture an archive binding"):
        export_basic_archive(tmp_path / "blocked", **f)


@pytest.mark.parametrize("nonempty", [False, True])
def test_existing_export_directory_is_not_overwritten(tmp_path, f, nonempty):
    out = tmp_path / "existing"
    out.mkdir()
    if nonempty:
        (out / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="destination exists"):
        export_basic_archive(out, **f)
    assert sorted(p.name for p in out.iterdir()) == (["keep.txt"] if nonempty else [])


def test_copied_archive_is_portable_and_does_not_publish_absolute_source_paths(tmp_path, f):
    out = tmp_path / "archive"
    export_basic_archive(out, **f)
    moved = tmp_path / "另一 位置" / "归档 副本"
    shutil.copytree(out, moved)
    assert validate_archive(moved)["valid"]
    for name in ("阅读.html", "笔记.md", "basic_sources.json", "project.json", "来源与使用说明.txt"):
        assert str(tmp_path) not in (moved / name).read_text(encoding="utf-8")
    public = read_json(moved / "basic_sources.json")
    assert public["originals"]["transcript"]["archive_path"] == "sources/transcript.srt"
    assert "path" not in public["originals"]["transcript"]


def test_basic_export_module_does_not_import_model_runtimes_or_provider_clients():
    code = ("import json,sys;import visual_audio_notes.basic_output;"
            "print(json.dumps(sorted(n for n in sys.modules if n in "
            "('torch','sentence_transformers','faster_whisper','visual_audio_notes.providers'))))")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            encoding="utf-8", check=True)
    assert json.loads(result.stdout) == []
