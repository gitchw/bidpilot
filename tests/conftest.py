from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bidpilot.intent import IntentParser


@pytest.fixture
def sample_spec():
    now = datetime(2026, 7, 17, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    return IntentParser().parse("最近1个月安徽服务器招标信息", now=now)
