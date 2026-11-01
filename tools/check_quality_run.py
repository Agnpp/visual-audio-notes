"""Mechanical checks only; not a semantic notes or concept coverage score."""
import argparse
from collections import Counter
from pathlib import Path

from visual_audio_notes.cli import verify_freeze
from visual_audio_notes.core import Chunk, file_hash, load_features, read_json, write_json
from visual_audio_notes.review import load_review, source_fingerprint


def check(run: Path, features: Path) -> dict:
    frozen = verify_freeze(run)
    if frozen["features_sha256"] != file_hash(features):
        raise ValueError("Input features changed")
    units, _ = load_features(features)
    chunks = [Chunk(**c) for c in read_json(run / "chunks.json")]
    sources = {u.unit_id: u.text for u in units} | {c.segment_id: c.text for c in chunks}
    result = read_json(run / "quality_notes.json")
    exact = 0
    for claim in result["ledger"]:
        for evidence in claim["evidence"]:
            text = sources[evidence["source_id"]]
            if text[evidence["source_start"]:evidence["source_end"]] != evidence["quote"]:
                raise ValueError("Not an exact source slice")
            if evidence["quote_match"] != "exact_source_slice":
                raise ValueError("Quote was not resolved from a selected source span")
            exact += 1
    review = load_review(run / "candidate_review.json",
                        expected_fingerprint=source_fingerprint(units, chunks))
    metrics = {
        "status": "mechanical_checks_passed_not_semantic_validation",
        "ledger_items": len(result["ledger"]),
        "origins": dict(Counter(c["origin"] for c in result["ledger"])),
        "key_items": sum(c["importance"] == "key" for c in result["ledger"]),
        "exact_source_slices": exact,
        "all_extracted_items_preserved": result["coverage_audit"]["all_extracted_claims_preserved"],
        "missing_after_composition": len(result["coverage_audit"]["missing_claim_ids"]),
        "source_gaps": dict(Counter(r["origin"] for r in result["source_audit"] if not r["claim_ids"])),
        "rejected_claims": len(result["rejected_claims"]),
        "extraction_failures": len(result["failures"]),
        "matches": dict(Counter(i["status"] for i in review["items"])),
        "model_calls": result["model_calls"],
        "freeze_sha256": file_hash(run / "freeze.json"),
    }
    if metrics["missing_after_composition"] or any(i["confirmed_unit_ids"] for i in review["items"]):
        raise ValueError("Unexpected missing item or automatic confirmation")
    return metrics


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--features", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    result = check(args.run, args.features)
    write_json(args.out, result)
    print(result, flush=True)


if __name__ == "__main__":
    main()
