from __future__ import annotations

import json
import time
from collections import Counter
from urllib.parse import urlencode

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

_GUANGDONG_REGION_CODES = {
    "广州": "440100",
    "深圳": "440300",
    "珠海": "440400",
    "汕头": "440500",
    "佛山": "440600",
    "韶关": "440200",
    "河源": "441600",
    "梅州": "441400",
    "惠州": "441300",
    "汕尾": "441500",
    "东莞": "441900",
    "中山": "442000",
    "江门": "440700",
    "阳江": "441700",
    "湛江": "440800",
    "茂名": "440900",
    "肇庆": "441200",
    "清远": "441800",
    "潮州": "445100",
    "揭阳": "445200",
    "云浮": "445300",
}


class GDGPOSource(SourceAdapter):
    """广东省政府采购网公开全文检索与公告详情。"""

    source_id = "gdgpo"
    name = "广东省政府采购网"
    official = True
    base_url = "https://gdgpo.czt.gd.gov.cn"
    homepage = f"{base_url}/"
    access_mode = "public"
    query_mode = "regional_fulltext_api"
    supports_query_variants = True
    supports_region_filter = True
    supports_date_filter = True
    supports_pagination = True
    supports_detail = True
    coverage_note = (
        "覆盖广东省政府采购公开公告的官方全文检索、正文与公开附件；"
        "仅在全国、广东及广东城市查询中启用，不登录电子卖场或交易工作台。"
    )

    site_id = "cd64e06a-21a7-4620-aebc-0576bab7e07a"
    channels = "fca71be5-fc0c-45db-96af-f513e9abda9d,95ff31f3-a1af-4bc4-b1a2-54c894476193"
    search_url = f"{base_url}/gpcms/rest/web/v2/info/selectInfoForIndex"
    detail_url = f"{base_url}/gpcms/rest/web/v2/info/getInfoById"

    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    def _plain_title(value: str) -> str:
        # The search API injects a <font> highlight in the middle of words;
        # joining nodes with a space would turn “计算<font>服务器</font>采购”
        # into a different title and hurt deduplication.
        return normalize_space(BeautifulSoup(value or "", "lxml").get_text("", strip=True))

    @classmethod
    def _portal_route(cls, row: dict) -> str:
        notice_id = str(row.get("id") or "")
        channel = str(row.get("channel") or "")
        channel_name = normalize_space(row.get("channelName") or "")
        notice_type = str(row.get("noticeType") or "")
        notice_types = {item.strip() for item in notice_type.split(",") if item.strip()}

        if channel in {
            "fca71be5-fc0c-45db-96af-f513e9abda9d",
            "958b68d2-d97f-4f98-a0f4-3a5802ec94a9",
        }:
            if notice_types.intersection({"59", "001051", "001009", "00105A"}):
                path = "/articleGd"
                params = {"id": notice_id, "channelName": channel_name}
            elif notice_type == "001101":
                path = "/articleRedHeadGd"
                params = {"id": notice_id, "channelName": channel_name}
            else:
                path = "/noticeGd"
                params = {
                    "type": "notice",
                    "id": notice_id,
                    "channel": channel,
                    "openTenderCode": row.get("openTenderCode") or "",
                    "channelName": channel_name,
                }
        elif channel == "95ff31f3-a1af-4bc4-b1a2-54c894476193":
            path = "/articleRedHeadGd"
            params = {"id": notice_id, "channelName": channel_name}
        elif channel == "82fad126-7447-43a2-94aa-d42647349ae9":
            path = "/noticeKjxyGd"
            params = {
                "id": notice_id,
                "channel": channel,
                "kcProjectCode": row.get("kcProjectCode") or "",
            }
        else:
            path = "/articleGd"
            params = {"type": "article", "id": notice_id, "channelName": channel_name}
        return f"{cls.base_url}{path}?{urlencode(params)}"

    @classmethod
    def _public_detail_url(cls, row: dict) -> str:
        # The SPA's history routes return HTTP 403 when opened directly even
        # though they work after an in-site click. The documented public detail
        # API is directly reachable and contains the same official正文/附件, so it
        # is the stable evidence URL used in reports and exported records.
        return f"{cls.detail_url}?{urlencode({'id': str(row.get('id') or '')})}"

    @classmethod
    def parse_search_response(cls, payload: dict) -> list[RawTender]:
        rows = (payload.get("data") or {}).get("rows") or []
        items: list[RawTender] = []
        for row in rows:
            title = cls._plain_title(str(row.get("title") or ""))
            record_id = str(row.get("id") or "")
            if not title or not record_id:
                continue
            source_url = cls._public_detail_url(row)
            description = clean_html(str(row.get("description") or ""))
            notice_name = normalize_space(row.get("noticeTypeName") or row.get("channelName") or "")
            project_id = normalize_space(row.get("openTenderCode") or "") or extract_project_id(
                f"{title} {description}"
            )
            items.append(
                RawTender(
                    source=cls.name,
                    source_url=source_url,
                    title=title,
                    published_at=parse_datetime(str(row.get("noticeTime") or "")),
                    region=normalize_space(row.get("regionName") or "广东") or "广东",
                    buyer=normalize_space(row.get("purchaser") or "") or None,
                    body=description,
                    event_type=detect_event_type(f"{notice_name} {title}"),
                    project_id=project_id,
                    evidence=[
                        EvidenceSpan(text=(description or title)[:500], source_url=source_url)
                    ],
                    auth_level="public",
                    source_metadata={
                        "record_id": record_id,
                        "notice_id": row.get("noticeId") or "",
                        "notice_type": row.get("noticeType") or "",
                        "notice_type_name": notice_name,
                        "channel": row.get("channel") or "",
                        "channel_name": row.get("channelName") or "",
                        "portal_route": cls._portal_route(row),
                        "region_code": row.get("regionCode") or "",
                        "raw_region_name": row.get("regionName") or "",
                        "budget": row.get("budget"),
                        "total_rows": (payload.get("data") or {}).get("total", 0),
                    },
                )
            )
        return items

    @staticmethod
    def apply_detail(item: RawTender, payload: dict) -> RawTender:
        data = payload.get("data") or {}
        raw_html = str(data.get("content") or data.get("noticeContent") or "")
        body = clean_html(raw_html) if raw_html else normalize_space(data.get("description") or "")
        if body and len(body) >= len(item.body):
            item.body = body
        item.title = GDGPOSource._plain_title(str(data.get("title") or item.title)) or item.title
        item.buyer = normalize_space(data.get("purchaser") or item.buyer or "") or None
        item.project_id = (
            normalize_space(data.get("openTenderCode") or "")
            or item.project_id
            or extract_project_id(item.body)
        )
        item.event_type = detect_event_type(
            f"{data.get('noticeTypeName') or ''} {item.title} {item.body[:160]}"
        )

        attachments: list[Attachment] = []
        seen: set[str] = set()
        for row in data.get("attchList") or []:
            url = str(row.get("fileUrl") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            attachments.append(
                Attachment(
                    name=normalize_space(row.get("fileName") or "附件")[:120],
                    url=url,
                )
            )
        if raw_html:
            soup = BeautifulSoup(raw_html, "lxml")
            for name, url in find_attachments(soup, item.source_url):
                if url in seen:
                    continue
                seen.add(url)
                attachments.append(Attachment(name=name, url=url))
        item.attachments = attachments
        item.evidence = [
            EvidenceSpan(text=(item.body or item.title)[:500], source_url=item.source_url)
        ]
        item.source_metadata.update(
            {
                "detail_loaded": bool(item.body),
                "notice_type_name": data.get("noticeTypeName")
                or item.source_metadata.get("notice_type_name", ""),
                "purchase_manner": data.get("purchaseMannerName") or "",
                "budget": data.get("budget") or item.source_metadata.get("budget"),
            }
        )
        return item

    @staticmethod
    def _region_code(spec: TenderQuerySpec) -> str:
        if not spec.region or spec.region == "广东":
            return ""
        return _GUANGDONG_REGION_CODES.get(spec.region, "")

    @staticmethod
    def _is_relevant_region(spec: TenderQuerySpec) -> bool:
        return not spec.region or spec.region == "广东" or spec.region in _GUANGDONG_REGION_CODES

    async def search(self, spec: TenderQuerySpec, fetcher: HttpFetcher) -> SourceSearchResult:
        started = time.perf_counter()
        if not self._is_relevant_region(spec):
            return SourceSearchResult(
                source=self.name,
                status=SourceStatus.SKIPPED,
                message=f"广东官方来源不覆盖当前地域“{spec.region}”，本轮已按地域路由跳过。",
                latency_ms=0,
            )

        headers = {
            "Accept": "application/json,text/plain,*/*",
            "Referer": self.homepage,
        }
        # The public portal occasionally expects a same-session landing request.
        # Failure here does not block the documented public API itself.
        try:
            await fetcher.get(self.homepage, headers=headers, retries=0)
        except FetchError:
            pass

        parsed: list[RawTender] = []
        errors: list[str] = []
        scanned_count = 0
        page_size = min(20, self.settings.max_results_per_source)
        max_pages = max(1, (self.settings.max_results_per_source + page_size - 1) // page_size)
        for page_number in range(1, max_pages + 1):
            params = {
                "currPage": page_number,
                "pageSize": page_size,
                "siteId": self.site_id,
                "channel": self.channels,
                "noticeType": "",
                "purchaser": "",
                "agency": "",
                "operationStartTime": f"{spec.start_date.isoformat()} 00:00:00",
                "operationEndTime": f"{spec.end_date.isoformat()} 23:59:59",
                "searchKey": spec.topic,
                "regionCode": self._region_code(spec),
                "selectTimeName": "noticeTime",
                "cityOrAreal": "",
                "requestSource": "qwjs",
                "purchaseManner": "",
            }
            try:
                page = await fetcher.get(
                    self.search_url,
                    params=params,
                    headers=headers,
                    retries=1,
                )
                payload = json.loads(page.text)
                if str(payload.get("code")) != "200":
                    raise ValueError(str(payload.get("msg") or "官方检索接口返回失败状态"))
                page_items = self.parse_search_response(payload)
                scanned_count += len((payload.get("data") or {}).get("rows") or [])
                parsed.extend(page_items)
                if (
                    len(page_items) < page_size
                    or len(parsed) >= self.settings.max_results_per_source
                ):
                    break
            except (FetchError, json.JSONDecodeError, TypeError, ValueError) as exc:
                errors.append(f"第 {page_number} 页：{exc}")
                break

        prefilter: Counter[str] = Counter()
        items: list[RawTender] = []
        seen: set[str] = set()
        for item in parsed[: self.settings.max_results_per_source]:
            record_id = str(item.source_metadata.get("record_id") or "")
            if not record_id or record_id in seen:
                continue
            seen.add(record_id)
            if not (spec.start_date <= item.published_at.date() <= spec.end_date):
                prefilter["outside_time"] += 1
                continue
            raw_region = normalize_space(item.source_metadata.get("raw_region_name") or "")
            route_region = spec.region if spec.region and spec.region != "广东" else "广东"
            item.region = normalize_space(f"{route_region} {raw_region}")
            try:
                detail = await fetcher.get(
                    self.detail_url,
                    params={"id": record_id},
                    headers=headers,
                    retries=1,
                )
                detail_payload = json.loads(detail.text)
                if str(detail_payload.get("code")) != "200":
                    raise ValueError(str(detail_payload.get("msg") or "详情接口返回失败状态"))
                item = self.apply_detail(item, detail_payload)
            except (FetchError, json.JSONDecodeError, TypeError, ValueError) as exc:
                item.source_metadata["detail_loaded"] = False
                errors.append(f"详情 {record_id}：{exc}")
            items.append(item)

        if not parsed and errors:
            status = SourceStatus.FAILED
        elif errors or any(not item.source_metadata.get("detail_loaded") for item in items):
            status = SourceStatus.PARTIAL
        else:
            status = SourceStatus.OK
        detailed_count = sum(bool(item.source_metadata.get("detail_loaded")) for item in items)
        message = (
            f"广东政府采购官方全文检索完成，扫描 {scanned_count} 条，保留 {len(items)} 条，"
            f"成功读取 {detailed_count} 条公开正文与附件。"
            "仅使用公告公开接口，不登录电子卖场、交易工作台或代办投标。"
        )
        if detailed_count < len(items):
            message += (
                f" 另有 {len(items) - detailed_count} 条采购计划模板仅公开列表字段，"
                "已明确标记为部分覆盖。"
            )
        if errors:
            message += " 部分请求降级：" + "；".join(errors[:2])
        return SourceSearchResult(
            source=self.name,
            status=status,
            items=items,
            scanned_count=scanned_count,
            prefilter_reasons=dict(prefilter),
            message=message,
            latency_ms=round((time.perf_counter() - started) * 1000),
        )
