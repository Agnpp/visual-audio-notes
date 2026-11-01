"""Conservative status for notes, separate from citation and matching checks.

This module is a reporting boundary, not identity verification. A supplied human
review is an audit assertion. No status returned here authorizes unattended
publication or converts candidate matches into human confirmations.
"""
from __future__ import annotations

from collections import Counter
import re


MECHANICAL_PASS = "mechanical_checks_passed_not_semantic_validation"
REFERENCE_REVIEWERS = {"independent_human", "external_expert"}
ANCHOR_STATUSES = {"full", "partial", "missing", "incorrect"}


def _hash(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _unique_ids(value: object) -> bool:
    return (isinstance(value, list) and bool(value)
            and all(isinstance(i, str) and i.strip() for i in value)
            and len(set(value)) == len(value))


def build_quality_gate(mechanical_checks: dict, content_review: dict | None = None) -> dict:
    """Report whether a frozen run is a draft, needs revision, or has review.

    ``mechanical_checks`` is the output of ``tools/check_quality_run.py``.
    Existing developer reviews with ``anchors`` and ``freeze_sha256`` are
    understood as diagnostics; they cannot promote automatic notes.

    ``reviewed_reference`` requires an explicit independent human/expert audit:
    matching ``source_freeze_sha256``, ``reviewer_kind``, nonempty ``reviewer``,
    ``evaluation_role='holdout'``, ``full_claim_review=True``,
    ``source_review={'independent_transcript':'complete',
    'courseware_text':'complete'}``, ``expected_essential_ids``, full anchors for
    exactly those IDs, and ``material_errors=unsupported_claims=0``. This status
    describes a manually reviewed reference, not proof of automatic accuracy.
    Missing fields, model-only verdicts, or a bare pass flag never suffice.
    """
    if not isinstance(mechanical_checks, dict):
        raise ValueError("Mechanical checks must be an object")
    frozen = mechanical_checks.get("freeze_sha256")
    if not _hash(frozen):
        raise ValueError("A valid frozen-run SHA-256 is required")
    if content_review is not None and not isinstance(content_review, dict):
        raise ValueError("Content review must be an object or absent")

    reasons: list[str] = []
    mechanical_issues: list[str] = []
    if mechanical_checks.get("status") != MECHANICAL_PASS:
        mechanical_issues.append("mechanical_checks_not_passed")
    if mechanical_checks.get("all_extracted_items_preserved") is not True:
        mechanical_issues.append("composition_preservation_not_established")
    for name in ("missing_after_composition", "rejected_claims", "extraction_failures"):
        value = mechanical_checks.get(name)
        if not _count(value):
            raise ValueError("Missing or invalid mechanical count: " + name)
        if value:
            mechanical_issues.append(name)
    reasons.extend(mechanical_issues)

    anchors: list[dict] = []
    review_issues: list[str] = []
    bound = False
    if content_review is not None:
        hashes = [content_review[k] for k in ("source_freeze_sha256", "freeze_sha256")
                  if k in content_review]
        if any(not _hash(h) or h != frozen for h in hashes):
            raise ValueError("Content review belongs to a different frozen run")
        bound = bool(hashes)
        if not bound:
            reasons.append("review_not_bound_to_frozen_run")
        raw_anchors = content_review.get("anchors", [])
        if not isinstance(raw_anchors, list):
            raise ValueError("Review anchors must be a list")
        seen: set[str] = set()
        for anchor in raw_anchors:
            if (not isinstance(anchor, dict) or not isinstance(anchor.get("id"), str)
                    or not anchor["id"].strip() or anchor["id"] in seen
                    or not isinstance(anchor.get("status"), str)
                    or anchor["status"] not in ANCHOR_STATUSES):
                raise ValueError("Malformed or duplicated reference anchor")
            seen.add(anchor["id"])
            anchors.append(anchor)
        for status in ("incorrect", "missing", "partial"):
            if any(a["status"] == status for a in anchors):
                review_issues.append("essential_anchor_" + status)
        for name in ("material_errors", "unsupported_claims"):
            if name in content_review:
                if not _count(content_review[name]):
                    raise ValueError("Invalid review count: " + name)
                if content_review[name]:
                    review_issues.append(name)
        # Diagnostics may be useful even before a formal independent review.
        # They can prevent promotion, but cannot grant it.
        reasons.extend(review_issues)

    status = "experimental_draft"
    reference_conditions = False
    if content_review is None:
        reasons.append("no_content_review")
    else:
        expected = content_review.get("expected_essential_ids")
        reference_conditions = (
            bound
            and isinstance(content_review.get("reviewer_kind"), str)
            and content_review["reviewer_kind"] in REFERENCE_REVIEWERS
            and isinstance(content_review.get("reviewer"), str)
            and bool(content_review["reviewer"].strip())
            and content_review.get("evaluation_role") == "holdout"
            and content_review.get("full_claim_review") is True
            and content_review.get("source_review") == {
                "independent_transcript": "complete", "courseware_text": "complete"}
            and _unique_ids(expected)
            and {a["id"] for a in anchors} == set(expected)
            and all(a["status"] == "full" for a in anchors)
            and content_review.get("material_errors") == 0
            and _count(content_review.get("material_errors"))
            and content_review.get("unsupported_claims") == 0
            and _count(content_review.get("unsupported_claims"))
        )
        if not reference_conditions:
            reasons.append("complete_independent_content_review_not_established")
    if mechanical_issues or review_issues:
        status = "needs_revision"
    elif reference_conditions:
        status = "reviewed_reference"
        reasons.append("manually_reviewed_reference_only")

    return {
        "schema_version": 1,
        "quality_status": status,
        "automatic_publish_allowed": False,
        "source_freeze_sha256": frozen,
        "recommended_rendering": ("reviewed_reference" if status == "reviewed_reference"
                                  else "evidence_only"),
        "source_policy": "Keep recording and courseware origins separate; evidence-only rendering must not display unreviewed paraphrases as facts.",
        "reasons": list(dict.fromkeys(reasons)),
        "essential_anchor_counts": dict(Counter(a["status"] for a in anchors)),
        "review_scope": (content_review or {}).get("scope", "No complete content review supplied"),
        "provenance_limit": "Review identity and completeness are supplied audit assertions, not authenticated proof.",
        "policy": "Mechanical success, model support verdicts and confirmed page matches never establish note correctness. No automatic publication is authorized.",
    }
