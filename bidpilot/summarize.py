from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx

from bidpilot.clean import normalize_space
from bidpilot.config import Settings
from bidpilot.models import EvidenceSpan, RawTender


@dataclass(slots=True)
class SummaryResult:
    summary: str
    evidence: list[EvidenceSpan]
    mode: str


class EvidenceSummarizer:
    """Generate summaries from captured evidence and reject unsupported numeric claims."""

    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    def extractive(item: RawTender) -> SummaryResult:
        body = normalize_space(item.body)
        evidence_text = body or item.title
        masked = bool(re.search(r"[*＊]+", evidence_text))
        facts: list[str] = []

        project = re.search(
            r"(?:项目编号|招标编号|采购编号)[：:]\s*[A-Za-z0-9_.\-/（）()]+", evidence_text
        )
        budget = re.search(
            r"(?:预算金额|项目预算|最高限价)[：:]?\s*[^。；;]{1,36}(?:元|万元|亿元)", evidence_text
        )
        deadline = re.search(
            r"(?:投标|响应文件|提交).*?(?:截止时间|截止)[：:]?\s*[^。；;]{1,40}", evidence_text
        )
        quantity = re.search(
            r"(?:采购|购置|招标)[^。；;]{0,40}\d+\s*(?:台|套|项|批)", evidence_text
        )
        for match in (project, budget, deadline, quantity):
            if match:
                fact = normalize_space(match.group(0)).strip("，,。；; ")
                # Public snippets sometimes mask member-only digits with '*'.
                # Do not elevate masked values into an executive summary.
                if fact and not re.search(r"[*＊]+", fact) and fact not in facts:
                    facts.append(fact)

        if not facts:
            sentences = [
                normalize_space(sentence)
                for sentence in re.split(r"(?<=[。！？；])", body)
                if len(normalize_space(sentence)) >= 12
                and not re.search(r"[*＊]+", sentence)
            ]
            facts.extend(sentences[:2])

        safe_title = re.sub(r"[*＊]+", "脱敏信息", item.title)
        prefix = f"{item.buyer}发布" if item.buyer else "该公告涉及"
        if facts:
            summary = f"{prefix}{safe_title}。" + "；".join(facts[:4]).rstrip("。") + "。"
        else:
            summary = f"{prefix}{safe_title}；详情请以来源页面为准。"
        caution = "公开摘要存在脱敏字段，预算与截止时间需授权后核验。" if masked else ""
        summary = normalize_space(summary)
        if caution:
            summary = summary[: 260 - len(caution)].rstrip("，,；;。 ") + "。" + caution
        else:
            summary = summary[:260]
        evidence = item.evidence or [
            EvidenceSpan(text=evidence_text[:500], source_url=item.source_url)
        ]
        return SummaryResult(summary=summary, evidence=evidence, mode="extractive")

    async def summarize(self, item: RawTender) -> SummaryResult:
        fallback = self.extractive(item)
        if not (
            self.settings.llm_base_url
            and self.settings.llm_api_key
            and self.settings.llm_model
            and item.body
        ):
            return fallback

        evidence = normalize_space(item.body)[:7000]
        endpoint = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        prompt = (
            "你是招投标情报分析员。只能根据【证据】生成不超过180字的中文摘要，"
            "不得补充证据中没有的数字、日期、机构或结论。输出严格 JSON："
            '{"summary":"..."}。\n【标题】' + item.title + "\n【证据】" + evidence
        )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    endpoint,
                    headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
                    json={
                        "model": self.settings.llm_model,
                        "temperature": 0,
                        "response_format": {"type": "json_object"},
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                summary = normalize_space(json.loads(content)["summary"])
            if not summary or not self._numbers_are_grounded(summary, evidence):
                return fallback
            return SummaryResult(summary=summary, evidence=fallback.evidence, mode="llm_grounded")
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return fallback

    @staticmethod
    def _numbers_are_grounded(summary: str, evidence: str) -> bool:
        numbers = re.findall(r"\d+(?:\.\d+)?", summary)
        return all(number in evidence for number in numbers)
