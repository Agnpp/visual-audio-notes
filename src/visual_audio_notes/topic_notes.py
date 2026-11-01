"""Draft topic organization over an already extracted quotation ledger.

This layer may rewrite and organize, but cannot silently lose ledger items or
turn courseware evidence into claims about what was spoken. Identifier checks
and complete accounting do not certify the model's rewritten explanations.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re

from .archive import markdown_text
from .core import digest, read_json, write_json
from .step_cache import CheckedResponse, persistent_settings, resolve_step


VERSION = "separate-origin-topics-v1"
ORIGINS = ("audio", "courseware")
LABELS = {"audio": "录音转写中的主题 · 请核对原音",
          "courseware": "课件主题/补充 · 是否讲过未核对"}
TOPIC_FIELDS = {"title", "key_idea", "explanation", "claim_ids"}
UNASSIGNED_REASON = "模型未归类，已保留到待核对队列"
MISSING_POLICIES = {"strict", "review_queue"}


def _ledger_index(ledger: object) -> dict[str, dict]:
    if not isinstance(ledger, list):
        raise ValueError("Topic organization needs an extracted ledger")
    index, source_origins = {}, {}
    for claim in ledger:
        if not isinstance(claim, dict):
            raise ValueError("Malformed ledger item")
        identifier = claim.get("claim_id")
        if (not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier)
                or identifier in index):
            raise ValueError("Invalid or duplicate ledger claim ID")
        origin = claim.get("origin")
        if (origin not in ORIGINS or claim.get("status") != "quote_verified_draft"
                or claim.get("importance") not in {"key", "supporting"}):
            raise ValueError("Only source-specific quote-verified drafts can be organized")
        if any(not isinstance(claim.get(field), str) or not claim[field].strip()
               for field in ("title_draft", "paraphrase_draft", "canonical_text")):
            raise ValueError("Malformed ledger draft text")
        evidence = claim.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("Ledger claim needs quotation evidence")
        for item in evidence:
            if (not isinstance(item, dict) or not isinstance(item.get("source_id"), str)
                    or not item["source_id"] or not isinstance(item.get("quote"), str)
                    or not item["quote"].strip()):
                raise ValueError("Malformed ledger quotation")
            previous_origin = source_origins.setdefault(item["source_id"], origin)
            if previous_origin != origin:
                raise ValueError("An evidence source cannot belong to both origins")
        index[identifier] = claim
    return index


def topic_schema(claim_ids: set[str], max_topics: int = 6) -> dict:
    if not claim_ids or type(max_topics) is not int or not 1 <= max_topics <= 8:
        raise ValueError("Topic schema needs source claims and one to eight topics")
    identifier = {"type": "string", "enum": sorted(claim_ids)}
    return {
        "type": "object", "additionalProperties": False,
        "required": ["topics", "deferred_claims", "uncertainties"],
        "properties": {
            "topics": {"type": "array", "maxItems": max_topics, "items": {
                "type": "object", "additionalProperties": False,
                "required": sorted(TOPIC_FIELDS),
                "properties": {
                    "title": {"type": "string", "minLength": 1},
                    "key_idea": {"type": "string", "minLength": 1},
                    "explanation": {"type": "string", "minLength": 1},
                    "claim_ids": {"type": "array", "minItems": 1,
                                  "maxItems": len(claim_ids), "uniqueItems": True,
                                  "items": identifier},
                }}},
            "deferred_claims": {"type": "array", "maxItems": len(claim_ids), "items": {
                "type": "object", "additionalProperties": False,
                "required": ["claim_id", "reason"],
                "properties": {"claim_id": identifier,
                               "reason": {"type": "string", "minLength": 1}}}},
            "uncertainties": {"type": "array", "maxItems": 20,
                              "items": {"type": "string", "minLength": 1}},
        },
    }


def _text(value: object, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("Invalid topic " + label)
    return value.strip()


def validate_topics(value: dict, claims: dict[str, dict], origin: str,
                    max_topics: int = 6, *, missing_policy: str = "strict") -> tuple[list[dict], list[dict], list[str], dict]:
    """Every claim is assigned exactly once, to a topic or an explicit deferral."""
    if missing_policy not in MISSING_POLICIES:
        raise ValueError("Unknown topic missing-claim policy")
    if origin not in ORIGINS or any(c["origin"] != origin for c in claims.values()):
        raise ValueError("Topic source origin mismatch")
    if not isinstance(value, dict) or set(value) != {"topics", "deferred_claims", "uncertainties"}:
        raise ValueError("Malformed topic response envelope")
    if not isinstance(value["topics"], list) or len(value["topics"]) > max_topics:
        raise ValueError("Invalid topic count")
    if not isinstance(value["deferred_claims"], list) or not isinstance(value["uncertainties"], list):
        raise ValueError("Malformed topic lists")
    assigned, deferred_ids, topics, deferred = set(), set(), [], []
    for position, raw in enumerate(value["topics"]):
        if not isinstance(raw, dict) or set(raw) != TOPIC_FIELDS:
            raise ValueError("Malformed topic item")
        title = _text(raw["title"], "title", 250)
        key_idea = _text(raw["key_idea"], "key idea", 3000)
        explanation = _text(raw["explanation"], "explanation", 12000)
        identifiers = raw["claim_ids"]
        if (not isinstance(identifiers, list) or not identifiers
                or any(not isinstance(i, str) or i not in claims for i in identifiers)):
            raise ValueError("Unknown or wrong-origin topic reference")
        if len(identifiers) != len(set(identifiers)) or assigned.intersection(identifiers):
            raise ValueError("A ledger claim was assigned to multiple topics")
        assigned.update(identifiers)
        topic = {"topic_id": "topic_" + digest([origin, position, raw])[:16],
                 "origin": origin, "title": title, "key_idea": key_idea,
                 "explanation": explanation, "claim_ids": list(identifiers),
                 "primary_claim_ids": [i for i in identifiers if claims[i]["importance"] == "key"],
                 "supporting_claim_ids": [i for i in identifiers if claims[i]["importance"] == "supporting"],
                 "evidence": [{"claim_id": i, **deepcopy(e)} for i in identifiers
                              for e in claims[i]["evidence"]],
                 "status": "draft_requires_review", "review_required": True}
        topics.append(topic)
    for raw in value["deferred_claims"]:
        if not isinstance(raw, dict) or set(raw) != {"claim_id", "reason"}:
            raise ValueError("Malformed deferred claim")
        identifier = raw["claim_id"]
        if not isinstance(identifier, str) or identifier not in claims:
            raise ValueError("Unknown or wrong-origin deferred reference")
        if identifier in assigned or identifier in deferred_ids:
            raise ValueError("A ledger claim was assigned more than once")
        deferred_ids.add(identifier)
        deferred.append({"claim_id": identifier, "reason": _text(raw["reason"], "deferral reason", 2000),
                         "importance": claims[identifier]["importance"], "origin": origin})
    missing = set(claims) - assigned - deferred_ids
    if missing and missing_policy == "strict":
        raise ValueError("Missing ledger claims in topic accounting: " + ", ".join(sorted(missing)))
    model_accounted = assigned | deferred_ids
    for identifier in sorted(missing):
        # Preserve the item without inventing a theme or a model explanation.
        # This is a program action, explicitly distinguishable from model output.
        deferred_ids.add(identifier)
        deferred.append({"claim_id": identifier, "reason": UNASSIGNED_REASON,
                         "importance": claims[identifier]["importance"], "origin": origin,
                         "source": "programmatic_unassigned"})
    if len(value["uncertainties"]) > 20:
        raise ValueError("Too many topic uncertainties")
    uncertainties = [_text(v, "uncertainty", 2000) for v in value["uncertainties"]]
    coverage = {"total_claims": len(claims), "assigned_claim_ids": sorted(assigned),
                "deferred_claim_ids": sorted(deferred_ids), "missing_claim_ids": [],
                "deferred_key_claim_ids": sorted(i for i in deferred_ids if claims[i]["importance"] == "key"),
                "all_claims_accounted_for": True,
                "model_accounted_claim_ids": sorted(model_accounted),
                "model_missing_claim_ids": sorted(missing),
                "programmatic_unassigned_claim_ids": sorted(missing),
                "all_model_claims_accounted_for": not missing,
                "all_key_claims_in_topics": not any(claims[i]["importance"] == "key" for i in deferred_ids),
                "scope": "Accounting only; not proof of semantic fidelity or source-level completeness"}
    return topics, deferred, uncertainties, coverage


def organize_topics(quality_result: dict, model, prompt: str, cache_dir: Path, config: dict) -> dict:
    """Organize accepted ledger drafts without source mixing or silent dropping."""
    if not isinstance(quality_result, dict):
        raise ValueError("Quality result must be an object")
    index = _ledger_index(quality_result.get("ledger"))
    max_topics = config.get("topic_max_topics_per_origin", 6)
    max_tokens = config.get("topic_max_tokens", 6000)
    max_input = config.get("topic_max_input_characters", 250000)
    missing_policy = config.get("topic_missing_policy", "strict")
    if not isinstance(missing_policy, str) or missing_policy not in MISSING_POLICIES:
        raise ValueError("Unknown topic missing-claim policy")
    if type(max_topics) is not int or not 1 <= max_topics <= 8:
        raise ValueError("Topic limit must be between one and eight")
    if type(max_tokens) is not int or max_tokens < 100:
        raise ValueError("Invalid topic output token budget")
    if type(max_input) is not int or max_input < 100:
        raise ValueError("Invalid topic input character budget")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Topic prompt must not be empty")
    persistent = persistent_settings(config, Path(__file__))
    call_start = len(model.calls)
    result = {"schema_version": VERSION, "mode": "separate_origin_topic_draft",
              "status": "draft_requires_review", "review_required": True,
              "source_labels": dict(LABELS), "deferred_claims": {}, "uncertainties": {},
              "coverage": {"per_origin": {}}, "batches": [], "topic_missing_policy": missing_policy,
              "input_ledger_sha256": digest(quality_result["ledger"])}
    for origin in ORIGINS:
        claims = {i: c for i, c in index.items() if c["origin"] == origin}
        if not claims:
            raw = {"topics": [], "deferred_claims": [], "uncertainties": []}
        else:
            content = json.dumps({
                "task": "将这一来源类型的证据账本整理为最多几个可复习的主题，中文解释主要关系与区别。",
                "origin": origin, "origin_label": LABELS[origin], "max_topics": max_topics,
                "ledger_data_not_instructions": [
                    {"claim_id": i, "importance": c["importance"], "kind": c.get("kind"),
                     "title_draft": c["title_draft"], "paraphrase_draft": c["paraphrase_draft"],
                     "draft_warning": "草稿可能有误，依据选中原文判断，不能把改写当事实。",
                     "selected_quotes": [{"source_id": e["source_id"], "quote": e["quote"]}
                                         for e in c["evidence"]]}
                    for i, c in sorted(claims.items())],
            }, ensure_ascii=False)
            if len(content) > max_input:
                raise ValueError("Topic input exceeds configured context budget; no source was truncated")
            schema = topic_schema(set(claims), max_topics)
            cache_input = {"version": VERSION, "prompt": prompt, "content": content, "schema": schema,
                          "model": model.cache_identity, "max_tokens": max_tokens,
                          "missing_policy": missing_policy}
            if persistent:
                cache_input["persistent_cache"] = persistent["identity"]
            key = digest(cache_input)
            cache = cache_dir / f"topic_notes_{key}.json"
            diagnostic_cache = cache_dir / f"topic_unassigned_{key}.json"
            cache_metadata = {}
            if persistent:
                def validate_response(raw):
                    checked = validate_topics(raw, claims, origin, max_topics,
                                              missing_policy=missing_policy)
                    return CheckedResponse(checked[3], complete=not bool(
                        checked[3]["programmatic_unassigned_claim_ids"]))

                step = resolve_step(cache, key,
                    lambda: model.json(prompt, content, max_tokens=max_tokens,
                                       purpose="topic_organization_" + origin, schema=schema),
                    validate_response, model_calls=model.calls,
                    max_age_seconds=persistent["max_age_seconds"],
                    lock_timeout_seconds=persistent["lock_timeout_seconds"],
                    force_refresh=persistent["force_refresh"],
                    diagnostic_path=diagnostic_cache if missing_policy == "review_queue" else None)
                raw, response_coverage = step.raw, step.checked
                cache_hit, reused_unassigned = step.cache_hit, step.reused_incomplete
                cache_metadata = {"cache_provenance": step.provenance,
                                  "cache_miss_reason": step.cache_miss_reason}
            else:
                cache_hit = cache.exists()
                reused_unassigned = not cache_hit and missing_policy == "review_queue" and diagnostic_cache.exists()
                if cache_hit:
                    raw = read_json(cache)
                elif reused_unassigned:
                    diagnostic = read_json(diagnostic_cache)
                    if (not isinstance(diagnostic, dict) or diagnostic.get("role") != "incomplete_topic_response"
                            or diagnostic.get("cache_key") != key):
                        raise ValueError("Malformed incomplete-topic diagnostic cache")
                    raw = diagnostic.get("raw_response")
                else:
                    raw = model.json(prompt, content, max_tokens=max_tokens,
                                     purpose="topic_organization_" + origin, schema=schema)
                # Validate before any successful cache write; missing references fail
                # explicitly while the original extracted ledger remains untouched.
                _, _, _, response_coverage = validate_topics(raw, claims, origin, max_topics,
                                                             missing_policy=missing_policy)
            unassigned = response_coverage["programmatic_unassigned_claim_ids"]
            if not persistent and unassigned and not reused_unassigned:
                # An incomplete response is not a successful model cache entry.
                # Keep original bytes as JSON data and the explicit action audit
                # in a distinct diagnostic artifact, revalidated on every reuse.
                write_json(diagnostic_cache, {"role": "incomplete_topic_response", "version": VERSION,
                                              "cache_key": key, "raw_response": raw,
                                              "audit": {"model_missing_claim_ids": unassigned,
                                                        "action": "programmatic_review_queue",
                                                        "reason": UNASSIGNED_REASON}})
            elif not persistent and not unassigned and not cache_hit:
                write_json(cache, raw)
            result["batches"].append({"origin": origin, "cache_hit": cache_hit,
                                      "cache_key": key, "source_claims": len(claims),
                                      "reused_incomplete_response": reused_unassigned,
                                      "model_missing_claim_ids": unassigned,
                                      "programmatic_unassigned_claim_ids": unassigned,
                                      "model_response_complete": not unassigned, **cache_metadata})
        topics, deferred, uncertainty, coverage = validate_topics(raw, claims, origin, max_topics,
                                                                 missing_policy=missing_policy)
        result[origin + "_topics"] = topics
        result["deferred_claims"][origin] = deferred
        result["uncertainties"][origin] = uncertainty
        result["coverage"]["per_origin"][origin] = coverage
    result["coverage"].update(
        total_claims=len(index), all_claims_accounted_for=True,
        missing_claim_ids=[], deferred_key_claim_ids=[i for origin in ORIGINS
            for i in result["coverage"]["per_origin"][origin]["deferred_key_claim_ids"]],
        programmatic_unassigned_claim_ids=[i for origin in ORIGINS
            for i in result["coverage"]["per_origin"][origin]["programmatic_unassigned_claim_ids"]],
        all_model_claims_accounted_for=all(result["coverage"]["per_origin"][o]["all_model_claims_accounted_for"]
                                            for o in ORIGINS),
        all_key_claims_in_topics=all(result["coverage"]["per_origin"][o]["all_key_claims_in_topics"]
                                        for o in ORIGINS),
        scope="No silent ledger dropping; topic wording, importance and extraction completeness still need review")
    result["model_calls"] = deepcopy(model.calls[call_start:])
    result["model"] = deepcopy(model.cache_identity)
    result["validation"] = "Origin-specific references and complete ledger accounting only; not a factual accuracy verdict"
    return result


def render_topic_markdown(result: dict, ledger: list[dict], *, include_evidence_appendix: bool = True) -> str:
    """Render draft themes first, with all quotations in an optional appendix."""
    index = _ledger_index(ledger)
    if result.get("input_ledger_sha256") != digest(ledger):
        raise ValueError("Topic notes belong to a different extracted ledger")
    if result.get("mode") != "separate_origin_topic_draft":
        raise ValueError("Unknown topic note mode")
    positions = {i: n for n, i in enumerate(index, 1)}
    lines = ["# 主题笔记草稿", "",
             "录音与课件分开整理。主题标题和解释由 AI 生成，引用及条目核对通过不代表内容正确。", "",
             "录音依据是自动转写，仍需核对原音；课件主题不证明本段录音讲过。", ""]

    def references(identifiers):
        if include_evidence_appendix:
            return "、".join(f"[{positions[i]}](#evidence-{positions[i]})" for i in identifiers)
        return "、".join("`" + i + "`" for i in identifiers)

    for origin in ORIGINS:
        claims = {i: c for i, c in index.items() if c["origin"] == origin}
        items = result.get(origin + "_topics")
        if not isinstance(items, list):
            raise ValueError("Missing source-specific topic list")
        raw = {"topics": [{f: t.get(f) for f in TOPIC_FIELDS} for t in items],
               "deferred_claims": [{"claim_id": d.get("claim_id"), "reason": d.get("reason")}
                                    for d in result.get("deferred_claims", {}).get(origin, [])],
               "uncertainties": result.get("uncertainties", {}).get(origin, [])}
        topics, deferred, uncertainties, coverage = validate_topics(raw, claims, origin, 8)
        lines += ["## " + LABELS[origin], ""]
        if not claims:
            lines += ["没有此类证据账本，未调用 AI。", ""]
        for topic in topics:
            lines += ["### " + markdown_text(topic["title"]), "",
                      "**核心重点（AI 草稿）**：" + markdown_text(topic["key_idea"]), "",
                      "**解释与关系（AI 草稿）**：" + markdown_text(topic["explanation"]), "",
                      "依据条目：" + references(topic["claim_ids"]), ""]
        if deferred:
            lines += ["### 尚未纳入主题的条目", "", "以下条目仍保留，不能把暂缓理解为没有价值或已经覆盖。", ""]
            for d in deferred:
                importance = "关键项" if d["importance"] == "key" else "辅助项"
                lines += [f"- {importance} {references([d['claim_id']])}：{markdown_text(d['reason'])}"]
            lines.append("")
            queued = [d["claim_id"] for d in result.get("deferred_claims", {}).get(origin, [])
                      if d.get("source") == "programmatic_unassigned"]
            if queued:
                lines += [f"其中 {len(queued)} 条是模型漏分配后由程序保留的待核对项，不代表模型已完成主题覆盖。", ""]
        if uncertainties:
            lines += ["### 待核对", ""] + ["- " + markdown_text(v) for v in uncertainties] + [""]
        lines += [f"账本交代：主题中 {len(coverage['assigned_claim_ids'])} 条，暂缓 {len(deferred)} 条；"
                  "这只检查条目去向，不证明重点完整或解释正确。", ""]
    if include_evidence_appendix and index:
        lines += ["## 原文证据附录", "", "<details>", "<summary>展开全部依据条目与原文</summary>", ""]
        for identifier, claim in index.items():
            lines += [f'<a id="evidence-{positions[identifier]}"></a>', "",
                      f"### 依据 {positions[identifier]} · `{identifier}`", "",
                      "来源类型：" + LABELS[claim["origin"]], ""]
            for evidence in claim["evidence"]:
                lines += ["来源标识：" + markdown_text(evidence["source_id"]), ""]
                lines += ["> " + markdown_text(line) for line in evidence["quote"].splitlines()] + [""]
        lines += ["</details>", ""]
    return "\n".join(lines)
