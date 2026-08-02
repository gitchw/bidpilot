from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from bidpilot.cli import _lan_access_urls

ROOT = Path(__file__).resolve().parents[1]


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_for_health(port: int, *, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                return json.loads(response.read())
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            time.sleep(0.1)
    raise AssertionError(f"BidPilot did not become healthy at {url}")


def lifecycle_environment(tmp_path: Path, port: int) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "BIDPILOT_DATA_DIR": str(tmp_path / "business-data"),
            "BIDPILOT_CONTROL_DIR": str(tmp_path / "control"),
            "BIDPILOT_DATABASE_PATH": str(tmp_path / "business-data" / "bidpilot.db"),
            "BIDPILOT_REPORT_DIR": str(tmp_path / "reports"),
            "BIDPILOT_EMBEDDED_WORKER": "false",
            "BIDPILOT_PORT": str(port),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "NO_COLOR": "1",
        }
    )
    return env


def run_cli(env: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "bidpilot", *arguments],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def test_real_cli_serve_status_restart_and_stop_share_control_directory(tmp_path: Path):
    port = available_port()
    env = lifecycle_environment(tmp_path, port)
    serve = subprocess.Popen(
        [sys.executable, "-m", "bidpilot", "serve", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    restart = None
    try:
        assert wait_for_health(port)["version"] == "0.8.0"
        status = run_cli(env, "status")
        assert status.returncode == 0, status.stdout + status.stderr
        assert "v0.8.0" in status.stdout
        state = json.loads((tmp_path / "control" / "runtime" / "server.json").read_text())
        assert state["port"] == port

        restart = subprocess.Popen(
            [sys.executable, "-m", "bidpilot", "restart", "--port", str(port)],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        assert serve.wait(timeout=15) == 0
        assert wait_for_health(port)["version"] == "0.8.0"

        stopped = run_cli(env, "stop", "--wait-seconds", "15")
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert restart.wait(timeout=15) == 0
        assert not (tmp_path / "control" / "runtime" / "server.json").exists()
        assert (tmp_path / "business-data" / "bidpilot.db").exists()
        assert (tmp_path / "control" / "secrets" / "control.token").exists()
    finally:
        stop_process(serve)
        if restart is not None:
            stop_process(restart)


def test_second_serve_cannot_overwrite_live_control_state(tmp_path: Path):
    first_port = available_port()
    second_port = available_port()
    env = lifecycle_environment(tmp_path, first_port)
    first = subprocess.Popen(
        [sys.executable, "-m", "bidpilot", "serve", "--port", str(first_port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert wait_for_health(first_port)["version"] == "0.8.0"
        state_path = tmp_path / "control" / "runtime" / "server.json"
        original = json.loads(state_path.read_text(encoding="utf-8"))

        second = run_cli(env, "serve", "--port", str(second_port))

        assert second.returncode != 0
        assert "已经存在受管实例" in second.stdout + second.stderr
        assert json.loads(state_path.read_text(encoding="utf-8")) == original
        assert wait_for_health(first_port)["status"] == "ok"
        stopped = run_cli(env, "stop", "--wait-seconds", "15")
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert first.wait(timeout=15) == 0
    finally:
        stop_process(first)


def test_compose_loopback_publish_enables_container_bridge_mutations():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert '"127.0.0.1:${BIDPILOT_PORT:-8000}:8000"' in compose
    assert "BIDPILOT_NETWORK_ACCESS_MODE: lan" in compose
    assert "BIDPILOT_LAN_ACCESS_POLICY: trusted_lan" in compose
    assert "BIDPILOT_LAN_TRUSTED_NETWORKS: auto" in compose


def test_local_mode_rejects_non_loopback_host_override(tmp_path: Path):
    env = lifecycle_environment(tmp_path, available_port())
    env["BIDPILOT_NETWORK_ACCESS_MODE"] = "local"
    result = run_cli(env, "serve", "--host", "0.0.0.0")
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "--host" in output
    assert "BIDPILOT_NETWORK_ACCESS_MODE=lan" in output


def test_lan_startup_urls_only_include_private_non_loopback_addresses(monkeypatch):
    monkeypatch.setattr(
        "bidpilot.cli.socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("192.168.1.12", 0)),
            (2, 1, 6, "", ("127.0.0.1", 0)),
            (2, 1, 6, "", ("8.8.8.8", 0)),
        ],
    )

    assert _lan_access_urls(8000) == ["http://192.168.1.12:8000"]
