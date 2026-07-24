from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bidpilot.config import Settings
from bidpilot.intent import CITY_REGIONS, NATIONWIDE_TOKENS, REGIONS, IntentParser
from bidpilot.models import (
    EventType,
    IntentComparison,
    IntentFieldDecision,
    IntentResolution,
    IntentSchedule,
    ScheduleKind,
    TenderQuerySpec,
)

LLMRequester = Callable[[dict[str, Any]], Awaitable[str]]

_FIELD_LABELS = {
    "topic": "主题",
    "region": "地域",
    "time_range": "时间范围",
    "schedule": "执行计划",
    "delivery_channel": "投递通道",
    "exclude_keywords": "排除词",
    "event_types": "公告类型",
}
_CHANNELS = {
    "local",
    "feishu",
    "feishu_webhook",
    "feishu_app",
    "email",
    "dingtalk_webhook",
    "wecom_webhook",
    "generic_webhook",
    "telegram_bot",
    "slack_webhook",
}
_CHANNEL_MARKERS = {
    "local": ("本地", "报告中心"),
    "feishu": ("飞书",),
    "feishu_webhook": ("飞书", "机器人"),
    "feishu_app": ("飞书", "应用"),
    "email": ("邮件", "邮箱"),
    "dingtalk_webhook": ("钉钉",),
    "wecom_webhook": ("企业微信", "企微"),
    "generic_webhook": ("webhook", "自动化接口"),
    "telegram_bot": ("telegram", "电报"),
    "slack_webhook": ("slack",),
}
_TIME_GROUNDING = re.compile(
    r"20\d{2}|最近|近\s*[一二两三四五六七八九十百\d]|过去|"
    r"今年|去年|本月|上月|本周|上周|今天|今日|昨天|昨日|季度|半年"
)
_SCHEDULE_GROUNDING = re.compile(
    r"每天|每日|天天|每周|每星期|每礼拜|每月|月底|最后一天|"
    r"明天|明日|后天|发送|推送|通知|提醒"
)
_NEGATIVE_MARKERS = re.compile(r"排除|不含|不要|过滤|剔除")
_RESTRICTIVE_EVENT_MARKERS = re.compile(r"只看|仅看|只要|仅要|限定|类型为")


class _LLMScheduleProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["immediate", "once", "daily", "weekly", "monthly"]
    send_time: str | None = None
    weekday: int | None = Field(default=None, ge=0, le=6)
    day_of_month: int | None = Field(default=None, ge=1, le=31)
    run_at: str | None = None


class _LLMConfidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: float | None = Field(default=None, ge=0, le=1)
    region: float | None = Field(default=None, ge=0, le=1)
    time_range: float | None = Field(default=None, ge=0, le=1)
    start_date: float | None = Field(default=None, ge=0, le=1)
    end_date: float | None = Field(default=None, ge=0, le=1)
    schedule: float | None = Field(default=None, ge=0, le=1)
    delivery_channel: float | None = Field(default=None, ge=0, le=1)
    exclude_keywords: float | None = Field(default=None, ge=0, le=1)
    event_types: float | None = Field(default=None, ge=0, le=1)

    def value_for(self, field: str) -> float | None:
        direct = getattr(self, field, None)
        if direct is not None:
            return direct
        if field == "time_range" and self.start_date is not None and self.end_date is not None:
            return min(self.start_date, self.end_date)
        return None


class _LLMIntentProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str | None = Field(default=None, min_length=2, max_length=60)
    region: str | None = Field(default=None, max_length=30)
    start_date: str | None = None
    end_date: str | None = None
    schedule: _LLMScheduleProposal | None = None
    delivery_channel: (
        Literal[
            "local",
            "feishu",
            "feishu_webhook",
            "feishu_app",
            "email",
            "dingtalk_webhook",
            "wecom_webhook",
            "generic_webhook",
            "telegram_bot",
            "slack_webhook",
        ]
        | None
    ) = None
    exclude_keywords: list[str] | None = Field(default=None, max_length=20)
    event_types: list[EventType] | None = Field(default=None, max_length=6)
    confidence: _LLMConfidence = Field(description="各提议字段的 0～1 置信度")
    rationale: str = Field(min_length=1, max_length=500)


class _InvalidLLMResponse(RuntimeError):
    pass


class HybridIntentEngine:
    """规则优先、LLM 提议、本地逐字段校验的混合意图引擎。"""

    version = "hybrid-v1"

    def __init__(
        self,
        settings: Settings,
        parser: IntentParser | None = None,
        requester: LLMRequester | None = None,
    ):
        self.settings = settings
        self.parser = parser or IntentParser(settings.timezone)
        self.requester = requester
        self._metrics = {
            "requests": 0,
            "applied": 0,
            "confirmed": 0,
            "fallbacks": 0,
            "invalid_responses": 0,
        }

    @property
    def metrics(self) -> dict[str, int | str | float]:
        return {
            **self._metrics,
            "mode": self.settings.intent_llm_mode,
            "confidence_threshold": self.settings.intent_llm_confidence_threshold,
        }

    async def resolve(self, query: str, now: datetime | None = None) -> TenderQuerySpec:
        baseline = self.parser.parse(query, now=now)
        effective_now = now or datetime.now(ZoneInfo(self.settings.timezone))
        if effective_now.tzinfo is None:
            effective_now = effective_now.replace(tzinfo=ZoneInfo(self.settings.timezone))
        return await self._resolve_baseline(baseline, effective_now)

    async def compare(self, query: str, now: datetime | None = None) -> IntentComparison:
        baseline = self.parser.parse(query, now=now)
        effective_now = now or datetime.now(ZoneInfo(self.settings.timezone))
        if effective_now.tzinfo is None:
            effective_now = effective_now.replace(tzinfo=ZoneInfo(self.settings.timezone))
        resolved = await self._resolve_baseline(baseline, effective_now)
        rule_copy = baseline.model_copy(deep=True)
        rule_copy.resolution = IntentResolution(summary="这是确定性规则基线，尚未应用 LLM 提议。")
        fields = (
            "topic",
            "keywords",
            "exclude_keywords",
            "event_types",
            "region",
            "region_code",
            "region_level",
            "start_date",
            "end_date",
            "schedule",
            "delivery_channel",
        )
        changed = [
            field for field in fields if getattr(rule_copy, field) != getattr(resolved, field)
        ]
        return IntentComparison(rules=rule_copy, resolved=resolved, changed_fields=changed)

    async def _resolve_baseline(
        self,
        baseline: TenderQuerySpec,
        now: datetime,
    ) -> TenderQuerySpec:
        result = baseline.model_copy(deep=True)
        reasons, repairable = self._trigger_analysis(baseline)
        mode = self.settings.intent_llm_mode
        if mode == "off":
            result.resolution = IntentResolution(
                llm_status="disabled",
                trigger_reasons=reasons,
                summary="LLM 意图辅助已关闭，本次只使用确定性规则。",
            )
            return result
        if mode == "auto" and not reasons:
            result.resolution = IntentResolution(
                llm_status="not_needed",
                summary="规则字段均达到置信门槛，无需调用 LLM。",
            )
            return result
        if mode == "always":
            reasons = [*reasons, "配置要求每次都由 LLM 复核"]

        if not (self.settings.llm_base_url and self.settings.llm_model):
            result.resolution = IntentResolution(
                llm_status="not_configured",
                trigger_reasons=list(dict.fromkeys(reasons)),
                summary="需要智能复核，但模型尚未配置；已安全使用规则结果。",
            )
            self._metrics["fallbacks"] += 1
            return result

        payload = self._request_payload(baseline, now, repairable)
        started = time.perf_counter()
        self._metrics["requests"] += 1
        try:
            content = await (
                self.requester(payload) if self.requester else self._request_llm(payload)
            )
            proposal = self._parse_strict_json(content)
        except _InvalidLLMResponse:
            latency = round((time.perf_counter() - started) * 1000)
            self._metrics["fallbacks"] += 1
            self._metrics["invalid_responses"] += 1
            result.resolution = IntentResolution(
                llm_status="invalid_response",
                trigger_reasons=list(dict.fromkeys(reasons)),
                latency_ms=latency,
                summary="模型没有返回符合契约的严格 JSON；已安全回退规则结果。",
            )
            return result
        except Exception:
            latency = round((time.perf_counter() - started) * 1000)
            self._metrics["fallbacks"] += 1
            result.resolution = IntentResolution(
                llm_status="unavailable",
                trigger_reasons=list(dict.fromkeys(reasons)),
                latency_ms=latency,
                summary="模型请求超时或不可用；任务未中断，已安全使用规则结果。",
            )
            return result

        latency = round((time.perf_counter() - started) * 1000)
        decisions, accepted, confirmed = self._validate_and_merge(
            result,
            proposal,
            repairable,
            now,
        )
        if accepted:
            result.parser_version = self.version
            result.resolution = IntentResolution(
                mode="hybrid",
                llm_status="applied",
                trigger_reasons=list(dict.fromkeys(reasons)),
                decisions=decisions,
                latency_ms=latency,
                summary=f"LLM 提议通过本地校验，安全修正 {accepted} 个字段。",
            )
            self._metrics["applied"] += 1
        elif confirmed:
            result.parser_version = self.version
            result.resolution = IntentResolution(
                mode="hybrid",
                llm_status="confirmed",
                trigger_reasons=list(dict.fromkeys(reasons)),
                decisions=decisions,
                latency_ms=latency,
                summary="LLM 复核结果与规则一致，没有改动已确认字段。",
            )
            self._metrics["confirmed"] += 1
        else:
            result.resolution = IntentResolution(
                llm_status="rejected",
                trigger_reasons=list(dict.fromkeys(reasons)),
                decisions=decisions,
                latency_ms=latency,
                summary="模型提议未通过本地校验；已完整保留规则结果。",
            )
            self._metrics["fallbacks"] += 1
        self._refresh_warnings(result)
        return result

    def _trigger_analysis(self, baseline: TenderQuerySpec) -> tuple[list[str], set[str]]:
        reasons: list[str] = []
        repairable: set[str] = set()
        threshold = self.settings.intent_llm_confidence_threshold
        for field in ("topic", "region", "time_range", "schedule"):
            score = baseline.slot_confidence.get(field, 0.0)
            if score < threshold:
                reasons.append(f"{_FIELD_LABELS[field]}置信度 {score:.2f} 低于阈值 {threshold:.2f}")
                repairable.add(field)

        warning_fields = {
            "未识别到明确地域": "region",
            "未识别到明确时间": "time_range",
            "未识别到明确主题": "topic",
            "未识别到发送时间": "schedule",
        }
        for warning in baseline.warnings:
            for marker, field in warning_fields.items():
                if marker in warning:
                    reasons.append(warning.rstrip("。"))
                    repairable.add(field)

        region_mentions = self._region_mentions(baseline.raw_query)
        if len(region_mentions) > 1:
            reasons.append("原问题同时出现多个不同地域，字段存在冲突")
            repairable.add("region")

        relative_ranges = re.findall(
            r"(?:最近|近|过去)\s*[〇零一二两三四五六七八九十百\d]+\s*(?:天|日|周|个月|月|年)",
            baseline.raw_query,
        )
        if len(set(relative_ranges)) > 1:
            reasons.append("原问题同时出现多个时间范围，字段存在冲突")
            repairable.add("time_range")

        explicit = re.search(
            r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?\s*(?:至|到|~|—|-)\s*"
            r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?",
            baseline.raw_query,
        )
        if explicit:
            try:
                left = date(*map(int, explicit.group(1, 2, 3)))
                right = date(*map(int, explicit.group(4, 5, 6)))
                if left > right:
                    reasons.append("原问题的日期先后顺序相反，需复核规则自动纠正")
                    repairable.add("time_range")
            except ValueError:
                pass

        if _NEGATIVE_MARKERS.search(baseline.raw_query) and not baseline.exclude_keywords:
            reasons.append("检测到排除表达但规则没有提取排除词")
            repairable.add("exclude_keywords")
        if _RESTRICTIVE_EVENT_MARKERS.search(baseline.raw_query) and not baseline.event_types:
            reasons.append("检测到公告类型限定但规则没有提取枚举")
            repairable.add("event_types")
        if baseline.delivery_channel == "local" and re.search(
            r"飞书|邮件|邮箱|钉钉|企业微信|企微|webhook|自动化接口|telegram|电报|slack",
            baseline.raw_query,
            re.IGNORECASE,
        ):
            reasons.append("检测到投递表达但规则仍为本地通道")
            repairable.add("delivery_channel")
        return list(dict.fromkeys(reasons)), repairable

    def _request_payload(
        self,
        baseline: TenderQuerySpec,
        now: datetime,
        repairable: set[str],
    ) -> dict[str, Any]:
        schema = _LLMIntentProposal.model_json_schema()
        baseline_data = baseline.model_dump(mode="json", exclude={"resolution"})
        return {
            "model": self.settings.llm_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是中文招投标意图复核器。规则结果是主结果，你只能提出修复建议。"
                        "只输出一个 JSON 对象，禁止 Markdown、代码围栏、解释前后缀和额外字段。"
                        "不要输出 region_code；不要猜测原问题没有出现的主题、地域、日期、排除词或渠道。"
                        "无法确认的字段填 null。weekday 使用周一=0 至周日=6。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "current_time": now.isoformat(),
                            "timezone": self.settings.timezone,
                            "original_query": baseline.raw_query,
                            "rule_baseline": baseline_data,
                            "fields_allowed_for_repair": sorted(repairable),
                            "allowed_delivery_channels": sorted(_CHANNELS),
                            "required_json_schema": schema,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "temperature": 0,
            "max_tokens": 1200,
            "response_format": {"type": "json_object"},
        }

    async def _request_llm(self, payload: dict[str, Any]) -> str:
        endpoint = self.settings.llm_base_url.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        async with httpx.AsyncClient(timeout=self.settings.llm_timeout) as client:
            response = await client.post(endpoint, headers=headers, json=payload)
            if response.status_code in {400, 422} and "response_format" in payload:
                compatible_payload = {k: v for k, v in payload.items() if k != "response_format"}
                response = await client.post(endpoint, headers=headers, json=compatible_payload)
            response.raise_for_status()
            data = response.json()
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise _InvalidLLMResponse from exc

    @staticmethod
    def _parse_strict_json(content: str) -> _LLMIntentProposal:
        stripped = content.strip()
        if not stripped.startswith("{") or not stripped.endswith("}") or "```" in stripped:
            raise _InvalidLLMResponse
        try:
            data = json.loads(stripped)
            if not isinstance(data, dict):
                raise _InvalidLLMResponse
            return _LLMIntentProposal.model_validate(data)
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
            raise _InvalidLLMResponse from exc

    def _validate_and_merge(
        self,
        result: TenderQuerySpec,
        proposal: _LLMIntentProposal,
        repairable: set[str],
        now: datetime,
    ) -> tuple[list[IntentFieldDecision], int, int]:
        decisions: list[IntentFieldDecision] = []
        accepted = 0
        confirmed = 0

        def confidence_ok(field: str) -> tuple[bool, str]:
            value = proposal.confidence.value_for(field)
            if value is None:
                return False, "模型没有提供该字段置信度"
            if not 0 <= value <= 1:
                return False, "模型置信度不在 0～1 范围"
            if value < 0.6:
                return False, f"模型自报置信度 {value:.2f} 低于最低接受线 0.60"
            return True, ""

        def add_decision(
            field: str,
            outcome: Literal["accepted", "rejected", "locked", "unchanged"],
            rule_value: Any,
            proposed_value: Any,
            final_value: Any,
            reason: str,
        ) -> None:
            decisions.append(
                IntentFieldDecision(
                    field=field,
                    outcome=outcome,
                    rule_value=self._json_value(rule_value),
                    proposed_value=self._json_value(proposed_value),
                    final_value=self._json_value(final_value),
                    reason=reason,
                )
            )

        if "topic" in proposal.model_fields_set and proposal.topic is not None:
            old = result.topic
            candidate = proposal.topic.strip()
            allowed, reason = confidence_ok("topic")
            if not allowed:
                add_decision("topic", "rejected", old, candidate, old, reason)
            elif not self._topic_grounded(candidate, result.raw_query):
                add_decision(
                    "topic",
                    "rejected",
                    old,
                    candidate,
                    old,
                    "主题不是原问题中的连续有效短语，拒绝模型补写",
                )
            elif "topic" not in repairable and candidate != old:
                add_decision(
                    "topic",
                    "locked",
                    old,
                    candidate,
                    old,
                    "规则主题达到置信门槛，禁止模型覆盖",
                )
            elif candidate == old:
                add_decision("topic", "unchanged", old, candidate, old, "模型与规则主题一致")
                confirmed += 1
            else:
                result.topic = candidate
                result.keywords = self.parser._expand_keywords(candidate)
                result.slot_confidence["topic"] = min(
                    0.95, proposal.confidence.value_for("topic") or 0.6
                )
                add_decision(
                    "topic", "accepted", old, candidate, candidate, "主题在原问题中且通过长度校验"
                )
                accepted += 1

        if "region" in proposal.model_fields_set:
            old = result.region
            candidate = proposal.region.strip() if proposal.region else None
            allowed, reason = confidence_ok("region")
            resolution = self._resolve_region_candidate(candidate, result.raw_query)
            if not allowed:
                add_decision("region", "rejected", old, candidate, old, reason)
            elif resolution is None:
                add_decision(
                    "region",
                    "rejected",
                    old,
                    candidate,
                    old,
                    "地域不在本地行政区词表、未出现在原问题，或原问题含多个地域",
                )
            else:
                region, code, level, matched_token = resolution
                if "region" not in repairable and region != old:
                    add_decision(
                        "region",
                        "locked",
                        old,
                        candidate,
                        old,
                        "规则地域达到置信门槛，禁止模型覆盖",
                    )
                elif region == old and code == result.region_code:
                    add_decision(
                        "region", "unchanged", old, candidate, old, "模型与本地地域解析一致"
                    )
                    confirmed += 1
                else:
                    result.region = region
                    result.region_code = code
                    result.region_level = level
                    result.slot_confidence["region"] = min(
                        0.98, proposal.confidence.value_for("region") or 0.6
                    )
                    add_decision(
                        "region",
                        "accepted",
                        old,
                        candidate,
                        region,
                        "地域名称在原问题中，本地词表已派生层级和省级来源代码",
                    )
                    accepted += 1
                    if matched_token and matched_token in result.topic:
                        repaired_topic = self.parser._parse_topic(result.raw_query, matched_token)
                        if repaired_topic != result.topic:
                            previous_topic = result.topic
                            result.topic = repaired_topic
                            result.keywords = self.parser._expand_keywords(repaired_topic)
                            add_decision(
                                "topic",
                                "accepted",
                                previous_topic,
                                None,
                                repaired_topic,
                                "接受地域后，用确定性规则从主题中移除地域词",
                            )
                            accepted += 1

        date_fields_set = bool({"start_date", "end_date"} & proposal.model_fields_set)
        if date_fields_set:
            old = [result.start_date.isoformat(), result.end_date.isoformat()]
            proposed = [proposal.start_date, proposal.end_date]
            allowed, reason = confidence_ok("time_range")
            parsed_range = self._validate_date_range(
                proposal.start_date,
                proposal.end_date,
                result.raw_query,
                now.date(),
            )
            if not allowed:
                add_decision("time_range", "rejected", old, proposed, old, reason)
            elif parsed_range is None:
                add_decision(
                    "time_range",
                    "rejected",
                    old,
                    proposed,
                    old,
                    "日期必须成对、为有效 ISO 日期、顺序正确、范围有界且原问题含时间表达",
                )
            elif "time_range" not in repairable and list(parsed_range) != [
                result.start_date,
                result.end_date,
            ]:
                add_decision(
                    "time_range",
                    "locked",
                    old,
                    proposed,
                    old,
                    "规则时间范围达到置信门槛，禁止模型覆盖",
                )
            elif list(parsed_range) == [result.start_date, result.end_date]:
                add_decision("time_range", "unchanged", old, proposed, old, "模型与规则日期一致")
                confirmed += 1
            else:
                result.start_date, result.end_date = parsed_range
                result.slot_confidence["time_range"] = min(
                    0.95, proposal.confidence.value_for("time_range") or 0.6
                )
                final_range = [result.start_date.isoformat(), result.end_date.isoformat()]
                add_decision(
                    "time_range",
                    "accepted",
                    old,
                    proposed,
                    final_range,
                    "日期通过 ISO、顺序、跨度、时界和原句依据校验",
                )
                accepted += 1

        if "schedule" in proposal.model_fields_set and proposal.schedule is not None:
            old = result.schedule.model_dump(mode="json")
            proposed = proposal.schedule.model_dump(mode="json")
            allowed, reason = confidence_ok("schedule")
            schedule = self._validate_schedule(proposal.schedule, result.raw_query, now)
            if not allowed:
                add_decision("schedule", "rejected", old, proposed, old, reason)
            elif schedule is None:
                add_decision(
                    "schedule",
                    "rejected",
                    old,
                    proposed,
                    old,
                    "计划缺少必填字段、时间非法、一次性时间已过期或原问题没有计划依据",
                )
            elif "schedule" not in repairable and schedule != result.schedule:
                add_decision(
                    "schedule",
                    "locked",
                    old,
                    proposed,
                    old,
                    "规则计划达到置信门槛，禁止模型覆盖",
                )
            elif schedule == result.schedule:
                add_decision("schedule", "unchanged", old, proposed, old, "模型与规则计划一致")
                confirmed += 1
            else:
                result.schedule = schedule
                result.slot_confidence["schedule"] = min(
                    0.95, proposal.confidence.value_for("schedule") or 0.6
                )
                add_decision(
                    "schedule",
                    "accepted",
                    old,
                    proposed,
                    schedule.model_dump(mode="json"),
                    "计划经本地 Pydantic 与未来时间校验",
                )
                accepted += 1

        if (
            "delivery_channel" in proposal.model_fields_set
            and proposal.delivery_channel is not None
        ):
            old = result.delivery_channel
            candidate = proposal.delivery_channel
            allowed, reason = confidence_ok("delivery_channel")
            if not allowed:
                add_decision("delivery_channel", "rejected", old, candidate, old, reason)
            elif not self._channel_grounded(candidate, result.raw_query):
                add_decision(
                    "delivery_channel",
                    "rejected",
                    old,
                    candidate,
                    old,
                    "通道枚举未在原问题中出现",
                )
            elif "delivery_channel" not in repairable and candidate != old:
                add_decision(
                    "delivery_channel",
                    "locked",
                    old,
                    candidate,
                    old,
                    "规则通道已明确，禁止模型覆盖",
                )
            elif candidate == old or {candidate, old} <= {
                "feishu",
                "feishu_webhook",
                "feishu_app",
            }:
                add_decision(
                    "delivery_channel",
                    "unchanged",
                    old,
                    candidate,
                    old,
                    "模型与规则通道语义一致",
                )
                confirmed += 1
            else:
                result.delivery_channel = candidate
                add_decision(
                    "delivery_channel",
                    "accepted",
                    old,
                    candidate,
                    candidate,
                    "通道为允许枚举且在原问题中有明确依据",
                )
                accepted += 1

        if (
            "exclude_keywords" in proposal.model_fields_set
            and proposal.exclude_keywords is not None
        ):
            old = result.exclude_keywords
            candidate = list(
                dict.fromkeys(item.strip() for item in proposal.exclude_keywords if item.strip())
            )
            allowed, reason = confidence_ok("exclude_keywords")
            grounded = bool(_NEGATIVE_MARKERS.search(result.raw_query)) and all(
                self._compact(item) in self._compact(result.raw_query) for item in candidate
            )
            if not allowed:
                add_decision("exclude_keywords", "rejected", old, candidate, old, reason)
            elif not grounded:
                add_decision(
                    "exclude_keywords",
                    "rejected",
                    old,
                    candidate,
                    old,
                    "排除词必须逐项出现在含否定标记的原问题中",
                )
            elif "exclude_keywords" not in repairable and candidate != old:
                add_decision(
                    "exclude_keywords",
                    "locked",
                    old,
                    candidate,
                    old,
                    "规则排除词已明确，禁止模型覆盖",
                )
            elif candidate == old:
                add_decision(
                    "exclude_keywords", "unchanged", old, candidate, old, "模型与规则排除词一致"
                )
                confirmed += 1
            else:
                result.exclude_keywords = candidate
                add_decision(
                    "exclude_keywords",
                    "accepted",
                    old,
                    candidate,
                    candidate,
                    "排除词逐项在原问题中且存在明确否定标记",
                )
                accepted += 1

        if "event_types" in proposal.model_fields_set and proposal.event_types is not None:
            old = result.event_types
            candidate = list(dict.fromkeys(proposal.event_types))
            allowed, reason = confidence_ok("event_types")
            grounded = bool(_RESTRICTIVE_EVENT_MARKERS.search(result.raw_query)) and all(
                item.value.removesuffix("公告") in result.raw_query for item in candidate
            )
            if not allowed:
                add_decision("event_types", "rejected", old, candidate, old, reason)
            elif not grounded:
                add_decision(
                    "event_types",
                    "rejected",
                    old,
                    candidate,
                    old,
                    "公告类型需要原问题中的限制词和对应类型名称共同佐证",
                )
            elif "event_types" not in repairable and candidate != old:
                add_decision(
                    "event_types",
                    "locked",
                    old,
                    candidate,
                    old,
                    "规则公告类型已明确，禁止模型覆盖",
                )
            elif candidate == old:
                add_decision(
                    "event_types", "unchanged", old, candidate, old, "模型与规则公告类型一致"
                )
                confirmed += 1
            else:
                result.event_types = candidate
                add_decision(
                    "event_types",
                    "accepted",
                    old,
                    candidate,
                    candidate,
                    "公告类型属于允许枚举且有原句限制依据",
                )
                accepted += 1

        if not decisions:
            decisions.append(
                IntentFieldDecision(
                    field="all",
                    outcome="rejected",
                    reason="模型没有提交任何可校验的非空字段",
                )
            )
        return decisions, accepted, confirmed

    @staticmethod
    def _compact(value: str) -> str:
        return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", value).casefold()

    def _topic_grounded(self, topic: str, query: str) -> bool:
        compact_topic = self._compact(topic)
        compact_query = self._compact(query)
        if not 2 <= len(compact_topic) <= 40 or compact_topic not in compact_query:
            return False
        return not bool(re.search(r"每天|每周|每月|发送|推送|帮我|查询", topic))

    @staticmethod
    def _region_hits(query: str) -> list[tuple[str, str, str, bool]]:
        """Prefer longest non-overlapping names and collapse province+city pairs."""
        tokens = {**REGIONS, **CITY_REGIONS}
        occupied: list[tuple[int, int]] = []
        hits: list[tuple[str, str, str, bool]] = []
        for token in sorted(tokens, key=len, reverse=True):
            for match in re.finditer(re.escape(token), query):
                span = match.span()
                if any(span[0] < end and start < span[1] for start, end in occupied):
                    continue
                canonical, code = tokens[token]
                hits.append((canonical, code, token, token in CITY_REGIONS))
                occupied.append(span)
        city_codes = {code for _canonical, code, _token, is_city in hits if is_city}
        return [hit for hit in hits if hit[3] or hit[1] not in city_codes]

    @classmethod
    def _region_mentions(cls, query: str) -> set[str]:
        mentions = {canonical for canonical, _code, _token, _is_city in cls._region_hits(query)}
        if any(token in query for token in NATIONWIDE_TOKENS):
            mentions.add("__nationwide__")
        return mentions

    def _resolve_region_candidate(
        self,
        candidate: str | None,
        query: str,
    ) -> tuple[str | None, str | None, Literal["nationwide", "province", "city"], str] | None:
        mentions = self._region_mentions(query)
        if len(mentions) != 1:
            return None
        if candidate is None:
            if mentions == {"__nationwide__"}:
                token = next(item for item in NATIONWIDE_TOKENS if item in query)
                return None, None, "nationwide", token
            return None
        hits = self._region_hits(query)
        hit = next(
            (
                item
                for item in hits
                if candidate in {item[0], item[2], item[0] + "市", item[0] + "省"}
            ),
            None,
        )
        if hit is None or hit[0] not in mentions:
            return None
        canonical, code, matched_token, is_city = hit
        level: Literal["province", "city"] = "city" if is_city else "province"
        return canonical, code, level, matched_token

    @staticmethod
    def _validate_date_range(
        start_value: str | None,
        end_value: str | None,
        query: str,
        today: date,
    ) -> tuple[date, date] | None:
        if not start_value or not end_value or not _TIME_GROUNDING.search(query):
            return None
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start_value) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", end_value
        ):
            return None
        try:
            start = date.fromisoformat(start_value)
            end = date.fromisoformat(end_value)
        except ValueError:
            return None
        if start > end or (end - start).days > 3650:
            return None
        if start < today - timedelta(days=3653) or end > today + timedelta(days=366):
            return None
        return start, end

    def _validate_schedule(
        self,
        proposal: _LLMScheduleProposal,
        query: str,
        now: datetime,
    ) -> IntentSchedule | None:
        if not _SCHEDULE_GROUNDING.search(query):
            return None

        def parse_clock(value: str | None) -> clock_time | None:
            if value is None or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                return None
            return clock_time.fromisoformat(value)

        try:
            kind = ScheduleKind(proposal.kind)
            if kind == ScheduleKind.IMMEDIATE:
                if any(
                    value is not None
                    for value in (
                        proposal.send_time,
                        proposal.weekday,
                        proposal.day_of_month,
                        proposal.run_at,
                    )
                ):
                    return None
                return IntentSchedule(kind=kind, timezone=self.settings.timezone)
            if kind == ScheduleKind.ONCE:
                if not proposal.run_at or any(
                    value is not None
                    for value in (proposal.send_time, proposal.weekday, proposal.day_of_month)
                ):
                    return None
                run_at = datetime.fromisoformat(proposal.run_at)
                if run_at.tzinfo is None:
                    run_at = run_at.replace(tzinfo=ZoneInfo(self.settings.timezone))
                if run_at <= now:
                    return None
                return IntentSchedule(
                    kind=kind,
                    run_at=run_at,
                    timezone=self.settings.timezone,
                    expression=f"一次性 {run_at.strftime('%Y-%m-%d %H:%M')}",
                )
            send_time = parse_clock(proposal.send_time)
            if send_time is None or proposal.run_at is not None:
                return None
            if kind == ScheduleKind.DAILY:
                if proposal.weekday is not None or proposal.day_of_month is not None:
                    return None
                return IntentSchedule(
                    kind=kind,
                    send_time=send_time,
                    timezone=self.settings.timezone,
                    expression=f"每日 {send_time.strftime('%H:%M')}",
                )
            if kind == ScheduleKind.WEEKLY:
                if proposal.weekday is None or proposal.day_of_month is not None:
                    return None
                weekday_names = "一二三四五六日"
                return IntentSchedule(
                    kind=kind,
                    send_time=send_time,
                    weekday=proposal.weekday,
                    timezone=self.settings.timezone,
                    expression=(
                        f"每周{weekday_names[proposal.weekday]} {send_time.strftime('%H:%M')}"
                    ),
                )
            if proposal.day_of_month is None or proposal.weekday is not None:
                return None
            label = "最后一天" if proposal.day_of_month == 31 else f"{proposal.day_of_month} 日"
            return IntentSchedule(
                kind=kind,
                send_time=send_time,
                day_of_month=proposal.day_of_month,
                timezone=self.settings.timezone,
                expression=f"每月{label} {send_time.strftime('%H:%M')}",
            )
        except (ValueError, ValidationError, IndexError):
            return None

    @staticmethod
    def _channel_grounded(channel: str, query: str) -> bool:
        if channel not in _CHANNELS:
            return False
        folded = query.casefold()
        return any(marker.casefold() in folded for marker in _CHANNEL_MARKERS[channel])

    @staticmethod
    def _refresh_warnings(result: TenderQuerySpec) -> None:
        warnings = list(result.warnings)
        if result.region is not None or any(
            token in result.raw_query for token in NATIONWIDE_TOKENS
        ):
            warnings = [item for item in warnings if "未识别到明确地域" not in item]
        if result.slot_confidence.get("time_range", 0) >= 0.6:
            warnings = [item for item in warnings if "未识别到明确时间" not in item]
        if result.topic not in {"招标", "采购", "招投标"} and len(result.topic) >= 2:
            warnings = [item for item in warnings if "未识别到明确主题" not in item]
        if result.schedule.send_time is not None:
            warnings = [item for item in warnings if "未识别到发送时间" not in item]
        result.warnings = list(dict.fromkeys(warnings))

    @staticmethod
    def _json_value(value: Any) -> Any:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        if isinstance(value, datetime | date | clock_time):
            return value.isoformat()
        if isinstance(value, list):
            return [HybridIntentEngine._json_value(item) for item in value]
        if isinstance(value, dict):
            return {key: HybridIntentEngine._json_value(item) for key, item in value.items()}
        if hasattr(value, "value"):
            return value.value
        return value
