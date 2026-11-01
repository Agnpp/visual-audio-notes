"""Fetch public research inputs and local runtimes; never fetch online subtitles."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("HF_HOME", str(ROOT / ".cache" / "huggingface"))
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")


def download(url: str, destination: Path) -> dict:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        temporary = destination.with_suffix(destination.suffix + ".part")
        size = temporary.stat().st_size if temporary.exists() else 0
        headers = {"User-Agent": "VisualAudioNotesResearch/0.0.1"}
        if size:
            headers["Range"] = f"bytes={size}-"
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=60) as response:
            append = size > 0 and response.status == 206
            with temporary.open("ab" if append else "wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
        temporary.replace(destination)
    digest = hashlib.file_digest(destination.open("rb"), "sha256").hexdigest()
    result = {"path": str(destination.relative_to(ROOT)), "url": url,
              "bytes": destination.stat().st_size, "sha256": digest}
    print(json.dumps({"downloaded": result}, ensure_ascii=True), flush=True)
    return result


def local_models() -> dict:
    from huggingface_hub import HfApi, hf_hub_download, snapshot_download
    api = HfApi(token=False)
    repository = "unsloth/Qwen3-4B-Instruct-2507-GGUF"
    info = api.model_info(repository)
    filename = next(f.rfilename for f in info.siblings if f.rfilename.endswith("Q4_K_M.gguf"))
    model = hf_hub_download(repository, filename=filename, revision=info.sha,
                            local_dir=ROOT / ".cache/models/llm", token=False)
    print(json.dumps({"llm_ready": filename, "revision": info.sha}), flush=True)
    results = {"llm": {"repo": repository, "revision": info.sha, "path": model}}
    for name, repository, patterns in [
        ("embedding", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
         ["*.json", "*.txt", "*.model", "model.safetensors", "1_Pooling/*"]),
        ("asr", "Systran/faster-whisper-small",
         ["config.json", "model.bin", "tokenizer.json", "vocabulary.*", "preprocessor_config.json"]),
    ]:
        info = api.model_info(repository)
        path = snapshot_download(repository, revision=info.sha,
                                 local_dir=ROOT / f".cache/models/{name}",
                                 allow_patterns=patterns, token=False)
        results[name] = {"repo": repository, "revision": info.sha, "path": path}
        print(json.dumps({"model_ready": name, "revision": info.sha}), flush=True)
    return results


def main() -> None:
    assets = [
        ("mit_pdf", "https://ocw.mit.edu/courses/6-0001-introduction-to-computer-science-and-programming-in-python-fall-2016/6ba59859535f1566dd57a7279aeba5d1_MIT6_0001F16_Lec4.pdf", "data/public/mit_functions/materials.pdf"),
        ("mit_video", "https://archive.org/download/MIT6.0001F16/MIT6_0001F16_Lecture_04_300k.mp4", "data/oracle/mit_functions/source.mp4"),
        ("zh_pdf", "https://speech.ee.ntu.edu.tw/~tlkagk/courses/MLDS_2015_2/Lecture/Brief%20ML%20(v2).pdf", "data/public/ntu_ml/materials.pdf"),
        ("zh_video", "https://speech.ee.ntu.edu.tw/~tlkagk/courses/MLDS_2015_2/Lecture/Brief%20ML%20(v2).ecm.mp4/media.mp4", "data/oracle/ntu_ml/source.mp4"),
        ("llama_runtime", "https://github.com/ggml-org/llama.cpp/releases/download/b12020/llama-b12020-bin-win-vulkan-x64.zip", ".runtime/llama-b12020.zip"),
    ]
    started = time.perf_counter()
    results = {}
    errors = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(download, url, ROOT / target): key for key, url, target in assets}
        futures[executor.submit(local_models)] = "models"
        for future in concurrent.futures.as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as exc:
                errors[key] = f"{type(exc).__name__}: {exc}"
                print(json.dumps({"failed": key, "error": errors[key]}), flush=True)
    if "llama_runtime" in results:
        target = (ROOT / ".runtime/llama-b12020").resolve()
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(ROOT / ".runtime/llama-b12020.zip") as archive:
            for item in archive.infolist():
                if not (target / item.filename).resolve().is_relative_to(target):
                    raise ValueError("Runtime ZIP member escapes destination")
            archive.extractall(target)
    result = {"downloads": results, "errors": errors,
              "seconds": round(time.perf_counter() - started, 2)}
    output = ROOT / "data/download_manifest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"finished": True, "seconds": result["seconds"], "errors": errors}), flush=True)
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
