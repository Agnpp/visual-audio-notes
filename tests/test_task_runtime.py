import json

import pytest

from visual_audio_notes.core import read_json
from visual_audio_notes.task_runtime import TaskBudgetExceeded, TaskCancelled, TaskModel, TaskRequestFailed, usage_report


class Model:
    cache_identity = {"provider": "fixture", "model": "flash-test", "thinking": "disabled"}

    def __init__(self):
        self.calls = []
        self.fail = False

    def json(self, *args, **kwargs):
        self.calls.append({"purpose": kwargs["purpose"], "status": "invalid_response" if self.fail else "validated",
                           "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                                     "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20},
                           "raw_response": "PRIVATE_RESPONSE", "prompt": "PRIVATE_PROMPT"})
        if self.fail:
            raise ValueError("Fixture failure")
        return {"ok": True}


def test_budget_stops_before_request_and_journal_keeps_actual_failed_usage(tmp_path):
    provider = Model()
    runtime = TaskModel(provider, tmp_path / "runtime.json", max_calls=2, max_requested_output_tokens=400)
    runtime.stage("evidence", "running")
    runtime.json("PRIVATE_SYSTEM", "PRIVATE_TRANSCRIPT", max_tokens=200, purpose="quality_extract_audio")
    provider.fail = True
    with pytest.raises(ValueError):
        runtime.json("PRIVATE_SYSTEM", "PRIVATE_TRANSCRIPT", max_tokens=200, purpose="quality_extract_audio")
    with pytest.raises(TaskBudgetExceeded):
        runtime.json("PRIVATE_SYSTEM", "PRIVATE_TRANSCRIPT", max_tokens=1)
    assert len(provider.calls) == 2
    runtime.finish("budget_stopped")
    saved = read_json(tmp_path / "runtime.json")
    assert saved["state"] == "budget_stopped"
    assert saved["reserved_output_tokens"] == 400
    assert saved["calls"][-1]["status"] == "invalid_response"
    assert saved["calls"][-1]["usage"]["completion_tokens"] == 20
    assert "PRIVATE_" not in json.dumps(saved)


def test_output_reservation_and_input_guard_do_not_truncate_or_call(tmp_path):
    provider = Model()
    runtime = TaskModel(provider, tmp_path / "runtime.json", max_requested_output_tokens=100, max_input_characters=50)
    with pytest.raises(TaskBudgetExceeded, match="output ceiling"):
        runtime.json("system", "data", max_tokens=101)
    with pytest.raises(TaskBudgetExceeded, match="input ceiling"):
        runtime.json("system", "x" * 51, max_tokens=50)
    assert not provider.calls and not runtime.calls
    assert read_json(runtime.journal)["reserved_output_tokens"] == 0


def test_interrupted_request_stays_unknown_in_journal(tmp_path):
    class Interrupted(Model):
        def json(self, *args, **kwargs):
            raise KeyboardInterrupt()

    runtime = TaskModel(Interrupted(), tmp_path / "runtime.json")
    with pytest.raises(KeyboardInterrupt):
        runtime.json("system", "data")
    assert read_json(runtime.journal)["calls"][0]["status"] == "failed_usage_unknown"
    report = usage_report(runtime.calls)
    assert report["reported_token_totals"]["prompt_tokens"] is None
    assert report["remote_input_cache"]["hit_rate"] is None


def test_usage_report_keeps_partial_unknown_and_failed_attempts_separate():
    calls = [
        {"purpose": "extract", "status": "validated", "usage": {"prompt_tokens": 100,
         "completion_tokens": 20, "total_tokens": 120, "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20}},
        {"purpose": "topics", "status": "invalid_response", "usage": {"prompt_tokens": 200,
         "completion_tokens": 40, "total_tokens": 240, "prompt_cache_hit_tokens": 100, "prompt_cache_miss_tokens": 100}},
        {"purpose": "topics", "status": "connection_error", "usage": {}},
    ]
    report = usage_report(calls, [{"cache_hit": True, "status": "extracted"},
                                {"cache_hit": False, "reused_incomplete_response": True}])
    assert report["api_attempts"] == 3
    assert report["reported_token_totals"]["completion_tokens"] == 60
    assert report["remote_input_cache"]["hit_rate"] == .6
    assert report["remote_input_cache"]["calls_without_consistent_cache_usage"] == 1
    assert report["usage_complete"] is False
    assert report["currency_cost"] is None
    assert report["local_result_cache"] == {"complete_reuses": 1, "incomplete_reuses": 1}


def test_inconsistent_or_boolean_usage_does_not_invent_hit_rate():
    report = usage_report([{"usage": {"prompt_tokens": 100, "prompt_cache_hit_tokens": 80,
                                      "prompt_cache_miss_tokens": 90, "completion_tokens": True}}])
    assert report["remote_input_cache"]["hit_rate"] is None
    assert report["reported_token_totals"]["completion_tokens"] is None
    assert usage_report([])["reported_token_totals"]["prompt_tokens"] is None
    assert usage_report([{"purpose": "topics", "usage": {}}])["by_purpose"]["topics"]["reported_prompt_tokens"] is None


def test_audio_usage_keeps_submitted_and_provider_duration_separate_including_failed_calls():
    report = usage_report([
        {"purpose": "speech_to_text", "status": "validated", "audio_seconds": 30,
         "provider_audio_usage": {"type": "duration", "seconds": 31}},
        {"purpose": "speech_to_text", "status": "invalid_response", "audio_seconds": 10,
         "usage": {"total_tokens": 50},
         "provider_audio_usage": {"type": "tokens", "input_tokens": 42, "output_tokens": 8, "total_tokens": 50}},
        {"purpose": "speech_to_text", "status": "failed_usage_unknown", "audio_seconds": 5},
        {"purpose": "topics", "usage": {"prompt_tokens": 100, "total_tokens": 120}},
    ])
    audio = report["audio_usage"]
    assert audio["api_attempts"] == 3
    assert audio["submitted_audio_seconds"] == 45
    assert audio["provider_reported_totals"] == {"seconds": 31, "input_tokens": 42,
                                                  "output_tokens": 8, "total_tokens": 50}
    assert audio["calls_reporting_each_field"] == dict.fromkeys(audio["provider_reported_totals"], 1)
    assert report["reported_token_totals"]["total_tokens"] == 120
    assert report["remote_input_cache"]["hit_rate"] is None
    assert report["currency_cost"] is None


def test_invalid_or_missing_audio_usage_does_not_become_zero_or_text_tokens():
    audio = usage_report([{"purpose": "speech_to_text", "audio_seconds": True,
                           "provider_audio_usage": {"type": "duration", "seconds": float("nan"),
                                                    "input_tokens": -1, "output_tokens": True}}])["audio_usage"]
    assert audio["submitted_audio_seconds"] is None
    assert all(value is None for value in audio["provider_reported_totals"].values())
    assert all(value == 0 for value in audio["calls_reporting_each_field"].values())
    assert usage_report([])["audio_usage"]["provider_reported_totals"]["seconds"] is None


def test_cancel_stops_next_request_but_retains_returned_usage(tmp_path):
    from threading import Event

    stop = Event()
    provider = Model()
    runtime = TaskModel(provider, tmp_path / "runtime.json", cancel_event=stop)
    runtime.json("system", "data", purpose="extract")
    stop.set()
    with pytest.raises(TaskCancelled):
        runtime.json("system", "more data", purpose="topics")
    runtime.finish("cancelled")
    saved = read_json(runtime.journal)
    assert saved["state"] == "cancelled" and len(provider.calls) == len(saved["calls"]) == 1
    assert usage_report(saved["calls"])["reported_token_totals"]["total_tokens"] == 120


def test_product_failure_policy_escapes_batch_recovery_and_retains_billable_usage(tmp_path):
    provider = Model()
    provider.fail = True
    runtime = TaskModel(provider, tmp_path / "runtime.json", stop_on_request_error=True)
    with pytest.raises(TaskRequestFailed):
        runtime.json("system", "data", purpose="quality_extract_audio")
    assert len(provider.calls) == 1
    assert runtime.calls[0]["status"] == "invalid_response"
    assert runtime.calls[0]["usage"]["completion_tokens"] == 20


@pytest.mark.parametrize("kwargs", [{"max_calls": 0}, {"max_calls": True},
                                   {"max_requested_output_tokens": -1}, {"max_input_characters": 0}])
def test_invalid_limits_do_not_create_journal(tmp_path, kwargs):
    with pytest.raises(ValueError):
        TaskModel(Model(), tmp_path / "runtime.json", **kwargs)
    assert not (tmp_path / "runtime.json").exists()
