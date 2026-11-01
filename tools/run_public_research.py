"""Reproduce public-input inference and archives without ever reading oracle labels."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def call(*arguments: object) -> None:
    command = [str(argument) for argument in arguments]
    subprocess.run(command, cwd=ROOT, check=True)


def make_audio(name: str) -> None:
    target = ROOT / f"data/public/{name}/recording.wav"
    if target.exists():
        return
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-n"]
    if name == "ntu_ml":
        command += ["-ss", "180"]
    command += ["-i", str(ROOT / f"data/oracle/{name}/source.mp4")]
    if name == "ntu_ml":
        command += ["-t", "1200"]
    command += ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(target)]
    call(*command)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("blind-v1-%Y%m%dT%H%M%SZ"))
    parser.add_argument("--datasets", nargs="+", choices=["mit_functions", "ntu_ml"],
                        default=["mit_functions", "ntu_ml"])
    parser.add_argument("--skip-bootstrap", action="store_true")
    parser.add_argument("--port", type=int, default=38801)
    args = parser.parse_args()
    output = ROOT / "outputs" / args.run_id
    if output.exists():
        raise SystemExit("Choose a new run ID; existing runs are preserved")
    if not args.skip_bootstrap:
        call(sys.executable, ROOT / "tools/bootstrap_research.py")
    model = next((ROOT / ".cache/models/llm").glob("*Q4_K_M.gguf"), None)
    if model is None:
        raise SystemExit("Local summary model is not downloaded")
    # Never attach to, replace, or stop a model server owned by another task.
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", args.port)) == 0:
            raise SystemExit("Research model port is already in use; choose another port")
    runtime = ROOT / ".runtime/llama-b12020/llama-server.exe"
    log_path = ROOT / ".runtime/llama_server.log"
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    process = None
    datasets = json.loads((ROOT / "benchmark/datasets.json").read_text(encoding="utf-8"))
    with log_path.open("w", encoding="utf-8") as log:
        try:
            process = subprocess.Popen([
                str(runtime), "-m", str(model), "--host", "127.0.0.1",
                "--port", str(args.port), "--ctx-size", "32768", "--parallel", "1",
                "--device", "Vulkan0", "--n-gpu-layers", "99", "--threads", "8",
                "--alias", "qwen3-4b-instruct-2507-q4_k_m",
            ], cwd=ROOT, stdout=log, stderr=log, creationflags=flags)
            endpoint = f"http://127.0.0.1:{args.port}"
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("Local server failed; inspect .runtime/llama_server.log")
                try:
                    with urllib.request.urlopen(endpoint + "/health", timeout=2) as response:
                        if response.status == 200:
                            break
                except Exception:
                    pass
                time.sleep(1)
            else:
                raise TimeoutError("Local model startup timed out")
            # A live contract check prevents silently ignored schema fields.
            from visual_audio_notes.notes import LocalModel
            probe = LocalModel(endpoint, "qwen3-4b-instruct-2507-q4_k_m")
            choice_schema = {
                "type": "object", "additionalProperties": False,
                "required": ["choice"],
                "properties": {"choice": {"type": "string", "enum": ["allowed_a", "allowed_b"]}},
            }
            result = probe.json("Return a JSON object.",
                                'Try to return {"choice":"not_allowed"}.',
                                max_tokens=80, schema=choice_schema,
                                purpose="runtime_schema_contract")
            if result.get("choice") not in ("allowed_a", "allowed_b"):
                raise ValueError("Local server did not apply the structured-output constraint")
            print("Live structured-output contract passed", flush=True)
            for name in args.datasets:
                make_audio(name)
                prepared = ROOT / f"data/work/{name}"
                prepared.mkdir(parents=True, exist_ok=True)
                credit = prepared / "source_credit.txt"
                credit.write_text(datasets[name]["credit"] + "\n" + datasets[name]["source_page"],
                                  encoding="utf-8")
                call(sys.executable, "-m", "visual_audio_notes.cli", "prepare",
                     "--pdf", ROOT / f"data/public/{name}/materials.pdf",
                     "--audio", ROOT / f"data/public/{name}/recording.wav",
                     "--language", datasets[name]["language"], "--work", prepared)
                run = output / name
                call(sys.executable, "-m", "visual_audio_notes.cli", "infer",
                     "--features", prepared / "features.json", "--out", run,
                     "--embedding", ROOT / ".cache/models/embedding", "--llm", endpoint,
                     "--model-file", model, "--llm-baseline")
                call(sys.executable, "-m", "visual_audio_notes.cli", "archive",
                     "--run", run, "--prepared", prepared,
                     "--pdf", ROOT / f"data/public/{name}/materials.pdf",
                     "--audio", ROOT / f"data/public/{name}/recording.wav",
                     "--out", run / "archive", "--title", datasets[name]["title"],
                     "--credit", credit)
            print(json.dumps({"finished": str(output), "oracle_accessed": False}), flush=True)
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
