from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bidpilot.clean import normalize_space
from bidpilot.config import Settings
from bidpilot.models import (
    BriefAction,
    BriefClaim,
    BriefEvidence,
    BriefPriority,
    BriefRisk,
    IntelligenceBrief,
    TenderQuerySpec,
    TenderRecord,
)


class InvalidIntelligenceResponse(ValueError):
    pass


class _ClaimProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=2, max_length=360)
    evidence_ids: list[str] = Field(min_length=1, max_length=5)


class _PriorityProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    reason: str = Field(min_length=2, max_length=360)
    recommended_action: str = Field(min_length=2, max_length=360)


class _RiskProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: Literal["high", "medium", "low"]
    text: str = Field(min_length=2, max_length=360)
    evidence_ids: list[str] = Field(min_length=1, max_length=5)


class _ActionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    priority: Literal["P0", "P1", "P2"]
    text: str = Field(min_length=2, max_length=360)
    evidence_ids: list[str] = Field(min_length=1, max_length=5)


class _BriefProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overview: str = Field(min_length=2, max_length=500)
    buyer_needs: list[_ClaimProposal] = Field(default_factory=list, max_length=6)
    priorities: list[_PriorityProposal] = Field(default_factory=list, min_length=1, max_length=5)
    risks: list[_RiskProposal] = Field(default_factory=list, max_length=6)
    actions: list[_ActionProposal] = Field(default_factory=list, min_length=1, max_length=6)


class IntelligenceBriefGenerator:
    """Create an evidence-ID-bound brief with a deterministic safe fallback."""

    version = "intelligence-v1"

    def __init__(self, settings: Settings):
        self.settings = settings

    def cache_key(self, spec: TenderQuerySpec, records: list[TenderRecord]) -> str:
        material = {
            "version": self.version,
            "mode": self.settings.intelligence_brief_mode,
            "model": self.settings.llm_model,
            "max_records": self.settings.intelligence_brief_max_records,
            "scope": {
                "topic": spec.topic,
                "region": spec.region,
                "start_date": spec.start_date.isoformat(),
                "end_date": spec.end_date.isoformat(),
            },
            "records": sorted((record.canonical_id, record.version_hash) for record in records),
        }
        payload = json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    async def generate(
        self,
        spec: TenderQuerySpec,
        records: list[TenderRecord],
    ) -> IntelligenceBrief:
        catalog = self._catalog(records)
        if not catalog:
            return self._empty_brief()
        if self.settings.intelligence_brief_mode == "off":
            return self._fallback(
                catalog,
                status="disabled",
                summary="AI 情报简报已关闭；已使用本地评分和公告阶段生成确定性建议。",
            )
        if not (self.settings.llm_base_url and self.settings.llm_model):
            return self._fallback(
                catalog,
                status="not_configured",
                summary="模型尚未配置；已使用本地评分和公告阶段生成确定性建议。",
            )

        request_input = self._request_input(spec, catalog)
        started = time.perf_counter()
        correction: str | None = None
        for attempt in range(2):
            try:
                content = await self._request(self._payload(request_input, correction=correction))
                proposal = _BriefProposal.model_validate(json.loads(content))
                brief = self._hydrate(proposal, catalog, request_input)
                latency_ms = round((time.perf_counter() - started) * 1000)
                return brief.model_copy(
                    update={
                        "latency_ms": latency_ms,
                        "repair_count": attempt,
                        "summary": (
                            f"模型仅基于 {len(catalog)} 条本地证据生成建议；"
                            "所有标题、采购人、评分和链接均由本地证据目录回填。"
                            + (" 首次输出被证据门拒绝后已自动修复 1 次。" if attempt else "")
                        ),
                    }
                )
            except json.JSONDecodeError:
                correction = "上次输出不是合法 JSON 对象；请重新严格按 Schema 输出。"
            except ValidationError:
                correction = "上次字段、枚举或数量不符合 JSON Schema；请删除额外字段并重新输出。"
            except InvalidIntelligenceResponse as exc:
                correction = f"上次未通过本地证据校验：{exc}。请重新输出。"
            except Exception:
                latency_ms = round((time.perf_counter() - started) * 1000)
                return self._fallback(
                    catalog,
                    status="unavailable",
                    latency_ms=latency_ms,
                    summary="模型服务超时或不可用；检索任务未中断，已生成确定性建议。",
                )
        latency_ms = round((time.perf_counter() - started) * 1000)
        return self._fallback(
            catalog,
            status="invalid_response",
            latency_ms=latency_ms,
            summary="模型连续两次未通过严格 JSON、证据编号或数字校验；已整体回退确定性建议。",
        )

    def _catalog(self, records: list[TenderRecord]) -> list[BriefEvidence]:
        ranked = sorted(
            records,
            key=lambda item: (
                item.opportunity_score,
                item.relevance_score,
                item.published_at,
            ),
            reverse=True,
        )[: self.settings.intelligence_brief_max_records]
        catalog = []
        for index, record in enumerate(ranked, start=1):
            excerpt = ""
            if record.evidence:
                excerpt = record.evidence[0].text
            excerpt = normalize_space(excerpt or record.body_excerpt or record.summary)[:500]
            catalog.append(
                BriefEvidence(
                    evidence_id=f"E{index:02d}",
                    record_id=record.canonical_id,
                    title=record.title,
                    buyer=record.buyer,
                    published_at=record.published_at,
                    event_type=record.event_type.value,
                    opportunity_score=record.opportunity_score,
                    source_url=record.source_urls[0],
                    excerpt=excerpt,
                )
            )
        return catalog

    def _empty_brief(self) -> IntelligenceBrief:
        return IntelligenceBrief(
            mode="deterministic",
            status="empty",
            overview="本轮没有通过证据核验的标讯，因此不生成采购需求、优先机会或行动建议。",
            generated_at=datetime.now(ZoneInfo(self.settings.timezone)),
            summary="零结果也是可信结论；请结合来源覆盖和过滤诊断决定是否调整查询。",
        )

    def _fallback(
        self,
        catalog: list[BriefEvidence],
        *,
        status: Literal["disabled", "not_configured", "invalid_response", "unavailable"],
        summary: str,
        latency_ms: int = 0,
    ) -> IntelligenceBrief:
        priorities = [self._deterministic_priority(item) for item in catalog[:3]]
        needs = [
            BriefClaim(
                text=(
                    f"{item.buyer or '原文未明确采购人'}公开了“{item.title}”相关需求，"
                    f"当前公告阶段为{item.event_type}。"
                ),
                evidence_ids=[item.evidence_id],
            )
            for item in catalog[:3]
        ]
        risks: list[BriefRisk] = []
        late_stage = next(
            (item for item in catalog if item.event_type in {"中标公告", "合同公告"}),
            None,
        )
        if late_stage:
            risks.append(
                BriefRisk(
                    level="medium",
                    text="部分高分记录已进入中标或合同阶段，更适合竞对复盘，不能当作仍可报名机会。",
                    evidence_ids=[late_stage.evidence_id],
                )
            )
        missing_buyer = next((item for item in catalog if not item.buyer), None)
        if missing_buyer:
            risks.append(
                BriefRisk(
                    level="low",
                    text="部分公告未能从公开正文稳定提取采购人，联系前需打开原文再次确认主体。",
                    evidence_ids=[missing_buyer.evidence_id],
                )
            )
        actions = [
            BriefAction(
                priority="P0" if index == 0 else "P1" if index == 1 else "P2",
                text=priority.recommended_action,
                evidence_ids=[priority.evidence_id],
            )
            for index, priority in enumerate(priorities)
        ]
        return IntelligenceBrief(
            mode="deterministic",
            status=status,
            overview=(
                f"本轮从 {len(catalog)} 条高相关证据中筛出 {len(priorities)} 条优先跟进记录；"
                "建议先核验仍处于采购意向、招标或更正阶段的项目。"
            ),
            buyer_needs=needs,
            priorities=priorities,
            risks=risks,
            actions=actions,
            evidence_catalog=catalog,
            generated_at=datetime.now(ZoneInfo(self.settings.timezone)),
            latency_ms=latency_ms,
            summary=summary,
        )

    @staticmethod
    def _deterministic_priority(item: BriefEvidence) -> BriefPriority:
        if item.event_type == "采购意向":
            reason = "处于采购意向阶段，通常仍有需求澄清和前置交流窗口。"
            action = "核验意向原文和预算节奏，准备场景化方案并建立采购单位联系人。"
        elif item.event_type in {"招标公告", "更正公告"}:
            reason = "处于招标或更正阶段，需优先核对资格条件、截止时间和最新附件。"
            action = "立即打开原文核验报名窗口、资质和附件，判断是否进入投标准备。"
        elif item.event_type in {"中标公告", "合同公告"}:
            reason = "已进入结果阶段，直接参与窗口可能关闭，但可用于采购人与竞对复盘。"
            action = "记录中标方、价格和采购人，加入竞对与续采监控，不误判为在招项目。"
        else:
            reason = "公告阶段需要人工确认，系统已保留可追溯原文供进一步判断。"
            action = "打开原文确认公告性质、有效期和下一步参与方式。"
        return BriefPriority(
            evidence_id=item.evidence_id,
            title=item.title,
            buyer=item.buyer,
            published_at=item.published_at,
            event_type=item.event_type,
            opportunity_score=item.opportunity_score,
            reason=reason,
            recommended_action=action,
            source_url=item.source_url,
        )

    @staticmethod
    def _request_input(
        spec: TenderQuerySpec,
        catalog: list[BriefEvidence],
    ) -> dict:
        return {
            "query_scope": {
                "topic": spec.topic,
                "region": spec.region or "全国",
                "start_date": spec.start_date.isoformat(),
                "end_date": spec.end_date.isoformat(),
                "record_count": len(catalog),
            },
            "evidence_catalog": [
                {
                    "evidence_id": item.evidence_id,
                    "title": item.title,
                    "buyer": item.buyer,
                    "published_at": item.published_at.isoformat(),
                    "event_type": item.event_type,
                    "opportunity_score": item.opportunity_score,
                    "excerpt": item.excerpt,
                }
                for item in catalog
            ],
        }

    def _payload(self, request_input: dict, *, correction: str | None = None) -> dict:
        schema = _BriefProposal.model_json_schema()
        user_content = {"schema": schema, "input": request_input}
        if correction:
            user_content["correction"] = correction
        return {
            "model": self.settings.llm_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是企业级招投标情报副驾驶。只能使用用户消息中的 evidence_catalog，"
                        "不得创造公告、采购人、数字、日期、URL 或证据编号。每一条需求、风险和行动"
                        "必须引用至少一个已有 E 编号。priorities 只返回 evidence_id、reason 和"
                        "recommended_action；标题、采购人、评分、日期和链接由本地系统回填。"
                        "区分采购意向/招标/更正与中标/合同阶段，不把结果公告说成仍可投标。"
                        "overview、buyer_needs、reason 和 risks 尽量不要写阿拉伯数字；如确有必要，"
                        "只能逐字复制 input 中已经出现的数字，严禁计算、换算、估算或补充。"
                        "只输出符合 JSON Schema 的 JSON 对象，不要 Markdown。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(user_content, ensure_ascii=False),
                },
            ],
            "temperature": 0,
            "max_tokens": 2600,
            "response_format": {"type": "json_object"},
        }

    async def _request(self, payload: dict) -> str:
        endpoint = self.settings.llm_base_url.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        async with httpx.AsyncClient(timeout=self.settings.llm_timeout) as client:
            response = await client.post(endpoint, headers=headers, json=payload)
            if response.status_code in {400, 422} and "response_format" in payload:
                compatible = {
                    key: value for key, value in payload.items() if key != "response_format"
                }
                response = await client.post(endpoint, headers=headers, json=compatible)
            response.raise_for_status()
            data = response.json()
        content = data["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise InvalidIntelligenceResponse("模型没有返回文本 JSON")
        return content.strip()

    def _hydrate(
        self,
        proposal: _BriefProposal,
        catalog: list[BriefEvidence],
        request_input: dict,
    ) -> IntelligenceBrief:
        by_id = {item.evidence_id: item for item in catalog}

        def validate_ids(ids: list[str]) -> list[str]:
            unique = list(dict.fromkeys(ids))
            if not unique or any(item not in by_id for item in unique):
                raise InvalidIntelligenceResponse("模型引用了不存在的证据编号")
            return unique

        overview_evidence = " ".join(
            f"{item.title} {item.buyer or ''} {item.event_type} {item.excerpt}" for item in catalog
        )
        if not self._numbers_are_grounded(proposal.overview, overview_evidence):
            raise InvalidIntelligenceResponse("概览使用了证据目录中不存在的完整数字")
        buyer_needs = []
        for item in proposal.buyer_needs:
            evidence_ids = validate_ids(item.evidence_ids)
            self._validate_numbers_for_ids(item.text, evidence_ids, by_id)
            buyer_needs.append(BriefClaim(text=item.text, evidence_ids=evidence_ids))

        priorities = []
        for item in proposal.priorities:
            evidence = by_id.get(item.evidence_id)
            if evidence is None:
                raise InvalidIntelligenceResponse("优先机会引用了不存在的证据编号")
            self._validate_numbers_for_ids(
                f"{item.reason} {item.recommended_action}",
                [item.evidence_id],
                by_id,
            )
            priorities.append(
                BriefPriority(
                    evidence_id=evidence.evidence_id,
                    title=evidence.title,
                    buyer=evidence.buyer,
                    published_at=evidence.published_at,
                    event_type=evidence.event_type,
                    opportunity_score=evidence.opportunity_score,
                    reason=item.reason,
                    recommended_action=item.recommended_action,
                    source_url=evidence.source_url,
                )
            )

        risks = []
        for item in proposal.risks:
            evidence_ids = validate_ids(item.evidence_ids)
            self._validate_numbers_for_ids(item.text, evidence_ids, by_id)
            risks.append(BriefRisk(level=item.level, text=item.text, evidence_ids=evidence_ids))

        actions = []
        for item in proposal.actions:
            evidence_ids = validate_ids(item.evidence_ids)
            self._validate_numbers_for_ids(item.text, evidence_ids, by_id)
            actions.append(
                BriefAction(
                    priority=item.priority,
                    text=item.text,
                    evidence_ids=evidence_ids,
                )
            )

        return IntelligenceBrief(
            mode="llm_grounded",
            status="applied",
            overview=proposal.overview,
            buyer_needs=buyer_needs,
            priorities=priorities,
            risks=risks,
            actions=actions,
            evidence_catalog=catalog,
            generated_at=datetime.now(ZoneInfo(self.settings.timezone)),
            summary="证据约束 AI 情报简报已生成。",
        )

    @staticmethod
    def _numbers_are_grounded(text: str, evidence: str) -> bool:
        numbers = set(re.findall(r"(?<![\d.])\d+(?:[.,]\d+)*(?:%|％)?(?![\d.])", text))
        evidence_numbers = set(re.findall(r"(?<![\d.])\d+(?:[.,]\d+)*(?:%|％)?(?![\d.])", evidence))
        return numbers <= evidence_numbers

    @classmethod
    def _validate_numbers_for_ids(
        cls,
        text: str,
        evidence_ids: list[str],
        by_id: dict[str, BriefEvidence],
    ) -> None:
        evidence = " ".join(
            f"{by_id[evidence_id].title} {by_id[evidence_id].buyer or ''} "
            f"{by_id[evidence_id].event_type} {by_id[evidence_id].excerpt}"
            for evidence_id in evidence_ids
        )
        if not cls._numbers_are_grounded(text, evidence):
            raise InvalidIntelligenceResponse("模型文本使用了所引用证据中不存在的完整数字")
