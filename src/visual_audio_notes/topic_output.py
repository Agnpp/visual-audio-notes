"""Portable, replayable topic drafts from separately organized source ledgers.

No retrieval score becomes a playback-page mapping. Quotes and complete source
accounting are retained even when supporting claims are deferred from themes.
The caller additionally validates raw-file binding before invoking this export.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import asdict
import hashlib
import html
from pathlib import Path

from .archive import READER_JS, export_archive, markdown_text, stamp, validate_archive
from .core import Chunk, chunks_from_segments, load_features, read_json, write_json
from .quality_output import _prepared_sources, _validate_result
from .review import source_fingerprint, validate_review
from .topic_notes import LABELS, render_topic_markdown


NOTICE = "所选 AI 生成主题草稿，来源需核对"


def export_topic_archive(out: Path, title: str, audio: Path, pdf: Path, prepared: Path,
                         review_state: dict, quality_result: dict, topic_result: dict,
                         source_credit: str, *, chunks: list[dict] | None = None) -> dict:
    """Render themes and keep local source files, quotations and all deferrals.

    Pass the frozen run's exact chunks when available. The default 40-second
    reconstruction is accepted only if it matches the review source fingerprint;
    it never guesses page timings. Raw PDF/audio hashes are bound by the caller.
    """
    _, segments = load_features(prepared / "features.json")
    if chunks is None:
        chunks = [asdict(c) for c in chunks_from_segments(segments, 40)]
    else:
        chunks = [asdict(c) if isinstance(c, Chunk) else deepcopy(c) for c in chunks]
    source_map = read_json(prepared / "source_map.json")
    units, sources = _prepared_sources(prepared, source_map, chunks)
    validate_review(review_state)
    if review_state["source_fingerprint"] != source_fingerprint(units, [Chunk(**c) for c in chunks]):
        raise ValueError("Topic review belongs to different input sources")
    _validate_result(quality_result, sources)
    ledger = quality_result["ledger"]
    # Revalidate all source-specific references, complete ledger accounting and
    # the input ledger digest. No model-written evidence object is trusted.
    render_topic_markdown(topic_result, ledger, include_evidence_appendix=False)
    by_claim = {c["claim_id"]: c for c in ledger}
    by_chunk = {c["segment_id"]: c for c in chunks}
    review_items = {item["segment_id"]: item for item in review_state["items"]}

    def refs(identifiers: list[str]) -> list[str]:
        return list(dict.fromkeys(e["source_id"] for i in identifiers for e in by_claim[i]["evidence"]))

    def location(identifier: str) -> str:
        if identifier in by_chunk:
            c = by_chunk[identifier]
            return f'录音 {stamp(c["start"])}–{stamp(c["end"])}'
        return f'课件第 {source_map[identifier]["page"]} 页'

    predictions = []
    for chunk in chunks:
        item = review_items[chunk["segment_id"]]
        single_confirmed = item["status"] == "confirmed" and len(item["confirmed_unit_ids"]) == 1
        predictions.append({"segment_id": chunk["segment_id"], "status": item["status"],
                            "unit_id": item["confirmed_unit_ids"][0] if single_confirmed else None})
    topics, claim_groups, deferred_ids = [], [], []
    organized_counts = {}
    for origin in ("audio", "courseware"):
        items = topic_result[origin + "_topics"]
        organized_counts[origin] = len(items)
        for topic in items:
            identifiers = topic["claim_ids"]
            # Origin is determined from ledger membership, not a model-written
            # origin field, title or output ordering.
            if any(by_claim[i]["origin"] != origin for i in identifiers):
                raise ValueError("Topic mixes audio and courseware claims")
            topics.append({"title": LABELS[origin] + "｜AI 主题草稿：" + topic["title"],
                           "key_idea": topic["key_idea"], "explanation": topic["explanation"],
                           "example": "", "pitfall": "", "limitation": "",
                           "evidence_ids": refs(identifiers)})
            claim_groups.append({"topic_id": topic.get("topic_id"), "origin": origin,
                                 "claim_ids": list(identifiers)})
        deferred = topic_result.get("deferred_claims", {}).get(origin, [])
        if deferred:
            deferred_ids.extend(d["claim_id"] for d in deferred)
            descriptions = []
            for item in deferred:
                identifier = item["claim_id"]
                if by_claim[identifier]["origin"] != origin:
                    raise ValueError("Deferred claim has a different origin")
                priority = "关键项" if by_claim[identifier]["importance"] == "key" else "辅助项"
                programmatic = "程序保留；" if item.get("source") == "programmatic_unassigned" else ""
                descriptions.append(f'{priority}（{identifier}）：{programmatic}{item["reason"]}')
            topics.append({"title": LABELS[origin] + "｜尚未纳入主题，保留待核对",
                           "key_idea": "\n\n".join(descriptions),
                           "explanation": "暂缓条目与原文完整保留，不代表已完成重点覆盖。",
                           "example": "", "pitfall": "", "limitation": "",
                           "evidence_ids": refs([d["claim_id"] for d in deferred])})
    gaps = [r for r in quality_result["source_audit"] if r["status"] != "has_quote_verified_claims"]
    if gaps:
        topics.append({"title": "来源缺口 · 仍需检查原件",
                       "key_idea": "\n\n".join(location(r["source_id"]) + "：" + r["reason"] for r in gaps),
                       "explanation": "没有提取到要点不代表该处没有重点。",
                       "example": "", "pitfall": "", "limitation": "",
                       "evidence_ids": [r["source_id"] for r in gaps]})
    uncertainties = ["主题与中文解释是 AI 草稿；引用存在不能证明归纳正确。",
                     "课件主题是独立资料内容，不能据此断言录音讲过或没有讲过。",
                     f"暂缓条目 {len(deferred_ids)} 条，来源缺口 {len(gaps)} 项，原始证据完整保存。"]
    for origin in ("audio", "courseware"):
        uncertainties.extend(LABELS[origin] + "：" + text for text in topic_result["uncertainties"][origin])
    overview = (f'录音主题 {organized_counts["audio"]} 个、课件主题 {organized_counts["courseware"]} 个。'
                f'账本 {len(ledger)} 条均有去向，其中 {len(deferred_ids)} 条尚未纳入主题，'
                f'来源缺口 {len(gaps)} 项。{NOTICE}；引用校验与条目保留不代表内容质量通过。')
    notes = {"overview": overview, "topics": topics, "review_questions": [], "uncertainties": uncertainties}
    checked = export_archive(out, title, audio, pdf, prepared, source_map, chunks, predictions, notes, source_credit)

    # Reword deployment-specific legacy labels and the playback message without
    # changing any frozen archive or fabricating an automatic confirmation.
    reader = READER_JS.replace("播放中的资料对应关系为自动候选，仍需核对", "播放中的资料对应关系有单页人工确认")
    reader = reader.replace("此段资料对应关系待确认", "此段没有单页人工确认，未自动关联课件")
    old_hash = base64.b64encode(hashlib.sha256(READER_JS.encode()).digest()).decode()
    new_hash = base64.b64encode(hashlib.sha256(reader.encode()).digest()).decode()
    document_path, markdown_path = out / "阅读.html", out / "笔记.md"
    document = document_path.read_text(encoding="utf-8")
    document = document.replace(READER_JS, reader).replace(old_hash, new_hash)
    document = document.replace("重点由本地模型整理。", NOTICE + "。")
    markdown = markdown_path.read_text(encoding="utf-8").replace(
        "本页为本地模型生成的研究样例。", NOTICE + "。")

    quote_html, quote_md = [], ["", "## 完整证据索引", "", "<details>",
                                "<summary>展开条目原文与来源</summary>", ""]
    for claim in ledger:
        identifier = claim["claim_id"]
        quote_html.append('<div><h3>' + html.escape(identifier) + " · " + html.escape(LABELS[claim["origin"]]) + "</h3>")
        quote_md += ["### `" + identifier + "` · " + LABELS[claim["origin"]], ""]
        for item in claim["evidence"]:
            source_id = item["source_id"]
            label = location(source_id)
            quote_html.append('<button class="ref" data-ref="' + html.escape(source_id, quote=True) + '">'
                              + html.escape(label) + "</button><blockquote>" + html.escape(item["quote"]) + "</blockquote>")
            if source_id in by_chunk:
                c = by_chunk[source_id]
                target = f'sources/recording{audio.suffix.lower()}#t={c["start"]:.2f},{c["end"]:.2f}'
            else:
                target = f'sources/materials.pdf#page={source_map[source_id]["page"]}'
            quote_md += [f"[{label}]({target})", ""]
            quote_md += ["> " + markdown_text(line) for line in item["quote"].splitlines()] + [""]
        quote_html.append("</div>")
    quote_md += ["</details>", ""]
    if ledger:
        appendix = ('<details class="extra"><summary>展开完整证据账本与原文</summary>'
                    + "".join(quote_html) + "</details>")
        document = document.replace("</section><aside", appendix + "</section><aside", 1)
        markdown += "\n".join(quote_md)
    document_path.write_text(document, encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    write_json(out / "完整证据账本.json", quality_result)
    write_json(out / "主题结构.json", topic_result)
    write_json(out / "匹配核对记录.json", review_state)
    manifest = read_json(out / "project.json")
    accounted = {i for group in claim_groups for i in group["claim_ids"]} | set(deferred_ids)
    if accounted != set(by_claim):
        raise ValueError("Topic archive lost ledger claim accounting")
    manifest["mode"] = "selected_ai_topic_draft"
    manifest["topic_quality"] = {"status": "draft_requires_review", "topic_claim_groups": claim_groups,
                                 "all_ledger_claim_ids": list(by_claim), "deferred_claim_ids": deferred_ids,
                                 "all_claims_accounted_for": True, "source_gap_count": len(gaps),
                                 "notice": NOTICE}
    write_json(out / "project.json", manifest)
    checked = {**checked, **validate_archive(out), "mode": "selected_ai_topic_draft",
               "organized_audio_topics": organized_counts["audio"],
               "organized_courseware_topics": organized_counts["courseware"],
               "ledger_claims": len(ledger), "deferred_claims": len(deferred_ids),
               "source_gaps": len(gaps), "all_ledger_claim_ids_preserved": True,
               "single_page_playback_mappings": sum(p["unit_id"] is not None for p in predictions)}
    write_json(out / "archive_checks.json", checked)
    return checked
