"""Export evidence-only when overall content review has failed or is incomplete.

This uses saved artifacts, never reruns a model and never changes frozen output.
"""
import argparse
from dataclasses import asdict
from pathlib import Path

from visual_audio_notes.binding import validate_binding
from visual_audio_notes.cli import verify_freeze
from visual_audio_notes.core import Chunk, file_hash, load_features, read_json, write_json
from visual_audio_notes.quality_gate import build_quality_gate
from visual_audio_notes.review import load_review, source_fingerprint
from visual_audio_notes.reviewed_output import export_reviewed_archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("run", "prepared", "audio", "pdf", "mechanical-checks", "semantic-review", "credit", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--content-review", type=Path)
    parser.add_argument("--title", default="图音笔记 · 待核对原文证据")
    args = parser.parse_args()
    if args.out.exists():
        raise ValueError("Use a new destination; existing archives are preserved")
    frozen = verify_freeze(args.run)
    freeze_hash = file_hash(args.run / "freeze.json")
    prepared_features = args.prepared / "features.json"
    if frozen["features_sha256"] != file_hash(prepared_features):
        raise ValueError("Prepared content changed")
    validate_binding(read_json(args.run / "archive_binding.json"), args.prepared, args.audio, args.pdf)
    mechanical = read_json(args.mechanical_checks)
    if mechanical.get("freeze_sha256") != freeze_hash:
        raise ValueError("Mechanical review belongs to a different extraction")
    content = read_json(args.content_review) if args.content_review else None
    gate = build_quality_gate(mechanical, content)
    semantic = read_json(args.semantic_review)
    if semantic.get("source_freeze_sha256") != freeze_hash:
        raise ValueError("Semantic review belongs to a different extraction")
    units, _ = load_features(prepared_features)
    chunks = [Chunk(**c) for c in read_json(args.run / "chunks.json")]
    edits = args.run / "edits/review.json"
    review = load_review(edits if edits.exists() else args.run / "candidate_review.json",
                         expected_fingerprint=source_fingerprint(units, chunks))
    checked = export_reviewed_archive(
        args.out, args.title, args.audio, args.pdf, args.prepared,
        read_json(args.prepared / "source_map.json"), [asdict(c) for c in chunks], review,
        read_json(args.run / "quality_notes.json"), args.credit.read_text(encoding="utf-8"),
        semantic_review=semantic, publication_gate=gate)
    write_json(args.out / "quality_gate_provenance.json", {
        "source_freeze_sha256": freeze_hash,
        "mechanical_checks_sha256": file_hash(args.mechanical_checks),
        "semantic_review_sha256": file_hash(args.semantic_review),
        "content_review_sha256": file_hash(args.content_review) if args.content_review else None,
        "role": "presentation_only_from_previously_frozen_outputs_not_new_inference_or_benchmark",
    })
    print({"quality_status": gate["quality_status"], "automatic_publish_allowed": False,
           "rendering": gate["recommended_rendering"], "checks": checked}, flush=True)


if __name__ == "__main__":
    main()
