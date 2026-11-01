"""Capture archive-source identity before inference; this is not model input."""
import argparse
from pathlib import Path

from visual_audio_notes.binding import create_binding, save_binding


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("prepared", "audio", "pdf", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    binding = create_binding(args.prepared, args.audio, args.pdf)
    save_binding(args.out, binding)
    print({"binding": str(args.out), "document_units": len(binding["unit_ids"]),
           "prepared_files": len(binding["prepared_hashes"]),
           "purpose": "Freeze with inference outputs; display provenance only"}, flush=True)


if __name__ == "__main__":
    main()
