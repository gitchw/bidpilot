from __future__ import annotations

import re
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
from bidpilot.intent import REGIONS
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

REGION_BY_CODE = {code: region for region, code in set(REGIONS.values())}


class GGZYSource(SourceAdapter):
    name = "全国公共资源交易平台"
    requires_auth = False
    base_url = "https://www.ggzy.gov.cn"

    def __init__(self, settings: Settings):
        self.settings = settings

    @classmethod
    def parse_home_feed(cls, html: str) -> list[RawTender]:
        soup = BeautifulSoup(html, "lxml")
        items: list[RawTender] = []
        for section in soup.select("div.main_list_on"):
            heading = normalize_space(
                section.select_one("h4").get_text(" ", strip=True)
                if section.select_one("h4")
                else ""
            )
            for row in section.select("li"):
                anchor = row.select_one('a[href*="/information/deal/html/a/"]')
                date_node = row.select_one("span")
                if anchor is None or date_node is None:
                    continue
                title = normalize_space(anchor.get_text(" ", strip=True))
                href = urljoin(cls.base_url, anchor.get("href", ""))
                code_match = re.search(r"/html/a/(\d{6})/", href)
                region = REGION_BY_CODE.get(code_match.group(1)) if code_match else None
                event_type = (
                    EventType.AWARD
                    if "成交" in heading or "结果" in heading
                    else detect_event_type(title)
                )
                items.append(
                    RawTender(
                        source=cls.name,
                        source_url=href,
                        title=title,
                        published_at=parse_datetime(date_node.get_text(" ", strip=True)),
                        region=region,
                        event_type=event_type,
                        project_id=extract_project_id(title),
                        auth_level="public",
                        source_metadata={"feed": heading, "scope": "homepage_recent"},
                    )
                )
        return items

    @staticmethod
    def parse_detail_page(html: str, item: RawTender) -> RawTender:
        soup = BeautifulSoup(html, "lxml")
        root = soup.select_one("div.detail_content") or soup.select_one("div.detail")
        if root is None:
            return item
        body = clean_html(str(root))
        if body:
            item.body = body
            item.project_id = item.project_id or extract_project_id(body)
            item.evidence = [EvidenceSpan(text=body[:500], source_url=item.source_url)]
        title = soup.select_one("h4.h4_o")
        if title:
            item.title = normalize_space(title.get_text(" ", strip=True))
        buyer = re.search(r"采购人信息\s*(?:名称)?[：:]?\s*([^，,。；;]{2,80})", body)
        if buyer:
            item.buyer = normalize_space(buyer.group(1)).removeprefix("名称：")
        item.attachments = [
            Attachment(name=name, url=url) for name, url in find_attachments(root, item.source_url)
        ]
        return item

    @staticmethod
    def _matches(item: RawTender, spec: TenderQuerySpec) -> bool:
        if not (spec.start_date <= item.published_at.date() <= spec.end_date):
            return False
        if spec.region and item.region and spec.region not in item.region:
            return False
        haystack = item.title.lower()
        return any(keyword.lower() in haystack for keyword in spec.keywords if len(keyword) >= 2)

    async def search(self, spec: TenderQuerySpec, fetcher: HttpFetcher) -> SourceSearchResult:
        started = time.perf_counter()
        try:
            page = await fetcher.get(self.base_url, encoding="utf-8", retries=1)
            feed = self.parse_home_feed(page.text)
            candidates = [item for item in feed if self._matches(item, spec)][
                : self.settings.max_results_per_source
            ]
            detailed: list[RawTender] = []
            errors: list[str] = []
            for item in candidates:
                detail_url = item.source_url.replace(
                    "/information/deal/html/a/", "/information/deal/html/b/"
                )
                try:
                    detail = await fetcher.get(detail_url, encoding="utf-8", retries=1)
                    detailed.append(self.parse_detail_page(detail.text, item))
                except FetchError as exc:
                    errors.append(str(exc))
                    detailed.append(item)
            message = "全国平台首页实时流已读取；该公开入口仅覆盖最新公告，不等同于全量历史检索。"
            if errors:
                message += " 详情失败：" + "；".join(errors[:2])
            return SourceSearchResult(
                source=self.name,
                status=SourceStatus.PARTIAL,
                items=detailed,
                message=message,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        except FetchError as exc:
            return SourceSearchResult(
                source=self.name,
                status=SourceStatus.FAILED,
                message=str(exc),
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
