from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field

from bidpilot.config import Settings
from bidpilot.db import Database
from bidpilot.fetch import HttpFetcher
from bidpilot.models import IntentSchedule, ScheduleKind, SourceStatus, TenderQuerySpec
from bidpilot.runtime_config import LocalSecretVault, RuntimeConfigError
from bidpilot.sources.base import SourceAdapter

_COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


class SourceAuthError(RuntimeError):
    """Safe, user-facing source authorization failure."""


class SourceAuthView(BaseModel):
    source_id: str = Field(description="稳定来源 ID")
    source_name: str = Field(description="来源中文名称")
    managed: bool = Field(description="是否支持由来源中心管理授权会话")
    state: Literal[
        "not_supported",
        "not_authorized",
        "authorizing",
        "authorized",
        "expired",
        "failed",
    ] = Field(description="脱敏授权状态")
    login_url: str = Field(default="", description="用户可见浏览器打开的官方登录页")
    authorization_scope: str = Field(default="", description="授权能增强的实际范围")
    authorized_at: datetime | None = None
    expires_at: datetime | None = None
    last_test_at: datetime | None = None
    last_test_status: Literal["not_tested", "passed", "failed"] = "not_tested"
    active_session_id: str = Field(
        default="",
        description="仅在本机授权窗口进行中时返回的一次性会话 ID",
    )
    message: str = ""


class SourceAuthSessionView(BaseModel):
    session_id: str = Field(description="一次性本机授权会话 ID")
    source_id: str
    source_name: str
    status: Literal["authorizing", "completed", "failed", "expired"]
    started_at: datetime
    expires_at: datetime
    login_url: str
    message: str


class SourceAuthTestResult(BaseModel):
    source_id: str
    success: bool
    status: Literal["passed", "failed"]
    message: str
    latency_ms: int = Field(default=0, ge=0)


@dataclass(frozen=True, slots=True)
class SourceAuthSpec:
    source_id: str
    source_name: str
    login_url: str
    allowed_domains: tuple[str, ...]
    setting_field: str
    authorization_scope: str

    @property
    def allowed_urls(self) -> list[str]:
        return [f"https://{domain}/" for domain in self.allowed_domains]


class _BrowserContext(Protocol):
    async def cookies(self, urls: list[str] | None = None) -> list[dict[str, Any]]: ...

    async def new_page(self) -> Any: ...

    async def close(self) -> None: ...


class _Browser(Protocol):
    async def close(self) -> None: ...


class _Playwright(Protocol):
    async def stop(self) -> None: ...


@dataclass(slots=True)
class _LiveSession:
    session_id: str
    spec: SourceAuthSpec
    started_at: datetime
    expires_at: datetime
    playwright: _Playwright
    browser: _Browser
    context: _BrowserContext
    status: Literal["authorizing", "completed", "failed", "expired"] = "authorizing"
    message: str = "浏览器已打开，请由你本人完成登录后回到来源中心点击“完成授权”。"


class SourceAuthManager:
    """User-driven visible-browser authorization with encrypted scoped cookies."""

    session_ttl = timedelta(minutes=15)

    def __init__(
        self,
        db: Database,
        settings: Settings,
        sources: list[SourceAdapter],
    ):
        self.db = db
        self.settings = settings
        self.sources = {source.source_id: source for source in sources}
        self.vault = LocalSecretVault(settings.data_dir / "secrets" / "source_auth.key")
        self.specs = self._build_specs()
        self._sessions: dict[str, _LiveSession] = {}
        self._lock = asyncio.Lock()
        self._migrate_legacy_qianlima_cookie()
        self.load_persisted()

    def _build_specs(self) -> dict[str, SourceAuthSpec]:
        configured = (
            SourceAuthSpec(
                source_id="qianlima",
                source_name="千里马招标网",
                login_url="https://wap.qianlima.com/login.jsp",
                allowed_domains=("wap.qianlima.com", "www.qianlima.com"),
                setting_field="qianlima_cookie",
                authorization_scope="使用用户免费会员账号原本可见的站内搜索与详情，不包含付费内容。",
            ),
            SourceAuthSpec(
                source_id="cecbid",
                source_name="中国招标投标网",
                login_url="https://www.cecbid.org.cn/login",
                allowed_domains=("www.cecbid.org.cn", "cecbid.org.cn"),
                setting_field="cecbid_cookie",
                authorization_scope="在公开搜索基础上尝试读取账号原本可见的会员详情。",
            ),
        )
        return {item.source_id: item for item in configured if item.source_id in self.sources}

    def is_managed(self, source_id: str) -> bool:
        return source_id in self.specs

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        if not value:
            return None
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    def _migrate_legacy_qianlima_cookie(self) -> None:
        path: Path = self.settings.qianlima_cookie_path
        if self.db.get_source_authorization("qianlima") or not path.exists():
            return
        with suppress(OSError, UnicodeError):
            cookie = path.read_text(encoding="utf-8").strip()
            if not cookie:
                return
            now = self._now()
            self.db.set_source_authorization(
                source_id="qianlima",
                encrypted_cookie=self.vault.encrypt(cookie),
                cookie_names=self._cookie_names_from_header(cookie),
                domains=["wap.qianlima.com", "www.qianlima.com"],
                authorized_at=now.isoformat(),
                expires_at=None,
                message="旧版本机会话已迁移到 SQLite 加密存储；建议在来源中心执行一次连接测试。",
            )
            path.unlink(missing_ok=True)

    def load_persisted(self) -> None:
        for spec in self.specs.values():
            row = self.db.get_source_authorization(spec.source_id)
            value = ""
            if row:
                expires_at = self._parse_datetime(row.get("expires_at"))
                if not expires_at or expires_at > self._now():
                    try:
                        value, _ = self.vault.decrypt(row["encrypted_cookie"])
                    except RuntimeConfigError:
                        value = ""
            setattr(self.settings, spec.setting_field, value)

    @staticmethod
    def _cookie_names_from_header(header: str) -> list[str]:
        return sorted(
            {
                part.split("=", 1)[0].strip()
                for part in header.split(";")
                if "=" in part and part.split("=", 1)[0].strip()
            }
        )

    @staticmethod
    def _domain_allowed(domain: str, allowed_domains: tuple[str, ...]) -> bool:
        normalized = domain.lstrip(".").lower()
        return any(
            normalized == allowed
            or normalized.endswith(f".{allowed}")
            or allowed.endswith(f".{normalized}")
            for allowed in allowed_domains
        )

    @classmethod
    def _serialize_cookies(
        cls,
        cookies: list[dict[str, Any]],
        allowed_domains: tuple[str, ...],
    ) -> tuple[str, list[str], datetime | None]:
        safe: dict[str, str] = {}
        expiries: list[datetime] = []
        for cookie in cookies:
            name = str(cookie.get("name") or "").strip()
            value = str(cookie.get("value") or "")
            domain = str(cookie.get("domain") or "")
            if (
                not name
                or not _COOKIE_NAME.fullmatch(name)
                or ";" in value
                or "\r" in value
                or "\n" in value
                or not cls._domain_allowed(domain, allowed_domains)
            ):
                continue
            safe[name] = value
            expiry = cookie.get("expires")
            if isinstance(expiry, (int, float)) and expiry > 0:
                with suppress(ValueError, OSError, OverflowError):
                    expiries.append(datetime.fromtimestamp(expiry, tz=UTC))
        if not safe:
            raise SourceAuthError("没有检测到允许域名的登录会话，请确认已在打开的浏览器中完成登录")
        header = "; ".join(f"{name}={safe[name]}" for name in sorted(safe))
        return header, sorted(safe), max(expiries) if expiries else None

    async def _launch_visible_browser(
        self,
        spec: SourceAuthSpec,
    ) -> tuple[_Playwright, _Browser, _BrowserContext]:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise SourceAuthError(
                "尚未安装网页授权组件。请在项目目录运行 `python bootstrap.py --auth`；"
                "它会同时安装 Playwright 与 Chromium。"
            ) from exc
        playwright = await async_playwright().start()
        try:
            browser = await playwright.chromium.launch(headless=False)
            context = await browser.new_context(locale="zh-CN")
            page = await context.new_page()
            await page.goto(spec.login_url, wait_until="domcontentloaded", timeout=30_000)
            return playwright, browser, context
        except Exception as exc:
            await playwright.stop()
            raise SourceAuthError(
                "未能打开可见授权浏览器，请检查 Chromium 是否安装以及本机是否允许弹出窗口"
            ) from exc

    async def _close_session(self, session: _LiveSession) -> None:
        with suppress(Exception):
            await session.context.close()
        with suppress(Exception):
            await session.browser.close()
        with suppress(Exception):
            await session.playwright.stop()

    async def _expire_sessions(self) -> None:
        now = self._now()
        expired = [
            session
            for session in self._sessions.values()
            if session.status == "authorizing" and session.expires_at <= now
        ]
        for session in expired:
            session.status = "expired"
            session.message = "授权窗口已超过 15 分钟，请重新开始。"
            await self._close_session(session)

    def _session_view(self, session: _LiveSession) -> SourceAuthSessionView:
        return SourceAuthSessionView(
            session_id=session.session_id,
            source_id=session.spec.source_id,
            source_name=session.spec.source_name,
            status=session.status,
            started_at=session.started_at,
            expires_at=session.expires_at,
            login_url=session.spec.login_url,
            message=session.message,
        )

    async def start(self, source_id: str) -> SourceAuthSessionView:
        async with self._lock:
            await self._expire_sessions()
            spec = self.specs.get(source_id)
            if spec is None:
                raise SourceAuthError("该来源当前没有可安全复用、且能真实增强检索的网页授权流程")
            existing = next(
                (
                    item
                    for item in self._sessions.values()
                    if item.spec.source_id == source_id and item.status == "authorizing"
                ),
                None,
            )
            if existing:
                return self._session_view(existing)
            playwright, browser, context = await self._launch_visible_browser(spec)
            now = self._now()
            session = _LiveSession(
                session_id=uuid4().hex,
                spec=spec,
                started_at=now,
                expires_at=now + self.session_ttl,
                playwright=playwright,
                browser=browser,
                context=context,
            )
            self._sessions[session.session_id] = session
            return self._session_view(session)

    async def session(self, session_id: str) -> SourceAuthSessionView:
        async with self._lock:
            await self._expire_sessions()
            session = self._sessions.get(session_id)
            if session is None:
                raise SourceAuthError("授权会话不存在或服务重启后已失效，请重新开始")
            return self._session_view(session)

    async def complete(self, session_id: str) -> SourceAuthSessionView:
        async with self._lock:
            await self._expire_sessions()
            session = self._sessions.get(session_id)
            if session is None:
                raise SourceAuthError("授权会话不存在或服务重启后已失效，请重新开始")
            if session.status != "authorizing":
                return self._session_view(session)
            try:
                cookies = await session.context.cookies(session.spec.allowed_urls)
                header, names, expires_at = self._serialize_cookies(
                    cookies,
                    session.spec.allowed_domains,
                )
                now = self._now()
                self.db.set_source_authorization(
                    source_id=session.spec.source_id,
                    encrypted_cookie=self.vault.encrypt(header),
                    cookie_names=names,
                    domains=list(session.spec.allowed_domains),
                    authorized_at=now.isoformat(),
                    expires_at=expires_at.isoformat() if expires_at else None,
                    message=(
                        f"已加密保存 {len(names)} 个允许域名的会话 Cookie；"
                        "未保存账号、密码、验证码或 CA 信息。"
                    ),
                )
                setattr(self.settings, session.spec.setting_field, header)
                session.status = "completed"
                session.message = "授权会话已加密保存。建议立即点击“测试授权”确认站点仍认可该会话。"
            except SourceAuthError as exc:
                session.status = "failed"
                session.message = str(exc)
            finally:
                await self._close_session(session)
            return self._session_view(session)

    def _authorization_row(self, source_id: str) -> dict[str, Any] | None:
        return self.db.get_source_authorization(source_id)

    def status(self, source_id: str) -> SourceAuthView:
        source = self.sources.get(source_id)
        spec = self.specs.get(source_id)
        name = source.name if source else source_id
        if spec is None:
            return SourceAuthView(
                source_id=source_id,
                source_name=name,
                managed=False,
                state="not_supported",
                login_url=getattr(source, "authorization_url", "") if source else "",
                authorization_scope=(
                    "原站可能提供登录、CA 或工作台，但当前适配器不会消费该会话来扩大公开检索。"
                    if source and source.authorization_supported
                    else "该公开来源无需授权。"
                ),
                message="没有保存任何登录会话。",
            )
        active = next(
            (
                item
                for item in self._sessions.values()
                if item.spec.source_id == source_id and item.status == "authorizing"
            ),
            None,
        )
        if active:
            return SourceAuthView(
                source_id=source_id,
                source_name=name,
                managed=True,
                state="authorizing",
                login_url=spec.login_url,
                authorization_scope=spec.authorization_scope,
                expires_at=active.expires_at,
                active_session_id=active.session_id,
                message=active.message,
            )
        row = self._authorization_row(source_id)
        if not row:
            return SourceAuthView(
                source_id=source_id,
                source_name=name,
                managed=True,
                state="not_authorized",
                login_url=spec.login_url,
                authorization_scope=spec.authorization_scope,
                message="尚未保存授权会话。",
            )
        authorized_at = self._parse_datetime(row.get("authorized_at"))
        expires_at = self._parse_datetime(row.get("expires_at"))
        state: Literal["authorized", "expired"] = (
            "expired" if expires_at and expires_at <= self._now() else "authorized"
        )
        return SourceAuthView(
            source_id=source_id,
            source_name=name,
            managed=True,
            state=state,
            login_url=spec.login_url,
            authorization_scope=spec.authorization_scope,
            authorized_at=authorized_at,
            expires_at=expires_at,
            last_test_at=self._parse_datetime(row.get("last_test_at")),
            last_test_status=row.get("last_test_status") or "not_tested",
            message=row.get("last_message") or "授权会话已加密保存。",
        )

    def list_status(self) -> list[SourceAuthView]:
        return [self.status(source_id) for source_id in self.sources]

    async def test(self, source_id: str) -> SourceAuthTestResult:
        from time import perf_counter

        spec = self.specs.get(source_id)
        source = self.sources.get(source_id)
        if spec is None or source is None:
            raise SourceAuthError("该来源没有可测试的检索增强授权")
        row = self._authorization_row(source_id)
        if not row:
            raise SourceAuthError("尚未保存授权会话，请先开始并完成授权")
        self.load_persisted()
        started = perf_counter()
        today = date.today()
        query = TenderQuerySpec(
            raw_query="授权连接测试：服务器",
            topic="服务器",
            keywords=["服务器"],
            start_date=today - timedelta(days=365),
            end_date=today,
            schedule=IntentSchedule(kind=ScheduleKind.IMMEDIATE),
        )
        try:
            async with HttpFetcher(self.settings) as fetcher:
                result = await source.search(query, fetcher)
            authenticated_items = [
                item for item in result.items if item.auth_level != "public_snippet"
            ]
            success = result.status not in {
                SourceStatus.AUTH_REQUIRED,
                SourceStatus.FAILED,
            } and bool(authenticated_items)
            if success:
                message = f"授权有效，真实检索读取到 {len(authenticated_items)} 条会员可见候选。"
            elif result.status == SourceStatus.AUTH_REQUIRED:
                message = "站点要求重新登录，当前会话可能已过期。"
            else:
                message = "站点可访问，但本次没有证明会员详情已解锁；请重新授权后再试。"
        except Exception:
            success = False
            message = "授权测试未通过，请检查网络、登录状态或站点结构是否变化。"
        status: Literal["passed", "failed"] = "passed" if success else "failed"
        self.db.update_source_authorization_test(source_id, status=status, message=message)
        return SourceAuthTestResult(
            source_id=source_id,
            success=success,
            status=status,
            message=message,
            latency_ms=round((perf_counter() - started) * 1000),
        )

    async def clear(self, source_id: str) -> SourceAuthView:
        async with self._lock:
            spec = self.specs.get(source_id)
            if spec is None:
                raise SourceAuthError("该来源没有由系统管理的授权会话")
            live = [item for item in self._sessions.values() if item.spec.source_id == source_id]
            for session in live:
                await self._close_session(session)
                self._sessions.pop(session.session_id, None)
            self.db.delete_source_authorization(source_id)
            setattr(self.settings, spec.setting_field, "")
            return self.status(source_id)

    async def close_all(self) -> None:
        async with self._lock:
            for session in list(self._sessions.values()):
                await self._close_session(session)
            self._sessions.clear()
