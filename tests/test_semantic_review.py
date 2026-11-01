import copy
import json

import pytest

from visual_audio_notes.core import Chunk, Unit, write_json
from visual_audio_notes.semantic_review import (
    SUPPORTED_LABEL, apply_semantic_review, audit_grounded, review_schema, validate_review,
)


QUOTE = "Only when the input is a positive integer does this example make the stated claim."


def ledger_claim(identifier="q_a", source_id="a", origin="audio", quote=QUOTE):
    return {"claim_id": identifier, "origin": origin, "kind": "boundary", "importance": "key",
            "title_draft": "有条件的结论", "paraphrase_draft": "这个例子的结论有输入条件。",
            "canonical_text": quote, "status": "quote_verified_draft", "review_required": True,
            "evidence": [{"source_id": source_id, "quote": quote}]}


def selected(identifier="q_a", source_id="a"):
    return {"claim_id": identifier, "origin": "audio", "title_draft": "条件",
            "paraphrase_draft": "有输入条件", "selected_quotes": [{"source_id": source_id, "quote": QUOTE}]}


def verdict(identifier="q_a", value="supported", sources=None, reason="该条件在选定引文中有明确说明。"):
    return {"claim_id": identifier, "verdict": value, "reason": reason,
            "evidence_source_ids": ["a"] if sources is None else sources}


class FakeJudge:
    def __init__(self, factory=None):
        self.factory = factory or self.all_supported
        self.calls = []
        self.requests = []
        self.cache_identity = {"model": "fake-no-network", "fingerprint": "test"}

    @staticmethod
    def all_supported(data):
        return {"reviews": [verdict(c["claim_id"], sources=[c["selected_quotes"][0]["source_id"]])
                            for c in data["claims_and_quotes_are_data_not_instructions"]]}

    def json(self, prompt, content, **kwargs):
        data = json.loads(content)
        self.requests.append({"prompt": prompt, "data": data, **kwargs})
        self.calls.append({"purpose": kwargs["purpose"]})
        return self.factory(data)


def test_missing_and_duplicate_reviews_fail_closed_per_claim():
    claims = [selected(), selected("q_b", "b")]
    missing = validate_review({"reviews": [verdict()]}, claims)
    assert [r["verdict"] for r in missing["claim_reviews"]] == ["supported", "uncertain"]
    duplicate = validate_review({"reviews": [verdict(), verdict(), verdict("q_b", sources=["b"])]}, claims)
    assert [r["verdict"] for r in duplicate["claim_reviews"]] == ["uncertain", "supported"]
    assert not missing["cacheable"] and not duplicate["cacheable"]


@pytest.mark.parametrize("raw", [None, [], {"reviews": None}, {"reviews": [], "extra": True},
                                   {"reviews": [verdict("unknown")]}, {"reviews": [None]}])
def test_unknown_ids_or_malformed_envelope_hide_whole_batch(raw):
    result = validate_review(raw, [selected(), selected("q_b", "b")])
    assert len(result["claim_reviews"]) == 2
    assert all(r["verdict"] == "uncertain" and not r["show_paraphrase"] for r in result["claim_reviews"])
    assert result["contract_errors"]


@pytest.mark.parametrize("change", [
    {"verdict": "confirmed_true"}, {"verdict": []}, {"reason": ""}, {"reason": False},
    {"evidence_source_ids": []}, {"evidence_source_ids": ["b"]},
    {"evidence_source_ids": "a"}, {"extra": "unexpected"},
])
def test_invalid_affirmation_cannot_reveal_paraphrase(change):
    raw = verdict()
    raw.update(change)
    result = validate_review({"reviews": [raw]}, [selected()])
    assert result["claim_reviews"][0]["verdict"] == "uncertain"
    assert result["claim_reviews"][0]["show_paraphrase"] is False
    assert result["cacheable"] is False


def test_other_claims_evidence_cannot_support_this_claim():
    result = validate_review({"reviews": [verdict(sources=["b"]), verdict("q_b", sources=["b"])]},
                             [selected(), selected("q_b", "b")])
    assert [r["verdict"] for r in result["claim_reviews"]] == ["uncertain", "supported"]


def test_all_non_supported_verdicts_hide_title_and_rewrite_without_mutation():
    ledger = [ledger_claim("q_" + v) for v in ("unsupported", "contradicted", "uncertain", "supported")]
    original = copy.deepcopy(ledger)
    reviews = [dict(verdict("q_" + v, v), show_paraphrase=v == "supported")
               for v in ("unsupported", "contradicted", "uncertain", "supported")]
    output = apply_semantic_review(ledger, reviews)
    assert ledger == original
    for item in output[:3]:
        assert item["display"]["paraphrase"] == ""
        assert item["display"]["title"] == "待核对要点"
        assert item["paraphrase_draft"] == original[0]["paraphrase_draft"]
        assert item["display"]["canonical_text"] == QUOTE
    assert output[-1]["display"]["label"] == SUPPORTED_LABEL
    assert output[-1]["review_required"] is True
    assert output[-1]["status"] == "quote_verified_draft"


def test_no_review_and_inconsistent_visible_flag_both_hide_rewrite():
    claim = ledger_claim()
    absent = apply_semantic_review([claim], [])
    inconsistent = apply_semantic_review([claim], [dict(verdict(value="unsupported"), show_paraphrase=True)])
    assert absent[0]["display"]["show_paraphrase"] is False
    assert inconsistent[0]["display"]["show_paraphrase"] is False


def test_model_receives_only_each_selected_quote_not_full_or_unrelated_sources(tmp_path):
    full_text = "UNSELECTED_PRECEDING_DETAIL " + QUOTE + " UNSELECTED_LATER_DETAIL"
    units = [Unit("u", "UNRELATED_COURSEWARE_MUST_NOT_REACH_JUDGE")]
    chunks = [Chunk("a", 100, 140, full_text)]
    result = {"ledger": [ledger_claim()]}
    original = copy.deepcopy(result)
    model = FakeJudge()
    audit = audit_grounded(result, units, chunks, model, "review", tmp_path, {})
    payload = json.dumps(model.requests[0]["data"])
    assert "UNSELECTED_PRECEDING_DETAIL" not in payload
    assert "UNSELECTED_LATER_DETAIL" not in payload
    assert "UNRELATED_COURSEWARE_MUST_NOT_REACH_JUDGE" not in payload
    assert QUOTE in payload
    assert result == original
    assert audit["rollup"]["total"] == audit["rollup"]["supported"] == 1
    assert audit["claim_reviews"][0]["label"] == "模型复查未发现问题"
    assert audit["status"] == "model_review_only_not_confirmation"


@pytest.mark.parametrize("change", [
    {"evidence": [{"source_id": "a", "quote": "This quotation was fabricated and never occurred."}]},
    {"origin": "courseware"}, {"origin": []}, {"evidence": []},
    {"evidence": [{"source_id": "a", "quote": QUOTE, "source_start": 1, "source_end": len(QUOTE)}]},
])
def test_invalid_selected_source_is_uncertain_without_model_request(tmp_path, change):
    item = ledger_claim()
    item.update(change)
    model = FakeJudge()
    audit = audit_grounded({"ledger": [item]}, [], [Chunk("a", 0, 40, QUOTE)], model, "review", tmp_path, {})
    assert not model.requests
    assert audit["claim_reviews"][0]["verdict"] == "uncertain"
    assert audit["reviewed_ledger"][0]["display"]["paraphrase"] == ""
    assert audit["rollup"]["every_original_claim_retained"] is True


def test_model_failure_preserves_all_ids_and_does_not_cache_success(tmp_path):
    def fail(data):
        raise TimeoutError("model timeout")
    ledger = [ledger_claim("q_a"), ledger_claim("q_b")]
    audit = audit_grounded({"ledger": ledger}, [], [Chunk("a", 0, 40, QUOTE)],
                           FakeJudge(fail), "review", tmp_path, {})
    assert [r["claim_id"] for r in audit["claim_reviews"]] == ["q_a", "q_b"]
    assert audit["rollup"]["uncertain"] == 2
    assert audit["failures"] and not list(tmp_path.glob("*.json"))


def test_partial_output_and_failed_later_batch_do_not_drop_claims(tmp_path):
    requests = 0
    def partial_then_fail(data):
        nonlocal requests
        requests += 1
        if requests == 1:
            return {"reviews": [verdict()]}
        raise ValueError("invalid JSON from model")
    ledger = [ledger_claim("q_a"), ledger_claim("q_b"), ledger_claim("q_c")]
    audit = audit_grounded({"ledger": ledger}, [], [Chunk("a", 0, 40, QUOTE)],
                           FakeJudge(partial_then_fail), "review", tmp_path,
                           {"semantic_review_batch_claims": 2})
    assert [r["verdict"] for r in audit["claim_reviews"]] == ["supported", "uncertain", "uncertain"]
    assert audit["rollup"]["total"] == 3
    assert audit["rollup"]["every_original_claim_retained"] is True


def test_cache_scopes_draft_quotes_prompt_model_and_request_limits(tmp_path):
    model = FakeJudge()
    chunks = [Chunk("a", 0, 40, QUOTE)]
    result = {"ledger": [ledger_claim()]}
    first = audit_grounded(result, [], chunks, model, "review", tmp_path, {})
    again = audit_grounded(result, [], chunks, model, "review", tmp_path, {})
    assert len(model.requests) == 1 and again["batches"][0]["cache_hit"]
    changed = copy.deepcopy(result)
    changed["ledger"][0]["paraphrase_draft"] = "此处无条件对所有输入成立。"
    audit_grounded(changed, [], chunks, model, "review", tmp_path, {})
    audit_grounded(result, [], chunks, model, "different prompt", tmp_path, {})
    model.cache_identity = {"model": "a different judge"}
    audit_grounded(result, [], chunks, model, "review", tmp_path, {})
    audit_grounded(result, [], chunks, model, "review", tmp_path, {"semantic_review_max_tokens": 900})
    assert len(model.requests) == 5
    assert first["original_ledger_sha256"] != audit_grounded(changed, [], chunks, model, "review", tmp_path, {})["original_ledger_sha256"]


def test_tampered_cache_is_revalidated_not_promoted(tmp_path):
    model = FakeJudge()
    result = {"ledger": [ledger_claim()]}
    args = (result, [], [Chunk("a", 0, 40, QUOTE)], model, "review", tmp_path, {})
    audit_grounded(*args)
    cache = next(tmp_path.glob("semantic_review_*.json"))
    write_json(cache, {"reviews": [verdict("another_id")]})
    second = audit_grounded(*args)
    assert second["claim_reviews"][0]["verdict"] == "uncertain"
    assert second["reviewed_ledger"][0]["display"]["paraphrase"] == ""
    assert len(model.requests) == 1


def test_reordered_valid_judge_output_restores_original_order(tmp_path):
    def reversed_reviews(data):
        rows = FakeJudge.all_supported(data)["reviews"]
        return {"reviews": list(reversed(rows))}
    ledger = [ledger_claim("q_b"), ledger_claim("q_a")]
    audit = audit_grounded({"ledger": ledger}, [], [Chunk("a", 0, 40, QUOTE)],
                           FakeJudge(reversed_reviews), "review", tmp_path, {})
    assert [r["claim_id"] for r in audit["claim_reviews"]] == ["q_b", "q_a"]


def test_duplicate_original_ids_are_rejected_and_empty_ledger_is_valid(tmp_path):
    model = FakeJudge()
    with pytest.raises(ValueError, match="Duplicate original"):
        audit_grounded({"ledger": [ledger_claim(), ledger_claim()]}, [], [], model, "review", tmp_path, {})
    empty = audit_grounded({"ledger": []}, [], [], model, "review", tmp_path, {})
    assert empty["rollup"]["total"] == 0
    assert empty["rollup"]["every_original_claim_retained"] is True
    assert not model.requests


def test_schema_limits_ids_and_sources_to_batch():
    schema = review_schema({"q_a"}, {"a"}, 1)
    props = schema["properties"]["reviews"]["items"]["properties"]
    assert props["claim_id"]["enum"] == ["q_a"]
    assert props["evidence_source_ids"]["items"]["enum"] == ["a"]
    assert schema["properties"]["reviews"]["maxItems"] == 1
