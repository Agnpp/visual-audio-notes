"""English reading entry for the same portable, source-preserving archive.

Only application labels are English. Original notes, transcripts and quotations
are never translated, and AI output is read from the already validated export.
The original archive files stay intact for compatibility with existing readers.
"""
from __future__ import annotations

from copy import deepcopy
import base64
import hashlib
import html
import json
from pathlib import Path
import shutil
from urllib.parse import quote

from .archive import READER_JS, markdown_text, safe_member, stamp
from .core import file_hash, read_json
from .desktop_locale import translate
from .task_runtime import atomic_json


def export_english_reader(project: Path, out: Path):
    meta = read_json(project / "project.json")
    content = read_json(project / "prepared/features.json")
    mapping = read_json(project / "prepared/source_map.json")
    manifest = read_json(out / "project.json")
    retained = read_json(out / "原件清单.json")["originals"]
    units = deepcopy(manifest.get("units", {}))
    evidence = deepcopy(manifest.get("evidence", {}))
    timeline = deepcopy(manifest.get("timeline", content["segments"]))
    for item in timeline:
        item.setdefault("unit_id", None)
        item.setdefault("status", "unreviewed")
        evidence[item["segment_id"]] = {"kind": "audio", "label": stamp(item["start"]) + "–" + stamp(item["end"]),
            "start": item["start"], "end": item["end"], "unit_id": item["unit_id"]}
    for uid, locator in mapping.items():
        if uid not in units:
            relative = "assets/" + uid + ".png"
            target = safe_member(out, relative)
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(safe_member(project / "prepared/pages", locator["image"]), target)
            units[uid] = {"page": locator["page"], "image": relative}
        evidence[uid] = {"kind": "page", "label": "Document page " + str(locator["page"])}
    sources = manifest.get("sources", {})
    if not sources.get("audio") and meta.get("audio"):
        original = next(item for item in meta["files"] if item["kind"] == "audio")
        sources["audio"] = next(item["path"] for item in retained if item["sha256"] == original["sha256"])
    escaped = html.escape
    markdown = ["# " + markdown_text(meta["title"]), "", "Source materials and notes. AI drafts and recognized text require review.", ""]
    cards = []

    def add_section(title, text):
        cards.append("<article><h2>" + escaped(title) + "</h2><p>" + escaped(text) + "</p></article>")
        markdown.extend(["## " + markdown_text(title), "", markdown_text(text), ""])

    quality_path, topic_path = out / "完整证据账本.json", out / "主题结构.json"
    quality = read_json(quality_path) if quality_path.is_file() else None
    topics = read_json(topic_path) if topic_path.is_file() else None
    claims = {row["claim_id"]: row for row in quality["ledger"]} if quality else {}

    def reference(ref):
        item = evidence[ref]
        label = item["label"]
        target = (sources.get("document", "sources/materials.pdf") + "#page=" + str(units[ref]["page"]) if item["kind"] == "page"
                  else sources.get("audio", "") + "#t=" + str(item["start"]) + "," + str(item["end"]))
        return '<button class="ref" data-ref="' + escaped(ref, quote=True) + '">' + escaped(label) + '</button>', "[" + label + "](" + quote(target, safe="/#=,.") + ")"

    if topics:
        for origin, label in [("audio", "Recorded speech · Based on the transcript"), ("courseware", "Document additions · Not confirmed in the recording")]:
            cards.append("<h2>" + label + "</h2>")
            markdown.extend(["## " + label, ""])
            for topic in topics[origin + "_topics"]:
                body = "<h3>" + escaped(topic["title"]) + "</h3><p>" + escaped(topic["key_idea"]) + "</p><p>" + escaped(topic["explanation"]) + "</p>"
                markdown.extend(["### " + markdown_text(topic["title"]), "", markdown_text(topic["key_idea"]), "", markdown_text(topic["explanation"]), ""])
                cards.append("<article>" + body + "<details><summary>Original evidence</summary>")
                for identifier in topic["claim_ids"]:
                    for item in claims[identifier]["evidence"]:
                        button, link = reference(item["source_id"])
                        cards.append(button + "<blockquote>" + escaped(item["quote"]) + "</blockquote>")
                        markdown.extend([link, "", *["> " + markdown_text(line) for line in item["quote"].splitlines()], ""])
                cards.append("</details></article>")
            for deferred in topics.get("deferred_claims", {}).get(origin, []):
                add_section("Deferred " + deferred["claim_id"], deferred["reason"])
            for text in topics.get("uncertainties", {}).get(origin, []):
                add_section("Uncertainty · " + label, text)
        # Keep all claims, including deferred evidence, available for inspection.
        cards.append('<details class="extra"><summary>Complete evidence ledger, including deferred claims</summary>')
        markdown.extend(["## Complete evidence ledger", ""])
        for identifier, claim in claims.items():
            cards.append("<h3>" + escaped(identifier) + " · " + ("Recorded speech" if claim["origin"] == "audio" else "Document addition") + "</h3>")
            for item in claim["evidence"]:
                button, link = reference(item["source_id"])
                cards.append(button + "<blockquote>" + escaped(item["quote"]) + "</blockquote>")
                markdown.extend(["### " + identifier, "", link, "", *["> " + markdown_text(line) for line in item["quote"].splitlines()], ""])
        cards.append("</details>")
        for row in quality["source_audit"]:
            if row["status"] != "has_quote_verified_claims":
                add_section("Source gap · " + row["source_id"], row["reason"])
    else:
        add_section("No AI summary generated", "This archive preserves sources, transcripts and manual notes. Recognition is separate from summarizing key points.")
    if meta["manual_notes"].strip():
        add_section("My notes", meta["manual_notes"])
    for warning in meta.get("preparation_warnings", []):
        add_section("Check the source", translate(warning, "en"))
    links = []
    for item in retained:
        links.append('<li><a href="' + escaped(quote(item["path"], safe="/"), quote=True) + '">' + escaped(item["filename"]) + "</a></li>")
        markdown.extend(["- [" + markdown_text(item["filename"]) + "](" + quote(item["path"], safe="/") + ")"])
    recognition_file = out / "识别记录.json"
    if recognition_file.is_file():
        recognition_href = quote(recognition_file.name)
        links.append(f'<li><a href="{recognition_href}">Recognition record</a></li>')
        markdown.append(f"- [Recognition record]({recognition_href})")
    cards.append('<details class="extra"><summary>Original files and machine-readable records</summary><ul>' + "".join(links) + '</ul><p>Recognition results, corrections, original and revised transcripts, source hashes and usage records are retained alongside this reader.</p></details>')
    if content["units"]:
        cards.append('<details class="extra"><summary>Extracted and corrected document text</summary>')
        for unit in content["units"]:
            button, link = reference(unit["unit_id"])
            cards.append(button + '<pre>' + escaped(unit["text"]) + '</pre>')
            markdown.extend(["", "## Document text · " + unit["unit_id"], "", link, "", markdown_text(unit["text"]), ""])
        cards.append('</details>')
    rows = []
    for cue in timeline:
        button, _ = reference(cue["segment_id"])
        rows.append('<div class="transcript-row" id="' + escaped(cue["segment_id"], quote=True) + '">' + button + '<p>' + escaped(cue["text"]) + '</p></div>')
    script = READER_JS.replace("资料第 ", "Document page ").replace(" 页", "")
    script = script.replace("播放中的资料对应关系为自动候选，仍需核对", "This single-page match was explicitly confirmed by you")
    script = script.replace("此段资料对应关系待确认", "No single-page match has been confirmed for this segment")
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    csp = "default-src 'none'; img-src 'self' file: data:; media-src 'self' file:; style-src 'unsafe-inline'; script-src 'sha256-" + digest + "'; connect-src 'none'; object-src 'none'; base-uri 'none'"
    payload = json.dumps({"units": units, "evidence": evidence, "timeline": timeline}, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026")
    document = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="' + escaped(csp, quote=True) + '"><title>' + escaped(meta["title"]) + '</title><style>'
        'body{font:15px/1.7 Segoe UI,Arial,sans-serif;color:#202124;background:#f6f8fc;margin:0}header,main{max-width:1200px;margin:auto;padding:24px}h1{font-size:28px}main{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(280px,1fr);gap:24px;padding-bottom:130px}article,.extra{background:white;padding:22px;border:1px solid #e1e5ee;border-radius:16px;margin:14px 0}p,pre,blockquote{white-space:pre-wrap;overflow-wrap:anywhere}pre{font:inherit}blockquote{border-left:3px solid #c2d7ff;padding-left:14px}.source{align-self:start;position:sticky;top:16px}.source img{width:100%;border-radius:12px}.ref{background:#edf3ff;border:1px solid #d3e3fd;border-radius:8px;color:#0b57d0;padding:7px;cursor:pointer;margin:4px}footer{position:fixed;bottom:0;background:white;border-top:1px solid #dde3ee;width:100%;padding:12px;box-sizing:border-box}audio{max-width:700px;width:100%}.active{background:#edf3ff}@media(max-width:850px){main{display:block}.source{position:static}}'
        '</style></head><body><header><small>Visual Audio Notes · Portable archive · AI drafts require review</small><h1>' + escaped(meta["title"]) + '</h1><p>Source references locate evidence; they do not verify interpretations. Document additions are not proof of what was said in the recording.</p></header><main><section>' + ''.join(cards) + '<details class="extra"><summary>Transcript and recording times</summary><div id="transcript">' + ''.join(rows) + '</div></details></section><aside class="source"><img id="preview" alt="Select a document source to preview it"><p id="caption"></p><p>Generated Office reading pages may differ from the original layout. Page numbers here index the archive preview.</p></aside></main><footer><audio controls preload="metadata" src="' + escaped(quote(sources.get("audio", ""), safe="/"), quote=True) + '"></audio><span id="now">Click a time, then play the recording.</span></footer><script type="application/json" id="archive-data">' + payload + '</script><script>' + script + '</script></body></html>')
    (out / "index.html").write_text(document, encoding="utf-8")
    (out / "notes.md").write_text("\n".join(markdown), encoding="utf-8")
    manifest.update(reader_language="en", reader_entry="index.html", notes_entry="notes.md")
    atomic_json(out / "project.json", manifest)
    checks = read_json(out / "archive_checks.json") if (out / "archive_checks.json").is_file() else {}
    checks.update(english_reader=True, english_notes_sha256=file_hash(out / "notes.md"))
    atomic_json(out / "archive_checks.json", checks)
