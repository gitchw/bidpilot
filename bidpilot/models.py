from __future__ import annotations

from datetime import date, datetime, time
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ScheduleKind(StrEnum):
    IMMEDIATE = "immediate"
    ONCE = "once"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class DeliveryPolicy(StrEnum):
    ALWAYS = "always"
    ON_CHANGE = "on_change"


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


class OpportunityStage(StrEnum):
    NEW = "new"
    FOLLOWING = "following"
    BIDDING = "bidding"
    WON = "won"
    LOST = "lost"
    ARCHIVED = "archived"


class IntentSchedule(BaseModel):
    kind: ScheduleKind = Field(default=ScheduleKind.IMMEDIATE, description="计划类型")
    send_time: time | None = Field(default=None, description="每日/每周/每月发送时刻")
    weekday: int | None = Field(default=None, description="周一为 0，周日为 6", ge=0, le=6)
    day_of_month: int | None = Field(
        default=None,
        description="每月日期；31 在短月自动落到月末",
        ge=1,
        le=31,
    )
    run_at: datetime | None = Field(default=None, description="一次性计划的绝对执行时间")
    timezone: str = Field(default="Asia/Shanghai", description="IANA 时区")
    expression: str = Field(default="立即执行", description="面向用户的中文计划说明")


class TenderQuerySpec(BaseModel):
    raw_query: str = Field(description="规范化后的用户原始问题")
    topic: str = Field(description="用于严格匹配的核心产品、服务或行业主题")
    keywords: list[str] = Field(description="主题和受控同义词扩展")
    exclude_keywords: list[str] = Field(default_factory=list, description="任一命中即排除")
    event_types: list[EventType] = Field(
        default_factory=list,
        description="显式公告类型过滤；空数组表示保留完整生命周期",
    )
    region: str | None = Field(default=None, description="省或城市名称；空值表示全国")
    region_code: str | None = Field(default=None, description="来源接口使用的省级行政代码")
    region_level: Literal["nationwide", "province", "city"] = Field(
        default="nationwide",
        description="全国、省级或城市粒度",
    )
    start_date: date = Field(description="检索开始日期，含当天")
    end_date: date = Field(description="检索结束日期，含当天")
    schedule: IntentSchedule = Field(default_factory=IntentSchedule, description="执行计划")
    delivery_channel: str = Field(default="local", description="解析出的投递通道 ID")
    slot_confidence: dict[str, float] = Field(
        default_factory=dict,
        description="主题、地域、时间和计划字段置信度",
    )
    warnings: list[str] = Field(default_factory=list, description="需要用户确认的解析警告")
    parser_version: str = Field(default="rules-v2", description="解析器规则版本")

    @field_validator("keywords")
    @classmethod
    def deduplicate_keywords(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))

    @field_validator("exclude_keywords")
    @classmethod
    def deduplicate_exclude_keywords(cls, value: list[str]) -> list[str]:
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


class OpportunityCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"canonical_id": "notice-id", "version_hash": "version-hash"}]
        }
    )

    canonical_id: str = Field(
        description="已写入 tender_items 的公告规范 ID",
        min_length=1,
        max_length=128,
    )
    version_hash: str = Field(
        description="该公告内容版本哈希",
        min_length=1,
        max_length=128,
    )


class OpportunityUpdate(BaseModel):
    stage: OpportunityStage | None = Field(default=None, description="机会阶段")
    owner: str | None = Field(default=None, description="负责人或团队", max_length=100)
    next_action_at: datetime | None = Field(default=None, description="下一步动作时间")
    notes: str | None = Field(default=None, description="跟进备注", max_length=4000)
    tags: list[str] | None = Field(default=None, description="最多 20 个标签", max_length=20)
    is_read: bool | None = Field(default=None, description="是否已读")

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return list(dict.fromkeys(item.strip()[:40] for item in value if item.strip()))


class Opportunity(BaseModel):
    id: str
    project_key: str
    record: TenderRecord
    stage: OpportunityStage = OpportunityStage.NEW
    owner: str = ""
    next_action_at: datetime | None = None
    notes: str = ""
    tags: list[str] = Field(default_factory=list)
    is_read: bool = False
    created_at: datetime
    updated_at: datetime


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
    delivery_channel: str | None = None
    delivery_status: str | None = None
    delivery_message: str | None = None


class SubscriptionCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "name": "深圳充电桩日报",
                    "query": "每天9点汇总最近1个月深圳充电桩信息",
                    "delivery_channel": "local",
                    "delivery_policy": "always",
                    "run_immediately": True,
                }
            ]
        }
    )

    name: str = Field(description="用户可读的订阅名称", min_length=1, max_length=100)
    query: str = Field(description="必须包含可识别计划的中文规则", min_length=2, max_length=500)
    delivery_channel: str = Field(default="local", description="已配置的投递通道 ID")
    delivery_policy: DeliveryPolicy = Field(
        default=DeliveryPolicy.ALWAYS,
        description="always 每轮回执；on_change 仅变化外发",
    )
    run_immediately: bool = Field(default=True, description="创建后是否立即进入待领取队列")


class SubscriptionUpdate(BaseModel):
    name: str | None = Field(default=None, description="新名称", min_length=1, max_length=100)
    query: str | None = Field(
        default=None,
        description="新自然语言规则；修改后重算下次时间",
        min_length=2,
        max_length=500,
    )
    delivery_channel: str | None = Field(default=None, description="新投递通道 ID")
    delivery_policy: DeliveryPolicy | None = Field(default=None, description="新无新增策略")


class Subscription(BaseModel):
    id: str
    name: str
    spec: TenderQuerySpec
    enabled: bool = True
    delivery_channel: str = "local"
    delivery_policy: DeliveryPolicy = DeliveryPolicy.ALWAYS
    created_at: datetime
    updated_at: datetime | None = None
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    last_status: RunStatus | None = None
    last_message: str | None = None
    last_new_count: int = 0
    consecutive_failures: int = 0
    last_run_id: str | None = None
    in_progress: bool = False


class HealthResponse(BaseModel):
    status: str
    version: str
    database: str
    report_dir: str
