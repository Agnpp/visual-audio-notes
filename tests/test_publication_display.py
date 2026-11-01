from copy import deepcopy
import json

import pytest

from test_reviewed_output import make_inputs, row
from visual_audio_notes.reviewed_output import export_reviewed_archive


@pytest.mark.parametrize("status", ["needs_revision", "experimental_draft", "unknown", None])
def test_whole_note_gate_overrides_all_supported_model_reviews(tmp_path, status):
    inputs = make_inputs(tmp_path)
    original = deepcopy(inputs["result"])
    reviews = {"claim_reviews": [row(c["claim_id"]) for c in original["ledger"]]}
    gate = {"quality_status": status, "automatic_publish_allowed": False}
    checks = export_reviewed_archive(tmp_path / "out", **inputs, semantic_review=reviews,
                                     publication_gate=gate)
    for name in ("阅读.html", "笔记.md"):
        text = (tmp_path / "out" / name).read_text(encoding="utf-8")
        assert "整体内容质量未通过" in text
        assert "原AI错误改写" not in text and "原AI标题" not in text
        assert "The operation returns a new list" in text
        assert "A list can change in place" in text
    saved = json.loads((tmp_path / "out/原始归纳草稿.json").read_text(encoding="utf-8"))
    assert original == saved == inputs["result"]
    assert checks["semantic_drafts_displayed"] == 0
    assert checks["semantic_drafts_hidden"] == len(original["ledger"])
    assert checks["all_ledger_claim_ids_preserved"]
    assert checks["single_page_playback_mappings"] == 0
    assert checks["mode"] == "quality_gate_evidence_only"
    manifest = json.loads((tmp_path / "out/project.json").read_text(encoding="utf-8"))
    assert manifest["quality"]["publication_gate"] == gate
