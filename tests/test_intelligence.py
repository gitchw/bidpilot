import json
from datetime import datetime
from pathlib import Path

from bidpilot.config import Settings
from bidpilot.intelligence import IntelligenceBriefGenerator
from bidpilot.models import (
    BriefAction,
    BriefClaim,
    BriefPriority,
    EventType,
    EvidenceSpan,
    IntelligenceBrief,
    TenderRecord,
)
from bidpilot.service import BidPilotService


def tender_record() -> TenderRecord:
    return TenderRecord(
        canonical_id="notice-1",
        project_key="project-1",
        version_hash="version-1",
        title="安徽大学 GPU 服务器采购公开招标公告",
        published_at=datetime(2026, 7, 10, 9, 0),
        region="安徽",
        buyer="安徽大学",
        event_type=EventType.TENDER,
        project_id="AH-2026-001",
        summary="安徽大学采购 GPU 服务器，预算金额 1200 万元。",
        body_excerpt="项目编号 AH-2026-001，预算金额 1200 万元。",
        evidence=[
            EvidenceSpan(
                text="项目编号 AH-2026-001，预算金额 1200 万元。",
                source_url="https://example.com/notices/1",
            )
        ],
        source_urls=["https://example.com/notices/1"],
        sources=["测试来源"],
        relevance_score=92,
        opportunity_score=88,
        lifecycle_id="project-1",
    )


async def test_intelligence_brief_hydrates_identity_and_url_from_local_evidence(sample_spec):
    settings = Settings(
        llm_base_url="https://model.example/v1",
        llm_model="test-model",
        intelligence_brief_mode="auto",
    )
    generator = IntelligenceBriefGenerator(settings)
    response = {
        "overview": "采购需求集中在 GPU 服务器。",
        "buyer_needs": [{"text": "安徽大学正在采购 GPU 服务器。", "evidence_ids": ["E01"]}],
        "priorities": [
            {
                "evidence_id": "E01",
                "reason": "仍处于公开招标阶段。",
                "recommended_action": "核验资格条件和截止时间。",
            }
        ],
        "risks": [],
        "actions": [
            {
                "priority": "P0",
                "text": "打开原文核验后进入投标准备。",
                "evidence_ids": ["E01"],
            }
        ],
    }

    async def fake_request(_payload):
        return json.dumps(response, ensure_ascii=False)

    generator._request = fake_request
    brief = await generator.generate(sample_spec, [tender_record()])

    assert brief.status == "applied"
    assert brief.mode == "llm_grounded"
    assert brief.priorities[0].title == "安徽大学 GPU 服务器采购公开招标公告"
    assert brief.priorities[0].buyer == "安徽大学"
    assert brief.priorities[0].source_url == "https://example.com/notices/1"


async def test_intelligence_brief_rejects_unknown_evidence_id_as_a_whole(sample_spec):
    settings = Settings(
        llm_base_url="https://model.example/v1",
        llm_model="test-model",
        intelligence_brief_mode="auto",
    )
    generator = IntelligenceBriefGenerator(settings)

    async def fake_request(_payload):
        return json.dumps(
            {
                "overview": "模型尝试引用不存在的公告。",
                "buyer_needs": [],
                "priorities": [
                    {
                        "evidence_id": "E99",
                        "reason": "不存在的证据。",
                        "recommended_action": "不应采用。",
                    }
                ],
                "risks": [],
                "actions": [{"priority": "P0", "text": "不应采用。", "evidence_ids": ["E99"]}],
            },
            ensure_ascii=False,
        )

    generator._request = fake_request
    brief = await generator.generate(sample_spec, [tender_record()])

    assert brief.status == "invalid_response"
    assert brief.mode == "deterministic"
    assert brief.priorities[0].evidence_id == "E01"
    assert brief.priorities[0].source_url == "https://example.com/notices/1"


async def test_intelligence_brief_repairs_one_rejected_model_response(sample_spec):
    settings = Settings(
        llm_base_url="https://model.example/v1",
        llm_model="test-model",
        intelligence_brief_mode="auto",
    )
    generator = IntelligenceBriefGenerator(settings)
    calls = 0

    async def fake_request(payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert "correction" not in json.loads(payload["messages"][1]["content"])
            evidence_id = "E99"
        else:
            assert "correction" in json.loads(payload["messages"][1]["content"])
            evidence_id = "E01"
        return json.dumps(
            {
                "overview": "采购需求集中在 GPU 服务器。",
                "buyer_needs": [],
                "priorities": [
                    {
                        "evidence_id": evidence_id,
                        "reason": "仍处于公开招标阶段。",
                        "recommended_action": "核验资格条件和截止时间。",
                    }
                ],
                "risks": [],
                "actions": [
                    {
                        "priority": "P0",
                        "text": "打开原文核验。",
                        "evidence_ids": [evidence_id],
                    }
                ],
            },
            ensure_ascii=False,
        )

    generator._request = fake_request
    brief = await generator.generate(sample_spec, [tender_record()])

    assert brief.status == "applied"
    assert brief.repair_count == 1
    assert calls == 2


async def test_intelligence_brief_rejects_numbers_borrowed_from_another_evidence(sample_spec):
    settings = Settings(
        llm_base_url="https://model.example/v1",
        llm_model="test-model",
        intelligence_brief_mode="auto",
    )
    generator = IntelligenceBriefGenerator(settings)
    first = tender_record()
    first.opportunity_score = 99
    first.summary = "项目甲预算金额 100 万元。"
    first.body_excerpt = "项目甲预算金额 100 万元。"
    first.evidence[0].text = "项目甲预算金额 100 万元。"
    second = tender_record().model_copy(deep=True)
    second.canonical_id = "notice-2"
    second.project_key = "project-2"
    second.version_hash = "version-2"
    second.title = "安徽大学项目乙服务器采购公告"
    second.opportunity_score = 80
    second.summary = "项目乙预算金额 200 万元。"
    second.body_excerpt = "项目乙预算金额 200 万元。"
    second.evidence[0].text = "项目乙预算金额 200 万元。"
    second.source_urls = ["https://example.com/notices/2"]

    async def fake_request(_payload):
        return json.dumps(
            {
                "overview": "两条服务器采购机会需要分别核验。",
                "buyer_needs": [],
                "priorities": [
                    {
                        "evidence_id": "E01",
                        "reason": "项目甲预算金额 200 万元。",
                        "recommended_action": "核验资格条件。",
                    }
                ],
                "risks": [],
                "actions": [
                    {"priority": "P0", "text": "打开项目甲原文。", "evidence_ids": ["E01"]}
                ],
            },
            ensure_ascii=False,
        )

    generator._request = fake_request
    brief = await generator.generate(sample_spec, [first, second])

    assert brief.status == "invalid_response"
    assert brief.mode == "deterministic"


async def test_intelligence_brief_rejects_substring_and_action_number_hallucinations(
    sample_spec,
):
    settings = Settings(
        llm_base_url="https://model.example/v1",
        llm_model="test-model",
        intelligence_brief_mode="auto",
    )
    generator = IntelligenceBriefGenerator(settings)

    async def fake_request(_payload):
        return json.dumps(
            {
                "overview": "GPU 服务器采购机会需要核验。",
                "buyer_needs": [],
                "priorities": [
                    {
                        "evidence_id": "E01",
                        "reason": "预算金额 12 万元。",
                        "recommended_action": "投入 999 人天完成投标。",
                    }
                ],
                "risks": [],
                "actions": [
                    {
                        "priority": "P0",
                        "text": "准备 888 亿元资金。",
                        "evidence_ids": ["E01"],
                    }
                ],
            },
            ensure_ascii=False,
        )

    generator._request = fake_request
    brief = await generator.generate(sample_spec, [tender_record()])

    assert brief.status == "invalid_response"
    assert brief.mode == "deterministic"


async def test_service_reuses_only_grounded_intelligence_cache(sample_spec, tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        report_dir=tmp_path / "reports",
        database_path=tmp_path / "data" / "test.db",
        qianlima_cookie_path=tmp_path / "data" / "secrets" / "qianlima_cookie.txt",
        llm_base_url="https://model.example/v1",
        llm_model="test-model",
        intelligence_brief_mode="auto",
    )
    service = BidPilotService(settings, sources=[])
    record = tender_record()
    evidence = service.intelligence._catalog([record])[0]
    calls = 0

    async def fake_generate(_spec, _records):
        nonlocal calls
        calls += 1
        return IntelligenceBrief(
            mode="llm_grounded",
            status="applied",
            overview="采购需求集中在 GPU 服务器。",
            buyer_needs=[BriefClaim(text="安徽大学正在采购。", evidence_ids=["E01"])],
            priorities=[
                BriefPriority(
                    evidence_id="E01",
                    title=evidence.title,
                    buyer=evidence.buyer,
                    published_at=evidence.published_at,
                    event_type=evidence.event_type,
                    opportunity_score=evidence.opportunity_score,
                    reason="仍处于公开招标阶段。",
                    recommended_action="核验资格条件和截止时间。",
                    source_url=evidence.source_url,
                )
            ],
            actions=[
                BriefAction(
                    priority="P0",
                    text="打开原文核验。",
                    evidence_ids=["E01"],
                )
            ],
            evidence_catalog=[evidence],
            generated_at=datetime(2026, 7, 18, 12, 0),
            summary="测试简报",
        )

    service.intelligence.generate = fake_generate
    first = await service._build_intelligence_brief(sample_spec, [record])
    second = await service._build_intelligence_brief(sample_spec, [record])

    assert first.status == "applied"
    assert second.status == "cached"
    assert second.cache_hit is True
    assert calls == 1
