"""Independent speech/OCR settings and user-scoped credentials for desktop."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import threading
import sys

from .desktop_credentials import load_secret, save_secret
from .providers import _endpoint
from .task_runtime import atomic_json
from .core import read_json


_LOCK = threading.RLock()
DEFAULTS = {
    "asr": {"provider": "local_whisper", "model": "small", "language": "auto",
            "endpoint": "", "remember_key": False},
    "ocr": {"provider": "local_rapidocr", "model": "deepseek-flash",
            "endpoint": "https://api.deepseek.com", "remember_key": False},
}


class RecognitionSettings:
    def __init__(self, root: Path):
        self.root = root
        self.values = deepcopy(DEFAULTS)
        marker = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])) / "desktop_flavor.json"
        self.flavor = read_json(marker).get("flavor", "full") if marker.is_file() else "full"
        if self.flavor == "api":
            self.values["asr"].update(provider="openai_compatible_asr", model="whisper-1")
            self.values["ocr"]["provider"] = "deepseek_vision"
        self.secrets = {"asr": "", "ocr": ""}
        path = root / "recognition.json"
        if path.is_file():
            data = read_json(path)
            for kind in self.values:
                if isinstance(data.get(kind), dict):
                    self.values[kind].update(data[kind])
                if self.values[kind].get("remember_key"):
                    try:
                        self.secrets[kind] = load_secret(root / ("credential-" + kind + ".bin"))
                    except (OSError, ValueError, UnicodeError):
                        self.values[kind]["remember_key"] = False

    def public(self):
        from .model_assets import asr_model_status
        result = deepcopy(self.values)
        for kind in result:
            result[kind]["key_configured"] = bool(self.secrets[kind])
        result["asr"]["local_model"] = asr_model_status(self.root / "models")
        result["asr"]["local_engine_available"] = importlib.util.find_spec("faster_whisper") is not None
        result["ocr"]["local_engine_available"] = importlib.util.find_spec("rapidocr_onnxruntime") is not None
        result["package_flavor"] = self.flavor
        return result

    def save(self, data):
        if not isinstance(data, dict) or set(data) - {"asr", "ocr"}:
            raise ValueError("识别能力设置格式不正确。")
        selected = deepcopy(self.values)
        secrets = dict(self.secrets)
        for kind, incoming in data.items():
            if not isinstance(incoming, dict) or set(incoming) - {
                    "provider", "model", "language", "endpoint", "remember_key", "api_key", "clear_key"}:
                raise ValueError("识别能力设置格式不正确。")
            allowed_providers = {"local_whisper", "openai_compatible_asr"} if kind == "asr" else {"local_rapidocr", "deepseek_vision"}
            values = {**selected[kind], **{k: v for k, v in incoming.items() if k not in {"api_key", "clear_key"}}}
            if values["provider"] not in allowed_providers or type(values["remember_key"]) is not bool:
                raise ValueError("请选择实际可用的识别方式。")
            if (not isinstance(values["model"], str) or not values["model"].strip()
                    or len(values["model"]) > 200 or any(ord(c) < 32 for c in values["model"])):
                raise ValueError("请填写识别模型名称。")
            if values.get("language", "auto") not in {"auto", "zh", "en"}:
                raise ValueError("请选择自动、中文或英文。")
            remote = values["provider"] in {"openai_compatible_asr", "deepseek_vision"}
            if remote:
                try:
                    values["endpoint"] = _endpoint(values["endpoint"], local=False)
                except ValueError:
                    raise ValueError("识别 API 地址无效；线上服务需要 HTTPS，本机服务可以使用回环地址。") from None
            changed = (values["provider"], values["endpoint"]) != (self.values[kind]["provider"], self.values[kind]["endpoint"])
            key = "" if incoming.get("clear_key") else incoming.get("api_key") or ("" if changed else secrets[kind])
            if not isinstance(key, str) or (key and (key != key.strip() or any(ord(c) < 33 or ord(c) > 126 for c in key))):
                raise ValueError("密钥不能包含空格或换行。")
            selected[kind], secrets[kind] = values, key
        # All configurations are validated before any setting or credential is changed.
        for kind in data:
            vault = self.root / ("credential-" + kind + ".bin")
            if selected[kind]["remember_key"] and secrets[kind]:
                save_secret(vault, secrets[kind])
            elif vault.is_file():
                vault.unlink()
        self.values, self.secrets = selected, secrets
        atomic_json(self.root / "recognition.json", selected)
        return self.public()

    def config(self, kind):
        return {**self.values[kind], "model_dir": str(self.root / "models"),
                "allow_download": False, "api_key_env": "VAN_RECOGNITION_" + kind.upper()}

    @contextmanager
    def credentials(self):
        with _LOCK:
            before = {}
            for kind, key in self.secrets.items():
                name = "VAN_RECOGNITION_" + kind.upper()
                before[name] = os.environ.get(name)
                if key:
                    os.environ[name] = key
                else:
                    os.environ.pop(name, None)
            try:
                yield
            finally:
                for name, value in before.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
