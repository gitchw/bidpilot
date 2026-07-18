from __future__ import annotations

import re
import time
from collections import Counter

from bs4 import BeautifulSoup

from bidpilot.clean import detect_event_type, extract_project_id, normalize_space, parse_datetime
from bidpilot.config import Settings
from bidpilot.fetch import FetchError, HttpFetcher
from bidpilot.models import (
    EventType,
    EvidenceSpan,
    RawTender,
    SourceSearchResult,
    SourceStatus,
    TenderQuerySpec,
)
from bidpilot.sources.base import SourceAdapter


class CEBPubServiceSource(SourceAdapter):
    """中国招标投标公共服务平台公开列表搜索，不绕过详情 WAF 或验证码。"""

    source_id = "cebpubservice"
    name = "中国招标投标公共服务平台"
    official = True
    base_url = "https://bulletin.cebpubservice.com"
    homepage = base_url
    access_mode = "public_list_with_user_authorized_detail"
    query_mode = "keyword_search"
    supports_query_variants = True
    supports_region_filter = True
    supports_date_filter = True
    supports_pagination = True
    supports_detail = False
    authorization_supported = True
    authorization_url = "https://ctbpsp.com/#/"
    coverage_note = "公开关键词列表可检索；详情站的 WAF、验证码和会员权限不自动绕过，当前证据粒度为公开列表摘要。"

    endpoints = (
        ("bulletin.html", "88", EventType.TENDER),
        ("change.html", "89", EventType.CHANGE),
        ("result.html", "90", EventType.AWARD),
        ("candidate.html", "91", EventType.AWARD),
        ("qualify.html", "92", EventType.TENDER),
    )

    def __init__(self, settings: Settings):
        self.settings = settings

    @classmethod
    def parse_list_page(
        cls,
        html: str,
        event_type: EventType,
        limit: int = 20,
    ) -> list[RawTender]:
        soup = BeautifulSoup(html, "lxml")
        items: list[RawTender] = []
        for row in soup.select("table tr"):
            anchor = row.select_one('a[href*="urlOpen"]')
            if anchor is None:
                continue
            href = anchor.get("href", "")
            match = re.search(r"urlOpen\(['\"]([0-9A-Za-z-]+)['\"]\)", href)
            title = normalize_space(anchor.get("title", "") or anchor.get_text(" ", strip=True))
            cells = row.select("td")
            if match is None or not title or len(cells) < 5:
                continue
            bulletin_id = match.group(1)
            industry = normalize_space(cells[1].get_text(" ", strip=True))
            region = normalize_space(cells[2].get_text(" ", strip=True)).strip("【】[]")
            publisher = normalize_space(cells[3].get_text(" ", strip=True))
            published_text = normalize_space(cells[4].get_text(" ", strip=True))
            url = (
                "https://ctbpsp.com/#/bulletinDetail?"
                f"uuid={bulletin_id}&inpvalue=&dataSource=0&tenderAgency="
            )
            body = normalize_space(
                f"公告标题：{title}。所属行业：{industry or '未标注'}。"
                f"所属地区：{region or '未标注'}。发布渠道：{publisher or '未标注'}。"
            )
            detected = detect_event_type(title)
            if detected == EventType.OTHER:
                detected = event_type
            items.append(
                RawTender(
                    source=cls.name,
                    source_url=url,
                    title=title,
                    published_at=parse_datetime(published_text),
                    region=region or None,
                    body=body,
                    event_type=detected,
                    project_id=extract_project_id(title),
                    evidence=[EvidenceSpan(text=body, source_url=url)],
                    auth_level="public_snippet",
                    source_metadata={
                        "bulletin_id": bulletin_id,
                        "industry": industry,
                        "publisher": publisher,
                        "category_id": event_type.value,
                    },
                )
            )
            if len(items) >= limit:
                break
        return items

    async def search(self, spec: TenderQuerySpec, fetcher: HttpFetcher) -> SourceSearchResult:
        started = time.perf_counter()
        parsed: list[RawTender] = []
        errors: list[str] = []
        selected = [
            endpoint
            for endpoint in self.endpoints
            if not spec.event_types or endpoint[2] in spec.event_types
        ]
        for path, category_id, event_type in selected:
            if len(parsed) >= self.settings.max_results_per_source:
                break
            try:
                response = await fetcher.get(
                    f"{self.base_url}/xxfbcmses/search/{path}",
                    params={
                        "searchDate": spec.start_date.isoformat(),
                        "dates": max(1, min(300, (spec.end_date - spec.start_date).days)),
                        "word": spec.topic,
                        "categoryId": category_id,
                        "industryName": "",
                        "area": spec.region or "",
                        "status": "",
                        "publishMedia": "",
                        "sourceInfo": "",
                        "showStatus": "1",
                        "startcheckDate": spec.start_date.isoformat(),
                        "endcheckDate": f"{spec.end_date.isoformat()} 23:59:59",
                        "page": 1,
                    },
                    headers={"Referer": f"{self.base_url}/"},
                    retries=1,
                )
                parsed.extend(
                    self.parse_list_page(
                        response.text,
                        event_type,
                        self.settings.max_results_per_source - len(parsed),
                    )
                )
            except FetchError as exc:
                errors.append(f"{event_type.value}: {exc}")

        if not parsed and errors:
            return SourceSearchResult(
                source=self.name,
                status=SourceStatus.FAILED,
                message="；".join(errors[:3]),
                latency_ms=round((time.perf_counter() - started) * 1000),
            )

        prefilter: Counter[str] = Counter()
        items = []
        for item in parsed:
            if not (spec.start_date <= item.published_at.date() <= spec.end_date):
                prefilter["outside_time"] += 1
            elif spec.region and item.region and spec.region not in item.region:
                prefilter["region_mismatch"] += 1
            else:
                items.append(item)
        message = (
            "中国招标投标公共服务平台公开关键词列表检索完成；当前不自动穿透详情 WAF、"
            "验证码或会员限制，原文链接供用户在浏览器核验。"
        )
        if errors:
            message += " 部分类别失败：" + "；".join(errors[:2])
        return SourceSearchResult(
            source=self.name,
            status=SourceStatus.PARTIAL,
            items=items,
            scanned_count=len(parsed),
            prefilter_reasons=dict(prefilter),
            message=message,
            latency_ms=round((time.perf_counter() - started) * 1000),
        )
