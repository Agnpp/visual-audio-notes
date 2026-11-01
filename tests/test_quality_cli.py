"""Fixture-only integration checks: never read real runs or evaluation material."""
from dataclasses import asdict
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys

import pytest

from visual_audio_notes import quality_cli
from visual_audio_notes.binding import create_binding
from visual_audio_notes.cli import verify_freeze
from visual_audio_notes.core import (
    Chunk, Segment, Unit, chunks_from_segments, file_hash, read_json, write_json,
)
from visual_audio_notes.review import create_review, load_review, source_fingerprint


def fixture_freeze(run, protocol, features):
    write_json(run / "protocol_snapshot.json", read_json(protocol))
    write_json(run / "freeze.json", {
        "features_sha256": file_hash(features), "code": {},
        "outputs": {p.name: file_hash(p) for p in run.glob("*.json") if p.name != "freeze.json"},
    })


@pytest.fixture
def prepared_run(tmp_path):
    prepared, run = tmp_path / "准备 材料", tmp_path / "研究 运行"
    prepared.mkdir()
    run.mkdir()
    units = [Unit("u_one", "A no-return function returns None."),
             Unit("u_two", "A function can be passed as an argument without invoking it.")]
    segments = [Segment("s_1", 2, 32, "We can pass a function without calling it.")]
    chunks = chunks_from_segments(segments, 40)
    write_json(prepared / "features.json", {
        "units": [asdict(u) for u in units], "segments": [asdict(s) for s in segments],
    })
    predictions = [{"segment_id": chunks[0].segment_id, "status": "accepted", "unit_id": "u_one",
                    "candidates": [{"unit_id": "u_one", "score": .9999}]}]
    write_json(run / "candidate_review.json", create_review(predictions, units, chunks))
    write_json(run / "chunks.json", [asdict(c) for c in chunks])
    write_json(run / "quality_notes.json", {"ledger": [], "source_audit": []})
    protocol = tmp_path / "protocol.json"
    write_json(protocol, {"chunk_seconds": 40})
    audio, pdf, credit = tmp_path / "recording.wav", tmp_path / "materials.pdf", tmp_path / "credit.txt"
    audio.write_bytes(b"fixture audio bytes, no real recording")
    pdf.write_bytes(b"fixture PDF bytes, no course material")
    credit.write_text("Synthetic unit-test material", encoding="utf-8")
    write_json(prepared / "prepare_metrics.json", {
        "input_hashes": {"audio": file_hash(audio), "pdf": file_hash(pdf)},
    })
    write_json(prepared / "source_map.json", {
        u.unit_id: {"page": i + 1, "image": f"{u.unit_id}.png"} for i, u in enumerate(units)
    })
    (prepared / "pages").mkdir()
    for unit in units:
        (prepared / "pages" / f"{unit.unit_id}.png").write_bytes(("fixture image " + unit.unit_id).encode())
    write_json(run / "archive_binding.json", create_binding(prepared, audio, pdf))
    fixture_freeze(run, protocol, prepared / "features.json")
    return SimpleNamespace(prepared=prepared, run=run, units=units, chunks=chunks, audio=audio,
                           pdf=pdf, credit=credit, protocol=protocol)


def review_args(f, **overrides):
    return SimpleNamespace(**{
        "run": f.run, "features": f.prepared / "features.json",
        "segment": f.chunks[0].segment_id, "unit": ["u_two"], "reviewer": "fixture reader",
        "decision": "match", "note": "Checked the source sentence", **overrides,
    })


def archive_args(f, **overrides):
    return SimpleNamespace(**{
        "run": f.run, "prepared": f.prepared, "audio": f.audio, "pdf": f.pdf,
        "out": f.run / "archive", "title": "Fixture notes", "credit": f.credit, **overrides,
    })


@pytest.fixture
def archive_spy(monkeypatch):
    calls = []
    module = ModuleType("visual_audio_notes.quality_output")

    def exporter(*args):
        calls.append(args)
        return {"fixture_export": True}

    module.export_quality_archive = exporter
    monkeypatch.setitem(sys.modules, "visual_audio_notes.quality_output", module)
    return calls


def test_review_cli_writes_edits_without_changing_frozen_predictions(prepared_run, capsys):
    f = prepared_run
    before = {p.name: file_hash(p) for p in f.run.glob("*.json")}
    quality_cli.review(review_args(f))
    quality_cli.review(review_args(f, unit=[], decision="defer", note="Need another listen"))
    after = {p.name: file_hash(p) for p in f.run.glob("*.json")}
    assert after == before
    verify_freeze(f.run)
    original = read_json(f.run / "candidate_review.json")
    assert original["items"][0]["status"] == "needs_review"
    assert not original["items"][0]["confirmed_unit_ids"]
    edits = load_review(f.run / "edits/review.json",
                        expected_fingerprint=source_fingerprint(f.units, f.chunks))
    assert edits["items"][0]["status"] == "needs_review"
    assert [e["review"]["decision"] for e in edits["history"]] == ["match", "defer"]
    assert "source_prediction_modified" in capsys.readouterr().out


def test_review_rejects_changed_source_before_persisting_an_edit(prepared_run):
    f = prepared_run
    features = read_json(f.prepared / "features.json")
    features["segments"][0]["text"] = "The corrected transcript makes a different claim."
    write_json(f.prepared / "features.json", features)
    with pytest.raises(ValueError, match="different sources"):
        quality_cli.review(review_args(f))
    assert not (f.run / "edits/review.json").exists()
    verify_freeze(f.run)


def test_review_rejects_forged_model_actor_in_saved_edits(prepared_run):
    f = prepared_run
    quality_cli.review(review_args(f))
    path = f.run / "edits/review.json"
    edits = read_json(path)
    edits["items"][0]["review"]["actor"] = "model"
    edits["history"][0]["review"]["actor"] = "model"
    write_json(path, edits)
    before = file_hash(path)
    with pytest.raises(ValueError, match="human decision provenance"):
        quality_cli.review(review_args(f))
    assert file_hash(path) == before
    verify_freeze(f.run)


def test_archive_exports_only_candidate_state_until_a_human_action(prepared_run, archive_spy):
    f = prepared_run
    quality_cli.archive(archive_args(f))
    exported_review = archive_spy[0][7]
    assert exported_review["items"][0]["status"] == "needs_review"
    assert not exported_review["items"][0]["confirmed_unit_ids"]
    quality_cli.review(review_args(f))
    quality_cli.archive(archive_args(f))
    exported_review = archive_spy[1][7]
    assert exported_review["items"][0]["status"] == "confirmed"
    assert exported_review["items"][0]["confirmed_unit_ids"] == ["u_two"]
    verify_freeze(f.run)


@pytest.mark.parametrize("changed", ["features", "audio", "pdf"])
def test_archive_blocks_accidentally_changed_inputs(prepared_run, archive_spy, changed):
    f = prepared_run
    path = f.prepared / "features.json" if changed == "features" else getattr(f, changed)
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="differs"):
        quality_cli.archive(archive_args(f))
    assert archive_spy == []


def test_archive_blocks_saved_decision_with_stale_fingerprint(prepared_run, archive_spy):
    f = prepared_run
    stale_units = [Unit(u.unit_id, u.text + " changed") for u in f.units]
    stale = create_review([
        {"segment_id": f.chunks[0].segment_id, "candidates": [{"unit_id": "u_one", "score": .9}]}
    ], stale_units, f.chunks)
    write_json(f.run / "edits/review.json", stale)
    with pytest.raises(ValueError, match="different sources"):
        quality_cli.archive(archive_args(f))
    assert archive_spy == []


def test_archive_must_bind_raw_input_hashes_to_frozen_run_not_mutable_working_metrics(prepared_run, archive_spy):
    f = prepared_run
    f.audio.write_bytes(b"A different recording attached after inference")
    metrics = read_json(f.prepared / "prepare_metrics.json")
    metrics["input_hashes"]["audio"] = file_hash(f.audio)
    write_json(f.prepared / "prepare_metrics.json", metrics)
    with pytest.raises(ValueError, match="source|manifest|differs|frozen"):
        quality_cli.archive(archive_args(f))
    assert archive_spy == []


@pytest.mark.parametrize("changed", ["source_map", "preview_image", "frozen_binding"])
def test_archive_rejects_changed_display_provenance_before_export(prepared_run, archive_spy, changed):
    f = prepared_run
    if changed == "source_map":
        mapping = read_json(f.prepared / "source_map.json")
        mapping["u_one"]["page"] = 99
        write_json(f.prepared / "source_map.json", mapping)
    elif changed == "preview_image":
        (f.prepared / "pages/u_one.png").write_bytes(b"replaced page preview")
    else:
        binding = read_json(f.run / "archive_binding.json")
        binding["input_hashes"]["audio"] = "0" * 64
        write_json(f.run / "archive_binding.json", binding)
    with pytest.raises(ValueError, match="frozen binding|Frozen output changed"):
        quality_cli.archive(archive_args(f))
    assert archive_spy == []


def test_archive_does_not_treat_changed_processing_metrics_as_new_source_authority(prepared_run, archive_spy):
    f = prepared_run
    # Processing timings/cache metadata can change without changing frozen sources.
    write_json(f.prepared / "prepare_metrics.json", {"input_hashes": {"audio": "0" * 64, "pdf": "0" * 64}})
    quality_cli.archive(archive_args(f))
    assert len(archive_spy) == 1


def test_infer_cannot_promote_even_a_misbehaving_legacy_ranker(monkeypatch, tmp_path, prepared_run):
    from visual_audio_notes import align, notes

    f = prepared_run
    features, protocol = tmp_path / "identical features at another path.json", f.protocol
    features.write_bytes((f.prepared / "features.json").read_bytes())
    model = tmp_path / "model.bin"
    model.write_bytes(b"fixture model identity only")
    seen_config = []

    def bad_ranker(units, chunks, config, embedding):
        seen_config.append(config)
        return {"lexical": [{"segment_id": chunks[0].segment_id, "status": "accepted",
                             "unit_id": "u_one", "candidates": [{"unit_id": "u_one", "score": 1.0}]}]}, {}

    monkeypatch.setattr(align, "align", bad_ranker)
    monkeypatch.setattr(notes, "LocalModel", lambda *args: object())
    module = ModuleType("visual_audio_notes.quality")
    module.synthesize_grounded = lambda *args: {
        "ledger": [], "failures": [], "rejected_claims": [], "source_audit": [],
    }
    monkeypatch.setitem(sys.modules, "visual_audio_notes.quality", module)
    monkeypatch.setattr(quality_cli, "freeze", fixture_freeze)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Fixture prompt", encoding="utf-8")
    out = tmp_path / "run"
    quality_cli.infer(SimpleNamespace(out=out, protocol=protocol, features=features,
                      binding=f.run / "archive_binding.json",
                      embedding=None, llm="http://127.0.0.1:1", model="fixture", model_file=model,
                      prompt=prompt, cache=tmp_path / "cache", evaluation_role="development"))
    assert seen_config[0]["automatic_score_floor"] == 1e9
    review = read_json(out / "candidate_review.json")
    assert all(i["status"] == "needs_review" and not i["confirmed_unit_ids"] for i in review["items"])
    assert read_json(out / "quality_metrics.json")["confirmed_matches"] == 0
    assert file_hash(out / "archive_binding.json") == file_hash(f.run / "archive_binding.json")
    assert "archive_binding.json" in read_json(out / "freeze.json")["outputs"]
    verify_freeze(out)


def test_infer_rejects_features_that_differ_from_binding_before_model_calls(monkeypatch, tmp_path, prepared_run):
    from visual_audio_notes import align, notes

    f = prepared_run
    features = tmp_path / "different features.json"
    value = read_json(f.prepared / "features.json")
    value["units"][0]["text"] = "A changed document extraction"
    write_json(features, value)
    called = []
    monkeypatch.setattr(align, "align", lambda *args: called.append("ranker"))
    monkeypatch.setattr(notes, "LocalModel", lambda *args: called.append("model"))
    out = tmp_path / "should not exist"
    with pytest.raises(ValueError, match="binding differs from inference features"):
        quality_cli.infer(SimpleNamespace(out=out, protocol=f.protocol, features=features,
                          binding=f.run / "archive_binding.json"))
    assert called == []
    assert not out.exists()
