from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from bidpilot.config import Settings
from bidpilot.hybrid_intent import HybridIntentEngine
from bidpilot.intent import IntentParser

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def make_engine(response: dict | str, **settings_overrides):
    calls: list[dict] = []

    async def requester(payload: dict) -> str:
        calls.append(payload)
        return response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)

    settings = Settings(
        llm_base_url="http://model.invalid/v1",
        llm_model="test-model",
        llm_api_key="unit-test-secret",
        **settings_overrides,
    )
    return HybridIntentEngine(settings, IntentParser(), requester=requester), calls


async def test_high_confidence_rules_skip_llm_in_auto_mode():
    engine, calls = make_engine("should not be used")
    spec = await engine.resolve("最近1个月深圳服务器招标信息", now=NOW)
    assert spec.region == "深圳"
    assert spec.topic == "服务器"
    assert spec.resolution.llm_status == "not_needed"
    assert calls == []


async def test_llm_repairs_only_grounded_low_confidence_fields():
    engine, calls = make_engine(
        {
            "topic": "储能系统",
            "region": "泉州",
            "start_date": "2026-06-03",
            "end_date": "2026-07-18",
            "schedule": None,
            "delivery_channel": None,
            "exclude_keywords": None,
            "event_types": None,
            "confidence": {
                "topic": 0.96,
                "region": 0.99,
                "start_date": 0.93,
                "end_date": 0.93,
            },
            "rationale": "原句中的近四十五日应为 45 天，主题为储能系统。",
        }
    )
    spec = await engine.resolve("帮我查近四十五日泉州储能系统项目", now=NOW)
    assert spec.topic == "储能系统"
    assert spec.region == "泉州"
    assert spec.region_code == "350000"
    assert spec.region_level == "city"
    assert spec.start_date == date(2026, 6, 3)
    assert spec.end_date == date(2026, 7, 18)
    assert spec.resolution.llm_status == "applied"
    assert spec.parser_version == "hybrid-v1"
    assert {item.field for item in spec.resolution.decisions if item.outcome == "accepted"} == {
        "topic",
        "time_range",
    }
    assert len(calls) == 1
    serialized_request = json.dumps(calls[0], ensure_ascii=False)
    assert "unit-test-secret" not in serialized_request
    assert "model.invalid" not in serialized_request


async def test_hallucinated_topic_and_region_are_rejected():
    engine, _calls = make_engine(
        {
            "topic": "量子计算",
            "region": "北京",
            "start_date": None,
            "end_date": None,
            "schedule": None,
            "delivery_channel": None,
            "exclude_keywords": None,
            "event_types": None,
            "confidence": {"topic": 0.99, "region": 0.99},
            "rationale": "猜测",
        }
    )
    spec = await engine.resolve("查询液冷设备招标信息", now=NOW)
    assert spec.topic == "液冷设备"
    assert spec.region is None
    assert spec.resolution.llm_status == "rejected"
    assert {item.outcome for item in spec.resolution.decisions} == {"rejected"}


async def test_markdown_wrapped_json_is_not_accepted():
    engine, _calls = make_engine(
        '```json\n{"topic":"液冷","confidence":{"topic":0.9},"rationale":"x"}\n```'
    )
    spec = await engine.resolve("查询液冷设备招标信息", now=NOW)
    assert spec.topic == "液冷设备"
    assert spec.resolution.mode == "rules"
    assert spec.resolution.llm_status == "invalid_response"


async def test_unknown_json_field_invalidates_whole_proposal():
    engine, _calls = make_engine(
        {
            "topic": "液冷设备",
            "confidence": {"topic": 0.9},
            "rationale": "原句",
            "region_code": "110000",
        }
    )
    spec = await engine.resolve("查询液冷设备招标信息", now=NOW)
    assert spec.resolution.llm_status == "invalid_response"
    assert spec.region_code is None


async def test_off_mode_never_calls_model_even_when_fields_are_missing():
    engine, calls = make_engine("should not be used", intent_llm_mode="off")
    spec = await engine.resolve("查询液冷设备招标信息", now=NOW)
    assert spec.resolution.llm_status == "disabled"
    assert calls == []


async def test_model_failure_keeps_rule_result():
    async def failing_requester(_payload: dict) -> str:
        raise TimeoutError

    settings = Settings(
        llm_base_url="http://model.invalid/v1",
        llm_model="test-model",
    )
    engine = HybridIntentEngine(settings, IntentParser(), requester=failing_requester)
    spec = await engine.resolve("查询液冷设备招标信息", now=NOW)
    assert spec.topic == "液冷设备"
    assert spec.resolution.llm_status == "unavailable"
    assert spec.resolution.mode == "rules"


async def test_compare_reports_only_real_changes():
    engine, _calls = make_engine(
        {
            "topic": "储能系统",
            "region": "泉州",
            "start_date": "2026-06-03",
            "end_date": "2026-07-18",
            "schedule": None,
            "delivery_channel": None,
            "exclude_keywords": None,
            "event_types": None,
            "confidence": {
                "topic": 0.96,
                "region": 0.99,
                "time_range": 0.93,
            },
            "rationale": "修复",
        }
    )
    comparison = await engine.compare("帮我查近四十五日泉州储能系统项目", now=NOW)
    assert comparison.rules.topic != comparison.resolved.topic
    assert "topic" in comparison.changed_fields
    assert "start_date" in comparison.changed_fields
    assert "region" not in comparison.changed_fields
