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
from bidpilot.sources.qianlima import QianlimaSource

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
        "captured_unverified",
        "authorized",
        "expired",
        "failed",
    ] = Field(description="脱敏授权状态")
    login_url: str = Field(default="", description="用户可见浏览器打开的官方登录页")
    authorization_scope: str = Field(default="", description="授权能增强的实际范围")
    authorized_at: datetime | None = None
    expires_at: datetime | None = None
    last_test_at: datetime | None = None
    last_test_status: Literal["not_tested", "passed", "failed", "inconclusive"] = "not_tested"
    active_session_id: str = Field(
        default="",
        description="仅在服务主机授权窗口进行中时返回的一次性会话 ID",
    )
    message: str = ""


class SourceAuthSessionView(BaseModel):
    session_id: str = Field(description="一次性服务主机授权会话 ID")
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
    status: Literal["passed", "failed", "inconclusive"]
    message: str
    latency_ms: int = Field(default=0, ge=0)


@dataclass(frozen=True, slots=True)
class SourceAuthSpec:
    source_id: str
    source_name: str
    login_url: str
    allowed_domains: tuple[str, ...]
    setting_field: str | None
    authorization_scope: str
    verification_queries: tuple[str, ...]
    session_mode: Literal["cookie", "persistent_browser"] = "cookie"

    @property
    def allowed_urls(self) -> list[str]:
        return [f"https://{domain}/" for domain in self.allowed_domains]


class _BrowserContext(Protocol):
    pages: list[Any]

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
    browser: _Browser | None
    context: _BrowserContext
    status: Literal["authorizing", "completed", "failed", "expired"] = "authorizing"
    message: str = (
        "可见浏览器已在运行 BidPilot 服务的电脑上打开。请在那台电脑亲自完成登录，"
        "再回到当前来源中心点击“完成授权”。"
    )


class SourceAuthManager:
    """User-driven source authorization with explicit per-source session boundaries."""

    session_ttl = timedelta(minutes=15)
    verification_ttl = timedelta(days=7)

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
        self._disable_legacy_qianlima_replay()
        self.load_persisted()

    def _build_specs(self) -> dict[str, SourceAuthSpec]:
        configured = (
            SourceAuthSpec(
                source_id="cecbid",
                source_name="中国招标投标网",
                login_url="https://www.cecbid.org.cn/login",
                allowed_domains=("www.cecbid.org.cn", "cecbid.org.cn"),
                setting_field="cecbid_cookie",
                authorization_scope=(
                    "经真实验证后，会员会话会同时用于站内搜索和账号原本可见的免费会员详情。"
                ),
                verification_queries=("购买", "服务", "工程"),
            ),
            SourceAuthSpec(
                source_id="qianlima",
                source_name="千里马招标网",
                login_url="https://search.vip.qianlima.com/",
                allowed_domains=("search.vip.qianlima.com", "vip.qianlima.com"),
                setting_field=None,
                authorization_scope=(
                    "保留用户本人登录的独立浏览器配置文件；即时任务执行有界免费列表检索，"
                    "只有已有书面授权或官方 API 记录编号且管理员显式开启后，监控任务才可复用；"
                    "所有查询仍受页数、冷却和每日预算限制，不导出 Cookie、不读取付费详情。"
                ),
                verification_queries=("服务器",),
                session_mode="persistent_browser",
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

    def _disable_legacy_qianlima_replay(self) -> None:
        """Remove obsolete Cookie replay while preserving the new browser-profile marker."""

        path: Path = self.settings.data_dir / "secrets" / "qianlima_cookie.txt"
        with suppress(OSError):
            path.unlink(missing_ok=True)
        row = self.db.get_source_authorization("qianlima")
        if not row:
            return
        try:
            value, _ = self.vault.decrypt(row["encrypted_cookie"])
        except RuntimeConfigError:
            value = ""
        source = self.sources.get("qianlima")
        keep = (
            value == QianlimaSource.profile_marker_value
            and isinstance(source, QianlimaSource)
            and source.profile_available(allow_unverified=True)
        )
        if not keep:
            self.db.delete_source_authorization("qianlima")

    def load_persisted(self) -> None:
        for spec in self.specs.values():
            row = self.db.get_source_authorization(spec.source_id)
            if spec.session_mode == "persistent_browser":
                continue
            value = ""
            if row:
                expires_at = self._parse_datetime(row.get("expires_at"))
                if row.get("last_test_status") == "passed" and (
                    not expires_at or expires_at > self._now()
                ):
                    try:
                        value, _ = self.vault.decrypt(row["encrypted_cookie"])
                    except RuntimeConfigError:
                        value = ""
            if spec.setting_field:
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
        # A long-lived tracking/remember cookie must not make a shorter login
        # session look valid for longer than it really is. The earliest declared
        # expiry is deliberately conservative; session cookies remain test-gated.
        return header, sorted(safe), min(expiries) if expiries else None

    async def _launch_visible_browser(
        self,
        spec: SourceAuthSpec,
    ) -> tuple[_Playwright, _Browser | None, _BrowserContext]:
        if spec.session_mode == "persistent_browser":
            source = self.sources.get(spec.source_id)
            if not isinstance(source, QianlimaSource):
                raise SourceAuthError("千里马持久浏览器来源没有正确加载")
            try:
                return await source.launch_authorization_browser()
            except RuntimeError as exc:
                raise SourceAuthError(str(exc)) from exc
            except Exception as exc:
                raise SourceAuthError(
                    "未能打开千里马持久浏览器，请检查 Chromium 安装或关闭占用该配置文件的窗口"
                ) from exc
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
        if session.spec.session_mode == "persistent_browser":
            source = self.sources.get(session.spec.source_id)
            if isinstance(source, QianlimaSource):
                await source.close_live_browser()
                return
        with suppress(Exception):
            await session.context.close()
        if session.browser is not None:
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
                now = self._now()
                if session.spec.session_mode == "persistent_browser":
                    source = self.sources.get(session.spec.source_id)
                    if not isinstance(source, QianlimaSource):
                        raise SourceAuthError("千里马持久浏览器来源没有正确加载")
                    if not await source.authorization_context_ready(session.context):
                        raise SourceAuthError(
                            "没有检测到千里马免费会员登录态；请在打开的浏览器中完成登录后再点完成"
                        )
                    source.mark_profile("captured_unverified")
                    source.adopt_live_context(session.playwright, session.context)
                    self.db.set_source_authorization(
                        source_id=session.spec.source_id,
                        encrypted_cookie=self.vault.encrypt(source.profile_marker_value),
                        cookie_names=[],
                        domains=list(session.spec.allowed_domains),
                        authorized_at=now.isoformat(),
                        expires_at=None,
                        message=(
                            "已保留独立浏览器配置文件；没有导出 Cookie，也没有保存账号、密码或验证码。"
                        ),
                    )
                    session.message = (
                        "已识别免费会员登录态且没有导出 Cookie，正在等待一次首屏真实检索验证；"
                        "验证通过前不会用于普通即时任务。"
                    )
                else:
                    cookies = await session.context.cookies(session.spec.allowed_urls)
                    header, names, expires_at = self._serialize_cookies(
                        cookies,
                        session.spec.allowed_domains,
                    )
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
                    session.message = (
                        "已加密捕获允许域名的会话，正在等待真实验证；验证通过前不会用于后台检索。"
                    )
                session.status = "completed"
            except SourceAuthError as exc:
                session.status = "failed"
                session.message = str(exc)
            finally:
                if session.spec.session_mode != "persistent_browser" or session.status == "failed":
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
        last_test_status = row.get("last_test_status") or "not_tested"
        if spec.session_mode == "persistent_browser" and isinstance(source, QianlimaSource):
            # The persisted Chromium profile, not the legacy seven-day DB
            # timestamp, is authoritative. A failed live health check marks
            # the profile expired and is reflected here immediately.
            expires_at = None
            if last_test_status == "passed" and source.profile_available():
                state = "authorized"
            elif last_test_status == "failed":
                state = "failed"
            elif source.profile_available(allow_unverified=True):
                state = "captured_unverified"
            else:
                state = "expired"
        elif expires_at and expires_at <= self._now():
            state: Literal["captured_unverified", "authorized", "expired", "failed"] = "expired"
        elif last_test_status == "passed":
            state = "authorized"
        elif last_test_status == "failed":
            state = "failed"
        else:
            state = "captured_unverified"
        display_last_test_status = last_test_status
        display_message = row.get("last_message") or "会话已捕获，等待真实验证。"
        if (
            spec.session_mode == "persistent_browser"
            and state == "expired"
            and last_test_status == "passed"
        ):
            display_last_test_status = "failed"
            display_message = "原站实时健康检查已判定持久会话失效，请重新登录后验证。"
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
            last_test_status=display_last_test_status,
            message=display_message,
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
        expires_at = self._parse_datetime(row.get("expires_at"))
        if expires_at and expires_at <= self._now() and spec.session_mode != "persistent_browser":
            if spec.setting_field:
                setattr(self.settings, spec.setting_field, "")
            raise SourceAuthError("授权会话已过期，请重新登录")
        try:
            session_value, _ = self.vault.decrypt(row["encrypted_cookie"])
        except RuntimeConfigError as exc:
            if spec.setting_field:
                setattr(self.settings, spec.setting_field, "")
            raise SourceAuthError("本机无法解密授权会话，请清除后重新登录") from exc
        if spec.session_mode == "persistent_browser":
            if (
                not isinstance(source, QianlimaSource)
                or session_value != source.profile_marker_value
                or not source.profile_available(allow_unverified=True)
            ):
                raise SourceAuthError("千里马浏览器配置文件不可用，请清除后重新登录")
        elif spec.setting_field:
            setattr(self.settings, spec.setting_field, session_value)
        started = perf_counter()
        today = date.today()
        success = False
        outcome: Literal["passed", "failed", "inconclusive"] = "inconclusive"
        message = ""
        saw_reachable_probe = False
        saw_candidates = False
        try:
            async with HttpFetcher(self.settings) as fetcher:
                for probe in spec.verification_queries:
                    query = TenderQuerySpec(
                        raw_query=f"授权连接测试：{probe}",
                        topic=probe,
                        keywords=[probe],
                        start_date=today - timedelta(days=365),
                        end_date=today,
                        schedule=IntentSchedule(kind=ScheduleKind.IMMEDIATE),
                    )
                    result = (
                        await source.search_foreground(query, allow_unverified=True)
                        if isinstance(source, QianlimaSource)
                        else await source.search(query, fetcher)
                    )
                    if result.status == SourceStatus.FAILED:
                        continue
                    saw_reachable_probe = True
                    if result.status == SourceStatus.AUTH_REQUIRED:
                        outcome = "failed"
                        message = "站点仍显示登录或会员门禁，当前会话没有通过验证。"
                        break
                    saw_candidates = saw_candidates or bool(result.items)
                    authenticated_items = [
                        item for item in result.items if item.auth_level == "free_member"
                    ]
                    if authenticated_items:
                        success = True
                        outcome = "passed"
                        message = (
                            "千里马免费登录态有效：已在系统托管持久浏览器执行一次有界列表检索，"
                            f"读取到 {len(authenticated_items)} 条免费会员列表候选；"
                            "未读取付费详情、未导出 Cookie。"
                            if isinstance(source, QianlimaSource)
                            else (
                                "授权有效：真实站内搜索与会员详情均已解锁，"
                                f"本次验证读取到 {len(authenticated_items)} 条会员可见候选。"
                            )
                        )
                        break
                else:
                    if not saw_reachable_probe:
                        outcome = "failed"
                        message = "授权测试无法访问站点，请检查网络或站点可用性。"
                    elif not saw_candidates:
                        outcome = "inconclusive"
                        message = (
                            "站点可访问，但多个验证词均没有候选，暂时无法判断登录是否有效；"
                            "会话不会用于后台检索，请稍后重试。"
                        )
                    else:
                        outcome = "failed"
                        message = "检索有候选，但会员详情仍未解锁，请重新登录后再试。"
        except Exception:
            outcome = "failed"
            message = "授权测试未通过，请检查网络、登录状态或站点结构是否变化。"
        if outcome != "passed" and spec.setting_field:
            setattr(self.settings, spec.setting_field, "")
        verified_until = None
        if outcome == "passed":
            if isinstance(source, QianlimaSource):
                # The browser profile persists until the original site expires
                # it. Every use performs a real health probe, so an arbitrary
                # seven-day BidPilot timer would only create false negatives.
                source.mark_profile("authorized")
            else:
                verification_cap = self._now() + self.verification_ttl
                verified_until = (
                    min(expires_at, verification_cap) if expires_at else verification_cap
                )
        preserve_verified_profile = (
            isinstance(source, QianlimaSource)
            and outcome == "inconclusive"
            and row.get("last_test_status") == "passed"
            and source.profile_available()
        )
        if not preserve_verified_profile:
            self.db.update_source_authorization_test(
                source_id,
                status=outcome,
                message=message,
                expires_at=verified_until.isoformat() if verified_until else None,
            )
        if isinstance(source, QianlimaSource):
            # Release Chromium's profile lock so a standalone Linux worker can
            # reopen the same verified profile after the web authorization flow.
            await source.close_live_browser()
        return SourceAuthTestResult(
            source_id=source_id,
            success=success,
            status=outcome,
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
            if isinstance(self.sources.get(source_id), QianlimaSource):
                try:
                    await self.sources[source_id].clear_profile()  # type: ignore[attr-defined]
                except RuntimeError as exc:
                    raise SourceAuthError(str(exc)) from exc
            elif spec.setting_field:
                setattr(self.settings, spec.setting_field, "")
            return self.status(source_id)

    async def close_all(self) -> None:
        async with self._lock:
            for session in list(self._sessions.values()):
                await self._close_session(session)
            self._sessions.clear()
