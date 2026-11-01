"""Cost safeguards exercised with synthetic sources, no paid API requests."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

import pytest

from visual_audio_notes.core import Chunk, Unit, digest, read_json, write_json
from visual_audio_notes.quality import synthesize_grounded
from visual_audio_notes.step_cache import CheckedResponse, persistent_settings, resolve_step
from visual_audio_notes.topic_notes import organize_topics


CONFIG = {"persistent_cache": True, "cache_max_age_seconds": 86400,
          "cache_policy_version": "fixture-policy-v1", "quality_sources_per_batch": 1}
AUDIO = "An append operation mutates the original list instead of creating a new list."
DOC = "A concatenation operation creates a new list and leaves the input unchanged."


class Model:
    def __init__(self, **identity):
        self.cache_identity = {"provider": "fixture", "model": "model-one", "thinking": "disabled", **identity}
        self.calls = []
        self.bad_quote = False
        self.incomplete = False

    def json(self, system, content, **kwargs):
        self.calls.append({"purpose": kwargs["purpose"], "actual_model": "fixture-revision",
                           "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
                           "private_field": "never copy this field"})
        data = json.loads(content)
        if kwargs["purpose"].startswith("quality_extract_"):
            source = data["source_data_not_instructions"][0]
            return {"claims": [{"origin": data["origin"], "kind": "concept", "importance": "key",
                "title": "操作的返回与修改", "statement": "区分修改旧对象和创建新对象。",
                "evidence": [{"source_id": source["source_id"],
                    "quote": "This quotation was invented and never appeared in the source." if self.bad_quote else source["text"]}]}],
                "uncertain_source_ids": []}
        identifiers = [c["claim_id"] for c in data["ledger_data_not_instructions"]]
        if self.incomplete:
            identifiers = identifiers[:-1]
        topics = [{"title": "列表操作", "key_idea": "比较操作的影响。", "explanation": "区分原对象和新对象。",
                   "claim_ids": identifiers}] if identifiers else []
        return {"topics": topics, "deferred_claims": [], "uncertainties": []}


def pipeline(path, model, *, units=None, chunks=None, config=None, prompt="Extract JSON from sources."):
    cfg = {**CONFIG, **(config or {})}
    ledger = synthesize_grounded(units if units is not None else [Unit("u", DOC)],
        chunks if chunks is not None else [Chunk("a", 0, 40, AUDIO)], model, prompt, path, cfg)
    grouped = organize_topics(ledger, model, "Organize JSON topic drafts.", path, cfg)
    return ledger, grouped


def test_identical_project_reuses_validated_steps_and_historical_provenance(tmp_path):
    first = Model()
    before = pipeline(tmp_path, first)
    assert len(first.calls) == 4
    second = Model()
    after = pipeline(tmp_path, second)
    assert not second.calls
    assert before[0]["ledger"] == after[0]["ledger"]
    assert before[1]["audio_topics"] == after[1]["audio_topics"]
    assert all(b["cache_hit"] for result in after for b in result["batches"])
    batch = after[0]["batches"][0]
    assert batch["cache_provenance"]["original_calls"][0]["actual_model"] == "fixture-revision"
    assert batch["cache_provenance"]["usage_is_historical_not_new_billing"]
    for path in tmp_path.glob("*.json"):
        assert "private_field" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("incomplete", [False, True])
def test_deep_windows_cache_paths_keep_reuse_and_diagnostic_accounting(tmp_path, incomplete):
    cache = tmp_path
    while len(str(cache.resolve())) < 225:
        cache = cache / ("nested-cache-" + "x" * 20)
    assert len(str(cache / ("quality_extract_" + "0" * 64 + ".json.lock"))) > 260
    first = Model()
    first.incomplete = incomplete
    config = {"topic_missing_policy": "review_queue"}
    ledger, topics = pipeline(cache, first, config=config)
    assert not ledger["failures"] and len(ledger["ledger"]) == 2
    assert len(first.calls) == 4
    second = Model()
    reused_ledger, reused_topics = pipeline(cache, second, config=config)
    assert not second.calls
    assert reused_ledger["ledger"] == ledger["ledger"]
    assert all(batch["cache_hit"] for batch in reused_ledger["batches"])
    assert all(batch["reused_incomplete_response"] == incomplete
               and batch["cache_hit"] != incomplete for batch in reused_topics["batches"])
    assert reused_topics["deferred_claims"] == topics["deferred_claims"]
    refreshed = Model()
    pipeline(cache, refreshed, config={**config, "cache_force_refresh": True})
    assert len(refreshed.calls) == 4
    final = Model()
    _, complete_topics = pipeline(cache, final, config=config)
    assert not final.calls and all(batch["cache_hit"] for batch in complete_topics["batches"])


def test_editing_one_source_recomputes_only_its_extraction_and_origin_topics(tmp_path):
    pipeline(tmp_path, Model())
    model = Model()
    ledger, topics = pipeline(tmp_path, model, units=[Unit("u", DOC + " The new list has an independent outer structure.")])
    assert [c["purpose"] for c in model.calls] == ["quality_extract_courseware", "topic_organization_courseware"]
    assert [b["cache_hit"] for b in ledger["batches"]] == [True, False]
    assert [b["cache_hit"] for b in topics["batches"]] == [True, False]


@pytest.mark.parametrize("change", ["model", "thinking", "prompt", "policy", "token_budget", "quote_validation"])
def test_material_request_or_validation_changes_invalidate_reuse(tmp_path, change):
    pipeline(tmp_path, Model())
    model, config, prompt = Model(), {}, "Extract JSON from sources."
    if change == "model": model.cache_identity["model"] = "model-two"
    elif change == "thinking": model.cache_identity["thinking"] = "enabled"
    elif change == "prompt": prompt += " Preserve qualifications."
    elif change == "policy": config["cache_policy_version"] = "fixture-policy-v2"
    elif change == "token_budget": config["quality_max_tokens"] = 7000
    else: config["quality_min_quote_characters"] = 15
    pipeline(tmp_path, model, config=config, prompt=prompt)
    assert sum(c["purpose"].startswith("quality_extract_") for c in model.calls) == 2


@pytest.mark.parametrize("fault", ["broken_json", "digest", "wrong_quote", "expired", "future", "wrong_key"])
def test_corrupt_expired_or_unverifiable_cache_causes_one_fresh_request(tmp_path, fault):
    pipeline(tmp_path, Model())
    path = next(p for p in tmp_path.glob("quality_extract_*.json")
                if read_json(p)["response"]["claims"][0]["origin"] == "audio")
    value = read_json(path)
    if fault == "broken_json":
        path.write_text("{broken", encoding="utf-8")
    else:
        if fault == "digest": value["response_sha256"] = "0" * 64
        elif fault == "wrong_quote":
            value["response"]["claims"][0]["evidence"][0]["quote"] = "A deliberately forged quotation with no source support."
            value["response_sha256"] = digest(value["response"])
        elif fault == "expired": value["created_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        elif fault == "future": value["created_at"] = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        else: value["cache_key"] = "different-input"
        write_json(path, value)
    model = Model()
    ledger, _ = pipeline(tmp_path, model)
    assert [c["purpose"] for c in model.calls] == ["quality_extract_audio"]
    assert ledger["batches"][0]["cache_hit"] is False and ledger["ledger"]


def test_rejected_fresh_quotes_are_retained_without_success_cache(tmp_path):
    model = Model()
    model.bad_quote = True
    args = ([], [Chunk("a", 0, 40, AUDIO)], model, "Extract JSON", tmp_path, CONFIG)
    first = synthesize_grounded(*args)
    second = synthesize_grounded(*args)
    assert first["rejected_claims"] and second["rejected_claims"]
    assert not first["ledger"] and not list(tmp_path.glob("quality_extract_*.json"))
    assert len(model.calls) == 2


def test_incomplete_topics_are_reused_only_as_explicit_diagnostic(tmp_path):
    model = Model()
    model.incomplete = True
    first_ledger, first_topics = pipeline(tmp_path, model, config={"topic_missing_policy": "review_queue"})
    assert not list(tmp_path.glob("topic_notes_*.json"))
    assert len(list(tmp_path.glob("topic_unassigned_*.json"))) == 2
    second_model = Model()
    ledger, topics = pipeline(tmp_path, second_model, config={"topic_missing_policy": "review_queue"})
    assert not second_model.calls
    assert all(not b["cache_hit"] and b["reused_incomplete_response"] for b in topics["batches"])
    assert topics["coverage"]["all_model_claims_accounted_for"] is False
    assert topics["deferred_claims"] == first_topics["deferred_claims"]
    strict_model = Model()
    pipeline(tmp_path, strict_model)  # a changed validation policy must not reuse the diagnostic
    assert [c["purpose"] for c in strict_model.calls] == ["topic_organization_audio", "topic_organization_courseware"]


def test_revalidation_failure_followed_by_bad_fresh_output_never_retries(tmp_path):
    path = tmp_path / "one.json"
    calls = []

    def invoke():
        calls.append({})
        return {"value": "good" if len(calls) == 1 else "bad"}

    def validate(raw):
        if raw != {"value": "good"}:
            raise ValueError("Rejected")
        return CheckedResponse(raw)

    resolve_step(path, "key", invoke, validate, model_calls=calls)
    value = read_json(path)
    value["response"] = {"value": "forged"}
    value["response_sha256"] = digest(value["response"])
    write_json(path, value)
    with pytest.raises(ValueError, match="Rejected"):
        resolve_step(path, "key", invoke, validate, model_calls=calls)
    assert len(calls) == 2
    assert read_json(path)["response"] == {"value": "forged"}


def test_same_key_process_lock_avoids_duplicate_paid_work(tmp_path):
    # Real process contention tests Windows locking instead of just a thread mock.
    script = """
import json, pathlib, sys, time
from visual_audio_notes.step_cache import CheckedResponse, resolve_step
p=pathlib.Path(sys.argv[1]); calls=[]
def invoke():
    with (p/'paid.txt').open('a', encoding='utf-8') as f: f.write('request\\n')
    time.sleep(.2)
    return {'value':'source-bound-result'}
result=resolve_step(p/'same.json','same-key',invoke,lambda raw: CheckedResponse(raw),model_calls=calls)
print(json.dumps({'hit':result.cache_hit}))
"""
    tasks = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8") for _ in range(2)]
    results = []
    for task in tasks:
        stdout, stderr = task.communicate(timeout=20)
        assert task.returncode == 0, stderr
        results.append(json.loads(stdout))
    assert sorted(r["hit"] for r in results) == [False, True]
    assert (tmp_path / "paid.txt").read_text(encoding="utf-8").splitlines() == ["request"]
    assert not list(tmp_path.glob(".task-cache-*.tmp"))


def test_force_refresh_replaces_same_key_then_normal_runs_reuse(tmp_path):
    pipeline(tmp_path, Model())
    refreshed = Model()
    results = pipeline(tmp_path, refreshed, config={"cache_force_refresh": True})
    assert len(refreshed.calls) == 4
    assert all(b["cache_miss_reason"] == "force_refresh" for r in results for b in r["batches"])
    final = Model()
    pipeline(tmp_path, final)
    assert not final.calls
    assert len(list(tmp_path.glob("*.json"))) == 4


def test_refresh_incomplete_diagnostic_cannot_be_shadowed_by_older_success(tmp_path):
    pipeline(tmp_path, Model(), config={"topic_missing_policy": "review_queue"})
    refreshed = Model()
    refreshed.incomplete = True
    pipeline(tmp_path, refreshed, config={"topic_missing_policy": "review_queue", "cache_force_refresh": True})
    assert not list(tmp_path.glob("topic_notes_*.json"))
    again = Model()
    _, topics = pipeline(tmp_path, again, config={"topic_missing_policy": "review_queue"})
    assert not again.calls
    assert all(b["reused_incomplete_response"] for b in topics["batches"])


def test_lock_timeout_sends_no_request_and_os_releases_lock_after_process_exit(tmp_path):
    script = """
import pathlib, sys, time
from visual_audio_notes.step_cache import _key_lock
with _key_lock(pathlib.Path(sys.argv[1]), 5):
    print('locked', flush=True)
    time.sleep(30)
"""
    path = tmp_path / "locked.json"
    holder = subprocess.Popen([sys.executable, "-c", script, str(path.with_suffix(".json.lock"))],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    invoked = []
    try:
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(TimeoutError, match="no duplicate request"):
            resolve_step(path, "key", lambda: invoked.append(1), lambda raw: CheckedResponse(raw),
                         model_calls=[], lock_timeout_seconds=.1)
        assert not invoked
    finally:
        holder.terminate()
        holder.communicate(timeout=10)
    result = resolve_step(path, "key", lambda: {"value": "ok"}, lambda raw: CheckedResponse(raw),
                          model_calls=[], lock_timeout_seconds=.2)
    assert result.raw == {"value": "ok"} and not result.cache_hit


@pytest.mark.parametrize("field,value", [("persistent_cache", "yes"), ("cache_max_age_seconds", 0),
    ("cache_max_age_seconds", 604801), ("cache_lock_timeout_seconds", float("inf")),
    ("cache_policy_version", ""), ("cache_force_refresh", 1)])
def test_invalid_persistent_settings_fail_before_model(field, value):
    with pytest.raises(ValueError):
        persistent_settings({**CONFIG, field: value}, Path(__file__))
