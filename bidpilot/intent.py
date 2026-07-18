from __future__ import annotations

import calendar
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta

from bidpilot.models import EventType, IntentSchedule, ScheduleKind, TenderQuerySpec

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

REGIONS.update(
    {
        "内蒙古自治区": ("内蒙古", "150000"),
        "广西壮族自治区": ("广西", "450000"),
        "西藏自治区": ("西藏", "540000"),
        "宁夏回族自治区": ("宁夏", "640000"),
        "新疆维吾尔自治区": ("新疆", "650000"),
        "香港特别行政区": ("香港", "810000"),
        "香港": ("香港", "810000"),
        "澳门特别行政区": ("澳门", "820000"),
        "澳门": ("澳门", "820000"),
    }
)

CITY_REGIONS: dict[str, tuple[str, str]] = {
    "深圳": ("深圳", "440000"),
    "广州": ("广州", "440000"),
    "东莞": ("东莞", "440000"),
    "佛山": ("佛山", "440000"),
    "珠海": ("珠海", "440000"),
    "南京": ("南京", "320000"),
    "苏州": ("苏州", "320000"),
    "无锡": ("无锡", "320000"),
    "常州": ("常州", "320000"),
    "南通": ("南通", "320000"),
    "杭州": ("杭州", "330000"),
    "宁波": ("宁波", "330000"),
    "温州": ("温州", "330000"),
    "成都": ("成都", "510000"),
    "绵阳": ("绵阳", "510000"),
    "武汉": ("武汉", "420000"),
    "西安": ("西安", "610000"),
    "济南": ("济南", "370000"),
    "青岛": ("青岛", "370000"),
    "长沙": ("长沙", "430000"),
    "郑州": ("郑州", "410000"),
    "合肥": ("合肥", "340000"),
    "福州": ("福州", "350000"),
    "厦门": ("厦门", "350000"),
    "南昌": ("南昌", "360000"),
    "石家庄": ("石家庄", "130000"),
    "沈阳": ("沈阳", "210000"),
    "大连": ("大连", "210000"),
    "长春": ("长春", "220000"),
    "哈尔滨": ("哈尔滨", "230000"),
    "昆明": ("昆明", "530000"),
    "贵阳": ("贵阳", "520000"),
    "兰州": ("兰州", "620000"),
    "乌鲁木齐": ("乌鲁木齐", "650000"),
    "海口": ("海口", "460000"),
    "三亚": ("三亚", "460000"),
}
CITY_REGIONS.update({f"{city}市": value for city, value in list(CITY_REGIONS.items())})

NATIONWIDE_TOKENS = ("全国", "全网", "不限地区", "不限地域")

TOPIC_SYNONYMS: dict[str, list[str]] = {
    "服务器": ["服务器", "计算节点", "算力设备", "机架式服务器", "GPU服务器"],
    "充电桩": ["充电桩", "充电站", "充电设施", "充换电", "新能源汽车充电"],
    "数据中心": ["数据中心", "机房", "算力中心", "智算中心"],
    "存储": ["存储", "磁盘阵列", "分布式存储", "存储服务器"],
    "液冷": ["液冷", "冷板液冷", "浸没式液冷", "液体冷却"],
    "算力": ["算力", "智算", "AI计算", "高性能计算", "计算集群"],
    "网络安全": ["网络安全", "信息安全", "等保", "安全设备", "防火墙"],
    "云服务": ["云服务", "云计算", "云平台", "公有云", "私有云"],
    "UPS": ["UPS", "不间断电源", "后备电源"],
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

CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
NUMBER_TOKEN = r"(?:\d+|[〇零一二两三四五六七八九十百]+)"


class IntentParser:
    version = "rules-v2"

    def __init__(self, timezone: str = "Asia/Shanghai"):
        self.timezone = timezone

    def parse(self, query: str, now: datetime | None = None) -> TenderQuerySpec:
        query = self._normalize(query)
        if not query:
            raise ValueError("查询内容不能为空")
        now = now or datetime.now(ZoneInfo(self.timezone))
        if now.tzinfo is None:
            now = now.replace(tzinfo=ZoneInfo(self.timezone))

        region, region_code, region_token, region_explicit = self._parse_region(query)
        region_level = (
            "city" if region_token in CITY_REGIONS else "province" if region else "nationwide"
        )
        start_date, end_date, time_explicit = self._parse_date_range(query, now.date())
        clock_explicit = self._parse_clock(query) is not None
        schedule = self._parse_schedule(query, now)
        exclude_keywords = self._parse_exclude_keywords(query)
        event_types = self._parse_event_types(query)
        topic = self._parse_topic(query, region_token)
        keywords = self._expand_keywords(topic)
        channel = self._parse_delivery_channel(query)

        warnings: list[str] = []
        if not region and not region_explicit:
            warnings.append("未识别到明确地域，将按全国范围检索。")
        if not time_explicit:
            warnings.append("未识别到明确时间范围，默认检索最近 30 天。")
        if len(topic) < 2 or topic == "招投标":
            warnings.append("未识别到明确主题，建议补充产品、服务或行业关键词。")
        if (
            schedule.kind
            in {
                ScheduleKind.DAILY,
                ScheduleKind.WEEKLY,
                ScheduleKind.MONTHLY,
            }
            and not clock_explicit
        ):
            warnings.append("未识别到发送时间，默认使用 09:00。")

        confidence = {
            "topic": 0.94 if topic and topic not in {"招标", "采购", "招投标"} else 0.55,
            "region": 0.98 if region or region_explicit else 0.5,
            "time_range": 0.97 if time_explicit else 0.65,
            "schedule": 0.98 if schedule.kind != ScheduleKind.IMMEDIATE else 0.9,
        }

        return TenderQuerySpec(
            raw_query=query,
            topic=topic,
            keywords=keywords,
            exclude_keywords=exclude_keywords,
            event_types=event_types,
            region=region,
            region_code=region_code,
            region_level=region_level,
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
    def _parse_number(value: str) -> int:
        value = value.strip()
        if value.isdigit():
            return int(value)
        total = 0
        current = 0
        for char in value:
            if char in {"十", "百"}:
                unit = 10 if char == "十" else 100
                total += (current or 1) * unit
                current = 0
            elif char in CHINESE_DIGITS:
                current = CHINESE_DIGITS[char]
        return total + current

    @staticmethod
    def _parse_region(query: str) -> tuple[str | None, str | None, str, bool]:
        region_tokens = {**REGIONS, **CITY_REGIONS}
        for token in sorted(region_tokens, key=len, reverse=True):
            if token in query:
                region, code = region_tokens[token]
                return region, code, token, True
        for token in NATIONWIDE_TOKENS:
            if token in query:
                return None, None, token, True
        return None, None, "", False

    @staticmethod
    def _month_bounds(year: int, month: int) -> tuple[date, date]:
        last_day = calendar.monthrange(year, month)[1]
        return date(year, month, 1), date(year, month, last_day)

    def _parse_date_range(self, query: str, today: date) -> tuple[date, date, bool]:
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

        month_range = re.search(
            r"(20\d{2})年\s*(1[0-2]|0?[1-9])月\s*(?:至|到|~|—|-)\s*"
            r"(?:(20\d{2})年\s*)?(1[0-2]|0?[1-9])月",
            query,
        )
        if month_range:
            start_year = int(month_range.group(1))
            start_month = int(month_range.group(2))
            end_year = int(month_range.group(3) or start_year)
            end_month = int(month_range.group(4))
            start = self._month_bounds(start_year, start_month)[0]
            end = self._month_bounds(end_year, end_month)[1]
            if start > end:
                start, end = (
                    self._month_bounds(end_year, end_month)[0],
                    self._month_bounds(start_year, start_month)[1],
                )
            return start, end, True

        half_year = re.search(r"(20\d{2})年\s*(上半年|下半年)", query)
        if half_year:
            year = int(half_year.group(1))
            if half_year.group(2) == "上半年":
                return date(year, 1, 1), date(year, 6, 30), True
            return date(year, 7, 1), date(year, 12, 31), True

        absolute_quarter = re.search(r"(20\d{2})年\s*第?([一二三四1-4])季度", query)
        if absolute_quarter:
            year = int(absolute_quarter.group(1))
            quarter = (
                int(absolute_quarter.group(2))
                if absolute_quarter.group(2).isdigit()
                else self._parse_number(absolute_quarter.group(2))
            )
            start_month = (quarter - 1) * 3 + 1
            return (
                date(year, start_month, 1),
                self._month_bounds(year, start_month + 2)[1],
                True,
            )

        absolute_month = re.search(r"(20\d{2})年\s*(1[0-2]|0?[1-9])月(?:份)?", query)
        if absolute_month:
            return (
                *self._month_bounds(int(absolute_month.group(1)), int(absolute_month.group(2))),
                True,
            )

        if re.search(r"(?:最近|近|过去)\s*半\s*年", query):
            return today - relativedelta(months=6), today, True
        if re.search(r"(?:最近|近|过去)\s*(?:一|1|一个)?\s*(?:季度|季)", query):
            return today - relativedelta(months=3), today, True

        recent = re.search(rf"(?:最近|近|过去)\s*({NUMBER_TOKEN})\s*(天|周|个月|月|年)", query)
        if recent:
            count = max(1, self._parse_number(recent.group(1)))
            unit = recent.group(2)
            if unit == "天":
                start = today - timedelta(days=min(count, 3650))
            elif unit == "周":
                start = today - timedelta(weeks=min(count, 520))
            elif unit == "年":
                start = today - relativedelta(years=min(count, 10))
            else:
                start = today - relativedelta(months=min(count, 120))
            return start, today, True

        if "今年以来" in query or "本年度" in query or "今年" in query:
            return date(today.year, 1, 1), today, True
        if "去年" in query:
            return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31), True
        if "本月" in query:
            start, end = self._month_bounds(today.year, today.month)
            return start, min(today, end), True
        if "上月" in query:
            previous = today - relativedelta(months=1)
            return (*self._month_bounds(previous.year, previous.month), True)
        if "本周" in query:
            return today - timedelta(days=today.weekday()), today, True
        if "上周" in query:
            this_monday = today - timedelta(days=today.weekday())
            return this_monday - timedelta(days=7), this_monday - timedelta(days=1), True
        if "今天" in query or "今日" in query:
            return today, today, True
        if "昨天" in query or "昨日" in query:
            yesterday = today - timedelta(days=1)
            return yesterday, yesterday, True

        return today - timedelta(days=30), today, False

    def _parse_schedule(self, query: str, now: datetime) -> IntentSchedule:
        send_time = self._parse_clock(query)

        if re.search(r"(?:每天|每日|天天)", query):
            send_time = send_time or time(9, 0)
            return IntentSchedule(
                kind=ScheduleKind.DAILY,
                send_time=send_time,
                timezone=self.timezone,
                expression=f"每日 {send_time.strftime('%H:%M')}",
            )

        weekly = re.search(r"(?:每周|每星期|每礼拜)\s*(?:周)?([一二三四五六日天])?", query)
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

        monthly_last = re.search(r"每月(?:的)?(?:最后一天|月底)", query)
        if monthly_last:
            send_time = send_time or time(9, 0)
            return IntentSchedule(
                kind=ScheduleKind.MONTHLY,
                send_time=send_time,
                day_of_month=31,
                timezone=self.timezone,
                expression=f"每月最后一天 {send_time.strftime('%H:%M')}",
            )

        monthly = re.search(rf"每月\s*({NUMBER_TOKEN})\s*(?:日|号)", query)
        if monthly:
            day_of_month = self._parse_number(monthly.group(1))
            if not 1 <= day_of_month <= 31:
                raise ValueError("每月执行日期必须在 1 到 31 日之间")
            send_time = send_time or time(9, 0)
            return IntentSchedule(
                kind=ScheduleKind.MONTHLY,
                send_time=send_time,
                day_of_month=day_of_month,
                timezone=self.timezone,
                expression=f"每月{day_of_month}日 {send_time.strftime('%H:%M')}",
            )

        if re.search(r"今天|今日|明天|明日|后天", query) and re.search(
            r"发送|推送|通知|提醒|汇总|发到|发给", query
        ):
            offset = 2 if "后天" in query else 1 if re.search(r"明天|明日", query) else 0
            target = now.date() + timedelta(days=offset)
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
        period_pattern = r"(?P<period>凌晨|早上|上午|中午|下午|晚上)?"
        colon = re.search(
            rf"{period_pattern}\s*(?P<hour>\d{{1,2}})\s*:\s*(?P<minute>\d{{1,2}})",
            query,
        )
        if colon:
            return IntentParser._clock_value(
                colon.group("period"), int(colon.group("hour")), int(colon.group("minute"))
            )
        chinese = re.search(
            rf"{period_pattern}\s*(?P<hour>{NUMBER_TOKEN})\s*(?:点|时)"
            rf"(?:(?P<half>半)|\s*(?P<minute>{NUMBER_TOKEN})\s*分)?",
            query,
        )
        if chinese:
            hour = IntentParser._parse_number(chinese.group("hour"))
            minute = (
                30
                if chinese.group("half")
                else IntentParser._parse_number(chinese.group("minute") or "0")
            )
            return IntentParser._clock_value(chinese.group("period"), hour, minute)
        return None

    @staticmethod
    def _clock_value(period: str | None, hour: int, minute: int) -> time:
        if period in {"下午", "晚上"} and 1 <= hour < 12:
            hour += 12
        elif period == "中午" and hour < 11:
            hour += 12
        elif period == "凌晨" and hour == 12:
            hour = 0
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("发送时间必须是 00:00 到 23:59 之间的有效时间")
        return time(hour, minute)

    def _parse_topic(self, query: str, region_token: str) -> str:
        working = query
        working = re.sub(
            r"(?:排除|不含|不要|过滤(?:掉)?|剔除)\s*[^，,。；;]+",
            " ",
            working,
        )
        working = re.sub(
            r"20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?\s*(?:至|到|~|—|-)\s*20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?",
            " ",
            working,
        )
        working = re.sub(
            r"20\d{2}年\s*\d{1,2}月\s*(?:至|到|~|—|-)\s*(?:20\d{2}年\s*)?\d{1,2}月",
            " ",
            working,
        )
        working = re.sub(r"20\d{2}年\s*(?:上半年|下半年|第?[一二三四1-4]季度)", " ", working)
        working = re.sub(r"20\d{2}年\s*\d{1,2}月(?:份)?", " ", working)
        working = re.sub(
            rf"(?:最近|近|过去)\s*(?:半\s*年|(?:一|1|一个)?\s*(?:季度|季)|{NUMBER_TOKEN}\s*(?:天|周|个月|月|年))",
            " ",
            working,
        )
        working = re.sub(
            r"(?:今年以来|本年度|今年|去年|本月|上月|本周|上周|今天|今日|昨天|昨日|明天|明日|后天)",
            " ",
            working,
        )
        working = re.sub(r"(?:每天|每日|天天)", " ", working)
        working = re.sub(r"(?:每周|每星期|每礼拜)\s*(?:周)?[一二三四五六日天]?", " ", working)
        working = re.sub(r"每月(?:的)?(?:最后一天|月底)", " ", working)
        working = re.sub(rf"每月\s*{NUMBER_TOKEN}\s*(?:日|号)", " ", working)
        working = re.sub(
            rf"(?:凌晨|早上|上午|中午|下午|晚上)?\s*(?:\d{{1,2}}\s*:\s*\d{{1,2}}|{NUMBER_TOKEN}\s*(?:点|时)(?:半|\s*{NUMBER_TOKEN}\s*分)?)",
            " ",
            working,
        )
        if region_token:
            working = working.replace(region_token, " ")
        for token in NATIONWIDE_TOKENS:
            working = working.replace(token, " ")
        working = re.sub(
            r"(?:汇总后|发送到|推送到|发到|发给|发送|推送|通知|提醒|汇总)(?:给我|我)?",
            " ",
            working,
        )
        working = re.sub(
            r"(?:飞书(?:群|机器人)?|电子邮件|邮件|邮箱|企业微信|企微|钉钉|自定义\s*[Ww]ebhook|[Ww]ebhook|自动化接口|报告中心)",
            " ",
            working,
        )
        working = re.sub(
            r"(?:请帮我|帮我找|帮我查|帮我|请问|请关注|关注|监控|跟踪|查询|查找|搜索|找一下|我想看|我想找|我要看|只看|仅看|只要|仅要|给我|都有哪些|有哪些|请)",
            " ",
            working,
        )
        working = re.sub(r"(?:区域|地区)内?的?|内的|相关的?|发布的?|把|的", " ", working)
        working = re.sub(
            r"(?:采购意向|公开招标|竞争性磋商|竞争性谈判|询价|招投标|招标|采购|标讯|中标|成交|更正|变更|澄清|结果|合同)(?:公告|项目|信息)?",
            " ",
            working,
        )
        working = re.sub(r"(?:公告|项目|信息|清单|列表)", " ", working)
        working = re.sub(r"[{}【】\[\]()（）]", " ", working)
        working = re.sub(r"[，,。；;：:、/与和或\s]+", "", working)
        working = re.sub(r"^(?:的|相关)+|(?:的|相关)+$", "", working)
        return working or "招投标"

    @staticmethod
    def _expand_keywords(topic: str) -> list[str]:
        expanded = [topic]
        topic_folded = topic.casefold()
        for key, synonyms in TOPIC_SYNONYMS.items():
            if key.casefold() in topic_folded or any(
                synonym.casefold() in topic_folded for synonym in synonyms
            ):
                expanded.extend((key, *synonyms))
        pieces = [p for p in re.split(r"[、/与和或 ]+", topic) if len(p) >= 2]
        expanded.extend(pieces)
        return list(dict.fromkeys(expanded))

    @staticmethod
    def _parse_exclude_keywords(query: str) -> list[str]:
        exclusions: list[str] = []
        for match in re.finditer(r"(?:排除|不含|不要|过滤(?:掉)?|剔除)\s*([^，,。；;]+)", query):
            clause = re.split(
                r"(?:每天|每日|每周|每月|发送|推送|通知|提醒|汇总)",
                match.group(1),
                maxsplit=1,
            )[0]
            pieces = re.split(r"[、,，/]|以及|或者|并且|和|与|或", clause)
            for piece in pieces:
                cleaned = re.sub(
                    r"(?:的)?(?:相关)?(?:项目|公告|信息)$",
                    "",
                    piece.strip(),
                ).strip()
                if cleaned:
                    exclusions.append(cleaned)
        return list(dict.fromkeys(exclusions))

    @staticmethod
    def _parse_event_types(query: str) -> list[EventType]:
        query = re.sub(
            r"(?:排除|不含|不要|过滤(?:掉)?|剔除)\s*[^，,。；;]+",
            " ",
            query,
        )
        event_types: list[EventType] = []
        patterns = (
            (EventType.INTENTION, r"采购意向"),
            (
                EventType.TENDER,
                r"公开招标|竞争性磋商|竞争性谈判|询价公告|招标公告|采购公告|"
                r"(?:只|仅)(?:看|要|查|保留)?\s*(?:招标|采购)(?:公告)?|"
                r"招标(?=\s*(?:和|与|、|及|/)\s*(?:中标|成交|更正|变更|澄清|合同|采购意向))",
            ),
            (EventType.CHANGE, r"更正|变更|澄清"),
            (EventType.AWARD, r"中标|成交|结果公告"),
            (EventType.CONTRACT, r"合同公告|采购合同"),
        )
        for event_type, pattern in patterns:
            if re.search(pattern, query):
                event_types.append(event_type)
        return event_types

    @staticmethod
    def _parse_delivery_channel(query: str) -> str:
        if "飞书" in query:
            return "feishu"
        if "邮件" in query or "邮箱" in query:
            return "email"
        if "企业微信" in query or "企微" in query:
            return "wecom_webhook"
        if "钉钉" in query:
            return "dingtalk_webhook"
        if re.search(r"自定义\s*webhook|自动化接口", query, re.IGNORECASE):
            return "generic_webhook"
        return "local"


def parse_intent(query: str, now: datetime | None = None) -> TenderQuerySpec:
    return IntentParser().parse(query, now=now)
