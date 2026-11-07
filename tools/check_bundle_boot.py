"""Verify a frozen build or an extracted ZIP in a fresh app data directory."""
import argparse
import hashlib
from pathlib import Path
import json
import os
import platform
import subprocess
import time
import urllib.request
import uuid
import tomllib
import zipfile


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def extract_bundle(dist, name, folder):
    report = json.loads((dist / (name + ".build.json")).read_text(encoding="utf-8"))
    bundle = dist / (name + ".zip")
    assert sha256(bundle) == report["archive_sha256"]
    unpacked = folder / "首次 解压"
    with zipfile.ZipFile(bundle) as archive:
        assert all((unpacked / member).resolve().is_relative_to(unpacked.resolve())
                   for member in archive.namelist())
        archive.extractall(unpacked)
    for guide in ("QuickStart.zh-CN.md", "QuickStart.en.md"):
        assert (unpacked / name / guide).is_file()
    return unpacked


def check_archive(api, folder):
    from create_desktop_fixture import create

    inputs = folder / "导入 测试"
    create(inputs)
    originals = {path.name: sha256(path) for path in inputs.iterdir()}
    api("preferences", {"language": "zh-CN"})
    project = api("import", {"paths": [str(path) for path in sorted(inputs.iterdir())]})
    assert project["counts"]["pages"] == project["counts"]["segments"] == 3
    api("notes", {"id": project["id"], "text": "中文 portable ZIP note"})
    for language, entry in (("zh-CN", "阅读.html"), ("en", "index.html")):
        api("preferences", {"language": language})
        exported = api("export", {"id": project["id"]})
        assert exported["mode"] == "basic_no_ai" and exported["reader_entry"] == entry
        archive = Path(exported["path"])
        reader = (archive / entry).read_text(encoding="utf-8")
        assert f'<html lang="{language}">' in reader and "中文 portable ZIP note" in reader
        index = json.loads((archive / "原件清单.json").read_text(encoding="utf-8"))
        assert {item["filename"]: sha256(archive / item["path"]) for item in index["originals"]} == originals


def check(flavor, *, from_zip=False):
    root = Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    name = "VisualAudioNotes-" + version + ("-api" if flavor == "api" else "") + "-windows-x64"
    folder = root / "output/recognition-qa" / ("boot-" + flavor + "-" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    ready = folder / "ready.json"
    dist = root / "dist"
    package_root = extract_bundle(dist, name, folder) if from_zip else dist
    executable = package_root / name / "VisualAudioNotes.exe"
    env = {key: value for key, value in os.environ.items()
           if key not in {"DEEPSEEK_API_KEY", "VAN_DESKTOP_RUNTIME_KEY", "ASR_API_KEY", "OCR_API_KEY", "PYTHONPATH"}}
    process = subprocess.Popen([str(executable), "--serve-only", "--data-dir", str(folder / "data"),
                                "--ready-file", str(ready)], cwd=executable.parent, env=env,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        for _ in range(200):
            try:
                info = json.loads(ready.read_text(encoding="utf-8"))
                break
            except (FileNotFoundError, json.JSONDecodeError):
                if process.poll() is not None:
                    raise RuntimeError("Frozen helper exited before ready") from None
                time.sleep(.1)
        else:
            raise RuntimeError("Frozen helper did not become ready")
        with urllib.request.urlopen(info["url"], timeout=10) as response:
            document = response.read().decode("utf-8")
        token = document.split('window.VAN_TOKEN = "', 1)[1].split('"', 1)[0]

        def api(action, data=None):
            request = urllib.request.Request(info["url"] + "/api/" + action,
                headers={"X-Van-Token": token, "Content-Type": "application/json"},
                data=None if data is None else json.dumps(data).encode("utf-8"))
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)["data"]

        state = api("state")
        assert not state["projects"] and not state["connection"]["configured"]
        recognition = state["recognition"]
        report = {"flavor": recognition["package_flavor"],
                  "windows": platform.platform(),
                  "extracted_from_zip": from_zip,
                  "fresh_app_data": True,
                  "asr_provider": recognition["asr"]["provider"],
                  "ocr_provider": recognition["ocr"]["provider"],
                  "local_asr_engine": recognition["asr"]["local_engine_available"],
                  "local_ocr_engine": recognition["ocr"]["local_engine_available"]}
        assert report["flavor"] == flavor
        assert report["local_asr_engine"] is (flavor == "full")
        assert report["local_ocr_engine"] is (flavor == "full")
        assert report["asr_provider"] == ("local_whisper" if flavor == "full" else "openai_compatible_asr")
        assert report["ocr_provider"] == ("local_rapidocr" if flavor == "full" else "deepseek_vision")
        if from_zip:
            check_archive(api, folder)
            report["bilingual_import_and_archive"] = True
            report["original_hashes_verified"] = True
        (folder / "checks.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
        return report
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("flavor", nargs="?", choices=("full", "api"), default="full")
    parser.add_argument("--from-zip", action="store_true")
    args = parser.parse_args()
    check(args.flavor, from_zip=args.from_zip)
