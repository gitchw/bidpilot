from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bidpilot.clean import normalize_space
from bidpilot.config import Settings
from bidpilot.intent import CITY_REGIONS, REGIONS
from bidpilot.models import (
    CandidateDecision,
    RawTender,
    RetrievalPlan,
    RetrievalQuery,
    RetrievalTerm,
    TenderQuerySpec,
)
from bidpilot.sources.base import SourceAdapter

LLMRequester = Callable[[dict[str, Any]], Awaitable[str]]


_DOMAIN_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "充电桩": ("充电基础设施", "充换电设备", "直流充电机", "交流充电设备"),
    "医疗设备": ("医疗器械", "医用设备", "医学装备", "诊疗设备", "医疗装备"),
    "医疗器械": ("医疗设备", "医用设备", "医学装备", "诊疗设备"),
    "数据中心": ("IDC", "模块化机房", "云计算中心", "服务器机房", "算力基础设施"),
    "服务器": ("计算服务器", "AI服务器", "信创服务器", "GPU计算节点", "算力服务器"),
    "存储": ("磁盘阵列", "分布式存储", "存储服务器", "数据存储设备"),
    "网络安全": ("信息安全", "网络防护", "安全设备", "等保建设", "密码设备"),
    "人工智能": ("AI平台", "大模型", "智能计算", "机器学习平台", "算力平台"),
    "云计算": ("云平台", "政务云", "私有云", "混合云", "云资源"),
}

_GENERIC_TERMS = {
    "采购",
    "招标",
    "项目",
    "设备",
    "系统",
    "服务",
    "工程",
    "公告",
    "公示",
    "信息",
    "建设",
    "货物",
    "产品",
    "解决方案",
}
_CONTROL_WORDS = re.compile(
    r"最近|过去|查询|搜索|查找|每天|每周|每月|推送|发送|通知|报告|招标信息|采购信息"
)
_DATE_OR_URL = re.compile(r"https?://|www\.|20\d{2}[-/.年]|\d{1,2}月\d{1,2}日", re.IGNORECASE)
_REGION_ONLY = {
    *(value[0] for value in REGIONS.values()),
    *(value[0] for value in CITY_REGIONS.values()),
}


class _ProposedTerm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=2, max_length=40)
    kind: Literal["synonym", "industry"]
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=240)


class _PlanProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    terms: list[_ProposedTerm] = Field(default_factory=list, max_length=12)
    query_variants: list[str] = Field(default_factory=list, max_length=8)
    source_priorities: list[str] = Field(default_factory=list, max_length=12)
    rationale: str = Field(min_length=1, max_length=500)


class _ProposedDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1, max_length=40)
    relevant: bool
    confidence: float = Field(ge=0, le=1)
    matched_concepts: list[str] = Field(default_factory=list, max_length=8)
    reason: str = Field(min_length=1, max_length=240)


class _ReviewProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: list[_ProposedDecision] = Field(default_factory=list, max_length=30)


class InvalidRetrievalResponse(RuntimeError):
    pass


class RetrievalPlanner:
    """Create and audit bounded retrieval plans; never manufacture tender facts."""

    version = "retrieval-v1"

    def __init__(
        self,
        settings: Settings,
        sources: Sequence[SourceAdapter],
        requester: LLMRequester | None = None,
    ):
        self.settings = settings
        self.sources = list(sources)
        self.requester = requester

    async def plan(self, spec: TenderQuerySpec) -> RetrievalPlan:
        base_terms = self._deterministic_terms(spec)
        source_ids = [source.source_id for source in self.sources]
        mode = self.settings.retrieval_llm_mode
        if mode == "off":
            return self._finish_plan(
                base_terms,
                region=spec.region,
                llm_status="disabled",
                mode="deterministic",
                summary="AI 检索规划已关闭；本轮使用原主题、规则同义词和本地行业词典。",
            )
        if not (self.settings.llm_base_url and self.settings.llm_model):
            return self._finish_plan(
                base_terms,
                region=spec.region,
                llm_status="not_configured",
                mode="deterministic",
                summary="模型尚未配置；本轮安全使用原主题、规则同义词和本地行业词典。",
            )

        started = time.perf_counter()
        try:
            payload = self._plan_payload(spec, base_terms, source_ids)
            content = await (self.requester(payload) if self.requester else self._request(payload))
            proposal = self._parse(content, _PlanProposal)
        except InvalidRetrievalResponse:
            return self._finish_plan(
                base_terms,
                region=spec.region,
                llm_status="invalid_response",
                mode="fallback",
                latency_ms=round((time.perf_counter() - started) * 1000),
                summary="模型检索计划不符合严格 JSON 契约；已安全回退本地检索计划。",
            )
        except Exception:
            return self._finish_plan(
                base_terms,
                region=spec.region,
                llm_status="unavailable",
                mode="fallback",
                latency_ms=round((time.perf_counter() - started) * 1000),
                summary="模型规划服务超时或不可用；已安全回退本地检索计划。",
            )

        accepted = list(base_terms)
        rejected: list[str] = []
        seen = {self._fold(term.text) for term in accepted}
        for proposed in proposal.terms:
            text = normalize_space(proposed.text)
            reason = self._term_rejection(text, proposed.confidence)
            folded = self._fold(text)
            if folded in seen:
                continue
            if reason:
                rejected.append(f"{text or '空词'}：{reason}")
                continue
            accepted.append(
                RetrievalTerm(
                    text=text,
                    kind=proposed.kind,
                    origin="llm",
                    reason=proposed.reason,
                )
            )
            seen.add(folded)
            if len(accepted) >= 16:
                break

        priorities = list(
            dict.fromkeys(item for item in proposal.source_priorities if item in source_ids)
        )
        plan = self._finish_plan(
            accepted,
            region=spec.region,
            llm_status="applied" if len(accepted) > len(base_terms) else "rejected",
            mode="hybrid" if len(accepted) > len(base_terms) else "deterministic",
            latency_ms=round((time.perf_counter() - started) * 1000),
            summary=(
                f"模型提出检索扩展，经本地校验接受 {len(accepted) - len(base_terms)} 个词；"
                f"共准备 {len(accepted)} 个证据检索概念。"
            ),
            rejected_terms=rejected,
            source_priorities=priorities,
            proposed_queries=proposal.query_variants,
        )
        return plan

    async def review_candidates(
        self,
        spec: TenderQuerySpec,
        terms: Sequence[RetrievalTerm],
        candidates: Sequence[RawTender],
    ) -> tuple[str, list[CandidateDecision], dict[str, float]]:
        if not candidates:
            return "not_needed", [], {}
        if self.settings.retrieval_llm_mode == "off" or not self.settings.retrieval_semantic_review:
            return "disabled", [], {}

        limited = list(candidates[: self.settings.retrieval_semantic_candidate_limit])
        if not (self.settings.llm_base_url and self.settings.llm_model):
            return (
                "not_configured",
                [
                    self._unreviewed(index, item, "模型未配置，边界候选按保守策略不纳入结果")
                    for index, item in enumerate(limited, start=1)
                ],
                {},
            )

        candidate_map = {f"c{index:03d}": item for index, item in enumerate(limited, start=1)}
        payload = self._review_payload(spec, terms, candidate_map)
        try:
            content = await (self.requester(payload) if self.requester else self._request(payload))
            proposal = self._parse(content, _ReviewProposal)
        except InvalidRetrievalResponse:
            return (
                "invalid_response",
                [
                    self._decision(
                        candidate_id, item, "not_reviewed", 0, "模型返回格式无效，已保守拒绝"
                    )
                    for candidate_id, item in candidate_map.items()
                ],
                {},
            )
        except Exception:
            return (
                "unavailable",
                [
                    self._decision(candidate_id, item, "not_reviewed", 0, "模型不可用，已保守拒绝")
                    for candidate_id, item in candidate_map.items()
                ],
                {},
            )

        by_id: dict[str, _ProposedDecision] = {}
        for proposed in proposal.decisions:
            if proposed.candidate_id in candidate_map and proposed.candidate_id not in by_id:
                by_id[proposed.candidate_id] = proposed
        accepted: dict[str, float] = {}
        decisions: list[CandidateDecision] = []
        threshold = self.settings.retrieval_semantic_threshold
        for candidate_id, item in candidate_map.items():
            proposed = by_id.get(candidate_id)
            if proposed is None:
                decisions.append(
                    self._decision(
                        candidate_id, item, "not_reviewed", 0, "模型未返回该候选，已保守拒绝"
                    )
                )
                continue
            allowed = proposed.relevant and proposed.confidence >= threshold
            outcome: Literal["accepted", "rejected"] = "accepted" if allowed else "rejected"
            reason = proposed.reason
            if proposed.relevant and proposed.confidence < threshold:
                reason = f"模型认为可能相关，但置信度低于 {threshold:.2f}；{reason}"
            decisions.append(
                self._decision(candidate_id, item, outcome, proposed.confidence, reason)
            )
            if allowed:
                accepted[item.source_url] = proposed.confidence
        return "applied", decisions, accepted

    def _deterministic_terms(self, spec: TenderQuerySpec) -> list[RetrievalTerm]:
        terms: list[RetrievalTerm] = []
        seen: set[str] = set()

        def add(
            text: str, kind: Literal["topic", "synonym", "industry"], origin: str, reason: str
        ) -> None:
            text = normalize_space(text)
            folded = self._fold(text)
            if folded in seen or self._term_rejection(text, 1):
                return
            seen.add(folded)
            terms.append(
                RetrievalTerm(text=text, kind=kind, origin=origin, reason=reason)  # type: ignore[arg-type]
            )

        add(spec.topic, "topic", "query", "用户问题中识别出的核心业务主题")
        for keyword in spec.keywords:
            add(keyword, "synonym", "rules", "意图规则库中的受控同义词")
        for key, expansions in _DOMAIN_EXPANSIONS.items():
            if key in spec.topic or spec.topic in key or key in spec.keywords:
                for expansion in expansions:
                    add(expansion, "industry", "lexicon", f"本地行业词典：{key} 的常见采购表述")
        return terms[:16]

    def _finish_plan(
        self,
        terms: list[RetrievalTerm],
        *,
        region: str | None,
        llm_status: Literal[
            "disabled",
            "not_configured",
            "applied",
            "rejected",
            "invalid_response",
            "unavailable",
        ],
        mode: Literal["deterministic", "hybrid", "fallback"],
        summary: str,
        latency_ms: int = 0,
        rejected_terms: list[str] | None = None,
        source_priorities: list[str] | None = None,
        proposed_queries: Sequence[str] = (),
    ) -> RetrievalPlan:
        queries = [
            RetrievalQuery(
                id="q-primary",
                text=terms[0].text,
                round=1,
                reason="所有来源先执行用户核心主题，建立可比较的确定性基线。",
                term_kind=terms[0].kind,
            )
        ]
        seen = {self._fold(terms[0].text)}
        reserve: list[tuple[str, str, Literal["topic", "synonym", "industry", "llm"]]] = []
        accepted_texts = [term.text for term in terms]
        deterministic = [term for term in terms[1:] if term.origin != "llm"]
        model_terms = [term for term in terms[1:] if term.origin == "llm"]
        ordered_terms: list[RetrievalTerm] = []
        if deterministic:
            ordered_terms.append(deterministic.pop(0))
        if model_terms:
            ordered_terms.append(model_terms.pop(0))
        ordered_terms.extend(deterministic)
        ordered_terms.extend(model_terms)
        for term in ordered_terms:
            folded = self._fold(term.text)
            if folded in seen or len(reserve) >= 10:
                continue
            reserve.append((term.text, term.reason, term.kind))
            seen.add(folded)
        for value in proposed_queries:
            value = normalize_space(value)
            folded = self._fold(value)
            if (
                folded in seen
                or not self._valid_query(value, accepted_texts, region)
                or len(reserve) >= 10
            ):
                continue
            reserve.append((value, "模型建议且通过本地词条、长度和通用词校验", "llm"))
            seen.add(folded)
        for index, (text, reason, kind) in enumerate(reserve, start=1):
            queries.append(
                RetrievalQuery(
                    id=f"q-expand-{index:02d}",
                    text=text,
                    round=2,
                    reason=reason,
                    term_kind=kind,
                )
            )
        return RetrievalPlan(
            planner_version=self.version,
            mode=mode,
            llm_status=llm_status,
            terms=terms,
            rejected_terms=(rejected_terms or [])[:16],
            queries=queries,
            source_priorities=source_priorities or [],
            max_rounds=self.settings.retrieval_max_rounds,
            query_budget_per_source=self.settings.retrieval_query_budget_per_source,
            latency_ms=latency_ms,
            summary=summary,
        )

    def _plan_payload(
        self,
        spec: TenderQuerySpec,
        terms: Sequence[RetrievalTerm],
        source_ids: Sequence[str],
    ) -> dict[str, Any]:
        return {
            "model": self.settings.llm_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是招投标检索规划器，只能扩展搜索词，不能生成或猜测公告、URL、采购人、"
                        "金额、日期或结果。只输出一个严格 JSON 对象，禁止 Markdown、代码围栏、解释前后缀"
                        "和额外字段。词条必须是原主题在真实采购公告中的同义词、简称、上位/下位行业术语；"
                        "不要输出采购、招标、项目、设备、系统等单独通用词。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "original_query": spec.raw_query,
                            "topic": spec.topic,
                            "region": spec.region or "全国",
                            "date_range": [spec.start_date.isoformat(), spec.end_date.isoformat()],
                            "existing_terms": [term.text for term in terms],
                            "allowed_source_ids": list(source_ids),
                            "required_json_schema": _PlanProposal.model_json_schema(),
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "temperature": 0,
            "max_tokens": 1400,
            "response_format": {"type": "json_object"},
        }

    def _review_payload(
        self,
        spec: TenderQuerySpec,
        terms: Sequence[RetrievalTerm],
        candidates: dict[str, RawTender],
    ) -> dict[str, Any]:
        rows = []
        for candidate_id, item in candidates.items():
            rows.append(
                {
                    "candidate_id": candidate_id,
                    "title": item.title[:240],
                    "buyer": (item.buyer or "")[:120],
                    "region": item.region or "",
                    "event_type": item.event_type.value,
                    "evidence_excerpt": normalize_space(item.body)[:500],
                }
            )
        return {
            "model": self.settings.llm_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是招投标候选相关性复核器。只能依据给出的标题和证据片段判断与用户主题的"
                        "业务相关性；不得补充事实，不得输出 URL，不得放宽日期、地域、公告类型或排除词。"
                        "只输出严格 JSON 对象，禁止 Markdown、额外字段和解释前后缀。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "topic": spec.topic,
                            "validated_concepts": [term.text for term in terms],
                            "candidates": rows,
                            "required_json_schema": _ReviewProposal.model_json_schema(),
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "temperature": 0,
            "max_tokens": 2200,
            "response_format": {"type": "json_object"},
        }

    async def _request(self, payload: dict[str, Any]) -> str:
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
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise InvalidRetrievalResponse from exc

    @staticmethod
    def _parse(content: str, schema: type[BaseModel]) -> Any:
        stripped = content.strip()
        if not stripped.startswith("{") or not stripped.endswith("}") or "```" in stripped:
            raise InvalidRetrievalResponse
        try:
            data = json.loads(stripped)
            if not isinstance(data, dict):
                raise InvalidRetrievalResponse
            return schema.model_validate(data)
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
            raise InvalidRetrievalResponse from exc

    @staticmethod
    def _fold(value: str) -> str:
        return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", value).casefold()

    @classmethod
    def _term_rejection(cls, text: str, confidence: float) -> str | None:
        folded = cls._fold(text)
        if confidence < 0.65:
            return "模型置信度低于 0.65"
        if not 2 <= len(folded) <= 40:
            return "长度必须为 2～40 个有效字符"
        if text.casefold() in {item.casefold() for item in _GENERIC_TERMS}:
            return "不能使用单独的通用采购词"
        if text in _REGION_ONLY:
            return "地域名称不能冒充业务主题"
        if _DATE_OR_URL.search(text):
            return "词条不能包含日期或 URL"
        if _CONTROL_WORDS.search(text):
            return "词条混入查询、计划或推送控制语句"
        return None

    @classmethod
    def _valid_query(
        cls,
        query: str,
        accepted_terms: Sequence[str],
        region: str | None,
    ) -> bool:
        if region and region in query:
            return False
        if cls._term_rejection(query, 1) is None:
            return True
        folded = cls._fold(query)
        if not 2 <= len(folded) <= 60 or _DATE_OR_URL.search(query) or _CONTROL_WORDS.search(query):
            return False
        return any(cls._fold(term) in folded for term in accepted_terms)

    @staticmethod
    def _evidence_excerpt(item: RawTender) -> str:
        return normalize_space(item.body or item.title)[:500]

    @classmethod
    def _decision(
        cls,
        candidate_id: str,
        item: RawTender,
        outcome: Literal["accepted", "rejected", "not_reviewed"],
        confidence: float,
        reason: str,
    ) -> CandidateDecision:
        return CandidateDecision(
            candidate_id=candidate_id,
            title=item.title,
            source=item.source,
            outcome=outcome,
            confidence=confidence,
            reason=reason,
            evidence_excerpt=cls._evidence_excerpt(item),
        )

    @classmethod
    def _unreviewed(cls, index: int, item: RawTender, reason: str) -> CandidateDecision:
        return cls._decision(f"c{index:03d}", item, "not_reviewed", 0, reason)
