"""Display filtering after a fallible model semantic review, without losing evidence.

This module does not modify any frozen extraction output. All original drafts are
retained in a separate audit artifact. A positive model review is explicitly not
human confirmation, truth verification, or proof of source-level completeness.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import html
from pathlib import Path

from .archive import markdown_text, validate_archive
from .core import read_json, write_json
from .quality_output import KINDS, LABELS, export_quality_archive


SUPPORTED_LABEL = "模型复查未发现问题，仍需核对"
HIDDEN_TITLE = "原文要点 · 改写待修正"
VERDICTS = {"supported", "unsupported", "contradicted", "uncertain"}


def _display_decisions(ledger: list[dict], semantic_review: object) -> tuple[list[dict], list[str]]:
    """Only a unique, well-formed supported+show=True review can display a draft."""
    expected = {c["claim_id"] for c in ledger}
    reviews = defaultdict(list)
    global_errors = []
    if not isinstance(semantic_review, dict) or not isinstance(semantic_review.get("claim_reviews"), list):
        global_errors.append("语义复查缺少有效的 claim_reviews 列表，所有改写保持隐藏。")
        rows = []
    else:
        rows = semantic_review["claim_reviews"]
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("claim_id"), str):
            global_errors.append("语义复查包含无法关联的记录，所有改写保持隐藏。")
        elif row["claim_id"] not in expected:
            global_errors.append("语义复查包含未知要点标识，所有改写保持隐藏。")
        else:
            reviews[row["claim_id"]].append(row)
    decisions = []
    for claim in ledger:
        identifier = claim["claim_id"]
        entries = reviews[identifier]
        reason = "缺少此要点的有效语义复查，暂不展示 AI 改写。"
        verdict = "uncertain"
        show = False
        if global_errors:
            reason = global_errors[0]
        elif len(entries) > 1:
            reason = "同一要点存在重复复查记录，不能据此确定改写是否受原文支持。"
        elif len(entries) == 1:
            row = entries[0]
            raw_verdict = row.get("verdict")
            refs = row.get("evidence_source_ids")
            allowed = {e["source_id"] for e in claim["evidence"]}
            valid_refs = (isinstance(refs, list) and bool(refs)
                          and all(isinstance(ref, str) and ref in allowed for ref in refs)
                          and len(refs) == len(set(refs)))
            valid = (isinstance(raw_verdict, str) and raw_verdict in VERDICTS
                     and type(row.get("show_paraphrase")) is bool and valid_refs)
            if valid:
                verdict = raw_verdict
                show = verdict == "supported" and row["show_paraphrase"] is True
                reason = row.get("reason") if isinstance(row.get("reason"), str) else "复查没有提供有效的文字说明。"
                if not reason.strip():
                    reason = "复查没有提供有效的文字说明。"
                if not show and verdict == "supported":
                    reason = "复查记录未允许展示此改写。" + reason
            else:
                reason = "此要点的复查格式、来源标识或展示许可无效，暂不展示 AI 改写。"
        # The reviewer may quote the rejected rewrite in its explanation. Move
        # those exact draft strings to the audit file rather than displaying them
        # again as prose under a different label. Source quotes remain untouched.
        if not show:
            for field in ("paraphrase_draft", "title_draft"):
                draft = claim.get(field)
                if isinstance(draft, str) and draft:
                    reason = reason.replace(draft, "[原草稿内容见审计文件]")
        decisions.append({"claim_id": identifier, "verdict": verdict,
                          "show_paraphrase": show, "label": SUPPORTED_LABEL if show else "改写未展示 · 请核对原文",
                          "reason": reason})
    return decisions, list(dict.fromkeys(global_errors))


def export_reviewed_archive(out: Path, title: str, audio: Path, pdf: Path, prepared: Path,
                            source_map: dict, chunks: list[dict], review_state: dict,
                            result: dict, source_credit: str, semantic_review: dict,
                            publication_gate: dict | None = None) -> dict:
    """Export reviewed draft presentation while keeping every original quoted claim.

    `semantic_review` uses a `claim_reviews` list. Unknown review identifiers or an
    invalid envelope fail closed for the whole presentation; missing, duplicate,
    or malformed per-claim reviews hide that claim's title and paraphrase. Neither
    model review nor this exporter creates any human matching confirmations.
    """
    original = deepcopy(result)
    display = deepcopy(result)
    decisions, errors = _display_decisions(original.get("ledger", []), semantic_review)
    # Per-claim model support cannot override a failed/incomplete whole-note
    # review. The caller binds the gate to the frozen extraction before export.
    gate_hides_drafts = publication_gate is not None and (
        publication_gate.get("quality_status") != "reviewed_reference")
    if gate_hides_drafts:
        for decision in decisions:
            decision.update(show_paraphrase=False, label="质量复核未通过 · 只展示原文",
                            reason="整体内容质量尚未通过独立复核，本处保留原文证据，自动改写另存审计文件。")
    by_id = {row["claim_id"]: row for row in decisions}
    for claim in display["ledger"]:
        decision = by_id[claim["claim_id"]]
        if decision["show_paraphrase"]:
            claim["paraphrase_draft"] = SUPPORTED_LABEL + "。" + claim["paraphrase_draft"]
        else:
            claim["title_draft"] = HIDDEN_TITLE
            claim["paraphrase_draft"] = ("质量门禁说明：" if gate_hides_drafts else
                                         "语义复查说明（模型判断，仍需核对）：") + decision["reason"]
    # The evidence, origins, IDs, source audits and composition accounting were not
    # changed. The strict quotation exporter validates them before making files.
    base_checked = export_quality_archive(out, title, audio, pdf, prepared, source_map, chunks,
                                          review_state, display, source_credit)
    html_path, markdown_path = out / "阅读.html", out / "笔记.md"
    document = html_path.read_text(encoding="utf-8")
    markdown = markdown_path.read_text(encoding="utf-8")
    for claim in display["ledger"]:
        identifier = claim["claim_id"]
        old_title = (f'{LABELS[claim["origin"]]}｜{KINDS[claim["kind"]]}｜{identifier}'
                     f'｜AI标题待核对：{claim["title_draft"]}')
        short = ("AI 标题草稿：" + claim["title_draft"]
                 if by_id[identifier]["show_paraphrase"] else HIDDEN_TITLE)
        new_title = f'{LABELS[claim["origin"]]}｜{KINDS[claim["kind"]]}｜{short}'
        old_heading = "<h2>" + html.escape(old_title) + "</h2>"
        new_heading = ('<h2 data-claim-id="' + html.escape(identifier, quote=True) + '">'
                       + html.escape(new_title) + "</h2>")
        if document.count(old_heading) != 1:
            raise ValueError("Cannot account for a rendered claim heading")
        document = document.replace(old_heading, new_heading, 1)
        old_markdown = "## " + markdown_text(old_title) + "\n"
        if markdown.count(old_markdown) != 1:
            raise ValueError("Cannot account for a Markdown claim heading")
        markdown = markdown.replace(old_markdown, "## " + markdown_text(new_title) + "\n", 1)
    shown = sum(row["show_paraphrase"] for row in decisions)
    summary = (f"语义复查后展示 AI 改写 {shown} 条，隐藏 {len(decisions) - shown} 条。"
               "所有原文证据仍保留；隐藏的原始草稿和模型复查记录保存在独立审计文件。"
               "模型复查可能遗漏错误，展示不等于人工确认或事实正确。")
    if gate_hides_drafts:
        summary = ("整体内容质量未通过独立复核：本阅读包仅展示原文摘录，全部 AI 标题与改写均已收起。"
                   "录音内容是自动转写，仍需回听核对；课件来源不证明录音讲过。"
                   "完整自动草稿与模型复查记录保留在独立审计文件。")
    document = document.replace("</header>", '<p class="notice">' + html.escape(summary) + "</p></header>", 1)
    markdown = markdown.replace("\n\n", "\n\n" + summary + "\n\n", 1)
    html_path.write_text(document, encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    write_json(out / "原始归纳草稿.json", original)
    audit = deepcopy(semantic_review) if isinstance(semantic_review, dict) else {"invalid_original_review": semantic_review}
    audit["display_policy"] = {"claim_decisions": decisions, "global_errors": errors,
                               "drafts_displayed": shown, "drafts_hidden": len(decisions) - shown,
                               "notice": "A model review is not human confirmation or truth verification"}
    write_json(out / "语义复查.json", audit)
    if publication_gate is not None:
        write_json(out / "质量门禁.json", publication_gate)
    manifest = read_json(out / "project.json")
    # Human-readable headings omit opaque IDs. Their association remains in HTML
    # data attributes, the manifest and complete ledger/audit files.
    for topic in manifest["notes"]["topics"]:
        for claim in display["ledger"]:
            needle = "｜" + claim["claim_id"] + "｜AI标题待核对："
            if needle in topic["title"]:
                prefix, tail = topic["title"].split(needle, 1)
                topic["title"] = prefix + "｜" + ("AI 标题草稿：" + tail if by_id[claim["claim_id"]]["show_paraphrase"] else HIDDEN_TITLE)
                break
    manifest["mode"] = "semantically_reviewed_quote_draft"
    manifest["quality"]["semantic_display"] = {"drafts_displayed": shown,
                                                "drafts_hidden": len(decisions) - shown,
                                                "notice": SUPPORTED_LABEL}
    if publication_gate is not None:
        manifest["quality"]["publication_gate"] = publication_gate
    if gate_hides_drafts:
        manifest["mode"] = "quality_gate_evidence_only"
        manifest["quality"]["semantic_display"]["notice"] = "整体质量未通过，仅展示原文证据"
    write_json(out / "project.json", manifest)
    checked = {**base_checked, **validate_archive(out)}
    checked.update(mode="semantically_reviewed_quote_draft", ledger_claims=len(display["ledger"]),
                   all_ledger_claim_ids_preserved=set(manifest["quality"]["claim_ids"]) == set(by_id),
                   claim_id_retention_scope="project.json, complete ledgers and hidden HTML data attributes",
                   semantic_drafts_displayed=shown, semantic_drafts_hidden=len(decisions) - shown,
                   semantic_global_errors=errors,
                   single_page_playback_mappings=sum(c["unit_id"] is not None for c in manifest["timeline"]))
    if gate_hides_drafts:
        checked["mode"] = "quality_gate_evidence_only"
    if not checked["all_ledger_claim_ids_preserved"]:
        raise ValueError("Reviewed export lost a source claim")
    write_json(out / "archive_checks.json", checked)
    return checked
