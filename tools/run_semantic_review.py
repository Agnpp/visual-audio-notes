"""Development-only semantic triage after the held-out results were inspected."""
import argparse
from dataclasses import asdict
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import time
import urllib.request

from visual_audio_notes.binding import validate_binding
from visual_audio_notes.cli import freeze, verify_freeze
from visual_audio_notes.core import Chunk, file_hash, load_features, read_json, write_json
from visual_audio_notes.notes import LocalModel
from visual_audio_notes.review import load_review, source_fingerprint

ROOT = Path(__file__).resolve().parents[1]


def main():
    from visual_audio_notes.semantic_review import audit_grounded
    from visual_audio_notes.reviewed_output import export_reviewed_archive
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--prepared", type=Path, required=True)
    p.add_argument("--pdf", type=Path, required=True)
    p.add_argument("--audio", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--port", type=int, default=38803)
    args = p.parse_args()
    if args.out.exists():
        raise ValueError("Existing review results are preserved")
    source_freeze = verify_freeze(args.run)
    features = args.prepared / "features.json"
    if file_hash(features) != source_freeze["features_sha256"]:
        raise ValueError("Features changed")
    validate_binding(read_json(args.run / "archive_binding.json"), args.prepared, args.audio, args.pdf)
    units, _ = load_features(features)
    chunks = [Chunk(**c) for c in read_json(args.run / "chunks.json")]
    result = read_json(args.run / "quality_notes.json")
    config_path = ROOT / "benchmark/semantic.review.v1.json"
    config = read_json(config_path)
    state = load_review(args.run / "candidate_review.json",
                        expected_fingerprint=source_fingerprint(units, chunks))
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", args.port)) == 0:
            raise ValueError("Port belongs to another process")
    args.out.mkdir(parents=True)
    model_file = next((ROOT / ".cache/models/llm").glob("*Q4_K_M.gguf"))
    process = None
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    with (args.out / "model-server.log").open("w", encoding="utf-8") as log:
        try:
            process = subprocess.Popen([str(ROOT / ".runtime/llama-b12020/llama-server.exe"),
                                        "-m", str(model_file), "--host", "127.0.0.1", "--port",
                                        str(args.port), "--ctx-size", "16384", "--parallel", "1",
                                        "--device", "Vulkan0", "--n-gpu-layers", "99", "--threads", "8",
                                        "--alias", "qwen3-4b-instruct-2507-q4_k_m"], cwd=ROOT,
                                       stdout=log, stderr=log, creationflags=flags)
            endpoint = f"http://127.0.0.1:{args.port}"
            for _ in range(240):
                if process.poll() is not None:
                    raise RuntimeError("Local model failed to start")
                try:
                    with urllib.request.urlopen(endpoint + "/health", timeout=2):
                        break
                except OSError:
                    time.sleep(1)
            else:
                raise TimeoutError("Local model startup timed out")
            model = LocalModel(endpoint, "qwen3-4b-instruct-2507-q4_k_m", file_hash(model_file))
            audit = audit_grounded(result, units, chunks, model,
                                    (ROOT / "prompts/semantic_review.txt").read_text(encoding="utf-8"),
                                    ROOT / ".cache/semantic_review", config)
            audit["evaluation_role"] = "development_after_holdout_unblinding_not_a_new_blind_score"
            audit["source_freeze_sha256"] = file_hash(args.run / "freeze.json")
            write_json(args.out / "semantic_assessment.json", audit)
            checks = export_reviewed_archive(
                args.out / "archive", "证据笔记 · 已加入改写风险复查", args.audio, args.pdf,
                args.prepared, read_json(args.prepared / "source_map.json"),
                [asdict(c) for c in chunks], state, result,
                (args.prepared / "quality_credit.txt").read_text(encoding="utf-8"),
                semantic_review=audit)
            write_json(args.out / "archive_result.json", checks)
            freeze(args.out, config_path, features)
            frozen = read_json(args.out / "freeze.json")
            script = ROOT / "tools/run_semantic_review.py"
            target = args.out / "source_snapshot/tools/run_semantic_review.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(script, target)
            frozen["code"]["tools/run_semantic_review.py"] = file_hash(script)
            frozen["stage"] = "development_semantic_triage_after_holdout_unblinding"
            frozen["source_run_freeze_sha256"] = file_hash(args.run / "freeze.json")
            write_json(args.out / "freeze.json", frozen)
            print({"review": audit["rollup"], "archive": checks}, flush=True)
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


if __name__ == "__main__":
    main()
