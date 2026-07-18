from __future__ import annotations

import asyncio
import json
from collections import Counter
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from bidpilot.buyer_radar import BuyerRadarAggregator
from bidpilot.clean import stable_hash
from bidpilot.config import Settings
from bidpilot.db import Database
from bidpilot.decision import OpportunityFitAssessor
from bidpilot.delivery import DeliveryManager, DeliveryReceipt
from bidpilot.evidence_qa import RunEvidenceQACopilot
from bidpilot.hybrid_intent import HybridIntentEngine
from bidpilot.intelligence import IntelligenceBriefGenerator
from bidpilot.intent import IntentParser
from bidpilot.models import (
    BuyerRadarResult,
    CompanyProfile,
    CompanyProfileUpdate,
    DeliveryPolicy,
    EventType,
    EvidenceAnswer,
    FeedbackUpdate,
    IntelligenceBrief,
    IntentComparison,
    IntentFieldDecision,
    Opportunity,
    OpportunityAssessmentSet,
    OpportunityCreate,
    OpportunityStage,
    OpportunityUpdate,
    RunResult,
    RunStatus,
    ScheduleKind,
    SourceStatus,
    Subscription,
    SubscriptionUpdate,
    TenderFeedback,
    TenderQuerySpec,
    TenderRecord,
)
from bidpilot.pipeline import TenderPipeline
from bidpilot.report import generate_report
from bidpilot.runtime_config import RuntimeConfiguration
from bidpilot.scheduler import maintain_subscription_lease, next_schedule_time, retry_time
from bidpilot.source_auth import SourceAuthManager
from bidpilot.sources import (
    CCGPSource,
    CEBPubServiceSource,
    CECBidSource,
    GDGPOSource,
    GGZYSource,
    MofcomSource,
    PLAPSource,
    QianlimaSource,
    SZGGZYSource,
    ZYCGSource,
)
from bidpilot.sources.base import SourceAdapter

_EVENT_RECENCY_RANK = {
    EventType.INTENTION: 0,
    EventType.TENDER: 1,
    EventType.CHANGE: 2,
    EventType.AWARD: 3,
    EventType.CONTRACT: 4,
    EventType.OTHER: 0,
}


class RunExecutionError(RuntimeError):
    def __init__(self, run_id: str, message: str):
        super().__init__(message)
        self.run_id = run_id


class SubscriptionBusyError(RuntimeError):
    pass


class RunEvidenceNotReadyError(RuntimeError):
    pass


class BidPilotService:
    def __init__(self, settings: Settings, sources: list[SourceAdapter] | None = None):
        self.settings = settings
        self.settings.ensure_directories()
        self.db = Database(settings.database_path)
        self.runtime_config = RuntimeConfiguration(self.db, settings)
        self.parser = IntentParser(settings.timezone)
        self.intent_engine = HybridIntentEngine(settings, self.parser)
        self.sources = sources or [
            SZGGZYSource(settings),
            GDGPOSource(settings),
            CEBPubServiceSource(settings),
            PLAPSource(settings),
            ZYCGSource(settings),
            CECBidSource(settings),
            CCGPSource(settings),
            GGZYSource(settings),
            MofcomSource(settings),
            QianlimaSource(settings),
        ]
        self.source_auth = SourceAuthManager(self.db, settings, self.sources)
        self.pipeline = TenderPipeline(settings, self.sources)
        self.intelligence = IntelligenceBriefGenerator(settings)
        self.fit_assessor = OpportunityFitAssessor(settings)
        self.evidence_qa = RunEvidenceQACopilot(settings, self.db)
        self.buyer_radar_aggregator = BuyerRadarAggregator()
        self.delivery = DeliveryManager(settings)
        self._live_results: dict[str, RunResult] = {}
        self.worker = None

    async def parse_intent(
        self,
        query: str,
        *,
        now: datetime | None = None,
    ) -> TenderQuerySpec:
        """Resolve an intent through rules first and a guarded LLM repair when needed."""
        self.runtime_config.load_persisted()
        return await self.intent_engine.resolve(query, now=now)

    async def compare_intent(
        self,
        query: str,
        *,
        now: datetime | None = None,
    ) -> IntentComparison:
        self.runtime_config.load_persisted()
        return await self.intent_engine.compare(query, now=now)

    @staticmethod
    def _lock_buyer_filter(
        spec: TenderQuerySpec,
        buyer_keywords: list[str],
    ) -> TenderQuerySpec:
        """Attach a local-only buyer constraint and make the lock visible in the intent audit."""
        validated = TenderQuerySpec.model_validate(
            {**spec.model_dump(mode="python"), "buyer_keywords": buyer_keywords}
        )
        locked = validated.buyer_keywords
        if not locked:
            return spec
        spec.buyer_keywords = locked
        spec.resolution.decisions = [
            decision for decision in spec.resolution.decisions if decision.field != "buyer_keywords"
        ]
        spec.resolution.decisions.append(
            IntentFieldDecision(
                field="buyer_keywords",
                outcome="locked",
                rule_value=[],
                proposed_value=None,
                final_value=locked,
                reason="采购单位来自本地买方雷达，模型和来源响应都不能改写该精确过滤条件",
            )
        )
        spec.resolution.trigger_reasons = list(
            dict.fromkeys(
                [
                    *spec.resolution.trigger_reasons,
                    "已应用本地买方雷达精确过滤",
                ]
            )
        )
        spec.resolution.summary = (
            f"{spec.resolution.summary.rstrip('。')}；已锁定本地采购单位：{'、'.join(locked)}。"
        )
        return spec

    async def run_query(
        self,
        query: str,
        *,
        subscription_id: str | None = None,
        delivery_channel: str | None = None,
        trigger_reason: str = "manual",
        buyer_keywords: list[str] | None = None,
    ) -> RunResult:
        # A standalone worker is a separate process. Reload the allowlisted
        # SQLite-backed settings before every real run so Web changes apply
        # without restarting that worker.
        self.runtime_config.load_persisted()
        self.source_auth.load_persisted()
        started_at = datetime.now(ZoneInfo(self.settings.timezone))
        spec = await self.intent_engine.resolve(query, now=started_at)
        if buyer_keywords:
            spec = self._lock_buyer_filter(spec, buyer_keywords)
        if delivery_channel:
            spec.delivery_channel = delivery_channel
        channel = spec.delivery_channel
        run_id = uuid4().hex
        self.db.create_run(
            run_id,
            spec,
            subscription_id=subscription_id,
            trigger_reason=trigger_reason,
        )
        diagnostics_dump: list[dict] = []
        result_count = 0
        report_path: Path | None = None
        subscription_row = self.db.get_subscription(subscription_id) if subscription_id else None
        try:
            pipeline_result = await self.pipeline.run(spec)
            records = pipeline_result.records
            result_count = len(records)
            self.db.upsert_records([record.model_dump(mode="json") for record in records])
            self._refresh_opportunities_for_projects(records)
            diagnostics_dump = [
                item.model_dump(mode="json") for item in pipeline_result.diagnostics
            ]
            for diagnostic in diagnostics_dump:
                self.db.add_source_run(run_id, diagnostic)

            incremental = subscription_id is not None
            output_records = records
            if subscription_id:
                keys = [(record.canonical_id, record.version_hash) for record in records]
                allowed = self.db.undelivered_keys(subscription_id, keys)
                output_records = [
                    record
                    for record in records
                    if (record.canonical_id, record.version_hash) in allowed
                ]

            self.db.set_run_items(
                run_id,
                [record.model_dump(mode="json") for record in output_records],
            )

            intelligence_brief, opportunity_assessments = await asyncio.gather(
                self._build_intelligence_brief(spec, output_records),
                self._build_opportunity_assessments(output_records),
            )

            if output_records or not incremental:
                report_path = generate_report(
                    spec,
                    output_records,
                    pipeline_result.diagnostics,
                    self.settings.report_dir,
                    generated_at=started_at,
                    incremental=incremental,
                    intelligence_brief=intelligence_brief,
                )

            policy = DeliveryPolicy(
                subscription_row.get("delivery_policy", DeliveryPolicy.ALWAYS.value)
                if subscription_row
                else DeliveryPolicy.ALWAYS.value
            )
            should_notify = report_path is not None or (
                incremental and policy == DeliveryPolicy.ALWAYS
            )
            if should_notify:
                try:
                    receipt = await self.delivery.deliver(
                        report_path,
                        channel,
                        new_count=len(output_records),
                        subscription_name=subscription_row["name"] if subscription_row else None,
                    )
                except Exception as exc:
                    self.db.create_delivery_attempt(
                        run_id=run_id,
                        subscription_id=subscription_id,
                        channel=channel,
                        success=False,
                        skipped=False,
                        message=str(exc),
                        external_id=None,
                    )
                    raise
            else:
                receipt = DeliveryReceipt(
                    channel=channel,
                    success=True,
                    skipped=True,
                    message="本轮无新增；按“仅有变化时通知”策略未外发。",
                )
            self.db.create_delivery_attempt(
                run_id=run_id,
                subscription_id=subscription_id,
                channel=receipt.channel,
                success=receipt.success,
                skipped=receipt.skipped,
                message=receipt.message,
                external_id=receipt.external_id,
            )
            if not receipt.success:
                raise RuntimeError(receipt.message)

            if report_path:
                self.db.create_report(
                    uuid4().hex,
                    run_id,
                    str(report_path),
                    len(output_records),
                    subscription_id,
                )
                if subscription_id:
                    self.db.mark_delivered(
                        subscription_id,
                        [(record.canonical_id, record.version_hash) for record in output_records],
                        str(report_path),
                    )

            partial_statuses = {
                SourceStatus.PARTIAL,
                SourceStatus.AUTH_REQUIRED,
                SourceStatus.FAILED,
            }
            status = (
                RunStatus.PARTIAL
                if any(item.status in partial_statuses for item in pipeline_result.diagnostics)
                else RunStatus.COMPLETED
            )
            completed_at = datetime.now(ZoneInfo(self.settings.timezone))
            result = RunResult(
                run_id=run_id,
                status=status,
                spec=spec,
                records=output_records,
                diagnostics=pipeline_result.diagnostics,
                search_explanation=pipeline_result.search_explanation,
                retrieval=pipeline_result.retrieval,
                intelligence_brief=intelligence_brief,
                opportunity_assessments=opportunity_assessments,
                report_path=str(report_path) if report_path else None,
                new_count=len(output_records),
                started_at=started_at,
                completed_at=completed_at,
                warnings=spec.warnings,
                delivery_channel=receipt.channel,
                delivery_status="skipped" if receipt.skipped else "success",
                delivery_message=receipt.message,
            )
            self.db.complete_run(
                run_id,
                status,
                report_path=str(report_path) if report_path else None,
                result_count=result_count,
                new_count=len(output_records),
                diagnostics=diagnostics_dump,
                retrieval=pipeline_result.retrieval.model_dump(mode="json"),
                brief=intelligence_brief.model_dump(mode="json"),
                assessment=opportunity_assessments.model_dump(mode="json"),
            )
            self._live_results[run_id] = result
            return result
        except Exception as exc:
            self.db.complete_run(
                run_id,
                RunStatus.FAILED,
                report_path=str(report_path) if report_path else None,
                result_count=result_count,
                new_count=0,
                diagnostics=diagnostics_dump,
                retrieval=None,
                error=str(exc),
            )
            raise RunExecutionError(run_id, str(exc)) from exc

    async def _build_intelligence_brief(
        self,
        spec: TenderQuerySpec,
        records: list[TenderRecord],
    ) -> IntelligenceBrief:
        cache_key = self.intelligence.cache_key(spec, records)
        if (
            records
            and self.settings.intelligence_brief_mode == "auto"
            and self.settings.llm_base_url
            and self.settings.llm_model
        ):
            cached = self.db.get_cached_intelligence_brief(cache_key)
            if cached:
                try:
                    brief = IntelligenceBrief.model_validate(cached)
                    if brief.mode == "llm_grounded":
                        return brief.model_copy(
                            update={
                                "status": "cached",
                                "cache_hit": True,
                                "summary": (
                                    "内容与模型配置未变化，已复用本机证据简报缓存；"
                                    "标题、评分和链接仍来自同一批可信记录。"
                                ),
                            }
                        )
                except (TypeError, ValueError):
                    pass
        brief = await self.intelligence.generate(spec, records)
        if brief.mode == "llm_grounded" and brief.status == "applied":
            self.db.set_cached_intelligence_brief(
                cache_key,
                brief.model_dump(mode="json"),
            )
        return brief

    async def _build_opportunity_assessments(
        self,
        records: list[TenderRecord],
    ) -> OpportunityAssessmentSet:
        profile = self.get_company_profile()
        feedback = self.list_feedback()
        cache_key = self.fit_assessor.cache_key(records, profile, feedback)
        if (
            records
            and self.settings.decision_assessment_mode == "auto"
            and self.settings.llm_base_url
            and self.settings.llm_model
            and profile.version != "empty"
        ):
            cached = self.db.get_cached_decision_assessment(cache_key)
            if cached:
                try:
                    assessment = OpportunityAssessmentSet.model_validate(cached)
                    if assessment.mode == "llm_grounded":
                        return assessment.model_copy(
                            update={
                                "status": "cached",
                                "cache_hit": True,
                                "summary": (
                                    "本轮证据、企业画像、反馈和模型配置均未变化，"
                                    "已复用证据约束适配判断缓存。"
                                ),
                            }
                        )
                except (TypeError, ValueError):
                    pass
        assessment = await self.fit_assessor.generate(records, profile, feedback)
        if assessment.mode == "llm_grounded" and assessment.status == "applied":
            self.db.set_cached_decision_assessment(
                cache_key,
                assessment.model_dump(mode="json"),
            )
        return assessment

    async def assess_run(self, run_id: str) -> OpportunityAssessmentSet:
        self.runtime_config.load_persisted()
        records = self.get_run_evidence(run_id)
        assessment = await self._build_opportunity_assessments(records)
        self.db.update_run_assessment(run_id, assessment.model_dump(mode="json"))
        live = self._live_results.get(run_id)
        if live is not None:
            self._live_results[run_id] = live.model_copy(
                update={"opportunity_assessments": assessment}
            )
        return assessment

    async def ask_run(self, run_id: str, question: str) -> EvidenceAnswer:
        self.runtime_config.load_persisted()
        run = self.db.get_run(run_id)
        if run is None:
            raise KeyError("运行记录不存在")
        if run["status"] in {RunStatus.QUEUED.value, RunStatus.RUNNING.value}:
            raise RunEvidenceNotReadyError("运行尚未结束，固定证据快照还没有准备完成")

        rows = self.db.list_run_items(run_id)
        records = []
        for row in rows:
            try:
                records.append(TenderRecord.model_validate_json(row["snapshot_json"]))
            except (KeyError, ValueError):
                return self.evidence_qa.evidence_incomplete(run_id, total_rows=len(rows))
        return await self.evidence_qa.answer(
            run_id,
            question,
            records,
            run_status=run["status"],
        )

    def _channel_is_configured(self, channel: str) -> bool:
        if channel == "feishu":
            return any(
                item["configured"]
                for item in self.delivery.channel_status()
                if item["id"].startswith("feishu_")
            )
        return any(
            item["id"] == channel and item["configured"] for item in self.delivery.channel_status()
        )

    def create_subscription(
        self,
        name: str,
        query: str,
        delivery_channel: str = "local",
        delivery_policy: DeliveryPolicy = DeliveryPolicy.ALWAYS,
        run_immediately: bool = True,
    ) -> Subscription:
        now = datetime.now(ZoneInfo(self.settings.timezone))
        spec = self.parser.parse(query, now=now)
        return self._create_subscription_from_spec(
            name,
            spec,
            delivery_channel,
            delivery_policy,
            run_immediately,
            now,
        )

    async def create_subscription_hybrid(
        self,
        name: str,
        query: str,
        delivery_channel: str = "local",
        delivery_policy: DeliveryPolicy = DeliveryPolicy.ALWAYS,
        run_immediately: bool = True,
    ) -> Subscription:
        now = datetime.now(ZoneInfo(self.settings.timezone))
        spec = await self.parse_intent(query, now=now)
        return self._create_subscription_from_spec(
            name,
            spec,
            delivery_channel,
            delivery_policy,
            run_immediately,
            now,
        )

    async def create_buyer_subscription(
        self,
        buyer_id: str,
        name: str,
        query: str,
        delivery_channel: str = "local",
        delivery_policy: DeliveryPolicy = DeliveryPolicy.ALWAYS,
        run_immediately: bool = True,
    ) -> Subscription:
        """Create a normal subscription with a locally selected buyer identity locked in."""
        rows = self.db.list_tender_items_for_buyer_radar()
        buyer = self.buyer_radar_aggregator.find_buyer(rows, buyer_id)
        if buyer is None:
            raise KeyError("采购单位不存在或已不在本地买方雷达中")
        if not 2 <= len(buyer.buyer_name) <= 60:
            raise ValueError("采购单位名称长度必须为 2～60 字，当前记录无法建立可靠的来源查询")
        now = datetime.now(ZoneInfo(self.settings.timezone))
        spec = await self.parse_intent(query, now=now)
        spec = self._lock_buyer_filter(spec, [buyer.buyer_name])
        return self._create_subscription_from_spec(
            name,
            spec,
            delivery_channel,
            delivery_policy,
            run_immediately,
            now,
        )

    def _create_subscription_from_spec(
        self,
        name: str,
        spec: TenderQuerySpec,
        delivery_channel: str,
        delivery_policy: DeliveryPolicy,
        run_immediately: bool,
        now: datetime,
    ) -> Subscription:
        if spec.schedule.kind == ScheduleKind.IMMEDIATE:
            raise ValueError("订阅问题必须包含每天、每周或明确的未来发送时间")
        if not self._channel_is_configured(delivery_channel):
            raise ValueError(f"投递通道 {delivery_channel} 尚未配置，不能创建虚假推送承诺")
        delivery_policy = DeliveryPolicy(delivery_policy)
        spec.delivery_channel = delivery_channel
        for row in self.db.list_subscriptions():
            if row["raw_query"] != spec.raw_query or row["delivery_channel"] != delivery_channel:
                continue
            try:
                existing_spec = TenderQuerySpec.model_validate_json(row["spec_json"])
            except (KeyError, TypeError, ValueError):
                continue
            if existing_spec.buyer_keywords != spec.buyer_keywords:
                continue
            existing = self._subscription_from_row(row)
            if run_immediately and not existing.in_progress:
                self.db.set_subscription_due(row["id"], now, enabled=True)
                row = self.db.get_subscription(row["id"])
                assert row is not None
            return self._subscription_from_row(row)

        subscription_id = uuid4().hex
        next_run_at = now if run_immediately else next_schedule_time(spec.schedule, now)
        self.db.create_subscription(
            subscription_id,
            name,
            spec,
            delivery_channel,
            next_run_at,
            delivery_policy,
        )
        row = self.db.get_subscription(subscription_id)
        assert row is not None
        return self._subscription_from_row(row)

    async def run_subscription(
        self,
        subscription_id: str,
        *,
        trigger_reason: str = "manual",
        lease_owner: str | None = None,
    ) -> RunResult:
        row = self.db.get_subscription(subscription_id)
        if row is None:
            raise KeyError(f"订阅不存在：{subscription_id}")
        now = datetime.now(ZoneInfo(self.settings.timezone))
        manually_claimed = lease_owner is None
        if lease_owner is None:
            lease_owner = f"manual:{uuid4().hex}"
            if not self.db.claim_subscription(
                subscription_id,
                worker_id=lease_owner,
                now=now,
                lease_until=now + timedelta(seconds=self.settings.worker_lease_seconds),
            ):
                raise SubscriptionBusyError("该订阅正在执行，请等待本轮完成后再试")
            row = self.db.get_subscription(subscription_id)
            assert row is not None
        elif row.get("lease_owner") != lease_owner:
            raise SubscriptionBusyError("订阅租约已被其他 worker 接管，本轮不再重复执行")
        spec = TenderQuerySpec.model_validate_json(row["spec_json"])
        renewal = None
        if manually_claimed:
            renewal = asyncio.create_task(
                maintain_subscription_lease(
                    self.db,
                    subscription_id,
                    worker_id=lease_owner,
                    timezone=ZoneInfo(self.settings.timezone),
                    lease_seconds=self.settings.worker_lease_seconds,
                ),
                name=f"bidpilot-manual-lease:{subscription_id}",
            )
        try:
            try:
                result = await self.run_query(
                    spec.raw_query,
                    subscription_id=subscription_id,
                    delivery_channel=row["delivery_channel"],
                    trigger_reason=trigger_reason,
                    buyer_keywords=spec.buyer_keywords,
                )
            except RunExecutionError as exc:
                failed_at = datetime.now(ZoneInfo(self.settings.timezone))
                retry_at = retry_time(failed_at, int(row.get("consecutive_failures", 0)))
                regular_at = next_schedule_time(spec.schedule, failed_at)
                next_run_at = min(retry_at, regular_at) if regular_at else retry_at
                self.db.finish_subscription_attempt(
                    subscription_id,
                    last_run_at=failed_at,
                    next_run_at=next_run_at,
                    status=RunStatus.FAILED,
                    message=str(exc),
                    new_count=0,
                    run_id=exc.run_id,
                    success=False,
                )
                raise

            finished_at = result.completed_at or datetime.now(ZoneInfo(self.settings.timezone))
            next_run_at = next_schedule_time(spec.schedule, finished_at)
            self.db.finish_subscription_attempt(
                subscription_id,
                last_run_at=finished_at,
                next_run_at=next_run_at,
                status=result.status,
                message=result.delivery_message or f"运行完成，新增 {result.new_count} 条。",
                new_count=result.new_count,
                run_id=result.run_id,
                success=True,
            )
            if spec.schedule.kind == ScheduleKind.ONCE and next_run_at is None:
                self.db.set_subscription_due(subscription_id, None, enabled=False)
            return result
        finally:
            if renewal:
                renewal.cancel()
                with suppress(asyncio.CancelledError):
                    await renewal

    def repair_subscription_schedules(self) -> int:
        now = datetime.now(ZoneInfo(self.settings.timezone))
        repaired = 0
        for row in self.db.list_subscriptions():
            if not row["enabled"] or row["next_run_at"]:
                continue
            spec = TenderQuerySpec.model_validate_json(row["spec_json"])
            next_run_at = next_schedule_time(spec.schedule, now)
            if next_run_at:
                self.db.set_subscription_due(row["id"], next_run_at)
                repaired += 1
        return repaired

    def _subscription_from_row(self, row: dict) -> Subscription:
        lease_until = datetime.fromisoformat(row["lease_until"]) if row.get("lease_until") else None
        now = datetime.now(ZoneInfo(self.settings.timezone))
        return Subscription(
            id=row["id"],
            name=row["name"],
            spec=TenderQuerySpec.model_validate_json(row["spec_json"]),
            enabled=bool(row["enabled"]),
            delivery_channel=row["delivery_channel"],
            delivery_policy=DeliveryPolicy(row.get("delivery_policy", "always")),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]) if row.get("updated_at") else None,
            last_run_at=(
                datetime.fromisoformat(row["last_run_at"]) if row.get("last_run_at") else None
            ),
            next_run_at=(
                datetime.fromisoformat(row["next_run_at"]) if row.get("next_run_at") else None
            ),
            last_status=RunStatus(row["last_status"]) if row.get("last_status") else None,
            last_message=row.get("last_message"),
            last_new_count=int(row.get("last_new_count", 0)),
            consecutive_failures=int(row.get("consecutive_failures", 0)),
            last_run_id=row.get("last_run_id"),
            in_progress=bool(row.get("lease_owner") and lease_until and lease_until > now),
        )

    def get_subscription(self, subscription_id: str) -> Subscription | None:
        row = self.db.get_subscription(subscription_id)
        return self._subscription_from_row(row) if row else None

    def list_subscriptions(self) -> list[dict]:
        return [
            self._subscription_from_row(row).model_dump(mode="json")
            for row in self.db.list_subscriptions()
        ]

    def update_subscription(self, subscription_id: str, update: SubscriptionUpdate) -> Subscription:
        row = self.db.get_subscription(subscription_id)
        if row is None:
            raise KeyError(f"订阅不存在：{subscription_id}")
        if update.delivery_channel and not self._channel_is_configured(update.delivery_channel):
            raise ValueError(f"投递通道 {update.delivery_channel} 尚未配置")
        spec = None
        next_run_at = None
        if update.query is not None:
            current = self._subscription_from_row(row)
            if current.in_progress:
                raise SubscriptionBusyError("该订阅正在执行，请在本轮完成后再修改规则")
            now = datetime.now(ZoneInfo(self.settings.timezone))
            spec = self.parser.parse(update.query, now=now)
            if spec.schedule.kind == ScheduleKind.IMMEDIATE:
                raise ValueError("订阅规则必须包含每天、每周或明确的未来发送时间")
            if current.spec.buyer_keywords:
                spec = self._lock_buyer_filter(spec, current.spec.buyer_keywords)
            spec.delivery_channel = update.delivery_channel or row["delivery_channel"]
            next_run_at = next_schedule_time(spec.schedule, now) if row["enabled"] else None
        self.db.update_subscription(
            subscription_id,
            name=update.name,
            spec=spec,
            next_run_at=next_run_at,
            update_next_run=spec is not None,
            delivery_channel=update.delivery_channel,
            delivery_policy=update.delivery_policy,
        )
        subscription = self.get_subscription(subscription_id)
        assert subscription is not None
        return subscription

    async def update_subscription_hybrid(
        self,
        subscription_id: str,
        update: SubscriptionUpdate,
    ) -> Subscription:
        row = self.db.get_subscription(subscription_id)
        if row is None:
            raise KeyError(f"订阅不存在：{subscription_id}")
        if update.delivery_channel and not self._channel_is_configured(update.delivery_channel):
            raise ValueError(f"投递通道 {update.delivery_channel} 尚未配置")
        spec = None
        next_run_at = None
        if update.query is not None:
            current = self._subscription_from_row(row)
            if current.in_progress:
                raise SubscriptionBusyError("该订阅正在执行，请在本轮完成后再修改规则")
            now = datetime.now(ZoneInfo(self.settings.timezone))
            spec = await self.parse_intent(update.query, now=now)
            if spec.schedule.kind == ScheduleKind.IMMEDIATE:
                raise ValueError("订阅规则必须包含每天、每周或明确的未来发送时间")
            if current.spec.buyer_keywords:
                spec = self._lock_buyer_filter(spec, current.spec.buyer_keywords)
            spec.delivery_channel = update.delivery_channel or row["delivery_channel"]
            next_run_at = next_schedule_time(spec.schedule, now) if row["enabled"] else None
        self.db.update_subscription(
            subscription_id,
            name=update.name,
            spec=spec,
            next_run_at=next_run_at,
            update_next_run=spec is not None,
            delivery_channel=update.delivery_channel,
            delivery_policy=update.delivery_policy,
        )
        subscription = self.get_subscription(subscription_id)
        assert subscription is not None
        return subscription

    def pause_subscription(self, subscription_id: str) -> Subscription:
        row = self.db.get_subscription(subscription_id)
        if row is None:
            raise KeyError(f"订阅不存在：{subscription_id}")
        if self._subscription_from_row(row).in_progress:
            raise SubscriptionBusyError("该订阅正在执行，请在本轮完成后再暂停")
        self.db.set_subscription_due(subscription_id, None, enabled=False)
        subscription = self.get_subscription(subscription_id)
        assert subscription is not None
        return subscription

    def resume_subscription(
        self, subscription_id: str, *, run_immediately: bool = False
    ) -> Subscription:
        row = self.db.get_subscription(subscription_id)
        if row is None:
            raise KeyError(f"订阅不存在：{subscription_id}")
        now = datetime.now(ZoneInfo(self.settings.timezone))
        spec = TenderQuerySpec.model_validate_json(row["spec_json"])
        next_run_at = now if run_immediately else next_schedule_time(spec.schedule, now)
        if next_run_at is None:
            raise ValueError("一次性订阅已经过期，请创建新的发送计划")
        self.db.set_subscription_due(subscription_id, next_run_at, enabled=True)
        subscription = self.get_subscription(subscription_id)
        assert subscription is not None
        return subscription

    def delete_subscription(self, subscription_id: str) -> None:
        row = self.db.get_subscription(subscription_id)
        if row is None:
            raise KeyError(f"订阅不存在：{subscription_id}")
        if self._subscription_from_row(row).in_progress:
            raise SubscriptionBusyError("该订阅正在执行，请在本轮完成后再删除")
        self.db.delete_subscription(subscription_id)

    def list_subscription_runs(self, subscription_id: str, limit: int = 20) -> list[dict]:
        rows = self.db.list_subscription_runs(subscription_id, limit)
        for row in rows:
            row["spec"] = json.loads(row.pop("spec_json"))
            row["diagnostics"] = json.loads(row.pop("diagnostics_json"))
            row["retrieval"] = json.loads(row.pop("retrieval_json", "{}") or "{}")
            row["intelligence_brief"] = json.loads(row.pop("brief_json", "{}") or "{}") or None
            row["opportunity_assessments"] = (
                json.loads(row.pop("assessment_json", "{}") or "{}") or None
            )
        return rows

    def list_delivery_attempts(self, subscription_id: str | None = None) -> list[dict]:
        return self.db.list_delivery_attempts(subscription_id=subscription_id)

    def list_runs(self, limit: int = 30) -> list[dict]:
        rows = self.db.list_runs(limit)
        for row in rows:
            row["spec"] = json.loads(row.pop("spec_json"))
            row["diagnostics"] = json.loads(row.pop("diagnostics_json"))
            row["retrieval"] = json.loads(row.pop("retrieval_json", "{}") or "{}")
            row["intelligence_brief"] = json.loads(row.pop("brief_json", "{}") or "{}") or None
            row["opportunity_assessments"] = (
                json.loads(row.pop("assessment_json", "{}") or "{}") or None
            )
        return rows

    def get_run(self, run_id: str) -> dict | RunResult | None:
        if run_id in self._live_results:
            return self._live_results[run_id]
        row = self.db.get_run(run_id)
        if row:
            row["spec"] = json.loads(row.pop("spec_json"))
            row["diagnostics"] = json.loads(row.pop("diagnostics_json"))
            row["retrieval"] = json.loads(row.pop("retrieval_json", "{}") or "{}")
            row["intelligence_brief"] = json.loads(row.pop("brief_json", "{}") or "{}") or None
            row["opportunity_assessments"] = (
                json.loads(row.pop("assessment_json", "{}") or "{}") or None
            )
        return row

    @staticmethod
    def _profile_from_row(row: dict | None) -> CompanyProfile:
        if row is None:
            return CompanyProfile()
        try:
            payload = json.loads(row["profile_json"])
            version = stable_hash(
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                length=16,
            )
            return CompanyProfile.model_validate(
                {
                    **payload,
                    "version": version,
                    "updated_at": row["updated_at"],
                }
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return CompanyProfile()

    def get_company_profile(self) -> CompanyProfile:
        return self._profile_from_row(self.db.get_company_profile())

    def update_company_profile(self, update: CompanyProfileUpdate) -> CompanyProfile:
        payload = update.model_dump(mode="json")
        row = self.db.set_company_profile(payload)
        return self._profile_from_row(row)

    def get_run_evidence(self, run_id: str) -> list[TenderRecord]:
        if self.db.get_run(run_id) is None:
            raise KeyError("运行记录不存在")
        records = []
        for row in self.db.list_run_items(run_id):
            try:
                records.append(TenderRecord.model_validate_json(row["snapshot_json"]))
            except (KeyError, ValueError):
                continue
        return records

    def _feedback_from_row(self, row: dict) -> TenderFeedback:
        selected = self.db.get_tender_item(row["canonical_id"], row["version_hash"])
        if selected is None:
            raise KeyError("反馈引用的标讯记录不存在")
        return TenderFeedback(
            canonical_id=row["canonical_id"],
            version_hash=row["version_hash"],
            verdict=row["verdict"],
            reason=row.get("reason", ""),
            record=TenderRecord.model_validate_json(selected["payload_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def set_feedback(
        self,
        canonical_id: str,
        version_hash: str,
        update: FeedbackUpdate,
    ) -> TenderFeedback:
        if self.db.get_tender_item(canonical_id, version_hash) is None:
            raise KeyError("只能评价系统已经抓取并验证过的标讯记录")
        row = self.db.upsert_feedback(
            canonical_id=canonical_id,
            version_hash=version_hash,
            verdict=update.verdict.value,
            reason=update.reason,
        )
        return self._feedback_from_row(row)

    def list_feedback(self, limit: int = 500) -> list[TenderFeedback]:
        feedback = []
        for row in self.db.list_feedback(limit):
            try:
                feedback.append(self._feedback_from_row(row))
            except (KeyError, ValueError):
                continue
        return feedback

    def delete_feedback(self, canonical_id: str, version_hash: str) -> None:
        if not self.db.delete_feedback(canonical_id, version_hash):
            raise KeyError("反馈记录不存在")

    def clear_feedback(self) -> int:
        return self.db.clear_feedback()

    def list_buyer_radar(
        self,
        *,
        search: str = "",
        limit: int = 100,
        activity_limit: int = 5,
    ) -> BuyerRadarResult:
        """Aggregate persisted notices only; this method never calls sources or an LLM."""
        rows = self.db.list_tender_items_for_buyer_radar()
        return self.buyer_radar_aggregator.aggregate(
            rows,
            search=search,
            limit=min(max(limit, 1), 200),
            activity_limit=min(max(activity_limit, 1), 100),
        )

    def list_reports(self) -> list[dict]:
        return self.db.list_reports()

    @staticmethod
    def _opportunity_from_row(row: dict) -> Opportunity:
        return Opportunity(
            id=row["id"],
            project_key=row["project_key"],
            record=TenderRecord.model_validate_json(row["snapshot_json"]),
            stage=OpportunityStage(row["stage"]),
            owner=row.get("owner", ""),
            next_action_at=(
                datetime.fromisoformat(row["next_action_at"]) if row.get("next_action_at") else None
            ),
            notes=row.get("notes", ""),
            tags=json.loads(row.get("tags_json") or "[]"),
            is_read=bool(row.get("is_read")),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _record_recency_key(self, record: TenderRecord) -> tuple[datetime, int]:
        published_at = record.published_at
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=ZoneInfo(self.settings.timezone))
        return (
            published_at.astimezone(UTC),
            _EVENT_RECENCY_RANK[record.event_type],
        )

    def _latest_project_record(self, project_key: str) -> TenderRecord:
        records = [
            TenderRecord.model_validate_json(row["payload_json"])
            for row in self.db.list_project_items(project_key)
        ]
        if not records:
            raise KeyError(f"项目不存在：{project_key}")
        _, latest = max(
            enumerate(records),
            key=lambda item: (*self._record_recency_key(item[1]), item[0]),
        )
        return latest

    def _refresh_opportunities_for_projects(self, records: list[TenderRecord]) -> None:
        for project_key in {record.project_key for record in records}:
            if self.db.get_opportunity_by_project_key(project_key) is None:
                continue
            latest = self._latest_project_record(project_key)
            self.db.refresh_opportunity_snapshot(
                project_key=project_key,
                canonical_id=latest.canonical_id,
                version_hash=latest.version_hash,
                snapshot=latest.model_dump(mode="json"),
            )

    def create_opportunity(self, request: OpportunityCreate) -> Opportunity:
        selected = self.db.get_tender_item(request.canonical_id, request.version_hash)
        if selected is None:
            raise KeyError("只能收藏系统已抓取并验证过的标讯记录")
        latest = self._latest_project_record(selected["project_key"])
        row = self.db.upsert_opportunity(
            opportunity_id=uuid4().hex,
            project_key=latest.project_key,
            canonical_id=latest.canonical_id,
            version_hash=latest.version_hash,
            snapshot=latest.model_dump(mode="json"),
        )
        return self._opportunity_from_row(row)

    def get_opportunity(self, opportunity_id: str) -> Opportunity | None:
        row = self.db.get_opportunity(opportunity_id)
        return self._opportunity_from_row(row) if row else None

    def list_opportunities(
        self,
        *,
        stage: OpportunityStage | None = None,
        search: str | None = None,
    ) -> list[dict]:
        opportunities = [
            self._opportunity_from_row(row)
            for row in self.db.list_opportunities(stage.value if stage else None)
        ]
        needle = (search or "").strip().casefold()
        if needle:
            opportunities = [
                item
                for item in opportunities
                if needle
                in " ".join(
                    (
                        item.record.title,
                        item.record.buyer or "",
                        item.record.region or "",
                        item.owner,
                        item.notes,
                        " ".join(item.tags),
                    )
                ).casefold()
            ]
        return [item.model_dump(mode="json") for item in opportunities]

    def update_opportunity(self, opportunity_id: str, update: OpportunityUpdate) -> Opportunity:
        if self.db.get_opportunity(opportunity_id) is None:
            raise KeyError(f"机会不存在：{opportunity_id}")
        changes: dict[str, object] = {}
        fields = update.model_fields_set
        if "stage" in fields and update.stage is not None:
            changes["stage"] = update.stage.value
        if "owner" in fields:
            changes["owner"] = (update.owner or "").strip()
        if "next_action_at" in fields:
            next_action_at = update.next_action_at
            if next_action_at and next_action_at.tzinfo is None:
                next_action_at = next_action_at.replace(tzinfo=ZoneInfo(self.settings.timezone))
            changes["next_action_at"] = next_action_at.isoformat() if next_action_at else None
        if "notes" in fields:
            changes["notes"] = (update.notes or "").strip()
        if "tags" in fields:
            changes["tags_json"] = json.dumps(update.tags or [], ensure_ascii=False)
        if "is_read" in fields:
            changes["is_read"] = int(bool(update.is_read))
        self.db.update_opportunity(opportunity_id, changes)
        result = self.get_opportunity(opportunity_id)
        assert result is not None
        return result

    def delete_opportunity(self, opportunity_id: str) -> None:
        if not self.db.delete_opportunity(opportunity_id):
            raise KeyError(f"机会不存在：{opportunity_id}")

    def opportunity_timeline(self, opportunity_id: str) -> list[dict]:
        opportunity = self.db.get_opportunity(opportunity_id)
        if opportunity is None:
            raise KeyError(f"机会不存在：{opportunity_id}")
        records = [
            TenderRecord.model_validate_json(row["payload_json"])
            for row in self.db.list_project_items(opportunity["project_key"])
        ]
        records.sort(key=self._record_recency_key)
        return [item.model_dump(mode="json") for item in records]

    def system_status(self) -> dict:
        now = datetime.now(UTC)
        workers = []
        for row in self.db.list_workers():
            heartbeat = datetime.fromisoformat(row["heartbeat_at"])
            if heartbeat.tzinfo is None:
                heartbeat = heartbeat.replace(tzinfo=UTC)
            row["online"] = (now - heartbeat).total_seconds() <= self.settings.worker_heartbeat_ttl
            workers.append(row)
        subscriptions = self.db.list_subscriptions()
        local_now = datetime.now(ZoneInfo(self.settings.timezone))
        running_count = sum(
            1
            for row in subscriptions
            if row.get("lease_owner")
            and row.get("lease_until")
            and datetime.fromisoformat(row["lease_until"]) > local_now
        )
        due_count = sum(
            1
            for row in subscriptions
            if row["enabled"]
            and row.get("next_run_at")
            and datetime.fromisoformat(row["next_run_at"]) <= local_now
        )
        return {
            "worker_online": any(item["online"] for item in workers),
            "workers": workers,
            "subscription_count": len(subscriptions),
            "enabled_subscription_count": sum(bool(row["enabled"]) for row in subscriptions),
            "running_subscription_count": running_count,
            "due_count": due_count,
            "delivery_channels": self.delivery.channel_status(),
            "intent_engine": self.intent_engine.metrics,
            "timezone": self.settings.timezone,
        }

    def source_status(self) -> list[dict]:
        latest = self.db.latest_source_runs()
        rows = []
        for source in self.sources:
            capabilities = source.capabilities()
            authorization = self.source_auth.status(source.source_id)
            configured = (
                authorization.state == "authorized" if isinstance(source, QianlimaSource) else True
            )
            rows.append(
                {
                    **capabilities,
                    "name": source.name,
                    "requires_auth": source.requires_auth,
                    "configured": configured,
                    "member_enhanced": (
                        authorization.state == "authorized"
                        if isinstance(source, CECBidSource)
                        else authorization.state == "authorized"
                        if isinstance(source, QianlimaSource)
                        else False
                    ),
                    "mode": (
                        "授权免费会员"
                        if source.requires_auth
                        else "公开 + 会员增强"
                        if isinstance(source, CECBidSource)
                        else "公开"
                    ),
                    "official": source.official,
                    "authorization_state": (authorization.state),
                    "authorization": authorization.model_dump(mode="json"),
                    "last_status": latest.get(source.name, {}).get("status"),
                    "last_checked_at": latest.get(source.name, {}).get("started_at"),
                    "last_message": latest.get(source.name, {}).get("message"),
                    "last_scanned_count": latest.get(source.name, {}).get("scanned_count", 0),
                    "last_fetched_count": latest.get(source.name, {}).get("fetched_count", 0),
                    "last_kept_count": latest.get(source.name, {}).get("kept_count", 0),
                    "last_latency_ms": latest.get(source.name, {}).get("latency_ms", 0),
                    "last_rejected_count": latest.get(source.name, {}).get("rejected_count", 0),
                    "last_rejection_reasons": json.loads(
                        latest.get(source.name, {}).get("rejection_json", "{}") or "{}"
                    ),
                }
            )
        return rows

    def source_health(self, window: int = 20) -> dict:
        window = max(5, min(window, 100))
        history = self.db.list_source_run_history(
            limit=max(100, window * max(len(self.sources), 1) * 3)
        )
        by_source: dict[str, list[dict]] = {}
        for row in history:
            by_source.setdefault(row["source"], []).append(row)

        source_rows = []
        for source in self.sources:
            samples = by_source.get(source.name, [])[:window]
            statuses = Counter(row["status"] for row in samples)
            latency_values = sorted(
                row["latency_ms"] for row in samples if row.get("latency_ms", 0) > 0
            )
            scanned = sum(row.get("scanned_count", 0) for row in samples)
            fetched = sum(row.get("fetched_count", 0) for row in samples)
            kept = sum(row.get("kept_count", 0) for row in samples)
            latest_status = samples[0]["status"] if samples else None
            authorization = self.source_auth.status(source.source_id)
            health_level, explanation = self._source_health_level(
                samples,
                statuses,
                latest_status,
                authorization.state,
            )
            source_rows.append(
                {
                    "id": source.source_id,
                    "name": source.name,
                    "health_level": health_level,
                    "explanation": explanation,
                    "sample_count": len(samples),
                    "status_counts": {
                        status.value: statuses.get(status.value, 0) for status in SourceStatus
                    },
                    "healthy_rate": round(
                        statuses.get(SourceStatus.OK.value, 0) / len(samples) * 100,
                        1,
                    )
                    if samples
                    else None,
                    "completion_rate": round(
                        sum(
                            statuses.get(status.value, 0)
                            for status in (
                                SourceStatus.OK,
                                SourceStatus.PARTIAL,
                                SourceStatus.SKIPPED,
                            )
                        )
                        / len(samples)
                        * 100,
                        1,
                    )
                    if samples
                    else None,
                    "average_latency_ms": round(sum(latency_values) / len(latency_values))
                    if latency_values
                    else 0,
                    "p95_latency_ms": latency_values[round((len(latency_values) - 1) * 0.95)]
                    if latency_values
                    else 0,
                    "scanned_count": scanned,
                    "fetched_count": fetched,
                    "kept_count": kept,
                    "yield_rate": round(kept / fetched * 100, 1) if fetched else 0,
                    "latency_trend": self._metric_trend(
                        [row.get("latency_ms", 0) for row in samples if row.get("latency_ms", 0)],
                        lower_is_better=True,
                    ),
                    "yield_trend": self._metric_trend(
                        [
                            row.get("kept_count", 0) / row.get("fetched_count", 1) * 100
                            for row in samples
                            if row.get("fetched_count", 0) > 0
                        ],
                        lower_is_better=False,
                    ),
                    "history": [
                        {
                            "run_id": row["run_id"],
                            "started_at": row["started_at"],
                            "status": row["status"],
                            "scanned_count": row.get("scanned_count", 0),
                            "fetched_count": row.get("fetched_count", 0),
                            "kept_count": row.get("kept_count", 0),
                            "rejected_count": row.get("rejected_count", 0),
                            "rejection_reasons": json.loads(
                                row.get("rejection_json", "{}") or "{}"
                            ),
                            "latency_ms": row.get("latency_ms", 0),
                            "message": row.get("message", ""),
                        }
                        for row in reversed(samples)
                    ],
                }
            )

        levels = Counter(row["health_level"] for row in source_rows)
        return {
            "window": window,
            "generated_at": datetime.now(ZoneInfo(self.settings.timezone)).isoformat(),
            "summary": {
                "source_count": len(source_rows),
                "sample_count": sum(row["sample_count"] for row in source_rows),
                "healthy": levels.get("healthy", 0),
                "degraded": levels.get("degraded", 0),
                "unhealthy": levels.get("unhealthy", 0),
                "auth_required": levels.get("auth_required", 0),
                "no_data": levels.get("no_data", 0),
            },
            "sources": source_rows,
        }

    @staticmethod
    def _source_health_level(
        samples: list[dict],
        statuses: Counter,
        latest_status: str | None,
        authorization_state: str,
    ) -> tuple[str, str]:
        if not samples:
            return "no_data", "尚无真实运行样本；完成一次检索后才会计算趋势。"
        if (
            latest_status == SourceStatus.AUTH_REQUIRED.value
            and authorization_state != "authorized"
        ):
            return "auth_required", "最近运行需要用户授权；来源本身未被判定为故障。"
        failed = statuses.get(SourceStatus.FAILED.value, 0)
        latest_two_failed = len(samples) >= 2 and all(
            row["status"] == SourceStatus.FAILED.value for row in samples[:2]
        )
        if latest_two_failed or failed / len(samples) >= 0.5:
            return "unhealthy", "最近样本持续失败或失败比例过高，请查看运行历史和原站状态。"
        if (
            latest_status
            in {
                SourceStatus.PARTIAL.value,
                SourceStatus.FAILED.value,
                SourceStatus.AUTH_REQUIRED.value,
            }
            or statuses.get(SourceStatus.OK.value, 0) / len(samples) < 0.7
        ):
            return "degraded", "来源仍可贡献数据，但近期存在覆盖不完整、授权或偶发故障。"
        return "healthy", "近期运行稳定；地域跳过不会被误计为来源故障。"

    @staticmethod
    def _metric_trend(values: list[float], *, lower_is_better: bool) -> str:
        if len(values) < 4:
            return "insufficient_data"
        midpoint = len(values) // 2
        recent = values[:midpoint]
        older = values[midpoint:]
        recent_average = sum(recent) / len(recent)
        older_average = sum(older) / len(older)
        change = (recent_average - older_average) / max(abs(older_average), 1)
        if abs(change) < 0.15:
            return "stable"
        improving = change < 0 if lower_is_better else change > 0
        return "improving" if improving else "worsening"
