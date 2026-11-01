"""Windows desktop entry; same local application can be previewed in a browser."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import sys
import threading


def resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))


def default_data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share")) / "VisualAudioNotes"


def show_error(message: str):
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "图音笔记", 0x10)
    elif sys.stderr is not None:
        print(message, file=sys.stderr)


@contextmanager
def data_lock(root: Path):
    """Do not run two writers against the same user's workspace."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / "desktop.lock").open("a+b") as stream:
        if stream.seek(0, 2) == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("图音笔记已打开，请回到现有窗口继续使用。") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def main(argv=None):
    parser = argparse.ArgumentParser(description="图音笔记桌面版")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--serve-only", action="store_true", help="仅启动本机预览服务，供开发验证")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--ready-file", type=Path, help="将本轮本机地址写入指定验证文件")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not 0 <= args.port <= 65535:
        parser.error("端口超出范围")
    resources = resource_root()
    if not getattr(sys, "frozen", False):
        sys.path.insert(0, str(resources / "src"))
    from visual_audio_notes.desktop_server import start_server

    with data_lock(args.data_dir.resolve()):
        server, url = start_server(args.data_dir.resolve(), resources, port=args.port)
        if args.ready_file:
            args.ready_file.parent.mkdir(parents=True, exist_ok=True)
            args.ready_file.write_text(json.dumps({"url": url, "pid": os.getpid(),
                                                  "mode": "serve-only" if args.serve_only else "desktop"}),
                                       encoding="utf-8")
        try:
            if args.serve_only:
                if sys.stdout is not None:
                    print(url, flush=True)
                stopped = threading.Event()
                signal.signal(signal.SIGINT, lambda *_: stopped.set())
                if hasattr(signal, "SIGTERM"):
                    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
                while not stopped.wait(.25):
                    pass
            else:
                import webview
                webview.settings["ALLOW_FILE_URLS"] = False
                webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = False
                window = webview.create_window(
                    "Visual Audio Notes · 图音笔记", url, width=1300, height=850, min_size=(1000, 680),
                    background_color="#f6f8fc", text_select=True, zoomable=True,
                )
                server.native_window = window
                webview.start(gui="edgechromium", debug=args.debug, private_mode=True)
        finally:
            server.shutdown()
            server.server_close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        show_error(str(error))
        raise SystemExit(1)
    except Exception:
        # Startup details stay out of UI: they can contain private local paths.
        show_error("图音笔记未能启动。请先完整解压文件夹，并确认已安装 Microsoft Edge WebView2 Runtime。\n"
                   "下载地址：https://developer.microsoft.com/microsoft-edge/webview2/\n"
                   "如果仍无法打开，请保留原文件夹并联系项目维护者。")
        raise SystemExit(1)
