"""Prepare a local build icon and package only the standalone desktop folder."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]
FLAVOR = os.environ.get("VAN_BUILD_FLAVOR", "full")
if FLAVOR not in {"full", "api"}:
    raise ValueError("Unknown desktop flavor")
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
NAME = "VisualAudioNotes-" + VERSION + ("-api" if FLAVOR == "api" else "") + "-windows-x64"


def prepare():
    from PIL import Image, ImageDraw
    folder = ROOT / "build" / "desktop"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / FLAVOR).mkdir(exist_ok=True)
    (folder / FLAVOR / "desktop_flavor.json").write_text(json.dumps({
        "flavor": FLAVOR, "local_asr": FLAVOR == "full", "local_ocr": FLAVOR == "full"}), encoding="utf-8")
    image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((9, 9, 247, 247), radius=54, fill="#d3e3fd")
    draw.rounded_rectangle((59, 45, 197, 211), radius=17, fill="white")
    for y, width in ((85, 77), (113, 77), (141, 48)):
        draw.rounded_rectangle((84, y, 84 + width, y + 10), radius=5, fill="#1967d2")
    draw.rounded_rectangle((146, 167, 219, 224), radius=15, fill="#1967d2")
    for x, h in ((161, 14), (173, 30), (185, 21), (197, 36)):
        draw.rounded_rectangle((x, 195 - h // 2, x + 5, 195 + h // 2), radius=2, fill="white")
    image.save(folder / "app.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def notices(destination: Path):
    from packaging.requirements import Requirement
    import re
    def canonical(value):
        return re.sub(r"[-_.]+", "-", value).lower()
    roots = ["pywebview", "pyinstaller", "pypdfium2", "Pillow", "jsonschema", "av", "numpy"]
    if FLAVOR == "full":
        roots += ["faster-whisper", "rapidocr-onnxruntime"]
    selected = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if canonical(name) in selected:
            continue
        selected.add(canonical(name))
        for value in importlib.metadata.requires(name) or []:
            requirement = Requirement(value)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                pending.append(requirement.name)
    licenses = destination / "第三方许可"
    licenses.mkdir(exist_ok=True)
    description = ("本包包含桌面运行时、CPU语音识别引擎与 RapidOCR 中文/英文识别模型，不包含 Whisper 语音权重、研究模型、公开课原件或密钥。"
                   if FLAVOR == "full" else "本包为 API 轻量版，包含桌面运行时和音频解码，不包含本地语音/OCR识别引擎、识别模型、公开课原件或密钥。")
    lines = ["# 第三方组件说明", "", description,
             "图音笔记自身代码采用根目录 LICENSE 所示的 MIT 许可；第三方代码与模型分别遵循各自许可。",
             "Python 及其标准库、PDFium 和以下组件遵循各自许可。组件许可不改变用户输入材料的许可。", ""]
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        shutil.copy2(python_license, licenses / "Python-LICENSE.txt")
    for source in (ROOT / "tools" / "licenses").glob("*.txt"):
        shutil.copy2(source, licenses / source.name)
    for distribution in sorted(importlib.metadata.distributions(), key=lambda d: d.metadata["Name"].lower()):
        name, version = distribution.metadata["Name"], distribution.version
        if canonical(name) not in selected:
            continue
        lines.append(f"- {name} {version}")
        for file in distribution.files or []:
            lower = str(file).lower()
            filename = Path(file).name.lower()
            if (".dist-info/" in lower and ("license" in filename or "copying" in filename
                                             or "notice" in filename or "/licenses/" in lower)):
                source = Path(distribution.locate_file(file))
                if source.is_file() and source.stat().st_size < 8 * 1024 * 1024:
                    safe_name = name.replace("/", "-") + "-" + str(file).replace("/", "_").replace("\\", "_")
                    shutil.copy2(source, licenses / safe_name)
    # PDFium wheel keeps its downstream notices in the raw package as well.
    import pypdfium2_raw
    raw_root = Path(pypdfium2_raw.__file__).parent
    for source in raw_root.rglob("*"):
        if source.is_file() and any(word in source.name.lower() for word in ("license", "notice")):
            shutil.copy2(source, licenses / ("PDFium-" + source.name))
    (destination / "第三方组件说明.md").write_text("\n".join(lines), encoding="utf-8-sig")


def build():
    # Windows PowerShell 5.1 treats native stderr progress as ErrorRecords.
    # Capture the build's combined log in Python and use the actual exit code.
    log = ROOT / "build" / "desktop" / FLAVOR / "build.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as stream:
        completed = subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm",
            "--workpath", "build/desktop/pyinstaller/" + FLAVOR, "--distpath", "dist", "desktop.spec"],
            cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, check=False)
    if completed.returncode:
        print(log.read_text(encoding="utf-8", errors="replace")[-8000:])
        raise SystemExit(completed.returncode)
    print("Desktop executable built; log: " + log.relative_to(ROOT).as_posix())


def package():
    destination = ROOT / "dist" / NAME
    executable = destination / "VisualAudioNotes.exe"
    if not executable.is_file():
        raise ValueError("Build the desktop executable before packaging")
    shutil.copy2(ROOT / "LICENSE", destination / "LICENSE")
    for language in ("en", "zh-CN"):
        guide = (ROOT / "docs" / ("guide." + language + ".md")).read_text(encoding="utf-8")
        for other in ("en", "zh-CN"):
            guide = guide.replace("(guide." + other + ".md)", "(QuickStart." + other + ".md)")
        guide = guide.replace("(../README.md)", "(https://github.com/Agnpp/visual-audio-notes)")
        guide = guide.replace("(../README.zh-CN.md)", "(https://github.com/Agnpp/visual-audio-notes/blob/main/README.zh-CN.md)")
        guide = guide.replace("(ROADMAP.md)", "(https://github.com/Agnpp/visual-audio-notes/blob/main/docs/ROADMAP.md)")
        (destination / ("QuickStart." + language + ".md")).write_text(guide, encoding="utf-8")
    shutil.copy2(ROOT / "docs" / "Office导入支持范围.md", destination / "Office导入支持范围.md")
    notices(destination)
    # Data copies support cache validation and also let us reject a stale build
    # after a source/UI change made between compilation and packaging.
    resource_files = list((ROOT / "src" / "visual_audio_notes").rglob("*.py"))
    resource_files += [path for path in (ROOT / "src" / "visual_audio_notes" / "desktop_ui").rglob("*")
                       if path.is_file()]
    resource_files += [path for path in (ROOT / "prompts").rglob("*") if path.is_file()]
    resource_files += [ROOT / "benchmark" / "provider.v3.json"]
    source_hashes = {}
    for source in sorted(resource_files):
        relative = source.relative_to(ROOT)
        bundled = destination / "_internal" / relative
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if not bundled.is_file() or hashlib.sha256(bundled.read_bytes()).hexdigest() != digest:
            raise ValueError("Bundled source is stale or missing: " + relative.as_posix())
        source_hashes[relative.as_posix()] = digest
    (destination / "构建来源.json").write_text(json.dumps({
        "version": VERSION, "flavor": FLAVOR, "source_sha256": source_hashes,
        "build_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "spec_sha256": hashlib.sha256((ROOT / "desktop.spec").read_bytes()).hexdigest(),
        "dependency_lock_sha256": hashlib.sha256((ROOT / "requirements-desktop.lock").read_bytes()).hexdigest(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    files = [path for path in destination.rglob("*") if path.is_file()]
    # Do not silently ship provider credentials, private sources or model weights.
    forbidden = [path for path in files if path.suffix.lower() in {".gguf", ".wav", ".mp3", ".m4a", ".pdf"}
                 or path.name.startswith(".env") or path.name in {"settings.json", "model.bin"}]
    if forbidden:
        raise ValueError("Unexpected private material or model found in package")
    bundled_onnx = [path.relative_to(destination).as_posix() for path in files if path.suffix.lower() == ".onnx"]
    if (FLAVOR == "full" and len(bundled_onnx) != 4) or (FLAVOR == "api" and bundled_onnx):
        raise ValueError("Recognition model assets do not match the selected package flavor")
    archive = ROOT / "dist" / (NAME + ".zip")
    pending_archive = archive.with_suffix(".zip.part")
    with zipfile.ZipFile(pending_archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zipped:
        for source in sorted(files):
            zipped.write(source, source.relative_to(destination.parent))
    os.replace(pending_archive, archive)
    report = {"version": VERSION, "flavor": FLAVOR, "platform": "windows-x64", "folder": destination.name,
              "file_count": len(files), "uncompressed_bytes": sum(p.stat().st_size for p in files),
              "archive_bytes": archive.stat().st_size,
              "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
              "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
              "bundled_source_count": len(source_hashes),
              "bundled_sources_verified_current": True,
              "code_signed": False, "large_asr_weights_bundled": False,
              "local_recognition_engines_bundled": FLAVOR == "full", "ocr_models_bundled": FLAVOR == "full",
              "bundled_onnx_assets": bundled_onnx,
              "managed_asr_first_download_bytes": 486212372 if FLAVOR == "full" else None,
              "ui_languages": ["zh-CN", "en"], "executable": "VisualAudioNotes.exe",
              "runtime_dependency": "Microsoft Edge WebView2 Runtime and .NET Framework 4.x"}
    (ROOT / "dist" / (NAME + ".build.json")).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    if hasattr(__import__("sys").stdout, "reconfigure"):
        __import__("sys").stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "build", "package"])
    args = parser.parse_args()
    {"prepare": prepare, "build": build, "package": package}[args.action]()
