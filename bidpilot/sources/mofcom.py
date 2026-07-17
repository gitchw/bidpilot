from __future__ import annotations

import json
import re
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from bidpilot.clean import (
    clean_html,
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


class MofcomSource(SourceAdapter):
    name = "商务部中国国际招标网"
    requires_auth = False
    base_url = "https://chinabidding.mofcom.gov.cn"
    search_url = f"{base_url}/zbwcms/front/bidding/bulletinInfoList"
    event_types = {
        1: EventType.TENDER,
        2: EventType.CHANGE,
        3: EventType.AWARD,
        4: EventType.AWARD,
    }

    def __init__(self, settings: Settings):
        self.settings = settings

    @classmethod
    def parse_search_response(cls, payload: dict, event_type: EventType) -> list[RawTender]:
        items: list[RawTender] = []
        for row in payload.get("rows", []):
            title = normalize_space(row.get("name", ""))
            file_path = row.get("filePath", "")
            if not title or not file_path:
                continue
            digest = normalize_space(row.get("digest", ""))
            raw_region = normalize_space(row.get("areaName", ""))
            region = re.sub(r"^(?:中华人民共和国|中国)", "", raw_region)
            region = region.removesuffix("省").removesuffix("市") or None
            url = urljoin(f"{cls.base_url}/bidDetail/", file_path.lstrip("/"))
            items.append(
                RawTender(
                    source=cls.name,
                    source_url=url,
                    title=title,
                    published_at=parse_datetime(row.get("publishTime", "")),
                    region=region,
                    body=digest,
                    event_type=event_type,
                    project_id=extract_project_id(digest),
                    evidence=[EvidenceSpan(text=digest, source_url=url)] if digest else [],
                    auth_level="public",
                    source_metadata={
                        "industry": row.get("industryName", ""),
                        "capital_source": row.get("capitalSourceName", ""),
                        "source_id": row.get("fdid", ""),
                    },
                )
            )
        return items

    @staticmethod
    def parse_detail_page(html: str, item: RawTender) -> RawTender:
        soup = BeautifulSoup(html, "lxml")
        root = soup.select_one("div.article")
        if root is None:
            return item
        body = clean_html(str(root))
        if body:
            item.body = body
            item.project_id = item.project_id or extract_project_id(body)
            item.evidence = [EvidenceSpan(text=body[:500], source_url=item.source_url)]
        buyer = re.search(
            r"招标人[：:]\s*(.{2,80}?)(?=\s*(?:地址|联系人|联系方式|投标|招标机构|招标方式|招标结果|招标代理|$))",
            body,
        )
        if buyer:
            item.buyer = normalize_space(buyer.group(1))
        item.attachments = [
            Attachment(name=name, url=url) for name, url in find_attachments(root, item.source_url)
        ]
        return item

    async def search(self, spec: TenderQuerySpec, fetcher: HttpFetcher) -> SourceSearchResult:
        started = time.perf_counter()
        candidates: list[RawTender] = []
        errors: list[str] = []
        headers = {
            "Referer": f"{self.base_url}/channel/business/bulletinList.shtml",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        for type_code, event_type in self.event_types.items():
            if len(candidates) >= self.settings.max_results_per_source:
                break
            try:
                response = await fetcher.post_form(
                    self.search_url,
                    data={
                        "pageNumber": "1",
                        "keyWord": spec.topic,
                        # The portal's shorter presets intermittently return an empty
                        # page for same-day records. Query one year, then enforce the
                        # exact user window locally.
                        "timeType": "5",
                        "rangeCode": "",
                        "typeCode": str(type_code),
                        "capitalSourceCode": "",
                        "industryCode": "",
                        "provinceCode": spec.region_code or "",
                    },
                    headers=headers,
                    encoding="utf-8",
                    retries=1,
                )
                payload = json.loads(response.text)
                parsed = self.parse_search_response(payload, event_type)
                candidates.extend(
                    item
                    for item in parsed
                    if spec.start_date <= item.published_at.date() <= spec.end_date
                    and (not spec.region or not item.region or spec.region in item.region)
                )
            except (FetchError, json.JSONDecodeError, TypeError, ValueError) as exc:
                errors.append(f"类型 {type_code}: {exc}")

        detailed: list[RawTender] = []
        for item in candidates[: self.settings.max_results_per_source]:
            try:
                page = await fetcher.get(item.source_url, encoding="utf-8", retries=1)
                detailed.append(self.parse_detail_page(page.text, item))
            except FetchError as exc:
                errors.append(f"详情 {item.source_url}: {exc}")
                detailed.append(item)

        latency = int((time.perf_counter() - started) * 1000)
        status = (
            SourceStatus.PARTIAL
            if errors and detailed
            else SourceStatus.FAILED
            if errors
            else SourceStatus.OK
        )
        return SourceSearchResult(
            source=self.name,
            status=status,
            items=detailed,
            message="；".join(errors[:3]) if errors else "商务部机电产品招标公告与详情检索完成。",
            latency_ms=latency,
        )
