"""Build a content-only ASR-text perturbation; this is not a fresh ASR test."""
from pathlib import Path
import random

from visual_audio_notes.core import read_json, write_json

root = Path(__file__).resolve().parents[1]
features = read_json(root / "data/work/mit_functions/features.json")
segments = [
    {**segment, "start": segment["start"] - 600, "end": segment["end"] - 600}
    for segment in features["segments"]
    if segment["start"] >= 600 and segment["end"] <= 1500
]
units = list(features["units"])
random.Random(713).shuffle(units)
output = root / "data/work/mit_midcrop/features.json"
write_json(output, {"units": units, "segments": segments})
write_json(output.parent / "probe_description.json", {
    "original_audio_offset": 600, "requested_duration": 900,
    "page_order_shuffled_again": True,
    "limitation": "Reuses independent local ASR text; does not measure cropped-audio ASR quality",
    "selection": "Fixed times, before gold inspection, retaining only whole segments",
})
print("Prepared middle-of-recording perturbation without labels or page timing.")
