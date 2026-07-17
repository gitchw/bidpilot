from pathlib import Path

from bidpilot.models import EventType
from bidpilot.sources.ccgp import CCGPSource
from bidpilot.sources.cecbid import CECBidSource
from bidpilot.sources.qianlima import QianlimaSource

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_cecbid_search_parser_extracts_evidence_and_event_type():
    items = CECBidSource.parse_search_page(fixture("cecbid_search.html"))
    assert len(items) == 2
    first = items[0]
    assert first.title == "安徽省某算力中心 GPU 服务器采购项目招标公告"
    assert first.region == "安徽"
    assert first.project_id == "AH-2026-001"
    assert first.event_type == EventType.TENDER
    assert first.evidence[0].source_url.endswith("ABC123?wd=服务器")
    assert items[1].event_type == EventType.AWARD


def test_ccgp_list_and_detail_parser_extract_required_fields():
    page_url = "https://www.ccgp.gov.cn/cggg/dfgg/gkzb/index.htm"
    items = CCGPSource.parse_list_page(fixture("ccgp_list.html"), page_url, EventType.TENDER)
    assert len(items) == 2
    first = CCGPSource.parse_detail_page(fixture("ccgp_detail.html"), items[0])
    assert first.region == "安徽"
    assert first.buyer == "安徽大学"
    assert first.project_id == "AH-2026-001"
    assert "1200 万元" in first.body
    assert first.attachments[0].url == "https://www.ccgp.gov.cn/files/AH-2026-001.docx"
    assert first.evidence


def test_qianlima_member_parser_is_structured():
    items = QianlimaSource.parse_search_page(fixture("qianlima_search.html"))
    assert len(items) == 1
    assert items[0].auth_level == "free_member"
    assert items[0].project_id == "QLM-2026-9"
    assert items[0].source_url == "https://wap.qianlima.com/zb/detail/QLM001.html"


def test_ccgp_prefilter_respects_region_topic_and_date(sample_spec):
    page_url = "https://www.ccgp.gov.cn/cggg/dfgg/gkzb/index.htm"
    items = CCGPSource.parse_list_page(fixture("ccgp_list.html"), page_url, EventType.TENDER)
    assert CCGPSource._matches_spec(items[0], sample_spec)
    assert not CCGPSource._matches_spec(items[1], sample_spec)
