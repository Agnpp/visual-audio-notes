from copy import deepcopy
from pathlib import Path

import pytest

from visual_audio_notes.binding import create_binding, save_binding, validate_binding
from visual_audio_notes.core import file_hash, read_json, write_json


@pytest.fixture
def prepared(tmp_path):
    work = tmp_path / "中文 归档"
    (work / "pages").mkdir(parents=True)
    (work / "pages/u_one.png").write_bytes(b"fixture image one")
    (work / "pages/u_two.png").write_bytes(b"fixture image two")
    write_json(work / "features.json", {
        "units": [{"unit_id": "u_one", "text": "Source one"}, {"unit_id": "u_two", "text": "Source two"}],
        "segments": [{"segment_id": "s_one", "start": 0, "end": 30, "text": "Independent recording"}],
    })
    write_json(work / "source_map.json", {
        "u_one": {"page": 1, "image": "u_one.png"}, "u_two": {"page": 2, "image": "u_two.png"},
    })
    audio, pdf = tmp_path / "recording.wav", tmp_path / "materials.pdf"
    audio.write_bytes(b"original fixture audio")
    pdf.write_bytes(b"original fixture document")
    write_json(work / "prepare_metrics.json", {"input_hashes": {"audio": file_hash(audio), "pdf": file_hash(pdf)}})
    return work, audio, pdf


def test_binding_captures_raw_inputs_and_every_referenced_preview(prepared, tmp_path):
    work, audio, pdf = prepared
    binding = create_binding(work, audio, pdf)
    assert set(binding["prepared_hashes"]) == {"features.json", "source_map.json", "pages/u_one.png", "pages/u_two.png"}
    assert binding["input_hashes"] == {"audio": file_hash(audio), "pdf": file_hash(pdf)}
    path = tmp_path / "binding.json"
    save_binding(path, binding)
    assert validate_binding(read_json(path), work, audio, pdf)["valid"]
    with pytest.raises(ValueError, match="exists"):
        save_binding(path, binding)


@pytest.mark.parametrize("changed", ["audio", "pdf"])
def test_mutating_both_raw_input_and_working_metrics_cannot_bypass_frozen_binding(prepared, changed):
    work, audio, pdf = prepared
    binding = create_binding(work, audio, pdf)
    path = audio if changed == "audio" else pdf
    path.write_bytes(b"different source")
    metrics = read_json(work / "prepare_metrics.json")
    metrics["input_hashes"][changed] = file_hash(path)
    write_json(work / "prepare_metrics.json", metrics)
    with pytest.raises(ValueError, match="frozen binding"):
        validate_binding(binding, work, audio, pdf)


@pytest.mark.parametrize("changed", ["page_file", "page_map", "page_number", "features"])
def test_replaced_map_images_or_extracted_content_invalidate_provenance(prepared, changed):
    work, audio, pdf = prepared
    binding = create_binding(work, audio, pdf)
    if changed == "page_file":
        (work / "pages/u_one.png").write_bytes(b"different page image")
    elif changed in {"page_map", "page_number"}:
        source_map = read_json(work / "source_map.json")
        source_map["u_one"]["image" if changed == "page_map" else "page"] = "u_two.png" if changed == "page_map" else 99
        write_json(work / "source_map.json", source_map)
    else:
        features = read_json(work / "features.json")
        features["units"][0]["text"] = "Corrected content"
        write_json(work / "features.json", features)
    with pytest.raises(ValueError, match="frozen binding"):
        validate_binding(binding, work, audio, pdf)


@pytest.mark.parametrize("image", ["../outside.png", "/absolute.png", "C:/outside.png", "nested\\file.png", "nested/../../outside.png"])
def test_page_paths_must_stay_inside_prepared_pages(prepared, image):
    work, audio, pdf = prepared
    source_map = read_json(work / "source_map.json")
    source_map["u_one"]["image"] = image
    write_json(work / "source_map.json", source_map)
    with pytest.raises(ValueError, match="relative path|escapes"):
        create_binding(work, audio, pdf)


def test_binding_refuses_to_capture_sources_different_from_preparation(prepared):
    work, audio, pdf = prepared
    audio.write_bytes(b"wrong audio already present before binding")
    with pytest.raises(ValueError, match="preparation input hashes"):
        create_binding(work, audio, pdf)


@pytest.mark.parametrize("missing", ["map_unit", "extra_map_unit", "image"])
def test_source_map_must_match_actual_units_and_existing_pages(prepared, missing):
    work, audio, pdf = prepared
    source_map = read_json(work / "source_map.json")
    if missing == "map_unit":
        source_map.pop("u_two")
    elif missing == "extra_map_unit":
        source_map["unknown"] = {"page": 3, "image": "u_one.png"}
    else:
        (work / "pages/u_two.png").unlink()
    write_json(work / "source_map.json", source_map)
    with pytest.raises(ValueError):
        create_binding(work, audio, pdf)


def test_deleted_or_forged_bound_resource_sets_are_rejected(prepared):
    work, audio, pdf = prepared
    binding = create_binding(work, audio, pdf)
    missing = deepcopy(binding)
    missing["prepared_hashes"].pop("pages/u_two.png")
    with pytest.raises(ValueError, match="resources differ"):
        validate_binding(missing, work, audio, pdf)
    escaping = deepcopy(binding)
    escaping["prepared_hashes"]["../outside.txt"] = "0" * 64
    with pytest.raises(ValueError, match="relative path"):
        validate_binding(escaping, work, audio, pdf)
