import json
from pathlib import Path
import urllib.error
import urllib.request

import pytest

from visual_audio_notes.desktop_server import start_server
from test_desktop_completion import materials


class Store:
    def __init__(self):
        self.calls = []
        self._preferences = {}

    def save_preferences(self, values):
        self._preferences = dict(values)
        return self._preferences

    def state(self):
        return {"projects": [], "connection": {"key_configured": False}, "job": None}

    def import_files(self, paths, title="", *, language="zh-CN"):
        self.calls.append({"paths": paths, "title": title, "bytes": [p.read_bytes() for p in paths]})
        return {"id": "p-test", "title": title}

    def project(self, identifier):
        return {"id": identifier}

    def add_files(self, identifier, paths):
        self.calls.append({"project_id": identifier, "bytes": [p.read_bytes() for p in paths]})
        return {"id": identifier, "title": "existing"}


@pytest.fixture
def app(tmp_path):
    ui = tmp_path / "resources/src/visual_audio_notes/desktop_ui"
    ui.mkdir(parents=True)
    (ui / "index.html").write_text('<script>window.VAN_TOKEN="__VAN_TOKEN__";window.VAN_LANGUAGE="__VAN_LANGUAGE__"</script>', encoding="utf-8")
    (ui / "en.json").write_text('{"全部资料":"All projects"}', encoding="utf-8")
    (ui / "i18n.js").write_text("// language bootstrap", encoding="utf-8")
    (ui / "app.js").write_text("// test", encoding="utf-8")
    (ui / "app.css").write_text("body{}", encoding="utf-8")
    root = tmp_path / "data"
    store = Store()
    server, url = start_server(root, tmp_path / "resources", store=store)
    yield server, url, root, store
    server.shutdown()
    server.server_close()


def request(app, path, data=None, headers=None, auth=True):
    server, url, *_ = app
    merged = {"X-Van-Token": server.token} if auth else {}
    merged.update(headers or {})
    req = urllib.request.Request(url + path, data=data, headers=merged)
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def test_bootstrap_is_local_and_api_requires_unguessable_session(app):
    server, *_ = app
    status, headers, body = request(app, "/", auth=False)
    assert status == 200 and server.token.encode() in body
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert request(app, "/api/state", auth=False)[0] == 403
    assert request(app, "/api/state", headers={"Origin": "https://external.example"})[0] == 403
    assert request(app, "/api/state", headers={"Host": "external.example"})[0] == 403
    status, _, body = request(app, "/api/state")
    assert status == 200
    assert json.loads(body)["data"]["native_dialogs"] is False
    assert request(app, "/../app.js")[0] == 404


def test_media_requires_session_and_handles_range_for_audio(app):
    server, _, root, _ = app
    path = root / "projects/p1/originals/recording.wav"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"0123456789")
    endpoint = "/media/projects/p1/originals/recording.wav"
    assert request(app, endpoint, auth=False)[0] == 403
    status, headers, body = request(app, endpoint + "?token=" + server.token, auth=False,
                                    headers={"Range": "bytes=2-5"})
    assert (status, body) == (206, b"2345")
    assert headers["Content-Range"] == "bytes 2-5/10"
    assert request(app, endpoint, headers={"Range": "bytes=-3"})[2] == b"789"
    assert request(app, endpoint, headers={"Range": "bytes=100-200"})[0] == 416
    assert request(app, "/media/projects/../../settings.json")[0] == 404
    assert request(app, "/media/projects/p1/secret.json")[0] == 404


def test_confirmation_uses_the_viewed_sources_and_decision_revision(tmp_path, materials):
    from visual_audio_notes.core import read_json
    from visual_audio_notes.desktop_store import DesktopStore

    subtitle = materials["subtitle"]
    subtitle.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nFirst explanation.\n\n"
                        "00:00:01.000 --> 00:00:01.900\nSecond explanation.\n", encoding="utf-8")
    store = DesktopStore(tmp_path / "workspace")
    initial = store.import_files([materials["pdf"], materials["audio"], subtitle])
    pid, uid = initial["id"], initial["pages"][0]["unit_id"]
    first, second = [cue["segment_id"] for cue in initial["segments"]]
    root = store.root / "projects" / pid
    server, url = start_server(store.root, Path(__file__).resolve().parents[1], store=store)

    def payload(view, sid):
        revision = next(item["revision"] for item in view["review_items"] if item["segment_id"] == sid)
        return {"id": pid, "segment_id": sid, "unit_ids": [uid], "review_version": view["review_version"],
                "decision_revision": revision}

    def confirm(data):
        status, _, raw = request((server, url), "/api/decide", json.dumps(data).encode(),
                                 {"Content-Type": "application/json", "X-Van-Language": "en"})
        return status, json.loads(raw)

    try:
        old = payload(initial, first)
        assert confirm({k: v for k, v in old.items() if k != "review_version"})[0] == 409
        assert confirm(old)[0] == 200
        assert confirm(old)[0] == 409
        assert confirm(payload(initial, second))[0] == 200
        before_edit = store.project(pid)
        revised = store.edit_text(pid, "transcript", first, "Revised first explanation.")
        statuses = {item["segment_id"]: item["status"] for item in revised["review_items"]}
        assert statuses == {first: "needs_review", second: "confirmed"}
        status, body = confirm(payload(before_edit, first))
        assert status == 409 and "Review the latest version" in body["error"]
        review = read_json(root / "prepared/review.json")
        assert [event["event"] for event in review["history"]] == [
            "decision", "decision", "inputs_changed", "decision_carried"]
        assert review["history"][-2]["invalidated_decisions"] == 1
        assert confirm(payload(revised, first))[0] == 200
        assert all(item["status"] == "confirmed" for item in DesktopStore(store.root).project(pid)["review_items"])
    finally:
        server.shutdown()
        server.server_close()


def test_mutation_requires_session_and_browser_import_falls_back(app):
    payload = json.dumps({}).encode()
    assert request(app, "/api/import", payload, auth=False)[0] == 403
    status, _, body = request(app, "/api/import", payload)
    assert status == 200 and json.loads(body)["data"] == {"needs_upload": True}
    assert request(app, "/api/import", b"[]")[0] == 400


def test_language_bootstrap_defaults_headers_and_saved_choice(app):
    assert b'VAN_LANGUAGE="en"' in request(app, '/', headers={'Accept-Language':'en-US,en;q=0.9'})[2]
    assert b'VAN_LANGUAGE="zh-CN"' in request(app, '/')[2]
    assert request(app, '/api/preferences', b'{"language":"en"}',auth=False)[0]==403
    assert request(app, '/api/preferences', b'{"language":"en"}')[0]==200
    assert b'VAN_LANGUAGE="en"' in request(app, '/')[2]
    assert b'VAN_LANGUAGE="zh-CN"' in request(app, '/',headers={'X-Van-Language':'zh-CN'})[2]
    response=request(app,'/api/unsupported',b'{}',headers={'X-Van-Language':'en'})
    assert json.loads(response[2])['error']=='This action is not supported.'
    assert request(app,'/i18n.js')[0]==200
    assert json.loads(request(app,'/translations.json')[2])['全部资料']=='All projects'


def test_browser_upload_copies_only_selected_files_to_store(app):
    boundary = "TEST-BOUNDARY"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"title\"\r\n\r\n测试资料\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"lesson.pdf\"\r\n"
            f"Content-Type: application/pdf\r\n\r\n%PDF fixture\r\n--{boundary}--\r\n").encode()
    status, _, result = request(app, "/api/upload", body,
                                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    assert status == 200 and json.loads(result)["data"]["title"] == "测试资料"
    assert app[3].calls[0]["bytes"] == [b"%PDF fixture"]
    assert not any((app[2] / "incoming").iterdir())


def test_native_cancel_never_imports_or_exports(app):
    class Window:
        def create_file_dialog(self, *args, **kwargs):
            return None

    app[0].native_window = Window()
    # pywebview is not a research dependency: stub its public enum only.
    import sys
    from types import SimpleNamespace
    from unittest.mock import patch
    fake = SimpleNamespace(FileDialog=SimpleNamespace(OPEN=10, FOLDER=20))
    with patch.dict(sys.modules, {"webview": fake}):
        assert json.loads(request(app, "/api/import", b"{}")[2])["data"] == {"cancelled": True}
        assert json.loads(request(app, "/api/export", b'{"id":"p1"}')[2])["data"] == {"cancelled": True}
    assert not app[3].calls


@pytest.mark.parametrize("transport", ["json_paths", "browser_upload", "native_picker"])
def test_adding_files_keeps_the_selected_project_in_each_transport(app, tmp_path, transport):
    selected = tmp_path / "selected.pdf"
    selected.write_bytes(b"%PDF additional source")
    if transport == "browser_upload":
        body = (b'--APPEND\r\nContent-Disposition: form-data; name="project_id"\r\n\r\np-existing\r\n'
                b'--APPEND\r\nContent-Disposition: form-data; name="files"; filename="selected.pdf"\r\n'
                b'Content-Type: application/pdf\r\n\r\n%PDF additional source\r\n--APPEND--\r\n')
        status, _, result = request(app, "/api/upload", body,
                                    headers={"Content-Type": "multipart/form-data; boundary=APPEND"})
    else:
        payload = {"project_id": "p-existing"}
        if transport == "json_paths":
            payload["paths"] = [str(selected)]
            status, _, result = request(app, "/api/import", json.dumps(payload).encode())
        else:
            import sys
            from types import SimpleNamespace
            from unittest.mock import patch

            app[0].native_window = SimpleNamespace(create_file_dialog=lambda *a, **k: [str(selected)])
            fake = SimpleNamespace(FileDialog=SimpleNamespace(OPEN=10))
            with patch.dict(sys.modules, {"webview": fake}):
                status, _, result = request(app, "/api/import", json.dumps(payload).encode())
    assert status == 200
    assert json.loads(result)["data"]["id"] == "p-existing"
    assert app[3].calls == [{"project_id": "p-existing", "bytes": [b"%PDF additional source"]}]
