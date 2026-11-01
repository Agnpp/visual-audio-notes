"""Evaluation-only code. This module must never be imported by inference."""
from __future__ import annotations

from pathlib import Path

from .cli import verify_freeze
from .core import read_json, write_json


def evaluate(run: Path, gold_path: Path, source_map_path: Path, output: Path) -> dict:
    frozen = verify_freeze(run)  # Verify predictions before opening any answers.
    gold = read_json(gold_path)
    source_map = read_json(source_map_path)
    chunks = read_json(run / "chunks.json")
    predictions = read_json(run / "predictions.json")
    page = {identifier: value["page"] for identifier, value in source_map.items()}
    total_pages = max(page.values())
    duration = max(c["end"] for c in chunks)
    by_method = {name: {p["segment_id"]: p for p in values}
                 for name, values in predictions.items()}
    baseline_path = run / "llm_baseline.json"
    failures = {}
    if baseline_path.exists():
        baseline = read_json(baseline_path)
        if isinstance(baseline, list):
            by_method["local_llm_direct"] = {
                p["segment_id"]: {
                    "status": "proposed", "unit_id": p["unit_id"],
                    "candidates": [{"unit_id": p["unit_id"]}] if p["unit_id"] else [],
                } for p in baseline
            }
        else:
            failures["local_llm_direct"] = baseline
    rows = []
    for sample in gold["samples"]:
        point = sample["time"]
        distances = [max(c["start"] - point, point - c["end"], 0) for c in chunks]
        index = min(range(len(chunks)), key=lambda i: distances[i])
        if distances[index] > 20:
            rows.append({"time": point, "excluded": "No speech window within 20 seconds"})
            continue
        chunk = chunks[index]
        allowed = sample.get("allowed_pages", [])
        visible = sample.get("visible_page")
        if not allowed and visible is None:
            rows.append({"time": point, "excluded": sample.get("reason", "No reliable label")})
            continue
        if not allowed:
            allowed = [visible]
        row = {"time": point, "segment_id": chunk["segment_id"],
               "visible_page": visible, "allowed_pages": allowed, "methods": {}}
        for name, items in by_method.items():
            prediction = items[chunk["segment_id"]]
            ranked = [page[c["unit_id"]] for c in prediction["candidates"]]
            row["methods"][name] = {
                "top1_page": ranked[0] if ranked else None,
                "exact": (ranked[0] == visible) if visible is not None and ranked else False,
                "exact_scored": visible is not None,
                "concept": bool(ranked and ranked[0] in allowed),
                "top3": any(p in allowed for p in ranked[:3]),
                "accepted": prediction["status"] == "accepted",
            }
        uniform_page = min(total_pages, int(point / duration * total_pages) + 1)
        row["methods"]["uniform_time"] = {
            "top1_page": uniform_page, "exact": uniform_page == visible,
            "exact_scored": visible is not None,
            "concept": uniform_page in allowed, "top3": uniform_page in allowed,
            "accepted": True,
        }
        rows.append(row)
    summary = {}
    names = [*by_method, "uniform_time"]
    for name in names:
        scored = [r["methods"][name] for r in rows if "methods" in r]
        exact = [r for r in scored if r["exact_scored"]]
        accepted = [r for r in scored if r["accepted"]]
        summary[name] = {
            "samples": len(scored), "exact_samples": len(exact),
            "exact_page_accuracy": sum(r["exact"] for r in exact) / len(exact) if exact else None,
            "concept_page_accuracy": sum(r["concept"] for r in scored) / len(scored) if scored else None,
            "top3_concept_recall": sum(r["top3"] for r in scored) / len(scored) if scored else None,
            "accepted_samples": len(accepted),
            "accepted_coverage": len(accepted) / len(scored) if scored else None,
            "accepted_concept_precision": sum(r["concept"] for r in accepted) / len(accepted) if accepted else None,
        }
    result = {"run_freeze": frozen, "label_author": gold.get("label_author"),
              "limitations": gold.get("limitations"), "summary": summary,
              "failures": failures, "samples": rows}
    write_json(output, result)
    return result
