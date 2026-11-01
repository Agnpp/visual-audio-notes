"""Evaluation-only visual evidence, usable only after inference was frozen."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess

from PIL import Image, ImageDraw, ImageFont

from visual_audio_notes.cli import verify_freeze
from visual_audio_notes.core import read_json, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--offset", type=float, default=0)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    verify_freeze(args.run)
    config = read_json(args.run / "protocol_snapshot.json")
    chunks = read_json(args.run / "chunks.json")
    duration = max(chunk["end"] for chunk in chunks)
    points = range(config["evaluation_first_second"],
                   int(duration - config["evaluation_context_seconds"]),
                   config["evaluation_step_seconds"])
    args.out.mkdir(parents=True, exist_ok=True)
    samples = []
    for point in points:
        output = args.out / f"audio_{point:05d}.png"
        if not output.exists():
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-n",
                "-ss", str(point + args.offset), "-i", str(args.video),
                "-frames:v", "1", "-vf", "scale=600:-1", str(output),
            ], check=True)
        samples.append({"time": point, "source_video_time": point + args.offset,
                        "frame": output.name})
    font_path = Path("C:/Windows/Fonts/arial.ttf")
    font = ImageFont.truetype(str(font_path), 18) if font_path.exists() else ImageFont.load_default()
    for first in range(0, len(samples), 6):
        batch = samples[first:first + 6]
        cards = []
        for sample in batch:
            with Image.open(args.out / sample["frame"]) as image:
                card = Image.new("RGB", (600, image.height + 34), "#f4f4f0")
                card.paste(image, (0, 34))
                ImageDraw.Draw(card).text((10, 7), f"audio {sample['time']}s | video {sample['source_video_time']}s",
                                          fill="#111111", font=font)
                cards.append(card)
        height = max(card.height for card in cards)
        sheet = Image.new("RGB", (1200, height * ((len(cards) + 1) // 2)), "white")
        for i, card in enumerate(cards):
            sheet.paste(card, ((i % 2) * 600, (i // 2) * height))
        sheet.save(args.out / f"sheet_{first // 6 + 1:02d}.png")
        for card in cards:
            card.close()
        sheet.close()
    write_json(args.out / "review_points.json", {
        "generated_after_freeze": True,
        "frame_use": "Evaluation only; never supplied to alignment or note generation",
        "samples": samples,
    })
    print(f"Prepared {len(samples)} evaluation frames after verifying frozen inference.", flush=True)


if __name__ == "__main__":
    main()
