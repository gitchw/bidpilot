from __future__ import annotations

import calendar
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta

from bidpilot.models import IntentSchedule, ScheduleKind, TenderQuerySpec

REGIONS: dict[str, tuple[str, str]] = {
    "北京": ("北京", "110000"),
    "北京市": ("北京", "110000"),
    "天津": ("天津", "120000"),
    "天津市": ("天津", "120000"),
    "河北": ("河北", "130000"),
    "河北省": ("河北", "130000"),
    "山西": ("山西", "140000"),
    "山西省": ("山西", "140000"),
    "内蒙古": ("内蒙古", "150000"),
    "辽宁": ("辽宁", "210000"),
    "吉林": ("吉林", "220000"),
    "黑龙江": ("黑龙江", "230000"),
    "上海": ("上海", "310000"),
    "上海市": ("上海", "310000"),
    "江苏": ("江苏", "320000"),
    "江苏省": ("江苏", "320000"),
    "浙江": ("浙江", "330000"),
    "浙江省": ("浙江", "330000"),
    "安徽": ("安徽", "340000"),
    "安徽省": ("安徽", "340000"),
    "福建": ("福建", "350000"),
    "福建省": ("福建", "350000"),
    "江西": ("江西", "360000"),
    "江西省": ("江西", "360000"),
    "山东": ("山东", "370000"),
    "山东省": ("山东", "370000"),
    "河南": ("河南", "410000"),
    "河南省": ("河南", "410000"),
    "湖北": ("湖北", "420000"),
    "湖北省": ("湖北", "420000"),
    "湖南": ("湖南", "430000"),
    "湖南省": ("湖南", "430000"),
    "广东": ("广东", "440000"),
    "广东省": ("广东", "440000"),
    "广西": ("广西", "450000"),
    "海南": ("海南", "460000"),
    "海南省": ("海南", "460000"),
    "重庆": ("重庆", "500000"),
    "重庆市": ("重庆", "500000"),
    "四川": ("四川", "510000"),
    "四川省": ("四川", "510000"),
    "贵州": ("贵州", "520000"),
    "贵州省": ("贵州", "520000"),
    "云南": ("云南", "530000"),
    "云南省": ("云南", "530000"),
    "西藏": ("西藏", "540000"),
    "陕西": ("陕西", "610000"),
    "陕西省": ("陕西", "610000"),
    "甘肃": ("甘肃", "620000"),
    "甘肃省": ("甘肃", "620000"),
    "青海": ("青海", "630000"),
    "青海省": ("青海", "630000"),
    "宁夏": ("宁夏", "640000"),
    "新疆": ("新疆", "650000"),
}

TOPIC_SYNONYMS: dict[str, list[str]] = {
    "服务器": ["服务器", "计算节点", "算力设备", "机架式服务器", "GPU服务器"],
    "充电桩": ["充电桩", "充电站", "充电设施", "充换电", "新能源汽车充电"],
    "数据中心": ["数据中心", "机房", "算力中心", "智算中心"],
    "存储": ["存储", "磁盘阵列", "分布式存储", "存储服务器"],
}

WEEKDAYS = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
}


class IntentParser:
    version = "rules-v1"

    def __init__(self, timezone: str = "Asia/Shanghai"):
        self.timezone = timezone

    def parse(self, query: str, now: datetime | None = None) -> TenderQuerySpec:
        query = self._normalize(query)
        if not query:
            raise ValueError("查询内容不能为空")
        now = now or datetime.now(ZoneInfo(self.timezone))
        if now.tzinfo is None:
            now = now.replace(tzinfo=ZoneInfo(self.timezone))

        region, region_code, region_token = self._parse_region(query)
        start_date, end_date, time_explicit = self._parse_date_range(query, now.date())
        schedule = self._parse_schedule(query, now)
        topic = self._parse_topic(query, region_token)
        keywords = self._expand_keywords(topic)
        channel = self._parse_delivery_channel(query)

        warnings: list[str] = []
        if not region:
            warnings.append("未识别到明确地域，将按全国范围检索。")
        if not time_explicit:
            warnings.append("未识别到明确时间范围，默认检索最近 30 天。")
        if len(topic) < 2:
            warnings.append("主题过短，建议补充产品或服务关键词。")
        if schedule.kind in {ScheduleKind.DAILY, ScheduleKind.WEEKLY} and not schedule.send_time:
            warnings.append("未识别到发送时间，默认每天 09:00。")

        confidence = {
            "topic": 0.94 if topic and topic not in {"招标", "采购"} else 0.55,
            "region": 0.98 if region else 0.5,
            "time_range": 0.97 if time_explicit else 0.65,
            "schedule": 0.98 if schedule.kind != ScheduleKind.IMMEDIATE else 0.9,
        }

        return TenderQuerySpec(
            raw_query=query,
            topic=topic,
            keywords=keywords,
            region=region,
            region_code=region_code,
            start_date=start_date,
            end_date=end_date,
            schedule=schedule,
            delivery_channel=channel,
            slot_confidence=confidence,
            warnings=warnings,
            parser_version=self.version,
        )

    @staticmethod
    def _normalize(text: str) -> str:
        text = text.strip().replace("：", ":")
        text = re.sub(r"\s+", " ", text)
        return text

    @staticmethod
    def _parse_region(query: str) -> tuple[str | None, str | None, str]:
        for token in sorted(REGIONS, key=len, reverse=True):
            if token in query:
                region, code = REGIONS[token]
                return region, code, token
        return None, None, ""

    @staticmethod
    def _month_bounds(year: int, month: int) -> tuple[date, date]:
        last_day = calendar.monthrange(year, month)[1]
        return date(year, month, 1), date(year, month, last_day)

    def _parse_date_range(self, query: str, today: date) -> tuple[date, date, bool]:
        absolute_month = re.search(r"(20\d{2})年\s*(1[0-2]|0?[1-9])月(?:份)?", query)
        if absolute_month:
            return (
                *self._month_bounds(int(absolute_month.group(1)), int(absolute_month.group(2))),
                True,
            )

        explicit_range = re.search(
            r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?\s*(?:至|到|~|—|-)\s*"
            r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?",
            query,
        )
        if explicit_range:
            start = date(*map(int, explicit_range.group(1, 2, 3)))
            end = date(*map(int, explicit_range.group(4, 5, 6)))
            if start > end:
                start, end = end, start
            return start, end, True

        recent = re.search(r"(?:最近|近)\s*(\d+)\s*(天|周|个月|月)", query)
        if recent:
            count = max(1, int(recent.group(1)))
            unit = recent.group(2)
            if unit == "天":
                start = today - timedelta(days=count)
            elif unit == "周":
                start = today - timedelta(weeks=count)
            else:
                start = today - relativedelta(months=count)
            return start, today, True

        if "本月" in query:
            start, end = self._month_bounds(today.year, today.month)
            return start, min(today, end), True
        if "上月" in query:
            previous = today - relativedelta(months=1)
            return (*self._month_bounds(previous.year, previous.month), True)
        if "最近一周" in query or "近一周" in query:
            return today - timedelta(days=7), today, True

        return today - timedelta(days=30), today, False

    def _parse_schedule(self, query: str, now: datetime) -> IntentSchedule:
        send_time = self._parse_clock(query)

        if re.search(r"(?:每天|每日)", query):
            send_time = send_time or time(9, 0)
            return IntentSchedule(
                kind=ScheduleKind.DAILY,
                send_time=send_time,
                timezone=self.timezone,
                expression=f"每日 {send_time.strftime('%H:%M')}",
            )

        weekly = re.search(r"每周\s*([一二三四五六日天])?", query)
        if weekly:
            weekday = WEEKDAYS.get(weekly.group(1) or "一", 0)
            send_time = send_time or time(9, 0)
            return IntentSchedule(
                kind=ScheduleKind.WEEKLY,
                send_time=send_time,
                weekday=weekday,
                timezone=self.timezone,
                expression=f"每周{weekly.group(1) or '一'} {send_time.strftime('%H:%M')}",
            )

        if re.search(r"(?:今天|今日|明天|明日).{0,12}(?:发送|推送|汇总)", query):
            target = now.date() + (
                timedelta(days=1) if re.search(r"明天|明日", query) else timedelta()
            )
            send_time = send_time or time(9, 0)
            run_at = datetime.combine(target, send_time, tzinfo=ZoneInfo(self.timezone))
            if run_at <= now:
                run_at = now + timedelta(minutes=1)
            return IntentSchedule(
                kind=ScheduleKind.ONCE,
                send_time=send_time,
                run_at=run_at,
                timezone=self.timezone,
                expression=f"一次性 {run_at.strftime('%Y-%m-%d %H:%M')}",
            )

        return IntentSchedule(
            kind=ScheduleKind.IMMEDIATE,
            timezone=self.timezone,
            expression="立即执行",
        )

    @staticmethod
    def _parse_clock(query: str) -> time | None:
        colon = re.search(r"(?:上午|早上|下午|晚上|中午)?\s*(\d{1,2})\s*:\s*(\d{2})", query)
        if colon:
            hour, minute = int(colon.group(1)), int(colon.group(2))
            prefix = query[max(0, colon.start() - 3) : colon.start() + 1]
            if re.search(r"下午|晚上", prefix) and hour < 12:
                hour += 12
            return time(min(hour, 23), min(minute, 59))
        chinese = re.search(
            r"(?:上午|早上|下午|晚上|中午)?\s*(\d{1,2})\s*点(?:\s*(\d{1,2})\s*分)?", query
        )
        if chinese:
            hour, minute = int(chinese.group(1)), int(chinese.group(2) or 0)
            prefix = query[max(0, chinese.start() - 3) : chinese.start() + 2]
            if re.search(r"下午|晚上", prefix) and hour < 12:
                hour += 12
            if "中午" in prefix and hour < 11:
                hour += 12
            return time(min(hour, 23), min(minute, 59))
        return None

    def _parse_topic(self, query: str, region_token: str) -> str:
        working = query
        working = re.split(r"[，,。；;]\s*(?:请|并|然后)?", working, maxsplit=1)[0]
        working = re.sub(r"20\d{2}年\s*\d{1,2}月(?:份)?", " ", working)
        working = re.sub(
            r"20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?\s*(?:至|到|~|—|-)\s*20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?",
            " ",
            working,
        )
        working = re.sub(r"(?:最近|近)\s*\d+\s*(?:天|周|个月|月)", " ", working)
        working = re.sub(r"(?:本月|上月|最近一周|近一周)", " ", working)
        if region_token:
            working = working.replace(region_token, " ")
        # Keep this pattern non-empty. An all-optional regex would match between
        # every character and prevent later phrase-level cleanup from working.
        working = re.sub(r"(?:区域|地区)内?的?|内的|的", " ", working)
        working = re.sub(r"(?:相关的?)?(?:招标|采购|标讯)(?:公告|项目)?信息", " ", working)
        working = re.sub(r"(?:招标|采购|标讯)(?:公告|项目)", " ", working)
        working = re.sub(r"都有哪些|有哪些|查询|查找|搜索|帮我|请问", " ", working)
        working = re.sub(r"[{}【】\[\]()（）]", " ", working)
        working = re.sub(r"\s+", "", working).strip("的相关")
        return working or "招投标"

    @staticmethod
    def _expand_keywords(topic: str) -> list[str]:
        for key, synonyms in TOPIC_SYNONYMS.items():
            if key in topic or topic in synonyms:
                return [topic, *synonyms]
        pieces = [p for p in re.split(r"[、/与和或 ]+", topic) if len(p) >= 2]
        return [topic, *pieces]

    @staticmethod
    def _parse_delivery_channel(query: str) -> str:
        if "飞书" in query:
            return "feishu"
        if "邮件" in query or "邮箱" in query:
            return "email"
        return "local"


def parse_intent(query: str, now: datetime | None = None) -> TenderQuerySpec:
    return IntentParser().parse(query, now=now)
