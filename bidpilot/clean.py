from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime

from bs4 import BeautifulSoup

from bidpilot.models import EventType

PROJECT_ID_PATTERNS = [
    r"(?:项目编号|招标编号|采购编号|标段编号)\s*[：:]\s*([A-Za-z0-9_.\-/（）()]+)",
    r"\b([A-Z]{2,8}[-_/]\d{4,}[-_/A-Z0-9]*)\b",
]


def normalize_space(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("\u200b", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def clean_html(html: str, selector: str | None = None) -> str:
    soup = BeautifulSoup(html, "lxml")
    root = soup.select_one(selector) if selector else soup
    if root is None:
        root = soup
    for node in root.select(
        "script,style,noscript,nav,header,footer,.nav,.advertisement,.ads,.share,.copyright"
    ):
        node.decompose()
    return normalize_space(root.get_text(" ", strip=True))


def detect_event_type(text: str) -> EventType:
    text = normalize_space(text)
    if re.search(r"采购意向|招标预告|需求公示", text):
        return EventType.INTENTION
    if re.search(r"更正|变更|澄清|补充公告", text):
        return EventType.CHANGE
    if re.search(r"中标|成交|结果公告|候选人公示", text):
        return EventType.AWARD
    if re.search(r"合同公告|合同公示", text):
        return EventType.CONTRACT
    if re.search(r"招标|采购公告|磋商|询价|谈判|资格预审", text):
        return EventType.TENDER
    return EventType.OTHER


def extract_project_id(text: str) -> str | None:
    for pattern in PROJECT_ID_PATTERNS:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = normalize_space(match.group(1)).strip("，,。.;；")
            if len(value) >= 4:
                return value
    return None


def stable_hash(*parts: object, length: int = 24) -> str:
    material = "\x1f".join(str(part or "") for part in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:length]


def parse_datetime(value: str, fallback: datetime | None = None) -> datetime:
    value = normalize_space(value)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", value)
    if match:
        return datetime(*map(int, match.groups()))
    return fallback or datetime.now()


def find_attachments(root: BeautifulSoup, base_url: str) -> list[tuple[str, str]]:
    from urllib.parse import urljoin

    attachments: list[tuple[str, str]] = []
    seen: set[str] = set()
    for anchor in root.select("a[href]"):
        href = anchor.get("href", "").strip()
        text = normalize_space(anchor.get_text(" ", strip=True)) or "附件"
        if not href or href.startswith(("javascript:", "#")):
            continue
        if not (re.search(r"\.(?:pdf|docx?|xlsx?|zip|rar)(?:\?|$)", href, re.I) or "附件" in text):
            continue
        url = urljoin(base_url, href)
        if url not in seen:
            seen.add(url)
            attachments.append((text[:120], url))
    return attachments
