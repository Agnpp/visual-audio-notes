"""Quote-grounded draft ledger, separate from the frozen phase-2 summarizer.

Quote validation proves that text occurs in a supplied source. It does NOT prove
that an ASR transcript is correct, or that a model's paraphrase follows from it.
Consequently original quotations are canonical; model rewrites remain drafts.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
import json
import re

from .core import Chunk, Unit, digest, normalize, read_json, write_json
from .step_cache import CheckedResponse, persistent_settings, resolve_step

if TYPE_CHECKING:
    from .notes import LocalModel


ORIGINS = {"audio", "courseware"}
KINDS = {"concept", "distinction", "example", "boundary", "procedure"}
IMPORTANCE = {"key", "supporting"}
SOURCE_LABELS = {
    "audio": "录音原话（独立转写，仍需核对）",
    "courseware": "课件内容/补充，未证明录音讲到",
}
ENGLISH_SOURCE_LABELS = {"audio": "Recording transcript, requires review",
                         "courseware": "Document additions, not confirmed in the recording"}
CLAIM_FIELDS = {"origin", "kind", "importance", "title", "statement", "evidence"}
EVIDENCE_FIELDS = {"source_id", "quote"}
SCHEMA_VERSION = "quote-ledger-v1"
SPAN_VERSION = "exact-source-spans-v1"


def extraction_schema(source_ids: set[str], origin: str, max_claims: int = 20) -> dict:
    """One origin per request: courseware cannot be laundered into audio claims."""
    if origin not in ORIGINS or not source_ids:
        raise ValueError("Extraction needs one known origin and at least one source")
    if isinstance(max_claims, bool) or not isinstance(max_claims, int) or not 1 <= max_claims <= 100:
        raise ValueError("Invalid claim limit")
    return {
        "type": "object", "additionalProperties": False,
        "required": ["claims", "uncertain_source_ids"],
        "properties": {
            "claims": {
                "type": "array", "minItems": 0, "maxItems": max_claims,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": sorted(CLAIM_FIELDS),
                    "properties": {
                        "origin": {"type": "string", "enum": [origin]},
                        "kind": {"type": "string", "enum": sorted(KINDS)},
                        "importance": {"type": "string", "enum": sorted(IMPORTANCE)},
                        "title": {"type": "string"},
                        "statement": {"type": "string"},
                        "evidence": {
                            "type": "array", "minItems": 1, "maxItems": 3,
                            "items": {
                                "type": "object", "additionalProperties": False,
                                "required": sorted(EVIDENCE_FIELDS),
                                "properties": {
                                    "source_id": {"type": "string", "enum": sorted(source_ids)},
                                    "quote": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
            "uncertain_source_ids": {
                "type": "array", "items": {"type": "string", "enum": sorted(source_ids)},
            },
        },
    }


def span_extraction_schema(source_ids: set[str], span_ids: set[str], origin: str,
                           max_claims: int = 20) -> dict:
    """The model selects opaque IDs; it never copies or edits source quotations."""
    if not span_ids:
        raise ValueError("Span extraction needs at least one span")
    schema = extraction_schema(source_ids, origin, max_claims)
    schema["properties"]["claims"]["items"]["properties"]["evidence"]["items"] = {
        "type": "object", "additionalProperties": False, "required": ["span_id"],
        "properties": {"span_id": {"type": "string", "enum": sorted(span_ids)}},
    }
    return schema


def make_source_spans(sources: dict[str, dict], target_characters: int = 360,
                      context_characters: int = 80) -> dict[str, dict]:
    """Deterministic, overlapping, exact character slices; no NFKC conversion.

    Short code-bearing sources stay intact (up to 1200 characters). Longer text
    uses sentence/paragraph boundaries near a target and overlapping context.
    Every non-whitespace character remains in at least one supplied span. A long
    code block can span windows; the prompt permits adjacent span selection.
    Offsets are retained for mechanical verification, not sent to the model.
    """
    if (isinstance(target_characters, bool) or not isinstance(target_characters, int)
            or not 80 <= target_characters <= 800):
        raise ValueError("Span target must be between 80 and 800 characters")
    if (isinstance(context_characters, bool) or not isinstance(context_characters, int)
            or not 0 <= context_characters <= 200):
        raise ValueError("Span context must be between 0 and 200 characters")
    spans = {}
    for identifier, source in sources.items():
        text = source["text"]
        if not isinstance(text, str) or source["origin"] not in ORIGINS:
            raise ValueError("Invalid source for span slicing")
        code = bool(re.search(r"(?m)^\s*(?:def\s|class\s|if\s|for\s|while\s|return\b|[A-Za-z_]\w*\s*(?:=|\+=))", text))
        if len(text) <= target_characters + context_characters or (code and len(text) <= 1200):
            intervals = [(0, len(text))]
        else:
            boundaries = sorted({0, len(text), *[
                m.end() for m in re.finditer(r"(?:[.!?。！？](?:\s+|$)|\n\s*\n)", text)
            ]})
            intervals = []
            core_start = 0
            while core_start < len(text):
                target = min(len(text), core_start + target_characters)
                nearby = [b for b in boundaries
                          if core_start + int(target_characters * .65) <= b
                          <= min(len(text), core_start + int(target_characters * 1.35))]
                core_end = min(nearby, key=lambda b: (abs(b - target), b)) if nearby else target
                start, end = max(0, core_start - context_characters), min(len(text), core_end + context_characters)
                # Keep a complete neighboring sentence when it fits the overlap.
                left = [b for b in boundaries if start <= b <= core_start]
                right = [b for b in boundaries if core_end <= b <= end]
                if left:
                    start = min(left)
                if right:
                    end = max(right)
                intervals.append((start, end))
                core_start = core_end
        for start, end in intervals:
            while start < end and text[start].isspace():
                start += 1
            while end > start and text[end - 1].isspace():
                end -= 1
            span_text = text[start:end]
            identity = {"source_id": identifier, "origin": source["origin"],
                        "start": start, "end": end, "text": span_text,
                        "version": SPAN_VERSION}
            span_id = "s_" + digest(identity)[:20]
            spans[span_id] = {"span_id": span_id, "source_id": identifier,
                              "origin": source["origin"], "source_start": start,
                              "source_end": end, "text": span_text}
    return spans


def validate_span_extraction(value: object, sources: dict[str, dict], spans: dict[str, dict],
                             origin: str, min_quote_characters: int = 12) -> dict:
    """Resolve IDs into original slices, then validate each atomic draft.

    Quote-mode normalization is not relied upon for provenance here: a selected
    span must equal its source's literal Python character slice, including all
    mathematical symbols, before it can become a quotation.
    """
    if not isinstance(value, dict) or set(value) != {"claims", "uncertain_source_ids"}:
        raise ValueError("Malformed extraction envelope")
    if not isinstance(value["claims"], list):
        raise ValueError("Malformed extraction arrays")
    checked = validate_extraction({"claims": [], "uncertain_source_ids": value["uncertain_source_ids"]},
                                  sources, origin, min_quote_characters)
    ledger, rejected = [], []
    for index, raw in enumerate(value["claims"]):
        reasons, citations = [], []
        if not isinstance(raw, dict) or set(raw) != CLAIM_FIELDS:
            rejected.append({"index": index, "claim": raw, "reasons": ["Malformed atomic claim"]})
            continue
        selected = raw["evidence"]
        if not isinstance(selected, list) or not 1 <= len(selected) <= 3:
            reasons.append("An atomic claim needs one to three selected spans")
        else:
            for reference in selected:
                if not isinstance(reference, dict) or set(reference) != {"span_id"}:
                    reasons.append("Malformed selected span reference")
                    continue
                span_id = reference["span_id"]
                if not isinstance(span_id, str) or span_id not in spans:
                    reasons.append("Unknown or out-of-batch span")
                    continue
                span = spans[span_id]
                source_id = span["source_id"]
                if (source_id not in sources or span["origin"] != origin
                        or sources[source_id]["origin"] != origin):
                    reasons.append("Selected span source origin mismatch")
                    continue
                start, end = span["source_start"], span["source_end"]
                text = sources[source_id]["text"]
                if (isinstance(start, bool) or isinstance(end, bool)
                        or not isinstance(start, int) or not isinstance(end, int)
                        or not 0 <= start <= end <= len(text)
                        or text[start:end] != span["text"]):
                    reasons.append("Selected span is not an exact source slice")
                    continue
                citations.append({"source_id": source_id, "quote": text[start:end]})
        if reasons:
            rejected.append({"index": index, "claim": raw, "reasons": reasons})
            continue
        resolved = {**raw, "evidence": citations}
        result = validate_extraction({"claims": [resolved], "uncertain_source_ids": []},
                                     sources, origin, min_quote_characters)
        if result["rejected_claims"]:
            rejected.append({"index": index, "claim": raw,
                             "reasons": result["rejected_claims"][0]["reasons"]})
            continue
        for item in result["ledger"]:
            for evidence, reference in zip(item["evidence"], selected):
                span = spans[reference["span_id"]]
                evidence.update({"quote": span["text"], "span_id": span["span_id"], "source_start": span["source_start"],
                                 "source_end": span["source_end"], "quote_match": "exact_source_slice"})
            item["canonical_text"] = "\n".join(e["quote"] for e in item["evidence"])
            # Exact span IDs/text are part of identity: NFKC must not merge two
            # mathematically distinct source quotations in this extraction mode.
            item["claim_id"] = "q_" + digest({
                "legacy_id": item["claim_id"],
                "span_ids": [spans[r["span_id"]]["span_id"] for r in selected],
                "canonical_text": item["canonical_text"],
            })[:16]
            ledger.append(item)
    return {"ledger": ledger, "rejected_claims": rejected,
            "uncertain_source_ids": checked["uncertain_source_ids"]}


def _sources(units: list[Unit], chunks: list[Chunk]) -> dict[str, dict]:
    records = [
        {"source_id": c.segment_id, "origin": "audio", "text": c.text} for c in chunks
    ] + [{"source_id": u.unit_id, "origin": "courseware", "text": u.text} for u in units]
    identifiers = [r["source_id"] for r in records]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Source IDs must be unique and disjoint across origins")
    if any(not isinstance(i, str) or not i for i in identifiers):
        raise ValueError("Invalid source ID")
    if any(not isinstance(r["text"], str) for r in records):
        raise ValueError("Invalid source text")
    return {r["source_id"]: r for r in records}


def validate_extraction(value: object, sources: dict[str, dict], origin: str,
                        min_quote_characters: int = 12) -> dict:
    """Keep valid quoted drafts; retain every rejected entry with its reason.

    `sources` is the exact batch sent to the model, not the entire corpus. This
    prevents a valid-looking reference to content the extraction never received.
    Legacy quote mode uses core.normalize: NFKC, nonprintable filtering, and
    whitespace collapse. This may fold symbols; it is not literal-byte equality.
    Span-selection mode first verifies an exact source slice independently.
    """
    if origin not in ORIGINS or any(r["origin"] != origin for r in sources.values()):
        raise ValueError("Batch source origin mismatch")
    if isinstance(min_quote_characters, bool) or not isinstance(min_quote_characters, int) or min_quote_characters < 1:
        raise ValueError("Invalid minimum quotation length")
    if not isinstance(value, dict) or set(value) != {"claims", "uncertain_source_ids"}:
        raise ValueError("Malformed extraction envelope")
    if not isinstance(value["claims"], list) or not isinstance(value["uncertain_source_ids"], list):
        raise ValueError("Malformed extraction arrays")
    if any(not isinstance(i, str) or i not in sources for i in value["uncertain_source_ids"]):
        raise ValueError("Unknown uncertain source")
    accepted, rejected = [], []
    for index, raw in enumerate(value["claims"]):
        reasons = []
        if not isinstance(raw, dict) or set(raw) != CLAIM_FIELDS:
            rejected.append({"index": index, "claim": raw, "reasons": ["Malformed atomic claim"]})
            continue
        if raw["origin"] != origin:
            reasons.append("Claim origin does not match request origin")
        if not isinstance(raw["kind"], str) or raw["kind"] not in KINDS:
            reasons.append("Unknown claim kind")
        if not isinstance(raw["importance"], str) or raw["importance"] not in IMPORTANCE:
            reasons.append("Unknown claim importance")
        for name, limit in (("title", 250), ("statement", 2000)):
            if not isinstance(raw[name], str) or not raw[name].strip() or len(raw[name]) > limit:
                reasons.append("Invalid " + name)
        citations = raw["evidence"]
        validated = []
        if not isinstance(citations, list) or not 1 <= len(citations) <= 3:
            reasons.append("An atomic claim needs one to three quotations")
        else:
            for citation in citations:
                if not isinstance(citation, dict) or set(citation) != EVIDENCE_FIELDS:
                    reasons.append("Malformed quotation")
                    continue
                identifier, quote = citation["source_id"], citation["quote"]
                if not isinstance(identifier, str) or identifier not in sources:
                    reasons.append("Unknown or out-of-batch source")
                    continue
                if sources[identifier]["origin"] != origin:
                    reasons.append("Quotation source origin mismatch")
                    continue
                if not isinstance(quote, str):
                    reasons.append("Quotation is not text")
                    continue
                quoted = normalize(quote)
                if len(quoted) < min_quote_characters:
                    reasons.append("Quotation too short for useful evidence")
                elif len(quoted) > 1600:
                    reasons.append("Quotation too long for an atomic claim")
                elif quoted not in normalize(sources[identifier]["text"]):
                    reasons.append("Quotation is not a verbatim normalized substring")
                else:
                    validated.append({
                        "source_id": identifier, "quote": quote.strip(),
                        "normalized_quote": quoted,
                        "source_text_sha256": digest(sources[identifier]["text"]),
                    })
        if reasons:
            rejected.append({"index": index, "claim": raw, "reasons": reasons})
            continue
        # A rewrite is not promoted to a fact merely because a citation exists.
        identity = {"origin": origin, "kind": raw["kind"],
                    "title": normalize(raw["title"]), "statement": normalize(raw["statement"]),
                    "evidence": [{"source_id": e["source_id"], "quote": e["normalized_quote"]}
                                 for e in validated]}
        accepted.append({
            "claim_id": "q_" + digest(identity)[:16], "origin": origin,
            "kind": raw["kind"], "importance": raw["importance"],
            "title_draft": raw["title"].strip(),
            "paraphrase_draft": raw["statement"].strip(),
            "canonical_text": "\n".join(e["quote"] for e in validated),
            "evidence": validated, "status": "quote_verified_draft",
            "review_required": True,
        })
    return {"ledger": accepted, "rejected_claims": rejected,
            "uncertain_source_ids": list(dict.fromkeys(value["uncertain_source_ids"]))}


def compose_notes(ledger: list[dict]) -> tuple[dict, dict]:
    """Lossless composition. No generative reduce step may discard key items."""
    ids = [c["claim_id"] for c in ledger]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate claim ID in composition")
    if any(c.get("origin") not in ORIGINS or c.get("status") != "quote_verified_draft"
           for c in ledger):
        raise ValueError("Only quote-validated drafts can be composed")
    notes = {
        "status": "draft", "review_required": True,
        "source_labels": dict(SOURCE_LABELS),
        "audio": [c for c in ledger if c["origin"] == "audio"],
        "courseware": [c for c in ledger if c["origin"] == "courseware"],
        "notice": "原文引文是核对依据；模型标题与改写均为待核对草稿。引用存在不代表改写正确。",
    }
    rendered = [c["claim_id"] for origin in ("audio", "courseware") for c in notes[origin]]
    keys = [c["claim_id"] for c in ledger if c["importance"] == "key"]
    coverage = {
        "key_claim_ids": keys, "rendered_claim_ids": rendered,
        "missing_key_claim_ids": sorted(set(keys) - set(rendered)),
        "missing_claim_ids": sorted(set(ids) - set(rendered)),
        "all_extracted_claims_preserved": set(ids) == set(rendered),
        "scope": "Preservation of accepted extraction only; NOT proof that extraction found every important source concept",
    }
    return notes, coverage


def _batches(records: list[dict], limit: int, max_sources: int | None = None) -> list[list[dict]]:
    result, batch, count = [], [], 0
    for record in records:
        if batch and (count + len(record["text"]) > limit
                      or (max_sources is not None and len(batch) >= max_sources)):
            result.append(batch)
            batch, count = [], 0
        batch.append(record)
        count += len(record["text"])
    if batch:
        result.append(batch)
    return result


def _origin_batches(sources, origin, limit, max_sources):
    # Source order is used solely for transcript reading continuity. Courseware
    # is canonicalized by opaque ID, never by original page ordinal.
    records = [r for r in sources.values() if r["origin"] == origin]
    if origin == "courseware":
        records.sort(key=lambda r: r["source_id"])
    return _batches(records, limit, max_sources)


def synthesize_grounded(units: list[Unit], chunks: list[Chunk], model: LocalModel,
                        prompt: str, cache_dir: Path, config: dict) -> dict:
    """Extract each origin independently and preserve all accepted quoted drafts.

    Does not accept matches, page mappings, existing notes, or evaluation labels.
    All input windows appear in source_audit, including failed/empty extractions.
    """
    sources = _sources(units, chunks)
    if not sources:
        raise ValueError("At least one source is required")
    limit = config.get("quality_batch_characters", 3500)
    max_claims = config.get("quality_max_claims_per_batch", 20)
    max_tokens = config.get("quality_max_tokens", 5000)
    min_quote = config.get("quality_min_quote_characters", 12)
    evidence_mode = config.get("quality_evidence_mode", "quote")
    span_characters = config.get("quality_span_characters", 360)
    span_context = config.get("quality_span_context_characters", 80)
    max_sources = config.get("quality_sources_per_batch")
    local_span_aliases = config.get("quality_local_span_aliases", False)
    english = config.get("output_language") == "en"
    source_labels = ENGLISH_SOURCE_LABELS if english else SOURCE_LABELS
    if (max_sources is not None and (isinstance(max_sources, bool)
                                    or not isinstance(max_sources, int)
                                    or not 1 <= max_sources <= 1000)):
        raise ValueError("quality_sources_per_batch must be null or an integer from 1 to 1000")
    if not isinstance(local_span_aliases, bool):
        raise ValueError("quality_local_span_aliases must be boolean")
    if evidence_mode not in {"quote", "span_selection"}:
        raise ValueError("Unknown quality evidence mode")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 100:
        raise ValueError("Invalid quality batch character limit")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 100:
        raise ValueError("Invalid quality token limit")
    persistent = persistent_settings(config, Path(__file__))
    call_start = len(model.calls)
    ledger_by_id, rejections, failures, batch_audits = {}, [], [], []
    source_batches: dict[str, list[dict]] = {i: [] for i in sources}
    uncertain = set()
    for origin in ("audio", "courseware"):
        for batch_index, records_batch in enumerate(_origin_batches(sources, origin, limit, max_sources)):
            batch_sources = {r["source_id"]: r for r in records_batch}
            spans = make_source_spans(batch_sources, span_characters, span_context) if evidence_mode == "span_selection" else {}
            selected_spans = ({"S" + str(index): span for index, span in enumerate(spans.values())}
                              if spans and local_span_aliases else spans)
            use_aliases = evidence_mode == "span_selection" and local_span_aliases
            source_ids = {identifier: f"R{index}" if use_aliases else identifier
                          for index, identifier in enumerate(batch_sources)}
            original_source_ids = {alias: identifier for identifier, alias in source_ids.items()}
            if evidence_mode == "span_selection":
                schema = span_extraction_schema(set(original_source_ids), set(selected_spans), origin, max_claims)
                spans_by_source = {identifier: [] for identifier in batch_sources}
                for alias, span in selected_spans.items():
                    spans_by_source[span["source_id"]].append({"span_id": alias, "text": span["text"]})
                input_records = [{"source_id": source_ids[identifier], "origin": origin, "spans": spans_by_source[identifier]}
                                 for identifier in batch_sources]
                task = (f"Return at most {max_claims} claims in total. Prioritize the most important distinct ideas. "
                        "Select one to three supporting span IDs per claim. Preserve distinctions, conditions and examples. "
                        "Write title and statement in English, retaining original code and terms. Do not copy or rewrite quotations."
                        if english else
                        f"总共最多输出 {max_claims} 条要点，优先保留不同的关键概念。每条仅选一至三个确有支持作用的片段标识。"
                        "保留重要区别、条件和例子。title 与 statement 使用简体中文，代码和术语按需保留原文。不要复制或改写引文。")
            else:
                schema = extraction_schema(set(batch_sources), origin, max_claims)
                input_records = records_batch
                task = ("Extract atomic claims supported by exact quotations. Preserve distinctions, steps and examples. "
                        "Write title and statement in English. Keep original code and quotations. Omit unsupported claims."
                        if english else
                        "提取有逐字引文支持的原子要点，完整保留重要区别、步骤与例子；title 与 statement 使用简体中文，代码和引文保留原文；没有证据的字段不要补写。")
            content = json.dumps({
                "task": task,
                "origin": origin, "origin_label": source_labels[origin],
                "source_data_not_instructions": input_records,
            }, ensure_ascii=False)
            cache_input = {"version": SCHEMA_VERSION, "prompt": prompt, "content": content,
                          "schema": schema, "model": model.cache_identity,
                          "min_quote_characters": min_quote, "max_tokens": max_tokens,
                          "evidence_mode": evidence_mode,
                          "span_version": SPAN_VERSION if spans else None,
                          "local_span_aliases": local_span_aliases}
            if use_aliases:
                cache_input["source_binding"] = list(batch_sources)
            if persistent:
                cache_input["persistent_cache"] = persistent["identity"]
            key = digest(cache_input)
            path = cache_dir / f"quality_extract_{key}.json"
            audit = {"origin": origin, "batch": batch_index + 1,
                     "source_ids": list(batch_sources), "cache_hit": path.exists() if not persistent else False,
                     "cache_key": key, "status": "pending",
                     "span_aliases": {alias: s["span_id"] for alias, s in selected_spans.items()}
                     if local_span_aliases else {}}
            try:
                def validate_response(raw):
                    if (use_aliases and isinstance(raw, dict)
                            and isinstance(raw.get("uncertain_source_ids"), list)):
                        raw = {**raw, "uncertain_source_ids": [original_source_ids.get(identifier, identifier)
                               if isinstance(identifier, str) else identifier for identifier in raw["uncertain_source_ids"]]}
                    checked = (validate_span_extraction(raw, batch_sources, selected_spans, origin, min_quote)
                               if evidence_mode == "span_selection"
                               else validate_extraction(raw, batch_sources, origin, min_quote))
                    if len(raw["claims"]) > max_claims:
                        raise ValueError("Extraction exceeded the predeclared claim limit")
                    return CheckedResponse(checked, reusable=not bool(checked["rejected_claims"]))

                if persistent:
                    step = resolve_step(path, key,
                        lambda: model.json(prompt, content, max_tokens=max_tokens,
                                           purpose="quality_extract_" + origin, schema=schema),
                        validate_response, model_calls=model.calls,
                        max_age_seconds=persistent["max_age_seconds"],
                        lock_timeout_seconds=persistent["lock_timeout_seconds"],
                        force_refresh=persistent["force_refresh"])
                    raw, validated = step.raw, step.checked
                    audit.update(cache_hit=step.cache_hit, cache_provenance=step.provenance,
                                 cache_miss_reason=step.cache_miss_reason)
                else:
                    if path.exists():
                        raw = read_json(path)
                    else:
                        raw = model.json(prompt, content, max_tokens=max_tokens,
                                         purpose="quality_extract_" + origin, schema=schema)
                    validated = validate_response(raw).value
                # Do not persist an invalid response as a successful cache entry.
                # Rejected raw claims are retained in this run's result instead.
                if not persistent and not audit["cache_hit"] and not validated["rejected_claims"]:
                    write_json(path, raw)
                uncertain.update(validated["uncertain_source_ids"])
                for claim in validated["ledger"]:
                    previous = ledger_by_id.get(claim["claim_id"])
                    if previous is None:
                        ledger_by_id[claim["claim_id"]] = claim
                    elif claim["importance"] == "key":
                        previous["importance"] = "key"
                for rejected in validated["rejected_claims"]:
                    rejections.append({"origin": origin, "batch": batch_index + 1, **rejected})
                audit.update({"status": "extracted", "accepted_claims": len(validated["ledger"]),
                              "rejected_claims": len(validated["rejected_claims"]),
                              "cacheable": not bool(validated["rejected_claims"]),
                              "evidence_mode": evidence_mode, "span_count": len(spans),
                              "at_claim_limit": len(raw["claims"]) == max_claims})
            except (ValueError, TimeoutError, OSError, KeyError, TypeError) as exc:
                audit.update({"status": "extraction_failed", "error": str(exc)})
                failures.append({"origin": origin, "batch": batch_index + 1,
                                 "source_ids": list(batch_sources), "reason": str(exc)})
            batch_audits.append(audit)
            for identifier in batch_sources:
                source_batches[identifier].append(audit)
            print(f"Quote ledger {origin} batch {batch_index + 1}: {audit['status']}", flush=True)

    ledger = list(ledger_by_id.values())
    notes, coverage = compose_notes(ledger)
    source_audit = []
    for identifier, source in sources.items():
        claim_ids = [c["claim_id"] for c in ledger
                     if any(e["source_id"] == identifier for e in c["evidence"])]
        audits = source_batches[identifier]
        failed = any(a["status"] == "extraction_failed" for a in audits)
        at_limit = any(a.get("at_claim_limit", False) for a in audits)
        if failed:
            status, reason = "extraction_failed", "提取失败，不能将该来源视为没有重点。"
        elif not claim_ids:
            status, reason = "no_verified_claims", "没有保留通过引文校验的要点；可能是寒暄、转写不清或遗漏，需要查看原文。"
        else:
            status, reason = "has_quote_verified_claims", "已保留部分可定位引文；不保证本来源所有重要概念均已提取。"
        if identifier in uncertain:
            reason += " 模型报告此处不确定。"
        if at_limit:
            reason += " 批次达到要点上限，存在遗漏风险。"
        source_audit.append({"source_id": identifier, "origin": source["origin"],
                             "claim_ids": claim_ids, "status": status, "reason": reason,
                             "review_required": True, "model_uncertain": identifier in uncertain,
                             "at_claim_limit": at_limit})
    return {
        "schema_version": SCHEMA_VERSION, "mode": "separate_origin_quote_ledger",
        "evidence_mode": evidence_mode,
        "ledger": ledger, "notes": notes, "coverage_audit": coverage,
        "source_audit": source_audit, "rejected_claims": rejections,
        "failures": failures, "batches": batch_audits,
        "model_calls": model.calls[call_start:], "model": model.cache_identity,
        "quality_status": "draft_requires_review" if ledger else "no_usable_claims",
        "validation": "Exact quote occurrence and lossless composition only; ASR accuracy, entailment and source-level completeness need review",
    }
