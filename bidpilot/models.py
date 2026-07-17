from __future__ import annotations

from datetime import date, datetime, time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ScheduleKind(StrEnum):
    IMMEDIATE = "immediate"
    ONCE = "once"
    DAILY = "daily"
    WEEKLY = "weekly"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class SourceStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    AUTH_REQUIRED = "auth_required"
    FAILED = "failed"
    SKIPPED = "skipped"


class EventType(StrEnum):
    INTENTION = "采购意向"
    TENDER = "招标公告"
    CHANGE = "更正公告"
    AWARD = "中标公告"
    CONTRACT = "合同公告"
    OTHER = "其他公告"


class IntentSchedule(BaseModel):
    kind: ScheduleKind = ScheduleKind.IMMEDIATE
    send_time: time | None = None
    weekday: int | None = Field(default=None, ge=0, le=6)
    run_at: datetime | None = None
    timezone: str = "Asia/Shanghai"
    expression: str = "立即执行"


class TenderQuerySpec(BaseModel):
    raw_query: str
    topic: str
    keywords: list[str]
    region: str | None = None
    region_code: str | None = None
    start_date: date
    end_date: date
    schedule: IntentSchedule = Field(default_factory=IntentSchedule)
    delivery_channel: str = "local"
    slot_confidence: dict[str, float] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    parser_version: str = "rules-v1"

    @field_validator("keywords")
    @classmethod
    def deduplicate_keywords(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))


class Attachment(BaseModel):
    name: str
    url: str


class EvidenceSpan(BaseModel):
    text: str
    source_url: str
    field: str = "core_content"


class RawTender(BaseModel):
    source: str
    source_url: str
    title: str
    published_at: datetime
    region: str | None = None
    buyer: str | None = None
    body: str = ""
    attachments: list[Attachment] = Field(default_factory=list)
    event_type: EventType = EventType.OTHER
    project_id: str | None = None
    fetched_at: datetime = Field(default_factory=datetime.now)
    evidence: list[EvidenceSpan] = Field(default_factory=list)
    auth_level: str = "public"
    source_metadata: dict[str, Any] = Field(default_factory=dict)


class TenderRecord(BaseModel):
    canonical_id: str
    project_key: str
    version_hash: str
    title: str
    published_at: datetime
    region: str | None = None
    buyer: str | None = None
    event_type: EventType
    project_id: str | None = None
    summary: str
    body_excerpt: str
    attachments: list[Attachment] = Field(default_factory=list)
    evidence: list[EvidenceSpan] = Field(default_factory=list)
    source_urls: list[str]
    sources: list[str]
    relevance_score: float = Field(ge=0, le=100)
    opportunity_score: float = Field(ge=0, le=100)
    duplicate_count: int = 1
    lifecycle_id: str
    auth_level: str = "public"


class SourceDiagnostic(BaseModel):
    source: str
    status: SourceStatus
    fetched_count: int = 0
    kept_count: int = 0
    latency_ms: int = 0
    message: str = ""


class SourceSearchResult(BaseModel):
    source: str
    status: SourceStatus
    items: list[RawTender] = Field(default_factory=list)
    message: str = ""
    latency_ms: int = 0


class RunResult(BaseModel):
    run_id: str
    status: RunStatus
    spec: TenderQuerySpec
    records: list[TenderRecord]
    diagnostics: list[SourceDiagnostic]
    report_path: str | None = None
    new_count: int = 0
    started_at: datetime
    completed_at: datetime | None = None
    warnings: list[str] = Field(default_factory=list)


class SubscriptionCreate(BaseModel):
    name: str
    query: str
    delivery_channel: str = "local"


class Subscription(BaseModel):
    id: str
    name: str
    spec: TenderQuerySpec
    enabled: bool = True
    delivery_channel: str = "local"
    created_at: datetime
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None


class HealthResponse(BaseModel):
    status: str
    version: str
    database: str
    report_dir: str
