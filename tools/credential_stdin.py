"""Run one task with an ephemeral credential supplied over standard input.

Never write the credential to a file, arguments, logs or output. This helper is
for the current developer test, not the eventual user's settings interface.
"""
import os
from pathlib import Path
import runpy
import sys


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        raise SystemExit("A task script is required")
    target = Path(sys.argv[1]).resolve(strict=True)
    root = Path(__file__).resolve().parents[1]
    if not target.is_relative_to(root / "tools"):
        raise SystemExit("Task script must be inside this workspace's tools directory")
    credential = sys.stdin.readline().strip()
    if not credential:
        raise SystemExit("No credential supplied")
    os.environ["DEEPSEEK_API_KEY"] = credential
    credential = None
    sys.argv = [str(target), *sys.argv[2:]]
    try:
        runpy.run_path(str(target), run_name="__main__")
    finally:
        os.environ.pop("DEEPSEEK_API_KEY", None)


if __name__ == "__main__":
    main()
