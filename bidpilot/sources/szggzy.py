from __future__ import annotations

import json
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


class SZGGZYSource(SourceAdapter):
    """深圳公共资源交易中心公开交易公告 API。"""

    source_id = "szggzy"
    name = "深圳公共资源交易中心"
    official = True
    base_url = "https://www.szggzy.com"
    homepage = f"{base_url}/static/index.html"
    access_mode = "public"
    query_mode = "regional_keyword_api"
    supports_query_variants = True
    supports_region_filter = True
    supports_date_filter = True
    supports_pagination = True
    supports_detail = True
    coverage_note = (
        "深圳市级政府采购、建设工程和阳光采购公开公告；其他地域查询会跳过，"
        "登录工作台、投标和 CA 能力不属于公开检索。"
    )

    page_url = f"{base_url}/cms/api/v1/trade/content/page"
    detail_url = f"{base_url}/cms/api/v1/trade/content/detail"
    channels = (
        (2850, 1, "政府采购", "政府采购"),
        (2851, 1, "建设工程", ""),
        (4161, 216, "阳光采购", ""),
    )

    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    def _safe_source_url(row: dict) -> str:
        content_id = str(row.get("contentId") or row.get("id") or "")
        # The list API still returns legacy ``linkTo`` values for part of the
        # catalogue (including the retired :8081 government-procurement site).
        # The current public detail shell is stable across all trade channels
        # and loads the same content by contentId, so evidence links must use
        # this canonical route instead of trusting stale deep links.
        return (
            "https://www.szggzy.com/jygg/details.html"
            f"?contentId={content_id}&channelId={row.get('channelId', '')}"
        )

    @classmethod
    def parse_page_response(cls, payload: dict) -> list[RawTender]:
        content = (payload.get("data") or {}).get("content") or []
        items: list[RawTender] = []
        for row in content:
            title = normalize_space(row.get("title") or row.get("noticeTitle") or "")
            content_id = str(row.get("contentId") or row.get("id") or "")
            if not title or not content_id:
                continue
            source_url = cls._safe_source_url(row)
            body = normalize_space(
                "。".join(
                    str(value)
                    for value in (
                        row.get("projectName"),
                        row.get("purchaseMan"),
                        row.get("proxyComName"),
                        row.get("winnerName"),
                    )
                    if value
                )
            )
            event_hint = normalize_space(
                row.get("appNoticeTypeName")
                or row.get("noticeTypeName")
                or row.get("rank1NoticeTypeName")
                or title
            )
            items.append(
                RawTender(
                    source=cls.name,
                    source_url=source_url,
                    title=title,
                    published_at=parse_datetime(
                        row.get("releaseTime") or row.get("publishTime") or ""
                    ),
                    region="深圳",
                    buyer=normalize_space(row.get("purchaseMan") or row.get("tenderer") or "")
                    or None,
                    body=body,
                    event_type=detect_event_type(f"{event_hint} {title}"),
                    project_id=(
                        normalize_space(
                            row.get("projectCode")
                            or row.get("bidSectionNumber")
                            or row.get("tenderProjectNumber")
                            or ""
                        )
                        or extract_project_id(f"{title} {body}")
                    ),
                    evidence=[EvidenceSpan(text=(body or title)[:500], source_url=source_url)],
                    auth_level="public",
                    source_metadata={
                        "content_id": content_id,
                        "channel_id": row.get("channelId"),
                        "area_name": row.get("areaName") or row.get("projectRegion") or "",
                        "notice_type": event_hint,
                        "trade_type": row.get("tradeType") or "",
                        "total_elements": (payload.get("data") or {}).get("totalElements", 0),
                    },
                )
            )
        return items

    @staticmethod
    def apply_detail(item: RawTender, payload: dict) -> RawTender:
        data = payload.get("data") or {}
        raw_html = data.get("txt") or ""
        if not raw_html:
            return item
        body = clean_html(raw_html)
        if len(body) <= len(item.body):
            return item
        attrs = {
            str(row.get("attrName")): row.get("attrValue")
            for row in data.get("attrs") or []
            if row.get("attrName")
        }
        soup = BeautifulSoup(raw_html, "lxml")
        item.body = body
        item.buyer = item.buyer or normalize_space(attrs.get("jygg_cgr") or "") or None
        item.project_id = (
            item.project_id
            or normalize_space(attrs.get("jygg_xmbh") or "")
            or extract_project_id(body)
        )
        item.attachments = [
            Attachment(name=name, url=urljoin(item.source_url, link))
            for name, link in find_attachments(soup, item.source_url)
        ]
        item.evidence = [EvidenceSpan(text=body[:500], source_url=item.source_url)]
        item.source_metadata["detail_loaded"] = True
        return item

    async def search(self, spec: TenderQuerySpec, fetcher: HttpFetcher) -> SourceSearchResult:
        started = time.perf_counter()
        if spec.region and spec.region not in {"深圳", "广东"}:
            return SourceSearchResult(
                source=self.name,
                status=SourceStatus.SKIPPED,
                message=f"区域官方源仅覆盖深圳，当前查询地域为{spec.region}，已跳过。",
                latency_ms=0,
            )

        parsed: list[RawTender] = []
        errors: list[str] = []
        remaining = self.settings.max_results_per_source
        headers = {"Referer": f"{self.base_url}/static/jygg/list.html"}
        for channel_id, site_id, channel_name, parent_business_type in self.channels:
            if remaining <= 0:
                break
            body = {
                "channelId": channel_id,
                "fields": [],
                "title": spec.topic,
                "releaseTimeBegin": f"{spec.start_date.isoformat()} 00:00:00",
                "releaseTimeEnd": f"{spec.end_date.isoformat()} 23:59:59",
                "page": 0,
                "size": remaining,
                "siteId": site_id,
            }
            if parent_business_type:
                body["parentBusinessType"] = parent_business_type
            try:
                response = await fetcher.post_json(
                    self.page_url,
                    json_body=body,
                    headers=headers,
                    retries=1,
                )
                payload = json.loads(response.text)
                if str(payload.get("code")) != "200":
                    raise ValueError(str(payload.get("message") or "公开接口返回失败状态"))
                rows = self.parse_page_response(payload)
                for item in rows:
                    item.source_metadata["channel_name"] = channel_name
                parsed.extend(rows)
                remaining = self.settings.max_results_per_source - len(parsed)
            except (FetchError, json.JSONDecodeError, TypeError, ValueError) as exc:
                errors.append(f"{channel_name}: {exc}")

        prefilter: Counter[str] = Counter()
        items: list[RawTender] = []
        seen: set[str] = set()
        for item in parsed:
            content_id = str(item.source_metadata.get("content_id") or "")
            if not content_id or content_id in seen:
                continue
            seen.add(content_id)
            if not (spec.start_date <= item.published_at.date() <= spec.end_date):
                prefilter["outside_time"] += 1
                continue
            try:
                detail = await fetcher.get(
                    self.detail_url,
                    params={"contentId": content_id},
                    headers=headers,
                    retries=1,
                )
                item = self.apply_detail(item, json.loads(detail.text))
            except (FetchError, json.JSONDecodeError, TypeError, ValueError):
                item.source_metadata["detail_loaded"] = False
            items.append(item)

        if not parsed and errors:
            status = SourceStatus.FAILED
        elif errors or any(not item.source_metadata.get("detail_loaded") for item in items):
            status = SourceStatus.PARTIAL
        else:
            status = SourceStatus.OK
        message = (
            f"深圳官方平台检索完成，扫描 {len(parsed)} 条，读取 {len(items)} 条公开详情。"
            "仅使用公告公开接口，不登录投标工作台或代替用户完成 CA。"
        )
        if errors:
            message += " 部分频道失败：" + "；".join(errors[:2])
        return SourceSearchResult(
            source=self.name,
            status=status,
            items=items,
            scanned_count=len(parsed),
            prefilter_reasons=dict(prefilter),
            message=message,
            latency_ms=round((time.perf_counter() - started) * 1000),
        )
