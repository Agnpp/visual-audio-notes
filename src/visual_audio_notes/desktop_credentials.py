"""User-scoped Windows DPAPI storage; plaintext credentials never touch disk."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path


class _Blob(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _transform(value: bytes, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise ValueError("记住密钥仅支持 Windows；可选择仅本次使用。")
    source_buffer = ctypes.create_string_buffer(value)
    source = _Blob(len(value), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    destination = _Blob()
    crypt = ctypes.windll.crypt32
    if decrypt:
        success = crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None,
                                          1, ctypes.byref(destination))
    else:
        success = crypt.CryptProtectData(ctypes.byref(source), "图音笔记", None, None, None,
                                        1, ctypes.byref(destination))
    if not success:
        raise ValueError("Windows 无法读取或保存此账户的密钥，请重新输入。")
    try:
        return ctypes.string_at(destination.data, destination.length)
    finally:
        ctypes.windll.kernel32.LocalFree(destination.data)


def save_secret(path: Path, value: str):
    encrypted = _transform(value.encode("utf-8"), False)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(encrypted)
    temporary.replace(path)


def load_secret(path: Path) -> str:
    if not path.is_file():
        return ""
    return _transform(path.read_bytes(), True).decode("utf-8")
