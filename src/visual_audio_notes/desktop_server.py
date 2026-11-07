"""Private loopback transport for the packaged desktop window and UI checks."""
from __future__ import annotations

from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import secrets
import threading
import urllib.parse
import uuid

from .desktop_locale import localize_payload, translate
from .review import StaleReviewError


MAX_UPLOAD = 512 * 1024 * 1024
MEDIA_TYPES = {".png", ".jpg", ".jpeg", ".webp", ".pdf", ".wav", ".mp3", ".m4a", ".ogg", ".vtt", ".srt"}
UPLOAD_TYPES = MEDIA_TYPES | {".pptx", ".docx", ".xlsx", ".vsdx", ".ppt", ".doc", ".xls", ".vsd"}


def _ui_root(resources: Path) -> Path:
    for path in (resources / "src/visual_audio_notes/desktop_ui", resources / "desktop_ui"):
        if (path / "index.html").is_file():
            return path
    raise ValueError("桌面界面资源不完整，请重新解压整个软件文件夹。")


class DesktopServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, root: Path, resources: Path, port: int, store=None):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.ui_root = _ui_root(resources)
        if store is None:
            from .desktop_store import DesktopStore
            store = DesktopStore(self.root, resources)
        self.store = store
        self.token = secrets.token_urlsafe(32)
        self.native_window = None
        super().__init__(("127.0.0.1", port), DesktopHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"


class DesktopHandler(BaseHTTPRequestHandler):
    server: DesktopServer

    def log_message(self, *args):
        # URLs may contain a media capability token, and source paths are private.
        pass

    def _headers(self, status: int, mime: str, length: int, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _json(self, value, status=200):
        data = json.dumps(localize_payload(value, self._language()), ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(data))
        self.wfile.write(data)

    def _error(self, message, status=400):
        self._json({"ok": False, "error": message}, status)

    def _language(self):
        chosen = self.headers.get("X-Van-Language")
        if chosen in {"en", "zh-CN"}:
            return chosen
        chosen = getattr(self.server.store, "_preferences", {}).get("language")
        if chosen in {"en", "zh-CN"}:
            return chosen
        return "en" if self.headers.get("Accept-Language", "").lower().startswith("en") else "zh-CN"

    def _valid_host(self):
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def _authorized(self, *, media=False):
        if not self._valid_host():
            return False
        origin = self.headers.get("Origin")
        if origin and origin != self.server.origin:
            return False
        supplied = self.headers.get("X-Van-Token", "")
        if media:
            supplied = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("token", [supplied])[0]
        return secrets.compare_digest(supplied, self.server.token)

    def do_GET(self):
        if not self._valid_host():
            return self._error("访问地址不正确。", 403)
        path = urllib.parse.urlsplit(self.path).path
        if path in {"/", "/index.html"}:
            data = (self.server.ui_root / "index.html").read_text(encoding="utf-8")
            data = data.replace("__VAN_TOKEN__", self.server.token).replace("__VAN_LANGUAGE__", self._language()).encode("utf-8")
            self._headers(200, "text/html; charset=utf-8", len(data))
            return self.wfile.write(data)
        if path in {"/app.js", "/app.css", "/i18n.js"}:
            data = (self.server.ui_root / path[1:]).read_bytes()
            self._headers(200, "application/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8", len(data))
            return self.wfile.write(data)
        if path == "/translations.json":
            data = (self.server.ui_root / "en.json").read_bytes()
            self._headers(200, "application/json; charset=utf-8", len(data))
            return self.wfile.write(data)
        if path == "/favicon.ico":
            self._headers(204, "image/x-icon", 0)
            return
        if path == "/api/state":
            if not self._authorized():
                return self._error("会话已失效，请重新打开软件。", 403)
            result = self.server.store.state()
            result["native_dialogs"] = self.server.native_window is not None
            return self._json({"ok": True, "data": result})
        if path.startswith("/media/"):
            if not self._authorized(media=True):
                return self._error("无法访问这份资料。", 403)
            return self._media(urllib.parse.unquote(path[len("/media/"):]))
        return self._error("没有找到内容。", 404)

    def _media(self, relative):
        candidate = (self.server.root / relative).resolve()
        if (not candidate.is_relative_to(self.server.root / "projects")
                or candidate.suffix.lower() not in MEDIA_TYPES or not candidate.is_file()):
            return self._error("没有找到资料。", 404)
        size = candidate.stat().st_size
        start, end, status = 0, size - 1, 200
        range_header = self.headers.get("Range")
        if range_header:
            import re
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            if not match or not any(match.groups()):
                self._headers(416, "application/octet-stream", 0, {"Content-Range": f"bytes */{size}"})
                return
            left, right = match.groups()
            if left:
                start, end = int(left), min(int(right), size - 1) if right else size - 1
            else:
                start = max(0, size - int(right))
            if start > end or start >= size:
                self._headers(416, "application/octet-stream", 0, {"Content-Range": f"bytes */{size}"})
                return
            status = 206
        extra = {"Accept-Ranges": "bytes"}
        if status == 206:
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"
        self._headers(status, mimetypes.guess_type(candidate.name)[0] or "application/octet-stream", max(0, end - start + 1), extra)
        try:
            with candidate.open("rb") as stream:
                stream.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    block = stream.read(min(65536, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if not self._authorized():
            return self._error("会话已失效或请求来源不正确。", 403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._error("文件大小不正确。")
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/api/upload":
                if not 0 < length <= MAX_UPLOAD:
                    return self._error("这次导入不能超过 512 MB；可使用桌面文件选择器导入更大的录音。", 413)
                return self._upload(length)
            if not 0 <= length <= 1024 * 1024:
                return self._error("请求过大。", 413)
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("请求内容不正确。")
            result = self._dispatch(path, data)
            self._json({"ok": True, "data": result})
        except StaleReviewError as error:
            self._error(str(error), 409)
        except (ValueError, TypeError, KeyError, OSError) as error:
            # Application validation is controlled and Chinese; unexpected
            # exceptions never expose a traceback or credential-bearing input.
            message = str(error) if isinstance(error, ValueError) else "操作未完成，请检查所选资料和保存位置。"
            self._error(message)
        except Exception:
            self._error("操作未完成，已保留现有资料。请重试或重新打开软件。", 500)

    def _dispatch(self, path, data):
        store = self.server.store
        if path == "/api/import":
            paths = data.get("paths")
            if paths is None:
                if self.server.native_window is None:
                    return {"needs_upload": True}
                import webview
                paths = self.server.native_window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True,
                    file_types=(translate("资料与录音 (*.pdf;*.png;*.jpg;*.jpeg;*.pptx;*.docx;*.xlsx;*.vsdx;*.ppt;*.doc;*.xls;*.vsd;*.wav;*.mp3;*.m4a;*.srt;*.vtt)", self._language()),))
                if not paths:
                    return {"cancelled": True}
            if not isinstance(paths, list) and not isinstance(paths, tuple):
                raise ValueError("请选择资料文件。")
            if data.get("project_id") is not None:
                return store.add_files(data["project_id"], [Path(p) for p in paths])
            return store.import_files([Path(p) for p in paths], data.get("title", ""), language=self._language())
        if path == "/api/project":
            return store.project(data["id"])
        if path == "/api/notes":
            return store.save_notes(data["id"], data["text"])
        if path == "/api/edit_text":
            return store.edit_text(data["id"], data["kind"], data["source_id"], data["text"],
                                   start=data.get("start"), end=data.get("end"))
        if path == "/api/ai":
            return store.start_ai(data["id"], refresh=data.get("refresh", False), language=self._language())
        if path == "/api/preferences":
            return store.save_preferences(data)
        if path == "/api/settings":
            return store.save_connection(data)
        if path == "/api/recognition_settings":
            return store.save_recognition(data)
        if path == "/api/download_model":
            return store.download_model()
        if path == "/api/cancel":
            return store.cancel()
        if path == "/api/models":
            return store.discover_models(data)
        if path == "/api/decide":
            version, revision = data.get("review_version"), data.get("decision_revision")
            if not isinstance(version, str) or type(revision) is not int:
                raise StaleReviewError()
            return store.decide(data["id"], data["segment_id"], data.get("unit_ids", []),
                                data.get("decision", "match"), expected_version=version,
                                expected_revision=revision)
        if path == "/api/export":
            base = None
            if self.server.native_window is not None:
                import webview
                selected = self.server.native_window.create_file_dialog(webview.FileDialog.FOLDER)
                if not selected:
                    return {"cancelled": True}
                base = Path(selected[0])
            base = base or self.server.root / "exports"
            destination = base / (translate("图音笔记-", self._language()) + uuid.uuid4().hex[:8])
            return store.export_project(data["id"], destination, language=self._language())
        if path == "/api/open_export":
            # Select only a path already recorded by the store, never arbitrary
            # executable/URL input from an HTTP request.
            project = store.project(data["id"])
            export = project.get("export_path")
            if not export or not Path(export).is_dir():
                raise ValueError("请先保存一份归档。")
            if os.name == "nt":
                os.startfile(export)
            return {"opened": os.name == "nt", "path": export}
        raise ValueError("不支持这个操作。")

    def _upload(self, length):
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data;"):
            raise ValueError("请选择要导入的文件。")
        raw = self.rfile.read(length)
        message = BytesParser(policy=default).parsebytes(
            ("Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + raw)
        title, project_id, items = "", None, []
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            if name == "title" and filename is None:
                title = part.get_payload(decode=True).decode("utf-8")[:200]
            if name == "project_id" and filename is None:
                project_id = part.get_payload(decode=True).decode("utf-8")
            if filename is not None:
                filename = Path(filename.replace("\\", "/")).name
                if not filename or Path(filename).suffix.lower() not in UPLOAD_TYPES:
                    raise ValueError("请导入 PDF、图片、Office 文档、录音或已有转写。")
                items.append((filename, part.get_payload(decode=True)))
        if not items or len(items) > 100 or len({name.casefold() for name, _ in items}) != len(items):
            raise ValueError("一次请选择 1–100 个不重名的文件。")
        incoming = self.server.root / "incoming" / uuid.uuid4().hex
        incoming.mkdir(parents=True)
        paths = []
        for filename, data in items:
            candidate = (incoming / filename).resolve()
            if not candidate.is_relative_to(incoming.resolve()):
                raise ValueError("文件名不正确。")
            candidate.write_bytes(data)
            paths.append(candidate)
        result = self.server.store.add_files(project_id, paths) if project_id is not None else self.server.store.import_files(paths, title, language=self._language())
        # Keep only imported managed originals after successful copying. Each
        # deletion is a verified leaf in this request's new incoming directory.
        for path in paths:
            if path.resolve().is_relative_to(incoming.resolve()):
                path.unlink()
        incoming.rmdir()
        self._json({"ok": True, "data": result})


def start_server(root: Path, resources: Path, port: int = 0, *, store=None):
    server = DesktopServer(Path(root), Path(resources), port, store=store)
    thread = threading.Thread(target=server.serve_forever, name="van-desktop-http", daemon=True)
    thread.start()
    return server, server.origin
