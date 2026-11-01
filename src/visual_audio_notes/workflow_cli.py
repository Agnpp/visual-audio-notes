"""Provider-independent workflow entry; desktop packaging is a later layer."""
from __future__ import annotations

import argparse
from dataclasses import asdict, fields
from pathlib import Path
import shutil
import sys
import time

from .capabilities import list_capabilities, plan_workflow
from .core import chunks_from_segments, file_hash, load_features, read_json, write_json
from .providers import ModelConnection, create_json_model
from .review import create_review, save_review
from .task_runtime import TaskBudgetExceeded, TaskModel, usage_report

ROOT = Path(__file__).resolve().parents[2]


def connection_from_file(path: Path, name: str) -> ModelConnection:
    registry = read_json(path)
    if not isinstance(registry, dict) or name not in registry:
        raise ValueError("Selected connection does not exist")
    selected = registry[name]
    allowed = {f.name for f in fields(ModelConnection)}
    if not isinstance(selected, dict) or set(selected) - allowed:
        raise ValueError("Connection config accepts public settings and a credential environment-variable name only")
    return ModelConnection(**selected)


def capabilities(args):
    if args.inputs:
        result = plan_workflow(read_json(args.inputs), read_json(args.connections) if args.connections else None)
    else:
        result = {"capabilities": list_capabilities(),
                  "notice": "需要 AI 与部署位置分开；已有文字或转写可以复用，不调用模型。"}
    if args.out:
        write_json(args.out, result)
    print(__import__("json").dumps(result, ensure_ascii=False, indent=2), flush=True)


def prepare(args):
    from .basic import prepare_basic
    from .binding import create_binding, save_binding
    result = prepare_basic(args.pdf, args.audio, args.transcript, args.work, args.seed)
    save_binding(args.work / "archive_binding.json", create_binding(args.work, args.audio, args.pdf))
    print(__import__("json").dumps(result, ensure_ascii=False, indent=2), flush=True)


def models(args):
    from .provider_discovery import list_connection_models
    result = list_connection_models(connection_from_file(args.providers, args.connection))
    if args.out:
        write_json(args.out, result)
    print(__import__("json").dumps(result, ensure_ascii=False, indent=2), flush=True)


def basic_archive(args):
    from .basic_output import export_basic_archive
    text = args.notes.read_text(encoding="utf-8") if args.notes else ""
    print(export_basic_archive(args.out, args.title, args.audio, args.pdf, args.prepared, text), flush=True)


def infer(args):
    if args.out.exists():
        raise ValueError("Use a new output directory; existing results are preserved")
    connection = connection_from_file(args.providers, args.connection)
    selected_model = create_json_model(connection)
    config = read_json(args.protocol)
    features_hash = file_hash(args.features)
    binding = read_json(args.binding)
    if binding.get("prepared_hashes", {}).get("features.json") != features_hash:
        raise ValueError("Source binding differs from inference features")
    units, segments = load_features(args.features)
    chunks = chunks_from_segments(segments, config["chunk_seconds"])
    args.out.mkdir(parents=True)
    started = time.perf_counter()
    # Normal projects reuse validated steps. Evaluation never reads product cache.
    product = args.evaluation_role == "product"
    persistent = product and not getattr(args, "no_result_cache", False)
    cache_dir = args.features.parent / ".van-cache" if persistent else args.out / "cache"
    selected_model.cache_identity = {key: value for key, value in selected_model.cache_identity.items()
                                     if key not in {"run_identity", "evaluation_role"}}
    if not persistent:
        selected_model.cache_identity.update(run_identity=args.out.name,
                                             evaluation_role=args.evaluation_role)
    config = {**config, "persistent_cache": persistent,
              "cache_force_refresh": bool(getattr(args, "refresh_ai", False)),
              "cache_max_age_seconds": 86400, "cache_policy_version": "product-task-cache-v1"}
    model = TaskModel(selected_model, args.out / "task_runtime.json",
                      max_calls=getattr(args, "max_api_calls", 32),
                      max_requested_output_tokens=getattr(args, "max_output_tokens", 128000),
                      stop_on_request_error=product)
    shutil.copy2(args.binding, args.out / "archive_binding.json")
    write_json(args.out / "connection.json", connection.public_dict())
    write_json(args.out / "chunks.json", [asdict(c) for c in chunks])
    try:
        _infer_stages(args, config, model, units, chunks, cache_dir, started)
    except BaseException as error:
        model.finish("budget_stopped" if isinstance(error, TaskBudgetExceeded) else
                     "interrupted" if isinstance(error, KeyboardInterrupt) else "failed")
        # Even a schema/provider failure may be billable. Keep its usage without
        # publishing a completed workflow or exposing provider response bodies.
        batches = []
        for name in ("quality_notes.json", "topic_notes.json"):
            path = args.out / name
            if path.is_file():
                batches.extend(read_json(path).get("batches", []))
        write_json(args.out / "usage_report.json", usage_report(model.calls, batches))
        raise


def _infer_stages(args, config, model, units, chunks, cache_dir, started):
    from .align import align
    from .cli import freeze
    from .quality import synthesize_grounded
    from .topic_notes import organize_topics, render_topic_markdown
    # Keyword candidates need no embedding model. Embedding is an explicit extra.
    model.stage("candidates", "running")
    rankings, timing = align(units, chunks, {**config, "automatic_score_floor": 1e9},
                              str(args.embedding.resolve()) if args.embedding else None)
    write_json(args.out / "ranking.json", rankings)
    save_review(args.out / "candidate_review.json",
                create_review(rankings.get("hybrid", rankings["lexical"]), units, chunks))
    model.stage("candidates", "complete")
    model.stage("evidence", "running")
    extraction = synthesize_grounded(units, chunks, model,
        (ROOT / "prompts/quality_spans.txt").read_text(encoding="utf-8"), cache_dir, config)
    write_json(args.out / "quality_notes.json", extraction)
    if not extraction.get("ledger"):
        write_json(args.out / "failure.json", {"stage": "evidence_extraction",
                   "status": "no_usable_claims", "complete": False})
        raise ValueError("No usable evidence claims; refusing to freeze a completed workflow")
    model.stage("evidence", "complete")
    model.stage("topics", "running")
    grouped = organize_topics(extraction, model,
        (ROOT / "prompts/topic_notes.txt").read_text(encoding="utf-8"), cache_dir, config)
    write_json(args.out / "topic_notes.json", grouped)
    (args.out / "主题笔记草稿.md").write_text(render_topic_markdown(grouped, extraction["ledger"]), encoding="utf-8")
    model.stage("topics", "complete")
    model.finish()
    report = usage_report(model.calls, extraction["batches"] + grouped["batches"])
    write_json(args.out / "usage_report.json", report)
    metrics = {"seconds": round(time.perf_counter() - started, 3), "ranking": timing,
               "pages": len(units), "chunks": len(chunks), "claims": len(extraction["ledger"]),
               "rejected_claims": len(extraction["rejected_claims"]), "extraction_failures": len(extraction["failures"]),
               "calls": model.calls, "evaluation_role": args.evaluation_role,
               "quality_status": "unverified_draft", "confirmed_matches": 0,
               "uses_ai": True, "connection": args.connection, "usage": report,
               "cache_mode": "project_validated_24h" if config["persistent_cache"] else "isolated_run",
               "data_scope": "independent transcript text and extracted courseware text; no video frames, answers or labels"}
    write_json(args.out / "workflow_metrics.json", metrics)
    freeze(args.out, args.protocol, args.features)
    frozen = read_json(args.out / "freeze.json")
    frozen["stage"] = ({"development": "provider_workflow_development_on_previously_reviewed_material",
                        "holdout": "provider_workflow_frozen_before_holdout_evaluation",
                        "product": "product_workflow_with_project_step_reuse"}[args.evaluation_role])
    write_json(args.out / "freeze.json", frozen)
    print({k: v for k, v in metrics.items() if k != "calls"}, flush=True)


def report_usage(args):
    journal = args.run / "task_runtime.json"
    if journal.is_file():
        calls = read_json(journal)["calls"]
    else:
        calls = read_json(args.run / "workflow_metrics.json")["calls"]
    batches = []
    for name in ("quality_notes.json", "topic_notes.json"):
        path = args.run / name
        if path.is_file():
            batches.extend(read_json(path).get("batches", []))
    print(__import__("json").dumps(usage_report(calls, batches), ensure_ascii=False, indent=2), flush=True)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="图音笔记：按 AI 能力选择工作流（开发者入口）")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("capabilities")
    p.add_argument("--inputs", type=Path)
    p.add_argument("--connections", type=Path)
    p.add_argument("--out", type=Path)
    p.set_defaults(function=capabilities)
    p = sub.add_parser("prepare-basic")
    for name in ("pdf", "audio", "transcript", "work"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--seed", type=int, default=20261101)
    p.set_defaults(function=prepare)
    p = sub.add_parser("models")
    p.add_argument("--providers", type=Path, default=ROOT / "configs/providers.example.json")
    p.add_argument("--connection", required=True)
    p.add_argument("--out", type=Path)
    p.set_defaults(function=models)
    p = sub.add_parser("usage", help="读取运行用量，不调用 API")
    p.add_argument("--run", type=Path, required=True)
    p.set_defaults(function=report_usage)
    p = sub.add_parser("archive-basic")
    for name in ("prepared", "pdf", "audio", "out"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--notes", type=Path)
    p.add_argument("--title", default="图音笔记 · 基础资料归档")
    p.set_defaults(function=basic_archive)
    p = sub.add_parser("infer")
    for name in ("features", "binding", "out"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--providers", type=Path, default=ROOT / "configs/providers.example.json")
    p.add_argument("--connection", required=True)
    p.add_argument("--protocol", type=Path, default=ROOT / "benchmark/provider.v3.json")
    p.add_argument("--embedding", type=Path)
    p.add_argument("--evaluation-role", choices=["product", "development", "holdout"], default="product")
    p.add_argument("--no-result-cache", action="store_true", help="本轮不使用项目结果缓存")
    p.add_argument("--refresh-ai", action="store_true", help="重新调用模型并更新本任务缓存")
    p.add_argument("--max-api-calls", type=int, default=32)
    p.add_argument("--max-output-tokens", type=int, default=128000,
                   help="本轮各请求最大输出 token 之和上限，不等于实际计费用量")
    p.set_defaults(function=infer)
    args = parser.parse_args()
    args.function(args)


if __name__ == "__main__":
    main()
