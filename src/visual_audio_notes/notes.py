from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import time
import urllib.error
import urllib.request

from .core import Chunk, Unit, digest, read_json, write_json

FIELDS = ("title", "key_idea", "explanation", "example", "pitfall", "limitation")


def notes_schema(allowed_ids: set[str], max_topics: int) -> dict:
    topic_properties = {field: {"type": "string"} for field in FIELDS}
    topic_properties["evidence_ids"] = {
        "type": "array", "minItems": 1, "maxItems": 8,
        "items": {"type": "string", "enum": sorted(allowed_ids)},
    }
    return {
        "type": "object", "additionalProperties": False,
        "required": ["overview", "topics", "review_questions", "uncertainties"],
        "properties": {
            "overview": {"type": "string"},
            "topics": {
                "type": "array", "minItems": 1, "maxItems": max_topics,
                "items": {"type": "object", "additionalProperties": False,
                         "properties": topic_properties,
                         "required": [*FIELDS, "evidence_ids"]},
            },
            "review_questions": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
            "uncertainties": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
        },
    }


class LocalModel:
    """A local-only llama.cpp client. No credentials or external API calls."""
    def __init__(self, endpoint: str, model: str, fingerprint: str = ""):
        from urllib.parse import urlparse
        parsed = urlparse(endpoint)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
            raise ValueError("Research inference is restricted to a local server")
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.cache_identity = {"model": model, "fingerprint": fingerprint,
                               "transport_version": 2}
        self.calls: list[dict] = []

    def json(self, system: str, content: str, max_tokens: int = 2000,
             purpose: str = "unspecified", schema: dict | None = None) -> dict:
        payload = {
            "model": self.model, "temperature": 0, "seed": 20261031,
            "max_tokens": max_tokens,
            "response_format": ({"type": "json_schema", "json_schema": {
                                    "name": "grounded_notes", "strict": True, "schema": schema}} if schema
                                else {"type": "json_object"}),
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": content}],
        }
        request = urllib.request.Request(
            self.endpoint + "/v1/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=600) as response:
            data = json.load(response)
        trace_path = Path.cwd() / ".cache/llm_traces" / f"{purpose}_{time.time_ns()}.json"
        write_json(trace_path, {"purpose": purpose, "request_sha256": digest(payload),
                                "response": data})
        self.calls.append({"purpose": purpose, "seconds": round(time.perf_counter() - started, 3),
                           "usage": data.get("usage", {}),
                           "finish_reason": data["choices"][0].get("finish_reason")})
        if data["choices"][0].get("finish_reason") == "length":
            raise ValueError("Model output was truncated; no partial notes accepted")
        content = data["choices"][0]["message"]["content"].strip()
        return json.loads(content)


def validate_notes(value: dict, allowed_ids: set[str]) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("overview"), str):
        raise ValueError("Missing notes overview")
    topics = value.get("topics")
    if not isinstance(topics, list) or not topics:
        raise ValueError("No synthesized topics")
    for topic in topics:
        if not isinstance(topic, dict) or any(not isinstance(topic.get(f), str) for f in FIELDS):
            raise ValueError("Malformed topic")
        references = topic.get("evidence_ids")
        if (not isinstance(references, list) or not references or
                any(not isinstance(ref, str) or ref not in allowed_ids for ref in references)):
            raise ValueError("Unknown or absent evidence; refusing unsupported notes")
        topic["evidence_ids"] = list(dict.fromkeys(references))
        if not topic["title"].strip() or not topic["key_idea"].strip():
            raise ValueError("Empty topic")
    for name in ("review_questions", "uncertainties"):
        if not isinstance(value.get(name), list) or any(not isinstance(s, str) for s in value[name]):
            raise ValueError("Malformed notes list")
    return value


def batches(chunks: list[Chunk], limit: int) -> list[list[Chunk]]:
    result: list[list[Chunk]] = []
    pending: list[Chunk] = []
    count = 0
    for chunk in chunks:
        if pending and count + len(chunk.text) > limit:
            result.append(pending)
            pending, count = [], 0
        pending.append(chunk)
        count += len(chunk.text)
    if pending:
        result.append(pending)
    return result


def synthesize(units: list[Unit], chunks: list[Chunk], predictions: list[dict],
               model: LocalModel, prompt: str, cache_dir: Path, config: dict) -> dict:
    allowed = {u.unit_id for u in units} | {c.segment_id for c in chunks}
    units_by_id = {u.unit_id: u for u in units}
    predictions_by_id = {p["segment_id"]: p for p in predictions}
    intermediate = []
    for index, batch in enumerate(batches(chunks, config["summary_batch_characters"])):
        candidate_ids = {
            candidate["unit_id"]
            for chunk in batch
            for candidate in predictions_by_id[chunk.segment_id]["candidates"][:2]
        }
        evidence = [{"id": c.segment_id, "type": "independent_audio_transcript", "text": c.text}
                    for c in batch]
        evidence += [{"id": identifier, "type": "document_text_unconfirmed_pairing",
                      "text": units_by_id[identifier].text}
                     for identifier in sorted(candidate_ids)]
        content = json.dumps({"task": "归纳这个批次的要点，最多四个主题。",
                              "evidence": evidence}, ensure_ascii=False)
        batch_ids = {e["id"] for e in evidence}
        schema = notes_schema(batch_ids, 4)
        key = digest({"prompt": prompt, "content": content, "model": model.cache_identity,
                      "schema": schema})
        cache = cache_dir / f"map_{key}.json"
        if cache.exists():
            notes = validate_notes(read_json(cache), batch_ids)
        else:
            notes = validate_notes(model.json(prompt, content, max_tokens=2600,
                                               purpose="synthesis_map", schema=schema), batch_ids)
            write_json(cache, notes)
        intermediate.append(notes)
        print(f"Synthesis batch {index + 1} ready", flush=True)
    content = json.dumps({
        "task": "将批次笔记分析、去重并归纳为最终笔记。最多六个主题。保留有价值的解释和例子。",
        "batch_notes": intermediate,
    }, ensure_ascii=False)
    # A reduce step may cite only evidence actually preserved by a map step.
    mapped_ids = {ref for note in intermediate for topic in note["topics"]
                  for ref in topic["evidence_ids"]}
    schema = notes_schema(allowed & mapped_ids, config["summary_max_topics"])
    key = digest({"prompt": prompt, "content": content, "model": model.cache_identity,
                  "schema": schema})
    cache = cache_dir / f"reduce_{key}.json"
    if cache.exists():
        final = validate_notes(read_json(cache), allowed & mapped_ids)
    else:
        final = validate_notes(model.json(prompt, content, max_tokens=4500,
                                           purpose="synthesis_reduce", schema=schema),
                               allowed & mapped_ids)
        write_json(cache, final)
    if len(final["topics"]) > config["summary_max_topics"]:
        raise ValueError("Too many topics in final synthesis")
    return {"notes": final, "intermediate": intermediate, "model_calls": model.calls,
            "model": model.cache_identity,
            "mode": "local_model_map_reduce",
            "validation": "Reference IDs checked; factual correctness requires human review"}
