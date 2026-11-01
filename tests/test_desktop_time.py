from pathlib import Path
import sys
from types import SimpleNamespace
import wave

import pytest

from visual_audio_notes.core import Segment
from visual_audio_notes.desktop_time import check_times


def audio(path, seconds=3):
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\0\0" * 16000 * seconds)
    return path


def test_wav_bounds_allow_overlap_and_do_not_claim_synchronization(tmp_path):
    source = audio(tmp_path / "audio.wav")
    cues = [Segment("a", 0, 2, "first"), Segment("b", 1, 3, "overlaps")]
    result = check_times(source, cues)
    assert result["checked"] and result["within_audio_bounds"]
    assert result["duration_seconds"] == 3
    assert not result["auto_offset_applied"] and not result["synchronization_verified"]
    assert cues[1].start == 1  # No repair, reordering, or offset.


def test_out_of_range_is_explicit_and_original_cues_are_unchanged(tmp_path):
    source = audio(tmp_path / "audio.wav")
    cues = [Segment("early", .1, 1, "ok"), Segment("late", 2, 4, "too long"), Segment("at_eof", 3, 3.1, "cannot seek here")]
    result = check_times(source, cues)
    assert not result["within_audio_bounds"]
    assert [item["segment_id"] for item in result["out_of_range"]] == ["late", "at_eof"]
    assert cues[1].end == 4 and "没有自动平移" in result["message"]


def test_small_rounding_tolerance_is_bounded_and_explicit(tmp_path):
    source = audio(tmp_path / "audio.wav")
    cues = [Segment("cue", 2, 3.1, "rounding")]
    assert check_times(source, cues)["within_audio_bounds"]
    assert not check_times(source, cues, tolerance_seconds=0)["within_audio_bounds"]


def test_unknown_metadata_is_not_reported_as_zero_or_success(tmp_path, monkeypatch):
    source = tmp_path / "unknown.m4a"
    source.write_bytes(b"not an audio file")
    monkeypatch.setitem(sys.modules, "av", None)
    result = check_times(source, [Segment("cue", 0, 1, "text")])
    assert result["checked"] is False
    assert result["duration_seconds"] is None and result["within_audio_bounds"] is None
    assert check_times(None, [])["duration_source"] == "audio_missing"


def test_compressed_audio_uses_audio_stream_metadata(tmp_path, monkeypatch):
    source = tmp_path / "audio.m4a"
    source.write_bytes(b"fake container fixture")
    class Container:
        streams = SimpleNamespace(audio=[SimpleNamespace(duration=4000, time_base=.001)])
        duration = None
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setitem(sys.modules, "av", SimpleNamespace(open=lambda _: Container(), time_base=1000000))
    result = check_times(source, [Segment("cue", 0, 2, "text")])
    assert result["duration_seconds"] == 4 and result["duration_source"] == "audio_container_metadata"


def test_decoder_specific_failure_is_explicitly_unchecked(tmp_path, monkeypatch):
    source = tmp_path / "audio.m4a"
    source.write_bytes(b"codec fixture")
    def broken(*args):
        raise RuntimeError("decoder-specific fixture error")
    monkeypatch.setitem(sys.modules, "av", SimpleNamespace(open=broken))
    result = check_times(source, [Segment("cue", 0, 1, "text")])
    assert result["checked"] is False and result["duration_seconds"] is None


@pytest.mark.parametrize("tolerance", [True, -1, 6, float("inf")])
def test_invalid_tolerance_is_rejected(tolerance):
    with pytest.raises(ValueError):
        check_times(None, [], tolerance_seconds=tolerance)


def test_invalid_cue_time_cannot_be_hidden_by_unknown_duration():
    with pytest.raises(ValueError):
        check_times(None, [Segment("bad", -1, 0, "text")])
