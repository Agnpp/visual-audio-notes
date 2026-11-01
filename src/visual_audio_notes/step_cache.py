"""Project-scoped, revalidated task results; separate from provider KV caches.

Only opt-in product workflows use this format. A lock covers the read, paid
request and validation, so a concurrent identical task does not pay twice.
Cache reuse proves the declared mechanical checks only, never factual quality.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import json
import math
import os
from pathlib import Path
import tempfile
import time
from typing import Callable

from .core import digest, file_hash


FORMAT = "validated-task-cache-v1"


@dataclass
class CheckedResponse:
    value: object
    reusable: bool = True
    complete: bool = True


@dataclass
class StepResponse:
    raw: dict
    checked: object
    cache_hit: bool
    reused_incomplete: bool
    provenance: dict
    cache_miss_reason: str | None = None


def persistent_settings(config: dict, validator_file: Path) -> dict | None:
    enabled = config.get("persistent_cache", False)
    if type(enabled) is not bool:
        raise ValueError("persistent_cache must be boolean")
    if not enabled:
        return None
    age = config.get("cache_max_age_seconds", 86400)
    wait = config.get("cache_lock_timeout_seconds", 30)
    refresh = config.get("cache_force_refresh", False)
    if type(refresh) is not bool:
        raise ValueError("cache_force_refresh must be boolean")
    _positive_seconds(age, "Cache maximum age", 604800)
    _positive_seconds(wait, "Cache lock timeout", 3600)
    policy = config.get("cache_policy_version", "product-task-cache-v1")
    if not isinstance(policy, str) or not policy.strip() or len(policy) > 100:
        raise ValueError("Invalid cache policy version")
    return {"identity": {"format": FORMAT, "policy": policy,
                         "validator_sha256": file_hash(validator_file),
                         "cache_implementation_sha256": file_hash(Path(__file__))},
            "max_age_seconds": age, "lock_timeout_seconds": wait, "force_refresh": refresh}


def _positive_seconds(value, label, maximum):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 < value <= maximum):
        raise ValueError(label + " is outside the permitted range")


@contextmanager
def _key_lock(path: Path, timeout: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        started = time.monotonic()
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() - started >= timeout:
                    raise TimeoutError("An identical task is still running; no duplicate request was sent") from None
                time.sleep(min(.05, max(.001, timeout - (time.monotonic() - started))))
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".task-cache-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, allow_nan=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _provenance(calls: list[dict]) -> dict:
    """Provider adapters already redact credentials; never copy arbitrary fields."""
    kept = []
    for call in calls:
        item = {}
        for name in ("actual_model", "requested_model", "response_id", "request_sha256"):
            value = call.get(name)
            if isinstance(value, str) and len(value) <= 200 and all(ord(c) >= 32 for c in value):
                item[name] = value
        usage = call.get("usage")
        if isinstance(usage, dict):
            item["usage"] = {name: usage[name] for name in (
                "prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens",
                "prompt_cache_miss_tokens") if type(usage.get(name)) is int and usage[name] >= 0}
        if item:
            kept.append(item)
    return {"original_calls": kept, "usage_is_historical_not_new_billing": True}


def _read(path: Path, key: str, validate: Callable, max_age: float, *, complete: bool):
    if not path.exists():
        return None, "missing"
    try:
        if path.stat().st_size > 16 * 1024 * 1024:
            return None, "invalid"
        envelope = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(envelope, dict) or envelope.get("format") != FORMAT
                or envelope.get("cache_key") != key or envelope.get("complete") is not complete):
            return None, "invalid"
        created = datetime.fromisoformat(envelope["created_at"])
        if created.tzinfo is None:
            return None, "invalid"
        age = (datetime.now(timezone.utc) - created).total_seconds()
        if age < 0 or age > max_age:
            return None, "expired"
        raw = envelope["response"]
        if not isinstance(raw, dict) or digest(raw) != envelope.get("response_sha256"):
            return None, "invalid"
        provenance = envelope.get("provenance")
        if not isinstance(provenance, dict):
            return None, "invalid"
        # Re-filter persisted metadata rather than trusting arbitrary disk fields.
        original_calls = provenance.get("original_calls")
        if not isinstance(original_calls, list) or any(not isinstance(c, dict) for c in original_calls):
            return None, "invalid"
        checked = validate(raw)
        if not isinstance(checked, CheckedResponse) or not checked.reusable or checked.complete is not complete:
            return None, "validation_failed"
        return StepResponse(raw, checked.value, complete, not complete,
                            {**_provenance(original_calls), "created_at": envelope["created_at"],
                             "response_sha256": envelope["response_sha256"]}), None
    except (ValueError, TypeError, KeyError, OSError, OverflowError, RecursionError):
        return None, "invalid"


def resolve_step(path: Path, key: str, invoke: Callable[[], dict],
                 validate: Callable[[dict], CheckedResponse], *, model_calls: list[dict],
                 max_age_seconds: float = 86400, lock_timeout_seconds: float = 30,
                 diagnostic_path: Path | None = None, force_refresh: bool = False) -> StepResponse:
    """One read or one fresh invocation, never a paid automatic retry.

    A reusable incomplete result is kept only at diagnostic_path and never
    reported as a successful cache hit. Invalid fresh responses are not written.
    """
    _positive_seconds(max_age_seconds, "Cache maximum age", 604800)
    _positive_seconds(lock_timeout_seconds, "Cache lock timeout", 3600)
    if type(force_refresh) is not bool:
        raise ValueError("cache_force_refresh must be boolean")
    with _key_lock(path.with_suffix(path.suffix + ".lock"), lock_timeout_seconds):
        result, reason = (None, "force_refresh") if force_refresh else _read(
            path, key, validate, max_age_seconds, complete=True)
        if result is not None:
            return result
        if diagnostic_path is not None and not force_refresh:
            result, diagnostic_reason = _read(diagnostic_path, key, validate, max_age_seconds, complete=False)
            if result is not None:
                return result
            if reason == "missing" and diagnostic_reason != "missing":
                reason = diagnostic_reason
        start = len(model_calls)
        raw = invoke()
        checked = validate(raw)
        if not isinstance(raw, dict) or not isinstance(checked, CheckedResponse):
            raise ValueError("Task cache requires a validated response object")
        provenance = _provenance(model_calls[start:])
        if checked.reusable:
            destination = path if checked.complete else diagnostic_path
            if destination is not None:
                created = datetime.now(timezone.utc).isoformat()
                response_sha = digest(raw)
                _atomic_json(destination, {"format": FORMAT, "cache_key": key,
                    "created_at": created, "complete": checked.complete,
                    "response_sha256": response_sha, "response": raw, "provenance": provenance})
                # A newly validated diagnostic must not be shadowed by an older
                # complete result after an explicit refresh (or vice versa).
                if checked.complete and diagnostic_path is not None:
                    diagnostic_path.unlink(missing_ok=True)
                elif not checked.complete:
                    path.unlink(missing_ok=True)
                provenance = {**provenance, "created_at": created, "response_sha256": response_sha}
        return StepResponse(raw, checked.value, False, False, provenance, reason)
