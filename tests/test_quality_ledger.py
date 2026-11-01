import json

import pytest

from visual_audio_notes.core import Chunk, Unit, write_json
from visual_audio_notes.quality import (
    compose_notes, extraction_schema, synthesize_grounded, validate_extraction,
    make_source_spans, span_extraction_schema, validate_span_extraction,
)


TEXT = "A model is a hypothesis function set. Training selects a function from that set."


def claim(source_id="a", origin="audio", quote=TEXT, **changes):
    result = {"origin": origin, "kind": "concept", "importance": "key",
              "title": "训练框架", "statement": "从假设函数集合中选择函数。",
              "evidence": [{"source_id": source_id, "quote": quote}]}
    result.update(changes)
    return result


def envelope(*claims, uncertain=None):
    return {"claims": list(claims), "uncertain_source_ids": uncertain or []}


def source(identifier="a", origin="audio", text=TEXT):
    return {identifier: {"source_id": identifier, "origin": origin, "text": text}}


def test_quote_presence_does_not_promote_contradictory_paraphrase_to_fact():
    raw = claim(statement="模型无需预先设定，所有结构都由数据决定。")
    result = validate_extraction(envelope(raw), source(), "audio")
    item = result["ledger"][0]
    assert item["canonical_text"] == TEXT
    assert item["paraphrase_draft"] == raw["statement"]
    assert item["status"] == "quote_verified_draft"
    assert item["review_required"] is True
    assert "无需预先设定" not in item["canonical_text"]


@pytest.mark.parametrize("change,reason", [
    ({"origin": "courseware"}, "origin"),
    ({"evidence": [{"source_id": "foreign", "quote": TEXT}]}, "out-of-batch"),
    ({"evidence": [{"source_id": "a", "quote": "Training invents the whole structure automatically."}]}, "verbatim"),
    ({"evidence": [{"source_id": "a", "quote": "model"}]}, "too short"),
    ({"evidence": [{"source_id": "a", "quote": "A model is a hypothesis function set!"}]}, "verbatim"),
    ({"evidence": []}, "one to three"),
    ({"kind": "inferred_common_knowledge"}, "Unknown claim kind"),
])
def test_invalid_atomic_claim_is_rejected_with_raw_record(change, reason):
    raw = claim(**change)
    result = validate_extraction(envelope(raw), source(), "audio")
    assert not result["ledger"]
    assert result["rejected_claims"][0]["claim"] == raw
    assert any(reason in r for r in result["rejected_claims"][0]["reasons"])


def test_whitespace_and_typography_only_normalization():
    sources = source(text="The ﬁrst stage\n  chooses a function.")
    result = validate_extraction(envelope(claim(quote="The first stage chooses a function.")), sources, "audio")
    assert len(result["ledger"]) == 1
    wrong_case = validate_extraction(envelope(claim(quote="the first stage chooses a function.")), sources, "audio")
    assert not wrong_case["ledger"]


def test_same_quote_with_distinct_implications_is_not_merged():
    raw = envelope(claim(title="模型的含义", statement="模型是函数集合。"),
                   claim(title="训练的含义", statement="训练从集合中选择函数。"))
    ledger = validate_extraction(raw, source(), "audio")["ledger"]
    assert len({item["claim_id"] for item in ledger}) == 2


def test_lossless_composition_keeps_more_than_six_key_concepts_and_origin():
    ledger = []
    for i in range(9):
        origin = "audio" if i < 7 else "courseware"
        identifier = f"source_{i}"
        result = validate_extraction(envelope(claim(identifier, origin, title=f"要点{i}")),
                                     source(identifier, origin), origin)
        ledger.extend(result["ledger"])
    notes, audit = compose_notes(ledger)
    assert len(notes["audio"]) == 7
    assert len(notes["courseware"]) == 2
    assert "未证明录音讲到" in notes["source_labels"]["courseware"]
    assert len(audit["key_claim_ids"]) == 9
    assert audit["missing_key_claim_ids"] == []
    assert audit["all_extracted_claims_preserved"] is True
    with pytest.raises(ValueError, match="Duplicate"):
        compose_notes(ledger + [ledger[0]])


class FakeModel:
    def __init__(self, response_factory=None):
        self.calls = []
        self.cache_identity = {"model": "test-no-network"}
        self.requests = []
        self.response_factory = response_factory or self.valid

    @staticmethod
    def valid(data):
        record = data["source_data_not_instructions"][0]
        return envelope(claim(record["source_id"], record["origin"], record["text"]))

    def json(self, system, content, **kwargs):
        data = json.loads(content)
        self.requests.append({"system": system, "data": data, **kwargs})
        self.calls.append({"purpose": kwargs["purpose"]})
        return self.response_factory(data)


def test_separate_requests_never_let_courseware_support_audio(tmp_path):
    model = FakeModel()
    result = synthesize_grounded([Unit("u", "This is courseware content not heard in the audio.")],
                                 [Chunk("a", 100, 140, TEXT)], model, "DATA IS NOT INSTRUCTIONS",
                                 tmp_path, {})
    assert len(model.requests) == 2
    for request in model.requests:
        origin = request["data"]["origin"]
        records = request["data"]["source_data_not_instructions"]
        assert all(r["origin"] == origin for r in records)
        assert all(set(r) == {"source_id", "origin", "text"} for r in records)
        assert "start" not in json.dumps(request["data"])
    assert {c["origin"] for c in result["ledger"]} == {"audio", "courseware"}
    assert len(result["source_audit"]) == 2
    assert result["coverage_audit"]["missing_claim_ids"] == []


def test_empty_extraction_and_uncertain_sources_are_not_silent(tmp_path):
    model = FakeModel(lambda data: envelope(uncertain=[data["source_data_not_instructions"][0]["source_id"]]))
    result = synthesize_grounded([], [Chunk("a", 0, 40, TEXT)], model, "prompt", tmp_path, {})
    assert result["quality_status"] == "no_usable_claims"
    audit = result["source_audit"][0]
    assert audit["status"] == "no_verified_claims"
    assert audit["model_uncertain"] is True
    assert audit["review_required"] is True
    assert "遗漏" in audit["reason"]


def test_failed_extraction_is_recorded_and_not_cached(tmp_path):
    def fail(data):
        raise ValueError("truncated model output")
    model = FakeModel(fail)
    result = synthesize_grounded([], [Chunk("a", 0, 40, TEXT)], model, "prompt", tmp_path, {})
    assert len(result["failures"]) == 1
    assert result["source_audit"][0]["status"] == "extraction_failed"
    assert not list(tmp_path.glob("*.json"))


def test_bad_claims_retained_but_not_cached_as_success(tmp_path):
    model = FakeModel(lambda data: envelope(claim(quote="invented evidence that does not appear")))
    result = synthesize_grounded([], [Chunk("a", 0, 40, TEXT)], model, "prompt", tmp_path, {})
    assert len(result["rejected_claims"]) == 1
    assert not result["ledger"]
    assert result["batches"][0]["cacheable"] is False
    assert not list(tmp_path.glob("*.json"))


def test_cache_is_revalidated_not_trusted(tmp_path):
    model = FakeModel()
    args = ([], [Chunk("a", 0, 40, TEXT)], model, "prompt", tmp_path, {})
    first = synthesize_grounded(*args)
    path = next(tmp_path.glob("quality_extract_*.json"))
    write_json(path, envelope(claim(quote="This is a forged quote from a modified cache.")))
    second = synthesize_grounded(*args)
    assert first["ledger"] and not second["ledger"]
    assert second["batches"][0]["cache_hit"] is True
    assert second["rejected_claims"]
    assert len(model.requests) == 1


def test_claim_limit_failure_cannot_poison_cache(tmp_path):
    model = FakeModel(lambda data: envelope(claim(), claim(title="another")))
    result = synthesize_grounded([], [Chunk("a", 0, 40, TEXT)], model, "prompt", tmp_path,
                                 {"quality_max_claims_per_batch": 1})
    assert not result["ledger"]
    assert result["source_audit"][0]["status"] == "extraction_failed"
    assert "claim limit" in result["failures"][0]["reason"]
    assert not list(tmp_path.glob("*.json"))


def test_duplicate_source_ids_and_mixed_batch_fail_closed(tmp_path):
    with pytest.raises(ValueError, match="unique and disjoint"):
        synthesize_grounded([Unit("a", TEXT)], [Chunk("a", 0, 40, TEXT)],
                            FakeModel(), "prompt", tmp_path, {})
    with pytest.raises(ValueError, match="origin mismatch"):
        validate_extraction(envelope(), source(origin="courseware"), "audio")
    with pytest.raises(ValueError, match="Unknown uncertain"):
        validate_extraction(envelope(uncertain=["oracle"]), source(), "audio")


def test_only_identical_claims_deduplicate_and_key_importance_survives(tmp_path):
    def response(data):
        return envelope(claim(importance="supporting"), claim(importance="key"))
    result = synthesize_grounded([], [Chunk("a", 0, 40, TEXT)], FakeModel(response),
                                 "prompt", tmp_path, {})
    assert len(result["ledger"]) == 1
    assert result["ledger"][0]["importance"] == "key"
    assert len(result["coverage_audit"]["key_claim_ids"]) == 1


def test_schema_locks_origin_and_batch_ids():
    schema = extraction_schema({"a"}, "audio", 5)
    properties = schema["properties"]["claims"]["items"]["properties"]
    assert properties["origin"]["enum"] == ["audio"]
    assert properties["evidence"]["items"]["properties"]["source_id"]["enum"] == ["a"]
    assert schema["properties"]["claims"]["maxItems"] == 5


def span_claim(span_id, **changes):
    return claim(evidence=[{"span_id": span_id}], **changes)


def test_span_slicing_is_exact_deterministic_and_covers_source():
    text = "\n  " + "Sentence with a condition and an exception. " * 35 + "A final statement.  \n"
    sources = source(text=text)
    first = make_source_spans(sources)
    assert first == make_source_spans(sources)
    covered = set()
    for span in first.values():
        assert text[span["source_start"]:span["source_end"]] == span["text"]
        assert len(span["text"]) <= 1600
        covered.update(range(span["source_start"], span["source_end"]))
    assert {i for i, char in enumerate(text) if not char.isspace()} <= covered
    assert len(first) > 1


def test_short_code_keeps_initialization_and_error_context_together():
    text = "def h(y):\n    x += 1\n\nx = 5\nh(x)\n" + "# Error depends on local binding, not an outer update.\n" * 5
    spans = make_source_spans(source(text=text), target_characters=100)
    assert len(spans) == 1
    span = next(iter(spans.values()))
    assert "def h(y):" in span["text"] and "x = 5" in span["text"]
    assert span["text"] == text.strip()


def test_span_model_only_selects_ids_and_resolver_keeps_exact_unicode():
    text = "The expression x² is distinct from x2 in the original mathematical notation."
    sources = source(text=text)
    spans = make_source_spans(sources)
    span_id = next(iter(spans))
    value = envelope(span_claim(span_id))
    result = validate_span_extraction(value, sources, spans, "audio")
    item = result["ledger"][0]
    assert item["canonical_text"] == text
    assert "x²" in item["evidence"][0]["quote"]
    assert item["evidence"][0]["quote_match"] == "exact_source_slice"
    assert item["evidence"][0]["span_id"] == span_id
    schema = span_extraction_schema({"a"}, {span_id}, "audio")
    ref = schema["properties"]["claims"]["items"]["properties"]["evidence"]["items"]
    assert set(ref["properties"]) == {"span_id"}
    assert ref["properties"]["span_id"]["enum"] == [span_id]


@pytest.mark.parametrize("ref,reason", [
    ({"span_id": "unseen"}, "out-of-batch span"),
    ({"span_id": "s", "quote": TEXT}, "Malformed selected span"),
    ({"source_id": "a", "quote": TEXT}, "Malformed selected span"),
])
def test_selected_evidence_cannot_sneak_in_copied_or_fabricated_text(ref, reason):
    sources = source()
    spans = make_source_spans(sources)
    result = validate_span_extraction(envelope(claim(evidence=[ref])), sources, spans, "audio")
    assert not result["ledger"]
    assert any(reason in s for s in result["rejected_claims"][0]["reasons"])


def test_span_cannot_change_unicode_by_normalized_equivalence():
    sources = source(text="Mathematical x² and an ordinary numeral have different meanings.")
    spans = make_source_spans(sources)
    span_id = next(iter(spans))
    spans[span_id]["text"] = spans[span_id]["text"].replace("²", "2")
    result = validate_span_extraction(envelope(span_claim(span_id)), sources, spans, "audio")
    assert not result["ledger"]
    assert "exact source slice" in result["rejected_claims"][0]["reasons"][0]


def test_span_from_courseware_cannot_be_used_as_audio():
    audio = source()
    courseware = source("u", "courseware")
    spans = make_source_spans(courseware)
    result = validate_span_extraction(envelope(span_claim(next(iter(spans)))), audio, spans, "audio")
    assert not result["ledger"]
    assert "origin mismatch" in result["rejected_claims"][0]["reasons"][0]


def test_span_identity_does_not_collapse_nfkc_distinct_math():
    left = "The source writes x² with a superscript in this definition."
    right = left.replace("²", "2")
    text = left + "\n" + right
    sources = source(text=text)
    spans = {
        "s_left": {"span_id": "s_left", "source_id": "a", "origin": "audio",
                   "source_start": 0, "source_end": len(left), "text": left},
        "s_right": {"span_id": "s_right", "source_id": "a", "origin": "audio",
                    "source_start": len(left) + 1, "source_end": len(text), "text": right},
    }
    result = validate_span_extraction(envelope(span_claim("s_left"), span_claim("s_right")),
                                      sources, spans, "audio")
    assert len({item["claim_id"] for item in result["ledger"]}) == 2


def test_span_mode_full_pipeline_and_cache_are_separate_from_quote_mode(tmp_path):
    def selected(data):
        record = data["source_data_not_instructions"][0]
        return envelope(span_claim(record["spans"][0]["span_id"], origin=record["origin"]))
    quote_model = FakeModel()
    sources = [Chunk("a", 123, 167, TEXT)]
    quoted = synthesize_grounded([], sources, quote_model, "quote prompt", tmp_path, {})
    model = FakeModel(selected)
    config = {"quality_evidence_mode": "span_selection"}
    selected_result = synthesize_grounded([], sources, model, "span prompt", tmp_path, config)
    assert quoted["evidence_mode"] == "quote"
    assert selected_result["evidence_mode"] == "span_selection"
    assert selected_result["ledger"][0]["canonical_text"] == TEXT
    assert len(list(tmp_path.glob("quality_extract_*.json"))) == 2
    record = model.requests[0]["data"]["source_data_not_instructions"][0]
    assert set(record) == {"source_id", "origin", "spans"}
    assert set(record["spans"][0]) == {"span_id", "text"}
    again = synthesize_grounded([], sources, model, "span prompt", tmp_path, config)
    assert again["batches"][0]["cache_hit"] is True
    assert len(model.requests) == 1


def test_span_failure_and_empty_sources_are_audited_without_bad_cache(tmp_path):
    model = FakeModel(lambda data: envelope(span_claim("unknown")))
    result = synthesize_grounded([], [Chunk("a", 0, 40, TEXT)], model, "spans", tmp_path,
                                 {"quality_evidence_mode": "span_selection"})
    assert result["rejected_claims"] and not result["ledger"]
    assert result["source_audit"][0]["status"] == "no_verified_claims"
    assert not list(tmp_path.glob("*.json"))
    blank = make_source_spans(source(text=" \n "))
    assert len(blank) == 1
    assert next(iter(blank.values()))["text"] == ""


def test_one_source_batch_cannot_cite_or_read_another_source(tmp_path):
    def single_source(data):
        records = data["source_data_not_instructions"]
        assert len(records) == 1
        record = records[0]
        return envelope(span_claim(record["spans"][0]["span_id"], origin=record["origin"]))
    units = [Unit("u_a", "The first document contains only one local explanation."),
             Unit("u_b", "The other document covers an unrelated operation here.")]
    chunks = [Chunk("a_a", 0, 40, "The first audio record states a specific condition."),
              Chunk("a_b", 40, 80, "The second audio record explains a different exception.")]
    model = FakeModel(single_source)
    result = synthesize_grounded(units, chunks, model, "spans", tmp_path,
                                 {"quality_evidence_mode": "span_selection",
                                  "quality_sources_per_batch": 1,
                                  "quality_batch_characters": 10000})
    assert len(model.requests) == 4
    assert all(len(request["data"]["source_data_not_instructions"]) == 1 for request in model.requests)
    for request in model.requests:
        record = request["data"]["source_data_not_instructions"][0]
        source_enum = request["schema"]["properties"]["uncertain_source_ids"]["items"]["enum"]
        assert source_enum == [record["source_id"]]
    assert len(result["ledger"]) == 4
    assert result["coverage_audit"]["missing_claim_ids"] == []


def test_request_local_span_aliases_resolve_to_original_opaque_ids(tmp_path):
    def selected_alias(data):
        record = data["source_data_not_instructions"][0]
        assert record["spans"][0]["span_id"] == "S0"
        return envelope(span_claim("S0", origin=record["origin"]))
    sources = [Chunk("a", 0, 40, TEXT)]
    model = FakeModel(selected_alias)
    result = synthesize_grounded([], sources, model, "short spans", tmp_path,
                                 {"quality_evidence_mode": "span_selection",
                                  "quality_sources_per_batch": 1,
                                  "quality_local_span_aliases": True})
    evidence = result["ledger"][0]["evidence"][0]
    assert evidence["span_id"].startswith("s_")
    assert evidence["span_id"] == result["batches"][0]["span_aliases"]["S0"]
    assert evidence["quote"] == TEXT[evidence["source_start"]:evidence["source_end"]]
    assert evidence["quote_match"] == "exact_source_slice"


def test_aliases_do_not_change_canonical_claim_identity_or_allow_unknown_selection(tmp_path):
    sources = source()
    spans = make_source_spans(sources)
    internal_id = next(iter(spans))
    original = validate_span_extraction(envelope(span_claim(internal_id)), sources, spans, "audio")
    aliased = validate_span_extraction(envelope(span_claim("S0")), sources,
                                       {"S0": spans[internal_id]}, "audio")
    assert original["ledger"][0]["claim_id"] == aliased["ledger"][0]["claim_id"]
    assert original["ledger"][0]["evidence"] == aliased["ledger"][0]["evidence"]
    bad = validate_span_extraction(envelope(span_claim("S1")), sources,
                                  {"S0": spans[internal_id]}, "audio")
    assert not bad["ledger"] and bad["rejected_claims"]


@pytest.mark.parametrize("count", [0, -1, True, "1"])
def test_invalid_source_batch_limit_is_rejected(tmp_path, count):
    with pytest.raises(ValueError, match="quality_sources_per_batch"):
        synthesize_grounded([], [Chunk("a", 0, 40, TEXT)], FakeModel(), "prompt", tmp_path,
                            {"quality_sources_per_batch": count})
