"""First selected-provider baseline, on a previously reviewed development source."""
import argparse
from pathlib import Path
import sys

from visual_audio_notes.binding import create_binding, save_binding
from visual_audio_notes.workflow_cli import main as workflow_main

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-id", default="deepseek-development")
    args = p.parse_args()
    if Path(args.run_id).name != args.run_id or args.run_id in (".", ".."):
        raise ValueError("Use a simple new run name")
    out = ROOT / "outputs/provider-v3-20261031" / args.run_id
    if out.exists():
        raise ValueError("Existing baseline is preserved; choose a new run ID")
    prepared = ROOT / "data/work/mit_collections_holdout"
    sources = ROOT / "data/public/mit_collections_holdout"
    binding = ROOT / ".cache" / f"provider-binding-{args.run_id}.json"
    save_binding(binding, create_binding(prepared, sources / "recording.wav", sources / "materials.pdf"))
    sys.argv = ["van-workflow", "infer", "--features", str(prepared / "features.json"),
                "--binding", str(binding), "--out", str(out), "--connection", "deepseek_flash",
                "--evaluation-role", "development"]
    workflow_main()


if __name__ == "__main__":
    main()
