# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import os
import tomllib
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, copy_metadata

root = Path(SPECPATH)
flavor = os.environ.get("VAN_BUILD_FLAVOR", "full")
if flavor not in {"full", "api"}:
    raise ValueError("Unknown desktop flavor")
local_engines = flavor == "full"
version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
package_name = "VisualAudioNotes-" + version + ("-api" if flavor == "api" else "") + "-windows-x64"
package_root = root / "src" / "visual_audio_notes"
datas = [(str(path), "src/visual_audio_notes/" + path.relative_to(package_root).parent.as_posix())
         for path in package_root.rglob("*.py")]
datas += [(str(package_root / "desktop_ui"), "src/visual_audio_notes/desktop_ui"),
          (str(root / "prompts"), "prompts"),
          (str(root / "benchmark" / "provider.v3.json"), "benchmark")]
# PyInstaller modules report __file__ at _MEIPASS/visual_audio_notes/*.py.
# Persistent validation hashes those exact source files rather than bytecode.
datas += [(str(root / "src" / "visual_audio_notes" / name), "visual_audio_notes")
          for name in ("quality.py", "topic_notes.py", "step_cache.py")]
datas += collect_data_files("webview")
datas += collect_data_files("jsonschema_specifications")
datas += [(str(root / "build" / "desktop" / flavor / "desktop_flavor.json"), ".")]
binaries = []
hidden = ["webview.platforms.winforms", "webview.platforms.edgechromium", "av", "numpy"]
excluded = ["torch", "torchvision", "torchaudio", "sklearn", "scipy", "sentence_transformers",
            "transformers", "PyQt5", "PyQt6", "PySide2", "PySide6", "cefpython3", "gi", "qtpy"]
if local_engines:
    datas += collect_data_files("rapidocr_onnxruntime")
    datas += collect_data_files("faster_whisper")
    datas += copy_metadata("rapidocr-onnxruntime")
    datas += copy_metadata("faster-whisper")
    binaries += collect_dynamic_libs("ctranslate2") + collect_dynamic_libs("onnxruntime")
    hidden += ["rapidocr_onnxruntime", "faster_whisper", "ctranslate2", "onnxruntime", "cv2"]
else:
    excluded += ["rapidocr_onnxruntime", "faster_whisper", "ctranslate2", "onnxruntime", "cv2",
                 "huggingface_hub", "tokenizers", "shapely", "pyclipper", "yaml"]

a = Analysis(
    [str(root / "tools" / "run_desktop.py")],
    pathex=[str(root / "src")], binaries=binaries, datas=datas,
    hiddenimports=hidden,
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=excluded,
    noarchive=False, optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="VisualAudioNotes",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=True,
    icon=str(root / "build" / "desktop" / "app.ico"),
    uac_admin=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False,
               name=package_name)
