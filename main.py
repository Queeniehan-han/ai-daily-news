# -*- coding: utf-8 -*-
"""
main.py — AI每日大事件 Max · Streamlit 界面

PRD 对应关系：
  - 「① 网页爬取」→ 全量抓取 84 个信息源（昨日 11am 至当日 11am · CST）。
  - 第一轮抓取完毕 → 自动开启评价（覆盖校验：所有信源是否都被摘取，不存在遗漏）。
  - 「② AI 结构化分析」→ 事件(20-30字) / 内容(100-150字) / 潜在影响(50-100字)
    + 分类（新产品发布/产品功能更新/新大模型发布/其他重大动态）+ 原文链接。
  - 侧边栏：板块筛选 / 公司筛选 / 新闻分类筛选 / Provider + 模型 + API Key。

启动：
  cd /Users/bytedance/workspace/ai_daily_news_max
  /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 -m streamlit run main.py
"""

from __future__ import annotations

import datetime as _dt
from typing import Dict, List

import pandas as pd
import streamlit as st

import config
from scraper import Scraper, get_strict_window
from llm_client import LLMClient, LLMError, LLMQuotaError, detect_bytedance_key
from news_processor import NewsProcessor, build_coverage_summary, StructuredNews

st.set_page_config(page_title="AI每日大事件 Max", page_icon="🗞", layout="wide")


# ──────────────────────────────────────────────────────────────────────────
# Session State 初始化
# ──────────────────────────────────────────────────────────────────────────
def _init_state():
    ss = st.session_state
    ss.setdefault("raw_items", None)        # List[NewsItem]
    ss.setdefault("reports", None)          # List[SourceReport]
    ss.setdefault("expected_sources", None) # 本轮实际启用/筛选后的信源
    ss.setdefault("coverage", None)         # dict
    ss.setdefault("structured", None)       # List[StructuredNews]
    ss.setdefault("logs", [])


_init_state()


def _log(msg: str):
    st.session_state.logs.append(msg)


# ──────────────────────────────────────────────────────────────────────────
# 侧边栏：Provider / API Key / 板块 / 公司 / 分类筛选
# ──────────────────────────────────────────────────────────────────────────
def render_sidebar() -> Dict:
    st.sidebar.header("⚙️ 模型配置")
    providers = list(config.LLM_PROVIDERS.keys())
    provider = st.sidebar.selectbox(
        "选择大模型 Provider", providers,
        index=providers.index(config.DEFAULT_PROVIDER))
    pconf = config.LLM_PROVIDERS[provider]
    model = st.sidebar.selectbox(
        "模型", pconf["models"],
        index=pconf["models"].index(pconf["default_model"]))
    api_key = st.sidebar.text_input(
        f"{provider} API Key", type="password",
        help=pconf.get("key_help", ""), key=f"apikey_{provider}")
    if pconf.get("key_help"):
        st.sidebar.caption(f"🔑 {pconf['key_help']}")

    if provider == "Bytedance ModelHub" and api_key and not detect_bytedance_key(api_key):
        st.sidebar.warning("该 Key 看起来不像 Bytedance ModelHub Key"
                           "（应含 _GPT_AK 或以 dSx 开头）。")

    st.sidebar.divider()
    st.sidebar.header("🔎 筛选板块")
    sel_cats = st.sidebar.multiselect(
        "感兴趣的信源板块（留空=全部）", config.get_source_categories(), default=[])

    st.sidebar.header("🏢 筛选公司")
    sel_companies = st.sidebar.multiselect(
        "关心的公司（留空=全部）", config.get_company_names(), default=[])

    st.sidebar.header("🗂 筛选新闻分类")
    sel_types = st.sidebar.multiselect(
        "新闻分类（留空=全部）", config.NEWS_TYPES, default=[])

    return {"provider": provider, "model": model, "api_key": api_key,
            "cats": sel_cats, "companies": sel_companies, "types": sel_types}


# ──────────────────────────────────────────────────────────────────────────
# 抓取 + 分析流程
# ──────────────────────────────────────────────────────────────────────────
def do_crawl(cfg: Dict):
    sources = config.get_all_sources()
    if cfg["cats"]:
        sources = [s for s in sources if s.get("category") in cfg["cats"]]
    if not sources:
        st.error("当前板块筛选下没有任何信息源，请调整筛选条件。")
        return

    st.session_state.logs = []
    scraper = Scraper(log_fn=_log)
    progress = st.progress(0.0, text="开始抓取…")
    status_box = st.empty()

    def on_progress(done: int, total: int, name: str):
        progress.progress(min(done / total, 1.0), text=f"抓取中 {done}/{total} · {name}")

    with st.spinner("正在全量抓取信息源（昨日 11am 至当日 11am 窗口）…"):
        raw_items, reports = scraper.run_all(sources, progress_fn=on_progress)
    progress.progress(1.0, text="抓取完成")

    st.session_state.raw_items = raw_items
    st.session_state.reports = reports
    st.session_state.expected_sources = sources
    # PRD：第一轮抓取完毕后自动开启评价（验证是否所有信源都被摘取，不存在遗漏）
    st.session_state.coverage = build_coverage_summary(reports, sources)
    st.session_state.structured = None
    status_box.success(
        f"抓取完成：{len(raw_items)} 条原始条目，覆盖 {len(reports)} / "
        f"{st.session_state.coverage['expected_total']} 个信息源。")


def do_analyze(cfg: Dict):
    if not st.session_state.raw_items:
        st.warning("请先点击「① 网页爬取」抓取数据。")
        return
    if not cfg["api_key"]:
        st.error(f"请在左侧输入 {cfg['provider']} 的 API Key。")
        return
    try:
        client = LLMClient(cfg["provider"], cfg["api_key"], cfg["model"])
    except LLMError as e:
        st.error(f"初始化 LLM 失败：{e}")
        return

    processor = NewsProcessor(client, log_fn=_log)
    with st.spinner("正在调用大模型结构化分析（事件/内容/潜在影响）…"):
        try:
            structured = processor.process(st.session_state.raw_items)
        except LLMQuotaError as e:
            st.error(f"❌ 配额/限频：{e}")
            return
        except LLMError as e:
            st.error(f"❌ 分析失败：{e}")
            return
    st.session_state.structured = structured
    st.success(f"分析完成：产出 {len(structured)} 条结构化新闻。")


# ──────────────────────────────────────────────────────────────────────────
# 渲染：覆盖评价（PRD 自动评价环节）
# ──────────────────────────────────────────────────────────────────────────
def _format_completeness(value: str) -> str:
    mapping = {
        "best_effort": "免费链路尽力合并",
        "suspected_partial": "疑似不全/缓存旧",
        "unknown_empty": "无法判断空窗",
        "failed": "抓取失败",
    }
    return mapping.get(value or "", "")


def _format_issue_type(value: str) -> str:
    mapping = {
        "ok": "正常有数据",
        "window_empty": "窗口内为空",
        "parser": "解析器需适配",
        "blocked": "反爬/权限拦截",
        "invalid_url": "URL/RSS 失效",
        "network": "网络/超时",
        "rate_limited": "限频",
        "undated": "无时间戳",
        "suspected_partial": "疑似不全",
        "unknown_empty": "无法判断空窗",
        "failed": "抓取失败",
        "empty_unknown": "空结果待排查",
    }
    return mapping.get(value or "", value or "")


def render_coverage():
    if st.session_state.reports:
        # 重新按当前代码归因，避免老 session_state 里的覆盖摘要继续显示旧口径。
        st.session_state.coverage = build_coverage_summary(
            st.session_state.reports,
            st.session_state.expected_sources,
        )
    cov = st.session_state.coverage
    if not cov:
        return
    st.subheader("📋 第一轮抓取评价（信源覆盖校验）")
    issue_counts = cov.get("issue_counts", {})
    need_fix = sum(issue_counts.get(k, 0) for k in (
        "parser", "invalid_url", "failed", "undated"))
    external_limited = sum(issue_counts.get(k, 0) for k in (
        "blocked", "network", "rate_limited"))
    suspected = issue_counts.get("suspected_partial", 0) + issue_counts.get("unknown_empty", 0)
    empty_or_uncertain = issue_counts.get("window_empty", 0) + issue_counts.get("empty_unknown", 0)
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("应抓信源", cov["expected_total"])
    c2.metric("已覆盖", cov["report_rows"])
    c3.metric("成功有数据", cov["success_count"])
    c4.metric("空窗/待确认", empty_or_uncertain)
    c5.metric("代码需修复", need_fix)
    c6.metric("外部/疑似", external_limited + suspected)
    st.caption(f"原始条目：{cov['total_items']} 条")

    if cov["all_covered"]:
        st.success("✅ 所有 PRD 信息源均已尝试抓取，不存在遗漏。")
    else:
        st.warning(f"⚠️ 存在未被尝试的信源：{cov['missing_sources']}")

    risky_kols = [
        r for r in st.session_state.reports
        if r.type == "x_kol" and r.completeness in {"suspected_partial", "unknown_empty", "failed"}
    ]
    if risky_kols:
        st.info("X.com KOL 当前使用免费/匿名多链路尽力合并；无官方付费 API 时无法证明全量。"
                "下方「完整性」会标出疑似不全、缓存陈旧或无法判断空窗的账号。")
    if external_limited:
        st.info("网络超时、反爬/权限拦截、限频属于外部访问限制；已从「代码需修复」中拆出，"
                "需要通过付费抓取 API、登录态/代理池、或官方 RSS/API 来提升稳定性。")

    with st.expander("各信源抓取明细", expanded=False):
        rows = [{
            "信源": r.name, "板块": r.category, "类型": r.type,
            "策略": r.strategy, "条数": r.count, "状态": r.status,
            "原因": _format_issue_type(getattr(r, "issue_type", "")),
            "完整性": _format_completeness(getattr(r, "completeness", "")),
            "最新可见": (getattr(r, "latest_seen_at", "") or "")[:16].replace("T", " "),
            "最旧可见": (getattr(r, "oldest_seen_at", "") or "")[:16].replace("T", " "),
            "说明": r.error,
        } for r in st.session_state.reports]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# ──────────────────────────────────────────────────────────────────────────
# 渲染：结构化结果（双 Tab：新闻媒体&公司 / X KOL）
# ──────────────────────────────────────────────────────────────────────────
def _apply_filters(items: List[StructuredNews], cfg: Dict) -> List[StructuredNews]:
    out = items
    if cfg["cats"]:
        out = [it for it in out if it.category in cfg["cats"]]
    if cfg["companies"]:
        out = [it for it in out if it.company in cfg["companies"]]
    if cfg["types"]:
        out = [it for it in out if it.news_type in cfg["types"]]
    return out


def _render_news_group(items: List[StructuredNews]):
    if not items:
        st.info("该分类下暂无符合筛选条件的新闻。")
        return
    for nt in config.NEWS_TYPES:
        group = [it for it in items if it.news_type == nt]
        if not group:
            continue
        st.markdown(f"#### 🏷 {nt}（{len(group)} 条）")
        for it in group:
            comp = f" · 🏢 {it.company}" if it.company else ""
            when = ""
            if it.published_at:
                when = f" · 🕒 {it.published_at[:16].replace('T', ' ')}"
            with st.container(border=True):
                st.markdown(f"**📌 事件：{it.event}**")
                st.markdown(f"**内容：** {it.detail}")
                st.markdown(f"**潜在影响：** {it.impact}")
                st.caption(f"来源：{it.source}{comp}{when} · [原文链接]({it.url})")


def render_results(cfg: Dict):
    structured = st.session_state.structured
    if structured is None:
        return
    filtered = _apply_filters(structured, cfg)
    kol_items = [it for it in filtered if str(it.category).startswith("X KOL")]
    other_items = [it for it in filtered if not str(it.category).startswith("X KOL")]

    st.subheader(f"🗞 结构化新闻（共 {len(filtered)} 条，已应用筛选）")
    tab_other, tab_kol = st.tabs([
        f"📰 新闻媒体 & 公司（{len(other_items)}）",
        f"👤 X.com KOL（{len(kol_items)}）",
    ])
    with tab_other:
        _render_news_group(other_items)
    with tab_kol:
        if not kol_items:
            st.info("X.com KOL 暂无窗口内结构化数据。当前使用免费/匿名多链路尽力合并，"
                    "常见原因是上游缓存陈旧、免费链路被限流、或窗口内 KOL 未发推。"
                    "详见上方「各信源抓取明细」。")
        else:
            _render_news_group(kol_items)

    # 导出
    if filtered:
        df = pd.DataFrame([{
            "分类": it.news_type, "事件": it.event, "内容": it.detail,
            "潜在影响": it.impact, "公司": it.company, "来源": it.source,
            "时间": it.published_at, "原文链接": it.url,
        } for it in filtered])
        st.download_button(
            "⬇️ 导出 CSV", df.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"ai_news_{_dt.date.today()}.csv", mime="text/csv")


# ──────────────────────────────────────────────────────────────────────────
# 主界面
# ──────────────────────────────────────────────────────────────────────────
def main():
    cfg = render_sidebar()

    st.title("🗞 AI每日大事件 Max")
    start, end = get_strict_window()
    st.caption(f"📅 抓取时间窗口（昨日 11am 至当日 11am · CST）："
               f"{start.strftime('%Y-%m-%d %H:%M')} → {end.strftime('%Y-%m-%d %H:%M')}　|　"
               f"信息源总数：{len(config.get_all_sources())}（"
               f"{len(config.WEB_SOURCES)} 网页/公司 + {len(config.X_KOL_SOURCES)} X KOL）")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("① 网页爬取（全量抓取所有信息源）", type="primary",
                     use_container_width=True):
            do_crawl(cfg)
    with col2:
        if st.button("② AI 结构化分析（事件/内容/潜在影响）", use_container_width=True):
            do_analyze(cfg)

    st.divider()

    if st.session_state.coverage:
        render_coverage()
        st.divider()

    if st.session_state.structured is not None:
        render_results(cfg)
    elif st.session_state.raw_items:
        st.info("✅ 抓取完成。请在左侧确认 Provider 与 API Key，"
                "然后点击「② AI 结构化分析」。")

    if st.session_state.logs:
        with st.expander("📜 运行日志", expanded=False):
            st.code("\n".join(st.session_state.logs[-200:]), language="text")


if __name__ == "__main__":
    main()
