"""Prepare PDF content and an existing subtitle file without running any model.

The supplied subtitle is user-provided material, not a successful ASR result or
proof of what the recording contains. Cue times stay on their original axis;
audio duration, speech correctness and audio/PDF alignment are not inferred.
"""
from __future__ import annotations

from dataclasses import asdict
from html.parser import HTMLParser
from pathlib import Path
import re
import time

from .core import Segment, Unit, digest, file_hash, normalize, write_json
from .ingest import extract_pdf


_TIME = r"(?:(?P<hours>\d{2,}):)?(?P<minutes>\d{2}):(?P<seconds>\d{2})(?P<separator>[.,])(?P<millis>\d{3})"
_TIME_RE = re.compile(_TIME)
_TIMING_RE = re.compile(r"^(?P<start>\S+)[ \t]*-->[ \t]*(?P<end>\S+)(?:[ \t]+(?P<settings>.*))?$")
_INLINE_VTT_TIME = re.compile(r"<\d{2}:\d{2}(?::\d{2})?\.\d{3}>")
_VOICE = re.compile(r"<v(?:\.[^\s>]+)?[ \t]+([^>]+)>", re.IGNORECASE)
_CUE_TIME_PREFIX = re.compile(r"^(?:\d{2,}:)?\d{2}:\d{2}[.,]\d{3}[ \t]*-->")
_DISPLAY_TAGS = {"b", "i", "u", "c", "v", "ruby", "rt", "lang", "span", "font"}


class _SubtitleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "p", "div"}:
            self.parts.append("\n")
        elif tag not in _DISPLAY_TAGS:
            # Unknown angle-bracket tokens may be code, e.g. List<T>, rather
            # than subtitle styling. Preserve them instead of guessing.
            self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if tag in {"p", "div"}:
            self.parts.append("\n")
        elif tag not in _DISPLAY_TAGS and tag != "br":
            self.parts.append("</" + tag + ">")

    def handle_data(self, data):
        self.parts.append(data)


def _cue_text(lines: list[str]) -> str:
    raw = "\n".join(lines)
    # WebVTT voice annotations carry a speaker name. Keep that meaning as text,
    # while removing display markup and inline karaoke timestamps.
    raw = _VOICE.sub(lambda match: match[1] + ": ", raw)
    raw = _INLINE_VTT_TIME.sub("", raw)
    parser = _SubtitleText()
    parser.feed(raw)
    parser.close()
    text = "\n".join(line.rstrip() for line in "".join(parser.parts).splitlines()).strip()
    if not text:
        raise ValueError("A subtitle cue has no readable text")
    return text


def _seconds(token: str, subtitle_format: str) -> float:
    match = _TIME_RE.fullmatch(token)
    if (not match or (subtitle_format == "srt" and match["hours"] is None)
            or (subtitle_format == "vtt" and match["separator"] != ".")):
        raise ValueError("Malformed subtitle timestamp")
    if int(match["minutes"]) > 59 or int(match["seconds"]) > 59:
        raise ValueError("Subtitle minutes and seconds must be below 60")
    try:
        return float(int(match["hours"] or 0) * 3600 + int(match["minutes"]) * 60 +
                     int(match["seconds"])) + int(match["millis"]) / 1000
    except (OverflowError, ValueError):
        raise ValueError("Subtitle timestamp is outside the supported range") from None


def parse_transcript(path: Path) -> list[Segment]:
    """Read UTF-8 SRT/VTT, keeping original times, multiline text and cue order.

    Equal starts and overlapping speakers are allowed. Decreasing start times,
    invalid or empty cues, and files with no usable cues are rejected instead of
    silently sorted or invented. VTT NOTE/STYLE/REGION blocks and cue positioning
    settings affect display, not the retained time axis. MPEG timestamp maps are
    not applied: this imports the file's own cue times without synchronization.
    """
    path = Path(path)
    subtitle_format = path.suffix.lower().lstrip(".")
    if subtitle_format not in {"srt", "vtt"}:
        raise ValueError("An existing SRT or VTT transcript is required")
    if not path.is_file():
        raise ValueError("Transcript file does not exist")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeError:
        raise ValueError("Transcript must be valid UTF-8 text") from None
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text or "\x00" in text:
        raise ValueError("Transcript is empty or contains invalid text")
    blocks = re.split(r"\n[ \t]*\n", text)
    if subtitle_format == "vtt":
        header = blocks.pop(0).splitlines()
        if not header or not re.fullmatch(r"WEBVTT(?:[ \t].*)?", header[0]):
            raise ValueError("VTT transcript must begin with a WEBVTT header")
        if any("-->" in line for line in header):
            raise ValueError("VTT header and cues must be separated by a blank line")
    transcript_hash = file_hash(path)
    segments: list[Segment] = []
    previous_start = -1.0
    for block_index, block in enumerate(blocks):
        lines = block.splitlines()
        if not lines:
            continue
        first = lines[0].strip()
        if subtitle_format == "vtt" and (
                first == "NOTE" or first.startswith(("NOTE ", "NOTE\t")) or first in {"STYLE", "REGION"}):
            continue
        # Optional SRT numeric sequence or WebVTT cue identifier precedes times.
        timing_index = 0 if "-->" in first else 1
        if timing_index and (len(lines) < 2 or
                             (subtitle_format == "srt" and not first.isdecimal())):
            raise ValueError("Malformed subtitle cue identifier or timing line")
        match = _TIMING_RE.fullmatch(lines[timing_index].strip())
        if not match or (subtitle_format == "srt" and match["settings"]):
            raise ValueError("Malformed subtitle cue timing")
        start, end = _seconds(match["start"], subtitle_format), _seconds(match["end"], subtitle_format)
        payload = lines[timing_index + 1:]
        if not payload or any(_CUE_TIME_PREFIX.match(line.strip()) for line in payload):
            raise ValueError("Subtitle cue text is missing or cues lack a blank separator")
        cleaned = _cue_text(payload)
        segment = Segment.from_wire({
            "segment_id": "s_" + digest([transcript_hash, block_index, start, end, cleaned])[:16],
            "start": start, "end": end, "text": cleaned,
        })
        if start < previous_start:
            raise ValueError("Subtitle cue start times must not decrease")
        previous_start = start
        segments.append(segment)
    if not segments:
        raise ValueError("Transcript contains no usable subtitle cues")
    return segments


def prepare_basic(pdf: Path, audio: Path, transcript: Path, work: Path,
                  seed: int = 20261101) -> dict:
    """Create a new model-free prepared folder from PDF and supplied subtitles.

    Original files are never edited. A blank/scanned PDF page still receives a
    preview and an opaque content ID, but its missing native text is flagged for
    OCR; no text or transcript is manufactured. The resulting feature contract
    contains content and opaque identifiers only, separate from source metadata.
    """
    pdf, audio, transcript, work = map(Path, (pdf, audio, transcript, work))
    if work.exists() or work.is_symlink():
        raise ValueError("Preparation destination exists; use a new folder")
    if type(seed) is not int:
        raise ValueError("Preparation seed must be an integer")
    if pdf.suffix.lower() != ".pdf" or not pdf.is_file():
        raise ValueError("An existing PDF document is required")
    if not audio.is_file() or audio.stat().st_size == 0:
        raise ValueError("An existing nonempty audio file is required")
    segments = parse_transcript(transcript)  # Validate before creating output.
    sources = {"pdf": pdf.resolve(), "audio": audio.resolve(), "transcript": transcript.resolve()}
    source_hashes = {name: file_hash(path) for name, path in sources.items()}
    started = time.perf_counter()
    work.mkdir(parents=True, exist_ok=False)
    try:
        units, source_map = extract_pdf(pdf, work / "pages", seed)
        if (not units or len({u.unit_id for u in units}) != len(units)
                or set(source_map) != {u.unit_id for u in units}):
            raise ValueError("PDF extraction did not produce consistent page content")
        for unit in units:
            Unit.from_wire(asdict(unit))
        if any(file_hash(path) != source_hashes[name] for name, path in sources.items()):
            raise ValueError("An original input changed during preparation")
        empty_ids = [u.unit_id for u in units if not normalize(u.text)]
        write_json(work / "features.json", {
            "units": [asdict(u) for u in units], "segments": [asdict(s) for s in segments],
        })
        write_json(work / "source_map.json", source_map)
        manifest = {
            "schema_version": 1, "mode": "native_pdf_and_supplied_subtitles",
            "uses_ai": False, "model_calls": 0,
            "transcript_kind": "user_provided_transcript",
            "originals": {name: {"path": str(path), "sha256": source_hashes[name],
                                  "bytes": path.stat().st_size} for name, path in sources.items()},
            "seed": seed,
            "limits": ["Subtitle correctness and audio/document correspondence were not verified.",
                       "Cue times are retained without applying external synchronization metadata.",
                       "Audio duration was not measured; cues were not checked against its end time."],
        }
        write_json(work / "basic_sources.json", manifest)
        summary = {
            "status": "prepared", "mode": manifest["mode"], "uses_ai": False, "model_calls": 0,
            "asr_performed": False, "transcript_kind": "user_provided_transcript",
            "transcript_format": transcript.suffix.lower().lstrip("."),
            "transcript_segments": len(segments), "original_cue_times_preserved": True,
            "transcript_quality_verified": False, "audio_duration_checked": False,
            "pages": len(units), "needs_ocr": bool(empty_ids), "empty_text_unit_ids": empty_ids,
            "input_hashes": source_hashes, "total_seconds": round(time.perf_counter() - started, 3),
            "source_manifest": "basic_sources.json",
        }
        write_json(work / "prepare_metrics.json", summary)
        return summary
    except Exception as exc:
        # Preserve partial artifacts for inspection; never mark a failed import
        # as a finished preparation and never delete a user's folder to retry.
        write_json(work / "preparation_failure.json", {
            "status": "failed", "complete": False, "error_type": type(exc).__name__,
        })
        raise
