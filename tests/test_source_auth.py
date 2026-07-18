from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from bidpilot.api import create_app
from bidpilot.config import Settings
from bidpilot.db import Database
from bidpilot.source_auth import SourceAuthManager
from bidpilot.sources.qianlima import QianlimaSource


class FakePlaywright:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


class FakeBrowser:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, cookies):
        self.cookie_rows = cookies
        self.closed = False

    async def cookies(self, urls=None):
        assert urls == ["https://wap.qianlima.com/", "https://www.qianlima.com/"]
        return self.cookie_rows

    async def close(self):
        self.closed = True


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "bidpilot.db",
        report_dir=tmp_path / "reports",
        embedded_worker=False,
        llm_base_url="",
        llm_api_key="",
        llm_model="",
        qianlima_cookie_path=tmp_path / "data" / "legacy-cookie.txt",
    )


async def test_source_auth_encrypts_scoped_cookies_and_clear_removes_session(tmp_path: Path):
    settings = make_settings(tmp_path)
    source = QianlimaSource(settings)
    manager = SourceAuthManager(Database(settings.database_path), settings, [source])
    playwright = FakePlaywright()
    browser = FakeBrowser()
    context = FakeContext(
        [
            {
                "name": "member_session",
                "value": "secret-cookie-value",
                "domain": ".qianlima.com",
                "expires": -1,
            },
            {
                "name": "tracking",
                "value": "must-not-save",
                "domain": ".example.com",
                "expires": -1,
            },
        ]
    )

    async def fake_launch(_spec):
        return playwright, browser, context

    manager._launch_visible_browser = fake_launch  # type: ignore[method-assign]
    started = await manager.start("qianlima")
    assert started.status == "authorizing"
    completed = await manager.complete(started.session_id)
    assert completed.status == "completed"
    assert settings.qianlima_cookie == "member_session=secret-cookie-value"
    row = manager.db.get_source_authorization("qianlima")
    assert row is not None
    assert "secret-cookie-value" not in row["encrypted_cookie"]
    assert "tracking" not in row["cookie_names_json"]
    assert manager.status("qianlima").state == "authorized"
    assert context.closed and browser.closed and playwright.stopped

    cleared = await manager.clear("qianlima")
    assert cleared.state == "not_authorized"
    assert settings.qianlima_cookie == ""
    assert manager.db.get_source_authorization("qianlima") is None


async def test_legacy_plaintext_cookie_is_migrated_then_deleted(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.qianlima_cookie_path.parent.mkdir(parents=True, exist_ok=True)
    settings.qianlima_cookie_path.write_text("legacy=plaintext-secret", encoding="utf-8")
    manager = SourceAuthManager(
        Database(settings.database_path),
        settings,
        [QianlimaSource(settings)],
    )
    row = manager.db.get_source_authorization("qianlima")
    assert row is not None
    assert "plaintext-secret" not in row["encrypted_cookie"]
    assert settings.qianlima_cookie == "legacy=plaintext-secret"
    assert not settings.qianlima_cookie_path.exists()


def test_source_auth_api_requires_token_and_never_returns_cookie(tmp_path: Path):
    settings = make_settings(tmp_path)
    app = create_app(settings, sources=[QianlimaSource(settings)])
    manager = app.state.service.source_auth
    playwright = FakePlaywright()
    browser = FakeBrowser()
    context = FakeContext(
        [
            {
                "name": "member_session",
                "value": "api-secret-cookie",
                "domain": ".qianlima.com",
                "expires": -1,
            }
        ]
    )

    async def fake_launch(_spec):
        return playwright, browser, context

    manager._launch_visible_browser = fake_launch  # type: ignore[method-assign]
    with TestClient(app) as client:
        denied = client.post("/api/v1/sources/qianlima/auth/start")
        assert denied.status_code == 403
        token = client.post("/api/v1/config/edit-token").json()["edit_token"]
        headers = {"X-BidPilot-Config-Token": token}
        started = client.post(
            "/api/v1/sources/qianlima/auth/start",
            headers=headers,
        )
        assert started.status_code == 200
        session_id = started.json()["session_id"]
        completed = client.post(
            f"/api/v1/sources/auth/sessions/{session_id}/complete",
            headers=headers,
        )
        assert completed.status_code == 200
        statuses = client.get("/api/v1/sources/status")
        assert statuses.status_code == 200
        body = statuses.text
        assert "api-secret-cookie" not in body
        assert "member_session" not in body
        assert statuses.json()[0]["authorization"]["state"] == "authorized"
