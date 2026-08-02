from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from contextlib import suppress
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from urllib.parse import urljoin

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
        "定时任务只读取无需登录的公开分类列表。用户本人在来源中心完成免费登录后，"
        "即时任务可通过同一个可见浏览器配置文件执行一次首屏检索；不导出或后台重放 Cookie，"
        "不自动翻页、不读取付费详情，也不把会员检索接入定时任务。"
    )

    profile_marker_value = "qianlima-persistent-browser-v1"
    foreground_cooldown_seconds = 10.0

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

    @property
    def profile_dir(self) -> Path:
        return self.settings.data_dir / "browser_profiles" / self.source_id

    @property
    def profile_state_path(self) -> Path:
        return self.profile_dir / "bidpilot-state.json"

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
        expires_at = state.get("expires_at") or ""
        if not expires_at:
            return False
        with suppress(ValueError):
            parsed = datetime.fromisoformat(expires_at)
            parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            return parsed > datetime.now(UTC)
        return False

    async def launch_authorization_browser(self):
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
                headless=False,
                locale="zh-CN",
            )
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(self.authorization_url, wait_until="domcontentloaded", timeout=30_000)
            return playwright, None, context
        except Exception:
            await playwright.stop()
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

    @staticmethod
    async def _authorized_search_page(context):
        for page in context.pages:
            if not page.url.startswith("https://search.vip.qianlima.com/"):
                continue
            search_box = page.locator('input[placeholder="请输入您要搜索的内容"]')
            member_center = page.get_by_text("会员中心", exact=True)
            if await search_box.count() == 1 and await member_center.count() >= 1:
                return page
        return None

    @classmethod
    async def authorization_context_ready(cls, context) -> bool:
        return await cls._authorized_search_page(context) is not None

    async def clear_profile(self) -> None:
        await self.close_live_browser()
        root = self.settings.data_dir.resolve()
        target = self.profile_dir.resolve()
        if target == root or root not in target.parents:
            raise RuntimeError("浏览器配置目录越出 BidPilot 数据目录，拒绝清理")
        if target.exists():
            shutil.rmtree(target)

    @classmethod
    def parse_foreground_rows(cls, rows: list[dict[str, str]], limit: int = 20) -> list[RawTender]:
        items: list[RawTender] = []
        seen: set[str] = set()
        for row in rows:
            title = normalize_space(row.get("title", ""))
            href = row.get("href", "")
            source_url = urljoin("https://www.qianlima.com/", href)
            published_text = normalize_space(row.get("published_at", ""))
            if not title or not href or not published_text or source_url in seen:
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
                        "coverage": "user_triggered_first_page",
                        "browser_session": "persistent_profile",
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
        if spec.schedule.kind != ScheduleKind.IMMEDIATE:
            return SourceSearchResult(
                source=self.name,
                status=SourceStatus.SKIPPED,
                message="会员浏览器检索只允许用户主动的即时任务，定时任务继续使用公开分类列表。",
            )
        if not self.profile_available(allow_unverified=allow_unverified):
            return SourceSearchResult(
                source=self.name,
                status=SourceStatus.AUTH_REQUIRED,
                message="千里马免费登录态尚未验证或已过期，请到来源中心重新登录。",
            )
        query = normalize_space(spec.topic)
        if not 2 <= len(query) <= 40:
            return SourceSearchResult(
                source=self.name,
                status=SourceStatus.SKIPPED,
                message="千里马前台查询词必须为 2～40 个字符。",
            )
        async with self._foreground_lock:
            now = time.monotonic()
            if now - self._last_foreground_search_at < self.foreground_cooldown_seconds:
                return SourceSearchResult(
                    source=self.name,
                    status=SourceStatus.SKIPPED,
                    message="为遵守原站频率边界，10 秒内只执行一次用户前台查询。",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            self._last_foreground_search_at = now
            owns_context = self._live_context is None
            if owns_context:
                try:
                    playwright, _browser, context = await self.launch_authorization_browser()
                except Exception:
                    return SourceSearchResult(
                        source=self.name,
                        status=SourceStatus.FAILED,
                        message="未能打开千里马持久浏览器，请检查 Playwright/Chromium 安装。",
                        latency_ms=int((time.perf_counter() - started) * 1000),
                    )
            else:
                playwright = self._live_playwright
                context = self._live_context
            try:
                page = await self._authorized_search_page(context)
                if page is None:
                    self.mark_profile("expired")
                    return SourceSearchResult(
                        source=self.name,
                        status=SourceStatus.AUTH_REQUIRED,
                        message="千里马已要求重新登录；浏览器配置仍在本机，但本轮没有读取任何会员结果。",
                        latency_ms=int((time.perf_counter() - started) * 1000),
                    )
                search_box = page.locator('input[placeholder="请输入您要搜索的内容"]')
                await search_box.fill(query)
                await search_box.press("Enter")
                await page.wait_for_timeout(1800)
                rows = await page.evaluate(
                    """() => Array.from(document.querySelectorAll('a.con-title[href*="/bid-"]'))
                      .slice(0, 20).map((link) => {
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
                          category: tags[2] || ''
                        };
                      })"""
                )
                items = self.parse_foreground_rows(
                    rows if isinstance(rows, list) else [],
                    limit=min(20, self.settings.max_results_per_source),
                )
                return SourceSearchResult(
                    source=self.name,
                    status=SourceStatus.PARTIAL,
                    items=items,
                    scanned_count=len(rows) if isinstance(rows, list) else 0,
                    message=(
                        f"已在用户本人登录的可见浏览器中执行 1 次首屏查询，读取 {len(items)} 条免费会员列表结果；"
                        "未翻页、未读取付费详情、未导出或后台重放 Cookie。"
                    ),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            except Exception:
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
        if spec.schedule.kind == ScheduleKind.IMMEDIATE and self.profile_available():
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
