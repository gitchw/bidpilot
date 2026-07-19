import asyncio
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
from docx import Document
from fastapi.testclient import TestClient

from bidpilot.api import create_app
from bidpilot.config import Settings
from bidpilot.models import (
    EventType,
    EvidenceSpan,
    FeedbackUpdate,
    FeedbackVerdict,
    OpportunityCreate,
    OpportunityStage,
    OpportunityUpdate,
    RawTender,
    RunStatus,
    SourceSearchResult,
    SourceStatus,
    SubscriptionUpdate,
    TenderQuerySpec,
)
from bidpilot.runtime_config import RuntimeConfigUpdate
from bidpilot.scheduler import SubscriptionWorker, next_schedule_time
from bidpilot.service import BidPilotService, RunExecutionError, SubscriptionBusyError
from bidpilot.sources.base import SourceAdapter


class FakeSource(SourceAdapter):
    name = "可验证测试源"

    async def search(self, spec: TenderQuerySpec, fetcher) -> SourceSearchResult:
        body = "项目编号：AH-2026-001。预算金额：1200万元。采购20台GPU服务器。"
        item = RawTender(
            source=self.name,
            source_url="https://example.com/tender/1",
            title="安徽大学 GPU 服务器采购公开招标公告",
            published_at=datetime(2026, 7, 10, 9, 0),
            region="安徽",
            buyer="安徽大学",
            body=body,
            event_type=EventType.TENDER,
            project_id="AH-2026-001",
            evidence=[EvidenceSpan(text=body, source_url="https://example.com/tender/1")],
        )
        return SourceSearchResult(
            source=self.name,
            status=SourceStatus.OK,
            items=[item],
            message="fixture",
            latency_ms=5,
        )


class SequencedLifecycleSource(SourceAdapter):
    name = "生命周期测试源"

    def __init__(self):
        self.calls = 0

    async def search(self, spec: TenderQuerySpec, fetcher) -> SourceSearchResult:
        self.calls += 1
        changed = self.calls > 1
        body = "项目编号：AH-2026-009。采购 GPU 服务器，资格条件以招标文件为准。"
        item = RawTender(
            source=self.name,
            source_url=(
                "https://example.com/tender/9-change" if changed else "https://example.com/tender/9"
            ),
            title=(
                "安徽大学 GPU 服务器采购更正公告" if changed else "安徽大学 GPU 服务器采购招标公告"
            ),
            published_at=datetime(2026, 7, 10, 9, 0),
            region="安徽",
            buyer="安徽大学",
            body=body,
            event_type=EventType.CHANGE if changed else EventType.TENDER,
            project_id="AH-2026-009",
            evidence=[
                EvidenceSpan(
                    text=body,
                    source_url=(
                        "https://example.com/tender/9-change"
                        if changed
                        else "https://example.com/tender/9"
                    ),
                )
            ],
        )
        return SourceSearchResult(
            source=self.name,
            status=SourceStatus.OK,
            items=[item],
            message="fixture",
            latency_ms=5,
        )


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        control_dir=tmp_path / "control",
        report_dir=tmp_path / "reports",
        database_path=tmp_path / "data" / "test.db",
        request_interval=0.1,
    )


def test_api_query_to_docx_flow(tmp_path: Path):
    app = create_app(make_settings(tmp_path), sources=[FakeSource()])
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        parsed = client.post("/api/v1/intent/parse", json={"query": "最近1个月安徽服务器招标信息"})
        assert parsed.status_code == 200
        assert parsed.json()["topic"] == "服务器"
        assert parsed.json()["resolution"]["llm_status"] == "not_needed"
        compared = client.post(
            "/api/v1/intent/compare",
            json={"query": "最近1个月安徽服务器招标信息"},
        )
        assert compared.status_code == 200
        assert compared.json()["changed_fields"] == []

        response = client.post(
            "/api/v1/runs",
            json={"query": "最近1个月安徽服务器招标信息", "delivery_channel": "local"},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["new_count"] == 1
        assert result["records"][0]["project_id"] == "AH-2026-001"
        assert result["intelligence_brief"]["status"] == "not_configured"
        assert result["intelligence_brief"]["priorities"][0]["evidence_id"] == "E01"
        assert result["opportunity_assessments"]["status"] == "profile_missing"
        assert result["opportunity_assessments"]["assessments"][0]["evidence_id"] == "E01"
        evidence = client.get(f"/api/v1/runs/{result['run_id']}/evidence")
        assert evidence.status_code == 200
        assert [item["canonical_id"] for item in evidence.json()] == [
            result["records"][0]["canonical_id"]
        ]
        edit_token = client.post("/api/v1/config/edit-token").json()["edit_token"]
        profile = client.put(
            "/api/v1/company-profile",
            headers={"X-BidPilot-Config-Token": edit_token},
            json={"offerings": ["GPU 服务器"], "target_regions": ["安徽"]},
        )
        assert profile.status_code == 200
        refreshed = client.post(f"/api/v1/runs/{result['run_id']}/assessments")
        assert refreshed.json()["status"] == "not_configured"
        assert (
            client.get(f"/api/v1/runs/{result['run_id']}").json()["opportunity_assessments"][
                "status"
            ]
            == "not_configured"
        )
        report_name = Path(result["report_path"]).name
        download = client.get(f"/api/v1/reports/{report_name}")
        assert download.status_code == 200
        assert download.content.startswith(b"PK")
        report = Document(BytesIO(download.content))
        assert "情报副驾驶" in "\n".join(item.text for item in report.paragraphs)
        assert client.get("/api/v1/reports").json()[0]["item_count"] == 1
        health = client.get("/api/v1/sources/health?window=20")
        assert health.status_code == 200
        assert health.json()["summary"]["source_count"] == 1
        assert health.json()["sources"][0]["sample_count"] == 1
        assert health.json()["sources"][0]["health_level"] == "healthy"


def test_source_health_tracks_partial_skipped_and_failed_runs(tmp_path: Path, sample_spec):
    service = BidPilotService(make_settings(tmp_path), sources=[FakeSource()])
    statuses = [SourceStatus.OK, SourceStatus.SKIPPED, SourceStatus.PARTIAL, SourceStatus.FAILED]
    for index, status in enumerate(statuses):
        run_id = f"health-{index}"
        service.db.create_run(run_id, sample_spec)
        service.db.add_source_run(
            run_id,
            {
                "source": FakeSource.name,
                "status": status.value,
                "scanned_count": 10 + index,
                "fetched_count": 5,
                "kept_count": 2,
                "rejected_count": 5 + index,
                "rejection_reasons": {"keyword_mismatch": 5 + index},
                "latency_ms": 100 * (index + 1),
                "message": status.value,
            },
        )
        service.db.complete_run(
            run_id,
            RunStatus.COMPLETED,
            report_path=None,
            result_count=2,
            new_count=2,
            diagnostics=[],
        )

    health = service.source_health(window=20)["sources"][0]

    assert health["sample_count"] == 4
    assert health["status_counts"] == {
        "ok": 1,
        "partial": 1,
        "auth_required": 0,
        "failed": 1,
        "skipped": 1,
    }
    assert health["health_level"] == "degraded"
    assert health["completion_rate"] == 75.0
    assert health["healthy_rate"] == 25.0
    assert health["average_latency_ms"] == 250
    assert health["history"][-1]["status"] == "failed"


async def test_subscription_second_run_is_zero_increment(tmp_path: Path):
    service = BidPilotService(make_settings(tmp_path), sources=[FakeSource()])
    subscription = service.create_subscription(
        "每日服务器简报",
        "最近1个月安徽服务器招标信息，请每天9:00发送给我",
    )
    first = await service.run_subscription(subscription.id)
    second = await service.run_subscription(subscription.id)
    assert first.new_count == 1
    assert first.report_path is not None
    assert second.new_count == 0
    assert second.report_path is None


def test_subscription_creation_is_idempotent(tmp_path: Path):
    service = BidPilotService(make_settings(tmp_path), sources=[FakeSource()])
    query = "最近1个月安徽服务器招标信息，请每天9:00发送给我"
    first = service.create_subscription("每日服务器简报", query)
    second = service.create_subscription("重复点击不应新建", query)
    assert first.id == second.id
    assert len(service.list_subscriptions()) == 1


async def test_recreating_existing_subscription_requeues_a_fresh_run(tmp_path: Path):
    service = BidPilotService(make_settings(tmp_path), sources=[FakeSource()])
    query = "最近1个月安徽服务器招标信息，请每天9:00发送给我"
    first = service.create_subscription("每日服务器简报", query)
    await service.run_subscription(first.id)
    completed = service.get_subscription(first.id)
    assert completed.next_run_at > datetime.now(ZoneInfo("Asia/Shanghai"))

    existing = service.create_subscription("重复点击", query, run_immediately=True)
    assert existing.id == first.id
    assert existing.enabled is True
    assert existing.next_run_at <= datetime.now(ZoneInfo("Asia/Shanghai"))


def test_subscription_requires_schedule(tmp_path: Path):
    service = BidPilotService(make_settings(tmp_path), sources=[FakeSource()])
    try:
        service.create_subscription("无效", "最近1个月安徽服务器招标信息")
    except ValueError as exc:
        assert "必须包含" in str(exc)
    else:
        raise AssertionError("Expected schedule validation")


def test_schedule_calculation_is_strictly_future():
    timezone = ZoneInfo("Asia/Shanghai")
    from bidpilot.intent import IntentParser

    daily = IntentParser().parse(
        "最近1个月安徽服务器招标信息，每天9:00发送",
        now=datetime(2026, 7, 17, 10, 0, tzinfo=timezone),
    )
    assert next_schedule_time(
        daily.schedule, datetime(2026, 7, 17, 10, 0, tzinfo=timezone)
    ) == datetime(2026, 7, 18, 9, 0, tzinfo=timezone)

    weekly = IntentParser().parse(
        "最近1个月安徽服务器招标信息，每周一8:30发送",
        now=datetime(2026, 7, 17, 10, 0, tzinfo=timezone),
    )
    next_week = next_schedule_time(weekly.schedule, datetime(2026, 7, 20, 8, 30, tzinfo=timezone))
    assert next_week == datetime(2026, 7, 27, 8, 30, tzinfo=timezone)

    monthly = IntentParser().parse(
        "最近1个月安徽服务器招标信息，每月31日8:30发送",
        now=datetime(2026, 2, 15, 10, 0, tzinfo=timezone),
    )
    assert next_schedule_time(
        monthly.schedule, datetime(2026, 2, 15, 10, 0, tzinfo=timezone)
    ) == datetime(2026, 2, 28, 8, 30, tzinfo=timezone)
    assert next_schedule_time(
        monthly.schedule, datetime(2026, 2, 28, 8, 30, tzinfo=timezone)
    ) == datetime(2026, 3, 31, 8, 30, tzinfo=timezone)


def test_subscription_management_api(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.embedded_worker = False
    app = create_app(settings, sources=[FakeSource()])
    query = "最近1个月安徽服务器招标信息，请每天9:00发送给我"
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/subscriptions",
            json={
                "name": "服务器日报",
                "query": query,
                "delivery_channel": "local",
                "delivery_policy": "always",
                "run_immediately": False,
            },
        )
        assert created.status_code == 200
        subscription = created.json()
        subscription_id = subscription["id"]
        assert subscription["next_run_at"] is not None

        paused = client.post(f"/api/v1/subscriptions/{subscription_id}/pause")
        assert paused.json()["enabled"] is False
        assert paused.json()["next_run_at"] is None

        resumed = client.post(
            f"/api/v1/subscriptions/{subscription_id}/resume",
            json={"run_immediately": False},
        )
        assert resumed.json()["enabled"] is True
        assert resumed.json()["next_run_at"] is not None

        updated = client.patch(
            f"/api/v1/subscriptions/{subscription_id}",
            json={
                "name": "重点服务器周报",
                "query": "近2周安徽服务器招标信息，每周一8:30发送",
                "delivery_policy": "on_change",
            },
        )
        updated_row = updated.json()
        assert updated_row["name"] == "重点服务器周报"
        assert updated_row["delivery_policy"] == "on_change"
        assert updated_row["spec"]["schedule"]["kind"] == "weekly"
        assert updated_row["spec"]["schedule"]["send_time"] == "08:30:00"
        assert updated_row["next_run_at"] is not None

        deleted = client.delete(f"/api/v1/subscriptions/{subscription_id}")
        assert deleted.json() == {"deleted": True}
        assert client.get(f"/api/v1/subscriptions/{subscription_id}").status_code == 404


def test_subscription_update_cannot_overwrite_a_new_worker_lease(tmp_path: Path, monkeypatch):
    service = BidPilotService(make_settings(tmp_path), sources=[FakeSource()])
    subscription = service.create_subscription(
        "服务器日报",
        "最近1个月安徽服务器招标信息，请每天9:00发送给我",
        run_immediately=False,
    )
    original_update = service.db.update_subscription

    def claim_then_update(subscription_id, **changes):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        assert service.db.claim_subscription(
            subscription_id,
            worker_id="worker-race",
            now=now,
            lease_until=now + timedelta(minutes=5),
        )
        return original_update(subscription_id, **changes)

    monkeypatch.setattr(service.db, "update_subscription", claim_then_update)
    try:
        service.update_subscription(
            subscription.id,
            SubscriptionUpdate(name="不应覆盖运行中的任务"),
        )
    except SubscriptionBusyError as exc:
        assert "正在执行" in str(exc)
    else:
        raise AssertionError("Expected the new worker lease to block the concurrent update")

    row = service.db.get_subscription(subscription.id)
    assert row is not None
    assert row["name"] == "服务器日报"
    assert row["lease_owner"] == "worker-race"


def test_subscription_delete_cannot_remove_a_new_worker_lease(tmp_path: Path, monkeypatch):
    service = BidPilotService(make_settings(tmp_path), sources=[FakeSource()])
    subscription = service.create_subscription(
        "服务器日报",
        "最近1个月安徽服务器招标信息，请每天9:00发送给我",
        run_immediately=False,
    )
    original_delete = service.db.delete_subscription

    def claim_then_delete(subscription_id, **conditions):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        assert service.db.claim_subscription(
            subscription_id,
            worker_id="worker-race",
            now=now,
            lease_until=now + timedelta(minutes=5),
        )
        return original_delete(subscription_id, **conditions)

    monkeypatch.setattr(service.db, "delete_subscription", claim_then_delete)
    try:
        service.delete_subscription(subscription.id)
    except SubscriptionBusyError as exc:
        assert "正在执行" in str(exc)
    else:
        raise AssertionError("Expected the new worker lease to block the concurrent delete")

    row = service.db.get_subscription(subscription.id)
    assert row is not None
    assert row["lease_owner"] == "worker-race"


async def test_durable_worker_continues_after_service_restart(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.embedded_worker = False
    query = "最近1个月安徽服务器招标信息，请每天9:00发送给我"
    first_service = BidPilotService(settings, sources=[FakeSource()])
    subscription = first_service.create_subscription("服务器日报", query, run_immediately=True)
    first_worker = SubscriptionWorker(first_service, kind="test")
    assert await first_worker.run_once() is True
    first_row = first_service.db.get_subscription(subscription.id)
    assert first_row["last_status"] == "completed"
    assert first_row["last_new_count"] == 1
    assert datetime.fromisoformat(first_row["next_run_at"]) > datetime.now(
        ZoneInfo("Asia/Shanghai")
    )

    # A new process/service instance reads the same durable state and can continue.
    due = datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(seconds=1)
    first_service.db.set_subscription_due(subscription.id, due)
    restarted_service = BidPilotService(settings, sources=[FakeSource()])
    restarted_worker = SubscriptionWorker(restarted_service, kind="test-restart")
    assert await restarted_worker.run_once() is True
    second_row = restarted_service.db.get_subscription(subscription.id)
    assert second_row["last_status"] == "completed"
    assert second_row["last_new_count"] == 0
    attempts = restarted_service.db.list_delivery_attempts(subscription_id=subscription.id)
    assert len(attempts) == 2
    assert all(attempt["success"] for attempt in attempts)


async def test_delivery_failure_keeps_increment_uncommitted_and_schedules_retry(
    tmp_path: Path, monkeypatch
):
    service = BidPilotService(make_settings(tmp_path), sources=[FakeSource()])
    subscription = service.create_subscription(
        "服务器日报",
        "最近1个月安徽服务器招标信息，请每天9:00发送给我",
        run_immediately=True,
    )

    async def fail_delivery(*args, **kwargs):
        raise RuntimeError("模拟投递服务不可用")

    monkeypatch.setattr(service.delivery, "deliver", fail_delivery)
    before = datetime.now(ZoneInfo("Asia/Shanghai"))
    try:
        await service.run_subscription(subscription.id, trigger_reason="schedule")
    except RunExecutionError as exc:
        assert "模拟投递服务不可用" in str(exc)
    else:
        raise AssertionError("Expected delivery failure")

    row = service.db.get_subscription(subscription.id)
    assert row["last_status"] == "failed"
    assert row["consecutive_failures"] == 1
    retry_at = datetime.fromisoformat(row["next_run_at"])
    assert timedelta(seconds=45) <= retry_at - before <= timedelta(seconds=90)
    with service.db.connection() as conn:
        delivered = conn.execute(
            "SELECT COUNT(*) FROM delivery_ledger WHERE subscription_id=?",
            (subscription.id,),
        ).fetchone()[0]
        reports = conn.execute(
            "SELECT COUNT(*) FROM reports WHERE subscription_id=?",
            (subscription.id,),
        ).fetchone()[0]
    assert delivered == 0
    assert reports == 0
    attempts = service.db.list_delivery_attempts(subscription_id=subscription.id)
    assert len(attempts) == 1
    assert attempts[0]["success"] == 0


def test_subscription_lease_blocks_duplicate_claim_and_recovers_after_expiry(tmp_path: Path):
    service = BidPilotService(make_settings(tmp_path), sources=[FakeSource()])
    subscription = service.create_subscription(
        "服务器日报",
        "最近1个月安徽服务器招标信息，请每天9:00发送给我",
        run_immediately=True,
    )
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    first = service.db.claim_due_subscription(
        worker_id="worker-a",
        now=now,
        lease_until=now + timedelta(minutes=5),
    )
    assert first and first["id"] == subscription.id
    assert (
        service.db.claim_due_subscription(
            worker_id="worker-b",
            now=now + timedelta(minutes=1),
            lease_until=now + timedelta(minutes=6),
        )
        is None
    )
    recovered = service.db.claim_due_subscription(
        worker_id="worker-b",
        now=now + timedelta(minutes=5, seconds=1),
        lease_until=now + timedelta(minutes=10),
    )
    assert recovered and recovered["id"] == subscription.id


async def test_manual_run_is_rejected_while_worker_owns_subscription(tmp_path: Path):
    service = BidPilotService(make_settings(tmp_path), sources=[FakeSource()])
    subscription = service.create_subscription(
        "服务器日报",
        "最近1个月安徽服务器招标信息，请每天9:00发送给我",
        run_immediately=True,
    )
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    claimed = service.db.claim_due_subscription(
        worker_id="worker-a",
        now=now,
        lease_until=now + timedelta(minutes=5),
    )
    assert claimed is not None
    assert service.get_subscription(subscription.id).in_progress is True
    try:
        await service.run_subscription(subscription.id)
    except SubscriptionBusyError as exc:
        assert "正在执行" in str(exc)
    else:
        raise AssertionError("Expected an active lease to block a duplicate manual run")
    for action in (service.pause_subscription, service.delete_subscription):
        try:
            action(subscription.id)
        except SubscriptionBusyError as exc:
            assert "正在执行" in str(exc)
        else:
            raise AssertionError("Expected an active lease to block destructive management")


async def test_worker_renews_lease_during_long_running_subscription(tmp_path: Path):
    started = asyncio.Event()

    class SlowSource(FakeSource):
        async def search(self, spec: TenderQuerySpec, fetcher) -> SourceSearchResult:
            started.set()
            await asyncio.sleep(0.65)
            return await super().search(spec, fetcher)

    settings = make_settings(tmp_path)
    # Production validation enforces >=30s. A short interval keeps this concurrency test fast.
    settings.worker_lease_seconds = 0.3
    service = BidPilotService(settings, sources=[SlowSource()])
    service.create_subscription(
        "服务器日报",
        "最近1个月安徽服务器招标信息，请每天9:00发送给我",
        run_immediately=True,
    )
    worker = SubscriptionWorker(service, kind="lease-test")
    task = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(started.wait(), timeout=3)
    await asyncio.sleep(0.42)

    other_service = BidPilotService(settings, sources=[FakeSource()])
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    duplicate = other_service.db.claim_due_subscription(
        worker_id="competing-worker",
        now=now,
        lease_until=now + timedelta(seconds=10),
    )
    assert duplicate is None
    assert await task is True


async def test_manual_run_renews_lease_during_long_running_subscription(tmp_path: Path):
    started = asyncio.Event()

    class SlowSource(FakeSource):
        async def search(self, spec: TenderQuerySpec, fetcher) -> SourceSearchResult:
            started.set()
            await asyncio.sleep(0.65)
            return await super().search(spec, fetcher)

    settings = make_settings(tmp_path)
    settings.worker_lease_seconds = 0.3
    service = BidPilotService(settings, sources=[SlowSource()])
    subscription = service.create_subscription(
        "服务器日报",
        "最近1个月安徽服务器招标信息，请每天9:00发送给我",
        run_immediately=True,
    )
    task = asyncio.create_task(service.run_subscription(subscription.id))
    await asyncio.wait_for(started.wait(), timeout=3)
    await asyncio.sleep(0.42)

    other_service = BidPilotService(settings, sources=[FakeSource()])
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    duplicate = other_service.db.claim_due_subscription(
        worker_id="competing-worker",
        now=now,
        lease_until=now + timedelta(seconds=10),
    )
    assert duplicate is None
    result = await task
    assert result.new_count == 1


async def test_opportunity_workspace_is_project_idempotent_and_persistent(tmp_path: Path):
    settings = make_settings(tmp_path)
    service = BidPilotService(settings, sources=[FakeSource()])
    run = await service.run_query("最近1个月安徽服务器招标信息")
    tender = run.records[0]
    award = tender.model_copy(
        update={
            "canonical_id": f"{tender.canonical_id}-award",
            "version_hash": "award-version",
            "title": "安徽大学 GPU 服务器采购中标公告",
            "event_type": EventType.AWARD,
            "published_at": tender.published_at + timedelta(days=1),
            "source_urls": ["https://example.com/tender/award"],
        }
    )
    service.db.upsert_records([award.model_dump(mode="json")])

    opportunity = service.create_opportunity(
        OpportunityCreate(
            canonical_id=tender.canonical_id,
            version_hash=tender.version_hash,
        )
    )
    assert opportunity.record.event_type == EventType.AWARD
    duplicate = service.create_opportunity(
        OpportunityCreate(
            canonical_id=award.canonical_id,
            version_hash=award.version_hash,
        )
    )
    assert duplicate.id == opportunity.id
    assert len(service.list_opportunities()) == 1

    next_action = datetime(2026, 7, 20, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    updated = service.update_opportunity(
        opportunity.id,
        OpportunityUpdate(
            stage=OpportunityStage.FOLLOWING,
            owner="王同学",
            next_action_at=next_action,
            notes="联系采购人并核验资质要求",
            tags=["重点", "服务器", "重点"],
            is_read=True,
        ),
    )
    assert updated.stage == OpportunityStage.FOLLOWING
    assert updated.owner == "王同学"
    assert updated.tags == ["重点", "服务器"]
    assert updated.next_action_at == next_action
    assert service.list_opportunities(search="采购人")[0]["id"] == opportunity.id

    timeline = service.opportunity_timeline(opportunity.id)
    assert [item["event_type"] for item in timeline] == ["招标公告", "中标公告"]
    assert timeline[1]["source_urls"] == ["https://example.com/tender/award"]

    restarted = BidPilotService(settings, sources=[FakeSource()])
    persisted = restarted.get_opportunity(opportunity.id)
    assert persisted is not None
    assert persisted.owner == "王同学"
    assert persisted.notes == "联系采购人并核验资质要求"
    service.set_feedback(
        tender.canonical_id,
        tender.version_hash,
        FeedbackUpdate(verdict=FeedbackVerdict.RELEVANT, reason="删除卡片不应删除反馈"),
    )

    app = create_app(settings, sources=[FakeSource()])
    with TestClient(app) as client:
        invalid = client.patch(
            f"/api/v1/opportunities/{opportunity.id}",
            json={"stage": "not-a-stage"},
        )
        assert invalid.status_code == 422
        missing = client.post(
            "/api/v1/opportunities",
            json={"canonical_id": "forged", "version_hash": "forged"},
        )
        assert missing.status_code == 404
        assert missing.json()["detail"] == "只能收藏系统已抓取并验证过的标讯记录"

        reports_before = [row["id"] for row in app.state.service.db.list_reports()]
        run_items_before = app.state.service.db.list_run_items(run.run_id)
        feedback_before = app.state.service.db.get_feedback(
            tender.canonical_id,
            tender.version_hash,
        )
        assert reports_before
        assert run_items_before
        assert feedback_before is not None
        deleted = client.delete(f"/api/v1/opportunities/{opportunity.id}")
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True}
        assert client.get(f"/api/v1/opportunities/{opportunity.id}").status_code == 404
        assert client.get(f"/api/v1/opportunities/{opportunity.id}/timeline").status_code == 404
        assert (
            app.state.service.db.get_tender_item(tender.canonical_id, tender.version_hash)
            is not None
        )
        assert (
            app.state.service.db.get_tender_item(award.canonical_id, award.version_hash) is not None
        )
        assert app.state.service.db.get_run(run.run_id) is not None
        assert [row["id"] for row in app.state.service.db.list_reports()] == reports_before
        assert app.state.service.db.list_run_items(run.run_id) == run_items_before
        assert (
            app.state.service.db.get_feedback(tender.canonical_id, tender.version_hash)
            == feedback_before
        )

        recreated = client.post(
            "/api/v1/opportunities",
            json={
                "canonical_id": tender.canonical_id,
                "version_hash": tender.version_hash,
            },
        )
        assert recreated.status_code == 200
        assert recreated.json()["id"] != opportunity.id
        assert recreated.json()["record"]["event_type"] == "中标公告"
        assert recreated.json()["stage"] == "new"
        assert recreated.json()["owner"] == ""
        assert recreated.json()["notes"] == ""
        assert recreated.json()["tags"] == []
        already_deleted = client.delete(f"/api/v1/opportunities/{opportunity.id}")
        assert already_deleted.status_code == 404
        delete_contract = client.get("/openapi.json").json()["paths"][
            "/api/v1/opportunities/{opportunity_id}"
        ]["delete"]
        assert delete_contract["summary"] == "从机会工作台删除卡片"
        assert delete_contract["responses"]["200"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/DeleteResultResponse"
        }
        assert delete_contract["tags"] == ["机会工作台"]
        for section in (
            "### 用途",
            "### 参数与请求体",
            "### 返回值",
            "### 副作用",
            "### 常见错误",
            "### 示例",
        ):
            assert section in delete_contract["description"]
        assert "不会删除 tender_items" in delete_contract["description"]


async def test_opportunity_patch_returns_404_if_card_is_deleted_during_update(
    tmp_path: Path, monkeypatch
):
    settings = make_settings(tmp_path)
    seed_service = BidPilotService(settings, sources=[FakeSource()])
    run = await seed_service.run_query("最近1个月安徽服务器招标信息")
    tender = run.records[0]
    opportunity = seed_service.create_opportunity(
        OpportunityCreate(
            canonical_id=tender.canonical_id,
            version_hash=tender.version_hash,
        )
    )

    app = create_app(settings, sources=[FakeSource()])
    with TestClient(app) as client:
        real_update = app.state.service.db.update_opportunity

        def delete_before_update(opportunity_id, changes):
            app.state.service.db.delete_opportunity(opportunity_id)
            return real_update(opportunity_id, changes)

        monkeypatch.setattr(
            app.state.service.db,
            "update_opportunity",
            delete_before_update,
        )
        response = client.patch(
            f"/api/v1/opportunities/{opportunity.id}",
            json={"owner": "并发测试"},
        )

    assert response.status_code == 404
    assert opportunity.id in response.json()["detail"]


async def test_opportunity_patch_returns_404_if_card_is_deleted_after_update(
    tmp_path: Path, monkeypatch
):
    settings = make_settings(tmp_path)
    seed_service = BidPilotService(settings, sources=[FakeSource()])
    run = await seed_service.run_query("最近1个月安徽服务器招标信息")
    tender = run.records[0]
    opportunity = seed_service.create_opportunity(
        OpportunityCreate(
            canonical_id=tender.canonical_id,
            version_hash=tender.version_hash,
        )
    )

    app = create_app(settings, sources=[FakeSource()])
    with TestClient(app) as client:
        real_update = app.state.service.db.update_opportunity

        def update_before_delete(opportunity_id, changes):
            updated = real_update(opportunity_id, changes)
            assert updated
            assert app.state.service.db.delete_opportunity(opportunity_id)
            return updated

        monkeypatch.setattr(
            app.state.service.db,
            "update_opportunity",
            update_before_delete,
        )
        response = client.patch(
            f"/api/v1/opportunities/{opportunity.id}",
            json={"owner": "并发测试"},
        )

    assert response.status_code == 404
    assert opportunity.id in response.json()["detail"]


async def test_new_lifecycle_event_refreshes_opportunity_without_losing_follow_up(tmp_path: Path):
    source = SequencedLifecycleSource()
    service = BidPilotService(make_settings(tmp_path), sources=[source])
    first_run = await service.run_query("最近1个月安徽服务器招标信息")
    tender = first_run.records[0]
    opportunity = service.create_opportunity(
        OpportunityCreate(
            canonical_id=tender.canonical_id,
            version_hash=tender.version_hash,
        )
    )
    service.update_opportunity(
        opportunity.id,
        OpportunityUpdate(
            stage=OpportunityStage.FOLLOWING,
            owner="项目负责人",
            notes="已核验初始招标公告",
            is_read=True,
        ),
    )

    await service.run_query("最近1个月安徽服务器招标信息")

    refreshed = service.get_opportunity(opportunity.id)
    assert refreshed is not None
    assert refreshed.record.event_type == EventType.CHANGE
    assert refreshed.record.title == "安徽大学 GPU 服务器采购更正公告"
    assert refreshed.is_read is False
    assert refreshed.stage == OpportunityStage.FOLLOWING
    assert refreshed.owner == "项目负责人"
    assert refreshed.notes == "已核验初始招标公告"
    assert [item["event_type"] for item in service.opportunity_timeline(opportunity.id)] == [
        "招标公告",
        "更正公告",
    ]


def test_runtime_config_is_masked_token_guarded_and_persistent(tmp_path: Path):
    settings = make_settings(tmp_path)
    secret_value = "test-key-that-must-never-be-returned"
    app = create_app(settings, sources=[FakeSource()])
    with TestClient(app) as client:
        initial = client.get("/api/v1/config")
        assert initial.status_code == 200
        assert initial.json()["ai"]["llm_api_key"] == {"configured": False}

        denied = client.put(
            "/api/v1/config",
            json={
                "revision": initial.json()["revision"],
                "llm_base_url": "http://127.0.0.1:8045/v1",
            },
        )
        assert denied.status_code == 403

        token_response = client.post("/api/v1/config/edit-token")
        assert token_response.status_code == 200
        token = token_response.json()["edit_token"]
        headers = {"X-BidPilot-Config-Token": token}
        saved = client.put(
            "/api/v1/config",
            headers=headers,
            json={
                "revision": initial.json()["revision"],
                "llm_base_url": "http://127.0.0.1:8045/v1",
                "llm_model": "compatible-test-model",
                "llm_api_key": secret_value,
                "intent_llm_mode": "always",
                "intent_llm_confidence_threshold": 0.91,
                "intelligence_brief_mode": "off",
                "intelligence_brief_max_records": 9,
                "decision_assessment_mode": "off",
                "decision_assessment_max_records": 11,
                "smtp_port": 587,
                "smtp_security": "starttls",
            },
        )
        assert saved.status_code == 200
        assert saved.json()["ai"]["ready"] is True
        assert saved.json()["ai"]["llm_api_key"] == {"configured": True}
        assert secret_value not in saved.text

        preserved = client.put(
            "/api/v1/config",
            headers=headers,
            json={"revision": saved.json()["revision"], "llm_api_key": ""},
        )
        assert preserved.json()["ai"]["llm_api_key"] == {"configured": True}

        rejected = client.put(
            "/api/v1/config",
            headers=headers,
            json={"revision": preserved.json()["revision"], "unknown_setting": "unsafe"},
        )
        assert rejected.status_code == 422

    restarted = BidPilotService(make_settings(tmp_path), sources=[FakeSource()])
    persisted = restarted.runtime_config.snapshot()
    assert persisted.ai.llm_base_url == "http://127.0.0.1:8045/v1"
    assert persisted.ai.llm_model == "compatible-test-model"
    assert persisted.ai.llm_api_key.configured is True
    assert persisted.ai.intent_llm_mode == "always"
    assert persisted.ai.intent_llm_confidence_threshold == 0.91
    assert persisted.ai.intelligence_brief_mode == "off"
    assert persisted.ai.intelligence_brief_max_records == 9
    assert persisted.ai.decision_assessment_mode == "off"
    assert persisted.ai.decision_assessment_max_records == 11
    assert restarted.settings.smtp_port == 587
    with restarted.db.connection() as conn:
        stored_secret = conn.execute(
            "SELECT value FROM runtime_config WHERE field='llm_api_key'"
        ).fetchone()["value"]
    assert secret_value not in stored_secret
    assert stored_secret.startswith("fernet:v1:")
    assert (settings.data_dir / "secrets" / "runtime_config.key").exists()

    app = create_app(make_settings(tmp_path), sources=[FakeSource()])
    with TestClient(app) as client:
        token = client.post("/api/v1/config/edit-token").json()["edit_token"]
        cleared = client.put(
            "/api/v1/config",
            headers={"X-BidPilot-Config-Token": token},
            json={
                "revision": client.get("/api/v1/config").json()["revision"],
                "clear_secrets": ["llm_api_key"],
            },
        )
        assert cleared.json()["ai"]["llm_api_key"] == {"configured": False}


def test_runtime_config_revision_metadata_and_reset_are_atomic(tmp_path: Path):
    settings = make_settings(tmp_path)
    app = create_app(settings, sources=[FakeSource()])
    with TestClient(app) as client:
        initial = client.get("/api/v1/config").json()
        token = client.post("/api/v1/config/edit-token").json()["edit_token"]
        headers = {"X-BidPilot-Config-Token": token}
        saved = client.put(
            "/api/v1/config",
            headers=headers,
            json={
                "revision": initial["revision"],
                "request_timeout": 44,
                "request_interval": 1.2,
                "max_results_per_source": 37,
                "ccgp_max_pages": 4,
                "retrieval_semantic_candidate_limit": 17,
                "record_summary_mode": "off",
                "record_summary_max_records": 6,
                "record_summary_concurrency": 2,
                "record_summary_max_chars": 3600,
                "worker_poll_interval": 4.5,
                "worker_lease_seconds": 600,
                "worker_heartbeat_ttl": 45,
            },
        )
        assert saved.status_code == 200
        body = saved.json()
        assert body["revision"] == initial["revision"] + 1
        assert body["retrieval"] == {
            "request_timeout": 44.0,
            "request_interval": 1.2,
            "max_results_per_source": 37,
            "ccgp_max_pages": 4,
        }
        assert body["ai"]["retrieval_semantic_candidate_limit"] == 17
        assert body["ai"]["record_summary_mode"] == "off"
        assert body["worker"]["lease_seconds"] == 600
        assert body["field_metadata"]["request_timeout"]["source"] == "web"
        assert body["field_metadata"]["request_timeout"]["updated_at"]

        stale = client.put(
            "/api/v1/config",
            headers=headers,
            json={"revision": initial["revision"], "request_timeout": 55},
        )
        assert stale.status_code == 409
        assert "revision" in stale.json()["detail"]
        assert client.get("/api/v1/config").json()["retrieval"]["request_timeout"] == 44

        reset = client.put(
            "/api/v1/config",
            headers=headers,
            json={
                "revision": body["revision"],
                "reset_fields": ["request_timeout"],
            },
        )
        assert reset.status_code == 200
        assert reset.json()["retrieval"]["request_timeout"] == 20
        assert reset.json()["field_metadata"]["request_timeout"]["source"] == "default"
        assert reset.json()["field_metadata"]["request_timeout"]["updated_at"] is None

        unknown_reset = client.put(
            "/api/v1/config",
            headers=headers,
            json={
                "revision": reset.json()["revision"],
                "reset_fields": ["database_path"],
            },
        )
        assert unknown_reset.status_code == 422


def test_lan_mode_requires_strong_secret_and_marks_restart_fields(tmp_path: Path):
    app = create_app(make_settings(tmp_path), sources=[FakeSource()])
    with TestClient(app) as client:
        initial = client.get("/api/v1/config").json()
        token = client.post("/api/v1/config/edit-token").json()["edit_token"]
        headers = {"X-BidPilot-Config-Token": token}
        weak = client.put(
            "/api/v1/config",
            headers=headers,
            json={
                "revision": initial["revision"],
                "network_access_mode": "lan",
                "lan_admin_token": "short",
            },
        )
        assert weak.status_code == 422

        public_network = client.put(
            "/api/v1/config",
            headers=headers,
            json={
                "revision": initial["revision"],
                "network_access_mode": "lan",
                "lan_access_policy": "trusted_lan",
                "lan_trusted_networks": "0.0.0.0/0",
            },
        )
        assert public_network.status_code == 422
        assert "可信网段" in public_network.json()["detail"]

        enabled = client.put(
            "/api/v1/config",
            headers=headers,
            json={
                "revision": initial["revision"],
                "network_access_mode": "lan",
                "lan_admin_token": "lan-admin-token-at-least-16",
                "port": 8000,
            },
        )
        assert enabled.status_code == 200
        body = enabled.json()
        assert body["network"]["access_mode"] == "lan"
        assert body["network"]["access_policy"] == "admin_token"
        assert body["network"]["bind_host_after_restart"] == "0.0.0.0"
        assert body["network"]["effective_access_mode"] == "local"
        assert body["network"]["effective_bind_host"] == "127.0.0.1"
        assert body["network"]["pending_restart"] is True
        assert body["network"]["lan_admin_token"] == {"configured": True}
        assert body["field_metadata"]["network_access_mode"]["restart_required"] is True
        assert "lan-admin-token-at-least-16" not in enabled.text

        impossible_clear = client.put(
            "/api/v1/config",
            headers=headers,
            json={
                "revision": body["revision"],
                "clear_secrets": ["lan_admin_token"],
            },
        )
        assert impossible_clear.status_code == 422

        trusted_without_token = client.put(
            "/api/v1/config",
            headers=headers,
            json={
                "revision": body["revision"],
                "lan_access_policy": "trusted_lan",
                "clear_secrets": ["lan_admin_token"],
            },
        )
        assert trusted_without_token.status_code == 200
        assert trusted_without_token.json()["network"]["lan_admin_token"] == {"configured": False}


async def test_remote_lan_writes_require_admin_token(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.network_access_mode = "lan"
    settings.lan_admin_token = "remote-admin-token-12345"
    app = create_app(settings, sources=[FakeSource()])
    transport = httpx.ASGITransport(app=app, client=("192.168.1.20", 43123))
    async with httpx.AsyncClient(transport=transport, base_url="http://bidpilot.lan") as client:
        assert (await client.get("/api/v1/config")).status_code == 200
        denied = await client.post("/api/v1/config/edit-token")
        assert denied.status_code == 403
        assert "管理员令牌" in denied.json()["detail"]
        wrong = await client.post(
            "/api/v1/config/edit-token",
            headers={"X-BidPilot-Admin-Token": "wrong"},
        )
        assert wrong.status_code == 403
        allowed = await client.post(
            "/api/v1/config/edit-token",
            headers={"X-BidPilot-Admin-Token": settings.lan_admin_token},
        )
        assert allowed.status_code == 200
        assert allowed.json()["edit_token"]


async def test_trusted_lan_can_manage_source_login_without_admin_token(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.network_access_mode = "lan"
    settings.lan_access_policy = "trusted_lan"
    settings.lan_trusted_networks = "auto"
    app = create_app(settings, sources=[FakeSource()])
    now = datetime.now()
    app.state.service.source_auth.start = AsyncMock(
        return_value={
            "session_id": "remote-visible-browser-session",
            "source_id": "cecbid",
            "source_name": "中国招标投标网",
            "status": "authorizing",
            "started_at": now,
            "expires_at": now + timedelta(minutes=15),
            "login_url": "https://www.cecbid.org.cn/login",
            "message": "登录窗口已在服务主机打开",
        }
    )
    transport = httpx.ASGITransport(app=app, client=("192.168.10.25", 43123))
    async with httpx.AsyncClient(transport=transport, base_url="http://bidpilot.lan") as client:
        token_response = await client.post("/api/v1/config/edit-token")
        assert token_response.status_code == 200
        started = await client.post(
            "/api/v1/sources/cecbid/auth/start",
            headers={"X-BidPilot-Config-Token": token_response.json()["edit_token"]},
        )
        assert started.status_code == 200
        assert started.json()["session_id"] == "remote-visible-browser-session"
        assert "服务主机" in started.json()["message"]

    public_transport = httpx.ASGITransport(app=app, client=("8.8.8.8", 43123))
    async with httpx.AsyncClient(
        transport=public_transport,
        base_url="http://bidpilot.example",
    ) as client:
        denied = await client.post(
            "/api/v1/config/edit-token",
            headers={"X-Forwarded-For": "192.168.10.25"},
        )
        assert denied.status_code == 403
        assert "可信局域网" in denied.json()["detail"]


async def test_network_policy_changes_only_after_service_restart(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.network_access_mode = "lan"
    settings.lan_access_policy = "admin_token"
    settings.lan_admin_token = "old-admin-token-at-least-16"
    app = create_app(settings, sources=[FakeSource()])
    transport = httpx.ASGITransport(app=app, client=("192.168.1.20", 43123))
    admin_headers = {"X-BidPilot-Admin-Token": settings.lan_admin_token}
    async with httpx.AsyncClient(transport=transport, base_url="http://bidpilot.lan") as client:
        edit = await client.post("/api/v1/config/edit-token", headers=admin_headers)
        current = (await client.get("/api/v1/config")).json()
        saved = await client.put(
            "/api/v1/config",
            headers={
                **admin_headers,
                "X-BidPilot-Config-Token": edit.json()["edit_token"],
            },
            json={
                "revision": current["revision"],
                "lan_access_policy": "trusted_lan",
            },
        )
        assert saved.status_code == 200
        assert saved.json()["network"]["access_policy"] == "trusted_lan"
        assert saved.json()["network"]["effective_access_policy"] == "admin_token"
        assert saved.json()["network"]["pending_restart"] is True
        assert (await client.post("/api/v1/config/edit-token")).status_code == 403
        assert (
            await client.post("/api/v1/config/edit-token", headers=admin_headers)
        ).status_code == 200

    restarted_settings = make_settings(tmp_path)
    restarted_settings.network_access_mode = "lan"
    restarted = create_app(restarted_settings, sources=[FakeSource()])
    restarted_transport = httpx.ASGITransport(
        app=restarted,
        client=("192.168.1.20", 43123),
    )
    async with httpx.AsyncClient(
        transport=restarted_transport,
        base_url="http://bidpilot.lan",
    ) as client:
        assert (await client.post("/api/v1/config/edit-token")).status_code == 200
        body = (await client.get("/api/v1/config")).json()
        assert body["network"]["effective_access_policy"] == "trusted_lan"
        assert body["network"]["pending_restart"] is False


def test_every_openapi_operation_has_detailed_chinese_usage_contract(tmp_path: Path):
    schema = create_app(make_settings(tmp_path), sources=[FakeSource()]).openapi()
    api_reference = Path("docs/API_REFERENCE.md").read_text(encoding="utf-8")
    required_sections = (
        "### 用途",
        "### 参数与请求体",
        "### 返回值",
        "### 副作用",
        "### 常见错误",
        "### 示例",
    )
    operations = []
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            operations.append((method.upper(), path, operation))

    assert len(operations) == 50
    for method, path, operation in operations:
        description = operation.get("description", "")
        documented_operation = f"`{method} {path}`"
        assert documented_operation in api_reference, (
            f"{method} {path} 未写入独立 API 参考，或请求方法与路径不匹配"
        )
        heading = f"### {documented_operation}"
        section_start = api_reference.index(heading)
        section_boundaries = [
            boundary
            for marker in ("\n### `", "\n## ")
            if (boundary := api_reference.find(marker, section_start + len(heading))) >= 0
        ]
        section_end = min(section_boundaries, default=len(api_reference))
        reference_section = api_reference[section_start:section_end]
        for label in ("- 用途：", "- 返回：", "- 副作用：", "- 错误：", "- 示例："):
            assert label in reference_section, f"{method} {path} 的 API 参考缺少 {label}"
        assert any(
            label in reference_section
            for label in ("- 参数：", "- 请求：", "- 路径参数：", "- 请求头：")
        ), f"{method} {path} 的 API 参考缺少参数或请求体说明"
        assert operation.get("summary"), f"{method} {path} 缺少中文摘要"
        assert operation.get("tags"), f"{method} {path} 缺少中文分组"
        for section in required_sections:
            assert section in description, f"{method} {path} 缺少 {section}"


async def test_standalone_worker_reloads_web_runtime_config_before_run(tmp_path: Path):
    web_service = BidPilotService(make_settings(tmp_path), sources=[FakeSource()])
    worker_service = BidPilotService(make_settings(tmp_path), sources=[FakeSource()])
    assert worker_service.settings.delivery_webhook_timeout == 20

    web_service.runtime_config.update(
        RuntimeConfigUpdate(
            revision=web_service.runtime_config.snapshot().revision,
            delivery_webhook_timeout=47,
        )
    )
    await worker_service.run_query("最近1个月安徽服务器招标信息")

    assert worker_service.settings.delivery_webhook_timeout == 47
