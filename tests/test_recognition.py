from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import threading
import wave

import pytest

from visual_audio_notes import recognition as r
from visual_audio_notes.core import read_json


KEY = "fixture-only-not-a-real-credential"


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "recording.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\0\0" * 16000)
    return path


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("ASR_API_KEY", KEY)
    monkeypatch.setenv("DEEPSEEK_API_KEY", KEY)
    resources = []
    def start(payload, status=200, location=None):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                calls.append({"path": self.path, "headers": dict(self.headers),
                              "body": self.rfile.read(int(self.headers["Content-Length"]))})
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())
        http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        resources.append((http, thread))
        return f"http://127.0.0.1:{http.server_port}", calls
    yield start
    for http, thread in resources:
        http.shutdown()
        http.server_close()
        thread.join()


def fake_chunks(monkeypatch, audio):
    monkeypatch.setattr(r, "_audio_chunks", lambda source, folder: [(audio, 0, 1)])


def test_remote_asr_requires_timestamps_and_reuses_result_without_new_call(audio, tmp_path, server, monkeypatch):
    fake_chunks(monkeypatch, audio)
    endpoint, calls = server({"segments": [{"start": 0, "end": .8, "text": "课堂重点"}],
                              "usage": {"type": "duration", "seconds": 1, "total_tokens": 10}})
    reserved = []
    config = {"provider": "openai_compatible_asr", "endpoint": endpoint, "model": "whisper-1",
              "_request_guard": lambda purpose, tokens: reserved.append((purpose, tokens))}
    result = r.transcribe(audio, config, tmp_path / "cache")
    assert result["segments"][0]["text"] == "课堂重点"
    assert result["segments"][0]["start"] == 0
    assert len(calls) == 1 and calls[0]["path"] == "/audio/transcriptions"
    assert b"verbose_json" in calls[0]["body"] and b"timestamp_granularities[]" in calls[0]["body"]
    assert KEY not in json.dumps(result)
    assert result["api_calls"][0]["provider_audio_usage"]["seconds"] == 1
    assert result["api_calls"][0]["audio_seconds"] == 1
    again = r.transcribe(audio, config, tmp_path / "cache")
    assert again["cache_hit"] and not again["api_calls"] and len(calls) == 1
    assert reserved == [("speech_to_text", 0)]
    assert "WEBVTT" in r.segments_to_vtt(result["segments"])


@pytest.mark.parametrize("segments", [None, [{"start": True, "end": .8, "text": "bad"}],
    [{"start": 0, "end": 2, "text": "out of audio"}], [{"start": -1, "end": 0, "text": "bad"}],
    [{"start": .8, "end": .2, "text": "bad"}]])
def test_remote_asr_rejects_fabricated_or_invalid_time_and_keeps_attempt_usage(audio, tmp_path, server, monkeypatch, segments):
    fake_chunks(monkeypatch, audio)
    endpoint, calls = server({"text": "no timestamps", "segments": segments, "usage": {"total_tokens": 19}})
    with pytest.raises(r.RecognitionError):
        r.transcribe(audio, {"provider": "openai_compatible_asr", "endpoint": endpoint, "model": "test"}, tmp_path / "cache")
    assert len(calls) == 1
    attempts = list((tmp_path / "cache/attempts").glob("*.json"))
    assert len(attempts) == 1 and read_json(attempts[0])["usage"]["total_tokens"] == 19
    assert not list((tmp_path / "cache").glob("asr-chunk-*.json"))


def test_remote_asr_chunk_cache_preserves_original_axis_and_recovers_without_repaying(audio, tmp_path, server, monkeypatch):
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    first.write_bytes(audio.read_bytes())
    second.write_bytes(audio.read_bytes() + b"different hash")
    monkeypatch.setattr(r, "_audio_chunks", lambda source, folder: [(first, 0, 1), (second, 600, 1)])
    endpoint, calls = server({"segments": [{"start": .1, "end": .9, "text": "same"}]})
    config = {"provider": "openai_compatible_asr", "endpoint": endpoint, "model": "test"}
    result = r.transcribe(audio, config, tmp_path / "cache")
    assert [s["start"] for s in result["segments"]] == [.1, 600.1]
    assert len(calls) == 2
    # Remove only the aggregate result, retain completed bounded chunks.
    for p in (tmp_path / "cache").glob("asr-*.json"):
        if not p.name.startswith("asr-chunk-"):
            p.unlink()
    result = r.transcribe(audio, config, tmp_path / "cache")
    assert len(calls) == 2 and result["api_calls"] == []


def test_api_language_changes_invalidate_cache(audio, tmp_path, server, monkeypatch):
    fake_chunks(monkeypatch, audio)
    endpoint, calls = server({"segments": [{"start": 0, "end": .5, "text": "word"}]})
    config = {"provider": "openai_compatible_asr", "endpoint": endpoint, "model": "test", "language": "zh"}
    r.transcribe(audio, config, tmp_path / "cache")
    r.transcribe(audio, {**config, "language": "en"}, tmp_path / "cache")
    assert len(calls) == 2


def test_redirect_does_not_forward_key(audio, tmp_path, server, monkeypatch):
    fake_chunks(monkeypatch, audio)
    other, other_calls = server({})
    endpoint, calls = server({}, status=307, location=other + "/stolen")
    with pytest.raises(r.RecognitionError, match="307"):
        r.transcribe(audio, {"provider": "openai_compatible_asr", "endpoint": endpoint, "model": "test"}, tmp_path / "cache")
    assert len(calls) == 1 and not other_calls
    attempt = read_json(next((tmp_path / "cache/attempts").glob("*.json")))
    assert attempt["audio_seconds"] == 1 and attempt["status"] == "failed_usage_unknown"
    assert "provider_audio_usage" not in attempt
    assert KEY not in "".join(p.read_text() for p in (tmp_path / "cache/attempts").glob("*.json"))


def test_speech_api_never_borrows_text_provider_credential(audio, tmp_path, server, monkeypatch):
    fake_chunks(monkeypatch, audio)
    endpoint, calls = server({"segments": []})
    monkeypatch.delenv("ASR_API_KEY")
    # A DeepSeek text key exists, but it must not be sent to a speech endpoint.
    assert "DEEPSEEK_API_KEY" in __import__("os").environ
    with pytest.raises(r.RecognitionError, match="密钥"):
        r.transcribe(audio, {"provider": "openai_compatible_asr", "endpoint": endpoint, "model": "test"}, tmp_path / "cache")
    assert not calls


def test_local_asr_does_not_download_without_explicit_action(audio, tmp_path):
    with pytest.raises(r.RecognitionError, match="准备语音模型"):
        r.transcribe(audio, {"provider": "local_whisper", "model_dir": str(tmp_path / "models")}, tmp_path / "cache")
    assert not (tmp_path / "models").exists()


def test_local_asr_preserves_source_times_and_caches(audio, tmp_path, monkeypatch):
    model = tmp_path / "model"
    model.mkdir()
    for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt"):
        (model / name).write_bytes(b"fixture")
    observed = []
    class Engine:
        def __init__(self, *args, **kwargs):
            observed.append(kwargs)
        def transcribe(self, path, **kwargs):
            assert kwargs["vad_filter"]
            return iter([SimpleNamespace(start=.2, end=.9, text=" important distinction "),
                         SimpleNamespace(start=.91, end=2.28, text=" unreliably timed tail ")]), SimpleNamespace(duration=1., language="en")
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Engine))
    result = r.transcribe(audio, {"provider": "local_whisper", "model_path": str(model)}, tmp_path / "cache")
    assert result["segments"][0]["start"] == .2 and result["segments"][0]["end"] == .9
    assert len(result["segments"]) == 1
    assert result["unlocated_segments"] == [{"row": 1, "reason": "invalid_local_asr_time",
                                            "text": " unreliably timed tail ",
                                            "reported_start": .91, "reported_end": 2.28}]
    assert r.transcribe(audio, {"provider": "local_whisper", "model_path": str(model)},
                        tmp_path / "cache")["unlocated_segments"] == result["unlocated_segments"]
    assert observed[0]["local_files_only"] and observed[0]["compute_type"] == "int8"
    assert not result["api_calls"]


def test_ocr_skips_native_text_and_caches_blank_pages(tmp_path, monkeypatch):
    image = tmp_path / "page.png"
    image.write_bytes(b"fixture")
    engine_calls = []
    def engine(path):
        engine_calls.append(path)
        return [[[[0, 0], [10, 0], [10, 10], [0, 10]], "中文 English", .92]], [.01]
    monkeypatch.setattr(r, "_local_ocr_engine", lambda: engine)
    pages = [{"unit_id": "u1", "image": tmp_path / "need-not-exist", "text": "native text"},
             {"unit_id": "u2", "image": image, "text": ""}]
    result = r.recognize_pages(pages, {"provider": "local_rapidocr"}, tmp_path / "cache")
    assert result["texts"] == {"u1": "native text", "u2": "中文 English"}
    assert len(engine_calls) == 1 and result["metrics"]["native_pages_reused"] == 1
    repeated = r.recognize_pages(pages, {"provider": "local_rapidocr"}, tmp_path / "cache")
    assert len(engine_calls) == 1 and repeated["metrics"]["cache_hits"] == 1


def test_ocr_failure_is_not_empty_success_or_cached(tmp_path, monkeypatch):
    image = tmp_path / "page.png"
    image.write_bytes(b"fixture")
    def engine(_):
        raise ValueError("private provider/source content must not leak")
    monkeypatch.setattr(r, "_local_ocr_engine", lambda: engine)
    result = r.recognize_pages([{"unit_id": "u1", "image": image, "text": ""}], {"provider": "local_rapidocr"}, tmp_path / "cache")
    assert result["failures"] and not result["texts"] and not list((tmp_path / "cache").glob("ocr-*.json"))
    assert "private provider" not in json.dumps(result)


def test_deepseek_image_ocr_is_explicit_and_text_only_is_not_ocr(tmp_path, server):
    from PIL import Image
    image = tmp_path / "image.png"
    Image.new("RGB", (40, 40), "white").save(image)
    endpoint, calls = server({"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps({"text": "图中文字", "uncertainties": []})}}], "usage": {"total_tokens": 21}})
    result = r.recognize_pages([{"unit_id": "u1", "image": image, "text": ""}],
                              {"provider": "deepseek_vision", "endpoint": endpoint}, tmp_path / "cache")
    assert result["texts"]["u1"] == "图中文字" and len(calls) == 1
    payload = json.loads(calls[0]["body"])
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert result["api_calls"][0]["usage"]["total_tokens"] == 21
    assert KEY not in json.dumps(result)
    with pytest.raises(r.RecognitionError, match="不具备"):
        r.recognize_pages([], {"provider": "deepseek"}, tmp_path / "invalid")


def test_decoder_fixed_chunks_preserve_original_duration(audio, tmp_path):
    result = r._audio_chunks(audio, tmp_path)
    assert len(result) == 1 and result[0][1] == 0 and result[0][2] == 1
    with wave.open(str(result[0][0]), "rb") as audio_file:
        assert audio_file.getnframes() == 16000 and audio_file.getframerate() == 16000


def test_cancel_before_work_never_loads_models_or_sends_request(audio, tmp_path, monkeypatch):
    event = threading.Event()
    event.set()
    with pytest.raises(r.RecognitionCancelled):
        r.transcribe(audio, {"provider": "local_whisper", "model_dir": str(tmp_path / "models")}, tmp_path / "cache", cancel_event=event)
    with pytest.raises(r.RecognitionCancelled):
        r.recognize_pages([{"unit_id": "u1", "image": tmp_path / "page", "text": ""}],
                          {"provider": "local_rapidocr"}, tmp_path / "ocr", cancel_event=event)


def test_cancel_after_first_api_chunk_retains_cache_and_does_not_request_next(audio, tmp_path, server, monkeypatch):
    event = threading.Event()
    monkeypatch.setattr(r, "_audio_chunks", lambda source, folder: [(audio, 0, 1), (audio, 600, 1)])
    endpoint, calls = server({"segments": [{"start": .1, "end": .9, "text": "word"}]})
    original = r._recorded_request
    def invoke(*args, **kwargs):
        result = original(*args, **kwargs)
        event.set()
        return result
    monkeypatch.setattr(r, "_recorded_request", invoke)
    with pytest.raises(r.RecognitionCancelled):
        r.transcribe(audio, {"provider": "openai_compatible_asr", "endpoint": endpoint, "model": "test"},
                     tmp_path / "cache", cancel_event=event)
    assert len(calls) == 1
    assert len(list((tmp_path / "cache").glob("asr-chunk-*.json"))) == 1


def test_shared_pipeline_guard_stops_ocr_before_new_paid_request(tmp_path, server):
    from PIL import Image
    from visual_audio_notes.task_runtime import TaskBudgetExceeded
    image = tmp_path / "page.png"
    Image.new("RGB", (50, 50), "white").save(image)
    endpoint, calls = server({})
    def guard(purpose, tokens):
        assert (purpose, tokens) == ("ocr", 6000)
        raise TaskBudgetExceeded("fixture budget exhausted")
    with pytest.raises(TaskBudgetExceeded):
        r.recognize_pages([{"unit_id": "u1", "image": image, "text": ""}],
            {"provider": "deepseek_vision", "endpoint": endpoint, "_request_guard": guard}, tmp_path / "cache")
    assert not calls
    assert not list((tmp_path / "cache").glob("attempts/*.json"))
