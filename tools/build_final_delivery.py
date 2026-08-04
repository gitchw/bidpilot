"""Build the competition-final DOCX set from the official template and repository docs."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Mm, Pt, RGBColor

INK = "172033"
BODY = "30394A"
MUTED = "657085"
BLUE = "2F6BFF"
BLUE_DARK = "1F3864"
BLUE_LIGHT = "EAF0FF"
PURPLE = "7B47FF"
PURPLE_LIGHT = "F7F3FF"
ORANGE = "F5A24B"
ORANGE_LIGHT = "FFF5EA"
LINE = "D9DFEA"
WHITE = "FFFFFF"


def set_font(run, size: float, *, bold: bool = False, color: str = BODY) -> None:
    run.font.name = "Arial"
    run._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "微软雅黑")
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = RGBColor.from_string(color)


def ensure_style(
    document: Document,
    name: str,
    *,
    size: float,
    bold: bool = False,
    color: str = BODY,
    before: float = 0,
    after: float = 6,
    line_spacing: float = 1.22,
) -> None:
    try:
        style = document.styles[name]
    except KeyError:
        style = document.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
    style.font.name = "Arial"
    style._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "微软雅黑")
    style.font.size = Pt(size)
    style.font.bold = bold
    style.font.color.rgb = RGBColor.from_string(color)
    style.paragraph_format.space_before = Pt(before)
    style.paragraph_format.space_after = Pt(after)
    style.paragraph_format.line_spacing = line_spacing


def set_repeat_table_header(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    header = OxmlElement("w:tblHeader")
    header.set(qn("w:val"), "true")
    tr_pr.append(header)


def keep_row_together(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    cant_split = OxmlElement("w:cantSplit")
    tr_pr.append(cant_split)


def shade_cell(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def set_cell_margins(
    cell, *, top: int = 80, start: int = 100, bottom: int = 80, end: int = 100
) -> None:
    tc = cell._tc
    tc_pr = tc.get_or_add_tcPr()
    tc_mar = tc_pr.first_child_found_in("w:tcMar")
    if tc_mar is None:
        tc_mar = OxmlElement("w:tcMar")
        tc_pr.append(tc_mar)
    for margin, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = tc_mar.find(qn(f"w:{margin}"))
        if node is None:
            node = OxmlElement(f"w:{margin}")
            tc_mar.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def set_table_borders(table, color: str = LINE, size: str = "4") -> None:
    tbl_pr = table._tbl.tblPr
    borders = tbl_pr.find(qn("w:tblBorders"))
    if borders is None:
        borders = OxmlElement("w:tblBorders")
        tbl_pr.append(borders)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        node = borders.find(qn(f"w:{edge}"))
        if node is None:
            node = OxmlElement(f"w:{edge}")
            borders.append(node)
        node.set(qn("w:val"), "single")
        node.set(qn("w:sz"), size)
        node.set(qn("w:space"), "0")
        node.set(qn("w:color"), color)


def set_cell_text(
    cell,
    text: str,
    *,
    bold: bool = False,
    color: str = BODY,
    size: float = 9.5,
    align=WD_ALIGN_PARAGRAPH.LEFT,
) -> None:
    cell.text = ""
    paragraph = cell.paragraphs[0]
    paragraph.alignment = align
    paragraph.paragraph_format.space_after = Pt(0)
    paragraph.paragraph_format.line_spacing = 1.15
    run = paragraph.add_run(text)
    set_font(run, size, bold=bold, color=color)
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    set_cell_margins(cell)


def add_table(
    document: Document,
    headers: list[str],
    rows: list[list[str]],
    *,
    header_fill: str = BLUE_DARK,
    header_text: str = WHITE,
    first_column_fill: str | None = None,
    widths_cm: list[float] | None = None,
) -> object:
    table = document.add_table(rows=1, cols=len(headers))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    set_table_borders(table)
    set_repeat_table_header(table.rows[0])
    for index, header in enumerate(headers):
        set_cell_text(table.cell(0, index), header, bold=True, color=header_text, size=9.5)
        shade_cell(table.cell(0, index), header_fill)
        if widths_cm:
            table.cell(0, index).width = Cm(widths_cm[index])
    for row_index, values in enumerate(rows):
        cells = table.add_row().cells
        keep_row_together(table.rows[-1])
        normalized_values = [*values[: len(headers)], *([""] * max(0, len(headers) - len(values)))]
        for column, value in enumerate(normalized_values):
            set_cell_text(cells[column], value, bold=column == 0 and first_column_fill is not None)
            if widths_cm:
                cells[column].width = Cm(widths_cm[column])
            if column == 0 and first_column_fill:
                shade_cell(cells[column], first_column_fill)
            elif row_index % 2:
                shade_cell(cells[column], "F8FAFD")
    document.add_paragraph().paragraph_format.space_after = Pt(0)
    return table


def add_hyperlink(paragraph, text: str, url: str) -> None:
    relationship_id = paragraph.part.relate_to(
        url,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True,
    )
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), relationship_id)
    run = OxmlElement("w:r")
    run_properties = OxmlElement("w:rPr")
    fonts = OxmlElement("w:rFonts")
    fonts.set(qn("w:ascii"), "Arial")
    fonts.set(qn("w:hAnsi"), "Arial")
    fonts.set(qn("w:eastAsia"), "微软雅黑")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), BLUE)
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    run_properties.extend([fonts, color, underline])
    run.append(run_properties)
    node = OxmlElement("w:t")
    node.text = text
    run.append(node)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)


def add_page_field(paragraph) -> None:
    paragraph.add_run("—  ")
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instruction = OxmlElement("w:instrText")
    instruction.set(qn("xml:space"), "preserve")
    instruction.text = " PAGE "
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    text = OxmlElement("w:t")
    text.text = "1"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.extend([begin, instruction, separate, text, end])
    paragraph.add_run("  —")


def clear_body(document: Document) -> None:
    body = document._element.body
    for child in list(body):
        if child.tag != qn("w:sectPr"):
            body.remove(child)


def configure_document(
    document: Document, *, header_text: str, source_derived: bool = False
) -> None:
    section = document.sections[0]
    section.page_width = Mm(210)
    section.page_height = Mm(297)
    section.top_margin = Mm(18 if source_derived else 20)
    section.bottom_margin = Mm(18 if source_derived else 20)
    section.left_margin = Mm(20 if source_derived else 22)
    section.right_margin = Mm(20 if source_derived else 22)
    section.header_distance = Mm(8)
    section.footer_distance = Mm(9)

    for name, args in {
        "Normal": dict(size=10.5, color=BODY, after=6, line_spacing=1.22),
        "Doc Title": dict(size=28, bold=True, color=INK, after=8, line_spacing=1.05),
        "Doc Subtitle": dict(size=13, color=BLUE, after=16, line_spacing=1.1),
        "Heading 1": dict(size=18, bold=True, color=INK, before=12, after=8, line_spacing=1.08),
        "Heading 2": dict(size=14, bold=True, color=BLUE, before=12, after=6, line_spacing=1.1),
        "Heading 3": dict(size=11.5, bold=True, color=INK, before=9, after=4, line_spacing=1.15),
        "Caption": dict(size=8.5, color=MUTED, after=8, line_spacing=1.0),
        "Code": dict(size=8.5, color=INK, after=4, line_spacing=1.0),
    }.items():
        ensure_style(document, name, **args)

    header = section.header
    header.is_linked_to_previous = False
    paragraph = header.paragraphs[0]
    paragraph.clear()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = paragraph.add_run(header_text)
    set_font(run, 8, bold=True, color=MUTED)
    border = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "4")
    bottom.set(qn("w:space"), "3")
    bottom.set(qn("w:color"), LINE)
    border.append(bottom)
    paragraph._p.get_or_add_pPr().append(border)

    footer = section.footer
    footer.is_linked_to_previous = False
    paragraph = footer.paragraphs[0]
    paragraph.clear()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    add_page_field(paragraph)
    for run in paragraph.runs:
        set_font(run, 8, color=MUTED)

    settings = document.settings._element
    update_fields = settings.find(qn("w:updateFields"))
    if update_fields is None:
        update_fields = OxmlElement("w:updateFields")
        settings.append(update_fields)
    # PAGE fields are refreshed by Word's print/export layout.  Forcing every
    # field to update immediately on open can make desktop Word repaginate
    # indefinitely for documents that contain long tables.
    update_fields.set(qn("w:val"), "false")


def add_title(document: Document, text: str, subtitle: str | None = None) -> None:
    paragraph = document.add_paragraph(style="Doc Title")
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.add_run(text)
    if subtitle:
        paragraph = document.add_paragraph(style="Doc Subtitle")
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.add_run(subtitle)


def add_heading(document: Document, text: str, level: int = 1, *, page_break: bool = False) -> None:
    if page_break:
        document.add_page_break()
    paragraph = document.add_paragraph(style=f"Heading {level}")
    paragraph.paragraph_format.keep_with_next = True
    run = paragraph.add_run(text)
    if level == 1:
        marker = paragraph._p.get_or_add_pPr()
        border = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), "12")
        bottom.set(qn("w:space"), "5")
        bottom.set(qn("w:color"), BLUE)
        border.append(bottom)
        marker.append(border)
    set_font(run, {1: 18, 2: 14, 3: 11.5}[level], bold=True, color=INK if level != 2 else BLUE)


def add_paragraph(document: Document, text: str, *, bold_lead: str | None = None) -> None:
    paragraph = document.add_paragraph(style="Normal")
    paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    if bold_lead and text.startswith(bold_lead):
        lead = paragraph.add_run(bold_lead)
        set_font(lead, 10.5, bold=True, color=INK)
        body = paragraph.add_run(text[len(bold_lead) :])
        set_font(body, 10.5)
    else:
        run = paragraph.add_run(text)
        set_font(run, 10.5)


def add_bullets(document: Document, items: list[str]) -> None:
    for item in items:
        paragraph = document.add_paragraph(style="List Bullet")
        paragraph.paragraph_format.space_after = Pt(3)
        run = paragraph.add_run(item)
        set_font(run, 10.3)


def add_callout(
    document: Document,
    title: str,
    body: str,
    *,
    fill: str = PURPLE_LIGHT,
    accent: str = PURPLE,
) -> None:
    table = document.add_table(rows=1, cols=1)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    set_table_borders(table, color=accent, size="6")
    cell = table.cell(0, 0)
    shade_cell(cell, fill)
    set_cell_margins(cell, top=120, start=150, bottom=120, end=150)
    cell.text = ""
    paragraph = cell.paragraphs[0]
    paragraph.paragraph_format.space_after = Pt(3)
    run = paragraph.add_run(title)
    set_font(run, 11, bold=True, color=accent)
    paragraph = cell.add_paragraph()
    paragraph.paragraph_format.space_after = Pt(0)
    paragraph.paragraph_format.line_spacing = 1.18
    run = paragraph.add_run(body)
    set_font(run, 10)
    document.add_paragraph().paragraph_format.space_after = Pt(0)


def add_image(document: Document, path: Path, caption: str, *, width_cm: float = 16.5) -> None:
    if not path.exists():
        return
    paragraph = document.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.space_after = Pt(3)
    image = paragraph.add_run().add_picture(str(path), width=Cm(width_cm))
    image._inline.docPr.set("title", caption)
    image._inline.docPr.set("descr", caption)
    paragraph = document.add_paragraph(style="Caption")
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run(caption)
    set_font(run, 8.5, color=MUTED)


def build_competition_docx(template: Path, output: Path, repo: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(template, output)
    document = Document(output)
    clear_body(document)
    configure_document(
        document,
        header_text="聚标成擎 × 超聚变｜标擎 BidPilot 40 强参赛方案",
        source_derived=True,
    )
    document.core_properties.title = "标擎 BidPilot——证据驱动的招投标情报 Agent"
    document.core_properties.subject = "超聚变招投标信息聚合工具命题 40 强参赛方案"
    document.core_properties.author = "聚标成擎"

    document.add_paragraph().paragraph_format.space_after = Pt(42)
    eyebrow = document.add_paragraph()
    eyebrow.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = eyebrow.add_run("AI 先锋未来人才大赛 · 40 强赛完整方案")
    set_font(run, 10, bold=True, color=BLUE)
    add_title(
        document,
        "超聚变 🤝 聚标成擎",
        "标擎 BidPilot——证据驱动的招投标情报 Agent",
    )
    document.add_paragraph().paragraph_format.space_after = Pt(16)
    add_callout(
        document,
        "一句话价值",
        "把一句自然语言编译成可审计的即时检索或长期订阅任务，从真实多源公告出发，完成清洗、硬过滤、跨站去重、证据约束 AI、Word 与仅新增投递，并把可信标讯接入机会和买方经营闭环。",
        fill=ORANGE_LIGHT,
        accent=ORANGE,
    )
    document.add_paragraph().paragraph_format.space_after = Pt(24)
    meta = document.add_paragraph()
    meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = meta.add_run(
        "命题企业：超聚变  ·  团队：聚标成擎  ·  版本：v0.8.0 跨平台终版  ·  提交日期：2026-08"
    )
    set_font(run, 9, color=MUTED)
    add_bullets(
        document,
        [
            "10 个真实来源适配器，包含 2 个经用户本人登录并真实验证的免费会员来源。",
            "288 项测试在 Windows 与 Debian 原生环境分别通过，GitHub 跨平台与容器 7 个作业全部通过。",
            "不硬编码结果，不伪造登录，不导出千里马 Cookie，不绕过验证码、频率或付费权限。",
        ],
    )

    add_heading(document, "一、参赛方案信息卡【必须包含】", page_break=True)
    add_table(
        document,
        ["项目", "填写内容"],
        [
            ["队名", "聚标成擎"],
            ["命题", "超聚变｜招投标信息聚合工具"],
            ["作品", "标擎 BidPilot——证据驱动的招投标情报 Agent"],
            [
                "一句话摘要",
                "把自然语言需求转化为可审计的多源检索/订阅任务，以真实公告证据生成 Word、增量投递和机会洞察。",
            ],
            [
                "成员介绍与分工",
                "成员 1｜产品与技术负责人（姓名以报名信息为准）：命题拆解、产品架构、全栈研发、AI/检索、测试、跨平台部署与交付。\n成员 2｜审计与合规负责人（姓名以报名信息为准）：需求审计、数据来源权限边界复核、证据链抽查、验收清单、风险披露与材料质量复核。",
            ],
            [
                "使用的飞书 AI 能力",
                "飞书会议智能纪要/妙记：Demo 录制后的自动转写、章节摘要与可检索回看。系统另支持飞书机器人/应用投递 Word；该能力按消息交付描述，不冒充 AI。",
            ],
        ],
        widths_cm=[3.2, 13.0],
        first_column_fill=BLUE_LIGHT,
    )
    add_callout(
        document,
        "评审一眼看懂",
        "创新不是“用大模型写摘要”，而是把自然语言、真实来源、登录状态、证据约束、增量账本和业务行动组织成一个可运行、可恢复、可审计的 Agent 工作流。",
    )
    add_heading(document, "四大评分维度的证据映射", level=2)
    add_table(
        document,
        ["评分维度", "我们的应答", "可核验证据"],
        [
            [
                "AI 应用创新性 30%",
                "规则基线 + 有界 LLM 提议 + 字段级确认快照；证据 ID 约束的情报、适配与问答",
                "检索计划、决策轨迹、证据引句、严格 JSON 拒绝测试",
            ],
            [
                "业务价值 30%",
                "多站人工检索升级为持续机会经营；指标均给出试点基线与公式",
                "Word、机会工作台、买方雷达、4 周试点量化方案",
            ],
            [
                "AI 应用深度 20%",
                "AI 参与意图、查询规划、边界复核、摘要、机会适配与本轮问答，但关键字段仍由本地证据锁定",
                "模块图、API、缓存与回退、未知引用拒绝",
            ],
            [
                "完整与落地 20%",
                "Web/API/CLI、10 来源、登录态、定时、增量、可靠投递、Windows/Linux/macOS、文档与测试闭环",
                "源代码、288 项测试、7 作业 CI、systemd/Compose",
            ],
        ],
        widths_cm=[3.2, 7.0, 6.0],
    )
    add_heading(document, "评审导航｜从评分维度到可核验证据", level=2)
    add_callout(
        document,
        "建议阅读顺序",
        "先抽查真实运行结果 Word 与原文链接，再核对详设、源码和测试记录，最后按 4 分 30 秒 Demo 脚本复现输入到输出。",
        fill=ORANGE_LIGHT,
        accent=ORANGE,
    )
    add_table(
        document,
        ["评分维度", "建议核验路径"],
        [
            ["AI 创新", "检索计划、字段确认快照、E 编号证据约束与拒绝测试"],
            ["业务价值", "运行 Word、机会工作台、买方雷达与 4 周量化试点"],
            ["AI 深度", "意图、规划、复核、摘要、适配、问答及确定性回退"],
            ["完整落地", "Web / API / CLI、10 来源、登录、增量投递与跨平台部署"],
        ],
        widths_cm=[3.4, 12.8],
        first_column_fill=PURPLE_LIGHT,
    )
    document.add_page_break()
    add_heading(document, "二、方案成果展示【必须包含】")
    add_heading(document, "1. 命题场景、问题与痛点", level=2)
    add_paragraph(
        document,
        "企业市场、采购、合规、竞争情报和项目拓展团队需要持续跟踪政府平台、公共资源交易中心、行业网站、商业聚合站和企业采购门户。真实工作不是一次搜索，而是每天重复完成“理解需求—多站检索—筛选核验—汇总发送—跟进项目”。",
    )
    add_table(
        document,
        ["痛点", "人工现状", "业务影响", "标擎应答"],
        [
            [
                "需求口径不稳定",
                "主题、地域、时间和频率依赖个人理解",
                "同一句需求由不同人员得到不同结果",
                "意图确认卡 + 字段级轨迹 + 签名快照",
            ],
            [
                "来源分散且异构",
                "逐站切换，列表/详情/PDF/动态页规则不一",
                "高价值线索漏检，搜索成本高",
                "10 个独立适配器 + 失败隔离 + 来源漏斗",
            ],
            [
                "登录态真假难判",
                "看到 Cookie 或页面就当作登录成功",
                "免费会员内容仍不可用，验收出现假绿灯",
                "搜索与免费详情双验证；千里马真实首屏验证",
            ],
            [
                "转载重复与阶段割裂",
                "同一公告多次复制，意向/招标/更正/中标分散",
                "重复跟进、上下文丢失",
                "跨站去重 + 项目生命周期聚合",
            ],
            [
                "0 条不可解释",
                "不知道是没有项目、关键词过窄还是来源失败",
                "错误判断市场机会",
                "扫描→候选→保留漏斗 + 覆盖缺口披露",
            ],
            [
                "定时发送不可靠",
                "脚本重启丢任务，多渠道失败后整轮重发",
                "漏发、重复、难审计",
                "持久 worker + 逐目标 Outbox + 增量版本账本",
            ],
        ],
        widths_cm=[2.6, 4.4, 4.3, 5.0],
    )
    add_heading(document, "Before / After", level=3)
    add_table(
        document,
        ["Before｜人工拼接", "After｜证据驱动 Agent"],
        [
            [
                "多个站点逐个搜索；关键词和时间口径靠记忆",
                "一句话形成可确认的主题、地域、时间、频率和交付目标",
            ],
            ["复制粘贴、转载重复、项目阶段割裂", "定向清洗、硬过滤、跨站聚类与生命周期视图"],
            ["0 条无法区分没有项目与来源异常", "每个来源给出状态、漏斗、排除理由和安全放宽建议"],
            ["定时脚本和邮件失败不可追踪", "逐目标持久 Outbox、有限重试、死信与恢复"],
            ["AI 结论无法回指原文", "所有结论绑定 E 编号、真实 URL 与逐字证据引句"],
        ],
        header_fill=BLUE,
        widths_cm=[8.1, 8.1],
    )

    add_heading(document, "2. 方案优势与创新点", page_break=True)
    add_callout(
        document,
        "创新 1｜可审计的混合意图编译器",
        "rules-v2 先给出确定性基线；仅在低置信、缺失或冲突字段调用可选 LLM。模型只返回严格 JSON 提议，本地逐字段校验后生成可确认、可签名、可复用的意图快照，避免同一需求在创建订阅时二次漂移。",
        fill=BLUE_LIGHT,
        accent=BLUE,
    )
    add_callout(
        document,
        "创新 2｜检索 Agent 有预算、有证据、有止损",
        "同义词、行业词与模型发现词分层；最多两轮、逐来源查询预算、URL 去重和覆盖缺口触发。模型发现词只扩大召回，不能直接成为相关性事实；日期、地域、公告类型与排除词始终本地硬过滤。",
    )
    add_callout(
        document,
        "创新 3｜登录不是 Cookie 存在，而是真实价值解锁",
        "中国招标投标网必须同时证明站内搜索和免费会员详情可读；千里马由本人可见登录、系统托管持久配置，只允许即时单主题首屏。失败、无法判断和过期均不会进入增强检索。",
        fill=ORANGE_LIGHT,
        accent=ORANGE,
    )
    add_callout(
        document,
        "创新 4｜从搜索结果到企业机会经营",
        "Word 不是终点。真实标讯可以进入项目级机会工作台，生命周期新事件更新证据但不覆盖人工负责人/下一步；买方雷达与企业能力画像让团队围绕真实证据决定投、关注或跳过。",
        fill=BLUE_LIGHT,
        accent=BLUE,
    )
    add_callout(
        document,
        "创新 5｜可靠增量不是一句“去重”",
        "公告版本、目标级成功账本与 Outbox 在外发前原子持久化。成功目标不因其他目标失败而重发；失败目标有限退避，达到上限进入死信，可单目标恢复且无需重新抓取。",
    )

    add_heading(document, "3. 具体方案说明（突出 AI 能力）", page_break=True)
    add_heading(document, "3.1 总体架构与数据流", level=2)
    add_table(
        document,
        ["① 输入层", "② Agent 编排层", "③ 证据层", "④ 交付与行动层"],
        [
            [
                "自然语言\nWeb / API / CLI",
                "规则基线\n可选 LLM 提议\n检索计划\n最多两轮补搜",
                "10 个来源\n登录状态机\n清洗/硬过滤\n去重/生命周期",
                "证据约束简报\nWord\n定时增量\nOutbox\n机会/买方/反馈",
            ]
        ],
        header_fill=BLUE_DARK,
        widths_cm=[4.0, 4.2, 4.2, 4.0],
    )
    add_paragraph(
        document,
        "控制面负责配置、来源授权、意图确认、订阅和机会管理；数据面负责抓取、清洗、过滤、去重、证据入库、报告和投递。SQLite 保存业务数据、运行证据、租约、版本账本和 Outbox；敏感配置另以本机密钥加密。",
    )
    add_heading(document, "3.2 AI 在核心链路中的位置", level=2)
    add_table(
        document,
        ["环节", "AI 参与", "确定性门禁", "失败回退"],
        [
            [
                "意图解析",
                "低置信字段的严格 JSON 修正",
                "日期/地域/频率再次本地校验",
                "使用 rules-v2 基线",
            ],
            [
                "检索规划",
                "提出发现词、来源优先级和安全补搜",
                "轮数与逐来源预算；发现词不等于可信相关词",
                "固定扩展词与首轮结果",
            ],
            [
                "语义边界",
                "批量复核边缘候选",
                "必须引用候选证据，硬过滤不交给模型",
                "确定性相关度阈值",
            ],
            [
                "情报简报",
                "买方需求、优先机会、风险与行动",
                "仅接受已知 E 编号；引句须逐字存在",
                "抽取式简报",
            ],
            [
                "企业适配",
                "结合能力画像给出投/关注/跳过",
                "数字与资格结论必须有公告证据",
                "透明规则评分",
            ],
            ["证据问答", "回答本轮追问", "只允许当前运行证据集；未知引用拒绝", "提示证据不足"],
        ],
        widths_cm=[2.5, 5.3, 5.3, 3.1],
    )
    add_callout(
        document,
        "原创 Prompt / 工作流原则",
        "模型看到的是有界结构化任务与 E 编号证据，不看到整个数据库、Cookie、Webhook 或系统密钥；输出先过 JSON schema、已知引用、逐字引句和 URL 禁止校验，再进入业务对象。模型失败不会中断报告。",
    )

    add_heading(document, "3.3 多来源、登录态与合规边界", page_break=True)
    add_table(
        document,
        ["来源组", "能力", "边界"],
        [
            [
                "公开官方来源",
                "政府采购、公共资源、国际招标、军队采购、广东/深圳等地域来源",
                "真实网络独立诊断；不把首页流冒充历史全量",
            ],
            [
                "中国招标投标网",
                "用户本人会话用于站内搜索与免费会员详情",
                "允许域名 HTTPS；搜索 + 详情双验证；7 天最长信任期",
            ],
            [
                "千里马",
                "公开分类 + 免费会员即时首屏",
                "首次可见登录；最多 20 条；10 秒冷却；不定时、不翻页、不读付费详情、不导出 Cookie",
            ],
        ],
        widths_cm=[3.4, 6.2, 6.8],
        first_column_fill=ORANGE_LIGHT,
    )
    add_paragraph(
        document,
        "真实验证记录：千里马“服务器”搜索首屏读取 20 条免费会员候选；普通即时任务通过本地时间与相关性硬校验保留 3 条 `auth_level=free_member` 记录。全过程未翻页、未进入详情、未导出 Cookie。",
        bold_lead="真实验证记录：",
    )
    add_heading(document, "3.4 清洗、去重与事实一致", level=2)
    add_bullets(
        document,
        [
            "定向 DOM/JSON 抽取，仅保留标题、发布时间、采购人、项目编号、地域、正文、附件与原文 URL；导航、广告和模板文案被移除。",
            "时间、地域、采购单位、公告类型、排除词和主题匹配产生可统计的淘汰理由；0 条报告保留扫描漏斗。",
            "跨站重复先按稳定 URL、项目编号、标题与采购人聚类；同项目不同阶段不被误删，而是进入生命周期。",
            "标题、时间、来源链接和附件由本地结构化记录固定回填；AI 无权改写原始事实字段。",
        ],
    )

    add_heading(document, "3.5 定时、仅新增与可靠投递", page_break=True)
    add_table(
        document,
        ["能力", "实现", "验收信号"],
        [
            [
                "时间与频率",
                "即时、每日、每周、每月、未来一次；时区固定为 Asia/Shanghai",
                "自然语言计划与 `next_run_at` 一致",
            ],
            [
                "仅新增",
                "按订阅 × 目标记录 canonical ID 与版本哈希",
                "同版本不重复；公告变化作为新版本推送",
            ],
            ["重启恢复", "worker 心跳、租约与 fencing token", "旧 worker 不能覆盖新接管者"],
            [
                "逐目标可靠性",
                "报告/Outbox/映射先落库，有限退避，5 次后死信",
                "成功目标不重发；失败目标可单独恢复",
            ],
            [
                "投递渠道",
                "本地、飞书、SMTP、钉钉、企业微信、通用 Webhook、Telegram、Slack",
                "能力元数据、脱敏配置和真实连接测试入口",
            ],
        ],
        widths_cm=[2.8, 8.1, 5.4],
    )
    add_callout(
        document,
        "可靠性诚实边界",
        "数据库内成功状态与目标级账本原子提交；但普通 Webhook/SMTP 在“外部已接收、本地回执前进程崩溃”的极小窗口仍可能重复，因此如实标注外部 at-least-once，不宣传所有平台端到端 exactly-once。",
        fill=ORANGE_LIGHT,
        accent=ORANGE,
    )

    assets = repo / "outputs" / "manuals" / "assets"
    add_heading(document, "3.6 可运行系统与交互证据", page_break=True)
    add_image(
        document, assets / "01-home.png", "图 1｜查询首页：自然语言输入、意图确认与可解释执行入口"
    )
    add_paragraph(
        document,
        "Web UI 将复杂能力拆成查询、订阅、机会、买方、决策、来源和配置 7 个可见工作区；同一业务能力也通过 REST API 与 CLI 提供，便于比赛验收和企业系统集成。",
    )
    add_image(
        document, assets / "07-sources.png", "图 2｜来源中心：能力、健康、授权状态与真实边界同屏"
    )

    add_heading(document, "3.7 从标讯到机会经营", page_break=True)
    add_image(
        document,
        assets / "04-opportunities.png",
        "图 3｜机会工作台：真实标讯、生命周期、负责人、下一步与标签",
    )
    add_image(
        document, assets / "05-buyers.png", "图 4｜买方雷达：基于本地证据聚合买方活跃度并创建监控"
    )
    add_paragraph(
        document,
        "机会只能从数据库中已存在的真实标讯创建，浏览器不能提交自造快照。生命周期新公告刷新证据并标记未读，但不会覆盖人工负责人、阶段、下一步、备注或标签。",
    )

    add_heading(document, "4. 方案价值", page_break=True)
    add_callout(
        document,
        "口径声明",
        "以下为建议在超聚变内部开展的 4 周试点目标与测量方法，不是既有客户业绩。所有数字必须先测人工基线，再由日志、报告与复核表验证。",
        fill=ORANGE_LIGHT,
        accent=ORANGE,
    )
    add_table(
        document,
        ["价值维度", "试点目标", "测量公式 / 证据"],
        [
            [
                "效率",
                "单次 2–4 小时人工检索整理压缩为 15–30 分钟人机复核；目标节省 75%–94%",
                "(人工基线时长 − BidPilot 复核时长) / 人工基线时长；抽样 20 个任务",
            ],
            [
                "覆盖",
                "已确认目标来源的计划执行覆盖率 ≥90%",
                "成功/部分成功来源数 ÷ 本轮适用来源数；失败必须披露",
            ],
            ["可信", "保留结果原文可回溯率 100%", "抽查 Word 每条来源 URL、时间、附件与核心内容"],
            [
                "降噪",
                "跨站重复进入人工清单的比例降低 ≥70%",
                "人工原清单重复数与生命周期聚类后重复数对比",
            ],
            [
                "时效",
                "计划触发后 30 分钟内形成 Word/通知",
                "`scheduled_at` 到报告/Outbox 首次落库时间",
            ],
            [
                "经营",
                "缩短首次跟进时间并提高有效机会转化",
                "机会创建到首次动作时长；投/关注/跳过与后续结果",
            ],
        ],
        widths_cm=[2.2, 7.3, 6.9],
    )
    add_heading(document, "4 周验证设计", level=2)
    add_table(
        document,
        ["周期", "动作", "交付"],
        [
            [
                "第 0 周",
                "确定来源、主题、人工检索口径，完成权限和合规确认",
                "基线样本、来源白名单、风险台账",
            ],
            [
                "第 1 周",
                "双轨运行：人工与 BidPilot 同时完成 20 个任务",
                "效率、覆盖、重复与事实一致对照",
            ],
            [
                "第 2–3 周",
                "启用每日/每周增量和机会工作台，观察失败恢复",
                "Outbox、增量账本、跟进时长",
            ],
            [
                "第 4 周",
                "复盘误报/漏报、人员反馈和来源健康，决定灰度范围",
                "试点评估、下一阶段 ROI 与治理清单",
            ],
        ],
        widths_cm=[2.2, 9.0, 5.1],
    )

    add_heading(document, "5. 方案体验入口与 Demo【建议包含】", page_break=True)
    add_table(
        document,
        ["入口", "说明"],
        [
            [
                "本机体验",
                "`python bootstrap.py --dev`，随后运行 `.venv` Python 的 `-m bidpilot serve`，打开 http://127.0.0.1:8000",
            ],
            [
                "API",
                "启动后打开 http://127.0.0.1:8000/docs；52 个中文 OpenAPI 操作覆盖查询、订阅、来源、配置、机会、买方与投递",
            ],
            [
                "Linux 后台",
                "按《Linux 部署与运维手册》安装 systemd Web/worker；默认仅监听 127.0.0.1",
            ],
            ["源码", "https://github.com/gitchw/bidpilot"],
            [
                "Demo 视频",
                "终版交付目录 `04_Demo/` 提供 4 分 30 秒脚本与分镜；公开视频链接在录制并设为公开可读后回填，不提供虚构链接或测试账号",
            ],
        ],
        widths_cm=[3.2, 13.0],
        first_column_fill=BLUE_LIGHT,
    )
    add_heading(document, "推荐演示问题", level=2)
    add_bullets(
        document,
        [
            "最近一年广东省服务器招标信息——展示多来源、清洗去重、Word 与原文证据。",
            "最近一年物业小区劳务购买服务招标信息——展示 0 条漏斗和安全放宽建议。",
            "服务器招标信息——展示千里马免费会员即时首屏 20 条扫描、3 条保留。",
            "最近 3 个月上海充电桩招标信息，每天早上 9 点汇总后发送给我——展示计划、仅新增和多目标 Outbox。",
        ],
    )

    add_heading(document, "三、自由展示区【加分项】", page_break=True)
    add_heading(document, "1. 工程完成度与跨平台证据", level=2)
    add_table(
        document,
        ["证据", "结果"],
        [
            [
                "Windows 本机",
                "288 项 Pytest；Ruff、format、compileall、JavaScript、JSON/YAML/TOML 全通过",
            ],
            [
                "Debian 13 / Python 3.13",
                "原生目录 288 项 Pytest 全通过；lint/format/compile/config 全通过",
            ],
            [
                "真实 systemd",
                "Web 与 worker 以 `bidpilot` 非特权用户 active；健康接口和 API/CLI 均确认 worker 在线；优雅停止后无残留",
            ],
            [
                "GitHub Actions",
                "Windows/Ubuntu/macOS × Python 3.11/3.13 + Ubuntu Compose/镜像构建，共 7 个作业全部 success",
            ],
            [
                "真实登录来源",
                "中国招标投标网搜索+免费详情验证；千里马首屏 20 条、普通任务保留 3 条",
            ],
            [
                "运行结果 Word",
                "终版目录至少 4 个不同问题；包含正向、零结果、公开来源与免费会员来源",
            ],
        ],
        widths_cm=[4.0, 12.2],
        first_column_fill=BLUE_LIGHT,
    )
    add_callout(
        document,
        "跨平台修复的审计价值",
        "原 CI 并非 Linux 业务逻辑崩溃，而是 POSIX ANSI 样式导致测试误报。修复保留了本机模式拒绝非回环绑定的安全行为，并把测试改为核验去样式后的用户可见语义；随后再补真实 systemd 与容器验证，避免“只改绿 CI”。",
    )

    add_heading(document, "2. 安全、权限与合规", level=2)
    add_bullets(
        document,
        [
            "默认只监听 127.0.0.1；LAN 仍不是公网身份系统。公网必须增加 HTTPS、登录、角色、限流和审计。",
            "敏感配置和来源授权本机加密；Windows 收紧 ACL，POSIX 使用目录 0700、文件 0600。",
            "请求层对 Cookie/Authorization 要求 HTTPS 与来源域名白名单，跨域重定向不携带敏感头。",
            "模型上下文不含 Cookie、API Key、Webhook、完整数据库、投递账本或机会私密字段。",
            "源码 ZIP、Git 与比赛材料排除 `.env`、数据库、浏览器配置、密钥、缓存和生成报告。",
        ],
    )

    add_heading(document, "3. 团队协作与审计视角", level=2)
    add_table(
        document,
        ["角色", "真实贡献", "交付责任"],
        [
            [
                "产品与技术负责人",
                "命题拆解、架构、全栈、AI/检索、测试、部署、文档与演示",
                "可运行系统、代码质量、跨平台与比赛交付",
            ],
            [
                "审计与合规负责人",
                "参与需求评审、来源权限边界复核、证据链抽查、风险披露与验收清单",
                "确保登录/付费边界、量化口径和材料陈述可核验",
            ],
        ],
        widths_cm=[3.5, 7.0, 5.7],
    )
    add_paragraph(
        document,
        "团队没有把审计角色包装成核心研发，也没有虚构客户成果。审计同事的价值在于把“功能能跑”提升为“权限不越界、证据可回溯、指标可复核、验收不遗漏”。",
    )

    add_heading(document, "4. 路线图与推广", level=2)
    add_table(
        document,
        ["阶段", "目标", "治理门槛"],
        [
            [
                "P0｜比赛与试点",
                "本地/systemd 单租户；验证来源、效率、质量与业务闭环",
                "来源白名单、人工复核、真实基线",
            ],
            [
                "P1｜部门灰度",
                "企业正式渠道、统一身份、角色权限、日志与备份",
                "HTTPS、SSO/RBAC、审计留存、容灾",
            ],
            [
                "P2｜规模化",
                "官方 API 合作、分布式队列、PostgreSQL、多租户隔离",
                "来源协议、容量压测、数据分级",
            ],
            [
                "P3｜跨行业复制",
                "采购、政策、新闻、合规与竞争情报的通用信息 Agent",
                "行业词典、评价基线与权限模型",
            ],
        ],
        widths_cm=[3.2, 7.0, 6.0],
    )

    add_heading(document, "四、附录", page_break=True)
    add_heading(document, "1. 命题交付物逐项对照", level=2)
    add_table(
        document,
        ["官方交付物", "终版位置", "状态"],
        [
            ["多个运行结果 Word", "01_运行结果Word/", "4 个不同问题，含登录来源与零结果诊断"],
            ["详设文档", "02_设计文档/详设文档.docx + .md", "覆盖核心模块、创新与部署"],
            ["完整代码", "03_代码与操作文档/源码 ZIP", "不含私密与生成数据；结果不硬编码"],
            ["操作文档", "03_代码与操作文档/", "Windows/Linux/macOS、API、配置、用户、比赛验收"],
            ["Demo 视频", "04_Demo/", "脚本与分镜已完成；公开视频链接待实际录制后回填"],
        ],
        widths_cm=[4.0, 7.6, 4.6],
    )
    add_heading(document, "2. 幕后故事", level=2)
    add_paragraph(
        document,
        "项目从“多站搜索脚本”演进为证据闭环。最费时间的不是把页面抓下来，而是承认每个来源的真实边界：零结果不能等于失败，Cookie 存在不能等于登录成功，付费内容不能包装成免费能力，外部消息也不能轻率宣传 exactly-once。每次发现假绿灯，团队都把它变成状态机、审计字段和回归测试。",
    )
    add_heading(document, "3. 参考资料", level=2)
    refs = [
        ("超聚变命题页", "https://activity.feishu.cn/future-talent?detail=chaojubian"),
        ("BidPilot GitHub", "https://github.com/gitchw/bidpilot"),
        ("FastAPI", "https://fastapi.tiangolo.com/"),
        ("Playwright for Python", "https://playwright.dev/python/"),
        ("Python", "https://docs.python.org/3/"),
        ("SQLite", "https://www.sqlite.org/docs.html"),
    ]
    for label, url in refs:
        paragraph = document.add_paragraph(style="List Bullet")
        label_run = paragraph.add_run(f"{label}：")
        set_font(label_run, 10.3)
        add_hyperlink(paragraph, url, url)

    # The image-heavy proposal contains explicit chapter page breaks. Keeping
    # every heading chained to the next large table/image can make desktop Word
    # repaginate indefinitely, so only this document relaxes that constraint.
    for paragraph in document.paragraphs:
        if paragraph.style and paragraph.style.name.startswith("Heading "):
            paragraph.paragraph_format.keep_with_next = False

    document.save(output)


def strip_inline_markdown(text: str) -> str:
    text = re.sub(r"\[([^]]+)]\(([^)]+)\)", r"\1（\2）", text)
    text = text.replace("`", "").replace("**", "").replace("__", "")
    return text.strip()


def parse_table(lines: list[str]) -> tuple[list[str], list[list[str]]]:
    rows = [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in lines]
    if len(rows) >= 2 and all(
        re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in rows[1]
    ):
        rows.pop(1)
    return rows[0], rows[1:]


def build_markdown_docx(source: Path, output: Path, *, subtitle: str) -> None:
    lines = source.read_text(encoding="utf-8").splitlines()
    document = Document()
    configure_document(document, header_text=f"聚标成擎｜{subtitle}")
    document.core_properties.author = "聚标成擎"
    title = next((line[2:].strip() for line in lines if line.startswith("# ")), source.stem)
    add_title(document, title, subtitle)
    index = 0
    in_code = False
    code_lines: list[str] = []
    first_title_consumed = False
    while index < len(lines):
        line = lines[index]
        if line.startswith("```"):
            if in_code:
                table = document.add_table(rows=1, cols=1)
                set_table_borders(table, color=LINE)
                cell = table.cell(0, 0)
                shade_cell(cell, "F4F6F8")
                set_cell_text(cell, "\n".join(code_lines), size=8.2, color=INK)
                code_lines = []
                in_code = False
            else:
                in_code = True
            index += 1
            continue
        if in_code:
            code_lines.append(line)
            index += 1
            continue
        if line.startswith("# "):
            if not first_title_consumed:
                first_title_consumed = True
            else:
                add_heading(document, strip_inline_markdown(line[2:]), page_break=True)
        elif line.startswith("## "):
            add_heading(document, strip_inline_markdown(line[3:]), level=1)
        elif line.startswith("### "):
            add_heading(document, strip_inline_markdown(line[4:]), level=2)
        elif line.startswith("#### "):
            add_heading(document, strip_inline_markdown(line[5:]), level=3)
        elif line.startswith("|"):
            table_lines = []
            while index < len(lines) and lines[index].startswith("|"):
                table_lines.append(lines[index])
                index += 1
            headers, rows = parse_table(table_lines)
            add_table(
                document,
                [strip_inline_markdown(item) for item in headers],
                [[strip_inline_markdown(item) for item in row] for row in rows],
            )
            continue
        elif re.match(r"^\s*[-*] ", line):
            add_bullets(document, [strip_inline_markdown(re.sub(r"^\s*[-*] ", "", line))])
        elif re.match(r"^\s*\d+[.)] ", line):
            paragraph = document.add_paragraph(style="List Number")
            item = re.sub(r"^\s*\d+[.)] ", "", line)
            run = paragraph.add_run(strip_inline_markdown(item))
            set_font(run, 10.3)
        elif line.startswith("> "):
            add_callout(document, "说明", strip_inline_markdown(line[2:]))
        elif line.strip() and not line.strip() == "---":
            add_paragraph(document, strip_inline_markdown(line))
        index += 1
    # Long tables can make desktop Word spend unbounded time repaginating when
    # every preceding heading is chained to the next block.  The generated
    # documents use explicit hierarchy and page breaks, so relax that chain.
    for paragraph in document.paragraphs:
        if paragraph.style and paragraph.style.name.startswith("Heading "):
            paragraph.paragraph_format.keep_with_next = False
    output.parent.mkdir(parents=True, exist_ok=True)
    document.save(output)


def copy_reports(source_dir: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    pattern = re.compile(r"_(\d{12})_[0-9a-f]{8}(?:-[^.]*)?\.docx$", re.IGNORECASE)
    for source in sorted(source_dir.glob("*.docx")):
        target_name = pattern.sub(r"_\1.docx", source.name)
        shutil.copy2(source, target_dir / target_name)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--previous-delivery", type=Path, required=True)
    args = parser.parse_args()

    repo = args.repo.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    competition = (
        output
        / "06_参赛方案"
        / "【40强赛】超聚变🤝聚标成擎｜标擎BidPilot——证据驱动的招投标情报Agent.docx"
    )
    build_competition_docx(args.template.resolve(), competition, repo)

    design_dir = output / "02_设计文档"
    build_markdown_docx(
        repo / "docs" / "详设文档.md", design_dir / "详设文档.docx", subtitle="详细设计说明书"
    )
    build_markdown_docx(
        repo / "docs" / "LINUX_DEPLOYMENT.md",
        design_dir / "Linux部署与运维手册.docx",
        subtitle="Linux 生产部署、登录来源与运维",
    )
    audit_dir = output / "05_验收与校验"
    build_markdown_docx(
        repo / "docs" / "CROSS_PLATFORM_TEST_AUDIT.md",
        audit_dir / "跨平台测试与审计报告.docx",
        subtitle="Windows / Linux / macOS / Container",
    )
    checklist_dir = output / "00_提交说明与清单"
    build_markdown_docx(
        repo / "docs" / "DELIVERY_CHECKLIST.md",
        checklist_dir / "提交说明与验收清单.docx",
        subtitle="超聚变命题终版交付",
    )

    copy_reports(args.previous_delivery / "01_运行结果Word", output / "01_运行结果Word")

    for source in (repo / "docs" / "详设文档.md", repo / "SPEC.md"):
        shutil.copy2(source, design_dir / source.name)
    for source in (
        repo / "docs" / "LINUX_DEPLOYMENT.md",
        repo / "docs" / "CROSS_PLATFORM_TEST_AUDIT.md",
    ):
        target = design_dir if "LINUX" in source.name else audit_dir
        shutil.copy2(source, target / source.name)

    demo_dir = output / "04_Demo"
    demo_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(repo / "docs" / "COMPETITION_DEMO_SCRIPT.md", demo_dir / "Demo录制脚本与分镜.md")
    previous_video = args.previous_delivery / "04_Demo" / "视频公开链接.txt"
    if previous_video.exists():
        shutil.copy2(previous_video, demo_dir / previous_video.name)

    print(competition)


if __name__ == "__main__":
    main()
