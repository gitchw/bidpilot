from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from bidpilot.clean import normalize_space, stable_hash
from bidpilot.intent import TOPIC_SYNONYMS
from bidpilot.models import (
    BuyerRadarActivity,
    BuyerRadarCard,
    BuyerRadarResult,
    BuyerRadarTopic,
    TenderRecord,
)

_TITLE_NOISE = re.compile(
    r"采购意向|需求公示|公开招标|竞争性磋商|竞争性谈判|询价|谈判|资格预审|"
    r"更正公告|变更公告|澄清公告|中标公告|成交公告|结果公告|合同公告|"
    r"招标公告|采购公告|公告|公示|项目",
    re.IGNORECASE,
)
_PUNCTUATION = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")


@dataclass(frozen=True, slots=True)
class _ObservedRecord:
    record: TenderRecord
    first_seen_at: datetime
    last_seen_at: datetime


def normalize_buyer_name(value: str | None) -> str:
    """Normalize conservatively without merging legal-entity suffixes or branches."""
    return normalize_space(value or "")


def buyer_identity(value: str) -> str:
    return stable_hash("buyer", normalize_buyer_name(value).casefold())


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _parse_observed_at(value: Any, fallback: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        parsed = fallback
    return _aware(parsed).astimezone(UTC)


def _row_sort_key(item: tuple[int, dict[str, Any]]) -> tuple[datetime, datetime, int]:
    index, row = item
    fallback = datetime.min.replace(tzinfo=UTC)
    return (
        _parse_observed_at(row.get("last_seen_at"), fallback),
        _parse_observed_at(row.get("first_seen_at"), fallback),
        -index,
    )


def _activity_sort_key(item: _ObservedRecord) -> tuple[datetime, datetime, str]:
    return (
        _aware(item.record.published_at).astimezone(UTC),
        item.last_seen_at,
        item.record.version_hash,
    )


def _primary_source_url(record: TenderRecord) -> str:
    for span in record.evidence:
        if _is_clickable_url(span.source_url):
            return span.source_url.strip()
    for url in record.source_urls:
        if _is_clickable_url(url):
            return url.strip()
    return ""


def _is_clickable_url(value: str) -> bool:
    value = value.strip()
    if not value or len(value) > 3000:
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme.casefold() in {"http", "https"} and bool(parsed.netloc)


def _record_topics(record: TenderRecord, buyer_name: str) -> list[str]:
    evidence_text = normalize_space(
        " ".join((record.title, record.summary, record.body_excerpt))
    ).casefold()
    matched = []
    for topic, synonyms in TOPIC_SYNONYMS.items():
        if any(term.casefold() in evidence_text for term in synonyms):
            matched.append(topic)
    if matched:
        return matched

    fallback = normalize_space(record.title)
    if buyer_name:
        fallback = re.sub(re.escape(buyer_name), " ", fallback, flags=re.IGNORECASE)
    if record.project_id:
        fallback = re.sub(re.escape(record.project_id), " ", fallback, flags=re.IGNORECASE)
    fallback = _TITLE_NOISE.sub(" ", fallback)
    fallback = normalize_space(_PUNCTUATION.sub(" ", fallback)).strip("-—_ ")
    return [fallback[:80] if len(fallback) >= 2 else "其他"]


class BuyerRadarAggregator:
    """Build buyer views from persisted tender rows without network or model calls."""

    def aggregate(
        self,
        rows: list[dict[str, Any]],
        *,
        search: str = "",
        limit: int = 100,
        activity_limit: int = 5,
    ) -> BuyerRadarResult:
        versions_per_notice: Counter[str] = Counter()
        selected: dict[str, _ObservedRecord] = {}
        all_notice_ids: set[str] = set()
        invalid_version_count = 0

        for _index, row in sorted(enumerate(rows), key=_row_sort_key, reverse=True):
            canonical_id = str(row.get("canonical_id") or "")
            if canonical_id:
                all_notice_ids.add(canonical_id)
                versions_per_notice[canonical_id] += 1
            try:
                record = TenderRecord.model_validate_json(row["payload_json"])
                if (
                    record.canonical_id != canonical_id
                    or record.version_hash != row.get("version_hash")
                    or record.project_key != row.get("project_key")
                ):
                    raise ValueError("tender_items identity does not match payload")
            except (KeyError, TypeError, ValueError):
                invalid_version_count += 1
                continue
            if canonical_id in selected:
                continue
            selected[canonical_id] = _ObservedRecord(
                record=record,
                first_seen_at=_parse_observed_at(row.get("first_seen_at"), record.published_at),
                last_seen_at=_parse_observed_at(row.get("last_seen_at"), record.published_at),
            )

        groups: dict[str, list[_ObservedRecord]] = defaultdict(list)
        for observed in selected.values():
            buyer_name = normalize_buyer_name(observed.record.buyer)
            if buyer_name and len(buyer_name) <= 300:
                groups[buyer_name.casefold()].append(observed)

        cards = [
            self._card(
                observed_records,
                versions_per_notice=versions_per_notice,
                activity_limit=activity_limit,
            )
            for observed_records in groups.values()
        ]
        cards.sort(
            key=lambda card: (
                _aware(card.latest_activity_at).astimezone(UTC),
                card.notice_count,
                card.buyer_name.casefold(),
            ),
            reverse=True,
        )

        needle = normalize_space(search).casefold()
        matched_cards = cards
        if needle:
            matched_cards = [
                card
                for card in cards
                if needle
                in " ".join(
                    (
                        card.buyer_name,
                        " ".join(topic.name for topic in card.top_topics),
                        " ".join(card.sources),
                    )
                ).casefold()
            ]
        returned = matched_cards[:limit]

        total_local_notice_count = len(all_notice_ids)
        identified_buyer_notice_count = sum(len(items) for items in groups.values())
        unknown_buyer_notice_count = max(
            total_local_notice_count - identified_buyer_notice_count,
            0,
        )
        coverage_rate = (
            round(identified_buyer_notice_count / total_local_notice_count * 100, 1)
            if total_local_notice_count
            else 0.0
        )
        return BuyerRadarResult(
            buyers=returned,
            total_buyer_count=len(cards),
            matched_buyer_count=len(matched_cards),
            returned_buyer_count=len(returned),
            total_local_notice_count=total_local_notice_count,
            identified_buyer_notice_count=identified_buyer_notice_count,
            unknown_buyer_notice_count=unknown_buyer_notice_count,
            invalid_version_count=invalid_version_count,
            buyer_coverage_rate=coverage_rate,
            generated_at=datetime.now(UTC),
            coverage_note=(
                f"买方雷达只统计本机 tender_items 中的 {total_local_notice_count} 条去重公告；"
                f"其中 {identified_buyer_notice_count} 条已识别采购单位，"
                f"{unknown_buyer_notice_count} 条尚未识别或缺少可用快照。"
                f"项目数按既有 project_key 估算，不能视为采购方官方项目总量。"
            ),
        )

    def find_buyer(self, rows: list[dict[str, Any]], buyer_id: str) -> BuyerRadarCard | None:
        result = self.aggregate(rows, limit=max(len(rows), 1), activity_limit=1)
        return next((card for card in result.buyers if card.buyer_id == buyer_id), None)

    def _card(
        self,
        observed_records: list[_ObservedRecord],
        *,
        versions_per_notice: Counter[str],
        activity_limit: int,
    ) -> BuyerRadarCard:
        ordered = sorted(observed_records, key=_activity_sort_key, reverse=True)
        buyer_name = normalize_buyer_name(ordered[0].record.buyer)
        topic_counts: Counter[str] = Counter()
        stage_counts: Counter[str] = Counter()
        sources: set[str] = set()
        activities: list[BuyerRadarActivity] = []

        for observed in ordered:
            record = observed.record
            stage_counts[record.event_type.value] += 1
            topic_counts.update(_record_topics(record, buyer_name))
            sources.update(
                normalize_space(source)[:200] for source in record.sources if source.strip()
            )
            source_url = _primary_source_url(record)
            activities.append(
                BuyerRadarActivity(
                    canonical_id=record.canonical_id,
                    version_hash=record.version_hash,
                    project_key=record.project_key,
                    title=record.title[:500],
                    buyer_name=normalize_buyer_name(record.buyer),
                    published_at=record.published_at,
                    first_seen_at=observed.first_seen_at,
                    last_seen_at=observed.last_seen_at,
                    region=record.region,
                    event_type=record.event_type,
                    summary=record.summary[:1000],
                    source_name=record.sources[0][:200] if record.sources else "",
                    source_url=source_url,
                    evidence_available=bool(source_url),
                )
            )

        oldest = min(ordered, key=_activity_sort_key)
        newest = ordered[0]
        topics = [
            BuyerRadarTopic(name=name, notice_count=count)
            for name, count in sorted(
                topic_counts.items(),
                key=lambda item: (-item[1], item[0]),
            )[:8]
        ]
        return BuyerRadarCard(
            buyer_id=buyer_identity(buyer_name),
            buyer_name=buyer_name,
            notice_count=len(ordered),
            version_count=sum(
                versions_per_notice[observed.record.canonical_id] for observed in ordered
            ),
            project_count=len({observed.record.project_key for observed in ordered}),
            first_activity_at=oldest.record.published_at,
            latest_activity_at=newest.record.published_at,
            last_seen_at=max(observed.last_seen_at for observed in ordered),
            stage_counts=dict(sorted(stage_counts.items())),
            top_topics=topics,
            sources=sorted(sources),
            evidence_notice_count=sum(activity.evidence_available for activity in activities),
            recent_activities=activities[:activity_limit],
        )
