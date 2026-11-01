"""Exercise actual frozen recognition code; API flavor uses a local fixture API.

The full flavor recognizes selected real audio/image inputs without supplied
subtitles. The API fixture is a protocol test, never a perception-quality score.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import uuid
import zipfile
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def make_office_fixture(path):
    """Self-authored OOXML input checks native parsing, not alignment quality."""
    drawing = "http://schemas.openxmlformats.org/drawingml/2006/main"
    presentation = "http://schemas.openxmlformats.org/presentationml/2006/main"
    relationships = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package = "http://schemas.openxmlformats.org/package/2006/relationships"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr("ppt/presentation.xml", f'<p:presentation xmlns:p="{presentation}" xmlns:r="{relationships}"><p:sldIdLst><p:sldId id="256" r:id="slide1"/></p:sldIdLst><p:sldSz cx="12192000" cy="6858000"/></p:presentation>')
        zipped.writestr("ppt/_rels/presentation.xml.rels", f'<Relationships xmlns="{package}"><Relationship Id="slide1" Type="{relationships}/slide" Target="slides/slide1.xml"/></Relationships>')
        zipped.writestr("ppt/slides/slide1.xml", f'<p:sld xmlns:p="{presentation}" xmlns:a="{drawing}"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>本机课件导入检查：原件和识别文字分别保留。</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>')


def start_fixture():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            calls.append(self.path)
            if self.path.endswith("/audio/transcriptions"):
                payload = {"segments": [{"start": 0., "end": 10., "text": "本地接口夹具：对象修改和返回值需要分别判断。"}],
                           "usage": {"type": "duration", "seconds": 30.0}}
            else:
                payload = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                    "text": "本地接口夹具：这不是在线OCR准确率结果。", "uncertainties": []}, ensure_ascii=False)}}],
                    "usage": {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40,
                              "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 30}}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, calls


def check(flavor):
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    name = "VisualAudioNotes-" + version + ("-api" if flavor == "api" else "") + "-windows-x64"
    folder = ROOT / "output/recognition-qa" / ("frozen-flow-" + flavor + "-" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    data = folder / "data"
    data.mkdir()
    if flavor == "full":
        models = data / "models/whisper-small"
        models.mkdir(parents=True)
        for filename in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"):
            source = ROOT / ".cache/models/asr" / filename
            try:
                os.link(source, models / filename)
            except OSError:
                shutil.copy2(source, models / filename)
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"DEEPSEEK_API_KEY", "VAN_DESKTOP_RUNTIME_KEY", "ASR_API_KEY"}}
    def start_helper(label):
        ready = folder / (label + "-ready.json")
        process = subprocess.Popen([str(ROOT / "dist" / name / "VisualAudioNotes.exe"), "--serve-only",
                                    "--data-dir", str(data), "--ready-file", str(ready)],
                                   env=environment, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        for _ in range(150):
            if ready.is_file():
                return process, json.loads(ready.read_text(encoding="utf-8"))
            if process.poll() is not None:
                raise RuntimeError("Frozen helper exited")
            time.sleep(.1)
        process.terminate()
        process.wait(timeout=10)
        raise RuntimeError("Frozen helper did not become ready")
    process, info = start_helper("initial")
    fixture = None
    try:
        document = urllib.request.urlopen(info["url"]).read().decode()
        token = document.split('window.VAN_TOKEN = "', 1)[1].split('"', 1)[0]
        def call(action, payload=None):
            request = urllib.request.Request(info["url"] + "/api/" + action,
                data=json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None,
                headers={"X-Van-Token": token, "Content-Type": "application/json", "X-Van-Language": "en"})
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    result = json.load(response)
            except urllib.error.HTTPError as error:
                try:
                    message = json.load(error).get("error", "Frozen API request failed")
                finally:
                    error.close()
                raise RuntimeError(f"{action}: {message}") from None
            if result.get("ok") is not True:
                raise RuntimeError(result.get("error", "API check failed"))
            return result["data"]
        state = call("state")
        call("preferences", {"language": "en"})
        assert not state["connection"]["configured"], "No text generation provider may be configured for this check"
        calls = []
        if flavor == "api":
            fixture, calls = start_fixture()
            endpoint = f"http://127.0.0.1:{fixture.server_port}"
            call("recognition_settings", {
                "asr": {"provider": "openai_compatible_asr", "model": "fixture-asr", "endpoint": endpoint,
                        "api_key": "fixture-not-a-real-credential", "remember_key": False},
                "ocr": {"provider": "deepseek_vision", "model": "fixture-vision", "endpoint": endpoint,
                        "api_key": "fixture-not-a-real-credential", "remember_key": False}})
        slides = folder / "原创课件.pptx"
        make_office_fixture(slides)
        paths = [ROOT / "output/desktop-qa/inputs/Lesson.pdf", ROOT / "output/recognition-qa/ch-en-source.png",
                 ROOT / "output/recognition-qa/zh-crop-30s.wav"]
        original_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths + [slides]}
        project = call("import", {"paths": [str(path) for path in paths], "title": "无字幕识别闭环检查"})
        assert project["counts"]["segments"] == 0 and project["counts"]["pages"] == 4
        started = time.perf_counter()
        call("ai", {"id": project["id"]})
        while time.perf_counter() - started < 120:
            state = call("state")
            if state["job"]["state"] != "running":
                break
            time.sleep(.25)
        if state["job"]["state"] != "complete":
            raise RuntimeError(json.dumps(state["job"], ensure_ascii=False))
        result = call("project", {"id": project["id"]})
        assert result["segments"]
        recognized = [page for page in result["pages"] if page.get("text_origin") == "ocr_requires_verification"]
        assert len(recognized) == 1 and recognized[0]["text"].strip()
        assert not result["has_ai"], "Recognition is not a generated topic summary"
        assert all(0 <= segment["start"] < segment["end"] <= 30.25 for segment in result["segments"])
        audio_usage = result["usage"]["recognition"]["audio_usage"]
        if flavor == "api":
            assert audio_usage["api_attempts"] == 1
            assert audio_usage["submitted_audio_seconds"] == 30.0
            assert audio_usage["provider_reported_totals"]["seconds"] == 30.0
            assert audio_usage["provider_reported_totals"]["total_tokens"] is None
            assert result["usage"]["recognition"]["reported_token_totals"]["total_tokens"] == 40
        else:
            assert audio_usage["api_attempts"] == 0
        before_append_segments = result["segments"]
        before_append_ocr_text = recognized[0]["text"]
        result = call("import", {"project_id": project["id"], "paths": [str(slides)]})
        assert result["counts"]["pages"] == 5
        assert result["segments"] == before_append_segments
        assert any("本机课件导入检查" in page["text"] for page in result["pages"])
        assert any(page["text"] == before_append_ocr_text for page in result["pages"])
        first = result["segments"][0]
        revised_text = first["text"] + "〔测试人工修订〕"
        result = call("edit_text", {"id": project["id"], "kind": "transcript",
                                   "source_id": first["segment_id"], "text": revised_text,
                                   "start": first["start"], "end": first["end"]})
        assert result["segments"][0]["text"] == revised_text
        call("notes", {"id": project["id"], "text": "这是编译程序识别闭环测试，未执行文字归纳。"})
        exported = call("export", {"id": project["id"]})
        archive = Path(exported["path"])
        assert (archive / "阅读.html").is_file() and (archive / "笔记.md").is_file()
        assert (archive / "index.html").is_file() and (archive / "notes.md").is_file()
        assert '<html lang="en">' in (archive / "index.html").read_text(encoding="utf-8")
        transcripts = json.loads((archive / "派生转写清单.json").read_text(encoding="utf-8"))["transcripts"]
        assert len(transcripts) >= 2
        for item in transcripts:
            assert hashlib.sha256((archive / item["path"]).read_bytes()).hexdigest() == item["sha256"]
        assert any(not item["current"] and "〔测试人工修订〕" not in (archive / item["path"]).read_text(encoding="utf-8")
                   for item in transcripts)
        assert any(item["current"] and "〔测试人工修订〕" in (archive / item["path"]).read_text(encoding="utf-8")
                   for item in transcripts)
        assert (archive / "资料补充记录.json").is_file()
        retained = json.loads((archive / "原件清单.json").read_text(encoding="utf-8"))["originals"]
        retained_hashes = {hashlib.sha256((archive / item["path"]).read_bytes()).hexdigest() for item in retained}
        assert set(original_hashes.values()) <= retained_hashes
        process.terminate()
        process.wait(timeout=10)
        process, info = start_helper("restarted")
        document = urllib.request.urlopen(info["url"]).read().decode()
        token = document.split('window.VAN_TOKEN = "', 1)[1].split('"', 1)[0]
        restarted = call("project", {"id": project["id"]})
        assert restarted["segments"] == result["segments"]
        assert [page["text"] for page in restarted["pages"]] == [page["text"] for page in result["pages"]]
        longest_cache_path = max((len(str(path.resolve())) for path in
                                  (data / "projects" / project["id"] / ".recognition-cache").rglob("*")
                                  if path.is_file()), default=0)
        if flavor == "api":
            assert longest_cache_path + 32 > 260, "Keep the original long-path regression depth"
        report = {"flavor": flavor, "scope": "actual frozen executable loopback integration",
                  "api_fixture_not_quality_score": flavor == "api", "input_subtitles_provided": False,
                  "pages": result["counts"]["pages"], "asr_segments": len(result["segments"]),
                  "ocr_pages": len(recognized), "all_asr_times_within_original_audio": True,
                  "topic_generation_executed": False, "paid_external_api_calls": 0,
                  "fixture_api_requests": calls, "seconds": round(time.perf_counter() - started, 3),
                  "audio_usage": audio_usage,
                  "archive": str(archive), "archive_reader_created": True, "english_reader_created": True,
                  "pptx_native_text_imported": True, "pptx_appended_after_recognition": True,
                  "asr_ocr_retained_after_append": True, "originals_preserved_sha256": original_hashes,
                  "manual_revision_kept_original_generated_transcript": True,
                  "longest_cache_absolute_path_characters": longest_cache_path,
                  "legacy_append_stage_would_add_characters": 32,
                  "archive_original_hashes_equal": True, "restart_preserved": True,
                  "weights_prepared_from_existing_research_cache": flavor == "full"}
        (folder / "checks.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
        return report
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)
        if fixture:
            fixture.shutdown()
            fixture.server_close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    check(sys.argv[1] if len(sys.argv) > 1 else "full")
