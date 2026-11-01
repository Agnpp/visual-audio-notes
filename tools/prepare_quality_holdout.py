"""Prepare a fixed new lecture excerpt without opening video frames or captions."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys

from bootstrap_research import download
from visual_audio_notes.core import write_json

ROOT = Path(__file__).resolve().parents[1]


def main():
    name = "mit_collections_holdout"
    pdf = ROOT / f"data/public/{name}/materials.pdf"
    video = ROOT / f"data/oracle/{name}/source.mp4"
    assets = [
        ("https://ocw.mit.edu/courses/6-0001-introduction-to-computer-science-and-programming-in-python-fall-2016/1776670e271578eeb99fc25975f20586_MIT6_0001F16_Lec5.pdf", pdf),
        ("https://archive.org/download/MIT6.0001F16/MIT6_0001F16_Lecture_05_300k.mp4", video),
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(download, url, path) for url, path in assets]
        metadata = [f.result() for f in futures]
    audio = pdf.with_name("recording.wav")
    if not audio.exists():
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-n",
                        "-ss", "600", "-i", str(video), "-t", "1200", "-vn",
                        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio)],
                       check=True)
    write_json(ROOT / f"data/work/{name}/source_manifest.json", {
        "assets": metadata, "audio_crop_seconds": [600, 1800],
        "selection": "fixed before inference; no frames, captions, reference notes or gold opened",
        "license": "MIT OCW CC BY-NC-SA 4.0; local research, not bundled as code",
    })
    subprocess.run([sys.executable, "-m", "visual_audio_notes.cli", "prepare", "--pdf", str(pdf),
                    "--audio", str(audio), "--language", "en", "--work",
                    str(ROOT / f"data/work/{name}"), "--protocol",
                    str(ROOT / "benchmark/quality.v2.json")], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
