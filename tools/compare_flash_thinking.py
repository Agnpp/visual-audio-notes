"""Compare selected Flash modes on one frozen desktop preparation."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import sys
import time

from visual_audio_notes.binding import validate_binding
from visual_audio_notes.core import Chunk, file_hash, load_features, read_json
from visual_audio_notes.desktop_ai import run_ai
from visual_audio_notes.model_selection import EFFORTS
from visual_audio_notes.quality import _origin_batches, _sources
from visual_audio_notes.task_runtime import TaskBudgetExceeded, TaskRequestFailed, atomic_json
from visual_audio_notes.workflow_cli import connection_from_file


ROOT = Path(__file__).resolve().parents[1]


def _snapshot(project, meta, binding, destination):
    # The evaluation copy excludes existing notes, prior decisions, model cache,
    # and anything outside the original audio, PDF and bound preparation.
    new_meta = {}
    for field in ("audio", "pdf"):
        name = "recording" if field == "audio" else "materials"
        relative = name + Path(meta[field]).suffix
        shutil.copyfile(project / meta[field], destination / relative)
        new_meta[field] = relative
    prepared = destination / "prepared"
    for name in [*binding["prepared_hashes"], "archive_binding.json"]:
        target = prepared / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(project / "prepared" / name, target)
    atomic_json(destination / "project.json", new_meta)


def _plan(args, connection, binding, units, segments):
    suffix = ".en.txt" if args.language == "en" else ".txt"
    config = read_json(ROOT / "benchmark/provider.v3.json")
    sources = _sources(units, [Chunk(s.segment_id, s.start, s.end, s.text) for s in segments])
    extraction_batches = {origin: len(_origin_batches(sources, origin, config["quality_batch_characters"],
                                                      config.get("quality_sources_per_batch")))
                          for origin in ("audio", "courseware")}
    extraction_calls = sum(extraction_batches.values())
    topic_calls = sum(bool(count) for count in extraction_batches.values())
    fresh_calls = extraction_calls + topic_calls
    fresh_output = extraction_calls * config["quality_max_tokens"] + topic_calls * config["topic_max_tokens"]
    limits_fit = (args.max_calls >= fresh_calls and args.max_output_tokens >= fresh_output
                  if args.max_calls is not None and args.max_output_tokens is not None else None)
    resources = [
        "tools/compare_flash_thinking.py", "benchmark/provider.v3.json",
        "prompts/quality_spans" + suffix, "prompts/topic_notes" + suffix,
        *["src/visual_audio_notes/" + name + ".py" for name in
          ("binding", "core", "desktop_ai", "desktop_matching", "providers", "quality", "review", "topic_notes",
           "task_runtime", "step_cache")],
    ]
    return {
        "execute": args.execute, "connection": connection.public_dict(), "modes_in_order": args.modes,
        "output_language": args.language, "pages": len(units), "transcript_segments": len(segments),
        "binding": binding, "resource_hashes": {name: file_hash(ROOT / name) for name in resources},
        "limits_per_mode": {"calls": args.max_calls, "requested_output_tokens": args.max_output_tokens},
        "fresh_run_per_mode": {"extraction_batches": extraction_batches, "topic_calls_upper_bound": topic_calls,
                               "calls_upper_bound": fresh_calls, "requested_output_tokens_upper_bound": fresh_output,
                               "within_selected_limits": limits_fit},
        "max_total_calls": args.max_calls * len(args.modes) if args.max_calls is not None else None,
        "max_total_requested_output_tokens": (args.max_output_tokens * len(args.modes)
                                              if args.max_output_tokens is not None else None),
        "latency_scope": "Desktop candidate/evidence/topic workflow with identical prepared inputs; excludes ASR and OCR.",
        "quality_status": "pending independent scoring after outputs are frozen",
    }


def compare(args):
    project = args.project.resolve()
    meta = read_json(project / "project.json")
    prepared = project / "prepared"
    if not prepared.resolve().is_relative_to(project):
        raise ValueError("Prepared sources must belong to the selected desktop project.")
    for field in ("audio", "pdf"):
        if not meta.get(field) or not (project / meta[field]).resolve().is_relative_to(project):
            raise ValueError("Choose a desktop project with contained audio, PDF and completed preparation.")
    binding = read_json(prepared / "archive_binding.json")
    validate_binding(binding, prepared, project / meta["audio"], project / meta["pdf"])
    units, segments = load_features(prepared / "features.json")
    connection = connection_from_file(args.providers, args.connection)
    if connection.provider != "deepseek" or connection.model != "deepseek-flash":
        raise ValueError("Select a DeepSeek connection for deepseek-flash.")
    plan = _plan(args, connection, binding, units, segments)
    if not args.execute:
        return plan
    if not plan["fresh_run_per_mode"]["within_selected_limits"]:
        needed = plan["fresh_run_per_mode"]
        raise ValueError(f"Selected limits do not cover a full comparison ({needed['calls_upper_bound']} calls, "
                         f"{needed['requested_output_tokens_upper_bound']} requested output tokens per mode). "
                         "Select shorter material or adjust the limits in the preflight.")
    if not os.environ.get(connection.api_key_env):
        raise ValueError("Configure the selected connection's credential environment variable before executing.")
    out = args.out.resolve()
    if out.is_relative_to(project):
        raise ValueError("Choose a new output folder outside the source project.")
    out.mkdir(parents=True, exist_ok=False)
    atomic_json(out / "protocol.json", plan)
    report = {"protocol_sha256": file_hash(out / "protocol.json"), "modes": {},
              "quality_status": "pending independent review"}
    for mode in args.modes:
        destination = out / mode
        destination.mkdir()
        _snapshot(project, meta, binding, destination)
        selected = replace(connection, thinking="disabled" if mode == "off" else "enabled",
                           reasoning_effort="high" if mode == "off" else mode)
        run = destination / "runs/result"
        started = time.perf_counter()
        status, failure = "complete", None
        try:
            run_ai(destination, ROOT, "result", selected, False, lambda *_: None,
                   language=args.language, max_calls=args.max_calls, max_output_tokens=args.max_output_tokens)
        except (ValueError, TaskRequestFailed, TaskBudgetExceeded, OSError) as error:
            status, failure = "failed", type(error).__name__
        except KeyboardInterrupt:
            status = "interrupted"
            raise
        finally:
            seconds = round(time.perf_counter() - started, 3)
            files = {path.name: file_hash(path) for path in sorted(run.glob("*")) if path.is_file()}
            usage_path = run / "usage_report.json"
            usage = read_json(usage_path) if usage_path.is_file() else None
            journal_path = run / "task_runtime.json"
            calls = read_json(journal_path)["calls"] if journal_path.is_file() else []
            report["modes"][mode] = {"status": status, "failure_type": failure, "seconds": seconds,
                                     "connection": selected.public_dict(), "usage": usage, "files_sha256": files,
                                     "attempted_calls": len(calls),
                                     "requests": [{field: call[field] for field in
                                                   ("purpose", "status", "seconds", "finish_reason", "response_error",
                                                    "reasoning_tokens", "http_status")
                                                   if field in call} for call in calls]}
            atomic_json(out / "comparison.json", report)
        if any(call.get("http_status") in {401, 403, 429} for call in calls):
            break
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path, help="Desktop project directory with prepared sources")
    parser.add_argument("--language", choices=("zh-CN", "en"), required=True)
    parser.add_argument("--providers", type=Path, default=ROOT / "configs/providers.example.json")
    parser.add_argument("--connection", default="deepseek_flash")
    parser.add_argument("--modes", nargs="+", choices=EFFORTS)
    parser.add_argument("--execute", action="store_true", help="Send the selected sources' text using your configured API")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--max-calls", type=int, help="Explicit call limit per mode")
    parser.add_argument("--max-output-tokens", type=int, help="Limit on requested output tokens per mode")
    args = parser.parse_args(argv)
    if args.modes and len(args.modes) != len(set(args.modes)):
        parser.error("Choose each mode at most once.")
    if args.execute and (not args.out or not args.modes or args.max_calls is None or args.max_output_tokens is None):
        parser.error("--execute requires --out, --modes, --max-calls and --max-output-tokens.")
    if ((args.max_calls is not None and not 1 <= args.max_calls <= 32)
            or (args.max_output_tokens is not None and not 1 <= args.max_output_tokens <= 128000)):
        parser.error("Limits must be positive and within the desktop ceilings (32 calls, 128000 output tokens).")
    args.modes = args.modes or ["off", "low", "high"]
    try:
        result = compare(args)
    except (ValueError, OSError, KeyError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(args.execute and (len(result["modes"]) != len(args.modes)
               or any(value["status"] != "complete" for value in result["modes"].values())))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
