"""Discover selectable models from the user's configured connection."""
from pathlib import Path
import argparse
import sys

from visual_audio_notes.core import write_json
from visual_audio_notes.model_selection import build_model_selector
from visual_audio_notes.provider_discovery import list_connection_models
from visual_audio_notes.workflow_cli import ROOT, connection_from_file


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    if args.out.exists():
        raise ValueError("Use a new discovery output")
    result = list_connection_models(connection_from_file(ROOT / "configs/providers.example.json", "deepseek_flash"))
    supported = {name: ["off", "low", "high", "max"] for name in ("deepseek-flash", "deepseek-v4-pro")}
    selector = build_model_selector("deepseek_flash", result["models"], supported,
                                    default_model="deepseek-flash")
    write_json(args.out, {"discovery": result, "selector": selector,
                         "scope": "Real model availability; no ASR/OCR/embedding/quality claim"})
    print({"discovery": result, "selector": selector}, flush=True)


if __name__ == "__main__":
    main()
