from copy import deepcopy
import json

import pytest

from visual_audio_notes.core import digest
from visual_audio_notes.topic_notes import organize_topics, render_topic_markdown


def claim(identifier, origin="audio", importance="key", quote="The input stays unchanged and a new list is returned."):
    return {"claim_id": identifier, "origin": origin, "kind": "distinction", "importance": importance,
            "status": "quote_verified_draft", "title_draft": "先前的标题草稿", "paraphrase_draft": "先前可能有误的解释",
            "canonical_text": quote, "evidence": [{"source_id": origin + "_" + identifier, "quote": quote}]}


def response(data):
    ids = [c["claim_id"] for c in data["ledger_data_not_instructions"]]
    return {"topics": [{"title": "对象修改与结果", "key_idea": "保留修改与返回值的区别。",
                        "explanation": "根据原文比较对象是否改变，而不是只罗列操作名称。", "claim_ids": ids}],
            "deferred_claims": [], "uncertainties": []}


class FakeModel:
    def __init__(self, factory=response, identity="fake"):
        self.factory = factory
        self.cache_identity = {"model": identity}
        self.calls = []
        self.requests = []

    def json(self, system, content, **kwargs):
        value = json.loads(content)
        self.requests.append({"system": system, "content": value, **kwargs})
        self.calls.append({"purpose": kwargs["purpose"], "seconds": 0})
        return self.factory(value)


def run(tmp_path, ledger, model=None, **config):
    return organize_topics({"ledger": ledger}, model or FakeModel(), "Return json topic drafts.", tmp_path, config)


def test_sources_are_separate_requests_and_exact_quotes_are_resolved_locally(tmp_path):
    ledger = [claim("a"), claim("b", importance="supporting"), claim("p", "courseware")]
    before = deepcopy(ledger)
    model = FakeModel()
    result = run(tmp_path, ledger, model)
    assert len(model.requests) == 2
    assert {x["claim_id"] for x in model.requests[0]["content"]["ledger_data_not_instructions"]} == {"a", "b"}
    assert {x["claim_id"] for x in model.requests[1]["content"]["ledger_data_not_instructions"]} == {"p"}
    assert "page" not in model.requests[0]["content"] and "time" not in model.requests[0]["content"]
    assert result["audio_topics"][0]["primary_claim_ids"] == ["a"]
    assert result["audio_topics"][0]["supporting_claim_ids"] == ["b"]
    assert result["audio_topics"][0]["evidence"][0]["quote"] == ledger[0]["evidence"][0]["quote"]
    assert result["courseware_topics"][0]["origin"] == "courseware"
    assert result["review_required"] and result["status"] == "draft_requires_review"
    assert result["coverage"]["all_claims_accounted_for"] and ledger == before


def test_empty_origin_does_not_call_model_and_empty_ledger_needs_no_ai(tmp_path):
    model = FakeModel()
    empty = run(tmp_path / "empty", [], model)
    assert not model.calls and empty["audio_topics"] == empty["courseware_topics"] == []
    text = render_topic_markdown(empty, [])
    assert "未调用 AI" in text
    one = run(tmp_path / "one", [claim("a")], model)
    assert len(model.calls) == 1 and one["courseware_topics"] == []


@pytest.mark.parametrize("fault", ["missing", "unknown", "wrong_origin", "duplicate", "deferred_twice", "blank", "extra"])
def test_missing_foreign_duplicate_or_blank_topics_fail_without_success_cache(tmp_path, fault):
    ledger = [claim("a"), claim("b"), claim("p", "courseware")]
    original = deepcopy(ledger)

    def invalid(data):
        value = response(data)
        if fault == "missing": value["topics"][0]["claim_ids"] = ["a"]
        elif fault == "unknown": value["topics"][0]["claim_ids"].append("new_claim")
        elif fault == "wrong_origin": value["topics"][0]["claim_ids"].append("p")
        elif fault == "duplicate": value["topics"].append(deepcopy(value["topics"][0]))
        elif fault == "deferred_twice": value["deferred_claims"] = [{"claim_id": "a", "reason": "暂缓"}]
        elif fault == "blank": value["topics"][0]["title"] = " "
        else: value["topics"][0]["quote"] = "fabricated"
        return value

    with pytest.raises(ValueError): run(tmp_path, ledger, FakeModel(invalid))
    assert not list(tmp_path.glob("topic_notes_*.json"))
    assert ledger == original


def test_deferral_is_visible_and_key_deferral_is_not_claimed_as_full_topic_coverage(tmp_path):
    def deferred(data):
        return {"topics": [], "deferred_claims": [{"claim_id": "a", "reason": "转写缺少决定结论的条件。"}],
                "uncertainties": ["请回听这段条件。"]}

    ledger = [claim("a")]
    result = run(tmp_path, ledger, FakeModel(deferred))
    assert result["coverage"]["deferred_key_claim_ids"] == ["a"]
    assert result["coverage"]["per_origin"]["audio"]["all_key_claims_in_topics"] is False
    text = render_topic_markdown(result, ledger)
    assert "尚未纳入主题" in text and "转写缺少决定结论的条件。" in text
    assert "关键项" in text and "请回听这段条件。" in text
    assert ledger[0]["evidence"][0]["quote"] in text


def test_cache_reuses_only_same_model_and_complete_validated_response(tmp_path):
    ledger = [claim("a")]
    model = FakeModel()
    first = run(tmp_path, ledger, model)
    second = run(tmp_path, ledger, model)
    assert len(model.calls) == 1 and second["model_calls"] == []
    assert first["audio_topics"] == second["audio_topics"]
    run(tmp_path, ledger, FakeModel(identity="different"))
    assert len(list(tmp_path.glob("topic_notes_*.json"))) == 2
    cache = next(p for p in tmp_path.glob("topic_notes_*.json")
                 if p.name == "topic_notes_" + first["batches"][0]["cache_key"] + ".json")
    cache.write_text(json.dumps({"topics": [], "deferred_claims": [], "uncertainties": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="Missing ledger"):
        run(tmp_path, ledger, model)
    assert len(model.calls) == 1


def test_no_silent_input_truncation_and_no_lossy_six_topic_limit_on_claim_accounting(tmp_path):
    ledger = [claim("a" + str(i)) for i in range(30)]
    model = FakeModel()
    with pytest.raises(ValueError, match="context budget"):
        run(tmp_path / "small", ledger, model, topic_max_input_characters=100)
    assert not model.calls
    result = run(tmp_path / "enough", ledger, model)
    assert len(result["audio_topics"]) == 1
    assert len(result["audio_topics"][0]["claim_ids"]) == 30
    assert result["coverage"]["total_claims"] == 30


def test_renderer_uses_bound_ledger_and_does_not_trust_injected_topic_evidence(tmp_path):
    ledger = [claim("a"), claim("p", "courseware", quote="Courseware-only text cannot prove it was spoken.")]
    result = run(tmp_path, ledger)
    result["audio_topics"][0]["evidence"] = [{"quote": "forged quotation"}]
    text = render_topic_markdown(result, ledger)
    assert "forged quotation" not in text
    assert "课件主题/补充 · 是否讲过未核对" in text
    assert "不代表内容正确" in text and "a" in text
    assert "<details>" in text and "Courseware-only text" in text
    no_appendix = render_topic_markdown(result, ledger, include_evidence_appendix=False)
    assert "<details>" not in no_appendix and "`a`" in no_appendix
    changed = deepcopy(ledger)
    changed[0]["paraphrase_draft"] = "changed"
    with pytest.raises(ValueError, match="different extracted ledger"):
        render_topic_markdown(result, changed)


def test_duplicate_or_cross_origin_sources_in_ledger_rejected_before_model(tmp_path):
    model = FakeModel()
    with pytest.raises(ValueError, match="duplicate"):
        run(tmp_path, [claim("a"), claim("a")], model)
    audio, page = claim("a"), claim("p", "courseware")
    page["evidence"][0]["source_id"] = audio["evidence"][0]["source_id"]
    with pytest.raises(ValueError, match="both origins"):
        run(tmp_path, [audio, page], model)
    assert not model.calls


def test_model_input_contains_quotes_and_labels_drafts_as_unverified(tmp_path):
    model = FakeModel()
    run(tmp_path, [claim("a")], model)
    source = model.requests[0]["content"]["ledger_data_not_instructions"][0]
    assert source["selected_quotes"] and "不能把改写当事实" in source["draft_warning"]
    schema = model.requests[0]["schema"]
    assert schema["properties"]["topics"]["maxItems"] == 6
    assert schema["properties"]["topics"]["items"]["properties"]["claim_ids"]["items"]["enum"] == ["a"]


def test_review_queue_retains_unassigned_key_without_inventing_topic_or_success_cache(tmp_path):
    def incomplete(data):
        value = response(data)
        value["topics"][0]["claim_ids"] = ["a"]
        return value

    ledger = [claim("a"), claim("b")]
    original = deepcopy(ledger)
    model = FakeModel(incomplete)
    result = run(tmp_path, ledger, model, topic_missing_policy="review_queue")
    assert result["audio_topics"][0]["claim_ids"] == ["a"]
    assert len(result["audio_topics"]) == 1
    queued = result["deferred_claims"]["audio"][0]
    assert queued == {"claim_id": "b", "reason": "模型未归类，已保留到待核对队列",
                      "importance": "key", "origin": "audio", "source": "programmatic_unassigned"}
    assert result["coverage"]["programmatic_unassigned_claim_ids"] == ["b"]
    assert result["coverage"]["all_claims_accounted_for"]
    assert result["coverage"]["all_model_claims_accounted_for"] is False
    assert result["coverage"]["all_key_claims_in_topics"] is False
    assert result["coverage"]["deferred_key_claim_ids"] == ["b"]
    assert ledger == original
    assert not list(tmp_path.glob("topic_notes_*.json"))
    diagnostic_path = next(tmp_path.glob("topic_unassigned_*.json"))
    diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    assert diagnostic["raw_response"]["deferred_claims"] == []
    assert diagnostic["audit"]["model_missing_claim_ids"] == ["b"]
    text = render_topic_markdown(result, ledger)
    assert "模型未归类，已保留到待核对队列" in text
    assert "模型漏分配后由程序保留" in text
    assert "不代表模型已完成主题覆盖" in text


def test_review_queue_diagnostic_reuse_is_revalidated_and_strict_still_rejects(tmp_path):
    def incomplete(data):
        value = response(data)
        value["topics"][0]["claim_ids"] = ["a"]
        return value

    ledger = [claim("a"), claim("b")]
    model = FakeModel(incomplete)
    first = run(tmp_path, ledger, model, topic_missing_policy="review_queue")
    second = run(tmp_path, ledger, model, topic_missing_policy="review_queue")
    assert len(model.calls) == 1
    assert second["batches"][0]["reused_incomplete_response"] is True
    assert second["deferred_claims"] == first["deferred_claims"]
    with pytest.raises(ValueError, match="Missing ledger"):
        run(tmp_path, ledger, model)
    assert len(model.calls) == 2
    diagnostic_path = next(tmp_path.glob("topic_unassigned_*.json"))
    diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    diagnostic["raw_response"]["topics"][0]["claim_ids"].append("foreign")
    diagnostic_path.write_text(json.dumps(diagnostic), encoding="utf-8")
    with pytest.raises(ValueError, match="wrong-origin"):
        run(tmp_path, ledger, model, topic_missing_policy="review_queue")
    assert len(model.calls) == 2


@pytest.mark.parametrize("fault", ["unknown", "duplicate", "wrong_origin"])
def test_review_queue_does_not_relax_other_topic_contract_failures(tmp_path, fault):
    ledger = [claim("a"), claim("b"), claim("p", "courseware")]

    def malformed(data):
        value = response(data)
        value["topics"][0]["claim_ids"] = ["a"]
        if fault == "unknown": value["topics"][0]["claim_ids"].append("invented")
        elif fault == "wrong_origin": value["topics"][0]["claim_ids"].append("p")
        else: value["topics"].append(deepcopy(value["topics"][0]))
        return value

    with pytest.raises(ValueError):
        run(tmp_path, ledger, FakeModel(malformed), topic_missing_policy="review_queue")
    assert not list(tmp_path.glob("*.json"))
