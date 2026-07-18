from __future__ import annotations

import re
from collections import defaultdict

from rapidfuzz.fuzz import ratio

from bidpilot.clean import normalize_space, stable_hash
from bidpilot.models import Attachment, RawTender, TenderQuerySpec, TenderRecord
from bidpilot.summarize import EvidenceSummarizer

EVENT_WORDS = re.compile(
    r"公开招标|竞争性磋商|竞争性谈判|询价|采购|招标|中标|成交|更正|变更|结果|公告|公示|项目"
)


def normalize_title(title: str) -> str:
    title = normalize_space(title).lower()
    title = EVENT_WORDS.sub("", title)
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", title)


def region_matches(item: RawTender, spec: TenderQuerySpec) -> bool:
    if not spec.region:
        return True
    evidence = f"{item.region or ''} {item.title} {item.buyer or ''} {item.body}"
    return spec.region in evidence


def buyer_matches(item: RawTender, spec: TenderQuerySpec) -> bool:
    """Match only explicit normalized buyer identities; do not infer aliases or subsidiaries."""
    if not spec.buyer_keywords:
        return True
    buyer_name = normalize_space(item.buyer or "").casefold()
    if not buyer_name:
        return False
    return buyer_name in {
        normalize_space(keyword).casefold() for keyword in spec.buyer_keywords if keyword.strip()
    }


def topic_evidence_text(item: RawTender, spec: TenderQuerySpec) -> tuple[str, str]:
    """Return topic evidence with locked buyer identities removed from title and body."""
    title = normalize_space(item.title)
    body = normalize_space(item.body)
    if spec.buyer_keywords:
        for buyer_keyword in spec.buyer_keywords:
            buyer_name = normalize_space(buyer_keyword)
            if not buyer_name:
                continue
            title = re.sub(re.escape(buyer_name), " ", title, flags=re.IGNORECASE)
            body = re.sub(re.escape(buyer_name), " ", body, flags=re.IGNORECASE)
    else:
        body = f"{item.buyer or ''} {body}"
    return normalize_space(title).casefold(), normalize_space(body).casefold()


def keyword_hits(item: RawTender, spec: TenderQuerySpec) -> tuple[int, bool]:
    title, body = topic_evidence_text(item, spec)
    exact_title = spec.topic.casefold() in title
    hits = sum(1 for keyword in spec.keywords if keyword.casefold() in f"{title} {body}")
    return hits, exact_title


def hard_filter_reason(item: RawTender, spec: TenderQuerySpec) -> str | None:
    """Apply non-negotiable constraints before any lexical or semantic topic decision."""
    if item.published_at.date() < spec.start_date or item.published_at.date() > spec.end_date:
        return "outside_time"
    if not region_matches(item, spec):
        return "region_mismatch"
    if not buyer_matches(item, spec):
        return "buyer_mismatch"
    if spec.event_types and item.event_type not in spec.event_types:
        return "event_type_mismatch"
    searchable = f"{item.title} {item.buyer or ''} {item.body}".casefold()
    if any(keyword.casefold() in searchable for keyword in spec.exclude_keywords):
        return "excluded_keyword"
    return None


def relevance_score(item: RawTender, spec: TenderQuerySpec) -> float:
    hits, exact_title = keyword_hits(item, spec)
    title, body = topic_evidence_text(item, spec)
    synonym_in_title = any(keyword.casefold() in title for keyword in spec.keywords)
    synonym_in_body = any(keyword.casefold() in body for keyword in spec.keywords)
    score = 0.0
    if exact_title:
        score += 48
    elif spec.topic.casefold() in body:
        score += 34
    elif synonym_in_title:
        score += 38
    elif synonym_in_body:
        score += 28
    score += min(hits * 7, 21)
    if spec.region:
        score += 18 if region_matches(item, spec) else 0
    else:
        score += 8
    if spec.start_date <= item.published_at.date() <= spec.end_date:
        score += 10
    if item.buyer:
        score += 3
    return min(score, 100)


def opportunity_score(item: RawTender, relevance: float) -> float:
    body = item.body
    score = relevance * 0.72
    if re.search(r"预算金额|项目预算|最高限价", body):
        score += 10
    if re.search(r"截止时间|开标时间|响应文件", body):
        score += 8
    if item.buyer:
        score += 5
    if item.attachments:
        score += 5
    if item.project_id:
        score += 4
    return min(round(score, 1), 100)


def _project_key(item: RawTender) -> str:
    if item.project_id:
        return stable_hash("project", item.project_id.upper())
    base = normalize_title(item.title)
    return stable_hash("project", base, normalize_space(item.buyer or ""), item.region or "")


def _canonical_id(item: RawTender) -> str:
    if item.project_id:
        return stable_hash("notice", item.project_id.upper(), item.event_type.value)
    return stable_hash(
        "notice",
        normalize_title(item.title),
        item.event_type.value,
        item.published_at.date().isoformat(),
        normalize_space(item.buyer or ""),
    )


def _version_hash(item: RawTender) -> str:
    return stable_hash(
        "version",
        normalize_space(item.title),
        normalize_space(item.body),
        item.published_at.isoformat(),
        *sorted(attachment.url for attachment in item.attachments),
    )


async def normalize_item(
    item: RawTender,
    spec: TenderQuerySpec,
    summarizer: EvidenceSummarizer,
) -> TenderRecord | None:
    record, _reason = await evaluate_item(item, spec, summarizer)
    return record


async def evaluate_item(
    item: RawTender,
    spec: TenderQuerySpec,
    summarizer: EvidenceSummarizer,
    *,
    semantic_confidence: float | None = None,
) -> tuple[TenderRecord | None, str | None]:
    """Return both the record and a stable reason when strict filtering rejects it."""
    hard_reason = hard_filter_reason(item, spec)
    if hard_reason:
        return None, hard_reason
    hits, exact_title = keyword_hits(item, spec)
    if not exact_title and hits == 0 and semantic_confidence is None:
        return None, "keyword_mismatch"
    relevance = relevance_score(item, spec)
    if semantic_confidence is not None:
        relevance = max(relevance, 45 + (semantic_confidence * 35))
    if relevance < 45:
        return None, "low_relevance"
    summary = await summarizer.summarize(item)
    project_key = _project_key(item)
    return TenderRecord(
        canonical_id=_canonical_id(item),
        project_key=project_key,
        version_hash=_version_hash(item),
        title=normalize_space(item.title),
        published_at=item.published_at,
        region=normalize_space(item.region or "") or None,
        buyer=normalize_space(item.buyer or "") or None,
        event_type=item.event_type,
        project_id=item.project_id,
        summary=summary.summary,
        body_excerpt=normalize_space(item.body)[:800],
        attachments=item.attachments,
        evidence=summary.evidence,
        source_urls=[item.source_url],
        sources=[item.source],
        relevance_score=round(relevance, 1),
        opportunity_score=opportunity_score(item, relevance),
        duplicate_count=1,
        lifecycle_id=project_key,
        auth_level=item.auth_level,
    ), None


def _merge_records(primary: TenderRecord, duplicate: TenderRecord) -> TenderRecord:
    primary.source_urls = list(dict.fromkeys([*primary.source_urls, *duplicate.source_urls]))
    primary.sources = list(dict.fromkeys([*primary.sources, *duplicate.sources]))
    primary.duplicate_count += duplicate.duplicate_count
    primary.relevance_score = max(primary.relevance_score, duplicate.relevance_score)
    primary.opportunity_score = min(
        100, max(primary.opportunity_score, duplicate.opportunity_score) + 3
    )
    if len(duplicate.body_excerpt) > len(primary.body_excerpt):
        primary.body_excerpt = duplicate.body_excerpt
        primary.summary = duplicate.summary
    attachment_map: dict[str, Attachment] = {item.url: item for item in primary.attachments}
    attachment_map.update({item.url: item for item in duplicate.attachments})
    primary.attachments = list(attachment_map.values())
    evidence_keys = {(item.text, item.source_url) for item in primary.evidence}
    for span in duplicate.evidence:
        if (span.text, span.source_url) not in evidence_keys:
            primary.evidence.append(span)
            evidence_keys.add((span.text, span.source_url))
    return primary


def deduplicate_records(records: list[TenderRecord]) -> list[TenderRecord]:
    by_id: dict[str, TenderRecord] = {}
    for record in sorted(records, key=lambda value: value.opportunity_score, reverse=True):
        if record.canonical_id in by_id:
            by_id[record.canonical_id] = _merge_records(by_id[record.canonical_id], record)
            continue
        duplicate_key = None
        for key, existing in by_id.items():
            if existing.event_type != record.event_type:
                continue
            if abs((existing.published_at.date() - record.published_at.date()).days) > 7:
                continue
            if ratio(normalize_title(existing.title), normalize_title(record.title)) >= 92:
                duplicate_key = key
                break
        if duplicate_key:
            by_id[duplicate_key] = _merge_records(by_id[duplicate_key], record)
        else:
            by_id[record.canonical_id] = record

    result = list(by_id.values())
    result.sort(key=lambda value: (value.opportunity_score, value.published_at), reverse=True)
    return result


def lifecycle_groups(records: list[TenderRecord]) -> dict[str, list[TenderRecord]]:
    groups: dict[str, list[TenderRecord]] = defaultdict(list)
    for record in records:
        groups[record.lifecycle_id].append(record)
    for group in groups.values():
        group.sort(key=lambda value: value.published_at)
    return dict(groups)
