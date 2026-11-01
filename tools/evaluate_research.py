"""Score frozen inference only. Labels are never imported by the inference CLI."""
import argparse
from pathlib import Path

from visual_audio_notes.core import read_json, write_json
from visual_audio_notes.evaluation import evaluate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--gold-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    summaries = {}
    for name in ("mit_functions", "ntu_ml"):
        run = args.run_root / name
        if not (run / "notes.json").is_file():
            raise ValueError("No complete notes; refusing a complete-pipeline quality score")
        result = evaluate(run, args.gold_root / f"{name}.json",
                          project / f"data/work/{name}/source_map.json",
                          args.output_root / f"{name}.json")
        summaries[name] = result["summary"]
    probe = args.run_root / "mit_midcrop"
    if (probe / "freeze.json").exists():
        original = read_json(args.gold_root / "mit_functions.json")
        derived = {**original, "samples": [
            {**sample, "time": sample["time"] - 600}
            for sample in original["samples"] if 660 <= sample["time"] < 1480
        ], "limitations": [*original.get("limitations", []),
                           "ASR-text crop probe; no fresh cropped-audio transcription"]}
        derived_path = args.gold_root / "mit_midcrop.json"
        write_json(derived_path, derived)
        result = evaluate(probe, derived_path,
                          project / "data/work/mit_functions/source_map.json",
                          args.output_root / "mit_midcrop.json")
        summaries["mit_midcrop"] = result["summary"]
    write_json(args.output_root / "summary.json", summaries)
    print("Scored frozen inference; detailed labels remain in local evaluation artifacts.")


if __name__ == "__main__":
    main()
