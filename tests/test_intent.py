from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bidpilot.intent import IntentParser
from bidpilot.models import EventType, ScheduleKind

NOW = datetime(2026, 7, 17, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.mark.parametrize(
    ("query", "topic", "region", "start", "end", "kind", "clock"),
    [
        (
            "最近1个月的安徽省区域内的服务器招标信息都有哪些",
            "服务器",
            "安徽",
            "2026-06-17",
            "2026-07-17",
            ScheduleKind.IMMEDIATE,
            None,
        ),
        (
            "2026年3月份的上海区域内的充电桩招标信息都有哪些",
            "充电桩",
            "上海",
            "2026-03-01",
            "2026-03-31",
            ScheduleKind.IMMEDIATE,
            None,
        ),
        (
            "最近3个月的上海区域内的充电桩招标信息都有哪些，请汇总后每天9:00发送给我",
            "充电桩",
            "上海",
            "2026-04-17",
            "2026-07-17",
            ScheduleKind.DAILY,
            "09:00:00",
        ),
        (
            "2026年4月份上海的充电桩招标信息都有哪些，请汇总后今天9:00发送给我",
            "充电桩",
            "上海",
            "2026-04-01",
            "2026-04-30",
            ScheduleKind.ONCE,
            "09:00:00",
        ),
    ],
)
def test_official_examples(query, topic, region, start, end, kind, clock):
    spec = IntentParser().parse(query, now=NOW)
    assert spec.topic == topic
    assert spec.region == region
    assert spec.start_date.isoformat() == start
    assert spec.end_date.isoformat() == end
    assert spec.schedule.kind == kind
    assert (spec.schedule.send_time.isoformat() if spec.schedule.send_time else None) == clock


def test_weekly_schedule_and_feishu_channel():
    spec = IntentParser().parse("近2周江苏数据中心招标信息，每周一上午8点推送到飞书", now=NOW)
    assert spec.topic == "数据中心"
    assert spec.region == "江苏"
    assert spec.schedule.kind == ScheduleKind.WEEKLY
    assert spec.schedule.weekday == 0
    assert spec.schedule.send_time.isoformat() == "08:00:00"
    assert spec.delivery_channel == "feishu"


def test_email_delivery_channel_is_preserved_for_scheduled_query():
    spec = IntentParser().parse(
        "近1个月江苏服务器招标信息，每天9点发送到邮箱",
        now=NOW,
    )
    assert spec.schedule.kind == ScheduleKind.DAILY
    assert spec.delivery_channel == "email"


def test_default_window_and_nationwide_warning():
    spec = IntentParser().parse("查询液冷设备招标信息", now=NOW)
    assert spec.topic == "液冷设备"
    assert spec.region is None
    assert spec.start_date.isoformat() == "2026-06-17"
    assert len(spec.warnings) == 2


def test_absolute_date_range_is_normalized():
    spec = IntentParser().parse("2026-07-10到2026-06-01北京服务器采购信息", now=NOW)
    assert spec.start_date.isoformat() == "2026-06-01"
    assert spec.end_date.isoformat() == "2026-07-10"


def test_empty_query_is_rejected():
    with pytest.raises(ValueError, match="不能为空"):
        IntentParser().parse("   ", now=NOW)


EXTENDED_INTENT_CASES = [
    (
        "帮我找近三个月上海地区的充电桩项目",
        {"topic": "充电桩", "region": "上海", "start": "2026-04-17", "end": "2026-07-17"},
    ),
    (
        "查询过去90天北京GPU服务器采购",
        {"topic": "GPU服务器", "region": "北京", "start": "2026-04-18", "end": "2026-07-17"},
    ),
    (
        "今年以来广东数据中心项目",
        {"topic": "数据中心", "region": "广东", "start": "2026-01-01", "end": "2026-07-17"},
    ),
    (
        "2026年1月到6月江苏芯片服务器招标",
        {
            "topic": "芯片服务器",
            "region": "江苏",
            "start": "2026-01-01",
            "end": "2026-06-30",
        },
    ),
    (
        "2026-01-01至2026-06-30浙江存储采购",
        {"topic": "存储", "region": "浙江", "start": "2026-01-01", "end": "2026-06-30"},
    ),
    (
        "最近半年全国算力中心标讯",
        {
            "topic": "算力中心",
            "region": None,
            "start": "2026-01-17",
            "end": "2026-07-17",
            "warnings": [],
        },
    ),
    (
        "近两个月安徽液冷设备",
        {"topic": "液冷设备", "region": "安徽", "start": "2026-05-17", "end": "2026-07-17"},
    ),
    (
        "今天发布的北京服务器公告",
        {"topic": "服务器", "region": "北京", "start": "2026-07-17", "end": "2026-07-17"},
    ),
    (
        "昨天上海充电桩中标公告",
        {
            "topic": "充电桩",
            "region": "上海",
            "start": "2026-07-16",
            "end": "2026-07-16",
            "events": [EventType.AWARD],
        },
    ),
    (
        "每天早上八点半推送江苏服务器信息",
        {
            "topic": "服务器",
            "region": "江苏",
            "kind": ScheduleKind.DAILY,
            "clock": "08:30:00",
        },
    ),
    (
        "每周五下午3点发送广东数据中心标讯",
        {
            "topic": "数据中心",
            "region": "广东",
            "kind": ScheduleKind.WEEKLY,
            "weekday": 4,
            "clock": "15:00:00",
        },
    ),
    (
        "每月1日上午9点发送存储采购",
        {
            "topic": "存储",
            "kind": ScheduleKind.MONTHLY,
            "day_of_month": 1,
            "clock": "09:00:00",
        },
    ),
    (
        "明天下午2点把北京GPU服务器信息发到邮箱",
        {
            "topic": "GPU服务器",
            "region": "北京",
            "kind": ScheduleKind.ONCE,
            "clock": "14:00:00",
            "channel": "email",
            "run_at": "2026-07-18T14:00:00+08:00",
        },
    ),
    (
        "近30天深圳液冷数据中心招标",
        {
            "topic": "液冷数据中心",
            "region": "深圳",
            "region_code": "440000",
        },
    ),
    (
        "最近1个月深圳充电桩招标信息",
        {
            "topic": "充电桩",
            "region": "深圳",
            "region_code": "440000",
            "region_level": "city",
            "start": "2026-06-17",
            "end": "2026-07-17",
            "events": [],
            "warnings": [],
        },
    ),
    (
        "近30天苏州服务器采购",
        {
            "topic": "服务器",
            "region": "苏州",
            "region_code": "320000",
        },
    ),
    (
        "近30天全国服务器招标",
        {"topic": "服务器", "region": None, "events": [], "warnings": []},
    ),
    (
        "广东数据中心液冷项目，排除运维服务和空调维保",
        {
            "topic": "数据中心液冷",
            "region": "广东",
            "exclude": ["运维服务", "空调维保"],
        },
    ),
    (
        "上海GPU服务器招标和中标公告",
        {
            "topic": "GPU服务器",
            "region": "上海",
            "events": [EventType.TENDER, EventType.AWARD],
        },
    ),
    (
        "近1个月江苏服务器，每周一9点发送到飞书",
        {"topic": "服务器", "kind": ScheduleKind.WEEKLY, "weekday": 0, "channel": "feishu"},
    ),
    (
        "近1个月江苏服务器，每天9点发送到企业微信",
        {"topic": "服务器", "kind": ScheduleKind.DAILY, "channel": "wecom_webhook"},
    ),
    (
        "近1个月江苏服务器，每天9点发送到钉钉",
        {"topic": "服务器", "kind": ScheduleKind.DAILY, "channel": "dingtalk_webhook"},
    ),
    (
        "近1个月江苏服务器，每天9点发送到自定义Webhook",
        {"topic": "服务器", "kind": ScheduleKind.DAILY, "channel": "generic_webhook"},
    ),
    (
        "近1个月江苏服务器，每天9点发送到 Telegram",
        {"topic": "服务器", "kind": ScheduleKind.DAILY, "channel": "telegram_bot"},
    ),
    (
        "近1个月江苏服务器，每天9点发送到 Slack",
        {"topic": "服务器", "kind": ScheduleKind.DAILY, "channel": "slack_webhook"},
    ),
    (
        "查找内蒙古自治区近2周算力设备采购",
        {"topic": "算力设备", "region": "内蒙古", "region_code": "150000"},
    ),
    (
        "请关注重庆市智算中心项目",
        {"topic": "智算中心", "region": "重庆", "region_code": "500000"},
    ),
    (
        "2026年第二季度浙江云服务合同公告",
        {
            "topic": "云服务",
            "region": "浙江",
            "start": "2026-04-01",
            "end": "2026-06-30",
            "events": [EventType.CONTRACT],
        },
    ),
    (
        "去年四川网络安全采购信息",
        {
            "topic": "网络安全",
            "region": "四川",
            "start": "2025-01-01",
            "end": "2025-12-31",
        },
    ),
    (
        "上周杭州UPS更正公告",
        {
            "topic": "UPS",
            "region": "杭州",
            "start": "2026-07-06",
            "end": "2026-07-12",
            "events": [EventType.CHANGE],
        },
    ),
    (
        "2026年下半年福建算力中心采购意向",
        {
            "topic": "算力中心",
            "region": "福建",
            "start": "2026-07-01",
            "end": "2026-12-31",
            "events": [EventType.INTENTION],
        },
    ),
    (
        "近一季度青岛云平台成交结果公告",
        {
            "topic": "云平台",
            "region": "青岛",
            "start": "2026-04-17",
            "end": "2026-07-17",
            "events": [EventType.AWARD],
        },
    ),
    (
        "近1个月北京服务器，每日推送",
        {"topic": "服务器", "kind": ScheduleKind.DAILY, "clock": "09:00:00"},
    ),
    (
        "深圳GPU服务器，只看招标公告",
        {
            "topic": "GPU服务器",
            "region": "深圳",
            "events": [EventType.TENDER],
        },
    ),
    (
        "深圳服务器，排除信息安全和中标公告",
        {
            "topic": "服务器",
            "region": "深圳",
            "exclude": ["信息安全", "中标"],
            "events": [],
        },
    ),
    (
        "每月最后一天18点发送广东数据中心信息",
        {
            "topic": "数据中心",
            "region": "广东",
            "kind": ScheduleKind.MONTHLY,
            "day_of_month": 31,
            "clock": "18:00:00",
        },
    ),
]


@pytest.mark.parametrize(
    ("query", "expected"),
    EXTENDED_INTENT_CASES,
    ids=[f"intent-v2-{index:02d}" for index in range(len(EXTENDED_INTENT_CASES))],
)
def test_extended_intent_matrix(query, expected):
    spec = IntentParser().parse(query, now=NOW)
    actual = {
        "topic": spec.topic,
        "region": spec.region,
        "region_code": spec.region_code,
        "region_level": spec.region_level,
        "start": spec.start_date.isoformat(),
        "end": spec.end_date.isoformat(),
        "kind": spec.schedule.kind,
        "clock": spec.schedule.send_time.isoformat() if spec.schedule.send_time else None,
        "weekday": spec.schedule.weekday,
        "day_of_month": spec.schedule.day_of_month,
        "run_at": spec.schedule.run_at.isoformat() if spec.schedule.run_at else None,
        "channel": spec.delivery_channel,
        "exclude": spec.exclude_keywords,
        "events": spec.event_types,
        "warnings": spec.warnings,
    }
    for field, expected_value in expected.items():
        assert actual[field] == expected_value, f"{query}: {field}={actual[field]!r}"


def test_recurring_schedule_without_clock_has_explicit_default_warning():
    spec = IntentParser().parse("近1个月北京服务器，每日推送", now=NOW)
    assert spec.schedule.send_time.isoformat() == "09:00:00"
    assert any("默认使用 09:00" in warning for warning in spec.warnings)


@pytest.mark.parametrize("query", ["每月32日9点推送服务器", "每天25:90推送服务器"])
def test_invalid_schedule_values_are_rejected(query):
    with pytest.raises(ValueError):
        IntentParser().parse(query, now=NOW)
