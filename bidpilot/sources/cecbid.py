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


class CECBidSource(SourceAdapter):
    source_id = "cecbid"
    name = "中国招标投标网"
    requires_auth = False
    base_url = "https://www.cecbid.org.cn"
    homepage = base_url
    access_mode = "public_with_member_detail"
    query_mode = "keyword_search"
    supports_query_variants = True
    supports_detail = True
    authorization_supported = True
    authorization_url = f"{base_url}/login"
    coverage_note = "公开搜索摘要可用；用户授权会员会话后可读取账号本来可见的详情。"

    def __init__(self, settings: Settings):
        self.settings = settings

    def _params(self, spec: TenderQuerySpec) -> list[tuple[str, str]]:
        # CEC's relevance search is materially better than its time/region facet
        # combination. Add region as a search term and enforce both facets again
        # in our evidence-based filter.
        query = f"{spec.region} {spec.topic}" if spec.region else spec.topic
        return [("wd", query), ("index[]", "tenders")]

    @classmethod
    def parse_search_page(cls, html: str, limit: int = 20) -> list[RawTender]:
        soup = BeautifulSoup(html, "lxml")
        cards = soup.select("div.card-body.text-black-50.border-bottom.lh-lg")
        items: list[RawTender] = []
        for card in cards:
            anchor = card.select_one('a[href*="/tenders/details/"]')
            if anchor is None:
                continue
            title = normalize_space(anchor.get_text(" ", strip=True))
            if not title:
                continue
            href = urljoin(cls.base_url, anchor.get("href", ""))
            snippet_node = card.select_one("div.my-2.text-secondary")
            snippet = (
                normalize_space(snippet_node.get_text(" ", strip=True)) if snippet_node else ""
            )
            footer = card.select_one("div.mt-2.text-end")
            footer_text = (
                normalize_space(footer.get_text(" ", strip=True)) if footer else card.get_text(" ")
            )
            date_match = re.search(r"20\d{2}-\d{2}-\d{2}", footer_text)
            published = parse_datetime(date_match.group(0) if date_match else "")
            region = None
            for badge in card.select("span[title]"):
                candidate = normalize_space(badge.get("title", ""))
                primary = candidate.split(" - ", 1)[0].strip()
                if primary.endswith(("省", "市", "自治区")) or primary in {
                    "北京",
                    "上海",
                    "天津",
                    "重庆",
                }:
                    region = primary.removesuffix("省").removesuffix("市")
                    break
            badges = [normalize_space(s.get_text(" ", strip=True)) for s in card.select("span")]
            event_hint = next((b for b in badges if "公告" in b or "公示" in b), title)
            project_id = extract_project_id(f"{title} {snippet}")
            items.append(
                RawTender(
                    source=cls.name,
                    source_url=href,
                    title=title,
                    published_at=published,
                    region=region,
                    body=snippet,
                    event_type=detect_event_type(event_hint),
                    project_id=project_id,
                    evidence=[EvidenceSpan(text=snippet[:360], source_url=href)] if snippet else [],
                    auth_level="public_snippet",
                )
            )
            if len(items) >= limit:
                break
        return items

    @staticmethod
    def _parse_detail(html: str, item: RawTender) -> RawTender:
        soup = BeautifulSoup(html, "lxml")
        page_text = normalize_space(soup.get_text(" ", strip=True))
        if "内容仅对会员开放" in page_text:
            return item

        candidates = soup.select(
            "article,.tender-content,.details-content,.detail-content,.card-body"
        )
        root = max(candidates, key=lambda node: len(node.get_text(" ", strip=True)), default=None)
        if root is None:
            root = soup
        body = clean_html(str(root))
        if len(body) > len(item.body) + 80:
            item.body = body
            item.auth_level = "free_member"
            item.project_id = item.project_id or extract_project_id(body)
            item.attachments = [
                Attachment(name=name, url=url)
                for name, url in find_attachments(root, item.source_url)
            ]
            item.evidence = [EvidenceSpan(text=body[:500], source_url=item.source_url)]
        meta = soup.select_one('meta[name="description"]')
        description = meta.get("content", "") if meta else ""
        buyer = re.search(r"采购单位[：:]\s*([^，,。]+)", description)
        if buyer:
            item.buyer = normalize_space(buyer.group(1))
        return item

    async def search(self, spec: TenderQuerySpec, fetcher: HttpFetcher) -> SourceSearchResult:
        started = time.perf_counter()
        try:
            page = await fetcher.get(f"{self.base_url}/search", params=self._params(spec))
            items = self.parse_search_page(page.text, self.settings.max_results_per_source)
            cookie = self.settings.cecbid_cookie.strip()
            if cookie and items:
                headers = {"Cookie": cookie, "Referer": page.final_url}
                detailed: list[RawTender] = []
                for item in items:
                    try:
                        detail = await fetcher.get(
                            item.source_url,
                            headers=headers,
                            retries=1,
                            authorized_hosts=("www.cecbid.org.cn", "cecbid.org.cn"),
                        )
                        detailed.append(self._parse_detail(detail.text, item))
                    except FetchError:
                        detailed.append(item)
                items = detailed
            latency = int((time.perf_counter() - started) * 1000)
            enhanced = any(item.auth_level == "free_member" for item in items)
            status = SourceStatus.OK if enhanced else SourceStatus.PARTIAL
            message = (
                "已使用授权会员态读取搜索与详情。"
                if enhanced
                else "已携带授权会话，但本轮没有证明会员正文已解锁；请在来源中心测试或重新授权。"
                if cookie
                else "已获取公开搜索摘要；可在来源中心打开可见浏览器授权会员会话。"
            )
            return SourceSearchResult(
                source=self.name,
                status=status,
                items=items,
                scanned_count=len(items),
                message=message,
                latency_ms=latency,
            )
        except (TimeoutError, FetchError) as exc:
            return SourceSearchResult(
                source=self.name,
                status=SourceStatus.FAILED,
                message=str(exc),
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
