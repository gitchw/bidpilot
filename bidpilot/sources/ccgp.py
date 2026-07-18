from __future__ import annotations

import time
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


class CCGPSource(SourceAdapter):
    name = "中国政府采购网"
    requires_auth = False
    base_url = "https://www.ccgp.gov.cn"
    category_paths: tuple[tuple[str, EventType], ...] = (
        ("/cggg/dfgg/gkzb/", EventType.TENDER),
        ("/cggg/dfgg/jzxcs/", EventType.TENDER),
        ("/cggg/dfgg/xjgg/", EventType.TENDER),
        ("/cggg/zygg/gkzb/", EventType.TENDER),
        ("/cggg/dfgg/zbgg/", EventType.AWARD),
        ("/cggg/zygg/zbgg/", EventType.AWARD),
        ("/cggg/dfgg/gzgg/", EventType.CHANGE),
    )

    def __init__(self, settings: Settings):
        self.settings = settings

    @classmethod
    def parse_list_page(cls, html: str, page_url: str, event_type: EventType) -> list[RawTender]:
        soup = BeautifulSoup(html, "lxml")
        container = soup.select_one("ul.c_list_bid")
        if container is None:
            return []
        items: list[RawTender] = []
        for row in container.select("li"):
            anchor = row.select_one("a[href]")
            ems = [normalize_space(em.get_text(" ", strip=True)) for em in row.select("em")]
            if anchor is None or not ems:
                continue
            title = normalize_space(anchor.get("title", "") or anchor.get_text(" ", strip=True))
            published = parse_datetime(ems[0])
            region = ems[1] if len(ems) > 1 else None
            buyer = ems[2] if len(ems) > 2 else None
            url = urljoin(page_url, anchor.get("href", ""))
            items.append(
                RawTender(
                    source=cls.name,
                    source_url=url,
                    title=title,
                    published_at=published,
                    region=region,
                    buyer=buyer,
                    event_type=event_type
                    if event_type != EventType.OTHER
                    else detect_event_type(title),
                    auth_level="public",
                )
            )
        return items

    @staticmethod
    def parse_detail_page(html: str, item: RawTender) -> RawTender:
        soup = BeautifulSoup(html, "lxml")
        root = soup.select_one("div.vF_detail_content") or soup.select_one(
            "div.vF_detail_content_container"
        )
        if root is None:
            return item
        body = clean_html(str(root))
        item.body = body
        item.project_id = extract_project_id(body)
        item.attachments = [
            Attachment(name=name, url=url) for name, url in find_attachments(root, item.source_url)
        ]
        item.evidence = [EvidenceSpan(text=body[:500], source_url=item.source_url)] if body else []
        title_node = soup.select_one("div.vF_detail_header h2") or soup.select_one("h2")
        if title_node:
            item.title = normalize_space(title_node.get_text(" ", strip=True))
        return item

    @staticmethod
    def _matches_spec(item: RawTender, spec: TenderQuerySpec) -> bool:
        if item.published_at.date() < spec.start_date or item.published_at.date() > spec.end_date:
            return False
        if (
            spec.region
            and spec.region_level != "city"
            and item.region
            and spec.region not in item.region
        ):
            return False
        haystack = f"{item.title} {item.buyer or ''}".lower()
        keywords = [keyword.lower() for keyword in spec.keywords if len(keyword) >= 2]
        return any(keyword in haystack for keyword in keywords)

    async def search(self, spec: TenderQuerySpec, fetcher: HttpFetcher) -> SourceSearchResult:
        started = time.perf_counter()
        candidates: list[RawTender] = []
        errors: list[str] = []
        stop_all = False
        for path, event_type in self.category_paths:
            if stop_all or len(candidates) >= self.settings.max_results_per_source:
                break
            for page_number in range(self.settings.ccgp_max_pages):
                filename = "index.htm" if page_number == 0 else f"index_{page_number}.htm"
                page_url = urljoin(self.base_url, f"{path}{filename}")
                try:
                    page = await fetcher.get(page_url, encoding="utf-8", retries=1)
                except FetchError as exc:
                    errors.append(f"{path}: {exc}")
                    break
                page_items = self.parse_list_page(page.text, page_url, event_type)
                if not page_items:
                    break
                for item in page_items:
                    if self._matches_spec(item, spec):
                        candidates.append(item)
                        if len(candidates) >= self.settings.max_results_per_source:
                            break
                oldest = min(
                    (item.published_at.date() for item in page_items), default=spec.end_date
                )
                if oldest < spec.start_date:
                    break

        detailed: list[RawTender] = []
        for item in candidates[: self.settings.max_results_per_source]:
            try:
                page = await fetcher.get(item.source_url, encoding="utf-8", retries=1)
                detailed.append(self.parse_detail_page(page.text, item))
            except FetchError as exc:
                errors.append(f"详情 {item.source_url}: {exc}")
                detailed.append(item)

        latency = int((time.perf_counter() - started) * 1000)
        if detailed:
            status = SourceStatus.PARTIAL if errors else SourceStatus.OK
        elif errors:
            status = SourceStatus.FAILED
        else:
            status = SourceStatus.OK
        return SourceSearchResult(
            source=self.name,
            status=status,
            items=detailed,
            message="；".join(errors[:3]) if errors else "官方公告列表与详情抓取完成。",
            latency_ms=latency,
        )
