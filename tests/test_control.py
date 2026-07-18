from __future__ import annotations

import time

from fastapi.testclient import TestClient

from bidpilot.api import create_app
from bidpilot.config import Settings
from bidpilot.control import ControlPlane


def make_settings(tmp_path):
    return Settings(
        data_dir=tmp_path / "data",
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
    state = control.write_state(host="127.0.0.1", port=8123, version="0.5.0")
    assert state["pid"] > 0
    assert control.read_state()["port"] == 8123
    assert token not in control.state_path.read_text(encoding="utf-8")
    assert control.verify(token)
    assert not control.verify("wrong-token")
    control.clear_state(pid=state["pid"] + 1)
    assert control.state_path.exists()
    control.clear_state(pid=state["pid"])
    assert not control.state_path.exists()


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
