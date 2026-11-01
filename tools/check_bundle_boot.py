"""Start only an owned frozen loopback helper and verify public capability flags."""
from pathlib import Path
import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid
import tomllib


def check(flavor):
    root = Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    name = "VisualAudioNotes-" + version + ("-api" if flavor == "api" else "") + "-windows-x64"
    folder = root / "output/recognition-qa" / ("boot-" + flavor + "-" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    ready = folder / "ready.json"
    executable = root / "dist" / name / "VisualAudioNotes.exe"
    process = subprocess.Popen([str(executable), "--serve-only", "--data-dir", str(folder / "data"),
                                "--ready-file", str(ready)], creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        for _ in range(100):
            if ready.is_file():
                break
            if process.poll() is not None:
                raise RuntimeError("Frozen helper exited before ready")
            time.sleep(.1)
        info = json.loads(ready.read_text(encoding="utf-8"))
        with urllib.request.urlopen(info["url"]) as response:
            document = response.read().decode("utf-8")
        token = document.split('window.VAN_TOKEN = "', 1)[1].split('"', 1)[0]
        request = urllib.request.Request(info["url"] + "/api/state", headers={"X-Van-Token": token})
        with urllib.request.urlopen(request) as response:
            state = json.load(response)["data"]
        recognition = state["recognition"]
        report = {"flavor": recognition["package_flavor"],
                  "asr_provider": recognition["asr"]["provider"],
                  "ocr_provider": recognition["ocr"]["provider"],
                  "local_asr_engine": recognition["asr"]["local_engine_available"],
                  "local_ocr_engine": recognition["ocr"]["local_engine_available"]}
        assert report["flavor"] == flavor
        assert report["local_asr_engine"] is (flavor == "full")
        assert report["local_ocr_engine"] is (flavor == "full")
        assert report["asr_provider"] == ("local_whisper" if flavor == "full" else "openai_compatible_asr")
        assert report["ocr_provider"] == ("local_rapidocr" if flavor == "full" else "deepseek_vision")
        (folder / "checks.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
        return report
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)


if __name__ == "__main__":
    check(sys.argv[1] if len(sys.argv) > 1 else "full")
