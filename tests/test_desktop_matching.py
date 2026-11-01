from dataclasses import replace
import json
import subprocess
import sys

import pytest

from visual_audio_notes.core import Chunk, Unit
from visual_audio_notes.desktop_matching import rank_candidates
from visual_audio_notes.review import apply_decision, create_review, refresh_review


def chunk(text, identifier="t_audio", start=12):
    return Chunk(identifier, start, start + 5, text)


def claim(source_id, quote, title, origin="courseware"):
    return {"status": "quote_verified_draft", "origin": origin,
            "title_draft": title, "paraphrase_draft": title,
            "evidence": [{"source_id": source_id, "quote": quote}]}


def test_unique_english_content_is_ranked_but_never_auto_confirmed():
    pages = [Unit("u_other", "Photosynthesis converts light into stored chemical energy."),
             Unit("u_list", "List concatenation creates a new list; append mutates the existing list.")]
    audio = [chunk("List concatenation creates a new list.")]
    result = rank_candidates(pages, audio)
    assert result[0]["candidates"][0]["unit_id"] == "u_list"
    assert result[0]["status"] == "review" and result[0]["unit_id"] is None
    assert result[0]["method"] == "lexical" and not result[0]["uses_ai_derived_terms"]
    assert create_review(result, pages, audio)["items"][0]["status"] == "needs_review"


def test_chinese_does_not_require_word_segmentation_or_external_model():
    pages = [Unit("u_x", "光合作用把光能转化为化学能"), Unit("u_y", "列表拼接返回新列表，追加操作修改原列表")]
    result = rank_candidates(pages, [chunk("列表拼接会返回一个新列表")])
    assert result[0]["candidates"][0]["unit_id"] == "u_y"


def test_duplicate_pages_tie_deterministically_and_keep_multiple_candidates():
    pages = [Unit("u_z", "List append changes an existing list."), Unit("u_a", "List append changes an existing list.")]
    result = rank_candidates(pages, [chunk(pages[0].text)])[0]
    assert [c["unit_id"] for c in result["candidates"]] == ["u_a", "u_z"]
    assert result["candidates"][0]["score"] == result["candidates"][1]["score"]
    assert result["unit_id"] is None


def test_empty_unrelated_and_stopword_only_sources_stay_unmatched():
    assert rank_candidates([Unit("u_empty", "")], [chunk("A meaningful audio statement")])[0]["candidates"] == []
    assert rank_candidates([Unit("u_p", "列表拼接与修改")], [chunk("Photosynthesis sunlight")])[0]["candidates"] == []
    assert rank_candidates([Unit("u_p", "the and a")], [chunk("the a and")])[0]["candidates"] == []
    assert rank_candidates([], [chunk("no pages")])[0]["candidates"] == []


def test_times_and_import_order_do_not_affect_candidate_scores():
    pages = [Unit("u_c", "List append mutation"), Unit("u_a", "Rain ocean clouds")]
    first = rank_candidates(pages, [chunk("List append mutation", start=1)])
    second = rank_candidates(list(reversed(pages)), [chunk("List append mutation", start=99999)])
    assert first == second


def test_ai_draft_vocabulary_can_bridge_languages_with_origin_and_quote_binding():
    pages = [Unit("u_list", "Concatenation creates a new outer list."), Unit("u_light", "Photosynthesis converts light into chemical energy.")]
    audio = [chunk("拼接创建新的外层列表。")]
    assert rank_candidates(pages, audio)[0]["candidates"] == []
    ledger = [claim("u_list", pages[0].text, "拼接创建新的外层列表")]
    result = rank_candidates(pages, audio, ledger=ledger)[0]
    assert result["candidates"][0]["unit_id"] == "u_list"
    assert result["uses_ai_derived_terms"] and result["method"] == "lexical_with_ai_derived_terms"
    assert result["status"] == "review" and result["unit_id"] is None


@pytest.mark.parametrize("change", ["unknown", "wrong_origin", "invented_quote", "not_verified"])
def test_invalid_ai_vocabulary_cannot_change_candidates(change):
    pages, audio = [Unit("u_a", "Original page text.")], [chunk("独立录音内容")]
    entry = claim("u_a", "Original page text.", "独立录音内容")
    if change == "unknown":
        entry["evidence"][0]["source_id"] = "outside_batch"
    elif change == "wrong_origin":
        entry["origin"] = "audio"
    elif change == "invented_quote":
        entry["evidence"][0]["quote"] = "Not in the actual page"
    else:
        entry["status"] = "supported_by_model"
    with pytest.raises(ValueError):
        rank_candidates(pages, audio, ledger=[entry])


def test_top_k_is_bounded_and_user_confirmation_survives_reranking():
    pages = [Unit("u_" + str(i), "List append changes a list number " + str(i)) for i in range(5)]
    audio = [chunk("List append changes a list")]
    predictions = rank_candidates(pages, audio)
    assert len(predictions[0]["candidates"]) == 3
    review = create_review(predictions, pages, audio)
    confirmed = apply_decision(review, audio[0].segment_id, [pages[-1].unit_id], reviewer="fixture-user",
                               expected_fingerprint=review["source_fingerprint"])
    refreshed = refresh_review(confirmed, rank_candidates(pages, audio, {"top_k": 1}), pages, audio)
    assert refreshed["items"][0]["confirmed_unit_ids"] == [pages[-1].unit_id]
    assert refreshed["items"][0]["status"] == "confirmed"


@pytest.mark.parametrize("config", [{"top_k": True}, {"top_k": 0}, {"candidate_score_floor": float("nan")},
                                   {"candidate_score_floor": -1}, {"candidate_max_features": 0}])
def test_invalid_configuration_rejected(config):
    with pytest.raises(ValueError):
        rank_candidates([Unit("u_a", "text")], [chunk("text")], config)


def test_source_identity_must_be_unique_and_disjoint():
    with pytest.raises(ValueError):
        rank_candidates([Unit("same", "one"), Unit("same", "two")], [chunk("audio")])
    with pytest.raises(ValueError):
        rank_candidates([Unit("same", "one")], [chunk("audio", identifier="same")])


def test_importing_candidate_retrieval_does_not_load_ml_libraries():
    result = subprocess.run([sys.executable, "-c", "import json,sys;import visual_audio_notes.desktop_matching;print(json.dumps([n for n in ('numpy','sklearn','torch','sentence_transformers') if n in sys.modules]))"],
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == []
