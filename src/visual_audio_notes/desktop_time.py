"""Check subtitle bounds against original audio metadata without changing time."""
from __future__ import annotations

from dataclasses import asdict
import math
from pathlib import Path
import wave

from .core import Chunk, Segment


def audio_duration(audio: Path | None) -> tuple[float | None, str]:
    if audio is None or not Path(audio).is_file():
        return None, "audio_missing"
    try:
        with wave.open(str(audio), "rb") as stream:
            duration = stream.getnframes() / stream.getframerate()
        if math.isfinite(duration) and duration > 0:
            return duration, "wav_header"
    except (wave.Error, EOFError, OSError, ZeroDivisionError):
        pass
    try:
        import av
        with av.open(str(audio)) as container:
            streams = list(container.streams.audio)
            if not streams:
                return None, "no_audio_stream"
            durations = [float(stream.duration * stream.time_base) for stream in streams
                         if stream.duration is not None and stream.time_base is not None]
            duration = max(durations) if durations else float(container.duration / av.time_base) if container.duration else None
        if duration is not None and math.isfinite(duration) and duration > 0:
            return duration, "audio_container_metadata"
    except Exception:
        # Decoder-specific errors, unavailable codecs and missing binary DLLs
        # all leave this metadata-only check explicitly unchecked. They must not
        # invent a zero duration or alter an otherwise valid imported subtitle.
        pass
    return None, "duration_unavailable"


def check_times(audio: Path | None, segments: list, *, tolerance_seconds: float = .25) -> dict:
    """Allow overlaps, detect out-of-range cues, never infer or shift alignment.

    A successful bounds check only establishes plausible navigation times, not
    that the subtitle words correspond to the original recording. Unreadable
    metadata is reported unchecked, not as a zero-second recording or success.
    """
    if (type(tolerance_seconds) not in (int, float) or not math.isfinite(tolerance_seconds)
            or not 0 <= tolerance_seconds <= 5):
        raise ValueError("Invalid subtitle timing tolerance")
    if not isinstance(segments, list):
        raise ValueError("Subtitle cues must be a list")
    cues = []
    for item in segments:
        value = asdict(item) if isinstance(item, (Segment, Chunk)) else item
        if not isinstance(value, dict):
            raise ValueError("Invalid subtitle cue")
        cues.append(Segment.from_wire(value))
    duration, source = audio_duration(audio)
    outside = []
    if duration is not None:
        for index, cue in enumerate(cues):
            if cue.start >= duration or cue.end > duration + tolerance_seconds:
                outside.append({"index": index, "segment_id": cue.segment_id,
                                "start": cue.start, "end": cue.end, "reason": "outside_original_audio"})
    checked = duration is not None
    if not checked:
        message = "未能读取原录音时长，尚未核对字幕范围；没有自动改动时间。"
    elif outside:
        message = f"有 {len(outside)} 段转写超出原录音时长（{duration:.2f} 秒），请核对字幕是否属于这段录音；没有自动平移或截断时间。"
    else:
        message = "转写时间未超出原录音范围；是否同步及文字是否准确仍需回听核对。"
    return {"checked": checked, "duration_seconds": duration, "duration_source": source,
            "cue_count": len(cues), "tolerance_seconds": tolerance_seconds,
            "within_audio_bounds": not outside if checked else None,
            "out_of_range": outside, "auto_offset_applied": False,
            "synchronization_verified": False, "message": message}
