"""Render a frozen provider result, without making model calls."""
import argparse
from pathlib import Path

from visual_audio_notes.binding import validate_binding
from visual_audio_notes.cli import ROOT, verify_freeze
from visual_audio_notes.core import file_hash, read_json, write_json
from visual_audio_notes.review import load_review
from visual_audio_notes.topic_output import export_topic_archive


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", type=Path, default=ROOT / "outputs/provider-v3-20261031/deepseek-topics")
    p.add_argument("--out", type=Path, default=ROOT / "outputs/provider-v3-20261031/topic_archive")
    args = p.parse_args()
    frozen = verify_freeze(args.run)
    prepared = ROOT / "data/work/mit_collections_holdout"
    source = ROOT / "data/public/mit_collections_holdout"
    if file_hash(prepared / "features.json") != frozen["features_sha256"]:
        raise ValueError("Prepared content differs from the frozen result")
    validate_binding(read_json(args.run / "archive_binding.json"), prepared,
                     source / "recording.wav", source / "materials.pdf")
    extraction, topics = read_json(args.run / "quality_notes.json"), read_json(args.run / "topic_notes.json")
    checks = export_topic_archive(args.out, "图音笔记 · DeepSeek Flash 主题草稿",
        source / "recording.wav", source / "materials.pdf", prepared,
        load_review(args.run / "candidate_review.json"), extraction, topics,
        (prepared / "quality_credit.txt").read_text(encoding="utf-8"),
        chunks=read_json(args.run / "chunks.json"))
    write_json(args.out / "processing_info.json", {
        "uses_ai": True, "source_run_freeze_sha256": file_hash(args.run / "freeze.json"),
        "extraction_connection": extraction.get("model"), "topic_connection": topics.get("model"),
        "evaluation_role": "development_on_seen_material_not_new_holdout", "quality_status": "draft_requires_review"})
    print(checks, flush=True)


if __name__ == "__main__":
    main()
