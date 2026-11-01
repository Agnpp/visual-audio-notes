from copy import deepcopy
import json

import pytest

from visual_audio_notes.core import Chunk, Unit, file_hash, write_json
from visual_audio_notes.quality import compose_notes
from visual_audio_notes.review import create_review
from visual_audio_notes.reviewed_output import export_reviewed_archive


def make_inputs(tmp_path, count=3):
    unit = Unit("u_page", "A list can change in place, keeping the same object.")
    chunk = Chunk("t_audio", 0, 40, "The operation returns a new list without changing the input.")
    prepared = tmp_path / "prepared"
    (prepared / "pages").mkdir(parents=True)
    (prepared / "pages/u_page.png").write_bytes(b"local-image")
    write_json(prepared / "features.json", {"units": [unit.__dict__]})
    audio, pdf = tmp_path / "recording.wav", tmp_path / "materials.pdf"
    audio.write_bytes(b"audio")
    pdf.write_bytes(b"%PDF")
    state = create_review([{"segment_id": "t_audio", "status": "accepted", "unit_id": "u_page",
                            "candidates": [{"unit_id": "u_page", "score": .99}]}], [unit], [chunk])
    ledger = []
    for i in range(count):
        source = unit if i % 2 else chunk
        identifier = unit.unit_id if i % 2 else chunk.segment_id
        ledger.append({"claim_id": "q_item_" + str(i), "origin": "courseware" if i % 2 else "audio",
                       "kind": "concept", "importance": "key", "title_draft": "原AI标题" + str(i),
                       "paraphrase_draft": "原AI错误改写" + str(i), "canonical_text": source.text,
                       "evidence": [{"source_id": identifier, "quote": source.text}],
                       "status": "quote_verified_draft"})
    notes, coverage = compose_notes(ledger)
    result = {"ledger": ledger, "notes": notes, "coverage_audit": coverage,
              "source_audit": [
                  {"source_id": identifier, "origin": origin,
                   "claim_ids": [c["claim_id"] for c in ledger if c["origin"] == origin],
                   "status": "has_quote_verified_claims", "reason": "已有可定位引文"}
                  for identifier, origin in [("u_page", "courseware"), ("t_audio", "audio")]
              ]}
    return dict(title="复查阅读包", audio=audio, pdf=pdf, prepared=prepared,
                source_map={"u_page": {"page": 1, "image": "u_page.png"}},
                chunks=[chunk.__dict__], review_state=state, result=result, source_credit="Local fixture")


def row(identifier, verdict="supported", show=True):
    return {"claim_id": identifier, "verdict": verdict, "show_paraphrase": show,
            "reason": "复查说明", "label": "模型标签", "evidence_source_ids": ["u_page" if int(identifier.rsplit('_', 1)[1]) % 2 else "t_audio"]}


def test_rejected_and_unreviewed_drafts_are_hidden_without_losing_originals(tmp_path):
    inputs = make_inputs(tmp_path)
    original = deepcopy(inputs["result"])
    rejected = row("q_item_0", "contradicted", False)
    rejected["reason"] = "原AI错误改写0 不受原文支持；原AI标题0 也不可靠。"
    checked = export_reviewed_archive(tmp_path / "out", **inputs,
                                      semantic_review={"claim_reviews": [rejected, row("q_item_1")]})
    html = (tmp_path / "out/阅读.html").read_text(encoding="utf-8")
    md = (tmp_path / "out/笔记.md").read_text(encoding="utf-8")
    for text in (html, md):
        assert "原AI错误改写0" not in text and "原AI标题0" not in text
        assert "原AI错误改写2" not in text and "原AI标题2" not in text
        assert "原AI错误改写1" in text
        assert "模型复查未发现问题，仍需核对" in text
        assert "原文要点 · 改写待修正" in text
        assert "The operation returns a new list" in text and "A list can change in place" in text
    assert "q_item_" not in md
    assert 'data-claim-id="q_item_0"' in html
    assert json.loads((tmp_path / "out/原始归纳草稿.json").read_text(encoding="utf-8")) == original
    assert inputs["result"] == original
    assert checked["semantic_drafts_displayed"] == 1
    assert checked["semantic_drafts_hidden"] == 2
    assert checked["notes_sha256"] == file_hash(tmp_path / "out/笔记.md")


@pytest.mark.parametrize("problem", ["missing", "duplicate", "unknown", "malformed", "bad_verdict", "wrong_source", "unsupported_show_true", "supported_show_false"])
def test_semantic_review_fails_closed(tmp_path, problem):
    inputs = make_inputs(tmp_path)
    rows = [row(c["claim_id"]) for c in inputs["result"]["ledger"]]
    if problem == "missing":
        rows = rows[1:]
    elif problem == "duplicate":
        rows.append(row("q_item_0"))
    elif problem == "unknown":
        rows.append(row("q_unknown_4"))
    elif problem == "malformed":
        rows = None
    elif problem == "bad_verdict":
        rows[0]["verdict"] = "certainly_true"
    elif problem == "wrong_source":
        rows[0]["evidence_source_ids"] = ["u_page"]
    elif problem == "unsupported_show_true":
        rows[0]["verdict"] = "unsupported"
    else:
        rows[0]["show_paraphrase"] = False
    checked = export_reviewed_archive(tmp_path / "out", **inputs, semantic_review={"claim_reviews": rows})
    md = (tmp_path / "out/笔记.md").read_text(encoding="utf-8")
    assert "原AI错误改写0" not in md and "原AI标题0" not in md
    if problem in {"unknown", "malformed"}:
        assert checked["semantic_drafts_displayed"] == 0


def test_all_53_source_claims_and_origins_survive_review_filtering(tmp_path):
    inputs = make_inputs(tmp_path, 53)
    rows = [row(c["claim_id"], "uncertain", False) for c in inputs["result"]["ledger"]]
    checked = export_reviewed_archive(tmp_path / "out", **inputs, semantic_review={"claim_reviews": rows})
    manifest = json.loads((tmp_path / "out/project.json").read_text(encoding="utf-8"))
    saved = json.loads((tmp_path / "out/质量账本.json").read_text(encoding="utf-8"))
    assert checked["all_ledger_claim_ids_preserved"] and checked["ledger_claims"] == 53
    assert set(manifest["quality"]["claim_ids"]) == {c["claim_id"] for c in inputs["result"]["ledger"]}
    for before, after in zip(inputs["result"]["ledger"], saved["ledger"]):
        for field in ["claim_id", "origin", "canonical_text", "evidence", "status"]:
            assert before[field] == after[field]
    assert saved["source_audit"] == inputs["result"]["source_audit"]
    assert manifest["timeline"][0]["unit_id"] is None
    assert manifest["timeline"][0]["status"] == "needs_review"
    assert len(manifest["notes"]["topics"]) == 53
