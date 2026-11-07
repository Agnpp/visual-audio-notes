"""Explicit review of content candidates, separate from retrieval confidence.

Only a caller handling an explicit human action should call ``apply_decision``.
The saved actor/provenance is an audit assertion, not authenticated identity or
cryptographic proof. Loading a file never invents a human decision from a score.
No page ordinals, source maps, evaluator labels, or external alignments are read.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path

from .core import Chunk, Segment, Unit, digest, read_json, write_json


POLICY = "Explicit human action only; retrieval scores are not correctness probabilities"
PROVENANCE = "explicit_human_decision"


class StaleReviewError(ValueError):
    def __init__(self):
        super().__init__("资料或核对记录已更新，请重新查看后确认。")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc(value: object) -> bool:
    try:
        return isinstance(value, str) and datetime.fromisoformat(value).utcoffset() == timedelta(0)
    except ValueError:
        return False


def _ids(values: object, *, allow_empty: bool = True) -> bool:
    return (isinstance(values, list) and (allow_empty or bool(values)) and
            all(isinstance(v, str) and bool(v.strip()) for v in values) and
            len(values) == len(set(values)))


def _fingerprint(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 64 and
            all(c in "0123456789abcdef" for c in value))


def source_fingerprint(units: list[Unit], chunks: list[Chunk]) -> str:
    """Changing text, identifiers, or audio times invalidates previous review."""
    for unit in units:
        Unit.from_wire(asdict(unit))
    for chunk in chunks:
        Segment.from_wire(asdict(chunk))
    unit_ids = [u.unit_id for u in units]
    segment_ids = [c.segment_id for c in chunks]
    if (not _ids(unit_ids, allow_empty=False) or not _ids(segment_ids, allow_empty=False)
            or set(unit_ids) & set(segment_ids)):
        raise ValueError("Source identifiers must be unique, nonempty, and disjoint")
    return digest({"units": [asdict(u) for u in sorted(units, key=lambda u: u.unit_id)],
                   "chunks": [asdict(c) for c in sorted(chunks, key=lambda c: c.segment_id)]})


def create_review(predictions: list[dict], units: list[Unit], chunks: list[Chunk]) -> dict:
    """Treat *every* legacy prediction, including accepted ones, as a candidate."""
    fingerprint = source_fingerprint(units, chunks)
    allowed = {u.unit_id for u in units}
    expected = {c.segment_id for c in chunks}
    by_id: dict[str, dict] = {}
    for prediction in predictions:
        if not isinstance(prediction, dict):
            raise ValueError("A prediction must be an object")
        identifier = prediction.get("segment_id")
        if not isinstance(identifier, str) or identifier not in expected or identifier in by_id:
            raise ValueError("Unknown or duplicated predicted segment")
        if (not isinstance(prediction.get("candidates", []), list) or
                any(not isinstance(c, dict) or not {"unit_id", "score"} <= set(c)
                    for c in prediction.get("candidates", []))):
            raise ValueError("Malformed predicted candidates")
        candidates = [{"unit_id": c["unit_id"], "score": c["score"]}
                      for c in prediction.get("candidates", [])]
        by_id[identifier] = {"segment_id": identifier, "candidates": candidates,
                             "status": "needs_review", "confirmed_unit_ids": [],
                             "review": None, "revision": 0}
    if set(by_id) != expected:
        raise ValueError("Predictions must cover every current segment")
    state = {"schema_version": 1, "source_fingerprint": fingerprint,
             "source_revision": 0, "policy": POLICY, "unit_ids": sorted(allowed),
             "segment_ids": sorted(expected), "items": [by_id[i] for i in sorted(expected)],
             "history": []}
    validate_review(state)
    return state


def validate_review(state: dict) -> None:
    root_fields = {"schema_version", "source_fingerprint", "source_revision", "policy",
                   "unit_ids", "segment_ids", "items", "history"}
    if not isinstance(state, dict) or set(state) != root_fields:
        raise ValueError("Invalid review document schema")
    if (type(state["schema_version"]) is not int or state["schema_version"] not in {1, 2} or
            state["policy"] != POLICY or not _fingerprint(state["source_fingerprint"]) or
            type(state["source_revision"]) is not int or state["source_revision"] < 0):
        raise ValueError("Invalid review source identity or policy")
    if not _ids(state["unit_ids"], allow_empty=False) or not _ids(state["segment_ids"], allow_empty=False):
        raise ValueError("Invalid registered review identifiers")
    allowed, expected = set(state["unit_ids"]), set(state["segment_ids"])
    if allowed & expected or not isinstance(state["items"], list) or not isinstance(state["history"], list):
        raise ValueError("Invalid review collections")
    latest = {}
    source_change = None
    for event in state["history"]:
        if not isinstance(event, dict) or not _utc(event.get("at_utc")):
            raise ValueError("Invalid review history")
        if event.get("event") == "decision":
            if set(event) != {"event", "at_utc", "segment_id", "unit_ids", "review", "revision"}:
                raise ValueError("Invalid decision event")
            _validate_actor(event["review"])
            if (not _ids(event["unit_ids"]) or not isinstance(event["segment_id"], str)
                    or type(event["revision"]) is not int or event["revision"] <= 0
                    or event["at_utc"] != event["review"]["reviewed_at_utc"] or
                    (event["review"]["decision"] == "match") != bool(event["unit_ids"]) or
                    event["review"]["source_revision"] > state["source_revision"]):
                raise ValueError("Invalid decision provenance")
            revision = event["review"]["source_revision"]
            fingerprint = event["review"]["source_fingerprint"]
            if revision == state["source_revision"]:
                if (event["segment_id"] not in expected or set(event["unit_ids"]) - allowed or
                        fingerprint != state["source_fingerprint"]):
                    raise ValueError("Decision does not belong to the current sources")
            latest[revision, event["segment_id"]] = (event, fingerprint)
        elif event.get("event") == "inputs_changed":
            if set(event) != {"event", "at_utc", "old_fingerprint", "new_fingerprint", "invalidated_decisions"}:
                raise ValueError("Invalid source-change event")
            if (not _fingerprint(event["old_fingerprint"]) or not _fingerprint(event["new_fingerprint"]) or
                    type(event["invalidated_decisions"]) is not int or event["invalidated_decisions"] < 0):
                raise ValueError("Invalid source-change provenance")
            source_change = event
        elif event.get("event") == "decision_carried":
            if state["schema_version"] != 2 or set(event) != {
                    "event", "at_utc", "segment_id", "source_revision", "source_fingerprint"}:
                raise ValueError("Invalid carried-decision event")
            identifier, revision, fingerprint = event["segment_id"], event["source_revision"], event["source_fingerprint"]
            if (not isinstance(identifier, str) or type(revision) is not int
                    or not 0 < revision <= state["source_revision"] or not _fingerprint(fingerprint)
                    or not source_change or fingerprint != source_change["new_fingerprint"]
                    or (revision, identifier) in latest):
                raise ValueError("Invalid carried-decision source identity")
            previous = latest.get((revision - 1, identifier))
            if (not previous or previous[1] != source_change["old_fingerprint"]
                    or previous[0]["review"]["decision"] not in {"match", "no_match"}):
                raise ValueError("A carried decision needs a prior explicit confirmation")
            if revision == state["source_revision"] and (
                    identifier not in expected or set(previous[0]["unit_ids"]) - allowed
                    or fingerprint != state["source_fingerprint"]):
                raise ValueError("Carried decision does not belong to the current sources")
            latest[revision, identifier] = (previous[0], fingerprint)
        else:
            raise ValueError("Unknown review history event")
    seen = set()
    for item in state["items"]:
        if not isinstance(item, dict) or set(item) != {
            "segment_id", "candidates", "status", "confirmed_unit_ids", "review", "revision"
        }:
            raise ValueError("Invalid review item schema")
        identifier = item["segment_id"]
        if not isinstance(identifier, str) or identifier not in expected or identifier in seen:
            raise ValueError("Unknown or duplicate review item")
        seen.add(identifier)
        if not isinstance(item["candidates"], list):
            raise ValueError("Candidates must be a list")
        candidate_ids = []
        for candidate in item["candidates"]:
            if not isinstance(candidate, dict) or set(candidate) != {"unit_id", "score"}:
                raise ValueError("Invalid candidate schema")
            if (not isinstance(candidate["unit_id"], str) or candidate["unit_id"] not in allowed or
                    isinstance(candidate["score"], bool) or
                    not isinstance(candidate["score"], (int, float)) or not math.isfinite(candidate["score"])):
                raise ValueError("Invalid candidate identifier or score")
            candidate_ids.append(candidate["unit_id"])
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("Duplicated candidate identifier")
        if (not _ids(item["confirmed_unit_ids"]) or set(item["confirmed_unit_ids"]) - allowed or
                type(item["revision"]) is not int or item["revision"] < 0):
            raise ValueError("Invalid confirmed selection")
        event, _ = latest.get((state["source_revision"], identifier), (None, None))
        if item["review"] is None:
            if item["status"] != "needs_review" or item["confirmed_unit_ids"] or item["revision"] or event:
                raise ValueError("A candidate cannot be confirmed without an explicit review")
        else:
            _validate_actor(item["review"])
            review = item["review"]
            expected_status = {"match": "confirmed", "no_match": "confirmed_unmatched", "defer": "needs_review"}
            if (item["status"] != expected_status[review["decision"]] or
                    (review["decision"] == "match") != bool(item["confirmed_unit_ids"])):
                raise ValueError("Review decision and displayed status disagree")
            if (not event or event["review"] != review or event["unit_ids"] != item["confirmed_unit_ids"]
                    or event["revision"] != item["revision"]):
                raise ValueError("Saved decision is missing matching history")
    if seen != expected:
        raise ValueError("Missing current review segments")


def _validate_actor(review: object) -> None:
    fields = {"actor", "provenance", "reviewer", "reviewed_at_utc", "decision", "note",
              "source_fingerprint", "source_revision"}
    if not isinstance(review, dict) or set(review) != fields:
        raise ValueError("Missing explicit human decision provenance")
    if (review["actor"] != "human" or review["provenance"] != PROVENANCE or
            not isinstance(review["reviewer"], str) or not review["reviewer"].strip() or
            not _utc(review["reviewed_at_utc"]) or not isinstance(review["decision"], str)
            or review["decision"] not in {"match", "no_match", "defer"}
            or not isinstance(review["note"], str) or not _fingerprint(review["source_fingerprint"])
            or type(review["source_revision"]) is not int or review["source_revision"] < 0):
        raise ValueError("Invalid explicit human decision provenance")


def apply_decision(state: dict, segment_id: str, unit_ids: list[str], *, reviewer: str,
                   expected_fingerprint: str, decision: str = "match", note: str = "") -> dict:
    """Record an explicit human action; model output must never call this itself."""
    validate_review(state)
    if expected_fingerprint != state["source_fingerprint"]:
        raise ValueError("Sources changed; reopen the current review before confirming")
    if segment_id not in state["segment_ids"] or not _ids(unit_ids) or set(unit_ids) - set(state["unit_ids"]):
        raise ValueError("Unknown or duplicate selection")
    if decision not in {"match", "no_match", "defer"} or (decision == "match") != bool(unit_ids):
        raise ValueError("A match needs pages; no-match/defer cannot confirm pages")
    result = deepcopy(state)
    review = {"actor": "human", "provenance": PROVENANCE, "reviewer": reviewer,
              "reviewed_at_utc": _now(), "decision": decision, "note": note,
              "source_fingerprint": state["source_fingerprint"], "source_revision": state["source_revision"]}
    _validate_actor(review)
    item = next(item for item in result["items"] if item["segment_id"] == segment_id)
    item.update(status={"match": "confirmed", "no_match": "confirmed_unmatched", "defer": "needs_review"}[decision],
                confirmed_unit_ids=list(unit_ids), review=review, revision=item["revision"] + 1)
    result["history"].append({"event": "decision", "at_utc": review["reviewed_at_utc"],
                              "segment_id": segment_id, "unit_ids": list(unit_ids),
                              "review": deepcopy(review), "revision": item["revision"]})
    validate_review(result)
    return result


def refresh_review(state: dict, predictions: list[dict], units: list[Unit], chunks: list[Chunk],
                   *, previous_sources: tuple[list[Unit], list[Chunk]] | None = None) -> dict:
    """Carry decisions only when their relevant sources can be verified unchanged."""
    validate_review(state)
    fresh = create_review(predictions, units, chunks)
    if fresh["source_fingerprint"] == state["source_fingerprint"]:
        result = deepcopy(state)
        candidates = {i["segment_id"]: i["candidates"] for i in fresh["items"]}
        for item in result["items"]:
            item["candidates"] = candidates[item["segment_id"]]
    else:
        result = fresh
        result["schema_version"] = state["schema_version"]
        result["source_revision"] = state["source_revision"] + 1
        transition = {
            "event": "inputs_changed", "at_utc": _now(),
            "old_fingerprint": state["source_fingerprint"], "new_fingerprint": fresh["source_fingerprint"],
            "invalidated_decisions": sum(i["review"] is not None for i in state["items"]),
        }
        result["history"] = deepcopy(state["history"]) + [transition]
        if previous_sources is not None and source_fingerprint(*previous_sources) == state["source_fingerprint"]:
            _carry_decisions(state, result, previous_sources, units, chunks, transition)
    validate_review(result)
    return result


def _carry_decisions(state, result, previous_sources, units, chunks, transition):
    old_units, old_chunks = previous_sources
    before_pages = {unit.unit_id: unit for unit in old_units}
    after_pages = {unit.unit_id: unit for unit in units}
    unchanged_pages = {uid for uid, unit in after_pages.items() if before_pages.get(uid) == unit}
    before_cues = {chunk.segment_id: chunk for chunk in old_chunks}
    unchanged_cues = {chunk.segment_id for chunk in chunks if before_cues.get(chunk.segment_id) == chunk}
    old_items = {item["segment_id"]: item for item in state["items"] if item["review"] is not None}
    for item in result["items"]:
        identifier = item["segment_id"]
        old = old_items.get(identifier)
        if old is None or identifier not in unchanged_cues:
            continue
        decision = old["review"]["decision"]
        if not ((decision == "match" and set(old["confirmed_unit_ids"]) <= unchanged_pages)
                or (decision == "no_match" and before_pages == after_pages)):
            continue
        for key in ("status", "confirmed_unit_ids", "review", "revision"):
            item[key] = deepcopy(old[key])
        result["schema_version"] = 2
        result["history"].append({"event": "decision_carried", "at_utc": transition["at_utc"],
                                  "segment_id": identifier, "source_revision": result["source_revision"],
                                  "source_fingerprint": result["source_fingerprint"]})
        transition["invalidated_decisions"] -= 1


def save_review(path: Path, state: dict) -> None:
    validate_review(state)
    write_json(path, state)


def load_review(path: Path, *, expected_fingerprint: str | None = None) -> dict:
    """Validate stored structure; supply current fingerprint to check source identity."""
    state = read_json(path)
    validate_review(state)
    if expected_fingerprint is not None and expected_fingerprint != state["source_fingerprint"]:
        raise ValueError("Saved decisions refer to different sources; refresh review first")
    return state


def export_review(state: dict) -> dict:
    """Expose confirmed selections, leaving unreviewed candidates explicitly separate."""
    validate_review(state)
    return {"source_fingerprint": state["source_fingerprint"],
            "source_revision": state["source_revision"], "policy": POLICY,
            "provenance_limit": "Actor identity is an audit assertion, not authenticated proof",
            "items": [{"segment_id": i["segment_id"], "status": i["status"],
                       "confirmed_unit_ids": list(i["confirmed_unit_ids"]),
                       "candidates": deepcopy(i["candidates"]), "review": deepcopy(i["review"])}
                      for i in state["items"]]}
