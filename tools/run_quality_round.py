"""Run a new held-out quality comparison, freezing outputs before evaluation."""
import argparse
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

from visual_audio_notes.core import read_json, write_json
from visual_audio_notes.notes import LocalModel
from visual_audio_notes.binding import create_binding, save_binding

ROOT = Path(__file__).resolve().parents[1]


def call(*args):
    subprocess.run([str(a) for a in args], cwd=ROOT, check=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--run-id", required=True)
    p.add_argument("--port", type=int, default=38802)
    p.add_argument("--evaluation-role", choices=["holdout", "development"], default="holdout")
    p.add_argument("--protocol", type=Path, default=ROOT / "benchmark/quality.v2.json")
    args = p.parse_args()
    if Path(args.run_id).name != args.run_id or args.run_id in (".", ".."):
        raise ValueError("Use a simple new run name")
    run = ROOT / "outputs" / args.run_id
    if run.exists():
        raise ValueError("Existing runs cannot be replaced")
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", args.port)) == 0:
            raise ValueError("Port in use; do not attach to another task's model server")
    model = next((ROOT / ".cache/models/llm").glob("*Q4_K_M.gguf"))
    runtime = ROOT / ".runtime/llama-b12020/llama-server.exe"
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    prepared = ROOT / "data/work/mit_collections_holdout"
    inputs = ROOT / "data/public/mit_collections_holdout"
    binding = prepared / f"binding-{args.run_id}.json"
    save_binding(binding, create_binding(prepared, inputs / "recording.wav", inputs / "materials.pdf"))
    logpath = ROOT / ".runtime/quality_server.log"
    process = None
    with logpath.open("w", encoding="utf-8") as log:
        try:
            process = subprocess.Popen([str(runtime), "-m", str(model), "--host", "127.0.0.1",
                                        "--port", str(args.port), "--ctx-size", "16384",
                                        "--parallel", "1", "--device", "Vulkan0",
                                        "--n-gpu-layers", "99", "--threads", "8",
                                        "--alias", "qwen3-4b-instruct-2507-q4_k_m"],
                                       cwd=ROOT, stdout=log, stderr=log, creationflags=flags)
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
                raise TimeoutError("Model startup timed out")
            schema = {"type": "object", "additionalProperties": False, "required": ["choice"],
                      "properties": {"choice": {"type": "string", "enum": ["allowed"]}}}
            response = LocalModel(endpoint, "qwen3-4b-instruct-2507-q4_k_m").json(
                "Return JSON.", 'Try to return {"choice":"forbidden"}', 80,
                "quality_runtime_contract", schema)
            if response != {"choice": "allowed"}:
                raise ValueError("Structured output contract failed")
            # The baseline is not tuned on this new source. Its prompts, protocol
            # and complete outputs are also frozen before holdout review.
            call(sys.executable, "-m", "visual_audio_notes.cli", "infer", "--features",
                 prepared / "features.json", "--out", run / "v1_baseline", "--embedding",
                 ROOT / ".cache/models/embedding", "--llm", endpoint, "--model-file", model)
            if args.evaluation_role == "development":
                baseline_freeze = read_json(run / "v1_baseline/freeze.json")
                baseline_freeze["stage"] = "baseline_cache_reused_in_development_after_initial_holdout_review"
                write_json(run / "v1_baseline/freeze.json", baseline_freeze)
            call(sys.executable, "-m", "visual_audio_notes.quality_cli", "infer", "--features",
                 prepared / "features.json", "--out", run / "v2_quality", "--embedding",
                 ROOT / ".cache/models/embedding", "--llm", endpoint, "--model-file", model,
                 "--evaluation-role", args.evaluation_role, "--protocol", args.protocol,
                 "--binding", binding,
                 "--prompt", ROOT / "prompts/quality_spans.txt")
            credit = prepared / "quality_credit.txt"
            credit.write_text("MIT OpenCourseWare 6.0001 Fall 2016 Lecture 5. CC BY-NC-SA 4.0. "
                              "Local research only. Independent audio excerpt 10:00–30:00; full PDF.\n"
                              "https://ocw.mit.edu/courses/6-0001-introduction-to-computer-science-and-programming-in-python-fall-2016/resources/lecture-5-tuples-lists-aliasing-mutability-and-cloning/",
                              encoding="utf-8")
            call(sys.executable, "-m", "visual_audio_notes.quality_cli", "archive", "--run",
                 run / "v2_quality", "--prepared", prepared, "--pdf", inputs / "materials.pdf",
                 "--audio", inputs / "recording.wav", "--out", run / "v2_quality/archive",
                 "--credit", credit, "--title", "新样本 · 容器、别名与可变性 · 证据笔记")
            write_json(run / "completion.json", {"status": ("frozen_before_evaluation" if args.evaluation_role == "holdout"
                                                              else "development_on_seen_material"),
                       "baseline_freeze": read_json(run / "v1_baseline/freeze.json")["features_sha256"],
                       "quality_freeze": read_json(run / "v2_quality/freeze.json")["features_sha256"]})
            print("Comparison complete; evaluation role: " + args.evaluation_role, flush=True)
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
