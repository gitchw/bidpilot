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


class PLAPSource(SourceAdapter):
    """军队采购网公开采购大厅 API; no login or CA bypass is attempted."""

    source_id = "plap"
    name = "军队采购网"
    official = True
    base_url = "https://www.plap.mil.cn"
    homepage = base_url
    access_mode = "public_with_authorized_workspace"
    query_mode = "keyword_api"
    supports_query_variants = True
    supports_region_filter = True
    supports_date_filter = False
    supports_pagination = True
    supports_detail = True
    authorization_supported = True
    authorization_url = (
        f"{base_url}/gateway/gp-auth-center/login?tenantId=Cxiangmu-001&redirectUrl="
    )
    coverage_note = (
        "公开公告正文无需登录；工作台、采购文件和角色能力只在用户主动登录或完成 CA 后按原权限使用。"
    )

    site_id = "404bb030-5be9-4070-85bd-c94b1473e8de"
    channel_id = "c5bff13f-21ca-4dac-b158-cb40accd3035"
    search_url = f"{base_url}/freecms-glht/rest/v1/notice/selectInfoMoreChannel.do"

    def __init__(self, settings: Settings):
        self.settings = settings

    @classmethod
    def parse_response(cls, payload: dict) -> list[RawTender]:
        items: list[RawTender] = []
        for row in payload.get("data") or []:
            title = normalize_space(row.get("title") or row.get("shorttitle") or "")
            page_url = row.get("pageurl") or row.get("htmlpath") or ""
            if not title or not page_url:
                continue
            url = urljoin(cls.base_url, page_url)
            query = []
            if row.get("noticeType"):
                query.append(f"noticeType={row['noticeType']}")
            if row.get("channel"):
                query.append(f"channel={row['channel']}")
            if query and "?" not in url:
                url += "?" + "&".join(query)
            raw_html = row.get("content") or row.get("description") or ""
            body = clean_html(raw_html) if "<" in raw_html else normalize_space(raw_html)
            region = normalize_space(row.get("regionName") or "")
            region = re.sub(r"(?:省|市|壮族自治区|回族自治区|维吾尔自治区|自治区)$", "", region)
            buyer_match = re.search(
                r"(?:采购单位|采购人|采购机构)(?:信息)?\s*(?:名称)?[：:]\s*(.{2,100}?)"
                r"(?=\s*(?:采购|项目|地址|联系人|预算|$))",
                body,
            )
            attachments = []
            if raw_html and "<" in raw_html:
                soup = BeautifulSoup(raw_html, "lxml")
                attachments = [
                    Attachment(name=name, url=link) for name, link in find_attachments(soup, url)
                ]
            items.append(
                RawTender(
                    source=cls.name,
                    source_url=url,
                    title=title,
                    published_at=parse_datetime(
                        row.get("noticeTime") or row.get("addtimeStr") or ""
                    ),
                    region=region or None,
                    buyer=normalize_space(buyer_match.group(1)) if buyer_match else None,
                    body=body,
                    attachments=attachments,
                    event_type=detect_event_type(title),
                    project_id=(
                        normalize_space(row.get("openTenderCode") or "")
                        or extract_project_id(f"{title} {body}")
                    ),
                    evidence=[EvidenceSpan(text=(body or title)[:500], source_url=url)],
                    auth_level="public",
                    source_metadata={
                        "notice_id": row.get("noticeId") or row.get("id") or "",
                        "purchase_manner": row.get("purchaseManner") or "",
                        "purchase_nature": row.get("purchaseNature") or "",
                        "budget": row.get("budget") or "",
                    },
                )
            )
        return items

    async def search(self, spec: TenderQuerySpec, fetcher: HttpFetcher) -> SourceSearchResult:
        started = time.perf_counter()
        params = {
            "siteId": self.site_id,
            "channel": self.channel_id,
            "currPage": 1,
            "pageSize": self.settings.max_results_per_source,
            "noticeType": "",
            "regionCode": spec.region_code or "",
            "title": spec.topic,
        }
        try:
            response = await fetcher.get(
                self.search_url,
                params=params,
                headers={
                    "Referer": f"{self.base_url}/freecms-glht/site/juncai/cggg/index.html",
                    "X-Requested-With": "XMLHttpRequest",
                },
                retries=1,
            )
            payload = json.loads(response.text)
            if str(payload.get("code")) != "200" or not isinstance(payload.get("data"), list):
                raise ValueError(str(payload.get("msg") or "公开公告接口返回失败状态"))
        except (FetchError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return SourceSearchResult(
                source=self.name,
                status=SourceStatus.FAILED,
                message=f"军队采购网公开公告检索失败：{exc}",
                latency_ms=round((time.perf_counter() - started) * 1000),
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
        return SourceSearchResult(
            source=self.name,
            status=SourceStatus.OK,
            items=items,
            scanned_count=len(parsed),
            prefilter_reasons=dict(prefilter),
            message=(
                f"军队采购网公开采购大厅检索完成，官方接口报告 {payload.get('total', 0)} 条匹配；"
                "本轮仅读取受限页数，不涉及工作台或 CA 权限。"
            ),
            latency_ms=round((time.perf_counter() - started) * 1000),
        )
