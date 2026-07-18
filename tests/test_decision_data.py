from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from bidpilot.api import create_app
from bidpilot.config import Settings
from bidpilot.intent import IntentParser
from bidpilot.models import (
    CompanyProfileUpdate,
    EventType,
    FeedbackUpdate,
    FeedbackVerdict,
    TenderRecord,
)
from bidpilot.service import BidPilotService


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        report_dir=tmp_path / "reports",
        database_path=tmp_path / "data" / "decision.db",
        request_interval=0.1,
        embedded_worker=False,
    )


def record(title: str = "广东政务云 AI 服务器采购公告") -> TenderRecord:
    return TenderRecord(
        canonical_id="record-001",
        project_key="project-001",
        version_hash="version-001",
        title=title,
        published_at=datetime(2026, 7, 18, 9, 0),
        region="广东",
        buyer="广东省政务服务和数据管理局",
        event_type=EventType.TENDER,
        project_id="GD-2026-001",
        summary="采购 AI 服务器并完成信创适配。",
        body_excerpt="采购 AI 服务器并完成信创适配与本地实施。",
        source_urls=["https://example.com/gd/1"],
        sources=["测试官方源"],
        relevance_score=95,
        opportunity_score=88,
        lifecycle_id="project-001",
    )


def test_company_profile_feedback_and_run_snapshot_are_durable(tmp_path: Path):
    service = BidPilotService(make_settings(tmp_path))
    profile = service.update_company_profile(
        CompanyProfileUpdate(
            company_name=" 示例数字科技 ",
            offerings=["AI 服务器", "AI 服务器", "数据中心集成"],
            strengths=["信创适配"],
            target_regions=["广东"],
            excluded_terms=["纯土建"],
            preferred_buyers=["政务数据局"],
            decision_focus="precision",
        )
    )
    assert profile.company_name == "示例数字科技"
    assert profile.offerings == ["AI 服务器", "数据中心集成"]
    assert profile.version != "empty"
    assert BidPilotService(make_settings(tmp_path)).get_company_profile().version == profile.version

    original = record()
    service.db.upsert_records([original.model_dump(mode="json")])
    spec = IntentParser(service.settings.timezone).parse("最近1个月广东服务器招标信息")
    service.db.create_run("run-001", spec)
    service.db.set_run_items("run-001", [original.model_dump(mode="json")])

    changed = original.model_copy(update={"title": "后来更新但版本键相同的全局标题"})
    service.db.upsert_records([changed.model_dump(mode="json")])
    restored = service.get_run_evidence("run-001")
    assert [item.title for item in restored] == [original.title]

    saved = service.set_feedback(
        original.canonical_id,
        original.version_hash,
        FeedbackUpdate(verdict=FeedbackVerdict.RELEVANT, reason="符合信创交付能力"),
    )
    assert saved.verdict == FeedbackVerdict.RELEVANT
    updated = service.set_feedback(
        original.canonical_id,
        original.version_hash,
        FeedbackUpdate(verdict=FeedbackVerdict.CONTACTED, reason="已由华南团队联系"),
    )
    assert updated.created_at == saved.created_at
    assert len(service.list_feedback()) == 1
    service.delete_feedback(original.canonical_id, original.version_hash)
    assert service.list_feedback() == []


def test_profile_and_feedback_api_are_token_guarded_and_graphical(tmp_path: Path):
    app = create_app(make_settings(tmp_path))
    service = app.state.service
    item = record()
    service.db.upsert_records([item.model_dump(mode="json")])

    with TestClient(app) as client:
        empty = client.get("/api/v1/company-profile")
        assert empty.status_code == 200
        assert empty.json()["version"] == "empty"

        payload = {
            "company_name": "示例科技",
            "offerings": ["AI服务器"],
            "strengths": ["信创适配"],
            "target_regions": ["广东"],
            "excluded_terms": [],
            "preferred_buyers": ["高校"],
            "decision_focus": "balanced",
        }
        assert client.put("/api/v1/company-profile", json=payload).status_code == 403
        token = client.post("/api/v1/config/edit-token").json()["edit_token"]
        headers = {"X-BidPilot-Config-Token": token}
        saved = client.put("/api/v1/company-profile", json=payload, headers=headers)
        assert saved.status_code == 200
        assert saved.json()["offerings"] == ["AI服务器"]

        feedback_path = f"/api/v1/feedback/{item.canonical_id}/{item.version_hash}"
        assert (
            client.put(feedback_path, json={"verdict": "relevant", "reason": "适合"}).status_code
            == 403
        )
        feedback = client.put(
            feedback_path,
            json={"verdict": "relevant", "reason": "适合华南交付"},
            headers=headers,
        )
        assert feedback.status_code == 200
        assert feedback.json()["record"]["title"] == item.title
        assert len(client.get("/api/v1/feedback").json()) == 1

        updated = client.put(
            feedback_path,
            json={"verdict": "watch", "reason": "等待预算确认"},
            headers=headers,
        )
        assert updated.status_code == 200
        assert updated.json()["verdict"] == "watch"
        assert len(client.get("/api/v1/feedback").json()) == 1

        deleted = client.delete(feedback_path, headers=headers)
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True}
        assert client.delete(feedback_path, headers=headers).status_code == 404

        missing = client.put(
            "/api/v1/feedback/missing/version",
            json={"verdict": "irrelevant", "reason": ""},
            headers=headers,
        )
        assert missing.status_code == 404


def test_clear_feedback_is_explicit_and_does_not_delete_tenders(tmp_path: Path):
    service = BidPilotService(make_settings(tmp_path))
    item = record()
    service.db.upsert_records([item.model_dump(mode="json")])
    service.set_feedback(
        item.canonical_id,
        item.version_hash,
        FeedbackUpdate(verdict=FeedbackVerdict.IRRELEVANT, reason="产品线不覆盖"),
    )

    assert service.clear_feedback() == 1
    assert service.clear_feedback() == 0
    assert service.db.get_tender_item(item.canonical_id, item.version_hash) is not None
