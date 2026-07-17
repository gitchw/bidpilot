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
from bidpilot.models import (
    Attachment,
    EvidenceSpan,
    RawTender,
    SourceSearchResult,
    SourceStatus,
    TenderQuerySpec,
)
from bidpilot.sources.base import SourceAdapter


class QianlimaSource(SourceAdapter):
    name = "千里马招标网"
    requires_auth = True
    base_url = "https://wap.qianlima.com"

    def __init__(self, settings: Settings):
        self.settings = settings

    @classmethod
    def parse_search_page(cls, html: str, limit: int = 20) -> list[RawTender]:
        soup = BeautifulSoup(html, "lxml")
        containers = soup.select(".search-list li,.result-list li,ul.list li,.search_result li")
        if not containers:
            containers = [
                anchor.parent
                for anchor in soup.select('a[href*="qianlima.com"],a[href*="/zb/"]')
                if anchor.parent is not None
            ]
        items: list[RawTender] = []
        seen: set[str] = set()
        for row in containers:
            anchor = row.select_one("a[href]")
            if anchor is None:
                continue
            title = normalize_space(anchor.get("title", "") or anchor.get_text(" ", strip=True))
            href = urljoin(cls.base_url, anchor.get("href", ""))
            if not title or href in seen or any(word in title for word in ("注册", "登录", "首页")):
                continue
            row_text = normalize_space(row.get_text(" ", strip=True))
            date_match = re.search(r"20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}", row_text)
            published = parse_datetime(date_match.group(0) if date_match else "")
            items.append(
                RawTender(
                    source=cls.name,
                    source_url=href,
                    title=title,
                    published_at=published,
                    body=row_text,
                    event_type=detect_event_type(title),
                    project_id=extract_project_id(row_text),
                    evidence=[EvidenceSpan(text=row_text[:360], source_url=href)],
                    auth_level="free_member",
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
        cookie = self.settings.load_qianlima_cookie()
        if not cookie:
            return SourceSearchResult(
                source=self.name,
                status=SourceStatus.AUTH_REQUIRED,
                message="需要用户授权的免费会员登录态；运行 bidpilot auth qianlima 完成登录。",
                latency_ms=0,
            )
        headers = {"Cookie": cookie, "Referer": f"{self.base_url}/"}
        try:
            page = await fetcher.get(
                f"{self.base_url}/search.jsp",
                params={"q": spec.topic},
                headers=headers,
                encoding="gb18030",
                retries=1,
            )
            lowered = page.final_url.lower()
            page_text = normalize_space(BeautifulSoup(page.text, "lxml").get_text(" ", strip=True))
            if (
                "register" in lowered
                or "login" in lowered
                or "免费 注册 查询 最新 招投标信息" in page_text
            ):
                return SourceSearchResult(
                    source=self.name,
                    status=SourceStatus.AUTH_REQUIRED,
                    message="登录态已过期，请重新授权免费会员会话。",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            items = self.parse_search_page(page.text, self.settings.max_results_per_source)
            detailed: list[RawTender] = []
            for item in items:
                try:
                    detail = await fetcher.get(
                        item.source_url, headers=headers, encoding="gb18030", retries=1
                    )
                    detailed.append(self.parse_detail_page(detail.text, item))
                except FetchError:
                    detailed.append(item)
            return SourceSearchResult(
                source=self.name,
                status=SourceStatus.OK,
                items=detailed,
                message="免费会员授权源抓取完成。",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        except FetchError as exc:
            return SourceSearchResult(
                source=self.name,
                status=SourceStatus.FAILED,
                message=str(exc),
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
