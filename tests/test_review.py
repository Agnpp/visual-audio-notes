from copy import deepcopy

import pytest

from visual_audio_notes.core import Chunk, Unit
from visual_audio_notes.review import (
    apply_decision, create_review, export_review, load_review, refresh_review,
    save_review, source_fingerprint, validate_review,
)


@pytest.fixture
def materials():
    units = [Unit("u_first", "A function without return gives None."),
             Unit("u_second", "Printing and returning have different purposes."),
             Unit("u_other", "Nested scopes bind names separately.")]
    chunks = [Chunk("t_one", 5, 45, "No explicit return means None."),
              Chunk("t_two", 50, 90, "Printing is different from returning.")]
    predictions = [
        {"segment_id": "t_one", "status": "accepted", "unit_id": "u_first",
         "candidates": [{"unit_id": "u_first", "score": 0.999999}]},
        {"segment_id": "t_two", "status": "accepted", "unit_id": "u_second",
         "candidates": [{"unit_id": "u_second", "score": .88}]},
    ]
    return units, chunks, predictions


def confirm(state, segment="t_one", units=None, **kwargs):
    return apply_decision(state, segment, ["u_first"] if units is None else units,
                          reviewer="课程整理者", expected_fingerprint=state["source_fingerprint"], **kwargs)


def test_even_high_scoring_legacy_accepted_outputs_are_only_candidates(materials):
    units, chunks, predictions = materials
    predictions[0]["review"] = {"actor": "human", "reviewer": "invented by model"}
    state = create_review(predictions, units, chunks)
    assert all(item["status"] == "needs_review" for item in state["items"])
    assert all(not item["confirmed_unit_ids"] and item["review"] is None for item in state["items"])
    assert not state["history"]
    exported = export_review(state)
    assert "not correctness probabilities" in exported["policy"]
    assert all(not item["confirmed_unit_ids"] for item in exported["items"])


def test_human_can_choose_multiple_pages_outside_the_shortlist_and_persist(materials, tmp_path):
    units, chunks, predictions = materials
    original = create_review(predictions, units, chunks)
    state = confirm(original, units=["u_first", "u_other"], note="核对原图和原话，两页都适用")
    assert original["items"][0]["status"] == "needs_review"
    item = state["items"][0]
    assert item["status"] == "confirmed"
    assert item["confirmed_unit_ids"] == ["u_first", "u_other"]
    assert item["review"]["reviewer"] == "课程整理者"
    assert item["review"]["actor"] == "human"
    assert item["review"]["reviewed_at_utc"].endswith("+00:00")
    path = tmp_path / "中文 项目" / "review.json"
    save_review(path, state)
    loaded = load_review(path, expected_fingerprint=state["source_fingerprint"])
    assert loaded == state
    assert export_review(loaded)["items"][0]["confirmed_unit_ids"] == ["u_first", "u_other"]


def test_unmatched_and_deferred_decisions_stay_distinct_and_history_is_retained(materials):
    units, chunks, predictions = materials
    state = create_review(predictions, units, chunks)
    state = confirm(state, units=[], decision="no_match", note="讲者补充，没有对应资料页")
    assert state["items"][0]["status"] == "confirmed_unmatched"
    state = confirm(state, units=[], decision="defer", note="需要再次回听")
    assert state["items"][0]["status"] == "needs_review"
    assert state["items"][0]["confirmed_unit_ids"] == []
    assert state["items"][0]["revision"] == 2
    assert [e["review"]["decision"] for e in state["history"]] == ["no_match", "defer"]


def test_source_order_and_changed_candidate_ranking_do_not_erase_a_human_review(materials):
    units, chunks, predictions = materials
    state = confirm(create_review(predictions, units, chunks))
    updated = deepcopy(predictions)
    updated[0]["candidates"] = [{"unit_id": "u_other", "score": .93}]
    refreshed = refresh_review(state, updated[::-1], units[::-1], chunks[::-1])
    assert source_fingerprint(units, chunks) == source_fingerprint(units[::-1], chunks[::-1])
    assert refreshed["items"][0]["status"] == "confirmed"
    assert refreshed["items"][0]["confirmed_unit_ids"] == ["u_first"]
    assert refreshed["items"][0]["candidates"][0]["unit_id"] == "u_other"
    assert refreshed["history"] == state["history"]


@pytest.mark.parametrize("changed", ["document", "transcript", "time"])
def test_any_changed_source_invalidates_the_displayed_confirmation(materials, changed):
    units, chunks, predictions = materials
    state = confirm(create_review(predictions, units, chunks))
    if changed == "document":
        units[2] = Unit("u_other", "Revised notes distinguish free and local variables.")
    elif changed == "transcript":
        chunks[0] = Chunk("t_one", 5, 45, "Corrected transcript changes the meaning.")
    else:
        chunks[0] = Chunk("t_one", 15, 55, chunks[0].text)
    refreshed = refresh_review(state, predictions, units, chunks)
    assert refreshed["source_revision"] == 1
    assert refreshed["source_fingerprint"] != state["source_fingerprint"]
    assert all(i["status"] == "needs_review" and not i["confirmed_unit_ids"] for i in refreshed["items"])
    assert refreshed["history"][-1]["event"] == "inputs_changed"
    assert refreshed["history"][-1]["invalidated_decisions"] == 1
    # Prior decisions remain an audit trail, not current evidence.
    assert refreshed["history"][0]["review"]["note"] == ""


def test_confirming_again_after_sources_change_uses_current_revision(materials):
    units, chunks, predictions = materials
    original = confirm(create_review(predictions, units, chunks))
    changed = [Unit(u.unit_id, u.text + " revised") for u in units]
    new = refresh_review(original, predictions, changed, chunks)
    with pytest.raises(ValueError, match="Sources changed"):
        apply_decision(new, "t_one", ["u_first"], reviewer="reader",
                       expected_fingerprint=original["source_fingerprint"])
    new = confirm(new, units=["u_other"])
    assert new["items"][0]["review"]["source_revision"] == 1
    assert new["items"][0]["status"] == "confirmed"


@pytest.mark.parametrize("selection,decision", [
    (["unknown"], "match"), (["u_first", "u_first"], "match"),
    ([], "match"), (["u_first"], "no_match"), (["u_first"], "defer"),
])
def test_invalid_selections_do_not_become_confirmations(materials, selection, decision):
    units, chunks, predictions = materials
    state = create_review(predictions, units, chunks)
    with pytest.raises(ValueError):
        confirm(state, units=selection, decision=decision)
    assert not state["history"]


@pytest.mark.parametrize("problem", ["unknown_candidate", "duplicate_segment", "missing_segment", "nan_score"])
def test_malformed_model_predictions_are_rejected(materials, problem):
    units, chunks, predictions = materials
    if problem == "unknown_candidate":
        predictions[0]["candidates"][0]["unit_id"] = "not-in-the-document"
    elif problem == "duplicate_segment":
        predictions[1]["segment_id"] = "t_one"
    elif problem == "missing_segment":
        predictions.pop()
    else:
        predictions[0]["candidates"][0]["score"] = float("nan")
    with pytest.raises(ValueError):
        create_review(predictions, units, chunks)


@pytest.mark.parametrize("problem", ["score_promoted", "model_actor", "missing_event", "modified_selection", "missing_reviewer"])
def test_persisted_fake_or_inconsistent_confirmations_are_rejected(materials, problem, tmp_path):
    units, chunks, predictions = materials
    state = create_review(predictions, units, chunks)
    if problem == "score_promoted":
        state["items"][0].update(status="confirmed", confirmed_unit_ids=["u_first"])
    else:
        state = confirm(state)
        if problem == "model_actor":
            state["items"][0]["review"]["actor"] = "model"
            state["history"][0]["review"]["actor"] = "model"
        elif problem == "missing_event":
            state["history"] = []
        elif problem == "modified_selection":
            state["items"][0]["confirmed_unit_ids"] = ["u_other"]
        else:
            state["items"][0]["review"]["reviewer"] = ""
            state["history"][0]["review"]["reviewer"] = ""
    with pytest.raises(ValueError):
        validate_review(state)
    with pytest.raises(ValueError):
        save_review(tmp_path / "invalid.json", state)
    assert not (tmp_path / "invalid.json").exists()


def test_load_never_silently_reuses_review_of_different_sources(materials, tmp_path):
    units, chunks, predictions = materials
    state = confirm(create_review(predictions, units, chunks))
    path = tmp_path / "review.json"
    save_review(path, state)
    with pytest.raises(ValueError, match="different sources"):
        load_review(path, expected_fingerprint="0" * 64)
