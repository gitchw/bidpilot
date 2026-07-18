import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bidpilot.intent import IntentParser
from bidpilot.models import EventType
from bidpilot.sources.ccgp import CCGPSource
from bidpilot.sources.cebpubservice import CEBPubServiceSource
from bidpilot.sources.cecbid import CECBidSource
from bidpilot.sources.ggzy import GGZYSource
from bidpilot.sources.mofcom import MofcomSource
from bidpilot.sources.plap import PLAPSource
from bidpilot.sources.qianlima import QianlimaSource
from bidpilot.sources.zycg import ZYCGSource

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


def test_city_query_keeps_province_labeled_candidate_for_detail_filtering():
    page_url = "https://www.ccgp.gov.cn/cggg/dfgg/gkzb/index.htm"
    item = CCGPSource.parse_list_page(fixture("ccgp_list.html"), page_url, EventType.TENDER)[
        0
    ].model_copy(
        update={
            "region": "广东",
            "title": "深圳市公共充电桩建设项目招标公告",
        }
    )
    spec = IntentParser().parse(
        "最近1个月深圳充电桩招标信息",
        now=datetime(2026, 7, 18, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert spec.region_level == "city"
    assert CCGPSource._matches_spec(item, spec)


def test_mofcom_json_and_detail_parsers_preserve_official_evidence():
    payload = {
        "rows": [
            {
                "name": "合肥先进光源服务器设备 - 国际招标公告",
                "publishTime": "2026-07-17",
                "areaName": "中国安徽省",
                "filePath": "/bidding/bulletin/202607/abc.html",
                "digest": "招标项目编号: 0729-264OIT321025，采购服务器2台",
                "industryName": "计算机设备",
                "capitalSourceName": "现汇",
                "fdid": "abc",
            }
        ]
    }
    item = MofcomSource.parse_search_response(payload, EventType.TENDER)[0]
    assert item.region == "安徽"
    assert item.source_url.endswith("/bidDetail/bidding/bulletin/202607/abc.html")
    detail = """
    <div class="article"><p>招标项目编号:0729-264OIT321025<br>
    招标人:合肥先进光源研究院<br>投标截止时间:2026-08-07 10:00</p>
    <a href="/files/spec.xls">附件</a></div>
    """
    item = MofcomSource.parse_detail_page(detail, item)
    assert item.buyer == "合肥先进光源研究院"
    assert item.attachments[0].url.endswith("/files/spec.xls")
    assert item.evidence


def test_ggzy_home_feed_and_detail_parsers_are_honest_about_region():
    home = """
    <div class="main_list_on"><h4>交易公告</h4><ul><li>
    <a href="/information/deal/html/a/340000/0201/20260717/abc.html">
    安徽大学 GPU 服务器采购公告</a><span>2026-07-17</span>
    </li></ul></div>
    <div class="main_list_on main_list_tw"><h4>成交公示</h4><ul><li>
    <a href="/information/deal/html/a/310000/0202/20260717/def.html">
    上海服务器采购成交公告</a><span>2026-07-17</span>
    </li></ul></div>
    """
    items = GGZYSource.parse_home_feed(home)
    assert items[0].region == "安徽"
    assert items[0].event_type == EventType.TENDER
    assert items[1].event_type == EventType.AWARD
    detail = """
    <div class="detail"><h4 class="h4_o">安徽大学 GPU 服务器采购公告</h4>
    <div class="detail_content"><p>项目编号：AH-2026-001。预算金额：1200万元。</p></div></div>
    """
    parsed = GGZYSource.parse_detail_page(detail, items[0])
    assert parsed.project_id == "AH-2026-001"
    assert "预算金额" in parsed.body


def test_zycg_public_json_parser_preserves_central_procurement_evidence():
    payload = json.loads(fixture("zycg_search.json"))
    item = ZYCGSource.parse_response(payload)[0]
    assert item.source == "中央政府采购网"
    assert item.project_id == "GC-HGX260001"
    assert item.buyer == "中国科学院某研究所"
    assert item.event_type == EventType.AWARD
    assert item.attachments[0].url == "https://www.zycg.gov.cn/files/zycg-001.pdf"
    assert item.source_url.endswith("zycg-001.html?id=zycg-001")


def test_plap_public_api_parser_extracts_full_content_without_login():
    payload = json.loads(fixture("plap_search.json"))
    item = PLAPSource.parse_response(payload)[0]
    assert item.source == "军队采购网"
    assert item.region == "北京"
    assert item.project_id == "2026-JQ06-W3077"
    assert item.event_type == EventType.INTENTION
    assert item.buyer == "某单位"
    assert "45万元" in item.body
    assert item.attachments[0].url == "https://www.plap.mil.cn/files/plap-001.docx"


def test_ceb_public_list_parser_keeps_bulletin_id_region_and_publisher():
    items = CEBPubServiceSource.parse_list_page(
        fixture("cebpubservice_search.html"), EventType.TENDER
    )
    assert len(items) == 2
    first = items[0]
    assert first.region == "北京"
    assert first.event_type == EventType.TENDER
    assert "uuid=ceb-uuid-001" in first.source_url
    assert first.source_metadata["industry"] == "信息电子"
    assert "中国电信阳光采购网" in first.body
    assert items[1].event_type == EventType.AWARD
