"""Portable review-first rendering for the separate-origin quotation ledger.

The original quote is displayed before its model rewrite. No retrieved candidate
becomes a playback-page mapping. Only one explicitly human-confirmed page may be
used for that optional convenience; multi-page decisions remain in the audit.
"""
from __future__ import annotations

import base64
from collections import Counter
import hashlib
from pathlib import Path
import re

from .archive import READER_JS, export_archive, stamp, validate_archive
from .core import Chunk, Segment, Unit, digest, normalize, read_json, write_json
from .review import source_fingerprint, validate_review


LABELS = {
    "audio": "录音转写 · 需核对原音",
    "courseware": "课件补充 · 是否讲过未核对",
}
KINDS = {"concept": "概念", "distinction": "关键区别", "example": "例子",
         "boundary": "条件与边界", "procedure": "步骤"}
AUDIT_STATUSES = {"has_quote_verified_claims", "no_verified_claims", "extraction_failed"}
NOTICE = ("先核对原文，再检查 AI 改写。引文存在不代表改写正确；"
          "录音转写可能误识别，课件来源不代表本段录音讲过。自动检索只提供候选。")


def _prepared_sources(prepared: Path, source_map: dict, chunks: list[dict]) -> tuple[list[Unit], dict]:
    features = read_json(prepared / "features.json")
    if not isinstance(features, dict) or not isinstance(features.get("units"), list):
        raise ValueError("Prepared feature content is required for quote validation")
    units = [Unit.from_wire(value) for value in features["units"]]
    if len({u.unit_id for u in units}) != len(units) or {u.unit_id for u in units} != set(source_map):
        raise ValueError("Prepared units and display page map disagree")
    parsed = [Segment.from_wire(c) for c in chunks]
    if len({c.segment_id for c in parsed}) != len(parsed):
        raise ValueError("Duplicate transcript source")
    records = {u.unit_id: {"origin": "courseware", "text": u.text} for u in units}
    for chunk in parsed:
        if chunk.segment_id in records:
            raise ValueError("Document and transcript identifiers overlap")
        records[chunk.segment_id] = {"origin": "audio", "text": chunk.text}
    return units, records


def _validate_result(result: dict, sources: dict) -> None:
    ledger = result.get("ledger")
    if not isinstance(ledger, list):
        raise ValueError("Quality result needs a ledger")
    ids = set()
    for claim in ledger:
        if not isinstance(claim, dict):
            raise ValueError("Malformed ledger claim")
        identifier = claim.get("claim_id")
        if (not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier)
                or identifier in ids):
            raise ValueError("Invalid or duplicate claim ID")
        ids.add(identifier)
        origin = claim.get("origin")
        if (origin not in LABELS or claim.get("status") != "quote_verified_draft"
                or claim.get("kind") not in KINDS or claim.get("importance") not in {"key", "supporting"}):
            raise ValueError("Only quote-verified source-specific drafts can be exported")
        if any(not isinstance(claim.get(key), str) for key in
               ("title_draft", "paraphrase_draft", "canonical_text")):
            raise ValueError("Malformed claim draft text")
        evidence = claim.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("A claim needs direct quotation evidence")
        quotations = []
        for item in evidence:
            if not isinstance(item, dict) or item.get("source_id") not in sources:
                raise ValueError("Unknown claim evidence source")
            source = sources[item["source_id"]]
            quote = item.get("quote")
            if source["origin"] != origin:
                raise ValueError("Claim evidence origin mismatch")
            if not isinstance(quote, str) or not normalize(quote) or normalize(quote) not in normalize(source["text"]):
                raise ValueError("Claim quotation no longer occurs in its source")
            if item.get("source_text_sha256", digest(source["text"])) != digest(source["text"]):
                raise ValueError("Claim source text changed")
            quotations.append(quote.strip())
        if normalize(claim["canonical_text"]) != normalize("\n".join(quotations)):
            raise ValueError("Canonical quotation text differs from evidence")
    coverage = result.get("coverage_audit")
    if (not isinstance(coverage, dict) or coverage.get("missing_key_claim_ids")
            or coverage.get("missing_claim_ids")
            or set(coverage.get("rendered_claim_ids", [])) != ids):
        raise ValueError("Missing ledger claims in composition; refusing silent export")
    audits = result.get("source_audit")
    if not isinstance(audits, list) or len(audits) != len(sources):
        raise ValueError("Source audit must account for every input source")
    seen = set()
    for row in audits:
        if (not isinstance(row, dict) or row.get("source_id") not in sources
                or row["source_id"] in seen or row.get("origin") != sources[row["source_id"]]["origin"]
                or row.get("status") not in AUDIT_STATUSES or not isinstance(row.get("reason"), str)):
            raise ValueError("Invalid source audit entry")
        seen.add(row["source_id"])
        expected = {c["claim_id"] for c in ledger
                    if any(e["source_id"] == row["source_id"] for e in c["evidence"])}
        if set(row.get("claim_ids", [])) != expected:
            raise ValueError("Source audit claim accounting disagrees with ledger")
        if (row["status"] == "has_quote_verified_claims") != bool(expected) and row["status"] != "extraction_failed":
            raise ValueError("Source audit coverage status disagrees with ledger")


def export_quality_archive(out: Path, title: str, audio: Path, pdf: Path, prepared: Path,
                           source_map: dict, chunks: list[dict], review_state: dict,
                           result: dict, source_credit: str) -> dict:
    """Export all quoted drafts, source gaps, and explicit matching review audit.

    Original page ordinals are used for display only, after synthesis is complete.
    The result's rewrite is never used as an automatically verified fact.
    """
    units, sources = _prepared_sources(prepared, source_map, chunks)
    validate_review(review_state)
    current = source_fingerprint(units, [Chunk(**c) for c in chunks])
    if review_state["source_fingerprint"] != current:
        raise ValueError("Review belongs to different input sources")
    _validate_result(result, sources)
    by_chunk = {c["segment_id"]: c for c in chunks}

    def source_label(identifier: str) -> str:
        if identifier in by_chunk:
            c = by_chunk[identifier]
            return f'录音 {stamp(c["start"])}–{stamp(c["end"])}'
        return f'课件第 {source_map[identifier]["page"]} 页'

    items = {i["segment_id"]: i for i in review_state["items"]}
    predictions = []
    for chunk in chunks:
        item = items[chunk["segment_id"]]
        confirmed = item["status"] == "confirmed" and len(item["confirmed_unit_ids"]) == 1
        predictions.append({"segment_id": chunk["segment_id"],
                            "unit_id": item["confirmed_unit_ids"][0] if confirmed else None,
                            "status": item["status"]})

    gaps = [r for r in result["source_audit"] if r["status"] != "has_quote_verified_claims"]
    counts = Counter(c["origin"] for c in result["ledger"])
    gap_counts = Counter(r["origin"] for r in gaps)
    failed = sum(r["status"] == "extraction_failed" for r in gaps)
    confirmed = sum(i["status"] == "confirmed" for i in review_state["items"])
    overview = (f'来源分开整理：录音 {counts["audio"]} 条，课件 {counts["courseware"]} 条。'
                f'已提取的 {len(result["ledger"])} 条均保留，不代表原资料重点已完整提取。'
                f'来源缺口：录音 {gap_counts["audio"]} 段、课件 {gap_counts["courseware"]} 页，'
                f'其中提取失败 {failed} 项。人工确认匹配 {confirmed}/{len(chunks)} 段。' + NOTICE)
    topics = []
    for origin in ("audio", "courseware"):
        for claim in (c for c in result["ledger"] if c["origin"] == origin):
            refs = list(dict.fromkeys(e["source_id"] for e in claim["evidence"]))
            quotations = "\n\n".join(f'{source_label(e["source_id"])}：{e["quote"]}' for e in claim["evidence"])
            topics.append({
                "title": f'{LABELS[origin]}｜{KINDS[claim["kind"]]}｜{claim["claim_id"]}｜AI标题待核对：{claim["title_draft"]}',
                "key_idea": quotations,
                "explanation": claim["paraphrase_draft"],
                "example": "", "pitfall": "", "limitation": "", "evidence_ids": refs,
            })
    if gaps:
        topics.append({
            "title": "来源缺口 · 没有核对要点不代表没有重点",
            "key_idea": "\n\n".join(f'{source_label(r["source_id"])}：{r["reason"]}' for r in gaps),
            "explanation": "", "example": "", "pitfall": "", "limitation": "",
            "evidence_ids": [r["source_id"] for r in gaps],
        })
    uncertain = [
        "所有 AI 标题和改写都待核对，原文引文命中不保证改写正确。",
        "录音要点来自自动转写，遇到术语、数字、条件或矛盾请回听原音。",
        "课件补充不代表本段录音讲过，也不能仅凭未匹配就认定老师没有讲。",
        f'来源缺口 {len(gaps)} 项、被拒绝要点 {len(result.get("rejected_claims", []))} 条；详情保留在质量账本.json。',
    ]
    for r in result["source_audit"]:
        if r.get("at_claim_limit"):
            uncertain.append(source_label(r["source_id"]) + "：所在批次达到抽取上限，仍可能遗漏。")
    notes = {"overview": overview, "topics": topics, "review_questions": [], "uncertainties": uncertain}
    export_archive(out, title, audio, pdf, prepared, source_map, chunks, predictions, notes, source_credit)

    # Rendered wording must follow this stage's actual provenance policy. Replace
    # only renderer-authored labels; source strings were escaped by export_archive.
    document_path = out / "阅读.html"
    document = document_path.read_text(encoding="utf-8")
    document = document.replace("<strong>核心重点</strong>", "<strong>原文证据 · 请核对上下文</strong>")
    document = document.replace("<strong>讲解逻辑</strong>", "<strong>AI 改写草稿 · 待核对</strong>")
    document = document.replace("重点由本地模型整理。点击来源核对原页或录音；自动页面对应关系仍需复核。", NOTICE, 1)
    new_script = READER_JS.replace("播放中的资料对应关系为自动候选，仍需核对", "播放对应页来自已保存的单页人工确认")
    new_script = new_script.replace("此段资料对应关系待确认", "此段没有单页人工确认，未自动关联课件")
    old_hash = base64.b64encode(hashlib.sha256(READER_JS.encode()).digest()).decode()
    new_hash = base64.b64encode(hashlib.sha256(new_script.encode()).digest()).decode()
    document = document.replace(READER_JS, new_script, 1).replace("sha256-" + old_hash, "sha256-" + new_hash, 1)
    document_path.write_text(document, encoding="utf-8")
    markdown_path = out / "笔记.md"
    markdown = markdown_path.read_text(encoding="utf-8")
    markdown = markdown.replace("**核心重点**：", "**原文证据 · 请核对上下文**：")
    markdown = markdown.replace("**讲解逻辑**：", "**AI 改写草稿 · 待核对**：")
    markdown_path.write_text(markdown, encoding="utf-8")
    manifest = read_json(out / "project.json")
    manifest.update(mode="quote_grounded_quality_draft", quality={
        "claim_ids": [c["claim_id"] for c in result["ledger"]],
        "source_gap_ids": [r["source_id"] for r in gaps],
        "source_labels": LABELS, "review_required": True,
        "playback_mapping_policy": "Single explicit human-confirmed page only; never retrieval candidates",
    })
    write_json(out / "project.json", manifest)
    write_json(out / "质量账本.json", result)
    write_json(out / "匹配核对记录.json", review_state)
    checked = validate_archive(out)
    checked.update(mode="quote_grounded_quality_draft", ledger_claims=len(result["ledger"]),
                   source_gaps=len(gaps), source_extraction_failures=failed,
                   single_page_playback_mappings=sum(p["unit_id"] is not None for p in predictions),
                   all_ledger_claim_ids_preserved=all(c["claim_id"] in markdown for c in result["ledger"]))
    if not checked["all_ledger_claim_ids_preserved"]:
        raise ValueError("An extracted claim disappeared from the archive")
    write_json(out / "archive_checks.json", checked)
    return checked
