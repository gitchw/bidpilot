from __future__ import annotations

import re
import time
from math import ceil
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
    query_mode = "public_category_feed"
    supports_query_variants = False
    supports_pagination = True
    supports_detail = False
    authorization_supported = True
    authorization_url = "https://search.vip.qianlima.com/"
    coverage_note = (
        "自动任务只读取无需登录的公开分类列表，并在本地执行主题、地域和日期硬校验；"
        "不保存或重放千里马会员 Cookie，不自动读取付费详情。需要完整站内能力时请主动打开原站。"
    )

    _EVENT_FEEDS = {
        EventType.TENDER: "/zbgg/",
        EventType.INTENTION: "/zbyg/",
        EventType.AWARD: "/zbjg/",
        EventType.CHANGE: "/zbbg/",
    }

    def __init__(self, settings: Settings):
        self.settings = settings

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
