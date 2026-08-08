# -*- coding: utf-8 -*-
"""生成 PDF 报告 — AI 每日大事件结构化分析"""
from __future__ import annotations

import io
from collections import defaultdict
from datetime import datetime, timezone

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    HRFlowable,
)

# 内置 CJK 字体，无需外部字体文件
pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

FONT = "STSong-Light"
PAGE_W, PAGE_H = A4
MARGIN = 18 * mm

# ── 颜色 ──
C_PRIMARY = colors.HexColor("#5367e8")
C_PRIMARY_SOFT = colors.HexColor("#eef0ff")
C_TEXT = colors.HexColor("#172033")
C_MUTED = colors.HexColor("#697386")
C_LINE = colors.HexColor("#e1e6ef")
C_ACCENT = colors.HexColor("#7258c7")
C_PDF_BADGE = colors.HexColor("#e0454b")

# ── 段落样式 ──
_style_title = ParagraphStyle(
    "Title",
    fontName=FONT,
    fontSize=22,
    leading=28,
    alignment=TA_CENTER,
    textColor=C_TEXT,
    spaceAfter=4 * mm,
)
_style_subtitle = ParagraphStyle(
    "Subtitle",
    fontName=FONT,
    fontSize=10,
    leading=14,
    alignment=TA_CENTER,
    textColor=C_MUTED,
    spaceAfter=8 * mm,
)
_style_h1 = ParagraphStyle(
    "H1",
    fontName=FONT,
    fontSize=14,
    leading=20,
    textColor=C_PRIMARY,
    spaceBefore=6 * mm,
    spaceAfter=3 * mm,
)
_style_meta_label = ParagraphStyle(
    "MetaLabel",
    fontName=FONT,
    fontSize=9,
    leading=13,
    textColor=C_MUTED,
)
_style_event = ParagraphStyle(
    "Event",
    fontName=FONT,
    fontSize=11.5,
    leading=16,
    textColor=C_TEXT,
    spaceBefore=3 * mm,
    spaceAfter=1.5 * mm,
)
_style_body = ParagraphStyle(
    "Body",
    fontName=FONT,
    fontSize=9.5,
    leading=14,
    textColor=C_TEXT,
    leftIndent=8 * mm,
    spaceAfter=1 * mm,
)
_style_source = ParagraphStyle(
    "Source",
    fontName=FONT,
    fontSize=8.5,
    leading=12,
    textColor=C_MUTED,
    leftIndent=8 * mm,
    spaceAfter=3 * mm,
)
_style_footer = ParagraphStyle(
    "Footer",
    fontName=FONT,
    fontSize=8,
    leading=11,
    alignment=TA_CENTER,
    textColor=C_MUTED,
)

_NEWS_TYPE_ORDER = [
    "新产品发布",
    "产品功能更新",
    "新大模型发布",
    "Agent智能体",
    "具身机器人",
    "项目融资",
    "其他重大动态",
]


def _esc(text: str) -> str:
    """转义 XML 特殊字符"""
    if not text:
        return ""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _fmt_dt(value) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


def _on_page(canvas, doc):
    """页脚 — 页码与品牌"""
    canvas.saveState()
    canvas.setFont(FONT, 8)
    canvas.setFillColor(C_MUTED)
    page_num = canvas.getPageNumber()
    footer = f"AI Daily Intelligence Desk  ·  第 {page_num} 页"
    canvas.drawCentredString(PAGE_W / 2, 10 * mm, footer)
    canvas.restoreState()


def generate_pdf_report(
    run,
    items: list,
    *,
    window_start: str = "",
    window_end: str = "",
) -> bytes:
    """生成 PDF 报告，返回二进制内容。

    Args:
        run: Run ORM 对象（可为 None）
        items: StructuredNewsRecord 列表（已经过 _prepare_structured_items 处理）
        window_start: 抓取窗口起始时间文本
        window_end: 抓取窗口结束时间文本
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=22 * mm,
        title="AI每日大事件 — 分析报告",
        author="AI Daily Intelligence Desk",
    )

    story: list = []

    # ── 标题区 ──
    story.append(Paragraph("AI 每日大事件", _style_title))
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M") + " UTC"
    story.append(Paragraph(f"结构化分析报告  ·  生成于 {now_str}", _style_subtitle))

    # ── 摘要统计 ──
    total = len(items)
    run_status = "—"
    run_id = "—"
    run_time = "—"
    if run:
        run_id = run.id
        run_status = run.status or "—"
        run_time = _fmt_dt(run.created_at)

    summary_data = [
        ["任务编号", run_id],
        ["任务状态", run_status],
        ["生成时间", run_time],
        ["结构化新闻", f"{total} 条"],
    ]
    if window_start or window_end:
        summary_data.append(["抓取窗口", f"{window_start} → {window_end} CST"])

    summary_table = Table(
        summary_data,
        colWidths=[28 * mm, PAGE_W - 2 * MARGIN - 28 * mm],
    )
    summary_table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), FONT, 9),
                ("TEXTCOLOR", (0, 0), (0, -1), C_MUTED),
                ("TEXTCOLOR", (1, 0), (1, -1), C_TEXT),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("BACKGROUND", (0, 0), (-1, -1), C_PRIMARY_SOFT),
                ("BOX", (0, 0), (-1, -1), 0.5, C_LINE),
                ("LINEBELOW", (0, 0), (-1, -2), 0.3, C_LINE),
            ]
        )
    )
    story.append(summary_table)
    story.append(Spacer(1, 6 * mm))

    if total == 0:
        story.append(
            Paragraph(
                "暂无已结构化的新闻内容。请先完成抓取与分析任务后再生成报告。",
                ParagraphStyle(
                    "Empty",
                    fontName=FONT,
                    fontSize=11,
                    leading=16,
                    alignment=TA_CENTER,
                    textColor=C_MUTED,
                ),
            )
        )
        doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
        buffer.seek(0)
        return buffer.getvalue()

    # ── 按主题分组 ──
    by_topic: dict[str, list] = defaultdict(list)
    for item in items:
        topic = getattr(item, "filter_topic", "") or item.news_type or "未分类"
        by_topic[topic].append(item)

    ordered_topics = [t for t in _NEWS_TYPE_ORDER if t in by_topic]
    ordered_topics.extend(
        t for t in sorted(by_topic.keys()) if t not in ordered_topics
    )

    # ── 各主题新闻 ──
    for topic in ordered_topics:
        group = by_topic[topic]
        story.append(
            Paragraph(
                f"{topic}（{len(group)} 条）",
                _style_h1,
            )
        )
        story.append(HRFlowable(width="100%", thickness=1, color=C_LINE))
        story.append(Spacer(1, 2 * mm))

        for idx, item in enumerate(group, 1):
            # 事件标题
            event_text = f"{idx}. {_esc(item.event)}"
            story.append(Paragraph(event_text, _style_event))

            # 新闻摘要
            if item.detail:
                story.append(
                    Paragraph(
                        f"<b>摘要：</b>{_esc(item.detail)}",
                        _style_body,
                    )
                )

            # 潜在影响
            if item.impact:
                story.append(
                    Paragraph(
                        f"<b>影响：</b>{_esc(item.impact)}",
                        _style_body,
                    )
                )

            # 来源与链接
            source_parts = []
            if item.source:
                source_parts.append(f"来源：{_esc(item.source)}")
            if item.company:
                source_parts.append(f"公司：{_esc(item.company)}")
            if item.published_at:
                source_parts.append(f"时间：{_esc(item.published_at)}")
            if item.url:
                source_parts.append(f'链接：<link href="{_esc(item.url)}">{_esc(item.url)}</link>')
            if source_parts:
                story.append(
                    Paragraph("  |  ".join(source_parts), _style_source)
                )

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    buffer.seek(0)
    return buffer.getvalue()
