from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import unicodedata


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = "".join(c if c.isprintable() or c.isspace() else " " for c in text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass(frozen=True)
class Unit:
    unit_id: str
    text: str

    @classmethod
    def from_wire(cls, value: dict) -> "Unit":
        if set(value) != {"unit_id", "text"}:
            raise ValueError("Units accept content and opaque IDs only; metadata is forbidden")
        if not all(isinstance(value[k], str) for k in value) or not value["unit_id"]:
            raise ValueError("Invalid unit")
        return cls(**value)


@dataclass(frozen=True)
class Segment:
    segment_id: str
    start: float
    end: float
    text: str

    @classmethod
    def from_wire(cls, value: dict) -> "Segment":
        if set(value) != {"segment_id", "start", "end", "text"}:
            raise ValueError("Transcript metadata or external alignment is forbidden")
        if not isinstance(value["text"], str) or not isinstance(value["segment_id"], str):
            raise ValueError("Invalid segment text or ID")
        for key in ("start", "end"):
            if isinstance(value[key], bool) or not isinstance(value[key], (int, float)):
                raise ValueError("Invalid segment time")
            if not math.isfinite(value[key]):
                raise ValueError("Non-finite time")
        if value["start"] < 0 or value["end"] <= value["start"]:
            raise ValueError("Invalid segment interval")
        return cls(**value)


@dataclass(frozen=True)
class Chunk:
    segment_id: str
    start: float
    end: float
    text: str


def chunks_from_segments(segments: list[Segment], seconds: float = 40) -> list[Chunk]:
    """Use audio time only to make citation windows, never to estimate a page."""
    if seconds <= 0:
        raise ValueError("Chunk duration must be positive")
    chunks: list[Chunk] = []
    pending: list[Segment] = []
    for segment in sorted(segments, key=lambda s: (s.start, s.end, s.segment_id)):
        if not normalize(segment.text):
            continue
        if pending and segment.start - pending[0].start >= seconds:
            chunks.append(_chunk(pending))
            pending = []
        pending.append(segment)
    if pending:
        chunks.append(_chunk(pending))
    return chunks


def _chunk(segments: list[Segment]) -> Chunk:
    identifier = "t_" + digest([s.segment_id for s in segments])[:12]
    return Chunk(identifier, segments[0].start, max(s.end for s in segments),
                 normalize(" ".join(s.text for s in segments)))


def load_features(path: Path) -> tuple[list[Unit], list[Segment]]:
    data = read_json(path)
    if not isinstance(data, dict) or set(data) != {"units", "segments"}:
        raise ValueError("Only units and independently transcribed segments are allowed")
    units = [Unit.from_wire(u) for u in data["units"]]
    segments = [Segment.from_wire(s) for s in data["segments"]]
    if not units or not segments:
        raise ValueError("Both document content and transcript are required")
    if len({u.unit_id for u in units}) != len(units):
        raise ValueError("Duplicate unit ID")
    if len({s.segment_id for s in segments}) != len(segments):
        raise ValueError("Duplicate segment ID")
    if {u.unit_id for u in units} & {s.segment_id for s in segments}:
        raise ValueError("Document and transcript IDs must be disjoint")
    return units, segments


def wire(value: object) -> dict:
    return asdict(value)
