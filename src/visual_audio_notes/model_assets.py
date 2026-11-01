"""Small, explicit model catalog with pinned source, license and checksums.

Only the user's explicit model-preparation action downloads Whisper weights.
The local OCR models are already part of the packaged RapidOCR dependency.
"""
from __future__ import annotations

from pathlib import Path
import os
import urllib.request

from .core import file_hash
from .step_cache import _key_lock
from .task_runtime import atomic_json


ASR_REVISION = "536b0662742c02347bc0e980a01041f333bce120"


class RecognitionCancelled(RuntimeError):
    """Completed cache entries remain available; no new request is started."""


ASR_FILES = {
    "config.json": (2370, "b55496ac7940a7ae47d2c01eab40edfd8701feec1229d9cce3b40014383fb828"),
    "model.bin": (483546902, "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671"),
    "tokenizer.json": (2203239, "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab"),
    "vocabulary.txt": (459861, "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913"),
}


def default_model_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share")) / "VisualAudioNotes/models"


def asr_model_status(model_dir: Path | None = None) -> dict:
    root = Path(model_dir or default_model_dir()) / "whisper-small"
    ready = all((root / name).is_file() and (root / name).stat().st_size == size
                for name, (size, _) in ASR_FILES.items())
    return {"id": "whisper-small", "model": "small", "ready": ready,
            "download_bytes": sum(size for size, _ in ASR_FILES.values()),
            "license": "MIT", "source": "https://huggingface.co/Systran/faster-whisper-small",
            "revision": ASR_REVISION, "path": str(root)}


def validate_asr_model(path: Path, *, pinned: bool = True) -> Path:
    path = Path(path)
    if not path.is_dir() or not all((path / name).is_file() for name in ASR_FILES):
        raise ValueError("本地语音模型尚未准备好，请先点击“准备语音模型”。")
    if pinned and any((path / name).stat().st_size != size or file_hash(path / name) != expected
                      for name, (size, expected) in ASR_FILES.items()):
        raise ValueError("语音模型文件校验未通过，请重新准备模型。")
    return path


def download_asr_model(model_dir: Path | None = None, progress=None, cancel_event=None) -> Path:
    """Resume .part files, then verify every exact pinned file before use."""
    root = Path(model_dir or default_model_dir()) / "whisper-small"
    root.mkdir(parents=True, exist_ok=True)
    with _key_lock(root / "download.lock", 30):
        total = sum(size for size, _ in ASR_FILES.values())
        completed = 0
        for name, (size, expected) in ASR_FILES.items():
            if cancel_event is not None and cancel_event.is_set():
                raise RecognitionCancelled("模型准备已取消，已下载部分保留，可稍后继续。")
            target = root / name
            if target.is_file() and target.stat().st_size == size and file_hash(target) == expected:
                completed += size
                continue
            part = root / (name + ".part")
            offset = part.stat().st_size if part.exists() else 0
            if offset > size:
                part.unlink()
                offset = 0
            if offset == size:
                if file_hash(part) == expected:
                    part.replace(target)
                    completed += size
                    continue
                part.unlink()
                offset = 0
            url = f"https://huggingface.co/Systran/faster-whisper-small/resolve/{ASR_REVISION}/{name}"
            request = urllib.request.Request(url, headers={"Range": f"bytes={offset}-"} if offset else {})
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    if offset and response.status != 206:
                        offset = 0
                    with part.open("ab" if offset else "wb") as stream:
                        while block := response.read(1024 * 1024):
                            if cancel_event is not None and cancel_event.is_set():
                                raise RecognitionCancelled("模型准备已取消，已下载部分保留，可稍后继续。")
                            offset += len(block)
                            if offset > size:
                                raise ValueError("模型下载体积异常，未使用下载结果。")
                            stream.write(block)
                            if progress:
                                progress("正在准备本地语音模型…", (completed + offset) / total)
            except (OSError, TimeoutError):
                raise ValueError("模型下载暂未完成，可以稍后继续；已下载部分已保留。") from None
            if part.stat().st_size != size or file_hash(part) != expected:
                part.unlink(missing_ok=True)
                raise ValueError("模型下载校验失败，请重试。")
            part.replace(target)
            completed += size
        atomic_json(root / "model_manifest.json", {
            "model": "Systran/faster-whisper-small", "revision": ASR_REVISION,
            "license": "MIT", "files": {name: {"bytes": size, "sha256": sha}
                                          for name, (size, sha) in ASR_FILES.items()}})
    if progress:
        progress("语音模型已准备好。", 1.0)
    return root
