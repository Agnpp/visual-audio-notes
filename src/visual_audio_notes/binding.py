"""Bind archive files before inference, without supplying display metadata to models.

This records file identity, not proof that extraction or a source claim is true.
The binding must itself be copied into the run's frozen outputs. Recreating a
binding after replacing files is not a substitute for validating that snapshot.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath

from .core import file_hash, load_features, read_json, write_json


def _sha(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 64 and
            all(c in "0123456789abcdef" for c in value))


def _member(root: Path, relative: object) -> Path:
    if (not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative
            or PurePosixPath(relative).is_absolute()
            or any(part in ("", ".", "..") for part in relative.split("/"))):
        raise ValueError("Binding resource must use a contained relative path")
    resolved_root = root.resolve()
    target = (resolved_root / relative).resolve()
    if not target.is_relative_to(resolved_root) or not target.is_file():
        raise ValueError("Binding resource is missing or escapes the prepared directory")
    return target


def _resources(prepared: Path) -> tuple[list[str], list[str]]:
    features = _member(prepared, "features.json")
    source_map_path = _member(prepared, "source_map.json")
    units, _ = load_features(features)
    identifiers = sorted(unit.unit_id for unit in units)
    source_map = read_json(source_map_path)
    if not isinstance(source_map, dict) or set(source_map) != set(identifiers):
        raise ValueError("Display source map must cover exactly the current document units")
    files = {"features.json", "source_map.json"}
    if (prepared / "basic_manifest.json").is_file():
        files.add("basic_manifest.json")
    if (prepared / "basic_sources.json").is_file():
        files.add("basic_sources.json")
    for name in ("office_sources.json", "recognition_sources.json", "time_checks.json", "corrections.json"):
        if (prepared / name).is_file():
            files.add(name)
    for value in source_map.values():
        if not isinstance(value, dict) or "image" not in value:
            raise ValueError("Display source map lacks a page image")
        image = value["image"]
        # Validate against the pages directory too: pages/../somefile is forbidden.
        _member(prepared / "pages", image)
        files.add("pages/" + image)
        if value.get("ocr_image"):
            _member(prepared, value["ocr_image"])
            files.add(value["ocr_image"])
    return identifiers, sorted(files)


def _validate_schema(binding: object) -> None:
    if (not isinstance(binding, dict) or set(binding) != {
            "schema_version", "input_hashes", "prepared_hashes", "unit_ids"}
            or type(binding["schema_version"]) is not int or binding["schema_version"] != 1):
        raise ValueError("Invalid archive binding schema")
    inputs, prepared, identifiers = binding["input_hashes"], binding["prepared_hashes"], binding["unit_ids"]
    if (not isinstance(inputs, dict) or set(inputs) != {"audio", "pdf"}
            or not all(_sha(value) for value in inputs.values())):
        raise ValueError("Invalid archive input hashes")
    if (not isinstance(prepared, dict) or not {"features.json", "source_map.json"} <= set(prepared)
            or not all(isinstance(key, str) and _sha(value) for key, value in prepared.items())):
        raise ValueError("Invalid prepared resource hashes")
    if (not isinstance(identifiers, list) or not identifiers
            or not all(isinstance(i, str) and bool(i) for i in identifiers)
            or len(set(identifiers)) != len(identifiers)):
        raise ValueError("Invalid bound document identifiers")


def create_binding(prepared: Path, audio: Path, pdf: Path) -> dict:
    """Capture actual files, requiring agreement with the preparation's raw inputs."""
    identifiers, files = _resources(prepared)
    actual_inputs = {"audio": file_hash(audio), "pdf": file_hash(pdf)}
    metrics = read_json(_member(prepared, "prepare_metrics.json"))
    declared = metrics.get("input_hashes") if isinstance(metrics, dict) else None
    if (not isinstance(declared, dict) or not set(actual_inputs) <= set(declared)
            or set(declared) - {"audio", "pdf", "transcript"}
            or any(not _sha(value) for value in declared.values())
            or any(declared.get(name) != value for name, value in actual_inputs.items())):
        raise ValueError("Actual audio/PDF differs from the preparation input hashes")
    binding = {"schema_version": 1, "input_hashes": actual_inputs,
               "prepared_hashes": {name: file_hash(_member(prepared, name)) for name in files},
               "unit_ids": identifiers}
    validate_binding(binding, prepared, audio, pdf)
    return binding


def validate_binding(binding: dict, prepared: Path, audio: Path, pdf: Path) -> dict:
    """Validate a *frozen* binding, never accepting mutable metrics as replacement."""
    _validate_schema(binding)
    actual_inputs = {"audio": file_hash(audio), "pdf": file_hash(pdf)}
    if actual_inputs != binding["input_hashes"]:
        raise ValueError("Archive audio/PDF differs from the frozen binding")
    for name, expected in binding["prepared_hashes"].items():
        if file_hash(_member(prepared, name)) != expected:
            raise ValueError("Prepared resource differs from the frozen binding: " + name)
    identifiers, files = _resources(prepared)
    if identifiers != sorted(binding["unit_ids"]) or set(files) != set(binding["prepared_hashes"]):
        raise ValueError("Prepared source map/resources differ from the frozen binding")
    return {"valid": True, "raw_sources": 2, "prepared_files": len(files),
            "verified": "File identities and contained paths; not semantic correctness"}


def save_binding(path: Path, binding: dict) -> None:
    """Write a new binding; a previous snapshot cannot be silently replaced."""
    _validate_schema(binding)
    if path.exists():
        raise ValueError("Binding destination exists; use a new snapshot path")
    write_json(path, binding)
