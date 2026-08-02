from __future__ import annotations

import re
import time
from datetime import date, timedelta
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

    def _params(
        self,
        spec: TenderQuerySpec,
        *,
        time_value: str | None,
    ) -> list[tuple[str, str]]:
        # CEC's relevance search is materially better than its time/region facet
        # combination. Add region as a search term and enforce both facets again
        # in our evidence-based filter.
        query = f"{spec.region} {spec.topic}" if spec.region else spec.topic
        params = [
            ("wd", query),
            ("index_all", "1"),
            ("region_all", "1"),
            ("project_all", "1"),
        ]
        if time_value:
            params.append(("time", time_value))
        return params

    @staticmethod
    def _time_values(spec: TenderQuerySpec) -> tuple[str | None, ...]:
        """Map an exact local date range to the site's current coarse facets."""

        today = date.today()
        if spec.start_date.year == spec.end_date.year and spec.end_date.year < today.year:
            year = spec.start_date.year
            return (str(year) if year >= 2021 else "before_2020",)
        if spec.start_date < today - timedelta(days=365):
            years: list[str] = []
            if spec.start_date.year <= 2020:
                years.append("before_2020")
            years.extend(
                str(year)
                for year in range(max(2021, spec.start_date.year), min(today.year, 2025) + 1)
            )
            if spec.end_date.year >= today.year:
                years.append("year")
            return tuple(dict.fromkeys(years)) or (None,)
        days = max(0, (spec.end_date - spec.start_date).days)
        if days <= 1:
            return ("today",)
        if days <= 31:
            return ("one_month",)
        if days <= 93:
            return ("three_months",)
        if days <= 184:
            return ("half_year",)
        return ("year",)

    @staticmethod
    def _page_text(html: str) -> str:
        return BeautifulSoup(html, "lxml").get_text(" ", strip=True)

    @classmethod
    def _is_login_page(cls, html: str, final_url: str = "") -> bool:
        if "/login" in final_url.lower():
            return True
        soup = BeautifulSoup(html, "lxml")
        return bool(soup.select_one('form[action*="/login"] input[type="password"]'))

    @classmethod
    def _detail_requires_auth(cls, html: str, final_url: str = "") -> bool:
        if cls._is_login_page(html, final_url):
            return True
        text = cls._page_text(html)
        return bool(
            re.search(r"内容\s*仅\s*对\s*会员\s*开放", text)
            or re.search(r"(?:请|立即)\s*登录.{0,20}(?:查看|解锁|会员)", text)
        )

    @classmethod
    def _explicit_no_results(cls, html: str) -> bool:
        return bool(re.search(r"没有找到与\s*.+?\s*相关的结果", cls._page_text(html)))

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

    @classmethod
    def _parse_detail(
        cls,
        html: str,
        item: RawTender,
        *,
        final_url: str = "",
    ) -> RawTender:
        soup = BeautifulSoup(html, "lxml")
        if cls._detail_requires_auth(html, final_url):
            return item

        candidates = soup.select(
            "article,.tender-content,.details-content,.detail-content,.card-body"
        )
        root = max(candidates, key=lambda node: len(node.get_text(" ", strip=True)), default=None)
        if root is None:
            root = soup
        body = clean_html(str(root))
        if len(body) > max(120, len(item.body) + 30):
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
            cookie = self.settings.cecbid_cookie.strip()
            auth_headers = {"Cookie": cookie, "Referer": f"{self.base_url}/"} if cookie else None
            allowed_hosts = ("www.cecbid.org.cn", "cecbid.org.cn") if cookie else None
            items_by_url: dict[str, RawTender] = {}
            explicit_zero = False
            for time_value in self._time_values(spec):
                page = await fetcher.get(
                    f"{self.base_url}/search",
                    params=self._params(spec, time_value=time_value),
                    headers=auth_headers,
                    authorized_hosts=allowed_hosts,
                )
                if cookie and self._is_login_page(page.text, page.final_url):
                    return SourceSearchResult(
                        source=self.name,
                        status=SourceStatus.AUTH_REQUIRED,
                        message="授权会话已被站点重定向到登录页，请重新授权。",
                        latency_ms=int((time.perf_counter() - started) * 1000),
                    )
                page_items = self.parse_search_page(
                    page.text,
                    self.settings.max_results_per_source,
                )
                explicit_zero = explicit_zero or self._explicit_no_results(page.text)
                for item in page_items:
                    items_by_url.setdefault(item.source_url, item)
                    if len(items_by_url) >= self.settings.max_results_per_source:
                        break
                if len(items_by_url) >= self.settings.max_results_per_source:
                    break
            items = list(items_by_url.values())
            if not items and not explicit_zero:
                return SourceSearchResult(
                    source=self.name,
                    status=SourceStatus.FAILED,
                    message="搜索页既没有结果也没有明确零结果提示，站点结构可能已变化。",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            if cookie and items:
                detailed: list[RawTender] = []
                gated_count = 0
                for item in items:
                    try:
                        detail = await fetcher.get(
                            item.source_url,
                            headers={"Cookie": cookie, "Referer": f"{self.base_url}/search"},
                            retries=1,
                            authorized_hosts=("www.cecbid.org.cn", "cecbid.org.cn"),
                        )
                        if self._detail_requires_auth(detail.text, detail.final_url):
                            gated_count += 1
                            detailed.append(item)
                        else:
                            detailed.append(
                                self._parse_detail(
                                    detail.text,
                                    item,
                                    final_url=detail.final_url,
                                )
                            )
                    except FetchError:
                        detailed.append(item)
                items = detailed
            latency = int((time.perf_counter() - started) * 1000)
            enhanced = any(item.auth_level == "free_member" for item in items)
            if cookie and items and not enhanced and gated_count:
                status = SourceStatus.AUTH_REQUIRED
            else:
                status = SourceStatus.OK if enhanced else SourceStatus.PARTIAL
            message = (
                "已使用授权会员态读取搜索与详情。"
                if enhanced
                else "站点仍返回会员门禁，授权会话可能已过期，请重新授权。"
                if status == SourceStatus.AUTH_REQUIRED
                else "已使用验证通过的会员态执行搜索；本轮没有匹配候选。"
                if cookie and not items
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
