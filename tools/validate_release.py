"""Validate non-Python release artifacts with only installed project dependencies."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    json.loads((ROOT / "feature_list.json").read_text(encoding="utf-8"))
    tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    with (ROOT / "compose.yaml").open(encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    if not isinstance(value, dict) or not {"web", "worker"}.issubset(value.get("services", {})):
        raise ValueError("compose.yaml must define web and worker services")
    for unit in ("bidpilot-web.service", "bidpilot-worker.service"):
        text = (ROOT / "deploy" / "systemd" / unit).read_text(encoding="utf-8")
        required = ("[Unit]", "[Service]", "[Install]", "Restart=on-failure")
        if not all(item in text for item in required):
            raise ValueError(f"incomplete systemd unit: {unit}")


if __name__ == "__main__":
    main()
