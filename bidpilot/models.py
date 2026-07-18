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


class IntentFieldDecision(BaseModel):
    """一次混合解析中某个字段的可审计合并决定。"""

    field: str = Field(description="被检查的结构化字段")
    outcome: Literal["accepted", "rejected", "locked", "unchanged"] = Field(
        description="接受模型提议、拒绝提议、规则高置信锁定或无需修改"
    )
    rule_value: Any | None = Field(default=None, description="规则基线值")
    proposed_value: Any | None = Field(default=None, description="经过 JSON 解析的模型提议值")
    final_value: Any | None = Field(default=None, description="最终采用值")
    reason: str = Field(description="本地校验与合并理由")


class IntentResolution(BaseModel):
    """不含密钥、端点和模型原文的安全意图解析轨迹。"""

    mode: Literal["rules", "hybrid"] = Field(default="rules", description="最终解析模式")
    llm_status: Literal[
        "not_needed",
        "disabled",
        "not_configured",
        "applied",
        "confirmed",
        "rejected",
        "invalid_response",
        "unavailable",
    ] = Field(default="not_needed", description="LLM 辅助状态")
    trigger_reasons: list[str] = Field(default_factory=list, description="调用或跳过 LLM 的原因")
    decisions: list[IntentFieldDecision] = Field(
        default_factory=list,
        description="字段级接受、拒绝或锁定记录",
    )
    latency_ms: int = Field(default=0, ge=0, description="本轮 LLM 调用耗时；未调用为 0")
    summary: str = Field(default="规则解析结果可直接使用。", description="面向用户的中文说明")


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
    parser_version: str = Field(default="hybrid-v1", description="最终意图解析器版本")
    resolution: IntentResolution = Field(
        default_factory=IntentResolution,
        description="规则与 LLM 的安全合并轨迹；历史数据缺失时自动补默认值",
    )

    @field_validator("keywords")
    @classmethod
    def deduplicate_keywords(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))

    @field_validator("exclude_keywords")
    @classmethod
    def deduplicate_exclude_keywords(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))


class IntentComparison(BaseModel):
    """同一问题的规则基线与混合引擎最终结果。"""

    rules: TenderQuerySpec = Field(description="未经过 LLM 的确定性规则基线")
    resolved: TenderQuerySpec = Field(description="严格校验和保守合并后的最终结果")
    changed_fields: list[str] = Field(default_factory=list, description="最终实际变化的字段")


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
    source: str = Field(description="来源显示名称")
    status: SourceStatus = Field(description="来源状态：正常完成、覆盖不完整、需要登录或抓取失败")
    scanned_count: int = Field(default=0, ge=0, description="来源页面/API 中实际扫描的公告数")
    fetched_count: int = Field(
        default=0, ge=0, description="通过来源端初筛并进入统一证据核验的候选数"
    )
    kept_count: int = Field(default=0, ge=0, description="统一核验、去重后保留的可信结果数")
    rejected_count: int = Field(
        default=0,
        ge=0,
        description="来源端预过滤和统一证据过滤合计排除的公告数",
    )
    rejection_reasons: dict[str, int] = Field(
        default_factory=dict,
        description="按时间、地域、主题等原因统计的淘汰数量",
    )
    latency_ms: int = Field(default=0, ge=0, description="该来源本轮耗时，单位毫秒")
    message: str = Field(default="", description="来源覆盖、授权或故障的中文说明")


class SourceSearchResult(BaseModel):
    source: str
    status: SourceStatus
    items: list[RawTender] = Field(default_factory=list)
    scanned_count: int = Field(default=0, ge=0, description="适配器扫描但不一定返回的公告数")
    prefilter_reasons: dict[str, int] = Field(
        default_factory=dict,
        description="来源适配器在下载详情前执行的安全预过滤统计",
    )
    message: str = ""
    latency_ms: int = 0


class SearchSuggestion(BaseModel):
    id: str = Field(description="稳定的建议标识")
    title: str = Field(description="按钮标题")
    query: str = Field(description="只填回查询框、不会自动执行的建议问题")
    explanation: str = Field(description="为什么建议这样调整，以及可能增加的噪声")


class SearchExplanation(BaseModel):
    outcome: Literal["matched", "matched_partial", "all_filtered", "no_candidates"] = Field(
        description="结果类型：完整命中、部分覆盖命中、候选全部被过滤或没有候选"
    )
    total_scanned: int = Field(default=0, ge=0, description="全部来源实际读取的列表公告总数")
    total_candidates: int = Field(
        default=0, ge=0, description="通过来源端初筛并进入统一核验的候选总数"
    )
    total_kept: int = Field(default=0, ge=0, description="最终保留的可信结果总数")
    rejection_reasons: dict[str, int] = Field(
        default_factory=dict,
        description="按时间、地域、主题、公告类型和排除词等原因汇总的排除数量",
    )
    coverage_complete: bool = Field(
        default=False,
        description="是否所有已接入来源都完成了当前能力范围内的正常检索",
    )
    coverage_notes: list[str] = Field(
        default_factory=list,
        description="覆盖不完整、需要登录或抓取失败的逐来源中文说明",
    )
    summary: str = Field(description="面向普通用户的真实结果解释")
    suggestions: list[SearchSuggestion] = Field(
        default_factory=list,
        description="最多三条安全放宽建议；客户端只能填回问题，不能自动执行",
    )


class RunResult(BaseModel):
    run_id: str
    status: RunStatus
    spec: TenderQuerySpec
    records: list[TenderRecord]
    diagnostics: list[SourceDiagnostic]
    search_explanation: SearchExplanation | None = Field(
        default=None,
        description="候选扫描、淘汰原因、覆盖边界和不自动执行的放宽建议",
    )
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
