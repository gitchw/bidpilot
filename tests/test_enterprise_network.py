import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from bidpilot.api import create_app
from bidpilot.config import Settings
from bidpilot.db import Database
from bidpilot.network_access import (
    parse_https_origins,
    parse_trusted_proxy_networks,
    resolve_forwarded_client,
)


def enterprise_settings(tmp_path: Path) -> Settings:
    return Settings(
        env="production",
        host="127.0.0.1",
        network_access_mode="enterprise",
        lan_access_policy="admin_token",
        lan_trusted_networks="192.168.0.0/16",
        lan_admin_token="enterprise-admin-token-12345",
        trusted_proxy_networks="127.0.0.1/32",
        enterprise_allowed_origins="https://bidpilot.corp.example",
        data_dir=tmp_path / "data",
        control_dir=tmp_path / "control",
        report_dir=tmp_path / "reports",
        database_path=tmp_path / "data" / "test.db",
        embedded_worker=False,
    )


def test_enterprise_mode_fails_closed_without_identity_and_https_boundary(tmp_path: Path):
    common = {
        "network_access_mode": "enterprise",
        "lan_access_policy": "admin_token",
        "lan_trusted_networks": "192.168.0.0/16",
        "data_dir": tmp_path / "data",
        "database_path": tmp_path / "data" / "test.db",
    }
    with pytest.raises(ValidationError, match="管理员令牌"):
        Settings(**common, enterprise_allowed_origins="https://bidpilot.corp.example")
    with pytest.raises(ValidationError, match="HTTPS 浏览器来源"):
        Settings(**common, lan_admin_token="enterprise-admin-token-12345")
    with pytest.raises(ValidationError, match="不能启用免令牌"):
        Settings(
            **{
                **common,
                "lan_access_policy": "trusted_lan",
                "lan_admin_token": "enterprise-admin-token-12345",
                "enterprise_allowed_origins": "https://bidpilot.corp.example",
            }
        )


def test_proxy_and_origin_parsers_reject_public_or_ambiguous_values():
    assert str(parse_trusted_proxy_networks("127.0.0.1/32,172.18.0.0/16")[0]) == ("127.0.0.1/32")
    assert parse_https_origins("https://BIDPILOT.corp.example/") == (
        "https://bidpilot.corp.example",
    )
    with pytest.raises(ValueError, match="私有或回环"):
        parse_trusted_proxy_networks("8.8.8.8/32")
    with pytest.raises(ValueError, match="私有或回环"):
        parse_trusted_proxy_networks("203.0.113.10/32")
    with pytest.raises(ValueError, match="HTTPS Origin"):
        parse_https_origins("http://bidpilot.corp.example")
    with pytest.raises(ValueError, match="HTTPS Origin"):
        parse_https_origins("https://bidpilot.corp.example/app")


def test_forwarded_client_is_used_only_from_an_explicit_trusted_proxy():
    assert resolve_forwarded_client("8.8.8.8", "192.168.1.20", "127.0.0.1/32") == (
        "8.8.8.8",
        False,
    )
    assert resolve_forwarded_client(
        "127.0.0.1",
        "192.168.1.20, 172.18.0.2",
        "127.0.0.1/32,172.18.0.0/16",
    ) == ("192.168.1.20", True)
    assert resolve_forwarded_client("127.0.0.1", "malformed, 192.168.1.20", "127.0.0.1/32") == (
        "127.0.0.1",
        False,
    )


def test_enterprise_api_requires_network_https_origin_and_token(tmp_path: Path):
    settings = enterprise_settings(tmp_path)
    app = create_app(settings, sources=[])
    forwarded = {
        "X-Forwarded-For": "192.168.10.25",
        "X-Forwarded-Proto": "https",
        "Origin": "https://bidpilot.corp.example",
    }
    token = {"X-BidPilot-Admin-Token": settings.lan_admin_token}

    with TestClient(app, client=("127.0.0.1", 43123)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/", headers=forwarded).status_code == 200

        denied = client.get("/api/v1/config", headers=forwarded)
        assert denied.status_code == 403
        assert "管理员令牌" in denied.json()["detail"]
        assert denied.headers["access-control-allow-origin"] == forwarded["Origin"]

        allowed = client.get("/api/v1/config", headers={**forwarded, **token})
        assert allowed.status_code == 200
        assert allowed.headers["strict-transport-security"] == "max-age=31536000"
        assert allowed.headers["access-control-allow-origin"] == forwarded["Origin"]
        assert allowed.headers["cache-control"] == "no-store"

        plaintext = client.get(
            "/api/v1/config",
            headers={
                "X-Forwarded-For": "192.168.10.25",
                **token,
            },
        )
        assert plaintext.status_code == 403
        assert "HTTPS" in plaintext.json()["detail"]

        wrong_origin = client.get(
            "/api/v1/config",
            headers={**forwarded, "Origin": "https://evil.example", **token},
        )
        assert wrong_origin.status_code == 403
        assert "来源" in wrong_origin.json()["detail"]

        preflight = client.options(
            "/api/v1/config",
            headers={
                **forwarded,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-BidPilot-Admin-Token",
            },
        )
        assert preflight.status_code == 204


def test_enterprise_network_fields_round_trip_through_runtime_config(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        control_dir=tmp_path / "control",
        report_dir=tmp_path / "reports",
        database_path=tmp_path / "data" / "test.db",
        embedded_worker=False,
    )
    app = create_app(settings, sources=[])
    with TestClient(app) as client:
        initial = client.get("/api/v1/config").json()
        edit_token = client.post("/api/v1/config/edit-token").json()["edit_token"]
        saved = client.put(
            "/api/v1/config",
            headers={"X-BidPilot-Config-Token": edit_token},
            json={
                "revision": initial["revision"],
                "network_access_mode": "enterprise",
                "lan_access_policy": "admin_token",
                "lan_trusted_networks": "10.20.0.0/16",
                "lan_admin_token": "enterprise-admin-token-12345",
                "trusted_proxy_networks": "127.0.0.1/32",
                "enterprise_allowed_origins": "https://bidpilot.corp.example",
            },
        )
        assert saved.status_code == 200
        network = saved.json()["network"]
        assert network["configuration_locked"] is False
        assert network["access_mode"] == "enterprise"
        assert network["trusted_proxy_networks"] == "127.0.0.1/32"
        assert network["enterprise_allowed_origins"] == "https://bidpilot.corp.example"
        assert network["effective_access_mode"] == "local"
        assert network["pending_restart"] is True

        unsafe = client.put(
            "/api/v1/config",
            headers={"X-BidPilot-Config-Token": edit_token},
            json={
                "revision": saved.json()["revision"],
                "lan_access_policy": "trusted_lan",
            },
        )
        assert unsafe.status_code == 422
        assert "不能启用免令牌" in unsafe.json()["detail"]


def test_untrusted_peer_cannot_spoof_forwarded_enterprise_client(tmp_path: Path):
    settings = enterprise_settings(tmp_path)
    app = create_app(settings, sources=[])
    with TestClient(
        app, base_url="https://bidpilot.corp.example", client=("8.8.8.8", 43123)
    ) as client:
        denied = client.get(
            "/api/v1/config",
            headers={
                "X-Forwarded-For": "192.168.10.25",
                "X-Forwarded-Proto": "https",
                "X-BidPilot-Admin-Token": settings.lan_admin_token,
            },
        )
    assert denied.status_code == 403
    assert "受信网段" in denied.json()["detail"]


def test_enterprise_environment_boundary_cannot_be_downgraded_by_old_database(tmp_path: Path):
    settings = enterprise_settings(tmp_path)
    database = Database(settings.database_path)
    database.set_runtime_config(
        {
            "network_access_mode": (json.dumps("lan"), False),
            "lan_access_policy": (json.dumps("trusted_lan"), False),
            "lan_trusted_networks": (json.dumps("auto"), False),
        }
    )

    app = create_app(settings, sources=[])
    assert settings.network_access_mode == "enterprise"
    assert settings.lan_access_policy == "admin_token"

    forwarded = {
        "X-Forwarded-For": "192.168.10.25",
        "X-Forwarded-Proto": "https",
        "Origin": "https://bidpilot.corp.example",
    }
    with TestClient(app, client=("127.0.0.1", 43123)) as client:
        denied = client.get("/api/v1/config", headers=forwarded)
        assert denied.status_code == 403
        assert "管理员令牌" in denied.json()["detail"]

        token = client.post(
            "/api/v1/config/edit-token",
            headers={**forwarded, "X-BidPilot-Admin-Token": settings.lan_admin_token},
        ).json()["edit_token"]
        update = client.put(
            "/api/v1/config",
            headers={
                **forwarded,
                "X-BidPilot-Admin-Token": settings.lan_admin_token,
                "X-BidPilot-Config-Token": token,
            },
            json={"revision": 1, "network_access_mode": "lan"},
        )
        assert update.status_code == 422
        assert "环境强制锁定" in update.json()["detail"]
        locked_view = client.get(
            "/api/v1/config",
            headers={**forwarded, "X-BidPilot-Admin-Token": settings.lan_admin_token},
        ).json()
        assert locked_view["network"]["configuration_locked"] is True
        assert locked_view["field_metadata"]["network_access_mode"]["source"] == "environment"


def test_enterprise_local_control_token_keeps_cli_status_and_shutdown_available(tmp_path: Path):
    settings = enterprise_settings(tmp_path)
    app = create_app(settings, sources=[])
    control_token = app.state.control_plane.ensure_token()
    app.state.shutdown_callback = lambda: None
    headers = {"X-BidPilot-Control-Token": control_token}

    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 43123)) as client:
        assert client.get("/api/v1/system/status").status_code == 403
        assert client.get("/api/v1/system/status", headers=headers).status_code == 200
        assert client.post("/api/v1/system/shutdown", headers=headers).status_code == 202
