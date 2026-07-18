from __future__ import annotations

import json
import re
import time
from collections import Counter
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


class ZYCGSource(SourceAdapter):
    """中央政府采购网公开公告搜索；接口异常时明确失败而不伪造覆盖。"""

    source_id = "zycg"
    name = "中央政府采购网"
    official = True
    base_url = "https://www.zycg.gov.cn"
    homepage = base_url
    access_mode = "public"
    query_mode = "keyword_api"
    supports_query_variants = True
    supports_date_filter = True
    supports_pagination = True
    supports_detail = False
    coverage_note = "覆盖中央国家机关政府采购中心公开公告；官方搜索接口异常时会明确标记失败。"

    site_id = "6f5243ee-d4d9-4b69-abbd-1e40576ccd7d"
    channel_id = "d0e7c5f4-b93e-4478-b7fe-61110bb47fd5"
    search_url = f"{base_url}/freecms/rest/v1/notice/selectInfoMore.do"
    fallback_url = f"{base_url}/freecms/rest/v1/notice/searchAll.do"

    def __init__(self, settings: Settings):
        self.settings = settings

    @classmethod
    def parse_response(cls, payload: dict) -> list[RawTender]:
        rows = payload.get("data") or []
        items: list[RawTender] = []
        for row in rows:
            title = normalize_space(row.get("title") or row.get("shorttitle") or "")
            page_url = row.get("pageurl") or row.get("pageUrl") or row.get("htmlpath") or ""
            if not title or not page_url:
                continue
            identifier = normalize_space(str(row.get("id") or row.get("noticeId") or ""))
            url = urljoin(cls.base_url, page_url)
            if identifier and "?" not in url:
                url += f"?id={identifier}"
            raw_html = row.get("content") or row.get("description") or ""
            body = clean_html(raw_html) if "<" in raw_html else normalize_space(raw_html)
            region = normalize_space(row.get("regionName") or row.get("areaName") or "")
            region = re.sub(r"(?:省|市|壮族自治区|回族自治区|维吾尔自治区|自治区)$", "", region)
            published = parse_datetime(
                row.get("noticeTime") or row.get("addtimeStr") or row.get("addtimees") or ""
            )
            buyer_match = re.search(
                r"(?:采购人|采购单位|采购机构)[：:]\s*(.{2,100}?)"
                r"(?=\s*(?:采购|项目|地址|联系人|预算|$))",
                body,
            )
            attachments = []
            if raw_html and "<" in raw_html:
                soup = BeautifulSoup(raw_html, "lxml")
                attachments = [
                    Attachment(name=name, url=urljoin(cls.base_url, link))
                    for name, link in find_attachments(soup, cls.base_url)
                ]
            items.append(
                RawTender(
                    source=cls.name,
                    source_url=url,
                    title=title,
                    published_at=published,
                    region=region or None,
                    buyer=normalize_space(buyer_match.group(1)) if buyer_match else None,
                    body=body,
                    attachments=attachments,
                    event_type=detect_event_type(title),
                    project_id=extract_project_id(f"{title} {body}"),
                    evidence=[EvidenceSpan(text=(body or title)[:500], source_url=url)],
                    auth_level="public",
                    source_metadata={
                        "notice_id": identifier,
                        "implement_way": row.get("implementWay", ""),
                        "notice_type": row.get("noticeType", ""),
                    },
                )
            )
        return items

    async def search(self, spec: TenderQuerySpec, fetcher: HttpFetcher) -> SourceSearchResult:
        started = time.perf_counter()
        errors: list[str] = []
        payload = None
        headers = {
            "Referer": f"{self.base_url}/freecms/site/zygjjgzfcgzx/cggg/index.html",
            "X-Requested-With": "XMLHttpRequest",
        }
        params = {
            "siteId": self.site_id,
            "channel": self.channel_id,
            "currPage": 1,
            "pageSize": self.settings.max_results_per_source,
            "title": spec.topic,
            "implementWay": "",
            "noticeType": "",
        }
        try:
            response = await fetcher.get(
                self.search_url,
                params=params,
                headers=headers,
                retries=1,
            )
            candidate = json.loads(response.text)
            if str(candidate.get("code")) in {"200", "0"} and isinstance(
                candidate.get("data"), list
            ):
                payload = candidate
            else:
                errors.append(str(candidate.get("msg") or "官方公告接口返回失败状态"))
        except (FetchError, json.JSONDecodeError, TypeError, ValueError) as exc:
            errors.append(str(exc))

        if payload is None:
            try:
                response = await fetcher.get(
                    self.fallback_url,
                    params={
                        "title": spec.topic,
                        "currPage": 1,
                        "pageSize": self.settings.max_results_per_source,
                    },
                    headers=headers,
                    retries=0,
                )
                candidate = json.loads(response.text)
                if str(candidate.get("code")) in {"200", "0"} and isinstance(
                    candidate.get("data"), list
                ):
                    payload = candidate
                else:
                    errors.append(str(candidate.get("msg") or "备用公开搜索返回失败状态"))
            except (FetchError, json.JSONDecodeError, TypeError, ValueError) as exc:
                errors.append(str(exc))

        latency = round((time.perf_counter() - started) * 1000)
        if payload is None:
            return SourceSearchResult(
                source=self.name,
                status=SourceStatus.FAILED,
                message="中央政府采购网两个公开搜索入口均未返回可用数据：" + "；".join(errors[:2]),
                latency_ms=latency,
            )

        parsed = self.parse_response(payload)
        prefilter: Counter[str] = Counter()
        items = []
        for item in parsed:
            if not (spec.start_date <= item.published_at.date() <= spec.end_date):
                prefilter["outside_time"] += 1
            elif spec.region and item.region and spec.region not in item.region:
                prefilter["region_mismatch"] += 1
            else:
                items.append(item)
        missing_detail = sum(not bool(item.body) for item in items)
        return SourceSearchResult(
            source=self.name,
            status=SourceStatus.PARTIAL if missing_detail else SourceStatus.OK,
            items=items,
            scanned_count=len(parsed),
            prefilter_reasons=dict(prefilter),
            message=(
                "中央政府采购网公开公告搜索完成。"
                + (
                    f"其中 {missing_detail} 条电子卖场公告只提供公开标题和原文入口；"
                    "未绕过详情网关访问限制。"
                    if missing_detail
                    else ""
                )
            ),
            latency_ms=latency,
        )
