import hashlib
import io
import threading

import pytest

from visual_audio_notes import model_assets as assets


class Response(io.BytesIO):
    status = 200


def catalog(monkeypatch):
    content = {"model.bin": b"model-fixture", "config.json": b"{}"}
    monkeypatch.setattr(assets, "ASR_FILES", {name: (len(value), hashlib.sha256(value).hexdigest())
                                           for name, value in content.items()})
    return content


def test_explicit_download_verifies_fixed_files_and_reuses_valid_assets(tmp_path, monkeypatch):
    content = catalog(monkeypatch)
    requested = []
    def download(request, timeout):
        requested.append(request.full_url)
        return Response(content[request.full_url.rsplit("/", 1)[-1]])
    monkeypatch.setattr(assets.urllib.request, "urlopen", download)
    assert not assets.asr_model_status(tmp_path)["ready"]
    root = assets.download_asr_model(tmp_path)
    assert assets.validate_asr_model(root) == root
    assert assets.asr_model_status(tmp_path)["ready"]
    assets.download_asr_model(tmp_path)
    assert len(requested) == 2
    assert all(assets.ASR_REVISION in url for url in requested)


def test_download_cancellation_preserves_partial_file_and_resumes(tmp_path, monkeypatch):
    content = catalog(monkeypatch)
    root = tmp_path / "whisper-small"
    root.mkdir()
    part = root / "model.bin.part"
    part.write_bytes(b"model-")
    event = threading.Event()
    event.set()
    with pytest.raises(assets.RecognitionCancelled):
        assets.download_asr_model(tmp_path, cancel_event=event)
    assert part.read_bytes() == b"model-"
    event.clear()
    requested = []
    def download(request, timeout):
        name = request.full_url.rsplit("/", 1)[-1]
        offset = int(request.headers.get("Range", "bytes=0-").split("=")[1].split("-")[0])
        requested.append(offset)
        response = Response(content[name][offset:])
        response.status = 206 if offset else 200
        return response
    monkeypatch.setattr(assets.urllib.request, "urlopen", download)
    assets.download_asr_model(tmp_path, cancel_event=event)
    assert requested == [6, 0]
    assert not part.exists()


def test_wrong_checksum_never_becomes_ready(tmp_path, monkeypatch):
    catalog(monkeypatch)
    monkeypatch.setattr(assets.urllib.request, "urlopen", lambda request, timeout: Response(b"wrong"))
    with pytest.raises(ValueError, match="校验"):
        assets.download_asr_model(tmp_path)
    assert not assets.asr_model_status(tmp_path)["ready"]


def test_completed_part_file_is_adopted_without_network(tmp_path, monkeypatch):
    content = catalog(monkeypatch)
    root = tmp_path / "whisper-small"
    root.mkdir()
    for name, value in content.items():
        (root / (name + ".part")).write_bytes(value)
    monkeypatch.setattr(assets.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network")))
    assets.download_asr_model(tmp_path)
    assert assets.asr_model_status(tmp_path)["ready"]
