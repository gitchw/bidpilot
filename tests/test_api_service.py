import asyncio
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest
from docx import Document
from fastapi.testclient import TestClient

from bidpilot.api import create_app
from bidpilot.config import Settings
from bidpilot.delivery import DeliveryReceipt
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
from bidpilot.scheduler import SubscriptionWorker, next_schedule_time, retry_time
from bidpilot.service import BidPilotService, SubscriptionBusyError
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


def test_retry_time_uses_capped_positive_jitter():
    now = datetime(2026, 7, 24, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert retry_time(now, 0, random_fraction=0) == now + timedelta(seconds=60)
    assert retry_time(now, 0, random_fraction=1) == now + timedelta(seconds=66)
    assert retry_time(now, 99, random_fraction=1) == now + timedelta(seconds=10860)


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


def test_confirmed_intent_snapshot_prevents_second_llm_parse_and_rejects_drift(tmp_path: Path):
    app = create_app(make_settings(tmp_path), sources=[FakeSource()])
    immediate_query = "最近1个月安徽服务器招标信息"
    scheduled_query = "最近1个月安徽服务器招标信息，请每天9:00发送给我"
    updated_query = "最近2周深圳服务器中标公告，请每周一8:30发送给我"
    with TestClient(app) as client:
        immediate = client.post("/api/v1/intent/parse", json={"query": immediate_query})
        scheduled = client.post("/api/v1/intent/parse", json={"query": scheduled_query})
        updated = client.post("/api/v1/intent/parse", json={"query": updated_query})
        assert immediate.status_code == 200
        assert scheduled.status_code == 200
        assert updated.status_code == 200
        immediate_token = immediate.json()["confirmation_snapshot"]
        scheduled_token = scheduled.json()["confirmation_snapshot"]
        updated_token = updated.json()["confirmation_snapshot"]

        app.state.service.intent_engine.resolve = AsyncMock(
            side_effect=AssertionError("confirmed actions must not parse with the LLM again")
        )
        run = client.post(
            "/api/v1/runs",
            json={
                "query": immediate_query,
                "intent_snapshot": immediate_token,
                "delivery_targets": ["local"],
            },
        )
        assert run.status_code == 200, run.text
        assert run.json()["spec"]["region"] == "安徽"

        subscription = client.post(
            "/api/v1/subscriptions",
            json={
                "name": "安徽服务器日报",
                "query": scheduled_query,
                "intent_snapshot": scheduled_token,
                "delivery_targets": ["local"],
                "run_immediately": False,
            },
        )
        assert subscription.status_code == 200, subscription.text
        assert subscription.json()["spec"]["region"] == "安徽"
        assert subscription.json()["spec"]["confirmation_snapshot"] is None

        updated_subscription = client.patch(
            f"/api/v1/subscriptions/{subscription.json()['id']}",
            json={"query": updated_query, "intent_snapshot": updated_token},
        )
        assert updated_subscription.status_code == 200, updated_subscription.text
        assert updated_subscription.json()["spec"]["region"] == "深圳"
        assert updated_subscription.json()["spec"]["schedule"]["kind"] == "weekly"
        assert updated_subscription.json()["spec"]["confirmation_snapshot"] is None

        empty_update_snapshot = client.patch(
            f"/api/v1/subscriptions/{subscription.json()['id']}",
            json={"query": updated_query, "intent_snapshot": ""},
        )
        assert empty_update_snapshot.status_code == 409
        assert "格式无效" in empty_update_snapshot.json()["detail"]

        drifted_update_snapshot = client.patch(
            f"/api/v1/subscriptions/{subscription.json()['id']}",
            json={
                "query": "最近2周广州服务器中标公告，请每周一8:30发送给我",
                "intent_snapshot": updated_token,
            },
        )
        assert drifted_update_snapshot.status_code == 409
        assert "问题内容已改变" in drifted_update_snapshot.json()["detail"]

        orphan_update_snapshot = client.patch(
            f"/api/v1/subscriptions/{subscription.json()['id']}",
            json={"intent_snapshot": updated_token},
        )
        assert orphan_update_snapshot.status_code == 422
        assert "必须与新自然语言规则同时提交" in orphan_update_snapshot.text

        empty_run_snapshot = client.post(
            "/api/v1/runs",
            json={
                "query": immediate_query,
                "intent_snapshot": "",
                "delivery_targets": ["local"],
            },
        )
        assert empty_run_snapshot.status_code == 409
        assert "格式无效" in empty_run_snapshot.json()["detail"]

        empty_subscription_snapshot = client.post(
            "/api/v1/subscriptions",
            json={
                "name": "空快照不得降级重解析",
                "query": scheduled_query,
                "intent_snapshot": "",
                "delivery_targets": ["local"],
                "run_immediately": False,
            },
        )
        assert empty_subscription_snapshot.status_code == 409
        assert "格式无效" in empty_subscription_snapshot.json()["detail"]

        changed_subscription = client.post(
            "/api/v1/subscriptions",
            json={
                "name": "错误复用的订阅",
                "query": "最近1个月深圳服务器招标信息，请每天9:00发送给我",
                "intent_snapshot": scheduled_token,
                "delivery_targets": ["local"],
                "run_immediately": False,
            },
        )
        assert changed_subscription.status_code == 409
        assert "问题内容已改变" in changed_subscription.json()["detail"]

        changed = client.post(
            "/api/v1/runs",
            json={
                "query": "最近1个月深圳服务器招标信息",
                "intent_snapshot": immediate_token,
                "delivery_targets": ["local"],
            },
        )
        assert changed.status_code == 409
        assert "问题内容已改变" in changed.json()["detail"]


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
    subscription_state = restarted_service.get_subscription(subscription.id)
    assert subscription_state is not None
    assert subscription_state.last_delivery_status == "success"
    assert "全部目标均已确认" in subscription_state.last_delivery_message
    attempts = restarted_service.db.list_delivery_attempts(subscription_id=subscription.id)
    assert len(attempts) == 2
    assert all(attempt["success"] for attempt in attempts)


async def test_delivery_failure_keeps_increment_uncommitted_and_queues_target_retry(
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
    before = datetime.now(ZoneInfo("UTC"))
    result = await service.run_subscription(subscription.id, trigger_reason="schedule")

    row = service.db.get_subscription(subscription.id)
    assert result.status == RunStatus.PARTIAL
    assert result.delivery_status == "partial"
    assert row["last_status"] == "completed"
    subscription_state = service.get_subscription(subscription.id)
    assert subscription_state is not None
    assert subscription_state.last_delivery_status == "partial"
    assert "重试" in subscription_state.last_delivery_message
    assert row["consecutive_failures"] == 0
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
    assert reports == 1
    attempts = service.db.list_delivery_attempts(subscription_id=subscription.id)
    assert len(attempts) == 1
    assert attempts[0]["success"] == 0
    assert attempts[0]["outbox_status"] == "retrying"
    retry_at = datetime.fromisoformat(attempts[0]["next_attempt_at"])
    assert timedelta(seconds=45) <= retry_at - before <= timedelta(seconds=90)


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


@pytest.mark.parametrize(
    ("status", "success"),
    [(RunStatus.COMPLETED, True), (RunStatus.FAILED, False)],
)
def test_stale_worker_cannot_finish_after_replacement_lease(
    tmp_path: Path,
    status: RunStatus,
    success: bool,
):
    service = BidPilotService(make_settings(tmp_path), sources=[FakeSource()])
    subscription = service.create_subscription(
        "服务器日报",
        "最近1个月安徽服务器招标信息，请每天9:00发送给我",
        run_immediately=True,
    )
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    assert service.db.claim_due_subscription(
        worker_id="worker-a",
        now=now,
        lease_until=now + timedelta(seconds=1),
    )
    assert service.db.claim_due_subscription(
        worker_id="worker-b",
        now=now + timedelta(seconds=2),
        lease_until=now + timedelta(minutes=5),
    )

    stale_finished = service.db.finish_subscription_attempt(
        subscription.id,
        worker_id="worker-a",
        last_run_at=now + timedelta(seconds=3),
        next_run_at=now + timedelta(days=1),
        status=status,
        message="旧执行者不得覆盖",
        new_count=99,
        run_id="old-run",
        success=success,
    )

    assert stale_finished is False
    after_stale = service.db.get_subscription(subscription.id)
    assert after_stale is not None
    assert after_stale["lease_owner"] == "worker-b"
    assert after_stale["last_run_id"] is None
    assert after_stale["last_new_count"] == 0

    replacement_finished = service.db.finish_subscription_attempt(
        subscription.id,
        worker_id="worker-b",
        last_run_at=now + timedelta(seconds=4),
        next_run_at=now + timedelta(days=1),
        status=status,
        message="新执行者结果",
        new_count=1,
        run_id="new-run",
        success=success,
    )
    assert replacement_finished is True
    after_replacement = service.db.get_subscription(subscription.id)
    assert after_replacement is not None
    assert after_replacement["lease_owner"] is None
    assert after_replacement["last_run_id"] == "new-run"
    assert after_replacement["last_new_count"] == 1


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
    settings.worker_lease_seconds = 1
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


async def test_worker_stops_old_execution_when_replacement_takes_lease(
    tmp_path: Path,
    monkeypatch,
):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class LeaseLossSource(FakeSource):
        async def search(self, spec: TenderQuerySpec, fetcher) -> SourceSearchResult:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            raise AssertionError("租约丢失后旧来源调用不应继续完成")

    settings = make_settings(tmp_path)
    settings.worker_lease_seconds = 1
    service = BidPilotService(settings, sources=[LeaseLossSource()])
    subscription = service.create_subscription(
        "租约接管日报",
        "最近1个月安徽服务器招标信息，请每天9:00发送给我",
        run_immediately=True,
    )
    worker = SubscriptionWorker(service, kind="lease-loss-test")

    def replace_lease(subscription_id, *, worker_id, lease_until):
        replacement_until = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(minutes=5)
        with service.db.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE subscriptions SET lease_owner=?, lease_until=?, updated_at=?
                WHERE id=? AND lease_owner=?
                """,
                (
                    "replacement-worker",
                    replacement_until.isoformat(),
                    datetime.now(ZoneInfo("UTC")).isoformat(),
                    subscription_id,
                    worker_id,
                ),
            )
        assert cursor.rowcount == 1
        return False

    monkeypatch.setattr(service.db, "renew_subscription_lease", replace_lease)
    old_execution = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(started.wait(), timeout=3)

    assert await asyncio.wait_for(old_execution, timeout=3) is True
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    after_loss = service.db.get_subscription(subscription.id)
    assert after_loss is not None
    assert after_loss["lease_owner"] == "replacement-worker"
    with service.db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0] == 0

    replacement = BidPilotService(settings, sources=[FakeSource()])
    result = await replacement.run_subscription(
        subscription.id,
        trigger_reason="schedule",
        lease_owner="replacement-worker",
    )

    assert result.new_count == 1
    final_row = replacement.db.get_subscription(subscription.id)
    assert final_row is not None
    assert final_row["lease_owner"] is None
    assert final_row["last_run_id"] == result.run_id
    with replacement.db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0] == 1


async def test_manual_run_stops_when_its_lease_is_replaced(tmp_path: Path, monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class LeaseLossSource(FakeSource):
        async def search(self, spec: TenderQuerySpec, fetcher) -> SourceSearchResult:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            raise AssertionError("手动运行丢失租约后不应继续")

    settings = make_settings(tmp_path)
    settings.worker_lease_seconds = 1
    service = BidPilotService(settings, sources=[LeaseLossSource()])
    subscription = service.create_subscription(
        "手动租约日报",
        "最近1个月安徽服务器招标信息，请每天9:00发送给我",
        run_immediately=False,
    )

    def replace_lease(subscription_id, *, worker_id, lease_until):
        with service.db.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE subscriptions SET lease_owner=?, lease_until=?, updated_at=?
                WHERE id=? AND lease_owner=?
                """,
                (
                    "replacement-worker",
                    (datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(minutes=5)).isoformat(),
                    datetime.now(ZoneInfo("UTC")).isoformat(),
                    subscription_id,
                    worker_id,
                ),
            )
        assert cursor.rowcount == 1
        return False

    monkeypatch.setattr(service.db, "renew_subscription_lease", replace_lease)
    execution = asyncio.create_task(service.run_subscription(subscription.id))
    await asyncio.wait_for(started.wait(), timeout=3)

    with pytest.raises(SubscriptionBusyError, match="外发前停止"):
        await asyncio.wait_for(execution, timeout=3)
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    row = service.db.get_subscription(subscription.id)
    assert row is not None
    assert row["lease_owner"] == "replacement-worker"
    with service.db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0] == 0


@pytest.mark.parametrize("shutdown_mode", ["worker_stop", "ctrl_c"])
async def test_worker_shutdown_releases_cancelled_run_for_immediate_takeover(
    tmp_path: Path, shutdown_mode: str
):
    started = asyncio.Event()

    class BlockingSource(FakeSource):
        async def search(self, spec: TenderQuerySpec, fetcher) -> SourceSearchResult:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("blocking source should only finish through cancellation")

    settings = make_settings(tmp_path)
    service = BidPilotService(settings, sources=[BlockingSource()])
    subscription = service.create_subscription(
        "可接管日报",
        "最近1个月安徽服务器招标信息，请每天9:00发送给我",
        run_immediately=True,
    )
    worker = SubscriptionWorker(service, kind="shutdown-test")
    if shutdown_mode == "worker_stop":
        worker.start()
        runner = worker._task
    else:
        runner = asyncio.create_task(worker.run_forever())

    assert runner is not None
    await asyncio.wait_for(started.wait(), timeout=3)
    before = service.db.get_subscription(subscription.id)
    assert before is not None
    assert before["lease_owner"] == worker.worker_id
    with service.db.connection() as conn:
        running = conn.execute(
            "SELECT id FROM runs WHERE subscription_id=? AND status='running'",
            (subscription.id,),
        ).fetchone()
    assert running is not None

    if shutdown_mode == "worker_stop":
        await worker.stop()
    else:
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
        await worker.stop()

    released = service.db.get_subscription(subscription.id)
    assert released is not None
    assert released["lease_owner"] is None
    assert released["lease_until"] is None
    cancelled_run = service.db.get_run(running["id"])
    assert cancelled_run is not None
    assert cancelled_run["status"] == "failed"
    assert cancelled_run["completed_at"] is not None
    assert "已取消" in cancelled_run["error"]

    restarted = BidPilotService(settings, sources=[FakeSource()])
    replacement = SubscriptionWorker(restarted, kind="replacement")
    assert await replacement.run_once() is True
    recovered = restarted.db.get_subscription(subscription.id)
    assert recovered is not None
    assert recovered["lease_owner"] is None
    assert recovered["last_status"] == "completed"


async def test_direct_subscription_cancellation_fails_run_and_releases_owned_lease(
    tmp_path: Path,
):
    started = asyncio.Event()

    class BlockingSource(FakeSource):
        async def search(self, spec: TenderQuerySpec, fetcher) -> SourceSearchResult:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("blocking source should only finish through cancellation")

    settings = make_settings(tmp_path)
    service = BidPilotService(settings, sources=[BlockingSource()])
    subscription = service.create_subscription(
        "手动取消日报",
        "最近1个月安徽服务器招标信息，请每天9:00发送给我",
        run_immediately=True,
    )
    task = asyncio.create_task(service.run_subscription(subscription.id))
    await asyncio.wait_for(started.wait(), timeout=3)

    leased = service.db.get_subscription(subscription.id)
    assert leased is not None
    assert leased["lease_owner"].startswith("manual:")
    with service.db.connection() as conn:
        running = conn.execute(
            "SELECT id FROM runs WHERE subscription_id=? AND status='running'",
            (subscription.id,),
        ).fetchone()
    assert running is not None

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    released = service.db.get_subscription(subscription.id)
    assert released is not None
    assert released["lease_owner"] is None
    assert released["lease_until"] is None
    assert released["last_run_id"] == running["id"]
    assert released["last_status"] == "failed"
    cancelled_run = service.db.get_run(running["id"])
    assert cancelled_run is not None
    assert cancelled_run["status"] == "failed"
    assert cancelled_run["completed_at"] is not None
    assert "已取消" in cancelled_run["error"]

    replacement = BidPilotService(settings, sources=[FakeSource()])
    assert await SubscriptionWorker(replacement, kind="replacement").run_once() is True


async def test_subscription_cancellation_does_not_release_replacement_lease(tmp_path: Path):
    started = asyncio.Event()

    class BlockingSource(FakeSource):
        async def search(self, spec: TenderQuerySpec, fetcher) -> SourceSearchResult:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("blocking source should only finish through cancellation")

    service = BidPilotService(make_settings(tmp_path), sources=[BlockingSource()])
    subscription = service.create_subscription(
        "租约栅栏日报",
        "最近1个月安徽服务器招标信息，请每天9:00发送给我",
        run_immediately=True,
    )
    task = asyncio.create_task(service.run_subscription(subscription.id))
    await asyncio.wait_for(started.wait(), timeout=3)
    with service.db.connection() as conn:
        running = conn.execute(
            "SELECT id FROM runs WHERE subscription_id=? AND status='running'",
            (subscription.id,),
        ).fetchone()
        conn.execute(
            "UPDATE subscriptions SET lease_owner=?, lease_until=? WHERE id=?",
            (
                "replacement-worker",
                (datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(minutes=5)).isoformat(),
                subscription.id,
            ),
        )
    assert running is not None

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    fenced = service.db.get_subscription(subscription.id)
    assert fenced is not None
    assert fenced["lease_owner"] == "replacement-worker"
    assert fenced["lease_until"] is not None
    cancelled_run = service.db.get_run(running["id"])
    assert cancelled_run is not None
    assert cancelled_run["status"] == "failed"
    assert "已取消" in cancelled_run["error"]


async def test_manual_run_renews_lease_during_long_running_subscription(tmp_path: Path):
    started = asyncio.Event()

    class SlowSource(FakeSource):
        async def search(self, spec: TenderQuerySpec, fetcher) -> SourceSearchResult:
            started.set()
            await asyncio.sleep(0.65)
            return await super().search(spec, fetcher)

    settings = make_settings(tmp_path)
    settings.worker_lease_seconds = 1
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
            next_action="联系采购人并核验报名材料",
            next_action_at=next_action,
            notes="联系采购人并核验资质要求",
            tags=["重点", "服务器", "重点"],
            is_read=True,
        ),
    )
    assert updated.stage == OpportunityStage.FOLLOWING
    assert updated.owner == "王同学"
    assert updated.next_action == "联系采购人并核验报名材料"
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
    assert persisted.next_action == "联系采购人并核验报名材料"
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
        api_updated = client.patch(
            f"/api/v1/opportunities/{opportunity.id}",
            json={
                "next_action": "提交资质清单并由项目负责人复核",
                "next_action_at": "2026-07-21T17:00:00+08:00",
            },
        )
        assert api_updated.status_code == 200
        assert api_updated.json()["next_action"] == "提交资质清单并由项目负责人复核"
        assert api_updated.json()["next_action_at"] == "2026-07-21T17:00:00+08:00"
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
        assert recreated.json()["next_action"] == ""
        assert recreated.json()["notes"] == ""
        assert recreated.json()["tags"] == []
        already_deleted = client.delete(f"/api/v1/opportunities/{opportunity.id}")
        assert already_deleted.status_code == 404
        openapi = client.get("/openapi.json").json()
        update_contract = openapi["paths"]["/api/v1/opportunities/{opportunity_id}"]["patch"]
        assert "`next_action`" in update_contract["description"]
        next_action_schema = openapi["components"]["schemas"]["OpportunityUpdate"]["properties"][
            "next_action"
        ]
        assert {item.get("maxLength") for item in next_action_schema["anyOf"]} == {None, 500}
        delete_contract = openapi["paths"]["/api/v1/opportunities/{opportunity_id}"]["delete"]
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
            next_action="核验更正内容并更新报价清单",
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
    assert refreshed.next_action == "核验更正内容并更新报价清单"
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
                "telegram_bot_token": "123456:CONFIG_SECRET",
                "telegram_chat_id": "-1001234567890",
                "telegram_message_thread_id": 42,
                "telegram_disable_notification": True,
                "telegram_protect_content": True,
                "slack_webhook_url": (
                    "https://hooks.slack.com/services/T00000000/B00000000/CONFIGSECRET"
                ),
            },
        )
        assert saved.status_code == 200
        assert saved.json()["ai"]["ready"] is True
        assert saved.json()["ai"]["llm_api_key"] == {"configured": True}
        assert saved.json()["telegram"]["bot_token"] == {"configured": True}
        assert saved.json()["telegram"]["ready"] is True
        assert saved.json()["slack"]["webhook_url"] == {"configured": True}
        assert saved.json()["slack"]["ready"] is True
        assert "CONFIG_SECRET" not in saved.text
        assert "CONFIGSECRET" not in saved.text
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
    assert persisted.telegram.bot_token.configured is True
    assert persisted.telegram.chat_id == "-1001234567890"
    assert persisted.telegram.message_thread_id == 42
    assert persisted.telegram.disable_notification is True
    assert persisted.telegram.protect_content is True
    assert persisted.slack.webhook_url.configured is True
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
                "clear_secrets": ["llm_api_key", "telegram_bot_token", "slack_webhook_url"],
            },
        )
        assert cleared.json()["ai"]["llm_api_key"] == {"configured": False}
        assert cleared.json()["telegram"]["bot_token"] == {"configured": False}
        assert cleared.json()["telegram"]["ready"] is False
        assert cleared.json()["slack"]["webhook_url"] == {"configured": False}
        assert cleared.json()["slack"]["ready"] is False


def test_telegram_and_slack_connection_test_routes_use_saved_channels(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path)
    settings.telegram_bot_token = "123456:TEST_TOKEN"
    settings.telegram_chat_id = "-100123"
    settings.slack_webhook_url = "https://hooks.slack.com/services/T000/B000/TESTSECRET"
    app = create_app(settings, sources=[FakeSource()])
    calls: list[str] = []

    async def deliver(path, channel, **kwargs):
        calls.append(channel)
        return DeliveryReceipt(channel, True, f"{channel} 测试成功", external_id="test:1")

    monkeypatch.setattr(app.state.service.delivery, "deliver", deliver)
    with TestClient(app) as client:
        token = client.post("/api/v1/config/edit-token").json()["edit_token"]
        headers = {"X-BidPilot-Config-Token": token}
        for channel in ("telegram_bot", "slack_webhook"):
            response = client.post(
                f"/api/v1/config/channels/{channel}/test",
                headers=headers,
            )
            assert response.status_code == 200
            assert response.json()["target"] == channel
            assert response.json()["success"] is True
    assert calls == ["telegram_bot", "slack_webhook"]


def test_runtime_config_reload_observes_reset_from_another_process(tmp_path: Path):
    web_service = BidPilotService(make_settings(tmp_path), sources=[FakeSource()])
    worker_service = BidPilotService(make_settings(tmp_path), sources=[FakeSource()])
    assert worker_service.settings.telegram_chat_id == ""

    saved = web_service.runtime_config.update(
        RuntimeConfigUpdate(
            revision=web_service.runtime_config.snapshot().revision,
            telegram_chat_id="-100123",
        )
    )
    worker_service.runtime_config.load_persisted()
    assert worker_service.settings.telegram_chat_id == "-100123"

    web_service.runtime_config.update(
        RuntimeConfigUpdate(
            revision=saved.revision,
            reset_fields=["telegram_chat_id"],
        )
    )
    worker_service.runtime_config.load_persisted()
    assert worker_service.settings.telegram_chat_id == ""


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


async def test_compose_trusted_bridge_can_perform_browser_mutations(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.network_access_mode = "lan"
    settings.lan_access_policy = "trusted_lan"
    settings.lan_trusted_networks = "auto"
    app = create_app(settings, sources=[FakeSource()])
    transport = httpx.ASGITransport(app=app, client=("172.18.0.1", 43123))

    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        response = await client.post("/api/v1/config/edit-token")

    assert response.status_code == 200
    assert response.json()["edit_token"]


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

    assert len(operations) == 52
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


def test_every_runtime_config_field_has_detailed_chinese_description(tmp_path: Path):
    schema = create_app(make_settings(tmp_path), sources=[FakeSource()]).openapi()
    properties = schema["components"]["schemas"]["RuntimeConfigUpdate"]["properties"]

    assert len(properties) == len(RuntimeConfigUpdate.model_fields)
    for name, field in properties.items():
        description = field.get("description", "")
        assert description, f"RuntimeConfigUpdate.{name} 缺少说明"
        assert any("\u4e00" <= char <= "\u9fff" for char in description), (
            f"RuntimeConfigUpdate.{name} 缺少中文解释"
        )


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
