import wave

import pytest

from visual_audio_notes import basic
from visual_audio_notes.core import Unit, file_hash, load_features, read_json


def subtitle(tmp_path, text, suffix="srt"):
    path = tmp_path / ("已有 转写." + suffix)
    path.write_bytes(text.encode("utf-8"))
    return path


def test_srt_bom_multiline_and_html_preserve_timing_and_meaning(tmp_path):
    path = subtitle(tmp_path, "\ufeff1\r\n00:01:02,340 --> 00:01:05,678\r\n<b>保留条件</b>\r\na &lt; b<br>第二行\r\n\r\n2\r\n00:01:06.001 --> 00:01:07.002\r\n不是所有方法都返回 None。")
    result = basic.parse_transcript(path)
    assert [(s.start, s.end) for s in result] == [(62.34, 65.678), (66.001, 67.002)]
    assert result[0].text == "保留条件\na < b\n第二行"
    assert result[1].text == "不是所有方法都返回 None。"
    assert basic.parse_transcript(path) == result
    assert len({s.segment_id for s in result}) == 2


def test_vtt_identifier_note_styles_settings_and_speaker_overlap(tmp_path):
    path = subtitle(tmp_path, """WEBVTT Example
Kind: captions

NOTE This is metadata, not a spoken cue.
00:00.000 --> 99:99.999 is not parsed here.

STYLE
::cue {color: blue}

REGION
id:area

speaker-one
01:02.123 --> 01:05.456 position:20% align:start
<v Alice><i>the first speaker</i></v>

speaker-two
01:03.000 --> 01:06.000 line:90%
<v Bob><00:01:03.000>the second speaker</v>
""", "vtt")
    result = basic.parse_transcript(path)
    assert len(result) == 2
    assert result[0].start == 62.123 and result[0].end == 65.456
    assert result[0].text == "Alice: the first speaker"
    assert result[1].text == "Bob: the second speaker"
    assert result[1].start < result[0].end  # Overlap is valid, not compressed.


def test_vtt_mpeg_mapping_does_not_manufacture_a_new_time_axis(tmp_path):
    path = subtitle(tmp_path, "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:900000\n\n00:00:02.000 --> 00:00:03.000\nOriginal time.\n", "vtt")
    result = basic.parse_transcript(path)
    assert result[0].start == 2 and result[0].end == 3


def test_srt_sequence_number_is_optional_and_equal_starts_are_allowed(tmp_path):
    path = subtitle(tmp_path, "00:00:00,000 --> 00:00:02,000\nOne.\n\n2\n00:00:00,000 --> 00:00:01,000\nTwo.")
    result = basic.parse_transcript(path)
    assert len(result) == 2 and result[0].start == result[1].start == 0


def test_plain_arrow_and_generic_code_tokens_are_not_discarded_as_metadata(tmp_path):
    path = subtitle(tmp_path, "1\n00:00:01,000 --> 00:00:02,000\nA --> B uses List<T>, not an invented conversion.")
    assert basic.parse_transcript(path)[0].text == "A --> B uses List<T>, not an invented conversion."


@pytest.mark.parametrize("content,suffix", [
    ("", "srt"), ("WEBVTT\n\nNOTE only metadata\n", "vtt"),
    ("1\n00:00:00,000 --> 00:00:01,000\n<b></b>", "srt"),
    ("1\n00:00:00,000 --> 00:00:01,000", "srt"),
    ("1\n00:00:61,000 --> 00:01:02,000\nBad seconds.", "srt"),
    ("1\n00:60:00,000 --> 01:01:00,000\nBad minutes.", "srt"),
    ("1\n-00:00:01,000 --> 00:00:01,000\nNegative.", "srt"),
    ("1\n00:00:02,000 --> 00:00:01,000\nBackwards.", "srt"),
    ("1\n00:00:01,000 --> 00:00:01,000\nZero interval.", "srt"),
    ("1\nNaN --> Infinity\nNot a timestamp.", "srt"),
    ("1\n00:00:01,0 --> 00:00:02,000\nWrong precision.", "srt"),
    ("00:00:01.000 --> 00:00:02.000\nMissing header.", "vtt"),
    ("WEBVTTBAD\n\n00:00.000 --> 00:01.000\nBad header.", "vtt"),
    ("WEBVTT\n00:00.000 --> 00:01.000\nNo blank header separator.", "vtt"),
    ("WEBVTT\n\n00:00,000 --> 00:01,000\nWrong VTT decimal.", "vtt"),
    ("1\n00:00:00,000 --> 00:00:02,000\nFirst\n2\n00:00:03,000 --> 00:00:04,000\nNo cue separator.", "srt"),
])
def test_invalid_subtitle_structure_or_times_are_not_silently_accepted(tmp_path, content, suffix):
    with pytest.raises(ValueError):
        basic.parse_transcript(subtitle(tmp_path, content, suffix))


def test_decreasing_start_times_are_rejected_instead_of_sorted(tmp_path):
    path = subtitle(tmp_path, "1\n00:00:02,000 --> 00:00:04,000\nFirst.\n\n2\n00:00:01,000 --> 00:00:03,000\nSecond.")
    with pytest.raises(ValueError, match="must not decrease"):
        basic.parse_transcript(path)


def test_non_utf8_and_wrong_extension_are_not_model_corrected(tmp_path):
    path = tmp_path / "not-utf8.srt"
    path.write_bytes(b"\xff\xfe\x00\x00")
    with pytest.raises(ValueError, match="UTF-8"):
        basic.parse_transcript(path)
    path = subtitle(tmp_path, "some text", "txt")
    with pytest.raises(ValueError, match="SRT or VTT"):
        basic.parse_transcript(path)


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    pdf, audio = tmp_path / "课件.pdf", tmp_path / "独立录音.wav"
    pdf.write_bytes(b"synthetic PDF fixture")
    audio.write_bytes(b"synthetic nonempty audio fixture")
    transcript = subtitle(tmp_path, "1\n00:00:12,500 --> 00:00:14,000\nThe input list is not changed.")
    calls = []

    def fake_extract(pdf_path, destination, seed):
        calls.append((pdf_path, destination, seed))
        destination.mkdir(parents=True)
        (destination / "u_text.png").write_bytes(b"fixture preview text")
        (destination / "u_scan.png").write_bytes(b"fixture preview blank")
        return [Unit("u_text", "A new list is returned."), Unit("u_scan", "")], {
            "u_text": {"page": 1, "image": "u_text.png", "original_text": "A new list is returned."},
            "u_scan": {"page": 2, "image": "u_scan.png", "original_text": ""},
        }

    monkeypatch.setattr(basic, "extract_pdf", fake_extract)
    return pdf, audio, transcript, tmp_path / "准备 输出", calls


def test_prepare_basic_is_real_model_free_import_with_separate_metadata(inputs):
    pdf, audio, transcript, work, calls = inputs
    before = {p: file_hash(p) for p in (pdf, audio, transcript)}
    summary = basic.prepare_basic(pdf, audio, transcript, work, seed=47)
    units, segments = load_features(work / "features.json")
    assert len(units) == 2 and len(segments) == 1
    assert segments[0].start == 12.5 and segments[0].end == 14
    assert segments[0].text == "The input list is not changed."
    assert summary["uses_ai"] is False and summary["model_calls"] == 0
    assert summary["asr_performed"] is False
    assert summary["transcript_kind"] == "user_provided_transcript"
    assert summary["transcript_quality_verified"] is False
    assert summary["audio_duration_checked"] is False
    assert summary["needs_ocr"] is True and summary["empty_text_unit_ids"] == ["u_scan"]
    assert all((work / "pages" / name).exists() for name in ("u_text.png", "u_scan.png"))
    assert calls == [(pdf, work / "pages", 47)]
    assert before == {p: file_hash(p) for p in before}
    metrics = read_json(work / "prepare_metrics.json")
    assert metrics["input_hashes"] == {"pdf": file_hash(pdf), "audio": file_hash(audio), "transcript": file_hash(transcript)}
    manifest = read_json(work / "basic_sources.json")
    assert manifest["originals"]["transcript"]["path"] == str(transcript.resolve())
    assert manifest["originals"]["transcript"]["sha256"] == file_hash(transcript)
    assert set(read_json(work / "features.json")) == {"units", "segments"}


def test_bad_transcript_fails_before_output_or_pdf_processing(inputs):
    pdf, audio, transcript, work, calls = inputs
    transcript.write_text("This is not a subtitle file.", encoding="utf-8")
    with pytest.raises(ValueError):
        basic.prepare_basic(pdf, audio, transcript, work)
    assert not work.exists() and calls == []


@pytest.mark.parametrize("nonempty", [False, True])
def test_existing_destination_is_never_overwritten(inputs, nonempty):
    pdf, audio, transcript, work, calls = inputs
    work.mkdir()
    if nonempty:
        (work / "keep.txt").write_text("preserve", encoding="utf-8")
    with pytest.raises(ValueError, match="destination exists"):
        basic.prepare_basic(pdf, audio, transcript, work)
    assert calls == []
    assert sorted(p.name for p in work.iterdir()) == (["keep.txt"] if nonempty else [])


def test_failed_pdf_preparation_does_not_report_completion(inputs, monkeypatch):
    pdf, audio, transcript, work, calls = inputs

    def fail(*args):
        raise ValueError("Fixture corrupt PDF")

    monkeypatch.setattr(basic, "extract_pdf", fail)
    with pytest.raises(ValueError, match="corrupt PDF"):
        basic.prepare_basic(pdf, audio, transcript, work)
    assert read_json(work / "preparation_failure.json")["complete"] is False
    assert not (work / "prepare_metrics.json").exists()
    assert not (work / "features.json").exists()


def test_original_file_changed_during_import_is_detected(inputs, monkeypatch):
    pdf, audio, transcript, work, calls = inputs
    original = basic.extract_pdf

    def changing(*args):
        result = original(*args)
        audio.write_bytes(b"different original during import")
        return result

    monkeypatch.setattr(basic, "extract_pdf", changing)
    with pytest.raises(ValueError, match="input changed"):
        basic.prepare_basic(pdf, audio, transcript, work)
    assert not (work / "prepare_metrics.json").exists()
    assert (work / "preparation_failure.json").exists()


def test_real_blank_pdf_still_gets_a_preview_without_invented_ocr(tmp_path):
    import pypdfium2 as pdfium
    from PIL import Image

    pdf, audio, work = tmp_path / "blank.pdf", tmp_path / "silent.wav", tmp_path / "actual-output"
    document = pdfium.PdfDocument.new()
    page = document.new_page(100, 100)
    page.close()
    document.save(pdf)
    document.close()
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\x00\x00" * 16000)
    transcript = subtitle(tmp_path, "1\n00:00:00,100 --> 00:00:00,500\nProvided text is not an ASR assertion.")
    summary = basic.prepare_basic(pdf, audio, transcript, work)
    units, segments = load_features(work / "features.json")
    assert len(units) == 1 and units[0].text == ""
    assert summary["needs_ocr"] is True and summary["model_calls"] == 0
    assert segments[0].text == "Provided text is not an ASR assertion."
    source_map = read_json(work / "source_map.json")
    preview = work / "pages" / source_map[units[0].unit_id]["image"]
    with Image.open(preview) as image:
        image.verify()
