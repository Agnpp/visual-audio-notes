"""Review staged source files before publishing; never print matched secrets."""
from pathlib import Path
import json
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def check():
    result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True)
    files = [Path(name) for name in result.stdout.decode("utf-8").split("\0") if name]
    forbidden_roots = {".venv", ".venv-desktop", ".cache", ".runtime", "data", "output", "outputs", "build", "dist", ".playwright-cli"}
    secret = re.compile(r"(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")
    problems = []
    for relative in files:
        path = ROOT / relative
        if (relative.parts[0] in forbidden_roots or path.name.startswith(".env")
                or path.suffix.lower() in {".bin", ".gguf", ".onnx", ".wav", ".mp3", ".m4a", ".pdf", ".zip", ".exe"}
                or path.stat().st_size > 2 * 1024 * 1024):
            problems.append({"file":relative.as_posix(), "reason":"private/generated/large material is not source"})
            continue
        if path.suffix.lower() == ".png":
            if relative.parts[:2] != ("docs", "images"):
                problems.append({"file":relative.as_posix(),"reason":"only reviewed UI screenshots belong in source"})
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeError:
            problems.append({"file":relative.as_posix(),"reason":"unexpected binary"})
            continue
        for number, line in enumerate(text.splitlines(),1):
            if secret.search(line):
                problems.append({"file":relative.as_posix(),"line":number,"reason":"credential-like value"})
    report = {"staged_source_files":len(files),"issues":problems,"secret_values_printed":False}
    print(json.dumps(report,ensure_ascii=True,indent=2))
    if not files or problems:
        raise SystemExit(1)
    return report


if __name__ == "__main__":
    check()
