from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from pathlib import Path
import random
import re
import time

from .core import Segment, Unit, digest, file_hash, normalize, read_json, write_json


def extract_pdf(pdf: Path, destination: Path, seed: int) -> tuple[list[Unit], dict]:
    """Keep display locators separate from the content-only feature contract."""
    import pypdfium2 as pdfium
    destination.mkdir(parents=True, exist_ok=True)
    document = pdfium.PdfDocument(pdf)
    raw_pages: list[str] = []
    for page in document:
        text_page = page.get_textpage()
        raw_pages.append(text_page.get_text_range())
        text_page.close()
        page.close()
    frequency = Counter(
        line for page in raw_pages
        for line in set(normalize(l) for l in page.splitlines()) if line
    )
    source_digest = file_hash(pdf)
    units = []
    source_map = {}
    for index, raw in enumerate(raw_pages):
        lines = [normalize(line) for line in raw.splitlines()]
        lines = [line for line in lines if line and not line.isdigit()
                 and not (len(line) < 80 and frequency[line] >= max(3, len(raw_pages) * .6))]
        identifier = "u_" + digest([source_digest, index])[:12]
        units.append(Unit(identifier, normalize(" ".join(lines))))
        image_path = destination / f"{identifier}.png"
        page = document[index]
        bitmap = page.render(scale=min(2.0, 1100 / page.get_width()))
        image = bitmap.to_pil()
        image.save(image_path)
        image.close()
        bitmap.close()
        page.close()
        source_map[identifier] = {"page": index + 1, "image": image_path.name,
                                  "original_text": raw}
    document.close()
    random.Random(seed).shuffle(units)
    return units, source_map


def transcribe(audio: Path, cache: Path, model: str, language: str) -> tuple[list[Segment], dict]:
    import importlib.metadata
    from faster_whisper import WhisperModel
    cache.parent.mkdir(parents=True, exist_ok=True)
    key = digest({"audio": file_hash(audio), "model": file_hash(Path(model) / "model.bin"),
                  "engine": importlib.metadata.version("faster-whisper"), "language": language,
                  "device": "cpu", "compute": "int8", "beam": 5, "vad": True})
    if cache.exists():
        previous = read_json(cache)
        if previous.get("cache_key") == key:
            return [Segment.from_wire(s) for s in previous["segments"]], {
                **previous["timing"], "cache_hit": True, "this_run_seconds": 0,
            }
    started = time.perf_counter()
    recognizer = WhisperModel(model, device="cpu", compute_type="int8",
                               cpu_threads=8, num_workers=1)
    segments, info = recognizer.transcribe(str(audio), language=language,
                                           beam_size=5, vad_filter=True)
    result = []
    for i, segment in enumerate(segments):
        if normalize(segment.text) and segment.end > segment.start:
            result.append(Segment(f"a_{i:05d}", float(segment.start),
                                   float(segment.end), normalize(segment.text)))
        if i and i % 100 == 0:
            print(f"ASR progress: {segment.end:.0f} audio seconds", flush=True)
    elapsed = round(time.perf_counter() - started, 3)
    timing = {"asr_seconds": elapsed, "duration_seconds": info.duration,
              "realtime_factor": round(elapsed / info.duration, 4),
              "cache_hit": False, "this_run_seconds": elapsed,
              "language": info.language, "segments": len(result)}
    write_json(cache, {"cache_key": key, "segments": [asdict(s) for s in result],
                       "timing": timing})
    return result, timing
