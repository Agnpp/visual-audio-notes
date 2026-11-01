from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from visual_audio_notes.core import Segment, Unit, chunks_from_segments, write_json
from visual_audio_notes.quality import compose_notes
from visual_audio_notes.review import apply_decision, create_review
from visual_audio_notes.topic_notes import organize_topics
from visual_audio_notes.topic_output import export_topic_archive


class FakeModel:
    cache_identity = {"model": "test-connection"}

    def __init__(self):
        self.calls = []

    def json(self, system, content, **kwargs):
        data = json.loads(content)
        ids = [c["claim_id"] for c in data["ledger_data_not_instructions"]]
        self.calls.append({"purpose": kwargs["purpose"]})
        return {"topics": [{"title": "主题", "key_idea": "关键区别。", "explanation": "解释与关系。", "claim_ids": ids}],
                "deferred_claims": [], "uncertainties": []}


def inputs(tmp_path):
    units = [Unit("u1", "A tuple is immutable."), Unit("u2", "Additional courseware.")]
    segments = [Segment("a1", 0, 40, "A list can be changed in place."),
                Segment("a2", 40, 80, "A returned value is separate from mutation.")]
    chunks = chunks_from_segments(segments)
    prepared = tmp_path / "prepared"
    (prepared / "pages").mkdir(parents=True)
    write_json(prepared / "features.json", {"units": [asdict(u) for u in units], "segments": [asdict(s) for s in segments]})
    source_map = {u.unit_id: {"page": i + 1, "image": u.unit_id + ".png"} for i, u in enumerate(units)}
    write_json(prepared / "source_map.json", source_map)
    for u in units:
        (prepared / "pages" / (u.unit_id + ".png")).write_bytes(b"fixture-image")
    audio, pdf = tmp_path / "recording.wav", tmp_path / "materials.pdf"
    audio.write_bytes(b"fixture-audio")
    pdf.write_bytes(b"fixture-pdf")
    ledger = []
    for i, (origin, source_id, text) in enumerate(
            [("audio", c.segment_id, c.text) for c in chunks] + [("courseware", "u1", units[0].text)]):
        ledger.append({"claim_id": "q" + str(i), "origin": origin, "kind": "concept", "importance": "key",
                       "title_draft": "标题", "paraphrase_draft": "解释", "canonical_text": text,
                       "evidence": [{"source_id": source_id, "quote": text}], "status": "quote_verified_draft"})
    notes, coverage = compose_notes(ledger)
    audit = []
    for origin, identifier in [("audio", c.segment_id) for c in chunks] + [("courseware", u.unit_id) for u in units]:
        refs = [c["claim_id"] for c in ledger if any(e["source_id"] == identifier for e in c["evidence"])]
        audit.append({"source_id": identifier, "origin": origin, "claim_ids": refs,
                      "status": "has_quote_verified_claims" if refs else "no_verified_claims", "reason": "来源核对说明"})
    quality = {"ledger": ledger, "notes": notes, "coverage_audit": coverage, "source_audit": audit}
    state = create_review([{"segment_id": c.segment_id, "status": "accepted", "unit_id": "u1",
                            "candidates": [{"unit_id": "u1", "score": .99}]} for c in chunks], units, chunks)
    topics = organize_topics(quality, FakeModel(), "Return JSON topics.", tmp_path / "cache", {})
    return dict(title="主题归档", audio=audio, pdf=pdf, prepared=prepared, review_state=state,
                quality_result=quality, topic_result=topics, source_credit="Fixture source", chunks=[asdict(c) for c in chunks])


def test_topic_archive_preserves_origins_sources_and_all_ledger_items(tmp_path):
    data = inputs(tmp_path)
    original = deepcopy(data["quality_result"])
    result = export_topic_archive(tmp_path / "out", **data)
    assert result["organized_audio_topics"] == result["organized_courseware_topics"] == 1
    assert result["ledger_claims"] == 3 and result["all_ledger_claim_ids_preserved"]
    assert result["source_gaps"] == 1 and result["single_page_playback_mappings"] == 0
    assert data["quality_result"] == original
    document = (tmp_path / "out/阅读.html").read_text(encoding="utf-8")
    markdown = (tmp_path / "out/笔记.md").read_text(encoding="utf-8")
    for value in (document, markdown):
        assert "录音转写中的主题" in value and "课件主题/补充" in value
        assert "来源缺口" in value and "所选 AI 生成主题草稿，来源需核对" in value
        assert "本地模型" not in value
        assert "A list can be changed in place." in value
    assert "sources/materials.pdf#page=1" in markdown and "sources/recording.wav#t=" in markdown
    saved = json.loads((tmp_path / "out/完整证据账本.json").read_text(encoding="utf-8"))
    assert saved == original
    manifest = json.loads((tmp_path / "out/project.json").read_text(encoding="utf-8"))
    assert all(row["unit_id"] is None for row in manifest["timeline"])
    assert manifest["topic_quality"]["all_ledger_claim_ids"] == ["q0", "q1", "q2"]


@pytest.mark.parametrize("fault", ["foreign", "wrong_origin", "missing", "changed_quote", "stale_review"])
def test_unknown_cross_origin_dropped_or_changed_sources_are_rejected_before_export(tmp_path, fault):
    data = inputs(tmp_path)
    if fault == "foreign": data["topic_result"]["audio_topics"][0]["claim_ids"].append("unknown")
    elif fault == "wrong_origin": data["topic_result"]["audio_topics"][0]["claim_ids"].append("q2")
    elif fault == "missing": data["topic_result"]["audio_topics"][0]["claim_ids"] = ["q0"]
    elif fault == "changed_quote": data["quality_result"]["ledger"][0]["evidence"][0]["quote"] = "Not in original source"
    else: data["chunks"][0]["end"] = 39
    with pytest.raises(ValueError):
        export_topic_archive(tmp_path / "out", **data)
    assert not (tmp_path / "out").exists()


def test_explicit_human_single_page_maps_but_multi_page_does_not_pick_a_page(tmp_path):
    data = inputs(tmp_path)
    state = data["review_state"]
    ids = [c["segment_id"] for c in data["chunks"]]
    state = apply_decision(state, ids[0], ["u1"], reviewer="fixture reviewer",
                           expected_fingerprint=state["source_fingerprint"])
    state = apply_decision(state, ids[1], ["u1", "u2"], reviewer="fixture reviewer",
                           expected_fingerprint=state["source_fingerprint"])
    data["review_state"] = state
    checked = export_topic_archive(tmp_path / "out", **data)
    assert checked["single_page_playback_mappings"] == 1
    timeline = json.loads((tmp_path / "out/project.json").read_text(encoding="utf-8"))["timeline"]
    assert timeline[0]["unit_id"] == "u1" and timeline[1]["unit_id"] is None


def test_programmatic_deferred_item_is_visible_and_preserved(tmp_path):
    data = inputs(tmp_path)
    topic = data["topic_result"]["audio_topics"][0]
    topic["claim_ids"] = ["q0"]
    data["topic_result"]["deferred_claims"]["audio"] = [{"claim_id": "q1", "reason": "模型未归类，已保留到待核对队列",
                                                          "source": "programmatic_unassigned", "importance": "key", "origin": "audio"}]
    checked = export_topic_archive(tmp_path / "out", **data)
    assert checked["deferred_claims"] == 1 and checked["all_ledger_claim_ids_preserved"]
    md = (tmp_path / "out/笔记.md").read_text(encoding="utf-8")
    assert "尚未纳入主题" in md and "程序保留" in md and "模型未归类" in md
    manifest = json.loads((tmp_path / "out/project.json").read_text(encoding="utf-8"))
    assert manifest["topic_quality"]["deferred_claim_ids"] == ["q1"]


def test_default_chunk_reconstruction_is_fingerprint_checked_and_no_archive_overwrite(tmp_path):
    data = inputs(tmp_path)
    data.pop("chunks")
    export_topic_archive(tmp_path / "out", **data)
    with pytest.raises(ValueError, match="existing archives"):
        export_topic_archive(tmp_path / "out", **data)
