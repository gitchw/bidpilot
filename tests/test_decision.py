import json
from datetime import datetime
from pathlib import Path

from bidpilot.config import Settings
from bidpilot.decision import OpportunityFitAssessor
from bidpilot.intent import IntentParser
from bidpilot.models import (
    CompanyProfile,
    CompanyProfileUpdate,
    EventType,
    EvidenceSpan,
    FeedbackUpdate,
    FeedbackVerdict,
    TenderFeedback,
    TenderRecord,
)
from bidpilot.service import BidPilotService


def make_settings(tmp_path: Path, **updates) -> Settings:
    values = {
        "data_dir": tmp_path / "data",
        "report_dir": tmp_path / "reports",
        "database_path": tmp_path / "data" / "decision-engine.db",
        "llm_base_url": "http://model.test/v1",
        "llm_model": "test-model",
        "decision_assessment_mode": "auto",
        "decision_assessment_max_records": 15,
        "request_interval": 0.1,
        "embedded_worker": False,
    }
    values.update(updates)
    return Settings(**values)


def tender(
    canonical_id: str = "record-001",
    title: str = "广东政务云 AI 服务器采购公告",
    event_type: EventType = EventType.TENDER,
) -> TenderRecord:
    return TenderRecord(
        canonical_id=canonical_id,
        project_key=f"project-{canonical_id}",
        version_hash=f"version-{canonical_id}",
        title=title,
        published_at=datetime(2026, 7, 18, 9, 0),
        region="广东",
        buyer="广东省政务服务和数据管理局",
        event_type=event_type,
        project_id="GD-2026-001",
        summary="采购 AI 服务器并完成信创适配。",
        body_excerpt="采购 AI 服务器并完成信创适配与本地实施。",
        source_urls=[f"https://example.com/{canonical_id}"],
        sources=["测试官方源"],
        relevance_score=95,
        opportunity_score=76,
        lifecycle_id=f"project-{canonical_id}",
    )


def profile() -> CompanyProfile:
    return CompanyProfile(
        company_name="示例科技",
        offerings=["AI 服务器", "数据中心集成"],
        strengths=["信创适配", "本地实施"],
        target_regions=["广东"],
        excluded_terms=["纯土建"],
        preferred_buyers=["政务服务和数据管理局"],
        decision_focus="balanced",
        version="profile-v1",
        updated_at=datetime(2026, 7, 18, 8, 0),
    )


def feedback(record: TenderRecord, verdict: FeedbackVerdict) -> TenderFeedback:
    return TenderFeedback(
        canonical_id=record.canonical_id,
        version_hash=record.version_hash,
        verdict=verdict,
        reason="用户人工判断",
        record=record,
        created_at=datetime(2026, 7, 18, 10, 0),
        updated_at=datetime(2026, 7, 18, 10, 0),
    )


def valid_response(evidence_id: str = "E01") -> str:
    return json.dumps(
        {
            "assessments": [
                {
                    "evidence_id": evidence_id,
                    "matched_profile_terms": ["AI 服务器", "信创适配"],
                    "evidence_quotes": ["采购 AI 服务器并完成信创适配"],
                }
            ]
        },
        ensure_ascii=False,
    )


async def test_llm_fit_hydrates_identity_and_applies_bounded_feedback(tmp_path: Path):
    calls = 0

    async def requester(_payload):
        nonlocal calls
        calls += 1
        return valid_response()

    item = tender()
    assessor = OpportunityFitAssessor(make_settings(tmp_path), requester=requester)
    result = await assessor.generate(
        [item],
        profile(),
        [feedback(item, FeedbackVerdict.RELEVANT)],
    )

    assert calls == 1
    assert result.mode == "llm_grounded"
    assert result.status == "applied"
    decision = result.assessments[0]
    assert decision.title == item.title
    assert decision.source_url == item.source_urls[0]
    assert decision.published_at == item.published_at
    assert decision.region == item.region
    assert decision.base_fit_score == 100
    assert decision.personalization_adjustment == 12
    assert decision.fit_score == 100
    assert decision.recommendation == "bid"
    assert decision.evidence_quotes == ["采购 AI 服务器并完成信创适配"]
    assert "相关" in decision.personalization_reason


async def test_fit_rejects_unknown_evidence_then_repairs_once(tmp_path: Path):
    responses = iter([valid_response("E99"), valid_response("E01")])

    async def requester(_payload):
        return next(responses)

    result = await OpportunityFitAssessor(make_settings(tmp_path), requester=requester).generate(
        [tender()], profile(), []
    )

    assert result.status == "applied"
    assert result.repair_count == 1
    assert result.assessments[0].canonical_id == "record-001"


async def test_fit_invalid_numbers_and_extra_fields_fall_back_as_a_whole(tmp_path: Path):
    invalid = json.loads(valid_response())
    invalid["assessments"][0]["evidence_quotes"] = ["内部消息保证中标且没有风险"]
    invalid["assessments"][0]["fit_score"] = 100
    invalid["assessments"][0]["recommendation"] = "bid"
    invalid["assessments"][0]["invented"] = "not allowed"

    async def requester(_payload):
        return json.dumps(invalid, ensure_ascii=False)

    result = await OpportunityFitAssessor(make_settings(tmp_path), requester=requester).generate(
        [tender()], profile(), []
    )

    assert result.mode == "deterministic"
    assert result.status == "invalid_response"
    assert result.assessments[0].title == tender().title


async def test_late_stage_never_becomes_open_bid_recommendation(tmp_path: Path):
    async def requester(_payload):
        return valid_response()

    result = await OpportunityFitAssessor(make_settings(tmp_path), requester=requester).generate(
        [tender(event_type=EventType.AWARD)], profile(), []
    )

    decision = result.assessments[0]
    assert decision.base_fit_score == 50
    assert decision.recommendation == "watch"


async def test_target_province_accepts_its_city_but_outside_region_cannot_be_bid(
    tmp_path: Path,
):
    async def requester(_payload):
        return valid_response()

    assessor = OpportunityFitAssessor(make_settings(tmp_path), requester=requester)
    shenzhen = tender()
    shenzhen.region = "深圳"
    in_region = await assessor.generate([shenzhen], profile(), [])

    outside = tender("record-002")
    outside.region = "北京"
    outside_region = await assessor.generate([outside], profile(), [])

    assert assessor._region_matches("深圳", ["广东"]) is True
    assert assessor._region_matches("广州", ["深圳"]) is False
    assert in_region.assessments[0].recommendation == "bid"
    assert outside_region.assessments[0].recommendation == "watch"


async def test_fit_rejects_numeric_substrings_and_ungrounded_chinese_quantities(
    tmp_path: Path,
):
    invalid = json.loads(valid_response())
    invalid["assessments"][0]["evidence_quotes"] = ["预计中标概率 26%，需要核验三级资质。"]

    async def requester(_payload):
        return json.dumps(invalid, ensure_ascii=False)

    result = await OpportunityFitAssessor(make_settings(tmp_path), requester=requester).generate(
        [tender()], profile(), []
    )

    assert result.status == "invalid_response"
    assert OpportunityFitAssessor._numbers_are_grounded("26%", "发布时间为 2026 年") is False
    assert (
        OpportunityFitAssessor._numbers_are_grounded("需要核验三级资质", "资格条件待核验") is False
    )


async def test_every_retained_record_gets_assessment_beyond_model_window(tmp_path: Path):
    records = [tender(f"record-{index:03d}") for index in range(1, 5)]
    response = {
        "assessments": [
            {
                **json.loads(valid_response(f"E{index:02d}"))["assessments"][0],
                "evidence_id": f"E{index:02d}",
            }
            for index in range(1, 4)
        ]
    }

    async def requester(payload):
        assert len(payload["messages"][1]["content"]) > 0
        return json.dumps(response, ensure_ascii=False)

    settings = make_settings(tmp_path, decision_assessment_max_records=3)
    result = await OpportunityFitAssessor(settings, requester=requester).generate(
        records, profile(), []
    )

    assert [item.evidence_id for item in result.assessments] == ["E01", "E02", "E03", "E04"]
    assert result.assessments[-1].canonical_id == "record-004"
    assert "其余 1 条" in result.summary


async def test_model_semantic_match_cannot_change_local_score_or_recommendation(tmp_path: Path):
    semantic_profile = profile().model_copy(update={"offerings": ["区块链存证"], "strengths": []})
    response = json.loads(valid_response())
    response["assessments"][0]["matched_profile_terms"] = ["区块链存证"]

    async def requester(_payload):
        return json.dumps(response, ensure_ascii=False)

    settings = make_settings(tmp_path)
    model_result = await OpportunityFitAssessor(settings, requester=requester).generate(
        [tender()], semantic_profile, []
    )
    local_result = await OpportunityFitAssessor(
        settings.model_copy(update={"decision_assessment_mode": "off"})
    ).generate([tender()], semantic_profile, [])

    assert model_result.status == "applied"
    assert model_result.assessments[0].matched_profile_terms == ["区块链存证"]
    assert model_result.assessments[0].base_fit_score == local_result.assessments[0].base_fit_score
    assert model_result.assessments[0].recommendation == local_result.assessments[0].recommendation
    assert "不直接改变本地分数" in model_result.assessments[0].reason


async def test_excluded_term_in_full_evidence_is_a_local_hard_boundary(tmp_path: Path):
    item = tender()
    item.evidence = [
        EvidenceSpan(
            text="采购 AI 服务器并完成信创适配，同时包含纯土建施工。",
            source_url=item.source_urls[0],
        )
    ]

    async def requester(_payload):
        return valid_response()

    result = await OpportunityFitAssessor(make_settings(tmp_path), requester=requester).generate(
        [item], profile(), []
    )

    assert result.assessments[0].fit_score <= 25
    assert result.assessments[0].recommendation == "skip"
    assert any("纯土建" in gap for gap in result.assessments[0].gaps)


async def test_evidence_id_itself_is_not_a_valid_quote(tmp_path: Path):
    response = json.loads(valid_response())
    response["assessments"][0]["evidence_quotes"] = ["E01"]
    item = tender(title="E01 广东政务云 AI 服务器采购公告")

    async def requester(_payload):
        return json.dumps(response, ensure_ascii=False)

    result = await OpportunityFitAssessor(make_settings(tmp_path), requester=requester).generate(
        [item], profile(), []
    )

    assert result.status == "invalid_response"


async def test_damaged_record_without_source_url_still_gets_safe_local_assessment(tmp_path: Path):
    item = tender()
    item.source_urls = []
    result = await OpportunityFitAssessor(
        make_settings(tmp_path, decision_assessment_mode="off")
    ).generate([item], profile(), [])

    assert result.status == "disabled"
    assert result.assessments[0].source_url == ""


async def test_missing_profile_uses_deterministic_fallback_without_model(tmp_path: Path):
    called = False

    async def requester(_payload):
        nonlocal called
        called = True
        return valid_response()

    result = await OpportunityFitAssessor(make_settings(tmp_path), requester=requester).generate(
        [tender()], CompanyProfile(), []
    )

    assert called is False
    assert result.status == "profile_missing"
    assert result.profile_configured is False
    assert result.assessments[0].gaps == ["企业画像尚未配置"]


def test_personalization_is_bounded_and_resettable(tmp_path: Path):
    item = tender()
    positives = [
        feedback(
            tender(f"positive-{index}", f"广东政务云 AI 服务器采购公告 {index}"),
            FeedbackVerdict.CONTACTED,
        )
        for index in range(8)
    ]
    positive, _ = OpportunityFitAssessor._personalization(item, positives)
    negative, _ = OpportunityFitAssessor._personalization(
        item,
        [
            feedback(
                tender(f"negative-{index}", f"广东政务云 AI 服务器采购公告 {index}"),
                FeedbackVerdict.IRRELEVANT,
            )
            for index in range(8)
        ],
    )
    reset, _ = OpportunityFitAssessor._personalization(item, [])
    direct_negative, direct_reason = OpportunityFitAssessor._personalization(
        item,
        [feedback(item, FeedbackVerdict.IRRELEVANT), *positives],
    )

    assert positive == 12
    assert negative == -12
    assert reset == 0
    assert direct_negative == -12
    assert "直接反馈优先" in direct_reason


async def test_service_reuses_only_valid_grounded_fit_cache(tmp_path: Path):
    calls = 0

    async def requester(_payload):
        nonlocal calls
        calls += 1
        return valid_response()

    settings = make_settings(tmp_path)
    service = BidPilotService(settings)
    service.fit_assessor = OpportunityFitAssessor(settings, requester=requester)
    service.update_company_profile(
        CompanyProfileUpdate(
            offerings=["AI 服务器"],
            strengths=["信创适配"],
            target_regions=["广东"],
            decision_focus="balanced",
        )
    )
    item = tender()
    service.db.upsert_records([item.model_dump(mode="json")])
    spec = IntentParser(settings.timezone).parse("最近1个月广东服务器招标信息")
    service.db.create_run("run-fit-cache", spec)
    service.db.set_run_items("run-fit-cache", [item.model_dump(mode="json")])

    first = await service.assess_run("run-fit-cache")
    second = await service.assess_run("run-fit-cache")

    assert first.status == "applied"
    assert second.status == "cached"
    assert second.cache_hit is True
    assert calls == 1

    service.set_feedback(
        item.canonical_id,
        item.version_hash,
        FeedbackUpdate(verdict=FeedbackVerdict.RELEVANT, reason="人工确认"),
    )
    third = await service.assess_run("run-fit-cache")
    assert third.status == "applied"
    assert third.assessments[0].personalization_adjustment == 12
    assert calls == 2

    changed_snapshot = item.model_copy(
        update={"body_excerpt": "采购 AI 服务器并完成信创适配与本地实施，同时增加运维要求。"}
    )
    service.db.set_run_items(
        "run-fit-cache",
        [changed_snapshot.model_dump(mode="json")],
    )
    fourth = await service.assess_run("run-fit-cache")
    assert fourth.status == "applied"
    assert calls == 3
