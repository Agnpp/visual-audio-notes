"""Bounded task execution around a selected model, without model-driven loops.

The journal survives a provider or later validation failure. It contains request
hashes and provider usage, never prompts, credentials, or raw responses. Output
ceilings reserve requested maximums; they are not a price or actual token bill.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import os
import tempfile
import time

from .core import digest


class TaskBudgetExceeded(RuntimeError):
    """Stop before another request; already completed work stays available."""


class TaskRequestFailed(RuntimeError):
    """Stop the product task after a failed request, without paid repair loops."""

    def __init__(self, message, *, response_error=None):
        super().__init__(message)
        self.response_error = response_error


class TaskCancelled(RuntimeError):
    """Cooperative stop before the next model call; active calls may be billed."""


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".task-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _limit(value, name, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"Invalid {name}")
    return value


def _call_metadata(call: dict) -> dict:
    result = {key: call[key] for key in
              ("purpose", "status", "request_sha256", "requested_model", "actual_model",
               "response_id", "finish_reason", "response_error", "reasoning_tokens", "http_status", "seconds", "system_prefix_sha256")
              if isinstance(call.get(key), (str, int, float)) and not isinstance(call.get(key), bool)}
    result["usage"] = {key: value for key, value in call.get("usage", {}).items()
                       if key in {"prompt_tokens", "completion_tokens", "total_tokens",
                                  "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"}
                       and type(value) is int and value >= 0} if isinstance(call.get("usage"), dict) else {}
    if _nonnegative_number(call.get("audio_seconds")):
        result["audio_seconds"] = call["audio_seconds"]
    audio = _provider_audio_usage(call)
    if audio:
        result["provider_audio_usage"] = audio
    return result


def _nonnegative_number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _provider_audio_usage(call):
    raw = call.get("provider_audio_usage")
    if not isinstance(raw, dict):
        return {}
    result = {key: value for key, value in raw.items()
              if key in {"seconds", "input_tokens", "output_tokens", "total_tokens"}
              and _nonnegative_number(value)}
    if raw.get("type") in {"duration", "tokens"}:
        result["type"] = raw["type"]
    return result


def _audio_usage_report(calls):
    speech = [call for call in calls if call.get("purpose") == "speech_to_text"]
    submitted = [call["audio_seconds"] for call in speech
                 if _nonnegative_number(call.get("audio_seconds"))]
    fields = ("seconds", "input_tokens", "output_tokens", "total_tokens")
    observed = [_provider_audio_usage(call) for call in speech]
    values = {key: [usage[key] for usage in observed if key in usage] for key in fields}
    return {
        "api_attempts": len(speech),
        "submitted_audio_seconds": sum(submitted) if submitted else None,
        "calls_with_submitted_duration": len(submitted),
        "provider_reported_totals": {key: sum(items) if items else None
                                     for key, items in values.items()},
        "calls_reporting_each_field": {key: len(items) for key, items in values.items()},
        "notice": "Submitted duration is decoded input, not billed duration. Provider audio usage is separate from text/cache tokens. Unknown is not zero; failed attempts remain included.",
    }


class TaskModel:
    """One request per step; no retries, reviewer agents, or provider fallback."""

    def __init__(self, model, journal: Path, *, max_calls=32,
                 max_requested_output_tokens=128000, max_input_characters=300000,
                 stop_on_request_error=False, cancel_event=None):
        self.model = model
        self.cache_identity = deepcopy(model.cache_identity)
        self.calls: list[dict] = []
        self.journal = journal
        self.max_calls = _limit(max_calls, "request ceiling", 10000)
        self.max_requested_output_tokens = _limit(max_requested_output_tokens, "output ceiling", 10000000)
        self.max_input_characters = _limit(max_input_characters, "input ceiling", 10000000)
        if type(stop_on_request_error) is not bool:
            raise ValueError("Invalid request failure policy")
        self.stop_on_request_error = stop_on_request_error
        self.cancel_event = cancel_event
        self.reserved_output_tokens = 0
        self.stages: list[dict] = []
        self.state = "prepared"
        self._save()

    def _save(self):
        atomic_json(self.journal, {
            "version": "bounded-task-runtime-v1", "state": self.state,
            "model": self.cache_identity, "stages": self.stages, "calls": self.calls,
            "limits": {"max_calls": self.max_calls,
                       "max_requested_output_tokens": self.max_requested_output_tokens,
                       "max_input_characters": self.max_input_characters,
                       "stop_on_request_error": self.stop_on_request_error},
            "reserved_output_tokens": self.reserved_output_tokens,
            "notice": "Ceilings bound attempts and requested maximum output; not actual charges. No automatic retries.",
        })

    def stage(self, name: str, state: str):
        self.check_cancelled()
        if name not in {"candidates", "evidence", "topics"} or state not in {"running", "complete"}:
            raise ValueError("Invalid task stage")
        self.state = "running"
        self.stages.append({"stage": name, "state": state})
        self._save()

    def finish(self, state="complete"):
        if state not in {"complete", "failed", "budget_stopped", "interrupted", "cancelled"}:
            raise ValueError("Invalid final task state")
        self.state = state
        self._save()

    def json(self, system, content, max_tokens=2000, purpose="unspecified", schema=None):
        self.check_cancelled()
        _limit(max_tokens, "step output ceiling", 393216)
        if not isinstance(system, str) or not isinstance(content, str):
            raise ValueError("Task model inputs must be text")
        if len(system) + len(content) + len(json.dumps(schema, ensure_ascii=False)) > self.max_input_characters:
            raise TaskBudgetExceeded("Step exceeds the input ceiling; sources were not truncated")
        if (len(self.calls) >= self.max_calls
                or self.reserved_output_tokens + max_tokens > self.max_requested_output_tokens):
            raise TaskBudgetExceeded("Task request/output ceiling reached; completed steps were preserved")
        self.reserved_output_tokens += max_tokens
        call = {"purpose": purpose, "status": "requesting", "usage": {},
                "request_sha256": digest({"system": system, "content": content, "schema": schema,
                                          "max_tokens": max_tokens, "model": self.cache_identity}),
                "requested_max_tokens": max_tokens}
        self.calls.append(call)
        self._save()  # An interrupted in-flight request can still have incurred usage.
        before = len(self.model.calls)
        started = time.perf_counter()
        try:
            result = self.model.json(system, content, max_tokens=max_tokens, purpose=purpose, schema=schema)
            call["status"] = "returned"
            return result
        except BaseException as error:
            call["status"] = "failed_usage_unknown"
            if self.stop_on_request_error and isinstance(error, Exception):
                observed = self.model.calls[before:]
                response_error = observed[-1].get("response_error") if observed else None
                raise TaskRequestFailed("Model request failed; task stopped and completed steps were preserved. See task usage/status.",
                                        response_error=response_error) from None
            raise
        finally:
            observed = self.model.calls[before:]
            if observed:
                call.update(_call_metadata(observed[-1]))
            call["seconds"] = round(time.perf_counter() - started, 3)
            self._save()

    def check_cancelled(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise TaskCancelled("Task stopped; completed steps are retained. Active requests cannot be revoked.")


def usage_report(calls: list[dict], batches: list[dict] | None = None) -> dict:
    """Report paid attempts separately from local reuse and incomplete usage."""
    names = ("prompt_tokens", "completion_tokens", "total_tokens",
             "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
    totals = {key: 0 for key in names}
    reported = {key: 0 for key in names}
    matched_hit = matched_miss = matched_prompt = matched_calls = 0
    per_purpose = {}
    for call in calls:
        purpose = call.get("purpose", "unspecified")
        per_purpose.setdefault(purpose, []).append(call)
        # Speech providers may also name a field total_tokens. Keep their
        # audio accounting separate from text/cache usage and pricing bases.
        usage = call.get("usage") if isinstance(call.get("usage"), dict) and purpose != "speech_to_text" else {}
        for key in names:
            value = usage.get(key)
            if type(value) is int and value >= 0:
                totals[key] += value
                reported[key] += 1
        hit, miss, prompt = (usage.get(key) for key in
                            ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "prompt_tokens"))
        if all(type(v) is int and v >= 0 for v in (hit, miss, prompt)) and hit + miss == prompt:
            matched_hit += hit
            matched_miss += miss
            matched_prompt += prompt
            matched_calls += 1
    batches = batches or []
    local_complete = sum(bool(b.get("cache_hit")) and b.get("status", "extracted") == "extracted"
                         and b.get("cacheable", True) and not b.get("rejected_claims", 0)
                         and not b.get("reused_incomplete_response", False) for b in batches)
    local_incomplete = sum(bool(b.get("reused_incomplete_response")) for b in batches)
    result = {
        "version": "task-usage-v1", "api_attempts": len(calls),
        "reported_token_totals": {key: totals[key] if reported[key] else None for key in names},
        "calls_reporting_each_token_field": reported,
        "usage_complete": bool(calls) and all(reported[key] == len(calls) for key in names),
        "remote_input_cache": {"hit_tokens": matched_hit if matched_calls else None,
                               "miss_tokens": matched_miss if matched_calls else None,
                               "hit_rate": matched_hit / matched_prompt if matched_prompt else None,
                               "calls_with_consistent_cache_usage": matched_calls,
                               "calls_without_consistent_cache_usage": len(calls) - matched_calls},
        "local_result_cache": {"complete_reuses": local_complete, "incomplete_reuses": local_incomplete},
        "audio_usage": _audio_usage_report(calls),
        "currency_cost": None,
        "notice": "Unknown usage is not zero. Includes failed attempts with reported usage. Local reuse makes no API call; server cache still generates output. No price inferred.",
    }
    result["by_purpose"] = {}
    for purpose, group in per_purpose.items():
        values = [c["usage"]["prompt_tokens"] for c in group if isinstance(c.get("usage"), dict)
                  and type(c["usage"].get("prompt_tokens")) is int and c["usage"]["prompt_tokens"] >= 0]
        result["by_purpose"][purpose] = {
            "attempts": len(group), "statuses": [c.get("status", "unknown") for c in group],
            "reported_prompt_tokens": sum(values) if values else None,
            "calls_without_prompt_usage": len(group) - len(values),
        }
    return result
