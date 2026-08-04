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
    enterprise_path = ROOT / "deploy" / "compose" / "compose.enterprise.yaml"
    with enterprise_path.open(encoding="utf-8") as handle:
        enterprise = yaml.safe_load(handle)
    services = enterprise.get("services", {}) if isinstance(enterprise, dict) else {}
    networks = enterprise.get("networks", {}) if isinstance(enterprise, dict) else {}
    if not {"web", "worker", "gateway"}.issubset(services):
        raise ValueError("enterprise compose must define web, worker and gateway")
    if services["web"].get("ports") or "8000" not in services["web"].get("expose", []):
        raise ValueError("enterprise web must expose but never publish port 8000")
    if networks.get("bidpilot-backend", {}).get("internal") is not True:
        raise ValueError("enterprise proxy network must stay internal")
    if networks.get("bidpilot-ingress", {}).get("internal") is True:
        raise ValueError("enterprise gateway ingress network must permit published HTTPS")
    gateway_networks = services["gateway"].get("networks", [])
    if not {"bidpilot-ingress", "bidpilot-backend"}.issubset(gateway_networks):
        raise ValueError(
            "enterprise gateway must bridge HTTPS ingress to the internal proxy network"
        )
    if "bidpilot-egress" not in networks:
        raise ValueError("enterprise web/worker require an un-published egress network")
    if not (ROOT / "deploy" / "nginx" / "bidpilot.conf.template").exists():
        raise ValueError("enterprise Nginx template is missing")
    for unit in ("bidpilot-web.service", "bidpilot-worker.service"):
        text = (ROOT / "deploy" / "systemd" / unit).read_text(encoding="utf-8")
        required = ("[Unit]", "[Service]", "[Install]", "Restart=on-failure")
        if not all(item in text for item in required):
            raise ValueError(f"incomplete systemd unit: {unit}")
    smoke_test = ROOT / "deploy" / "systemd" / "smoke-test.sh"
    if not smoke_test.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash"):
        raise ValueError("systemd smoke test must be an executable Bash script")


if __name__ == "__main__":
    main()
