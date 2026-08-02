from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from lxml import etree

TEMPLATE_SHA256 = "D7C138C4A010E67E652527C235DB1FEE6C0DAF47F6A8653A7C820970C3E76D19"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


def qn(local: str) -> str:
    return f"{{{W}}}{local}"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def text_of(element) -> str:
    return "".join(element.xpath(".//w:t/text()", namespaces=NS))


def set_paragraph_text(paragraph, text: str) -> None:
    runs = paragraph.findall(qn("r"))
    keeper = runs[0] if runs else etree.SubElement(paragraph, qn("r"))
    for run in runs[1:]:
        paragraph.remove(run)
    for child in list(keeper):
        if child.tag != qn("rPr"):
            keeper.remove(child)
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if index:
            etree.SubElement(keeper, qn("br"))
        node = etree.SubElement(keeper, qn("t"))
        if line.startswith(" ") or line.endswith(" "):
            node.set(XML_SPACE, "preserve")
        node.text = line


def set_cell_text(cell, text: str) -> None:
    paragraphs = cell.findall(qn("p"))
    paragraph = paragraphs[0] if paragraphs else etree.SubElement(cell, qn("p"))
    for child in list(cell):
        if child.tag != qn("tcPr") and child is not paragraph:
            cell.remove(child)
    if cell[-1] is not paragraph:
        cell.remove(paragraph)
        cell.append(paragraph)
    set_paragraph_text(paragraph, text)


def find_table(body, token: str):
    for table in body.findall(qn("tbl")):
        if token in text_of(table):
            return table
    raise RuntimeError(f"没有找到模板表格：{token}")


def cell_at(table, row_index: int, cell_index: int):
    rows = table.findall(qn("tr"))
    cells = rows[row_index].findall(qn("tc"))
    return cells[cell_index]


def patch_document(document_xml: bytes) -> bytes:
    parser = etree.XMLParser(remove_blank_text=False)
    root = etree.fromstring(document_xml, parser)
    body = root.find(qn("body"))
    if body is None:
        raise RuntimeError("模板缺少 w:body")
    original_paragraphs = body.findall(qn("p"))
    original_tables = body.findall(qn("tbl"))
    if len(original_paragraphs) < 30 or len(original_tables) != 11:
        raise RuntimeError("模板结构与已核验的 4 页、11 表格版本不一致")

    set_paragraph_text(
        original_paragraphs[0],
        "【40强赛】超聚变🤝聚标成擎｜标擎 BidPilot——证据驱动的招投标情报 Agent",
    )
    for paragraph in original_paragraphs:
        if text_of(paragraph) in {
            "请在8月16日之前提交参赛完整方案",
            "[Timer - 2026-08-16 23:59]",
        }:
            body.remove(paragraph)

    for table in list(body.findall(qn("tbl"))):
        content = text_of(table)
        if (
            "以下模板供同学们参考" in content
            or "提交时请删除以下评分维度表格" in content
            or ("评分项" in content and "AI 应用创新性" in content)
        ):
            body.remove(table)

    info = find_table(body, "成员介绍&分工")
    info_values = {
        "队名": "聚标成擎",
        "命题": "超聚变｜招投标信息聚合工具",
        "一句话摘要": (
            "把一句中文问题编译成可审计的多源检索或长期订阅任务，"
            "从真实公告证据出发完成筛选、去重、Word 交付与机会跟进。"
        ),
        "成员介绍&分工": (
            "核心研发成员（单人队）：担任产品负责人、全栈研发与交付负责人，"
            "完成命题拆解、架构设计、AI/检索、前后端、测试、文档与演示。"
        ),
        "使用的飞书 AI 能力": (
            "飞书会议智能纪要/妙记：用于 Demo 录制后的自动转写、章节摘要和可检索回看；"
            "飞书机器人/应用用于报告交付（按平台能力如实标注，不冒充 AI）。"
        ),
    }
    for row in info.findall(qn("tr"))[1:]:
        cells = row.findall(qn("tc"))
        key = text_of(cells[0]).strip()
        if key in info_values:
            set_cell_text(cells[1], info_values[key])

    set_cell_text(
        cell_at(find_table(body, "请清晰描述你所选命题"), 0, 0),
        "场景：售前、市场与投标团队需要持续跟踪分散在政府、公共资源交易和行业平台的采购意向、"
        "招标、更正、中标与合同信息。\n"
        "核心问题：入口多、字段异构、登录边界不同；人工搜索既漏信息又重复，转载噪声难去重，"
        "搜索结果和后续行动脱节。\n"
        "业务影响：人员每天反复检索和复制，早期机会发现慢；同一项目多阶段公告割裂；"
        "无法证明信息覆盖，也难稳定做到“只发新增”。\n"
        "本方案流程：一句话问题 → 意图确认 → 有界多源检索 → 证据清洗与硬过滤 → "
        "跨站去重/生命周期 → Word 与多目标增量投递 → 机会/买方/反馈闭环。",
    )
    set_cell_text(
        cell_at(find_table(body, "几句话凸显你的方案亮点"), 0, 0),
        "1｜证据优先：AI 只能在真实公告证据内解释，标题、时间、采购人和 URL 由本地固定回填。\n"
        "2｜登录不是假绿灯：中国招标投标网必须真实解锁搜索与免费会员详情；千里马保持用户本人登录的"
        "可见浏览器，只做即时单次首屏。\n"
        "3｜从“搜链接”到“经营闭环”：跨站去重、生命周期、企业适配、机会工作台、买方雷达和反馈学习贯通。\n"
        "4｜可靠增量：逐目标持久 Outbox、租约、有限退避、死信与版本账本，服务重启后继续。",
    )
    before_after = find_table(body, "Before")
    set_cell_text(
        cell_at(before_after, 0, 0),
        "Before\n多站逐个搜索，关键词和时间口径靠记忆\n复制粘贴、转载重复、阶段割裂\n"
        "0 条不知道是无项目还是来源失败\n订阅和邮件发送难追踪、易重复\nAI 可能生成无法回指的结论",
    )
    set_cell_text(
        cell_at(before_after, 0, 1),
        "After\n一句话形成可确认的主题/地域/时间/计划\n10 个真实来源独立诊断、跨站去重和生命周期聚合\n"
        "扫描→候选→保留漏斗解释每个 0 条\n逐目标新增账本和持久 Outbox 可恢复\nAI 必须引用真实证据",
    )
    set_cell_text(
        cell_at(find_table(body, "非常重要！请展示方案完整核心功能"), 0, 0),
        "完整核心链路\n"
        "① 规则优先的混合意图引擎：rules-v2 先生成确定性基线；仅对低置信、缺失或冲突字段调用"
        "OpenAI-compatible 模型。模型只返回严格 JSON 提议，本地逐字段校验并生成签名确认快照。\n"
        "② 有界多源召回：本地同义词、行业词与模型发现词分层，最多两轮补搜。10 个真实适配器独立运行，"
        "单源失败不拖垮整轮；时间、地域、公告类型和排除词始终本地硬校验。\n"
        "③ 登录来源双模式：中国招标投标网只有真实搜索和免费会员详情同时解锁才进入后台；千里马由用户本人"
        "在独立可见浏览器登录，系统保持同一进程，只允许即时任务提交一个主题、读取首屏最多 20 条，不导出 "
        "Cookie、不翻页、不访问付费详情。\n"
        "④ 证据流水线：定向 DOM 抽取和正文清洗移除导航/广告；保留项目编号、采购人、发布时间、地域、正文、"
        "附件和原文 URL；严格过滤后跨站去重，并把招标、更正、中标和合同聚成项目生命周期。\n"
        "⑤ AI 情报与决策：默认离线抽取；可选模型生成证据约束简报、企业适配与本轮追问。证据引句必须逐字存在，"
        "未知引用和未经证据支持的数字动作会被拒绝。\n"
        "⑥ 可交付、可恢复：即时/每日/每周/每月/一次性计划由 SQLite worker 执行。Word、逐目标 Outbox 和"
        "公告版本在外发前原子落库；成功目标不因其他目标失败而重发，失败目标可有限重试和单目标死信恢复。",
    )
    set_cell_text(
        cell_at(find_table(body, "描述方案落地后"), 0, 0),
        "试点目标（不是既有客户业绩，需在超聚变内部基线验证）\n"
        "效率：把每次 2—4 小时的多站检索与整理，压缩为 15—30 分钟的人机复核；首周以真实计时对照验证。\n"
        "质量：目标来源覆盖率 ≥90%，保留结果原文可回溯率 100%，跨站重复进入人工清单的比例降低 ≥70%。\n"
        "时效：新公告在计划执行后 30 分钟内形成 Word/通知；登录或来源失败必须在同一轮披露。\n"
        "经营：将标讯直接转为项目机会、负责人、下一步和买方监控；观察有效机会转化和首次响应时长。\n"
        "推广：适配器、意图规则、渠道和企业画像均模块化，可复制到其他产品线和地域。",
    )
    set_cell_text(
        cell_at(find_table(body, "体验入口（如有）"), 0, 0),
        "本机体验：按《零基础操作说明书》运行 python bootstrap.py --dev，再执行 python -m bidpilot serve，"
        "打开 http://127.0.0.1:8000；API 文档位于 /docs。\n"
        "推荐问题一：最近一年广东省服务器招标信息。\n"
        "推荐问题二：最近一年物业小区劳务购买服务招标信息（演示零结果诊断）。\n"
        "登录来源：来源中心分别验证中国招标投标网免费会员详情、千里马用户前台首屏。\n"
        "Demo：交付包 04_Demo/Demo录制脚本与分镜.md。提交前使用飞书会议录制并生成妙记，将公开链接"
        "回填到本段；不提供虚构测试账号或不可访问链接。",
    )
    set_cell_text(
        cell_at(find_table(body, "这里是你的自由展示区"), 0, 0),
        "工程证据\n"
        "10 个真实来源适配器；52 个中文 OpenAPI 操作；62 个网页/API 可写配置字段。\n"
        "Web、REST API、CLI、原生 Python 和 Docker Compose；SQLite 租约支持重启恢复。\n"
        "本地、飞书、SMTP、钉钉、企业微信、通用 Webhook、Telegram 和 Slack 多目标投递。\n"
        "59 页零基础说明书、真实 UI 截图、API/配置/渠道/用户/比赛验收文档。\n"
        "安全门禁覆盖域名白名单、本机加密、Cookie/Token 脱敏、局域网策略、严格 JSON 与证据引用。\n\n"
        "推广路径\n第 1 周测量人工基线并确认来源；第 2—4 周灰度每日任务和机会工作台；"
        "第 2 月接入企业正式渠道与权限审计；第 3 月基于真实反馈扩展来源和行业词典。",
    )
    set_paragraph_text(
        original_paragraphs[27],
        "项目从一个“多站搜索脚本”演进为证据闭环。最费时间的不是把页面抓下来，而是承认每个来源的真实边界："
        "零结果不能等于失败，Cookie 存在不能等于登录成功，付费内容不能包装成免费能力。团队为此建立固定检索评测、"
        "失败注入、真实授权验证和逐页 Word 复核；每次发现假绿灯，都把它变成状态机和回归测试。",
    )
    set_paragraph_text(
        original_paragraphs[29],
        "超聚变命题页：https://activity.feishu.cn/future-talent?detail=chaojubian\n"
        "千里马会员注册须知：https://center.qianlima.com/register_xz.jsp\n"
        "FastAPI：https://fastapi.tiangolo.com/｜Playwright：https://playwright.dev/python/\n"
        "Python：https://docs.python.org/3/｜SQLite：https://www.sqlite.org/docs.html",
    )
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone="yes")


def build(template: Path, output: Path) -> None:
    if sha256(template) != TEMPLATE_SHA256:
        raise RuntimeError("模板 SHA-256 与已核验原件不一致，拒绝继续")
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(template, "r") as source, ZipFile(output, "w", ZIP_DEFLATED) as target:
        for info in source.infolist():
            payload = source.read(info.filename)
            if info.filename == "word/document.xml":
                payload = patch_document(payload)
            target.writestr(info, payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="从官方 40 强模板生成 BidPilot 参赛方案")
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.template.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()
