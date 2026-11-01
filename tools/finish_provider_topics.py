"""Continue grouping saved evidence; never repeat paid extraction or read gold."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import shutil
import time

from visual_audio_notes.cli import ROOT, freeze, verify_freeze
from visual_audio_notes.core import file_hash, read_json, write_json
from visual_audio_notes.providers import create_json_model
from visual_audio_notes.topic_notes import organize_topics, render_topic_markdown
from visual_audio_notes.workflow_cli import connection_from_file


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-run", type=Path, required=True)
    p.add_argument("--features", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--providers", type=Path, default=ROOT / "configs/providers.example.json")
    p.add_argument("--connection", default="deepseek_flash")
    p.add_argument("--protocol", type=Path, default=ROOT / "benchmark/provider.v3.json")
    args = p.parse_args()
    if args.out.exists():
        raise ValueError("Use a new directory; results are preserved")
    frozen = verify_freeze(args.source_run)
    if file_hash(args.features) != frozen["features_sha256"]:
        raise ValueError("Evidence inputs changed")
    extraction = read_json(args.source_run / "quality_notes.json")
    config = read_json(args.protocol)
    connection = connection_from_file(args.providers, args.connection)
    model = create_json_model(connection)
    model.cache_identity = {**model.cache_identity, "run_identity": args.out.name}
    args.out.mkdir(parents=True)
    for name in ("archive_binding.json", "chunks.json", "ranking.json", "candidate_review.json", "quality_notes.json"):
        shutil.copy2(args.source_run / name, args.out / name)
    shutil.copy2(args.source_run / "connection.json", args.out / "extraction_connection.json")
    write_json(args.out / "connection.json", connection.public_dict())
    started = time.perf_counter()
    topics = organize_topics(extraction, model,
        (ROOT / "prompts/topic_notes.txt").read_text(encoding="utf-8"), args.out / "cache", config)
    write_json(args.out / "topic_notes.json", topics)
    (args.out / "主题笔记草稿.md").write_text(render_topic_markdown(topics, extraction["ledger"]), encoding="utf-8")
    write_json(args.out / "workflow_metrics.json", {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "evaluation_role": "development_on_previously_reviewed_material_not_new_holdout",
        "extraction_reused": True, "source_run_freeze_sha256": file_hash(args.source_run / "freeze.json"),
        "new_extraction_calls": 0, "new_topic_calls": model.calls,
        "extraction_model": extraction.get("model"), "topic_model": model.cache_identity,
        "seconds": round(time.perf_counter() - started, 3), "quality_status": "unverified_draft"})
    freeze(args.out, args.protocol, args.features)
    saved = read_json(args.out / "freeze.json")
    saved["stage"] = "provider_topic_development_reusing_saved_extraction"
    write_json(args.out / "freeze.json", saved)
    print({"topic_result_frozen": str(args.out), "topics_audio": len(topics["audio_topics"]),
           "topics_courseware": len(topics["courseware_topics"]), "new_calls": len(model.calls)}, flush=True)


if __name__ == "__main__":
    main()
