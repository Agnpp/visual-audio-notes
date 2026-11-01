"""Quality-development entry point; independent of frozen phase-2 inference."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
import shutil
import time

from .cli import ROOT, MemoryWatch, freeze, verify_freeze
from .core import Chunk, chunks_from_segments, file_hash, load_features, read_json, write_json
from .review import (apply_decision, create_review, export_review, load_review,
                     save_review, source_fingerprint)


def infer(args):
    from .align import align
    from .notes import LocalModel
    from .quality import synthesize_grounded
    if args.out.exists() and any(args.out.iterdir()):
        raise ValueError("Use a new output directory; previous results are preserved")
    config = read_json(args.protocol)
    units, segments = load_features(args.features)
    binding = read_json(args.binding)
    if binding.get("prepared_hashes", {}).get("features.json") != file_hash(args.features):
        raise ValueError("Archive binding differs from inference features")
    chunks = chunks_from_segments(segments, config["chunk_seconds"])
    args.out.mkdir(parents=True, exist_ok=True)
    # Attach immutable display provenance; never send it to either model.
    shutil.copy2(args.binding, args.out / "archive_binding.json")
    started = time.perf_counter()
    with MemoryWatch() as memory:
        # Scores order candidates only. Disable legacy acceptance at its source
        # as well as in the explicit review layer; no score can confirm a page.
        rankings, timings = align(units, chunks,
                                  {**config, "automatic_score_floor": 1e9},
                                  str(args.embedding.resolve()) if args.embedding else None)
        ranked = rankings.get("hybrid", rankings["lexical"])
        review = create_review(ranked, units, chunks)
        write_json(args.out / "ranking.json", {"purpose": "candidate_order_only", "methods": rankings})
        write_json(args.out / "chunks.json", [asdict(c) for c in chunks])
        save_review(args.out / "candidate_review.json", review)
        model = LocalModel(args.llm, args.model, file_hash(args.model_file))
        result = synthesize_grounded(units, chunks, model,
                                     args.prompt.read_text(encoding="utf-8"), args.cache, config)
        write_json(args.out / "quality_notes.json", result)
        metrics = {"seconds": round(time.perf_counter() - started, 3), "alignment": timings,
                   "python_peak_rss_mb_excluding_model_server": round(memory.peak / 1024**2, 2),
                   "sources": len(units) + len(chunks), "ledger_items": len(result["ledger"]),
                   "confirmed_matches": sum(i["status"] == "confirmed" for i in review["items"]),
                   "extraction_failures": len(result["failures"]),
                   "rejected_claims": len(result["rejected_claims"]),
                   "uncovered_sources": sum(not r["claim_ids"] for r in result["source_audit"]),
                   "evaluation_role": args.evaluation_role}
        write_json(args.out / "quality_metrics.json", metrics)
    freeze(args.out, args.protocol, args.features)
    frozen = read_json(args.out / "freeze.json")
    for name in ("tools/run_quality_round.py", "tools/create_archive_binding.py",
                 "tools/prepare_quality_holdout.py", "docs/阶段3质量验证协议.md"):
        source = ROOT / name
        target = args.out / "source_snapshot" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        frozen["code"][name] = file_hash(source)
    frozen["stage"] = ("quality_v2_frozen_before_new_holdout_evaluation" if args.evaluation_role == "holdout"
                       else "quality_v2_development_on_previously_seen_material")
    write_json(args.out / "freeze.json", frozen)
    print(metrics, flush=True)


def review(args):
    verify_freeze(args.run)
    units, segments = load_features(args.features)
    config = read_json(args.run / "protocol_snapshot.json")
    chunks = chunks_from_segments(segments, config["chunk_seconds"])
    fingerprint = source_fingerprint(units, chunks)
    edits = args.run / "edits/review.json"
    state = load_review(edits if edits.exists() else args.run / "candidate_review.json",
                        expected_fingerprint=fingerprint)
    state = apply_decision(state, args.segment, args.unit, reviewer=args.reviewer,
                           expected_fingerprint=fingerprint, decision=args.decision, note=args.note)
    save_review(edits, state)
    print({"saved": str(edits), "source_prediction_modified": False,
           "human_action_audit": export_review(state)}, flush=True)


def archive(args):
    from .binding import validate_binding
    from .quality_output import export_quality_archive
    frozen = verify_freeze(args.run)
    features = args.prepared / "features.json"
    if file_hash(features) != frozen["features_sha256"]:
        raise ValueError("Prepared input differs from the frozen run")
    # The binding was captured before inference and is itself in freeze.json.
    # Current editable preparation metrics cannot replace its source hashes.
    validate_binding(read_json(args.run / "archive_binding.json"), args.prepared,
                     args.audio, args.pdf)
    units, _ = load_features(features)
    chunks = [Chunk(**c) for c in read_json(args.run / "chunks.json")]
    edits = args.run / "edits/review.json"
    state = load_review(edits if edits.exists() else args.run / "candidate_review.json",
                        expected_fingerprint=source_fingerprint(units, chunks))
    checks = export_quality_archive(args.out, args.title, args.audio, args.pdf, args.prepared,
                                    read_json(args.prepared / "source_map.json"),
                                    [asdict(c) for c in chunks], state,
                                    read_json(args.run / "quality_notes.json"),
                                    args.credit.read_text(encoding="utf-8"))
    print(checks, flush=True)


def main():
    p = argparse.ArgumentParser(description="图音笔记：证据账本与人工确认（质量验证版）")
    sub = p.add_subparsers(dest="command", required=True)
    q = sub.add_parser("infer")
    q.add_argument("--features", type=Path, required=True)
    q.add_argument("--out", type=Path, required=True)
    q.add_argument("--binding", type=Path, required=True)
    q.add_argument("--embedding", type=Path)
    q.add_argument("--llm", required=True)
    q.add_argument("--model-file", type=Path, required=True)
    q.add_argument("--model", default="qwen3-4b-instruct-2507-q4_k_m")
    q.add_argument("--protocol", type=Path, default=ROOT / "benchmark/quality.v2.json")
    q.add_argument("--prompt", type=Path, default=ROOT / "prompts/quality_spans.txt")
    q.add_argument("--cache", type=Path, default=ROOT / ".cache/quality")
    q.add_argument("--evaluation-role", choices=["holdout", "development"], required=True)
    q.set_defaults(function=infer)
    q = sub.add_parser("review")
    q.add_argument("--run", type=Path, required=True)
    q.add_argument("--features", type=Path, required=True)
    q.add_argument("--segment", required=True)
    q.add_argument("--unit", action="append", default=[])
    q.add_argument("--decision", choices=["match", "no_match", "defer"], required=True)
    q.add_argument("--reviewer", required=True)
    q.add_argument("--note", default="")
    q.set_defaults(function=review)
    q = sub.add_parser("archive")
    for name in ("run", "prepared", "pdf", "audio", "out", "credit"):
        q.add_argument("--" + name, type=Path, required=True)
    q.add_argument("--title", required=True)
    q.set_defaults(function=archive)
    args = p.parse_args()
    args.function(args)


if __name__ == "__main__":
    main()
