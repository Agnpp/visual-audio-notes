from copy import deepcopy
import json

import pytest

from visual_audio_notes.core import Chunk, Unit, write_json
from visual_audio_notes.quality import compose_notes
from visual_audio_notes.quality_output import export_quality_archive
from visual_audio_notes.review import apply_decision, create_review


@pytest.fixture
def inputs(tmp_path):
    units = [Unit("u_one", "A list is mutable and may change."),
             Unit("u_two", "A tuple is immutable and cannot change.")]
    chunks = [Chunk("t_one", 60, 100, "A list is mutable and may change."),
              Chunk("t_two", 100, 140, "Please wait while I open the editor.")]
    prepared = tmp_path / "prepared"
    (prepared / "pages").mkdir(parents=True)
    for unit in units:
        (prepared / "pages" / f"{unit.unit_id}.png").write_bytes(b"test-page")
    write_json(prepared / "features.json", {"units": [u.__dict__ for u in units]})
    audio, pdf = tmp_path / "audio.wav", tmp_path / "slides.pdf"
    audio.write_bytes(b"test-audio")
    pdf.write_bytes(b"%PDF-test")
    source_map = {u.unit_id: {"page": i + 1, "image": f"{u.unit_id}.png"} for i, u in enumerate(units)}
    predictions = [{"segment_id": c.segment_id, "unit_id": "u_one", "status": "accepted",
                    "candidates": [{"unit_id": "u_one", "score": .99}]} for c in chunks]
    state = create_review(predictions, units, chunks)
    ledger = [
        {"claim_id": "q_audio", "origin": "audio", "kind": "concept", "importance": "key",
         "title_draft": "列表可变", "paraphrase_draft": "模型的解释还需核对。",
         "canonical_text": units[0].text, "evidence": [{"source_id": "t_one", "quote": units[0].text}],
         "status": "quote_verified_draft"},
        {"claim_id": "q_page", "origin": "courseware", "kind": "distinction", "importance": "key",
         "title_draft": "元组不可变", "paraphrase_draft": "资料不等于讲者在本段讲过。",
         "canonical_text": units[1].text, "evidence": [{"source_id": "u_two", "quote": units[1].text}],
         "status": "quote_verified_draft"},
    ]
    notes, coverage = compose_notes(ledger)
    result = {"ledger": ledger, "notes": notes, "coverage_audit": coverage,
              "source_audit": [
                  {"source_id": "t_one", "origin": "audio", "claim_ids": ["q_audio"],
                   "status": "has_quote_verified_claims", "reason": "已有引文"},
                  {"source_id": "t_two", "origin": "audio", "claim_ids": [],
                   "status": "no_verified_claims", "reason": "没有核对要点，需查看原文"},
                  {"source_id": "u_one", "origin": "courseware", "claim_ids": [],
                   "status": "extraction_failed", "reason": "提取失败，不代表没有重点"},
                  {"source_id": "u_two", "origin": "courseware", "claim_ids": ["q_page"],
                   "status": "has_quote_verified_claims", "reason": "已有引文"},
              ], "rejected_claims": []}
    return dict(title="质量样例", audio=audio, pdf=pdf, prepared=prepared, source_map=source_map,
                chunks=[c.__dict__ for c in chunks], review_state=state, result=result,
                source_credit="Local test fixture")


def test_separate_sources_quotes_and_visible_gaps_are_preserved(tmp_path, inputs):
    out = tmp_path / "中文 空格阅读包"
    checked = export_quality_archive(out, **inputs)
    markdown = (out / "笔记.md").read_text(encoding="utf-8")
    document = (out / "阅读.html").read_text(encoding="utf-8")
    assert "录音转写 · 需核对原音" in markdown
    assert "课件补充 · 是否讲过未核对" in markdown
    assert "AI 改写草稿 · 待核对" in markdown
    assert "A list is mutable and may change." in markdown
    assert "来源缺口" in markdown and "录音 01:40–02:20" in markdown and "课件第 1 页" in markdown
    assert "提取失败" in document and "q_audio" in document and "q_page" in document
    assert "**容易混淆**" not in markdown and "<strong>适用边界</strong>" not in document
    assert checked["source_gaps"] == 2 and checked["ledger_claims"] == 2
    assert checked["all_ledger_claim_ids_preserved"]
    assert json.loads((out / "质量账本.json").read_text(encoding="utf-8")) == inputs["result"]


def test_candidates_never_become_playback_associations(tmp_path, inputs):
    checked = export_quality_archive(tmp_path / "out", **inputs)
    manifest = json.loads((tmp_path / "out/project.json").read_text(encoding="utf-8"))
    assert checked["single_page_playback_mappings"] == 0
    assert all(c["unit_id"] is None for c in manifest["timeline"])
    assert all(c["status"] == "needs_review" for c in manifest["timeline"])


def test_single_confirmed_page_can_map_but_multi_page_review_is_not_arbitrarily_collapsed(tmp_path, inputs):
    state = inputs["review_state"]
    state = apply_decision(state, "t_one", ["u_one"], reviewer="fixture human",
                           expected_fingerprint=state["source_fingerprint"])
    state = apply_decision(state, "t_two", ["u_one", "u_two"], reviewer="fixture human",
                           expected_fingerprint=state["source_fingerprint"])
    inputs["review_state"] = state
    checked = export_quality_archive(tmp_path / "out", **inputs)
    manifest = json.loads((tmp_path / "out/project.json").read_text(encoding="utf-8"))
    assert checked["single_page_playback_mappings"] == 1
    assert [c["unit_id"] for c in manifest["timeline"]] == ["u_one", None]
    saved = json.loads((tmp_path / "out/匹配核对记录.json").read_text(encoding="utf-8"))
    assert saved["items"][1]["confirmed_unit_ids"] == ["u_one", "u_two"]


@pytest.mark.parametrize("problem", ["origin", "unknown", "quote", "canonical", "missing_key", "missing_source", "changed_inputs"])
def test_export_rejects_invalid_provenance_or_silent_omission(tmp_path, inputs, problem):
    value = deepcopy(inputs)
    claim = value["result"]["ledger"][0]
    if problem == "origin":
        claim["evidence"][0]["source_id"] = "u_one"
    elif problem == "unknown":
        claim["evidence"][0]["source_id"] = "t_missing"
    elif problem == "quote":
        claim["evidence"][0]["quote"] = "This was never said."
    elif problem == "canonical":
        claim["canonical_text"] = "A fabricated canonical sentence."
    elif problem == "missing_key":
        value["result"]["coverage_audit"]["missing_key_claim_ids"] = ["q_audio"]
    elif problem == "missing_source":
        value["result"]["source_audit"].pop()
    else:
        value["chunks"][0]["text"] += " Changed."
    with pytest.raises(ValueError):
        export_quality_archive(tmp_path / "out", **value)
    assert not (tmp_path / "out").exists()
