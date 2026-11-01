from dataclasses import asdict
import math
import random

import numpy as np
import pytest

from visual_audio_notes.align import align, rank_scores
from visual_audio_notes.core import Chunk, Segment, Unit, chunks_from_segments, load_features, write_json


CONFIG = {"automatic_score_floor": .35, "automatic_margin_floor": .04,
          "top_k": 3, "semantic_weight": .7, "lexical_weight": .3}


def test_import_order_and_audio_offset_do_not_determine_pages():
    units = [Unit("u_z", "A red train travels on railway tracks."),
             Unit("u_a", "Green apples grow on fruit trees."),
             Unit("u_m", "Rain fills the river with flowing water.")]
    chunks = [Chunk("t_1", 600, 620, "The fruit trees grow green apples."),
              Chunk("t_2", 650, 670, "Railway tracks carry a red train."),
              Chunk("t_3", 710, 730, "Green apples and fruit trees again.")]
    expected = ["u_a", "u_z", "u_a"]  # Includes a return to a previous document.
    first, _ = align(units, chunks, CONFIG)
    shuffled = list(units)
    random.Random(9).shuffle(shuffled)
    shifted = [Chunk(c.segment_id, c.start + 9000, c.end + 9000, c.text) for c in chunks]
    second, _ = align(shuffled, shifted, CONFIG)
    assert [p["candidates"][0]["unit_id"] for p in first["lexical"]] == expected
    assert [p["candidates"] for p in first["lexical"]] == [p["candidates"] for p in second["lexical"]]


@pytest.mark.parametrize("field", ["gold", "slide_times", "source_map", "chapters"])
def test_answers_and_source_metadata_are_rejected(tmp_path, field):
    path = tmp_path / "features.json"
    write_json(path, {"units": [{"unit_id": "u_a", "text": "x", field: []}],
                      "segments": [asdict(Segment("a_1", 0, 1, "x"))]})
    with pytest.raises(ValueError, match="metadata"):
        load_features(path)


def test_duplicate_pages_and_tied_scores_remain_reviewable():
    units = [Unit("u_a", "identical"), Unit("u_b", "identical")]
    result = rank_scores(units, [Chunk("t_a", 0, 3, "speech")],
                         np.array([[.9, .9]]), CONFIG, "test")
    assert result[0]["status"] == "review"
    assert result[0]["unit_id"] is None
    assert len(result[0]["candidates"]) == 2


def test_silence_is_not_removed_from_original_time_axis():
    segments = [Segment("a_1", 1, 3, "first"), Segment("a_2", 120, 125, "second")]
    chunks = chunks_from_segments(segments, 40)
    assert [(c.start, c.end) for c in chunks] == [(1, 3), (120, 125)]
    assert segments[1].start == 120


@pytest.mark.parametrize("start,end", [(math.nan, 3), (0, math.inf), (-1, 2), (2, 1)])
def test_invalid_times_are_rejected(start, end):
    with pytest.raises(ValueError):
        Segment.from_wire({"segment_id": "a", "text": "x", "start": start, "end": end})
