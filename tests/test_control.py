from __future__ import annotations

import os
import time
from types import SimpleNamespace

import httpx
import pytest
import typer
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from bidpilot.api import create_app
from bidpilot.cli import _python_module_command, _stop_service, _validate_bind_host, app
from bidpilot.config import Settings
from bidpilot.control import ControlPlane, local_control_url


def make_settings(tmp_path):
    return Settings(
        data_dir=tmp_path / "data",
        control_dir=tmp_path / "control",
        report_dir=tmp_path / "reports",
        database_path=tmp_path / "data" / "test.db",
        embedded_worker=False,
    )


def test_control_token_and_runtime_state_are_separate(tmp_path):
    settings = make_settings(tmp_path)
    control = ControlPlane(settings)
    token = control.ensure_token()
    assert len(token) >= 48
    assert control.ensure_token() == token
    state = control.write_state(host="127.0.0.1", port=8123, version="0.8.0")
    assert state["pid"] > 0
    assert control.read_state()["port"] == 8123
    assert token not in control.state_path.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        control.write_state(host="127.0.0.1", port=9123, version="0.8.0")
    assert control.read_state()["port"] == 8123
    assert control.token_path.parent == settings.control_dir / "secrets"
    assert control.state_path.parent == settings.control_dir / "runtime"
    assert control.verify(token)
    assert not control.verify("wrong-token")
    control.clear_state(pid=state["pid"] + 1)
    assert control.state_path.exists()
    control.clear_state(pid=state["pid"])
    assert not control.state_path.exists()


def test_control_plane_detects_current_process(tmp_path):
    control = ControlPlane(make_settings(tmp_path))

    assert control.process_is_alive(os.getpid()) is True
    assert control.process_is_alive(-1) is False


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("0.0.0.0", "http://127.0.0.1:8000"),
        ("localhost", "http://127.0.0.1:8000"),
        ("::", "http://[::1]:8000"),
        ("[::]", "http://[::1]:8000"),
        ("::1", "http://[::1]:8000"),
        ("127.0.0.1", "http://127.0.0.1:8000"),
    ],
)
def test_local_control_url_uses_matching_loopback_family(host, expected):
    assert local_control_url(host, 8000) == expected


def test_pid_fenced_cleanup_preserves_an_unreadable_runtime_claim(tmp_path):
    control = ControlPlane(make_settings(tmp_path))
    control.state_path.parent.mkdir(parents=True, exist_ok=True)
    control.state_path.write_text('{"pid":', encoding="utf-8")

    control.clear_state(pid=os.getpid())

    assert control.state_path.exists()
    control.clear_state()
    assert not control.state_path.exists()


def test_stop_preserves_claim_while_owner_is_still_starting(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    control = ControlPlane(settings)
    state = control.write_state(host="127.0.0.1", port=8123, version="0.8.0")

    class OfflineClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            request = httpx.Request("GET", url)
            raise httpx.ConnectError("still starting", request=request)

    monkeypatch.setattr(
        "bidpilot.cli._service_target",
        lambda _settings: (control, state, "http://127.0.0.1:8123"),
    )
    monkeypatch.setattr("bidpilot.cli.httpx.Client", OfflineClient)

    assert _stop_service(settings, 1.0) is False
    assert control.read_state()["pid"] == os.getpid()


def test_restart_bind_validation_matches_serve(tmp_path):
    settings = make_settings(tmp_path)
    settings.network_access_mode = "local"

    with pytest.raises(typer.BadParameter, match="BIDPILOT_NETWORK_ACCESS_MODE=lan"):
        _validate_bind_host(settings, "0.0.0.0")


def test_cli_run_accepts_repeated_delivery_targets(tmp_path, monkeypatch):
    captured = {}

    class FakeService:
        def __init__(self, _settings):
            pass

        async def run_query(self, query, **kwargs):
            captured["query"] = query
            captured.update(kwargs)
            return SimpleNamespace(new_count=0, report_path=None, diagnostics=[])

    monkeypatch.setattr("bidpilot.cli.BidPilotService", FakeService)
    monkeypatch.setattr("bidpilot.cli.get_settings", lambda: make_settings(tmp_path))
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "run",
            "最近一周深圳服务器",
            "--channel",
            "email",
            "--target",
            "local",
            "--target",
            "telegram_bot",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured == {
        "query": "最近一周深圳服务器",
        "delivery_channel": "email",
        "delivery_targets": ["local", "telegram_bot"],
    }
    help_result = runner.invoke(app, ["run", "--help"])
    assert help_result.exit_code == 0
    assert "telegram_bot" in help_result.output
    assert "slack_webhook" in help_result.output


@pytest.mark.parametrize("command", ["serve", "restart"])
def test_cli_rejects_invalid_service_ports_before_lifecycle_changes(command):
    result = CliRunner().invoke(app, [command, "--port", "0"])

    assert result.exit_code == 2
    assert "1" in result.output and "65535" in result.output


def test_runtime_config_reports_actual_cli_endpoint(tmp_path):
    settings = make_settings(tmp_path)
    settings.network_access_mode = "lan"
    settings.port = 8080
    app = create_app(settings, sources=[])

    app.state.service.runtime_config.set_effective_endpoint(host="127.0.0.1", port=8000)
    view = app.state.service.runtime_config.snapshot()

    assert view.network.access_mode == "lan"
    assert view.network.port == 8080
    assert view.network.effective_access_mode == "local"
    assert view.network.effective_bind_host == "127.0.0.1"
    assert view.network.effective_port == 8000
    assert view.network.pending_restart is True


def test_python_module_command_is_copyable_in_powershell(monkeypatch, tmp_path):
    executable = tmp_path / "Python Runtime" / "python.exe"
    monkeypatch.setattr("bidpilot.cli.sys.executable", str(executable))
    monkeypatch.setattr("bidpilot.cli.sys.platform", "win32")

    command = _python_module_command("stop")

    assert command == f'& "{executable.resolve()}" -m bidpilot stop'


def test_shutdown_endpoint_requires_local_token_and_controller(tmp_path):
    settings = make_settings(tmp_path)
    app = create_app(settings, sources=[])
    token = app.state.control_plane.ensure_token()
    called: list[bool] = []
    app.state.shutdown_callback = lambda: called.append(True)
    with TestClient(app) as client:
        denied = client.post("/api/v1/system/shutdown")
        assert denied.status_code == 403
        accepted = client.post(
            "/api/v1/system/shutdown",
            headers={"X-BidPilot-Control-Token": token},
        )
        assert accepted.status_code == 202
        assert accepted.json()["accepted"] is True
        time.sleep(0.25)
    assert called == [True]


def test_shutdown_control_token_works_through_explicit_lan_bind_address(tmp_path):
    settings = make_settings(tmp_path)
    app = create_app(settings, sources=[])
    token = app.state.control_plane.ensure_token()
    called: list[bool] = []
    app.state.shutdown_callback = lambda: called.append(True)

    with TestClient(app, client=("192.168.50.20", 50000)) as client:
        denied = client.post("/api/v1/system/shutdown")
        assert denied.status_code == 403
        accepted = client.post(
            "/api/v1/system/shutdown",
            headers={"X-BidPilot-Control-Token": token},
        )
        assert accepted.status_code == 202
        assert accepted.json()["accepted"] is True
        time.sleep(0.25)

    assert called == [True]


def test_shutdown_endpoint_refuses_unmanaged_uvicorn_app(tmp_path):
    settings = make_settings(tmp_path)
    app = create_app(settings, sources=[])
    token = app.state.control_plane.ensure_token()
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/system/shutdown",
            headers={"X-BidPilot-Control-Token": token},
        )
    assert response.status_code == 409
