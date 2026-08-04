from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from bidpilot.api import create_app
from bidpilot.config import Settings
from bidpilot.db import Database
from bidpilot.models import RawTender, SourceSearchResult, SourceStatus
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
    def __init__(self, cookies, expected_urls, pages=None):
        self.cookie_rows = cookies
        self.expected_urls = expected_urls
        self.pages = pages or []
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
    assert "运行 BidPilot 服务的电脑上打开" in started.message
    completed = await manager.complete(started.session_id)
    assert completed.status == "completed"
    assert settings.cecbid_cookie == ""
    row = manager.db.get_source_authorization("cecbid")
    assert row is not None
    assert "secret-cookie-value" not in row["encrypted_cookie"]
    assert "tracking" not in row["cookie_names_json"]
    assert manager.status("cecbid").state == "captured_unverified"
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
    legacy_path = settings.data_dir / "secrets" / "qianlima_cookie.txt"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text("legacy=plaintext-secret", encoding="utf-8")
    manager = SourceAuthManager(
        Database(settings.database_path),
        settings,
        [QianlimaSource(settings)],
    )
    row = manager.db.get_source_authorization("qianlima")
    assert row is None
    assert not legacy_path.exists()


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
        assert statuses.json()[0]["authorization"]["state"] == "captured_unverified"


def test_qianlima_is_managed_as_persistent_browser_without_cookie_replay(tmp_path: Path):
    settings = make_settings(tmp_path)
    manager = SourceAuthManager(
        Database(settings.database_path),
        settings,
        [QianlimaSource(settings)],
    )

    status = manager.status("qianlima")

    assert status.managed is True
    assert status.state == "not_authorized"
    assert status.login_url == "https://search.vip.qianlima.com/"
    assert "不导出 Cookie" in status.authorization_scope
    assert "管理员显式开启" in status.authorization_scope
    assert "每日预算" in status.authorization_scope


def test_qianlima_exposes_current_user_controlled_free_login_handoff(tmp_path: Path):
    settings = make_settings(tmp_path)
    service_source = QianlimaSource(settings)
    capabilities = service_source.capabilities()

    assert capabilities["authorization_action_label"] == "在系统内免费登录"
    assert service_source.authorization_url == "https://search.vip.qianlima.com/"
    assert service_source.authorization_action_label == "在系统内免费登录"
    assert "用户本人" in service_source.coverage_note
    assert "不导出或后台重放 Cookie" in service_source.coverage_note


async def test_qianlima_persistent_profile_completes_tests_and_clears_without_cookie_export(
    tmp_path: Path,
):
    settings = make_settings(tmp_path)
    source = QianlimaSource(settings)
    manager = SourceAuthManager(Database(settings.database_path), settings, [source])
    playwright = FakePlaywright()
    context = FakeContext([], [], pages=[object()])

    async def fake_launch(_spec):
        return playwright, None, context

    async def ready(_context):
        return True

    manager._launch_visible_browser = fake_launch  # type: ignore[method-assign]
    source.authorization_context_ready = ready  # type: ignore[method-assign]
    started = await manager.start("qianlima")
    completed = await manager.complete(started.session_id)

    assert completed.status == "completed"
    assert "没有导出 Cookie" in completed.message
    assert source.profile_available(allow_unverified=True)
    row = manager.db.get_source_authorization("qianlima")
    assert row is not None
    assert row["cookie_names_json"] == "[]"
    assert "qianlima-persistent-browser-v1" not in row["encrypted_cookie"]
    assert manager.status("qianlima").state == "captured_unverified"

    async def foreground_passed(_spec, *, allow_unverified=False):
        assert allow_unverified is True
        return SourceSearchResult(
            source=source.name,
            status=SourceStatus.PARTIAL,
            items=[
                RawTender(
                    source=source.name,
                    source_url="https://www.qianlima.com/bid-123.html",
                    title="服务器采购公告",
                    published_at=datetime.now(),
                    body="服务器采购公告 北京 货物",
                    auth_level="free_member",
                )
            ],
        )

    source.search_foreground = foreground_passed  # type: ignore[method-assign]
    tested = await manager.test("qianlima")
    assert tested.success is True
    assert tested.status == "passed"
    assert "免费登录态有效" in tested.message
    assert source.profile_available()
    assert manager.status("qianlima").state == "authorized"
    assert context.closed is True
    assert playwright.stopped is True
    assert source._read_profile_state()["expires_at"] == ""

    async def foreground_cooling_down(_spec, *, allow_unverified=False):
        assert allow_unverified is True
        return SourceSearchResult(
            source=source.name,
            status=SourceStatus.SKIPPED,
            message="距上次免费会员查询不足冷却时间",
        )

    source.search_foreground = foreground_cooling_down  # type: ignore[method-assign]
    retested = await manager.test("qianlima")
    assert retested.status == "inconclusive"
    assert manager.status("qianlima").state == "authorized"

    cleared = await manager.clear("qianlima")
    assert cleared.state == "not_authorized"
    assert not source.profile_dir.exists()
    assert manager.db.get_source_authorization("qianlima") is None


def test_qianlima_status_uses_live_profile_health_not_legacy_timer(tmp_path: Path):
    settings = make_settings(tmp_path)
    source = QianlimaSource(settings)
    manager = SourceAuthManager(Database(settings.database_path), settings, [source])
    source.mark_profile("authorized")
    manager.db.set_source_authorization(
        source_id="qianlima",
        encrypted_cookie=manager.vault.encrypt(source.profile_marker_value),
        cookie_names=[],
        domains=["search.vip.qianlima.com", "vip.qianlima.com"],
        authorized_at="2026-08-01T00:00:00+00:00",
        expires_at="2026-08-02T00:00:00+00:00",
        message="legacy timer",
    )
    manager.db.update_source_authorization_test(
        "qianlima",
        status="passed",
        message="verified",
    )

    healthy = manager.status("qianlima")
    assert healthy.state == "authorized"
    assert healthy.expires_at is None

    source.mark_profile("expired")
    expired = manager.status("qianlima")
    assert expired.state == "expired"
    assert expired.last_test_status == "failed"
    assert "实时健康检查" in expired.message


async def test_unverified_or_failed_session_is_never_loaded_for_background_search(
    tmp_path: Path,
):
    settings = make_settings(tmp_path)
    source = CECBidSource(settings)
    manager = SourceAuthManager(Database(settings.database_path), settings, [source])
    encrypted = manager.vault.encrypt("member_session=secret")
    manager.db.set_source_authorization(
        source_id="cecbid",
        encrypted_cookie=encrypted,
        cookie_names=["member_session"],
        domains=["cecbid.org.cn"],
        authorized_at=datetime.now().astimezone().isoformat(),
        expires_at=None,
        message="captured",
    )

    manager.load_persisted()
    assert settings.cecbid_cookie == ""
    assert manager.status("cecbid").state == "captured_unverified"

    manager.db.update_source_authorization_test(
        "cecbid",
        status="failed",
        message="login gate",
    )
    manager.load_persisted()
    assert settings.cecbid_cookie == ""
    assert manager.status("cecbid").state == "failed"


async def test_auth_test_distinguishes_passed_failed_and_inconclusive(tmp_path: Path):
    settings = make_settings(tmp_path)
    source = CECBidSource(settings)
    manager = SourceAuthManager(Database(settings.database_path), settings, [source])
    manager.db.set_source_authorization(
        source_id="cecbid",
        encrypted_cookie=manager.vault.encrypt("member_session=secret"),
        cookie_names=["member_session"],
        domains=["cecbid.org.cn"],
        authorized_at=datetime.now().astimezone().isoformat(),
        expires_at=None,
        message="captured",
    )

    async def inconclusive(_spec, _fetcher):
        return SourceSearchResult(
            source=source.name,
            status=SourceStatus.PARTIAL,
            items=[],
        )

    source.search = inconclusive  # type: ignore[method-assign]
    result = await manager.test("cecbid")
    assert result.status == "inconclusive"
    assert result.success is False
    assert settings.cecbid_cookie == ""
    assert manager.status("cecbid").state == "captured_unverified"

    async def passed(_spec, _fetcher):
        assert settings.cecbid_cookie == "member_session=secret"
        return SourceSearchResult(
            source=source.name,
            status=SourceStatus.OK,
            items=[
                RawTender(
                    source=source.name,
                    source_url="https://www.cecbid.org.cn/tenders/details/verified",
                    title="真实会员候选",
                    published_at=datetime.now(),
                    body="会员正文",
                    auth_level="free_member",
                )
            ],
        )

    source.search = passed  # type: ignore[method-assign]
    result = await manager.test("cecbid")
    assert result.status == "passed"
    assert result.success is True
    assert settings.cecbid_cookie == "member_session=secret"
    assert manager.status("cecbid").state == "authorized"

    async def failed(_spec, _fetcher):
        return SourceSearchResult(
            source=source.name,
            status=SourceStatus.AUTH_REQUIRED,
        )

    source.search = failed  # type: ignore[method-assign]
    result = await manager.test("cecbid")
    assert result.status == "failed"
    assert result.success is False
    assert settings.cecbid_cookie == ""
    assert manager.status("cecbid").state == "failed"
