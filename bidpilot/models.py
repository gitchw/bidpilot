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


class RetrievalTerm(BaseModel):
    """One locally validated concept that may be used to retrieve real notices."""

    text: str = Field(min_length=2, max_length=40, description="经过本地校验的检索词")
    kind: Literal["topic", "synonym", "industry", "llm"] = Field(
        description="原主题、确定性同义词、行业术语或模型建议"
    )
    origin: Literal["query", "rules", "lexicon", "llm"] = Field(description="词条的可审计来源")
    reason: str = Field(max_length=240, description="使用该词的中文理由")
    trusted_for_match: bool = Field(
        default=True,
        description="是否可直接参与最终字面匹配；模型发现词固定为 false，只能扩大候选召回",
    )


class RetrievalQuery(BaseModel):
    """A bounded query variant; it never represents a generated tender notice."""

    id: str = Field(min_length=1, max_length=40, description="稳定查询标识")
    text: str = Field(min_length=2, max_length=60, description="发送给来源搜索框的查询词")
    round: int = Field(ge=1, le=2, description="计划使用的召回轮次")
    reason: str = Field(max_length=240, description="为什么执行该查询")
    term_kind: Literal["topic", "buyer", "synonym", "industry", "llm"] = Field(
        default="topic",
        description="查询由核心主题、本地锁定买方、同义词、行业词或模型建议产生",
    )


class RetrievalPlan(BaseModel):
    """Safe retrieval plan produced before any source is queried."""

    planner_version: str = "retrieval-v1"
    mode: Literal["deterministic", "hybrid", "fallback"] = "deterministic"
    llm_status: Literal[
        "disabled",
        "not_configured",
        "applied",
        "rejected",
        "invalid_response",
        "unavailable",
    ] = "disabled"
    terms: list[RetrievalTerm] = Field(default_factory=list, max_length=16)
    rejected_terms: list[str] = Field(
        default_factory=list,
        max_length=16,
        description="未通过本地约束的模型词条及简短原因；不保存模型原文",
    )
    queries: list[RetrievalQuery] = Field(default_factory=list, max_length=12)
    source_priorities: list[str] = Field(
        default_factory=list,
        description="模型建议且经本地来源 ID 白名单校验后的优先级",
    )
    max_rounds: int = Field(default=2, ge=1, le=2)
    query_budget_per_source: int = Field(default=2, ge=1, le=5)
    latency_ms: int = Field(default=0, ge=0)
    summary: str = Field(description="面向普通用户的计划说明")


class SearchRoundDiagnostic(BaseModel):
    round: int = Field(ge=1, le=2)
    trigger: str = Field(description="首轮或补搜的触发原因")
    queries: list[RetrievalQuery] = Field(default_factory=list)
    source_calls: int = Field(default=0, ge=0)
    scanned_count: int = Field(default=0, ge=0)
    candidate_count: int = Field(default=0, ge=0)
    unique_candidate_count: int = Field(default=0, ge=0)
    matched_candidate_count: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    gaps_after_round: list[str] = Field(default_factory=list)


class CandidateDecision(BaseModel):
    """Evidence-bound semantic review decision for a hard-filter-safe candidate."""

    candidate_id: str = Field(description="本轮本地候选 ID，不由模型生成 URL")
    title: str = Field(description="候选标题，便于用户核对")
    source: str = Field(description="真实来源名称")
    outcome: Literal["accepted", "rejected", "not_reviewed"]
    confidence: float = Field(default=0, ge=0, le=1)
    reason: str = Field(max_length=300)
    evidence_excerpt: str = Field(default="", max_length=500)
    evidence_quote: str = Field(
        default="",
        max_length=240,
        description="模型提议且已由本地确认逐字存在于该候选的证据引句",
    )
    matched_concepts: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="模型选择且通过本地可信概念白名单校验的主题概念",
    )


class RetrievalTrace(BaseModel):
    plan: RetrievalPlan
    rounds: list[SearchRoundDiagnostic] = Field(default_factory=list, max_length=2)
    gap_analysis: list[str] = Field(default_factory=list)
    semantic_review_status: Literal[
        "disabled",
        "not_needed",
        "not_configured",
        "applied",
        "invalid_response",
        "unavailable",
    ] = "not_needed"
    semantic_decisions: list[CandidateDecision] = Field(default_factory=list)
    unique_raw_candidates: int = Field(default=0, ge=0)
    summary: str = Field(description="完整检索链路的中文摘要")


class BriefEvidence(BaseModel):
    """A locally hydrated evidence reference; the model never controls its URL."""

    evidence_id: str = Field(pattern=r"^E\d{2,6}$", description="本轮稳定证据编号")
    record_id: str = Field(description="本地可信标讯规范 ID")
    title: str = Field(max_length=500)
    buyer: str | None = Field(default=None, max_length=300)
    published_at: datetime
    event_type: str = Field(max_length=40)
    opportunity_score: float = Field(ge=0, le=100)
    source_url: str = Field(max_length=3000)
    excerpt: str = Field(max_length=500)


class BriefClaim(BaseModel):
    text: str = Field(min_length=2, max_length=360)
    evidence_ids: list[str] = Field(min_length=1, max_length=5)


class BriefPriority(BaseModel):
    evidence_id: str
    title: str = Field(max_length=500)
    buyer: str | None = Field(default=None, max_length=300)
    published_at: datetime
    event_type: str = Field(max_length=40)
    opportunity_score: float = Field(ge=0, le=100)
    reason: str = Field(min_length=2, max_length=360)
    recommended_action: str = Field(min_length=2, max_length=360)
    source_url: str = Field(max_length=3000)


class BriefRisk(BaseModel):
    level: Literal["high", "medium", "low"]
    text: str = Field(min_length=2, max_length=360)
    evidence_ids: list[str] = Field(min_length=1, max_length=5)


class BriefAction(BaseModel):
    priority: Literal["P0", "P1", "P2"]
    text: str = Field(min_length=2, max_length=360)
    evidence_ids: list[str] = Field(min_length=1, max_length=5)


class IntelligenceBrief(BaseModel):
    """Evidence-grounded buyer needs, priorities, risks and next actions."""

    brief_version: str = "intelligence-v1"
    mode: Literal["llm_grounded", "deterministic"] = "deterministic"
    status: Literal[
        "applied",
        "cached",
        "disabled",
        "not_configured",
        "invalid_response",
        "unavailable",
        "empty",
    ]
    overview: str = Field(max_length=500)
    buyer_needs: list[BriefClaim] = Field(default_factory=list, max_length=6)
    priorities: list[BriefPriority] = Field(default_factory=list, max_length=5)
    risks: list[BriefRisk] = Field(default_factory=list, max_length=6)
    actions: list[BriefAction] = Field(default_factory=list, max_length=6)
    evidence_catalog: list[BriefEvidence] = Field(default_factory=list, max_length=25)
    generated_at: datetime
    latency_ms: int = Field(default=0, ge=0)
    repair_count: int = Field(default=0, ge=0, le=1)
    cache_hit: bool = False
    summary: str = Field(max_length=500)


class CompanyProfileUpdate(BaseModel):
    """User-owned business context used for local fit decisions, never source queries."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "company_name": "示例数字科技有限公司",
                    "offerings": ["AI 服务器", "数据中心集成", "运维服务"],
                    "strengths": ["信创适配", "本地交付", "等保建设"],
                    "target_regions": ["广东", "深圳"],
                    "excluded_terms": ["纯土建", "药品"],
                    "preferred_buyers": ["高校", "政务数据局"],
                    "decision_focus": "balanced",
                }
            ]
        },
    )

    company_name: str = Field(default="", max_length=120, description="企业或团队名称")
    offerings: list[str] = Field(
        default_factory=list,
        max_length=30,
        description="能够交付的产品与服务；用于适配判断，不会发送给检索来源",
    )
    strengths: list[str] = Field(
        default_factory=list,
        max_length=30,
        description="资质、交付、行业或技术优势",
    )
    target_regions: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="优先经营地域；空数组表示不额外限制",
    )
    excluded_terms: list[str] = Field(
        default_factory=list,
        max_length=30,
        description="企业明确不做或不希望投入的业务词",
    )
    preferred_buyers: list[str] = Field(
        default_factory=list,
        max_length=30,
        description="偏好的采购单位或客户类型",
    )
    decision_focus: Literal["balanced", "growth", "precision"] = Field(
        default="balanced",
        description="平衡、增长优先或精准优先的决策偏好",
    )

    @field_validator("company_name")
    @classmethod
    def normalize_company_name(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator(
        "offerings",
        "strengths",
        "target_regions",
        "excluded_terms",
        "preferred_buyers",
    )
    @classmethod
    def normalize_profile_lists(cls, value: list[str]) -> list[str]:
        cleaned = [" ".join(item.split())[:80] for item in value if item.strip()]
        return list(dict.fromkeys(cleaned))


class CompanyProfile(CompanyProfileUpdate):
    version: str = Field(default="empty", description="画像内容的本地稳定版本，用于缓存失效")
    updated_at: datetime | None = Field(default=None, description="最后一次网页保存时间")


class OpportunityFitAssessment(BaseModel):
    evidence_id: str = Field(pattern=r"^E\d{2,6}$")
    canonical_id: str
    version_hash: str
    title: str = Field(max_length=500)
    buyer: str | None = Field(default=None, max_length=300)
    published_at: datetime
    region: str | None = Field(default=None, max_length=120)
    event_type: str = Field(max_length=40)
    source_url: str = Field(max_length=3000)
    base_fit_score: float = Field(ge=0, le=100)
    personalization_adjustment: float = Field(ge=-12, le=12)
    fit_score: float = Field(ge=0, le=100)
    recommendation: Literal["bid", "watch", "skip"]
    matched_profile_terms: list[str] = Field(default_factory=list, max_length=12)
    evidence_quotes: list[str] = Field(
        default_factory=list,
        max_length=3,
        description="模型或本地判断实际引用的公告原文短句；模型返回时必须逐字存在于该条证据",
    )
    gaps: list[str] = Field(default_factory=list, max_length=5)
    reason: str = Field(max_length=360)
    risk: str = Field(default="", max_length=360)
    next_action: str = Field(max_length=360)
    personalization_reason: str = Field(default="", max_length=360)


class OpportunityAssessmentSet(BaseModel):
    assessment_version: str = "fit-v1"
    mode: Literal["llm_grounded", "deterministic"] = "deterministic"
    status: Literal[
        "applied",
        "cached",
        "disabled",
        "not_configured",
        "profile_missing",
        "invalid_response",
        "unavailable",
        "empty",
    ]
    profile_version: str = "empty"
    profile_configured: bool = False
    feedback_count: int = Field(default=0, ge=0)
    assessments: list[OpportunityFitAssessment] = Field(default_factory=list)
    evidence_catalog: list[BriefEvidence] = Field(default_factory=list)
    generated_at: datetime
    latency_ms: int = Field(default=0, ge=0)
    repair_count: int = Field(default=0, ge=0, le=1)
    cache_hit: bool = False
    summary: str = Field(max_length=600)


class EvidenceQuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(
        min_length=2,
        max_length=1000,
        description="针对某次运行固定证据集提出的问题；会在用户主动提交后发送给已配置模型",
    )

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        return " ".join(value.split())


class EvidenceAnswerCitation(BaseModel):
    evidence_id: str = Field(pattern=r"^E\d{2,6}$")
    quote: str = Field(min_length=2, max_length=220)
    title: str = Field(max_length=500)
    buyer: str | None = Field(default=None, max_length=300)
    published_at: datetime
    region: str | None = Field(default=None, max_length=120)
    event_type: str = Field(max_length=40)
    source_url: str = Field(max_length=3000)


class EvidenceAnswerClaim(BaseModel):
    text: str = Field(min_length=2, max_length=360)
    citations: list[EvidenceAnswerCitation] = Field(
        min_length=1,
        max_length=8,
        description="支撑同一条本地组装结论的证据字段；模型单次仍最多选择 8 项",
    )


class EvidenceAnswer(BaseModel):
    answer_version: str = "run-qa-v3"
    run_id: str
    mode: Literal["llm_grounded", "deterministic"] = "deterministic"
    status: Literal[
        "applied",
        "cached",
        "not_configured",
        "unavailable",
        "invalid_response",
        "insufficient_evidence",
        "refused",
        "empty",
        "evidence_incomplete",
    ]
    answerable: bool = False
    answer: str = Field(max_length=8000)
    claims: list[EvidenceAnswerClaim] = Field(
        default_factory=list,
        max_length=25,
        description="模型选择最多 8 条；结构化本地回退可覆盖当前最多 25 条固定上下文",
    )
    evidence_catalog: list[BriefEvidence] = Field(default_factory=list)
    total_evidence_count: int = Field(default=0, ge=0)
    context_evidence_count: int = Field(default=0, ge=0)
    context_truncated: bool = False
    generated_at: datetime
    latency_ms: int = Field(default=0, ge=0)
    repair_count: int = Field(default=0, ge=0, le=1)
    cache_hit: bool = False
    limitations: list[str] = Field(default_factory=list, max_length=8)
    summary: str = Field(max_length=800)


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


class FeedbackVerdict(StrEnum):
    RELEVANT = "relevant"
    IRRELEVANT = "irrelevant"
    WATCH = "watch"
    CONTACTED = "contacted"


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
    buyer_keywords: list[str] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "采购单位精确过滤白名单；空数组表示不限制。该字段由买方雷达从本地已抓取记录写入，"
            "不会由模型猜测，历史订阅缺少该字段时自动按空数组兼容。"
        ),
    )
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

    @field_validator("buyer_keywords")
    @classmethod
    def normalize_buyer_keywords(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in value:
            normalized = " ".join(item.split())[:200]
            key = normalized.casefold()
            if normalized and key not in seen:
                cleaned.append(normalized)
                seen.add(key)
        return cleaned

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
    summary_mode: Literal["extractive", "llm_grounded"] = "extractive"
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


class BuyerRadarTopic(BaseModel):
    """从本地已抓取公告文本确定性统计出的买方主题。"""

    name: str = Field(description="本地词典或公告标题提取出的主题名称", min_length=1, max_length=80)
    notice_count: int = Field(
        ge=1,
        description="包含该主题的去重公告数；同一公告的多个内容版本只计算一次",
    )


class BuyerRadarActivity(BaseModel):
    """买方雷达使用的一条本地持久化公告证据。"""

    canonical_id: str = Field(description="本地公告规范 ID，用于公告级去重")
    version_hash: str = Field(description="当前展示的本地内容版本哈希")
    project_key: str = Field(description="既有项目聚合键；不会为买方雷达重新计算")
    title: str = Field(description="本地标讯快照中的公告标题", max_length=500)
    buyer_name: str = Field(description="本地标讯快照中的采购单位名称", max_length=300)
    published_at: datetime = Field(description="公告原文发布时间，不是系统抓取时间")
    first_seen_at: datetime = Field(description="系统首次保存该内容版本的时间")
    last_seen_at: datetime = Field(description="系统最近一次发现该内容版本的时间")
    region: str | None = Field(default=None, description="公告标注的地域；原文未提供时为空")
    event_type: EventType = Field(description="公告生命周期阶段，如招标、更正、中标或合同")
    summary: str = Field(description="本地保存的证据摘要；不由买方雷达重新生成", max_length=1000)
    source_name: str = Field(
        default="",
        description="主证据来源名称；缺失时为空字符串，不会猜测来源",
        max_length=200,
    )
    source_url: str = Field(
        default="",
        description="从本地证据或来源链接回填的主原文地址；缺失时为空字符串",
        max_length=3000,
    )
    evidence_available: bool = Field(description="本地快照是否保留了可点击原文地址")


class BuyerRadarCard(BaseModel):
    """单个采购单位可审计的活动、生命周期和主题聚合。"""

    buyer_id: str = Field(description="采购单位规范名称的本地稳定哈希，不是外部机构编码")
    buyer_name: str = Field(description="本地标讯中最近使用的采购单位名称", max_length=300)
    notice_count: int = Field(ge=1, description="按 canonical_id 去重后的公告数量")
    version_count: int = Field(
        ge=1,
        description="这些公告在 tender_items 中保留的内容版本行数，用于审计变更",
    )
    project_count: int = Field(
        ge=1,
        description="按既有 project_key 去重得到的估算项目数，不宣称为采购方官方项目总数",
    )
    project_count_is_estimate: bool = Field(
        default=True,
        description="始终为 true，提醒项目键可能因标题变化或缺失项目编号而拆分",
    )
    first_activity_at: datetime = Field(description="该采购单位最早一条本地公告的原文发布时间")
    latest_activity_at: datetime = Field(description="该采购单位最近一条本地公告的原文发布时间")
    last_seen_at: datetime = Field(description="系统最近一次发现该采购单位公告内容的时间")
    stage_counts: dict[str, int] = Field(
        default_factory=dict,
        description="按公告生命周期阶段统计的去重公告数；不是销售机会跟进阶段",
    )
    top_topics: list[BuyerRadarTopic] = Field(
        default_factory=list,
        max_length=8,
        description="从去重公告文本确定性统计的高频主题，不是采购预测",
    )
    sources: list[str] = Field(
        default_factory=list,
        description="这些公告在本地快照中保留的真实来源名称",
    )
    evidence_notice_count: int = Field(
        ge=0,
        description="至少保留一个本地原文地址的去重公告数",
    )
    recent_activities: list[BuyerRadarActivity] = Field(
        default_factory=list,
        description="按原文发布时间倒序返回的近期去重公告证据",
    )


class BuyerRadarResult(BaseModel):
    """只使用本地 tender_items 构建、同时披露识别覆盖率的买方雷达。"""

    buyers: list[BuyerRadarCard] = Field(description="经过搜索和数量限制后返回的采购单位卡片")
    total_buyer_count: int = Field(ge=0, description="全部本地公告中可识别的采购单位总数")
    matched_buyer_count: int = Field(ge=0, description="应用 search 后匹配的采购单位数")
    returned_buyer_count: int = Field(ge=0, description="本次响应实际返回的采购单位数")
    total_local_notice_count: int = Field(
        ge=0,
        description="tender_items 按 canonical_id 去重后的全部本地公告数",
    )
    identified_buyer_notice_count: int = Field(
        ge=0,
        description="去重后具有可用采购单位名称的本地公告数",
    )
    unknown_buyer_notice_count: int = Field(
        ge=0,
        description="去重后未识别采购单位或最新可用快照损坏的本地公告数",
    )
    invalid_version_count: int = Field(
        ge=0,
        description="无法按 TenderRecord 恢复的历史内容版本行数；这些行不会被伪造补齐",
    )
    buyer_coverage_rate: float = Field(
        ge=0,
        le=100,
        description="可识别采购单位公告数占全部去重公告数的百分比",
    )
    generated_at: datetime = Field(description="本次本地聚合完成时间")
    coverage_note: str = Field(
        description="面向用户解释采购单位识别覆盖、未知记录和项目数估算边界",
        max_length=800,
    )


class FeedbackUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: FeedbackVerdict = Field(description="相关、无关、观察或已联系")
    reason: str = Field(
        default="",
        max_length=500,
        description="用户可选的判断原因；不会发送给检索来源",
    )

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        return " ".join(value.split())


class TenderFeedback(BaseModel):
    canonical_id: str
    version_hash: str
    verdict: FeedbackVerdict
    reason: str = ""
    record: TenderRecord
    created_at: datetime
    updated_at: datetime


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
    retrieval: RetrievalTrace | None = Field(
        default=None,
        description="AI 检索计划、多轮补搜、缺口判断和语义复核的完整审计轨迹",
    )
    intelligence_brief: IntelligenceBrief | None = Field(
        default=None,
        description="只引用本轮真实证据编号的采购需求、优先机会、风险和行动建议",
    )
    opportunity_assessments: OpportunityAssessmentSet | None = Field(
        default=None,
        description="结合企业画像和有界反馈学习生成的逐机会适配判断",
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


class BuyerSubscriptionCreate(SubscriptionCreate):
    """不允许客户端提交或改写采购单位身份的标准订阅控制项。"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "name": "安徽大学采购监控",
                    "query": "每天9点汇总最近30天服务器采购公告",
                    "delivery_channel": "local",
                    "delivery_policy": "on_change",
                    "run_immediately": False,
                }
            ]
        },
    )


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
