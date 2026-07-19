from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from bidpilot.api import create_app
from bidpilot.config import Settings
from bidpilot.db import Database
from bidpilot.source_auth import SourceAuthManager
from bidpilot.sources.cecbid import CECBidSource
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
    def __init__(self, cookies, expected_urls):
        self.cookie_rows = cookies
        self.expected_urls = expected_urls
        self.closed = False

    async def cookies(self, urls=None):
        assert urls == self.expected_urls
        return self.cookie_rows

    async def close(self):
        self.closed = True


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        control_dir=tmp_path / "control",
        database_path=tmp_path / "data" / "bidpilot.db",
        report_dir=tmp_path / "reports",
        embedded_worker=False,
        llm_base_url="",
        llm_api_key="",
        llm_model="",
        qianlima_cookie_path=tmp_path / "data" / "legacy-cookie.txt",
    )


async def test_cecbid_auth_encrypts_scoped_cookies_and_clear_removes_session(tmp_path: Path):
    settings = make_settings(tmp_path)
    source = CECBidSource(settings)
    manager = SourceAuthManager(Database(settings.database_path), settings, [source])
    playwright = FakePlaywright()
    browser = FakeBrowser()
    context = FakeContext(
        [
            {
                "name": "member_session",
                "value": "secret-cookie-value",
                "domain": ".cecbid.org.cn",
                "expires": -1,
            },
            {
                "name": "tracking",
                "value": "must-not-save",
                "domain": ".example.com",
                "expires": -1,
            },
        ],
        ["https://www.cecbid.org.cn/", "https://cecbid.org.cn/"],
    )

    async def fake_launch(_spec):
        return playwright, browser, context

    manager._launch_visible_browser = fake_launch  # type: ignore[method-assign]
    started = await manager.start("cecbid")
    assert started.status == "authorizing"
    completed = await manager.complete(started.session_id)
    assert completed.status == "completed"
    assert settings.cecbid_cookie == "member_session=secret-cookie-value"
    row = manager.db.get_source_authorization("cecbid")
    assert row is not None
    assert "secret-cookie-value" not in row["encrypted_cookie"]
    assert "tracking" not in row["cookie_names_json"]
    assert manager.status("cecbid").state == "authorized"
    assert context.closed and browser.closed and playwright.stopped

    cleared = await manager.clear("cecbid")
    assert cleared.state == "not_authorized"
    assert settings.cecbid_cookie == ""
    assert manager.db.get_source_authorization("cecbid") is None


async def test_complete_without_scoped_cookie_returns_failed_not_false_success(tmp_path: Path):
    settings = make_settings(tmp_path)
    manager = SourceAuthManager(
        Database(settings.database_path),
        settings,
        [CECBidSource(settings)],
    )
    context = FakeContext([], ["https://www.cecbid.org.cn/", "https://cecbid.org.cn/"])

    async def fake_launch(_spec):
        return FakePlaywright(), FakeBrowser(), context

    manager._launch_visible_browser = fake_launch  # type: ignore[method-assign]
    started = await manager.start("cecbid")
    completed = await manager.complete(started.session_id)

    assert completed.status == "failed"
    assert "没有检测到" in completed.message
    assert manager.db.get_source_authorization("cecbid") is None


async def test_legacy_qianlima_cookie_is_deleted_and_never_migrated(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.qianlima_cookie_path.parent.mkdir(parents=True, exist_ok=True)
    settings.qianlima_cookie_path.write_text("legacy=plaintext-secret", encoding="utf-8")
    manager = SourceAuthManager(
        Database(settings.database_path),
        settings,
        [QianlimaSource(settings)],
    )
    row = manager.db.get_source_authorization("qianlima")
    assert row is None
    assert settings.qianlima_cookie == ""
    assert not settings.qianlima_cookie_path.exists()


def test_source_auth_api_requires_token_and_never_returns_cookie(tmp_path: Path):
    settings = make_settings(tmp_path)
    app = create_app(settings, sources=[CECBidSource(settings)])
    manager = app.state.service.source_auth
    playwright = FakePlaywright()
    browser = FakeBrowser()
    context = FakeContext(
        [
            {
                "name": "member_session",
                "value": "api-secret-cookie",
                "domain": ".cecbid.org.cn",
                "expires": -1,
            }
        ],
        ["https://www.cecbid.org.cn/", "https://cecbid.org.cn/"],
    )

    async def fake_launch(_spec):
        return playwright, browser, context

    manager._launch_visible_browser = fake_launch  # type: ignore[method-assign]
    with TestClient(app) as client:
        denied = client.post("/api/v1/sources/cecbid/auth/start")
        assert denied.status_code == 403
        token = client.post("/api/v1/config/edit-token").json()["edit_token"]
        headers = {"X-BidPilot-Config-Token": token}
        started = client.post(
            "/api/v1/sources/cecbid/auth/start",
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


def test_qianlima_is_user_assisted_and_rejects_cookie_capture(tmp_path: Path):
    settings = make_settings(tmp_path)
    manager = SourceAuthManager(
        Database(settings.database_path),
        settings,
        [QianlimaSource(settings)],
    )

    status = manager.status("qianlima")

    assert status.managed is False
    assert status.state == "not_supported"
    assert status.login_url == "https://search.vip.qianlima.com/"
    assert "不会消费该会话" in status.authorization_scope
