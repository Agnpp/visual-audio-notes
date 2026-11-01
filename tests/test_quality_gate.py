"""Fixture-only tests of the boundary between source checks and note quality."""
from copy import deepcopy

import pytest

from visual_audio_notes.quality_gate import build_quality_gate


SHA = "a" * 64


@pytest.fixture
def mechanical():
    return {"status": "mechanical_checks_passed_not_semantic_validation",
            "freeze_sha256": SHA, "all_extracted_items_preserved": True,
            "missing_after_composition": 0, "rejected_claims": 0, "extraction_failures": 0,
            "matches": {"confirmed": 29}, "exact_source_slices": 152}


@pytest.fixture
def reviewed():
    return {"source_freeze_sha256": SHA, "reviewer_kind": "independent_human",
            "reviewer": "Fixture reviewer", "evaluation_role": "holdout",
            "full_claim_review": True,
            "source_review": {"independent_transcript": "complete", "courseware_text": "complete"},
            "expected_essential_ids": ["H1", "H2"],
            "anchors": [{"id": "H1", "status": "full"}, {"id": "H2", "status": "full"}],
            "material_errors": 0, "unsupported_claims": 0}


def test_mechanical_success_and_page_confirmations_do_not_verify_notes(mechanical):
    result = build_quality_gate(mechanical)
    assert result["quality_status"] == "experimental_draft"
    assert result["automatic_publish_allowed"] is False
    assert result["recommended_rendering"] == "evidence_only"


def test_model_support_verdicts_are_not_independent_review(mechanical):
    model = {"source_freeze_sha256": SHA, "reviewer_kind": "automatic_model",
             "rollup": {"supported": 131, "unsupported": 11}, "quality_gate_passed": True}
    result = build_quality_gate(mechanical, model)
    assert result["quality_status"] == "experimental_draft"
    assert not result["automatic_publish_allowed"]


def test_developer_diagnostics_with_errors_require_revision(mechanical):
    review = {"freeze_sha256": SHA, "role": "development_after_unblinding",
              "anchors": [{"id": "H1", "status": "incorrect"},
                          {"id": "H2", "status": "full"}]}
    result = build_quality_gate(mechanical, review)
    assert result["quality_status"] == "needs_revision"
    assert result["essential_anchor_counts"] == {"incorrect": 1, "full": 1}
    assert "essential_anchor_incorrect" in result["reasons"]
    assert result["recommended_rendering"] == "evidence_only"


@pytest.mark.parametrize("field,value", [("source_freeze_sha256", "b" * 64),
                                        ("freeze_sha256", "not-a-hash")])
def test_review_from_other_source_is_rejected(mechanical, reviewed, field, value):
    reviewed[field] = value
    with pytest.raises(ValueError, match="different frozen run"):
        build_quality_gate(mechanical, reviewed)


@pytest.mark.parametrize("status", ["partial", "missing", "incorrect"])
def test_incomplete_or_wrong_essential_concept_blocks_reference(mechanical, reviewed, status):
    reviewed["anchors"][1]["status"] = status
    assert build_quality_gate(mechanical, reviewed)["quality_status"] == "needs_revision"


def test_complete_reference_is_manually_reviewed_not_automatic_publish(mechanical, reviewed):
    result = build_quality_gate(mechanical, reviewed)
    assert result["quality_status"] == "reviewed_reference"
    assert result["automatic_publish_allowed"] is False
    assert result["recommended_rendering"] == "reviewed_reference"
    assert "not authenticated" in result["provenance_limit"]


@pytest.mark.parametrize("field", ["reviewer", "source_freeze_sha256", "full_claim_review",
                                  "source_review", "expected_essential_ids", "material_errors",
                                  "unsupported_claims"])
def test_missing_review_assertion_cannot_promote_reference(mechanical, reviewed, field):
    reviewed.pop(field)
    assert build_quality_gate(mechanical, reviewed)["quality_status"] == "experimental_draft"


@pytest.mark.parametrize("kind", ["collaborating_assistant", "automatic_model", "developer"])
def test_assistant_review_cannot_be_mislabeled_independent_reference(mechanical, reviewed, kind):
    reviewed["reviewer_kind"] = kind
    assert build_quality_gate(mechanical, reviewed)["quality_status"] == "experimental_draft"


def test_post_unblinding_development_is_not_new_holdout_review(mechanical, reviewed):
    reviewed["evaluation_role"] = "development"
    assert build_quality_gate(mechanical, reviewed)["quality_status"] == "experimental_draft"


def test_anchor_subset_cannot_silently_omit_required_concept(mechanical, reviewed):
    reviewed["anchors"].pop()
    assert build_quality_gate(mechanical, reviewed)["quality_status"] == "experimental_draft"


def test_material_error_count_overrides_pass_flag(mechanical, reviewed):
    reviewed.update(material_errors=1, quality_gate_passed=True)
    assert build_quality_gate(mechanical, reviewed)["quality_status"] == "needs_revision"


@pytest.mark.parametrize("field", ["missing_after_composition", "rejected_claims", "extraction_failures"])
def test_mechanical_failures_cannot_be_overridden_by_review(mechanical, reviewed, field):
    mechanical[field] = 1
    assert build_quality_gate(mechanical, reviewed)["quality_status"] == "needs_revision"


@pytest.mark.parametrize("bad", [True, -1, "0", None])
def test_malformed_counts_fail_closed(mechanical, reviewed, bad):
    reviewed["material_errors"] = bad
    with pytest.raises(ValueError, match="Invalid review count"):
        build_quality_gate(mechanical, reviewed)


def test_duplicate_anchor_not_accepted_as_full_coverage(mechanical, reviewed):
    reviewed["anchors"].append(deepcopy(reviewed["anchors"][0]))
    with pytest.raises(ValueError, match="duplicated"):
        build_quality_gate(mechanical, reviewed)


def test_malformed_reviewer_identity_is_not_an_independent_review(mechanical, reviewed):
    reviewed["reviewer_kind"] = ["independent_human"]
    assert build_quality_gate(mechanical, reviewed)["quality_status"] == "experimental_draft"


def test_malformed_anchor_status_rejected(mechanical, reviewed):
    reviewed["anchors"][0]["status"] = {"full": True}
    with pytest.raises(ValueError, match="Malformed"):
        build_quality_gate(mechanical, reviewed)


def test_invalid_source_hash_cannot_make_status_artifact(mechanical):
    mechanical["freeze_sha256"] = "a"
    with pytest.raises(ValueError, match="SHA-256"):
        build_quality_gate(mechanical)


def test_inputs_are_not_rewritten_to_create_a_pass(mechanical, reviewed):
    before = deepcopy((mechanical, reviewed))
    build_quality_gate(mechanical, reviewed)
    assert (mechanical, reviewed) == before
