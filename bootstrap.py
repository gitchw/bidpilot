"""Cross-platform local environment bootstrap for BidPilot."""

from __future__ import annotations

import argparse
import subprocess
import sys
import venv
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dev", action="store_true", help="install test and lint dependencies")
    parser.add_argument("--auth", action="store_true", help="install Playwright login support")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    environment = root / ".venv"
    if not environment.exists():
        venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    extras = [name for name, enabled in (("dev", args.dev), ("auth", args.auth)) if enabled]
    target = ".[" + ",".join(extras) + "]" if extras else "."
    subprocess.run([str(python), "-m", "pip", "install", "--upgrade", "pip"], check=True)
    subprocess.run([str(python), "-m", "pip", "install", "-e", target], cwd=root, check=True)
    print(f"BidPilot environment is ready: {python}")
    print(f"Start the app: {python} -m bidpilot serve")


if __name__ == "__main__":
    main()
