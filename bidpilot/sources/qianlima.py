from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import time
from contextlib import suppress
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from bidpilot.clean import (
    clean_html,
    detect_event_type,
    extract_project_id,
    find_attachments,
    normalize_space,
    parse_datetime,
)
from bidpilot.config import Settings
from bidpilot.fetch import FetchError, HttpFetcher
from bidpilot.models import (
    Attachment,
    EventType,
    EvidenceSpan,
    RawTender,
    ScheduleKind,
    SourceSearchResult,
    SourceStatus,
    TenderQuerySpec,
)
from bidpilot.sources.base import SourceAdapter


class QianlimaSource(SourceAdapter):
    source_id = "qianlima"
    name = "千里马招标网"
    requires_auth = False
    base_url = "https://wap.qianlima.com"
    homepage = "https://www.qianlima.com"
    access_mode = "public_user_assisted"
    query_mode = "public_feed_plus_user_foreground"
    supports_query_variants = False
    supports_pagination = True
    supports_detail = False
    authorization_supported = True
    authorization_url = "https://search.vip.qianlima.com/"
    authorization_action_label = "在系统内免费登录"
    coverage_note = (
        "定时任务只读取无需登录的公开分类列表。用户本人在来源状态页面完成免费登录后，"
        "即时任务可通过系统托管的同一个持久浏览器配置执行有界免费列表检索；只有已有书面授权或"
        "官方 API 记录编号且管理员显式开启后，监控任务才可在页数、间隔和每日预算内复用。"
        "授权、Web、worker 与清除使用跨进程 profile 锁。不导出或后台重放 Cookie，"
        "也不把 Cookie 交给 HTTP 客户端，"
        "不读取付费详情，不绕过验证码、WAF、账号权限或原站频率限制。"
    )

    profile_marker_value = "qianlima-persistent-browser-v1"
    usage_state_version = "1"

    _EVENT_FEEDS = {
        EventType.TENDER: "/zbgg/",
        EventType.INTENTION: "/zbyg/",
        EventType.AWARD: "/zbjg/",
        EventType.CHANGE: "/zbbg/",
    }

    def __init__(self, settings: Settings):
        self.settings = settings
        self._foreground_lock = asyncio.Lock()
        self._last_foreground_search_at = 0.0
        self._live_playwright = None
        self._live_context = None
        self._profile_lock_handle = None

    @property
    def profile_dir(self) -> Path:
        return self.settings.data_dir / "browser_profiles" / self.source_id

    @property
    def profile_state_path(self) -> Path:
        return self.profile_dir / "bidpilot-state.json"

    @property
    def usage_state_path(self) -> Path:
        return self.profile_dir / "bidpilot-usage.json"

    @property
    def usage_lock_path(self) -> Path:
        return self.profile_dir / "bidpilot-usage.lock"

    @property
    def profile_lock_path(self) -> Path:
        return self.profile_dir.parent / f".{self.source_id}.profile.lock"

    def _acquire_profile_lock(self, *, wait_seconds: float = 3.0) -> None:
        """Hold an OS-backed cross-process lease for the complete Chromium session."""

        if self._profile_lock_handle is not None:
            return
        self.profile_lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.profile_lock_path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._profile_lock_handle = handle
                return
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    handle.close()
                    raise RuntimeError(
                        "千里马浏览器配置正被另一个 Web、worker 或授权进程使用，请稍后重试"
                    ) from None
                time.sleep(0.05)

    def _release_profile_lock(self) -> None:
        handle = self._profile_lock_handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._profile_lock_handle = None

    def _read_profile_state(self) -> dict[str, str]:
        try:
            value = json.loads(self.profile_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    def mark_profile(
        self,
        state: str,
        *,
        expires_at: datetime | None = None,
    ) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": "1",
            "state": state,
            "updated_at": datetime.now(UTC).isoformat(),
            "expires_at": expires_at.isoformat() if expires_at else "",
        }
        self.profile_state_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def profile_available(self, *, allow_unverified: bool = False) -> bool:
        state = self._read_profile_state()
        if state.get("version") != "1":
            return False
        if allow_unverified and state.get("state") == "captured_unverified":
            return True
        if state.get("state") != "authorized":
            return False
        # Chromium owns the actual login lifetime. Legacy markers may still
        # carry a seven-day BidPilot timestamp, but every member query now
        # health-checks the live site and marks the profile expired on failure.
        return True

    @staticmethod
    def graphical_session_available() -> bool:
        """Return whether a visible Chromium window can be presented to the operator."""
        if sys.platform in {"win32", "darwin"}:
            return True
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

    def search_browser_headless(self) -> bool:
        """Resolve the persistent search browser mode without weakening login boundaries."""
        mode = self.settings.qianlima_browser_mode
        if mode == "headless":
            return True
        if mode == "visible":
            if not self.graphical_session_available():
                raise RuntimeError(
                    "千里马浏览器模式设为 visible，但当前 Linux 服务没有 DISPLAY/WAYLAND_DISPLAY；"
                    "请通过本机桌面或受 SSH 隧道保护的图形会话登录，或改为 auto/headless 复用已验证配置。"
                )
            return False
        return not self.graphical_session_available()

    async def _launch_persistent_browser(self, *, headless: bool):
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "尚未安装网页授权组件。请在项目目录运行 `python bootstrap.py --auth`。"
            ) from exc
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        playwright = await async_playwright().start()
        try:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=headless,
                locale="zh-CN",
            )
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(self.authorization_url, wait_until="domcontentloaded", timeout=30_000)
            return playwright, None, context
        except Exception:
            await playwright.stop()
            raise

    async def launch_authorization_browser(self):
        """Launch the operator-visible browser used only for interactive login."""
        if self._live_playwright is not None and self._live_context is not None:
            try:
                page = (
                    self._live_context.pages[0]
                    if self._live_context.pages
                    else await self._live_context.new_page()
                )
                await page.goto(
                    self.authorization_url,
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                return self._live_playwright, None, self._live_context
            except Exception:
                await self.close_live_browser()
        if not self.graphical_session_available():
            raise RuntimeError(
                "当前 Linux 服务没有可见图形会话，不能让用户安全完成登录。请在桌面会话中运行授权，"
                "或按 Linux 部署手册通过仅监听 127.0.0.1 的 noVNC/VNC 并使用 SSH 隧道操作；"
                "系统不会在不可见窗口中代填账号、验证码或冒充登录成功。"
            )
        await asyncio.to_thread(self._acquire_profile_lock)
        try:
            return await self._launch_persistent_browser(headless=False)
        except Exception:
            await asyncio.to_thread(self._release_profile_lock)
            raise

    async def launch_search_browser(self):
        """Open the verified profile for one bounded search, headless on servers when needed."""
        await asyncio.to_thread(self._acquire_profile_lock)
        try:
            return await self._launch_persistent_browser(headless=self.search_browser_headless())
        except Exception:
            await asyncio.to_thread(self._release_profile_lock)
            raise

    def adopt_live_context(self, playwright, context) -> None:
        self._live_playwright = playwright
        self._live_context = context

    async def close_live_browser(self) -> None:
        if self._live_context is not None:
            with suppress(Exception):
                await self._live_context.close()
        if self._live_playwright is not None:
            with suppress(Exception):
                await self._live_playwright.stop()
        self._live_context = None
        self._live_playwright = None
        await asyncio.to_thread(self._release_profile_lock)

    @staticmethod
    async def _authorized_search_page(context, *, timeout_ms: int = 6_000):
        """Wait for the SPA to expose both search and signed-in account controls."""

        deadline = time.monotonic() + max(0, timeout_ms) / 1000
        while True:
            for page in context.pages:
                if not page.url.startswith("https://search.vip.qianlima.com/"):
                    continue
                search_box = page.locator('input[placeholder="请输入您要搜索的内容"]')
                member_center = page.get_by_text("会员中心", exact=True)
                personal_center = page.locator('a[href*="vip.qianlima.com/index.html"]')
                if (
                    await search_box.count() == 1
                    and await member_center.count() >= 1
                    and await personal_center.count() >= 1
                ):
                    return page
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.2)

    @classmethod
    async def authorization_context_ready(cls, context) -> bool:
        return await cls._authorized_search_page(context) is not None

    async def clear_profile(self) -> None:
        await self.close_live_browser()
        await asyncio.to_thread(self._acquire_profile_lock)
        try:
            root = self.settings.data_dir.resolve()
            target = self.profile_dir.resolve()
            if target == root or root not in target.parents:
                raise RuntimeError("浏览器配置目录越出 BidPilot 数据目录，拒绝清理")
            if target.exists():
                shutil.rmtree(target)
        finally:
            await asyncio.to_thread(self._release_profile_lock)

    def _read_usage_state(self) -> dict[str, object]:
        try:
            value = json.loads(self.usage_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_usage_state(self, state: dict[str, object]) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.usage_state_path.with_name(
            f"{self.usage_state_path.name}.{os.getpid()}.tmp"
        )
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self.usage_state_path)

    def _with_usage_lock(self, operation):
        """Serialize the tiny audit ledger across web/worker processes."""

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 3.0
        while True:
            try:
                descriptor = os.open(
                    self.usage_lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                os.close(descriptor)
                break
            except FileExistsError:
                with suppress(OSError):
                    if time.time() - self.usage_lock_path.stat().st_mtime > 120:
                        self.usage_lock_path.unlink()
                        continue
                if time.monotonic() >= deadline:
                    raise RuntimeError("千里马用量账本正被另一个任务更新，请稍后重试") from None
                time.sleep(0.05)
        try:
            return operation()
        finally:
            with suppress(OSError):
                self.usage_lock_path.unlink()

    def _reserve_member_query(
        self,
        *,
        query: str,
        schedule_kind: ScheduleKind,
    ) -> tuple[bool, str, dict[str, object]]:
        now = datetime.now(UTC)
        local_day = now.astimezone(ZoneInfo(self.settings.timezone)).date().isoformat()
        query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]

        def reserve():
            state = self._read_usage_state()
            if (
                state.get("version") != self.usage_state_version
                or state.get("local_date") != local_day
            ):
                state = {
                    "version": self.usage_state_version,
                    "local_date": local_day,
                    "queries_used": 0,
                }
            last_query_at = str(state.get("last_query_at") or "")
            if last_query_at:
                with suppress(ValueError):
                    parsed = datetime.fromisoformat(last_query_at)
                    parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
                    remaining = (
                        self.settings.qianlima_member_cooldown_seconds
                        - (now - parsed).total_seconds()
                    )
                    if remaining > 0:
                        return (
                            False,
                            f"距上次免费会员查询不足冷却时间，请约 {ceil(remaining)} 秒后重试。",
                            state,
                        )
            used = int(state.get("queries_used") or 0)
            budget = self.settings.qianlima_member_daily_query_budget
            if used >= budget:
                return (
                    False,
                    f"今日免费会员查询预算 {budget} 次已用完，公开分类检索仍会继续。",
                    state,
                )
            state.update(
                {
                    "queries_used": used + 1,
                    "last_query_at": now.isoformat(),
                    "last_query_hash": query_hash,
                    "last_schedule_kind": schedule_kind.value,
                    "last_outcome": "reserved",
                }
            )
            self._write_usage_state(state)
            return True, "", state

        return self._with_usage_lock(reserve)

    def _record_member_query_outcome(
        self,
        *,
        outcome: str,
        pages_read: int = 0,
        scanned: int = 0,
        kept: int = 0,
    ) -> None:
        def record():
            state = self._read_usage_state()
            state.update(
                {
                    "version": self.usage_state_version,
                    "last_health_at": datetime.now(UTC).isoformat(),
                    "last_outcome": outcome,
                    "last_pages_read": pages_read,
                    "last_scanned": scanned,
                    "last_kept": kept,
                }
            )
            self._write_usage_state(state)

        self._with_usage_lock(record)

    @staticmethod
    def _safe_free_list_url(href: str) -> str | None:
        """Allow only list-result links; the adapter never opens these detail URLs."""

        source_url = urljoin("https://www.qianlima.com/", href)
        if not re.fullmatch(r"https://www\.qianlima\.com/bid-\d+\.html", source_url):
            return None
        return source_url

    @classmethod
    def parse_foreground_rows(
        cls,
        rows: list[dict[str, str]],
        limit: int = 20,
        *,
        audit_metadata: dict[str, object] | None = None,
    ) -> list[RawTender]:
        items: list[RawTender] = []
        seen: set[str] = set()
        for row in rows:
            title = normalize_space(row.get("title", ""))
            href = row.get("href", "")
            source_url = cls._safe_free_list_url(href)
            published_text = normalize_space(row.get("published_at", ""))
            if not title or not href or not published_text or source_url in seen:
                continue
            if source_url is None:
                continue
            published = parse_datetime(published_text)
            event_label = normalize_space(row.get("event_label", ""))
            region = normalize_space(row.get("region", "")) or None
            category = normalize_space(row.get("category", ""))
            body = normalize_space(
                " ".join((title, published_text, event_label, region or "", category))
            )
            items.append(
                RawTender(
                    source=cls.name,
                    source_url=source_url,
                    title=title,
                    published_at=published,
                    body=body,
                    region=region,
                    event_type=detect_event_type(f"{event_label} {title}"),
                    project_id=extract_project_id(body),
                    evidence=[EvidenceSpan(text=body[:360], source_url=source_url)],
                    auth_level="free_member",
                    source_metadata={
                        "coverage": "bounded_free_member_list",
                        "browser_session": "persistent_profile",
                        "page": int(row.get("page") or 1),
                        "detail_access": "blocked_by_adapter",
                        **(audit_metadata or {}),
                    },
                )
            )
            seen.add(source_url)
            if len(items) >= limit:
                break
        return items

    async def search_foreground(
        self,
        spec: TenderQuerySpec,
        *,
        allow_unverified: bool = False,
    ) -> SourceSearchResult:
        started = time.perf_counter()
        if (
            spec.schedule.kind != ScheduleKind.IMMEDIATE
            and not self.settings.qianlima_member_monitoring_enabled
        ):
            return SourceSearchResult(
                source=self.name,
                status=SourceStatus.SKIPPED,
                message=(
                    "该监控任务继续使用公开分类列表；只有管理员显式开启千里马会员监控后，"
                    "用户创建的监控任务才会在有界预算内复用登录配置。"
                ),
            )
        if not self.profile_available(allow_unverified=allow_unverified):
            return SourceSearchResult(
                source=self.name,
                status=SourceStatus.AUTH_REQUIRED,
                message="千里马免费登录态尚未验证或已过期，请到来源状态页面重新登录。",
            )
        query = normalize_space(spec.topic)
        if not 2 <= len(query) <= 40:
            return SourceSearchResult(
                source=self.name,
                status=SourceStatus.SKIPPED,
                message="千里马前台查询词必须为 2～40 个字符。",
            )
        async with self._foreground_lock:
            try:
                reserved, reservation_message, usage_state = await asyncio.to_thread(
                    self._reserve_member_query,
                    query=query,
                    schedule_kind=spec.schedule.kind,
                )
            except RuntimeError as exc:
                return SourceSearchResult(
                    source=self.name,
                    status=SourceStatus.SKIPPED,
                    message=str(exc),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            if not reserved:
                return SourceSearchResult(
                    source=self.name,
                    status=SourceStatus.SKIPPED,
                    message=reservation_message,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            owns_context = self._live_context is None
            if owns_context:
                try:
                    playwright, _browser, context = await self.launch_search_browser()
                except Exception as exc:
                    with suppress(Exception):
                        await asyncio.to_thread(
                            self._record_member_query_outcome,
                            outcome="browser_launch_failed",
                        )
                    return SourceSearchResult(
                        source=self.name,
                        status=SourceStatus.FAILED,
                        message=(
                            "未能打开千里马持久浏览器，请检查 Playwright/Chromium、浏览器模式与 Linux 图形会话。"
                            f"（{type(exc).__name__}）"
                        ),
                        latency_ms=int((time.perf_counter() - started) * 1000),
                    )
            else:
                playwright = self._live_playwright
                context = self._live_context
            try:
                page = await self._authorized_search_page(context)
                if page is None:
                    self.mark_profile("expired")
                    await asyncio.to_thread(
                        self._record_member_query_outcome,
                        outcome="auth_required",
                    )
                    return SourceSearchResult(
                        source=self.name,
                        status=SourceStatus.AUTH_REQUIRED,
                        message="千里马已要求重新登录；浏览器配置仍在本机，但本轮没有读取任何会员结果。",
                        latency_ms=int((time.perf_counter() - started) * 1000),
                    )
                search_box = page.locator('input[placeholder="请输入您要搜索的内容"]')
                await search_box.fill(query)
                await search_box.press("Enter")
                try:
                    await page.wait_for_function(
                        """() => document.querySelectorAll(
                          'a.con-title[href*="/bid-"]'
                        ).length > 0""",
                        timeout=10_000,
                    )
                except Exception:
                    # A valid free-member search can genuinely return no rows.
                    # Session health is checked separately above and below.
                    pass
                all_rows: list[dict[str, str]] = []
                pages_read = 0
                max_pages = self.settings.qianlima_member_max_pages
                max_results = self.settings.qianlima_member_max_results
                for page_number in range(1, max_pages + 1):
                    if page_number > 1:
                        pager = page.locator("#dataListPager .pagingUl")
                        page_link = pager.locator("a").filter(
                            has_text=re.compile(rf"^{page_number}$")
                        )
                        if await page_link.count() != 1:
                            break
                        await page_link.click()
                        await page.wait_for_timeout(1_800)
                        active_page = pager.locator("a.activP-d").filter(
                            has_text=re.compile(rf"^{page_number}$")
                        )
                        if await active_page.count() != 1:
                            break
                    if await self._authorized_search_page(context, timeout_ms=1_500) is None:
                        self.mark_profile("expired")
                        await asyncio.to_thread(
                            self._record_member_query_outcome,
                            outcome="auth_required",
                            pages_read=pages_read,
                            scanned=len(all_rows),
                        )
                        return SourceSearchResult(
                            source=self.name,
                            status=SourceStatus.AUTH_REQUIRED,
                            message="千里马在检索过程中要求重新登录，本轮已停止且没有访问详情页。",
                            latency_ms=int((time.perf_counter() - started) * 1000),
                        )
                    remaining = max_results - len(all_rows)
                    if remaining <= 0:
                        break
                    rows = await page.evaluate(
                        """({limit, pageNumber}) => Array.from(document.querySelectorAll('a.con-title[href*="/bid-"]'))
                      .slice(0, limit).map((link) => {
                        const row = link.closest('li');
                        const tags = Array.from(row?.querySelectorAll('a.con-address') || [])
                          .map((item) => (item.textContent || '').replace(/\\s+/g, ' ').trim());
                        const dateText = (row?.textContent || '').match(/20\\d{2}-\\d{2}-\\d{2}/)?.[0] || '';
                        return {
                          title: (link.textContent || '').replace(/\\s+/g, ' ').trim(),
                          href: link.getAttribute('href') || '',
                          published_at: dateText,
                          event_label: tags[0] || '',
                          region: tags[1] || '',
                          category: tags[2] || '',
                          page: String(pageNumber)
                        };
                      })""",
                        {"limit": remaining, "pageNumber": page_number},
                    )
                    if not isinstance(rows, list) or not rows:
                        break
                    all_rows.extend(rows)
                    pages_read += 1
                audit_metadata = {
                    "query_hash": str(usage_state.get("last_query_hash") or ""),
                    "schedule_kind": spec.schedule.kind.value,
                    "pages_requested": max_pages,
                    "pages_read": pages_read,
                    "paid_detail_navigation": "mechanically_disabled",
                }
                items = self.parse_foreground_rows(
                    all_rows,
                    limit=max_results,
                    audit_metadata=audit_metadata,
                )
                outcome = "passed" if items else "no_results"
                self.mark_profile("authorized")
                await asyncio.to_thread(
                    self._record_member_query_outcome,
                    outcome=outcome,
                    pages_read=pages_read,
                    scanned=len(all_rows),
                    kept=len(items),
                )
                return SourceSearchResult(
                    source=self.name,
                    status=SourceStatus.PARTIAL,
                    items=items,
                    scanned_count=len(all_rows),
                    message=(
                        f"已在用户本人登录的系统托管持久浏览器中执行 1 次有界查询，"
                        f"读取 {pages_read} 页、{len(items)} 条免费会员列表结果；"
                        + ("当前关键词未返回列表候选；" if not items else "")
                        + "付费详情导航被适配器机械禁用，未导出或通过 HTTP 重放 Cookie。"
                    ),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            except Exception:
                with suppress(Exception):
                    await asyncio.to_thread(
                        self._record_member_query_outcome,
                        outcome="failed",
                    )
                return SourceSearchResult(
                    source=self.name,
                    status=SourceStatus.FAILED,
                    message="千里马前台页面结构或网络状态发生变化，本轮没有继续重试。",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            finally:
                if owns_context:
                    with suppress(Exception):
                        await context.close()
                    if playwright is not None:
                        with suppress(Exception):
                            await playwright.stop()
                    await asyncio.to_thread(self._release_profile_lock)

    @classmethod
    def parse_search_page(cls, html: str, limit: int = 20) -> list[RawTender]:
        soup = BeautifulSoup(html, "lxml")
        anchors = soup.select(
            '.list-con a[href*="/zb/detail/"],.search-list li a[href],'
            ".result-list li a[href],ul.list li a[href],.search_result li a[href]"
        )
        if not anchors:
            anchors = soup.select('a[href*="qianlima.com/zb/detail/"],a[href*="/zb/detail/"]')
        items: list[RawTender] = []
        seen: set[str] = set()
        for anchor in anchors:
            row = anchor.parent or anchor
            title_node = anchor.select_one(".title")
            title = normalize_space(
                title_node.get_text(" ", strip=True)
                if title_node is not None
                else anchor.get("title", "") or anchor.get_text(" ", strip=True)
            )
            href = urljoin(cls.base_url, anchor.get("href", ""))
            if not title or href in seen or any(word in title for word in ("注册", "登录", "首页")):
                continue
            row_text = normalize_space(
                anchor.get_text(" ", strip=True)
                if title_node is not None
                else row.get_text(" ", strip=True)
            )
            date_match = re.search(r"20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}", row_text)
            published = parse_datetime(date_match.group(0) if date_match else "")
            region_nodes = anchor.select(".second-line span")
            region = (
                normalize_space(region_nodes[0].get_text(" ", strip=True)) if region_nodes else None
            )
            items.append(
                RawTender(
                    source=cls.name,
                    source_url=href,
                    title=title,
                    published_at=published,
                    body=row_text,
                    region=region,
                    event_type=detect_event_type(title),
                    project_id=extract_project_id(row_text),
                    evidence=[EvidenceSpan(text=row_text[:360], source_url=href)],
                    auth_level="public_snippet",
                    source_metadata={"coverage": "public_category_feed"},
                )
            )
            seen.add(href)
            if len(items) >= limit:
                break
        return items

    @staticmethod
    def parse_detail_page(html: str, item: RawTender) -> RawTender:
        soup = BeautifulSoup(html, "lxml")
        candidates = soup.select("article,.article-content,.detail-content,.content")
        root = max(candidates, key=lambda node: len(node.get_text(" ", strip=True)), default=None)
        if root is None:
            return item
        body = clean_html(str(root))
        if body:
            item.body = body
            item.project_id = item.project_id or extract_project_id(body)
            item.evidence = [EvidenceSpan(text=body[:500], source_url=item.source_url)]
            item.attachments = [
                Attachment(name=name, url=url)
                for name, url in find_attachments(root, item.source_url)
            ]
        return item

    async def search(self, spec: TenderQuerySpec, fetcher: HttpFetcher) -> SourceSearchResult:
        if self.profile_available() and (
            spec.schedule.kind == ScheduleKind.IMMEDIATE
            or self.settings.qianlima_member_monitoring_enabled
        ):
            return await self.search_foreground(spec)
        started = time.perf_counter()
        try:
            requested_events = list(dict.fromkeys(spec.event_types)) or list(self._EVENT_FEEDS)
            feeds = [
                (event_type, self._EVENT_FEEDS[event_type])
                for event_type in requested_events
                if event_type in self._EVENT_FEEDS
            ]
            if not feeds:
                return SourceSearchResult(
                    source=self.name,
                    status=SourceStatus.SKIPPED,
                    message="公开分类列表不覆盖本次指定的公告类型，已诚实跳过。",
                    latency_ms=0,
                )
            limit = self.settings.max_results_per_source
            per_feed_limit = max(1, ceil(limit / len(feeds)))
            items: list[RawTender] = []
            seen: set[str] = set()
            scanned = 0
            max_pages = 2 if len(feeds) == 1 else 1
            for event_type, path in feeds:
                feed_items: list[RawTender] = []
                for page_number in range(1, max_pages + 1):
                    page_path = path if page_number == 1 else f"{path.rstrip('/')}/p{page_number}"
                    page = await fetcher.get(
                        urljoin(self.base_url, page_path),
                        encoding="utf-8",
                        retries=1,
                    )
                    parsed = self.parse_search_page(page.text, per_feed_limit)
                    scanned += len(parsed)
                    for item in parsed:
                        if item.event_type == EventType.OTHER:
                            item.event_type = event_type
                        if item.source_url not in seen:
                            feed_items.append(item)
                            seen.add(item.source_url)
                        if len(feed_items) >= per_feed_limit:
                            break
                    if len(feed_items) >= per_feed_limit or not parsed:
                        break
                items.extend(feed_items)
            items = items[:limit]
            return SourceSearchResult(
                source=self.name,
                status=SourceStatus.PARTIAL,
                items=items,
                scanned_count=scanned,
                message=(
                    "已读取无需登录的公开分类列表；主题、地域和日期由本地硬校验。"
                    "自动任务不会保存或重放会员 Cookie，也不会绕过付费详情。"
                ),
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        except FetchError as exc:
            return SourceSearchResult(
                source=self.name,
                status=SourceStatus.FAILED,
                message=str(exc),
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
