from __future__ import annotations

import base64
import hashlib
import html
import json
from pathlib import Path
import re
import shutil
from urllib.parse import quote

from .core import file_hash, write_json


def safe_member(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or "\\" in relative or ":" in relative:
        raise ValueError("Invalid portable resource path")
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("Resource path escapes archive")
    return target


def stamp(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def markdown_text(value: str) -> str:
    value = html.escape(value, quote=False).replace("\\", "\\\\")
    return re.sub(r"([!\[\]])", r"\\\1", value)


READER_JS = r"""
const data = JSON.parse(document.getElementById('archive-data').textContent);
const audio = document.querySelector('audio');
const preview = document.getElementById('preview');
const caption = document.getElementById('caption');
const transcript = document.getElementById('transcript');
function showUnit(id) {
  const unit = data.units[id];
  if (!unit) return;
  preview.src = unit.image;
  caption.textContent = '资料第 ' + unit.page + ' 页';
}
function seek(id) {
  const source = data.evidence[id];
  if (!source) return;
  if (source.kind === 'page') showUnit(id);
  else {
    audio.currentTime = source.start;
    if (source.unit_id) showUnit(source.unit_id);
    document.getElementById('now').textContent = source.label;
  }
}
document.querySelectorAll('[data-ref]').forEach(button => {
  button.addEventListener('click', () => seek(button.dataset.ref));
});
let highlighted = null;
audio.addEventListener('timeupdate', () => {
  const current = data.timeline.find(t => audio.currentTime >= t.start && audio.currentTime < t.end);
  if (!current || current.segment_id === highlighted) return;
  highlighted = current.segment_id;
  document.querySelectorAll('.transcript-row.active').forEach(el => el.classList.remove('active'));
  const row = document.getElementById(current.segment_id);
  if (row) row.classList.add('active');
  if (current.unit_id) showUnit(current.unit_id);
  document.getElementById('now').textContent =
      current.unit_id ? '播放中的资料对应关系为自动候选，仍需核对' : '此段资料对应关系待确认';
});
if (Object.keys(data.units).length) showUnit(Object.keys(data.units)[0]);
"""


def export_archive(out: Path, title: str, audio: Path, pdf: Path, prepared: Path,
                   source_map: dict, chunks: list[dict], predictions: list[dict],
                   notes: dict, source_credit: str) -> dict:
    if out.exists() and any(out.iterdir()):
        raise ValueError("Archive destination must be empty; existing archives are preserved")
    out.mkdir(parents=True, exist_ok=True)
    (out / "sources").mkdir()
    (out / "assets").mkdir()
    audio_name = "sources/recording" + audio.suffix.lower()
    pdf_name = "sources/materials.pdf"
    shutil.copy2(audio, out / audio_name)
    shutil.copy2(pdf, out / pdf_name)
    units = {}
    evidence = {}
    for identifier, locator in sorted(source_map.items(), key=lambda pair: pair[1]["page"]):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier):
            raise ValueError("Unsafe document identifier")
        filename = f"assets/{identifier}.png"
        source = safe_member(prepared / "pages", locator["image"])
        shutil.copy2(source, out / filename)
        units[identifier] = {"page": locator["page"], "image": filename}
        evidence[identifier] = {"kind": "page", "label": f"资料第 {locator['page']} 页"}
    by_id = {p["segment_id"]: p for p in predictions}
    timeline = []
    for chunk in chunks:
        match = by_id[chunk["segment_id"]]
        evidence[chunk["segment_id"]] = {
            "kind": "audio", "label": stamp(chunk["start"]) + "-" + stamp(chunk["end"]),
            "start": chunk["start"], "end": chunk["end"], "unit_id": match["unit_id"],
        }
        timeline.append({**chunk, "unit_id": match["unit_id"], "status": match["status"]})
    references = {ref for topic in notes["topics"] for ref in topic["evidence_ids"]}
    if references - evidence.keys():
        raise ValueError("Notes contain unknown archive references")

    def buttons(refs: list[str]) -> str:
        return " ".join(
            f'<button class="ref" data-ref="{html.escape(ref, quote=True)}">'
            f'{html.escape(evidence[ref]["label"])}</button>' for ref in refs
        )

    fields = [("key_idea", "核心重点"), ("explanation", "讲解逻辑"),
              ("example", "关键例子"), ("pitfall", "容易混淆"), ("limitation", "适用边界")]
    cards, markdown = [], [f"# {markdown_text(title)}", "", markdown_text(notes["overview"]), ""]
    for topic in notes["topics"]:
        paragraphs = []
        markdown += ["## " + markdown_text(topic["title"]), ""]
        for field, label in fields:
            if topic[field].strip():
                paragraphs.append(f'<p><strong>{label}</strong> {html.escape(topic[field])}</p>')
                markdown += [f"**{label}**：{markdown_text(topic[field])}", ""]
        cards.append('<article><h2>' + html.escape(topic["title"]) + '</h2>' +
                     "".join(paragraphs) + '<div class="refs">' +
                     buttons(topic["evidence_ids"]) + "</div></article>")
        links = []
        for ref in topic["evidence_ids"]:
            item = evidence[ref]
            target = (pdf_name + f'#page={units[ref]["page"]}' if item["kind"] == "page"
                      else audio_name + f'#t={item["start"]:.2f},{item["end"]:.2f}')
            links.append(f'[{item["label"]}]({target})')
        markdown += ["来源：" + " · ".join(links), ""]
    questions = "".join(f"<li>{html.escape(q)}</li>" for q in notes["review_questions"])
    uncertainties = "".join(f"<li>{html.escape(q)}</li>" for q in notes["uncertainties"])
    markdown += ["## 复习问题", ""] + ["- " + markdown_text(q) for q in notes["review_questions"]]
    markdown += ["", "## 待确认", ""] + ["- " + markdown_text(q) for q in notes["uncertainties"]]
    markdown += ["", "## 来源与验证边界", "", source_credit, "",
                 "本页为本地模型生成的研究样例。引用已检查，事实与匹配仍需人工核对。", ""]
    (out / "笔记.md").write_text("\n".join(markdown), encoding="utf-8")
    (out / "转写稿.md").write_text("\n\n".join(
        f'[{stamp(c["start"])}-{stamp(c["end"])}] {c["text"]}' for c in chunks
    ), encoding="utf-8")
    (out / "来源与使用说明.txt").write_text(source_credit, encoding="utf-8")
    rows = "".join(
        f'<div id="{html.escape(c["segment_id"], quote=True)}" class="transcript-row">'
        + buttons([c["segment_id"]]) + f'<span>{html.escape(c["text"])}</span></div>'
        for c in chunks
    )
    payload = json.dumps({"units": units, "evidence": evidence, "timeline": timeline},
                         ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026")
    script_hash = base64.b64encode(hashlib.sha256(READER_JS.encode()).digest()).decode()
    csp = ("default-src 'none'; img-src 'self' file: data:; media-src 'self' file:; "
           f"style-src 'unsafe-inline'; script-src 'sha256-{script_hash}'; "
           "connect-src 'none'; object-src 'none'; base-uri 'none'")
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{html.escape(csp, quote=True)}">
<title>{html.escape(title)}</title><style>
:root{{font-family:"Segoe UI","Microsoft YaHei",sans-serif;color:#20302c;background:#f5f4ef}}
*{{box-sizing:border-box}}body{{margin:0}}header{{padding:30px 5vw 16px;border-bottom:1px solid #d9ded8}}
h1{{font-size:27px;margin:0 0 10px}}h2{{font-size:20px}}p{{line-height:1.85}}
.badge{{font-size:12px;color:#566f63}}.layout{{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(320px,.9fr);gap:24px;max-width:1400px;margin:24px auto;padding:0 4vw 120px}}
article,.extra{{background:white;padding:22px 26px;border-radius:12px;border:1px solid #e1e5dc;margin-bottom:18px}}
article p strong{{display:block;font-size:12px;color:#517467;margin:14px 0 4px}}
.ref{{border:1px solid #d2ded5;border-radius:5px;background:#eef4ef;color:#295c43;padding:5px 9px;margin:4px;cursor:pointer;font:inherit;font-size:12px}}
.source{{position:sticky;top:20px;align-self:start}}.source img{{width:100%;border:1px solid #dbe1d7;background:white;border-radius:8px}}
#caption{{font-size:13px;color:#50645c}}footer{{position:fixed;bottom:0;left:0;right:0;background:#fffef8;border-top:1px solid #d5ded5;padding:10px 5vw;display:flex;align-items:center;gap:20px}}
audio{{width:50%;min-width:270px}}#now{{font-size:12px;color:#5d6f64}}summary{{cursor:pointer}}
.transcript-row{{padding:10px 5px;border-bottom:1px solid #eef0ea;line-height:1.7;font-size:13px}}.active{{background:#edf5e9}}
.notice{{color:#6b735f;font-size:12px;line-height:1.6}}li{{line-height:1.8}}
@media(max-width:850px){{.layout{{display:block}}.source{{position:static}}footer{{display:block}}audio{{width:100%}}}}
</style></head><body>
<header><div class="badge">图音笔记 · 可离线归档的研究样例</div><h1>{html.escape(title)}</h1>
<p>{html.escape(notes["overview"])}</p><div class="notice">重点由本地模型整理。点击来源核对原页或录音；自动页面对应关系仍需复核。</div></header>
<main class="layout"><section>{''.join(cards)}
<div class="extra"><h2>复习问题</h2><ul>{questions}</ul><h2>待确认</h2><ul>{uncertainties}</ul></div>
<details class="extra"><summary>查看完整讲解与时间索引</summary><div id="transcript">{rows}</div></details>
<details class="extra"><summary>来源与使用说明</summary><p>{html.escape(source_credit)}</p></details>
</section><aside class="source"><img id="preview" alt="点击来源查看课件"><p id="caption"></p>
<p class="notice">阅读包包含原课件、独立录音和转写，不需要运行模型或联网。</p></aside></main>
<footer><audio controls preload="metadata" src="{quote(audio_name)}"></audio><span id="now">点击时间标签定位录音，然后按播放。</span></footer>
<script type="application/json" id="archive-data">{payload}</script>
<script>{READER_JS}</script></body></html>"""
    (out / "阅读.html").write_text(document, encoding="utf-8")
    manifest = {
        "schema_version": 1, "title": title, "mode": "research_local_model",
        "sources": {"audio": audio_name, "document": pdf_name},
        "units": units, "evidence": evidence, "timeline": timeline, "notes": notes,
        "provenance": source_credit,
    }
    write_json(out / "project.json", manifest)
    checked = validate_archive(out)
    write_json(out / "archive_checks.json", checked)
    return checked


def validate_archive(root: Path) -> dict:
    from .core import read_json
    manifest = read_json(root / "project.json")
    resources = list(manifest["sources"].values()) + [u["image"] for u in manifest["units"].values()]
    for relative in resources:
        if not safe_member(root, relative).is_file():
            raise ValueError("Missing archive resource: " + relative)
    for topic in manifest["notes"]["topics"]:
        if any(ref not in manifest["evidence"] for ref in topic["evidence_ids"]):
            raise ValueError("Broken note evidence")
    return {"valid": True, "local_resources": len(resources),
            "topics": len(manifest["notes"]["topics"]),
            "verified": "Local resources and reference IDs only; not a semantic fact check",
            "notes_sha256": file_hash(root / "笔记.md")}
