from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bidpilot.intent import IntentParser
from bidpilot.models import ScheduleKind

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
