# -*- coding: utf-8 -*-
"""test_logic.py — 单元测试（第二重检查：逻辑验证）

覆盖关键逻辑：时间窗口 / 日期提取（含粘字母陷阱）/ 去重 / JSON 提取 /
配额识别 / Key 识别 / 公司关联 / 覆盖校验 / 配置完整性（PRD 合规）。

运行：
  /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 test_logic.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

import config
from scraper import (parse_date_from_text, parse_iso_or_struct, parse_relative_age, within_window,
                     get_strict_window, NewsItem, verify_coverage, SourceReport,
                     Scraper, KolFetchResult, classify_report_issue)
from news_processor import (
    dedupe,
    _extract_json,
    _guess_company,
    classify_special_news_type,
)
from llm_client import _classify_error, LLMQuotaError, LLMError, detect_bytedance_key

CST = config.CST
_passed, _failed = 0, 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✅ {name}")
    else:
        _failed += 1
        print(f"  ❌ {name}")


print("=== 1. 时间窗口（PRD：昨日11am至当日11am）===")
start, end = get_strict_window()
check("窗口跨度=1天", (end - start) == timedelta(days=1))
check("窗口起点为11am", start.hour == 11 and start.minute == 0)
check("窗口内时间通过", within_window(start + timedelta(hours=5)))
check("窗口外(更早)被拒", not within_window(start - timedelta(hours=5)))
check("无时间戳被拒(返回False, PRD硬约束)", within_window(None) is False)
check("naive datetime 按 CST 处理", within_window((start + timedelta(hours=3)).replace(tzinfo=None)))
check("超出窗口+grace内仍接受", within_window(end + timedelta(hours=1)))
check("超出窗口+超grace被拒", not within_window(end + timedelta(hours=5)))

print("=== 2. 日期提取（含生产 case：粘字母陷阱）===")
check("'Jun 3, 2026Grok' 解析成功",
      parse_date_from_text("Jun 3, 2026Grok Becomes the Voice")
      == datetime(2026, 6, 3, 12, 0, tzinfo=CST))
check("'2026-06-09 Apple' 解析成功",
      parse_date_from_text("2026-06-09 Apple released")
      == datetime(2026, 6, 9, 12, 0, tzinfo=CST))
check("'06.08.26Today' mdy 解析成功",
      parse_date_from_text("Introducing FrontierCode06.08.26Today's")
      == datetime(2026, 6, 8, 12, 0, tzinfo=CST))
check("中文 '2026年6月9日' 解析成功",
      parse_date_from_text("2026年6月9日 重大突破")
      == datetime(2026, 6, 9, 12, 0, tzinfo=CST))
check("无日期文本返回None", parse_date_from_text("just some random text") is None)
check("空文本返回None", parse_date_from_text("") is None)
check("非法月份(13)被护栏拒绝", parse_date_from_text("13/45/2026 text") is None)
check("年份越界(1850)被拒", parse_date_from_text("1850-06-09") is None)
check("Twitter created_at 解析",
      parse_iso_or_struct("Tue Jan 24 20:14:18 +0000 2023") is not None)
check("ISO 字符串解析", parse_iso_or_struct("2026-06-09T08:00:00+08:00") is not None)
check("None 输入返回 None", parse_iso_or_struct(None) is None)
relative_hour = parse_relative_age("date: 6 hours ago")
check("AnySearch 相对时间解析", relative_hour is not None)

print("=== 3. 去重（PRD：过滤重复新闻；两段式：规则+近重复聚类）===")
items = [
    NewsItem(title="OpenAI 发布 GPT", url="https://a.com/x?ref=1", source="s1"),
    NewsItem(title="OpenAI 发布 GPT", url="https://a.com/x?ref=2", source="s2"),  # 同标题
    NewsItem(title="不同标题", url="https://a.com/x", source="s3"),                # 同URL(去query)
    NewsItem(title="全新内容", url="https://b.com/y", source="s4"),
]
deduped = dedupe(items)
check("去重后剩 2 条", len(deduped) == 2)
check("保留首条", deduped[0].source == "s1")
check("空列表不报错", dedupe([]) == [])

# 近重复聚类：标题高度相似（转载稿）应合并，且保留高质量官方源
near_items = [
    NewsItem(title="谷歌发布 Gemini 3 Pro 大模型，性能大幅提升！",
             url="https://weixin.sogou.com/link?url=abc", source="转载号"),
    NewsItem(title="谷歌发布 Gemini 3 Pro 大模型，性能大幅提升",
             url="https://blog.google/technology/ai/gemini-3-pro/", source="Google AI"),
    NewsItem(title="完全无关的另一条新闻标题这里写点别的",
             url="https://c.com/z", source="s5"),
]
near_deduped = dedupe(near_items)
check("近重复聚类后剩 2 条", len(near_deduped) == 2)
check("代表稿保留官方源(blog.google)",
      any("blog.google" in it.url for it in near_deduped))
check("聚合转载稿被丢弃",
      not any("sogou" in it.url for it in near_deduped))
check("不相似条目不被误合并",
      any(it.url == "https://c.com/z" for it in near_deduped))

print("=== 4. JSON 提取（容忍 markdown 包裹）===")
check("纯JSON", _extract_json('{"results":[{"index":0}]}')["results"][0]["index"] == 0)
check("```json 包裹", _extract_json('```json\n{"results":[]}\n```')["results"] == [])
check("前后缀文字",
      _extract_json('好的，结果是：{"results":[{"index":1}]} 完毕')["results"][0]["index"] == 1)
try:
    _extract_json("no json here")
    check("无JSON应抛错", False)
except ValueError:
    check("无JSON应抛错", True)

print("=== 5. 配额错误识别 ===")
check("429被识别为配额错误",
      isinstance(_classify_error(Exception("HTTP 429 rate limit")), LLMQuotaError))
check("中文'资源不足'被识别",
      isinstance(_classify_error(Exception("gemini普通账户资源不足 -4302")), LLMQuotaError))
check("普通错误不误判为配额",
      not isinstance(_classify_error(Exception("connection refused")), LLMQuotaError)
      and isinstance(_classify_error(Exception("connection refused")), LLMError))

print("=== 6. Bytedance Key 识别 ===")
check("含_GPT_AK", detect_bytedance_key("xxxx_GPT_AK"))
check("以dSx开头", detect_bytedance_key("dSxabc123"))
check("普通sk-key不误判", not detect_bytedance_key("***"))
check("空Key返回False", not detect_bytedance_key(""))

print("=== 7. 公司关联猜测 ===")
check("OpenAI 命中", _guess_company("OpenAI 发布 ChatGPT 新功能") == "OpenAI")
check("中文'字节'命中字节跳动", _guess_company("字节跳动豆包大模型升级") == "字节跳动 (Bytedance)")
check("无关键词返回空", _guess_company("某不知名小公司发布产品") == "")

print("=== 8. 覆盖校验（PRD：自动评价不存在遗漏）===")
fake_sources = config.get_all_sources()[:3]
reports = [SourceReport(name=s["name"], category=s.get("category", ""),
                        type=s.get("type", ""), count=2, status="success")
           for s in fake_sources]
cov = verify_coverage(reports, fake_sources)
check("全覆盖时 all_covered=True", cov["all_covered"] is True)
check("缺失信源被检出", verify_coverage(reports[:2], fake_sources)["all_covered"] is False)
check("missing_sources 列出缺失名",
      verify_coverage(reports[:2], fake_sources)["missing_sources"]
      == [fake_sources[2]["name"]])
filtered_cov = verify_coverage(reports[:1], fake_sources[:1])
check("覆盖评价使用本轮实际信源作分母",
      filtered_cov["expected_total"] == 1
      and filtered_cov["report_rows"] == 1
      and filtered_cov["all_covered"] is True)
blocked_rep = SourceReport(name="blocked", category="c", type="web",
                           status="empty", error="直抓: HTTP 403")
parser_rep = SourceReport(name="parser", category="c", type="web",
                          status="empty", error="页面 200 但未提取到任何文章链接")
window_rep = SourceReport(name="old", category="c", type="web",
                          status="empty", error="RSS: feed 有 20 条但均在窗口外")
mixed_empty_rep = SourceReport(
    name="mixed-empty", category="c", type="web", status="empty",
    error="自动发现 Feed: 尝试 4 个入口均无窗口内结果; "
          "sitemap: 检查 5 个 sitemap，窗口内 0 条; "
          "直抓: 提取到 3 条候选, 详情页核验后均在窗口外")
plain_empty_rep = SourceReport(
    name="plain-empty", category="c", type="web", status="empty",
    error="窗口内 0 条（昨日11am-当日11am 无新发布）")
check("403 归因为反爬/权限拦截", classify_report_issue(blocked_rep) == "blocked")
check("结构不识别归因为解析器问题", classify_report_issue(parser_rep) == "parser")
check("窗口外归因为窗口内为空", classify_report_issue(window_rep) == "window_empty")
check("多级兜底已验证空窗不归为需要修复",
      classify_report_issue(mixed_empty_rep) == "window_empty")
check("普通空结果不归为需要修复",
      classify_report_issue(plain_empty_rep) == "window_empty")
cov_issue = verify_coverage([blocked_rep, parser_rep, window_rep], [
    {"name": "blocked"}, {"name": "parser"}, {"name": "old"},
])
check("覆盖摘要包含原因计数",
      cov_issue["issue_counts"]["blocked"] == 1
      and cov_issue["issue_counts"]["parser"] == 1
      and cov_issue["issue_counts"]["window_empty"] == 1)

print("=== 8.1 X KOL 多免费链路合并与审计 ===")
scraper = Scraper()
dt1 = start + timedelta(hours=1)
dt2 = start + timedelta(hours=2)
kol_a = NewsItem(title="tweet one", url="https://x.com/a/status/100?ref=1",
                 source="KOL", published_at=dt1.isoformat(),
                 scrape_strategy="scrapecreators")
kol_a_dup = NewsItem(title="tweet one mirror", url="https://x.com/a/status/100",
                     source="KOL", published_at=dt1.isoformat(),
                     scrape_strategy="syndication")
kol_b = NewsItem(title="tweet two", url="https://x.com/a/status/101",
                 source="KOL", published_at=dt2.isoformat(),
                 scrape_strategy="syndication")
kol_results = [
    KolFetchResult(provider="scrapecreators", items=[kol_a], ok=True,
                   latest_dt=dt1, oldest_dt=dt1),
    KolFetchResult(provider="syndication", items=[kol_a_dup, kol_b], ok=True,
                   latest_dt=dt2, oldest_dt=dt1),
]
merged_kol = scraper._merge_kol_items(kol_results)
latest_kol, oldest_kol = scraper._kol_bounds(kol_results)
check("跨链路按 status id 去重", len(merged_kol) == 2)
check("KOL 合并后按发布时间倒序", merged_kol[0].url.endswith("/101"))
check("KOL 可见时间边界正确", latest_kol == dt2 and oldest_kol == dt1)
rep_kol = SourceReport(name="KOL", category="X KOL", type="x_kol",
                       completeness="best_effort", latest_seen_at=dt2.isoformat())
check("SourceReport 支持完整性字段",
      rep_kol.completeness == "best_effort" and rep_kol.latest_seen_at == dt2.isoformat())
rep_kol_timeout = SourceReport(name="KOL timeout", category="X KOL", type="x_kol",
                               status="timeout", error="X KOL 批次超过预算")
check("X KOL 超时不归为代码失败",
      classify_report_issue(rep_kol_timeout) == "suspected_partial")

scraper._kol_fetch_via_sc = lambda *a: KolFetchResult(
    provider="scrapecreators", items=[kol_a], ok=True,
    note="SC ok", latest_dt=dt1, oldest_dt=dt1)
scraper._kol_fetch_via_syndication = lambda *a: KolFetchResult(
    provider="syndication", items=[kol_a_dup, kol_b], ok=True,
    note="SYN ok", latest_dt=dt2, oldest_dt=dt1)
scraper._kol_fetch_via_rss_mirrors = lambda *a: []
multi_items, multi_strategy, multi_note, multi_comp, multi_latest, multi_oldest = (
    scraper.scrape_x_kol_multi("a", "KOL", "X KOL"))
check("ScrapeCreators 成功时默认跳过匿名交叉验证",
      len(multi_items) == 1 and multi_strategy == "scrapecreators"
      and multi_latest == dt1 and multi_oldest == dt1)

old_crosscheck = config.X_KOL_CROSSCHECK_FREE_WHEN_SC_OK
config.X_KOL_CROSSCHECK_FREE_WHEN_SC_OK = True
multi_items, multi_strategy, multi_note, multi_comp, multi_latest, multi_oldest = (
    scraper.scrape_x_kol_multi("a", "KOL", "X KOL"))
config.X_KOL_CROSSCHECK_FREE_WHEN_SC_OK = old_crosscheck
check("scrape_x_kol_multi 合并两条免费链路",
      len(multi_items) == 2 and multi_comp == "best_effort")
check("scrape_x_kol_multi 记录链路和非官方说明",
      "scrapecreators" in multi_strategy and "syndication" in multi_strategy
      and "非官方 API" in multi_note)
check("scrape_x_kol_multi 回填最新/最旧可见时间",
      multi_latest == dt2 and multi_oldest == dt1)

scraper_empty = Scraper()
scraper_empty._kol_fetch_via_sc = lambda *a: KolFetchResult(
    provider="scrapecreators", ok=True, note="SC old",
    latest_dt=start - timedelta(days=10), oldest_dt=start - timedelta(days=20))
scraper_empty._kol_fetch_via_syndication = lambda *a: KolFetchResult(
    provider="syndication", ok=False, limited=True, note="SYN 429")
scraper_empty._kol_fetch_via_rss_mirrors = lambda *a: []
empty_items, _, _, empty_comp, _, _ = scraper_empty.scrape_x_kol_multi(
    "a", "KOL", "X KOL")
check("空结果且限流时标记疑似不全",
      empty_items == [] and empty_comp == "suspected_partial")

print("=== 8.2 网页结构化数据与 sitemap 兜底 ===")
json_html = """
<html><head><script type="application/ld+json">
{"@type":"NewsArticle","headline":"OpenAI 发布全新智能体产品",
 "url":"https://example.com/news/openai-agent",
 "datePublished":"%s","description":"OpenAI 发布面向企业的全新智能体产品。"}
</script></head><body></body></html>
""" % dt2.isoformat()
json_items, json_undated = Scraper()._extract_via_structured_data(
    json_html, "https://example.com/news/", "Example", "测试")
check("JSON-LD 提取窗口内文章",
      len(json_items) == 1 and not json_undated
      and json_items[0].scrape_strategy == "jsonld")

spa_json_html = """
<html><body><script type="application/json">
{"props":{"posts":[{"type":"Article","title":"Example 发布全新多模态 AI 模型",
"url":"/news/multimodal-model","publishedAt":"%s",
"description":"Example 发布全新多模态 AI 模型并开放企业测试。"}]}}
</script></body></html>
""" % dt2.isoformat()
spa_json_items, _ = Scraper()._extract_via_structured_data(
    spa_json_html, "https://example.com/news/", "Example", "测试")
check("SPA application/json 状态提取窗口内文章",
      len(spa_json_items) == 1
      and spa_json_items[0].url == "https://example.com/news/multimodal-model")


class _Resp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status
        self.content = text.encode("utf-8")
        self.headers = {}


feed_scraper = Scraper()


def _fake_feed_get(url, **kwargs):
    if url == "https://example.com/blog":
        return _Resp(
            "<html><head><link rel='alternate' type='application/rss+xml' "
            "href='/feed.xml'></head><body></body></html>")
    if url == "https://example.com/feed.xml":
        return _Resp(
            "<?xml version='1.0'?><rss><channel>"
            "<item><title>Example 发布新的 AI 模型平台</title>"
            "<link>https://example.com/news/ai-model-platform</link>"
            f"<pubDate>{dt2.isoformat()}</pubDate>"
            "<description>Example 发布新的 AI 模型平台，用于测试 Feed 自动发现。</description>"
            "</item></channel></rss>")
    return _Resp("", status=404)


feed_scraper._http_get = _fake_feed_get
feed_items = feed_scraper.scrape_discovered_feeds(
    "Example", "https://example.com/blog", "测试")
check("自动发现 RSS/Atom Feed 并提取窗口内文章",
      len(feed_items) == 1 and feed_items[0].scrape_strategy == "feed_discovery")


sitemap_scraper = Scraper()


def _fake_get(url, **kwargs):
    if url.endswith("/robots.txt"):
        return _Resp("Sitemap: https://example.com/sitemap.xml")
    if url.endswith("/sitemap.xml"):
        return _Resp(
            "<urlset><url><loc>https://example.com/news/post-1</loc>"
            f"<lastmod>{dt2.isoformat()}</lastmod></url></urlset>")
    if url.endswith("/news/post-1"):
        return _Resp(
            "<html><head><meta property='og:title' content='Example 发布 AI 新闻'>"
            "<meta property='article:published_time' content='%s'>"
            "<meta name='description' content='Example 发布了一条用于测试的 AI 新闻。'>"
            "</head><body></body></html>" % dt2.isoformat())
    return _Resp("", status=404)


sitemap_scraper._http_get = _fake_get
sitemap_items = sitemap_scraper.scrape_sitemap("Example", "https://example.com/news/", "测试")
check("sitemap 兜底补抓窗口内文章",
      len(sitemap_items) == 1 and sitemap_items[0].scrape_strategy == "sitemap"
      and "Example 发布 AI 新闻" in sitemap_items[0].title)

undated_sitemap = Scraper()


def _fake_undated_sitemap_get(url, **kwargs):
    if url.endswith("/robots.txt"):
        return _Resp("")
    if url.endswith("/sitemap.xml"):
        return _Resp(
            "<urlset><url><loc>https://example.com/news/post-2</loc></url></urlset>")
    if url.endswith("/news/post-2"):
        return _Resp(
            "<html><head><meta property='og:title' content='Example 发布详情页日期新闻'>"
            "<meta property='article:published_time' content='%s'>"
            "</head><body><article><p>这是一段足够长的 AI 新闻正文，用于验证详情页日期核验和正文摘录。</p></article></body></html>"
            % dt2.isoformat())
    return _Resp("", status=404)


undated_sitemap._http_get = _fake_undated_sitemap_get
undated_items = undated_sitemap.scrape_sitemap(
    "Example", "https://example.com/news/", "测试")
check("sitemap 无 lastmod 时抓详情页核验发布日期",
      len(undated_items) == 1 and undated_items[0].published_at.startswith(str(dt2.date())))

lastmod_only_sitemap = Scraper()


def _fake_lastmod_only_get(url, **kwargs):
    if url.endswith("/robots.txt"):
        return _Resp("")
    if url.endswith("/sitemap.xml"):
        return _Resp(
            "<urlset><url><loc>https://example.com/news/old-page</loc>"
            f"<lastmod>{dt2.isoformat()}</lastmod></url></urlset>")
    if url.endswith("/news/old-page"):
        return _Resp(
            "<html><head><meta property='og:title' content='Old evergreen AI page'>"
            "<meta name='description' content='An old page updated by the CMS without a publication date.'>"
            "</head><body></body></html>")
    return _Resp("", status=404)


lastmod_only_sitemap._http_get = _fake_lastmod_only_get
check("sitemap lastmod 不冒充文章发布时间",
      lastmod_only_sitemap.scrape_sitemap(
          "Example", "https://example.com/news/", "测试") == [])

cross_html = (
    f"<html><body><a href='https://app.therundown.ai/p/story'>"
    f"{dt2.strftime('%Y-%m-%d')} Example AI story with enough title text</a></body></html>")
cross_items, _ = Scraper()._extract_via_anchors(
    cross_html, "https://www.rundown.ai/articles", "The Rundown AI",
    "海外快讯/Newsletter", ["app.therundown.ai"])
check("允许配置的内容子域文章链接",
      len(cross_items) == 1 and cross_items[0].url.startswith("https://app.therundown.ai/"))

join_scraper = Scraper()
check("修复完整 URL 中嵌入绝对 URL 的脏链接",
      join_scraper._join_url(
          "http://www.jiqizhixin.com/",
          "http://www.jiqizhixin.com/https://www.jiqizhixin.com/articles/2026-06-25-6")
      == "https://www.jiqizhixin.com/articles/2026-06-25-6")

audit_items = Scraper()._audit_items("Audit", [
    NewsItem("valid news", "https://e.com/a", "Audit",
             published_at=dt2.isoformat(), content="ok"),
    NewsItem("no timestamp", "https://e.com/b", "Audit"),
    NewsItem("old news", "https://e.com/c", "Audit",
             published_at=(start - timedelta(days=2)).isoformat()),
])
check("最终质量闸门丢弃无时间和窗外条目",
      len(audit_items) == 1 and audit_items[0].title == "valid news")
quality_scraper = Scraper()
relevant_items = quality_scraper._filter_ai_relevant("Broad Feed", [
    NewsItem("Rivian starts SUV deliveries", "https://e.com/car", "Broad Feed",
             content="A new electric vehicle ships today.", published_at=dt2.isoformat()),
    NewsItem("OpenAI launches a coding agent", "https://e.com/agent", "Broad Feed",
             content="The AI agent can call developer tools.", published_at=dt2.isoformat()),
])
check("宽泛 RSS 前置过滤明显非 AI 内容",
      len(relevant_items) == 1 and relevant_items[0].url.endswith("/agent"))
check("栏目页 URL 不会被误判为文章",
      not Scraper()._looks_like_article_url(
          "https://example.com/news", "https://example.com/news/", require_hint=True)
      and Scraper()._is_generic_page_title("Research | Example AI"))

google_entry_ok = {
    "title": "可灵独立后拿下融资，打响 AI 视频竞赛 - 极客公园",
    "summary": "AI 视频与大模型相关报道",
    "source": {"title": "极客公园", "href": "https://www.geekpark.net"},
}
google_entry_other = {
    "title": "普通科技新闻",
    "summary": "无关报道",
    "source": {"title": "其他媒体", "href": "https://example.com"},
}
scraper_for_google = Scraper()
check("Google News 兜底严格匹配目标媒体",
      scraper_for_google._google_news_matches_source(
          google_entry_ok, ["极客公园"], ["geekpark.net"])
      and not scraper_for_google._google_news_matches_source(
          google_entry_other, ["极客公园"], ["geekpark.net"]))
check("Google News 兜底过滤非 AI/垃圾条目",
      scraper_for_google._google_news_item_relevant("DeepSeek 自研 AI 芯片", "")
      and not scraper_for_google._google_news_item_relevant(
          "热门标签", "谷歌留痕 免费试用"))


class _OrderScraper(Scraper):
    def __init__(self, success_at):
        super().__init__()
        self.success_at = success_at
        self.calls = []
    def scrape_rss(self, name, rss_url, category):
        self.calls.append("rss")
        return [NewsItem("rss ok", "https://e.com/rss", name, category,
                         published_at=dt2.isoformat(), scrape_strategy="rss")] if self.success_at == "rss" else []
    def scrape_discovered_feeds(self, name, url, category, known_urls=None):
        self.calls.append("discover")
        return [NewsItem("discover ok", "https://e.com/feed", name, category,
                         published_at=dt2.isoformat(),
                         scrape_strategy="feed_discovery")] if self.success_at == "discover" else []
    def scrape_sitemap(self, name, url, category):
        self.calls.append("sitemap")
        return [NewsItem("sitemap ok", "https://e.com/sm", name, category,
                         published_at=dt2.isoformat(), scrape_strategy="sitemap")] if self.success_at == "sitemap" else []
    def scrape_web(self, name, url, category, allowed_hosts=None):
        self.calls.append("web")
        return [NewsItem("web ok", "https://e.com/web", name, category,
                         published_at=dt2.isoformat(), scrape_strategy="jsonld")] if self.success_at == "web" else []
    def scrape_sogou(self, query, name, category, top_n=10):
        self.calls.append("sogou")
        return [NewsItem("sogou ok", "https://e.com/sogou", name, category,
                         published_at=dt2.isoformat(), scrape_strategy="sogou")] if self.success_at == "sogou" else []


src_order = {"name": "Order", "url": "https://e.com", "rss_url": "https://e.com/rss",
             "type": "web", "category": "测试", "sogou_fallback": True}
order_rss = _OrderScraper("rss")
rep_rss = order_rss.scrape_source(src_order)
check("RSS First：RSS 成功后不再调用后续链路",
      rep_rss.strategy == "rss" and order_rss.calls == ["rss"])
order_discover = _OrderScraper("discover")
rep_discover = order_discover.scrape_source(src_order)
check("RSS 为空后优先自动发现 Feed",
      rep_discover.strategy == "feed_discovery"
      and order_discover.calls == ["rss", "discover"])
order_sitemap = _OrderScraper("sitemap")
rep_sitemap = order_sitemap.scrape_source(src_order)
check("Feed 自动发现为空后再到 sitemap",
      rep_sitemap.strategy == "sitemap"
      and order_sitemap.calls == ["rss", "discover", "sitemap"])
order_sogou = _OrderScraper("sogou")
rep_sogou = order_sogou.scrape_source(src_order)
check("RSS/sitemap/网页均空后才调用 Sogou",
      rep_sogou.strategy == "sogou"
      and order_sogou.calls == ["rss", "discover", "sitemap", "web", "sogou"])

anysearch_scraper = Scraper()
anysearch_headers = {}


def _fake_anysearch_post(endpoint, payload, headers):
    anysearch_headers.update(headers)
    return {
        "code": 0,
        "message": "success",
        "data": {
            "results": [
                {
                    "title": "OpenAI 发布 GPT-5.6 大模型",
                    "url": "https://openai.com/index/gpt-5-6/",
                    "snippet": f"OpenAI 发布 GPT-5.6，Published: {dt2.isoformat()}",
                    "content": f"OpenAI 发布 GPT-5.6，Published: {dt2.isoformat()}",
                },
                {
                    "title": "窗口外旧 AI 新闻",
                    "url": "https://example.com/old-ai-news",
                    "snippet": "旧新闻 2020-01-01",
                    "content": "旧新闻 2020-01-01",
                },
            ]
        },
    }


anysearch_scraper._anysearch_post = _fake_anysearch_post
anysearch_items = anysearch_scraper.scrape_anysearch({
    "name": "AnySearch",
    "type": "anysearch",
    "category": "搜索聚合",
    "api_key": "as_sk_test",
    "anysearch_queries": ["AI news"],
    "max_results": 2,
})
check("AnySearch API 结果进入窗口内原始条目",
      len(anysearch_items) == 1 and anysearch_items[0].scrape_strategy == "anysearch")
check("AnySearch API Key 透传到请求头",
      anysearch_headers.get("Authorization") == "Bearer as_sk_test")
anysearch_rep = anysearch_scraper.scrape_source({
    "name": "AnySearch",
    "url": "https://anysearch.com/",
    "type": "anysearch",
    "category": "搜索聚合",
    "anysearch_queries": ["AI news"],
    "max_results": 2,
})
check("AnySearch 默认源调度成功",
      anysearch_rep.status == "success" and anysearch_rep.strategy == "anysearch")

tavily_scraper = Scraper()
tavily_headers = {}
tavily_payload = {}


def _fake_tavily_post(endpoint, payload, headers):
    tavily_headers.update(headers)
    tavily_payload.update(payload)
    return {
        "query": payload["query"],
        "results": [
            {
                "title": "Anthropic 发布新一代 AI Agent",
                "url": "https://example.com/new-ai-agent",
                "content": f"Anthropic 发布新一代 AI Agent，Published: {dt2.isoformat()}",
                "score": 0.92,
            },
            {
                "title": "窗口外旧 AI 新闻",
                "url": "https://example.com/old-tavily-ai-news",
                "content": "旧新闻 2020-01-01",
                "score": 0.85,
            },
        ],
    }


tavily_scraper._tavily_post = _fake_tavily_post
tavily_source = {
    "name": "Tavily",
    "url": "https://app.tavily.com/home",
    "type": "tavily",
    "category": "搜索聚合",
    "api_key": "tvly-test",
    "tavily_queries": ["latest AI news"],
    "max_results": 2,
}
tavily_items = tavily_scraper.scrape_tavily(tavily_source)
check("Tavily API 结果进入窗口内原始条目",
      len(tavily_items) == 1 and tavily_items[0].scrape_strategy == "tavily")
check("Tavily API Key 透传到请求头",
      tavily_headers.get("Authorization") == "Bearer tvly-test")
check("Tavily 使用新闻主题与严格日期参数",
      tavily_payload.get("topic") == "news"
      and tavily_payload.get("start_date")
      and tavily_payload.get("end_date"))
tavily_rep = tavily_scraper.scrape_source(tavily_source)
check("Tavily 默认源调度成功",
      tavily_rep.status == "success" and tavily_rep.strategy == "tavily")

print("=== 9. 配置完整性（PRD 合规）===")
check("信源总数=101", len(config.get_all_sources()) == 101)
check("Web/公司/搜索=64", len(config.WEB_SOURCES) == 64)
check("KOL=37", len(config.X_KOL_SOURCES) == 37)
check("Provider=8", len(config.LLM_PROVIDERS) == 8)
check("默认Provider存在", config.DEFAULT_PROVIDER in config.LLM_PROVIDERS)
check("所有KOL有handle", all("handle" in s for s in config.X_KOL_SOURCES))
check("所有源有name/url/category",
      all(s.get("name") and s.get("url") and s.get("category")
          for s in config.get_all_sources()))
check("KOL handle 无重复",
      len({s["handle"] for s in config.X_KOL_SOURCES}) == 37)
check("信源名无重复",
      len({s["name"] for s in config.get_all_sources()}) == 101)
check("AnySearch 默认源存在",
      any(s["name"] == "AnySearch" and s.get("type") == "anysearch"
          for s in config.get_all_sources()))
check("Tavily 默认源存在",
      any(s["name"] == "Tavily"
          and s.get("url") == "https://app.tavily.com/home"
          and s.get("type") == "tavily"
          for s in config.get_all_sources()))

expected_agent_industry_sources = {
    "Latent Space": (
        "https://www.latent.space/",
        "https://www.latent.space/feed",
    ),
    "Import AI": (
        "https://importai.substack.com/",
        "https://jack-clark.net/feed/",
    ),
    "Last Week in AI": (
        "https://lastweekin.ai/",
        "https://lastweekin.ai/feed",
    ),
    "Ben's Bites": (
        "https://www.bensbites.com/",
        "https://www.bensbites.com/feed",
    ),
    "Interconnects": (
        "https://www.interconnects.ai/",
        "https://www.interconnects.ai/feed",
    ),
    "One Useful Thing": (
        "https://www.oneusefulthing.org/",
        "https://www.oneusefulthing.org/feed",
    ),
    "Simon Willison": (
        "https://simonwillison.net/",
        "https://simonwillison.net/atom/everything/",
    ),
    "Microsoft Agent Framework Blog": (
        "https://devblogs.microsoft.com/agent-framework/",
        "https://devblogs.microsoft.com/agent-framework/feed/",
    ),
    "AWS Machine Learning Blog": (
        "https://aws.amazon.com/blogs/machine-learning/",
        "https://aws.amazon.com/blogs/machine-learning/feed/",
    ),
    "GitHub Copilot Changelog": (
        "https://github.blog/changelog/label/copilot/",
        "https://github.blog/changelog/label/copilot/feed/",
    ),
    "Vercel": (
        "https://vercel.com/",
        "https://vercel.com/atom",
    ),
    "n8n Blog": (
        "https://blog.n8n.io/",
        "https://blog.n8n.io/rss/",
    ),
    "Zapier Blog": (
        "https://zapier.com/blog/",
        "https://zapier.com/blog/feed/",
    ),
    "Salesforce AI Blog": (
        "https://www.salesforce.com/blog/category/ai/",
        "https://www.salesforce.com/blog/category/ai/feed/",
    ),
    "UiPath Agent SDK": (
        "https://github.com/UiPath/uipath-python",
        "https://github.com/UiPath/uipath-python/releases.atom",
    ),
    "Browser Use": (
        "https://www.browser-use.com/",
        "https://www.browser-use.com/rss.xml",
    ),
}
check("Agent 行业信息源数量=16",
      len(config.AGENT_INDUSTRY_SOURCES) == 16)
agent_industry_by_name = {
    source["name"]: source
    for source in config.AGENT_INDUSTRY_SOURCES
}
for source_name, (source_url, rss_url) in expected_agent_industry_sources.items():
    source = agent_industry_by_name.get(source_name, {})
    check(
        f"Agent 信息源配置完整：{source_name}",
        source.get("url") == source_url
        and source.get("rss_url") == rss_url
        and source.get("type") == "web"
        and source.get("category") == "Agent智能体信息源",
    )

# PRD 信源逐项合规（抓取网站 12 个 URL 必须原样存在）
print("=== 10. PRD 网站信源逐项合规 ===")
prd_web_urls = {
    "The Rundown AI": "https://www.rundown.ai/",
    "TLDR AI": "https://tldr.tech/ai",
    "The Decoder": "https://the-decoder.com/",
    "The Information": "https://www.theinformation.com/",
    "TechCrunch": "https://techcrunch.com/category/artificial-intelligence/",
    "MIT Technology Review": "https://www.technologyreview.com/topic/artificial-intelligence/",
    "Hugging Face": "https://huggingface.co/",
    "Reddit r/LocalLLaMA": "https://www.reddit.com/r/LocalLLaMA/",
    "机器之心": "https://www.jiqizhixin.com/",
    "新智元": "https://www.xinzhiyuan.com/",
    "极客公园": "https://www.geekpark.net/",
    "钛媒体": "https://www.tmtpost.com/",
}
all_urls = {s["url"].rstrip("/") for s in config.WEB_SOURCES}
for prd_name, prd_url in prd_web_urls.items():
    check(f"PRD URL 存在：{prd_name}", prd_url.rstrip("/") in all_urls)

# PRD 大模型企业逐项合规（子串匹配，Kimi/Moonshot 合并仍通过）
print("=== 11. PRD 大模型企业逐项合规 ===")
prd_companies = ["OpenAI", "Anthropic", "Nvidia", "Meta", "Google",
                 "Bytedance", "Tencent", "Alibaba", "Kimi", "Moonshot",
                 "智谱 GLM", "DeepSeek", "Grok", "xAI"]
company_blob = " ".join(c["name"].lower() for c in config.COMPANY_LLM)
for name in prd_companies:
    check(f"PRD 公司存在：{name}", name.lower() in company_blob)

# PRD KOL handle 逐项合规（37 个全量）
print("=== 12. PRD KOL handle 逐项合规 ===")
prd_handles = [
    "karpathy", "ylecun", "AndrewYNg", "drfeifei", "DrJimFan", "denny_zhou",
    "tri_dao", "SwaroopMishra_",
    "sama", "gdb", "demishassabis", "AravSrinivas", "elonmusk",
    "ClementDelangue", "hwchase17", "bindureddy",
    "ArtificialAnl", "lmsysorg", "swe_bench", "NousResearch", "TogetherAM",
    "LiveBenchAI",
    "emollick", "simonw", "_akhaliq", "rasbt", "natolambert", "_philipp_schmid",
    "kbindas", "DrustZ", "b_clavie", "maximelabonne", "antonosika", "anya_tw",
    "rowancheung", "mattshumer_", "OfficialLoganK",
]
config_handles = {s["handle"] for s in config.X_KOL_SOURCES}
check("PRD KOL 数量=37", len(prd_handles) == 37)
for h in prd_handles:
    check(f"PRD KOL 存在：@{h}", h in config_handles)

# PRD 支持模型合规（Bytedance ModelHub）
print("=== 13. PRD Bytedance 模型合规 ===")
bd_models = config.LLM_PROVIDERS["Bytedance ModelHub"]["models"]
for m in [
    "gpt-5.5-2026-04-24",
    "gemini-3.1-p",
    "gemini-3.1-p-priority",
    "gemini-3.5-flash",
    "gpt-5.6-terra",
    "gpt-5.6-sol",
    "gpt-5.6-luna",
    "deepseek_v4_pro",
]:
    check(f"模型存在：{m}", m in bd_models)
check("Bytedance endpoint 正确",
      config.LLM_PROVIDERS["Bytedance ModelHub"]["base_url"]
      == "https://aidp.bytedance.net/api/modelhub/online/v2/crawl")
check("api_version 正确",
      config.LLM_PROVIDERS["Bytedance ModelHub"]["api_version"] == "2024-03-01-preview")

from app.main import (
    _company_filter_values,
    _dedupe_dashboard_structured_items,
    _prepare_structured_items,
    _selected_model,
    _structured_filter_options,
)
from app.models import StructuredNewsRecord
from fastapi import HTTPException

check("手动型号优先于预设模型",
      _selected_model("gemini-3.5-flash", "gpt-5.6-terra") == "gpt-5.6-terra")
check("未填写手动型号时使用预设模型",
      _selected_model("gpt-5.6-sol", "") == "gpt-5.6-sol")
try:
    _selected_model("", "", required=True)
    check("不选择模型时必须填写手动型号", False)
except HTTPException as exc:
    check("不选择模型时必须填写手动型号", exc.status_code == 400)

print("=== 13.1 公司筛选拆分与别名归一化 ===")
check("联合公司拆分为独立选项",
      _company_filter_values("Apple / Alibaba") == ["Apple", "阿里巴巴"])
check("三个联合公司均可独立筛选",
      _company_filter_values("OpenAI / Anthropic / Google")
      == ["OpenAI", "Anthropic", "Google"])
check("智谱中英文别名统一",
      all(_company_filter_values(name) == ["智谱 AI"]
          for name in ["智谱", "智谱AI", "智谱 GLM (Zhipu)", "Zhipu AI"]))
check("月之暗面中英文别名统一",
      all(_company_filter_values(name) == ["月之暗面"]
          for name in ["月之暗面", "Moonshot", "Moonshot AI", "Kimi / Moonshot"]))
check("NVIDIA 中英文及大小写别名统一",
      all(_company_filter_values(name) == ["NVIDIA"]
          for name in ["NVIDIA", "Nvidia", "英伟达"]))
check("中英文集团别名统一",
      _company_filter_values("字节跳动 (Bytedance) / Tencent / Alibaba")
      == ["字节跳动", "腾讯", "阿里巴巴"])

embodied_record = StructuredNewsRecord(
    event="具身智能体驱动人形机器人完成复杂操作",
    detail="机器人基础模型融合视觉、语言与动作控制能力。",
    news_type="具身机器人",
    company="",
    source="测试信源",
)
_prepare_structured_items([embodied_record])
check("具身机器人主题不被智能体关键词覆盖",
      embodied_record.filter_topic == "具身机器人")
empty_topic_options = _structured_filter_options([])
check("具身机器人始终出现在主题筛选",
      "具身机器人" in empty_topic_options["topics"])
check("具身机器人已加入结构化新闻合法分类",
      "具身机器人" in config.NEWS_TYPES)

financing_record = StructuredNewsRecord(
    event="AI Agent 创业公司完成新一轮战略融资",
    detail="该公司披露融资轮次、募资金额及本轮投资方。",
    news_type="项目融资",
    company="测试公司",
    source="测试信源",
)
_prepare_structured_items([financing_record])
check("项目融资主题不被 Agent 关键词覆盖",
      financing_record.filter_topic == "项目融资")
check("项目融资始终出现在主题筛选",
      "项目融资" in empty_topic_options["topics"])
check("项目融资已加入结构化新闻合法分类",
      "项目融资" in config.NEWS_TYPES)

print("=== 13.2 历史专项主题迁移规则 ===")
check("旧融资新闻归入项目融资",
      classify_special_news_type(
          "AI Agent 创业公司完成一亿美元 A 轮融资",
          current_type="Agent智能体",
      ) == "项目融资")
check("机器人公司融资优先归入项目融资",
      classify_special_news_type(
          "工业 AI 机器人公司完成十亿美元融资",
          "资金将用于机器人基础模型研发。",
          current_type="具身机器人",
      ) == "项目融资")
check("IPO 筹备归入项目融资",
      classify_special_news_type(
          "月之暗面寻求批准启动香港 IPO 筹备工作",
          current_type="其他重大动态",
      ) == "项目融资")
check("旧具身模型新闻归入具身机器人",
      classify_special_news_type(
          "腾讯开源具身智能基座模型",
          "模型打通感知、规划与行动闭环。",
          current_type="新大模型发布",
      ) == "具身机器人")
check("机器人模型新闻归入具身机器人",
      classify_special_news_type(
          "Mistral 发布机器人导航大模型",
          current_type="新大模型发布",
      ) == "具身机器人")
check("单纯估值分化不误判为项目融资",
      classify_special_news_type(
          "两家 AI 公司估值差距继续扩大",
          "资本市场投资策略出现分化。",
          current_type="其他重大动态",
      ) == "其他重大动态")
check("营收新闻提及历史融资不误判",
      classify_special_news_type(
          "投资人称 Anthropic 年化营收创新高",
          "报道同时回顾该投资人曾领投公司的 D 轮融资。",
          current_type="其他重大动态",
      ) == "其他重大动态")
check("并购估值不误判为项目融资",
      classify_special_news_type(
          "腾讯拟以二十亿美元估值控股 AI 智能体公司",
          current_type="Agent智能体",
      ) == "Agent智能体")
check("AI 聊天机器人不误判为具身机器人",
      classify_special_news_type(
          "医疗 AI 聊天机器人上线",
          "产品帮助医生查询医学资料。",
          current_type="新产品发布",
      ) == "新产品发布")
check("AI 爬虫不误判为具身机器人",
      classify_special_news_type(
          "平台联合 Cloudflare 封堵 AI 爬虫",
          "平台阻止用于训练 AI 模型的机器人，并强化 robots.txt 规则。",
          current_type="其他重大动态",
      ) == "其他重大动态")
check("潜在影响提到机器人不改变新闻事实分类",
      classify_special_news_type(
          "手术视频基础模型发布",
          "模型用于理解和分析手术视频。",
          "未来可能为智能手术机器人提供技术支撑。",
          current_type="新大模型发布",
      ) == "新大模型发布")

print("=== 13.3 今日简报跨日期去重 ===")


def _history_record(
    record_id,
    event,
    url,
    published_at,
    *,
    company="测试公司",
    detail="用于跨日期去重测试的结构化新闻摘要。",
):
    return StructuredNewsRecord(
        id=record_id,
        event=event,
        detail=detail,
        impact="用于验证代表稿选择和页面展示隔离。",
        news_type="其他重大动态",
        company=company,
        source="测试信源",
        url=url,
        published_at=published_at,
        category="海外科技媒体",
    )


cross_date_records = [
    _history_record(
        1,
        "OpenAI正式发布全新GPT模型能力",
        "https://techcrunch.com/openai-gpt?ref=old",
        "2026-07-20T10:00:00+08:00",
    ),
    _history_record(
        2,
        "OpenAI正式发布全新GPT模型能力",
        "https://openai.com/news/gpt",
        "2026-07-21T10:00:00+08:00",
    ),
    _history_record(
        3,
        "OpenAI正式发布全新GPT模型能力",
        "https://example.com/openai-gpt",
        "2026-07-22T10:00:00+08:00",
    ),
    _history_record(
        4,
        "另一家公司发布完全不同的模型",
        "https://example.com/other",
        "2026-07-22T11:00:00+08:00",
    ),
]
_dedupe_dashboard_structured_items(cross_date_records)
check("跨日期相似新闻仅保留官方源",
      [record.id for record in cross_date_records
       if not record.cross_date_duplicate] == [2, 4])

same_quality_records = [
    _history_record(
        5,
        "某AI公司完成新一轮融资",
        "https://example-a.com/funding",
        "2026-07-18T09:00:00+08:00",
    ),
    _history_record(
        6,
        "某AI公司完成新一轮融资",
        "https://example-b.com/funding",
        "2026-07-19T09:00:00+08:00",
    ),
]
_dedupe_dashboard_structured_items(same_quality_records)
check("同质量信源优先保留更早日期",
      not same_quality_records[0].cross_date_duplicate
      and same_quality_records[1].cross_date_duplicate)

same_date_records = [
    _history_record(
        7,
        "同日媒体报道某模型正式发布",
        "https://example-a.com/model",
        "2026-07-20T09:00:00+08:00",
    ),
    _history_record(
        8,
        "同日媒体报道某模型正式发布",
        "https://example-b.com/model",
        "2026-07-20T15:00:00+08:00",
    ),
]
_dedupe_dashboard_structured_items(same_date_records)
check("同一日期新闻不参与跨日期去重",
      not any(record.cross_date_duplicate for record in same_date_records))

exact_url_records = [
    _history_record(
        9,
        "某模型发布的首篇报道",
        "https://example.com/news?id=first",
        "2026-07-18T09:00:00+08:00",
    ),
    _history_record(
        10,
        "媒体以完全不同标题再次报道该事件",
        "https://example.com/news?id=second",
        "2026-07-19T09:00:00+08:00",
    ),
]
_dedupe_dashboard_structured_items(exact_url_records)
check("URL 去 query 后执行跨日期精确去重",
      not exact_url_records[0].cross_date_duplicate
      and exact_url_records[1].cross_date_duplicate)

semantic_release_records = [
    _history_record(
        11,
        "月之暗面发布开源大模型 Kimi K3",
        "https://example-a.com/kimi-k3",
        "2026-07-17T09:00:00+08:00",
        company="Moonshot AI",
    ),
    _history_record(
        12,
        "Moonshot AI推出Kimi K3挑战全球前沿模型",
        "https://example-b.com/moonshot-model",
        "2026-07-20T09:00:00+08:00",
        company="月之暗面",
    ),
]
_dedupe_dashboard_structured_items(semantic_release_records)
check("不同措辞的同一模型发布可语义去重",
      not semantic_release_records[0].cross_date_duplicate
      and semantic_release_records[1].cross_date_duplicate)

semantic_security_records = [
    _history_record(
        13,
        "OpenAI新模型被曝自动删除用户文件",
        "https://example-a.com/security",
        "2026-07-15T09:00:00+08:00",
        company="OpenAI",
    ),
    _history_record(
        14,
        "GPT-5.6全访问模式误删用户主目录引发警报",
        "https://example-b.com/security",
        "2026-07-18T09:00:00+08:00",
        company="OpenAI",
    ),
]
_dedupe_dashboard_structured_items(semantic_security_records)
check("标题差异较大的同一安全事件可语义去重",
      sum(record.cross_date_duplicate for record in semantic_security_records) == 1)

different_action_records = [
    _history_record(
        15,
        "OpenAI正式发布GPT-5.6大模型",
        "https://example-a.com/release",
        "2026-07-16T09:00:00+08:00",
        company="OpenAI",
    ),
    _history_record(
        16,
        "GPT-5.6全访问模式误删用户文件",
        "https://example-b.com/incident",
        "2026-07-18T09:00:00+08:00",
        company="OpenAI",
    ),
    _history_record(
        17,
        "GPT-5.6 Sol在权威大模型评测中登顶",
        "https://example-c.com/benchmark",
        "2026-07-19T09:00:00+08:00",
        company="OpenAI",
    ),
]
_dedupe_dashboard_structured_items(different_action_records)
check("同公司同模型但核心动作不同不得合并",
      not any(record.cross_date_duplicate for record in different_action_records))

composite_event_records = [
    _history_record(
        18,
        "OpenAI正式发布GPT-5.6系列模型",
        "https://example-a.com/gpt",
        "2026-07-16T09:00:00+08:00",
        company="OpenAI",
    ),
    _history_record(
        19,
        "OpenAI上线GPT-5.6并推出ChatGPT Work",
        "https://example-b.com/gpt-work",
        "2026-07-17T09:00:00+08:00",
        company="OpenAI",
    ),
]
_dedupe_dashboard_structured_items(composite_event_records)
check("包含另一款新产品的复合新闻不得被隐藏",
      not any(record.cross_date_duplicate for record in composite_event_records))

named_entity_records = [
    _history_record(
        20,
        "DeepMind将视频生成模型改造为感知模型",
        "https://example-a.com/genception",
        "2026-07-14T09:00:00+08:00",
        company="Google DeepMind",
        detail="Google DeepMind论文GenCeption将视频生成模型用于视觉感知。",
    ),
    _history_record(
        21,
        "DeepMind用视频生成器探索世界模型",
        "https://example-b.com/world-model",
        "2026-07-19T09:00:00+08:00",
        company="Google DeepMind",
        detail="Google DeepMind提出GenCeption并探索通用世界模型能力。",
    ),
]
_dedupe_dashboard_structured_items(named_entity_records)
check("正文共享专有事件名可识别同一研究",
      sum(record.cross_date_duplicate for record in named_entity_records) == 1)

settlement_records = [
    _history_record(
        22,
        "Anthropic十五亿美元版权和解获最终批准",
        "https://example-a.com/settlement",
        "2026-07-21T09:00:00+08:00",
        company="Anthropic",
    ),
    _history_record(
        23,
        "Anthropic与作者达成15亿美元盗版作品和解",
        "https://example-b.com/copyright",
        "2026-07-23T09:00:00+08:00",
        company="Anthropic",
    ),
]
_dedupe_dashboard_structured_items(settlement_records)
check("不同金额写法的同一法律和解可语义去重",
      sum(record.cross_date_duplicate for record in settlement_records) == 1)

sogou_redirect_records = [
    _history_record(
        24,
        "腾讯发布全新模型",
        "https://weixin.sogou.com/link?url=article-a&token=one",
        "2026-07-20T09:00:00+08:00",
        company="腾讯",
    ),
    _history_record(
        25,
        "阿里发布另一款模型",
        "https://weixin.sogou.com/link?url=article-b&token=two",
        "2026-07-21T09:00:00+08:00",
        company="阿里巴巴",
    ),
]
_dedupe_dashboard_structured_items(sogou_redirect_records)
check("搜狗相同入口下不同目标文章不得误合并",
      not any(record.cross_date_duplicate for record in sogou_redirect_records))

company_records = [
    StructuredNewsRecord(
        event="联合公司新闻",
        news_type="其他重大动态",
        company="Apple / Alibaba",
    ),
    StructuredNewsRecord(
        event="智谱新闻",
        news_type="其他重大动态",
        company="智谱AI",
    ),
    StructuredNewsRecord(
        event="月之暗面新闻",
        news_type="其他重大动态",
        company="Kimi / Moonshot",
    ),
]
_prepare_structured_items(company_records)
check("结构化新闻携带多个公司筛选值",
      company_records[0].filter_companies == ["Apple", "阿里巴巴"])
company_options = _structured_filter_options(company_records)
check("公司下拉只包含拆分归一后的单项",
      set(company_options["companies"]) == {"Apple", "阿里巴巴", "智谱 AI", "月之暗面"}
      and all("/" not in company for company in company_options["companies"]))
check("统一名称仍支持中英文别名搜索",
      "Moonshot" in company_options["company_search_terms"]["月之暗面"]
      and "Zhipu" in company_options["company_search_terms"]["智谱 AI"])

# PRD 公开三方 Provider 合规
print("=== 14. PRD 公开三方 Provider 合规 ===")
for p in ["OpenRouter", "Gemini", "OpenAI", "Anthropic", "Kimi", "MiniMax", "DeepSeek"]:
    check(f"Provider 存在：{p}", p in config.LLM_PROVIDERS)

print("=== 15. 板块筛选（main._apply_filters 含 category）===")
from news_processor import StructuredNews, _pick_representative


def _sn(event, cat="海外科技媒体", company="", url="https://a.com/1", detail="d"):
    return StructuredNews(event=event, detail=detail, impact="i", news_type="其他重大动态",
                          source="s", url=url, company=company, category=cat)


import main as _main
sample = [
    _sn("新闻A", cat="海外科技媒体"),
    _sn("新闻B", cat="国内媒体"),
    _sn("新闻C", cat="公司-大模型企业", company="OpenAI"),
]
cfg0 = {"cats": [], "companies": [], "types": []}
check("空筛选返回全部", len(_main._apply_filters(sample, cfg0)) == 3)
cfg1 = {"cats": ["国内媒体"], "companies": [], "types": []}
out1 = _main._apply_filters(sample, cfg1)
check("板块筛选生效(只剩国内媒体)", len(out1) == 1 and out1[0].event == "新闻B")
cfg2 = {"cats": ["海外科技媒体", "公司-大模型企业"], "companies": ["OpenAI"], "types": []}
out2 = _main._apply_filters(sample, cfg2)
check("板块+公司组合筛选", len(out2) == 1 and out2[0].event == "新闻C")

print("=== 16. 语义去重（代表稿选择 + 分组防御校验）===")
grp = [
    _sn("Anthropic 发布 Fable 5", url="https://weixin.sogou.com/link?x=1", detail="短"),
    _sn("Anthropic 推出 Claude Fable 5 公开版",
        url="https://www.anthropic.com/news/fable-5", detail="这是更详实的官方稿内容描述"),
]
rep = _pick_representative(grp)
check("代表稿选官方源", "anthropic.com" in rep.url)
grp2 = [_sn("事件X", url="https://c.com/1", detail="内容较短"),
        _sn("事件X另一种说法", url="https://d.com/2", detail="内容明显更长更详实的一篇")]
check("同质量源选更详实者", _pick_representative(grp2).url == "https://d.com/2")


class _FakeClient:
    """模拟 LLM：返回固定 duplicate_groups（含越界/重叠等脏数据）。"""
    def __init__(self, resp):
        self.resp = resp
    def chat(self, *a, **k):
        return self.resp


from news_processor import NewsProcessor
news5 = [
    _sn("Anthropic 发布 Fable 5", url="https://weixin.sogou.com/x"),
    _sn("OpenAI 提交 IPO", url="https://a.com/2"),
    _sn("Anthropic Fable 5 公开上线", url="https://www.anthropic.com/news/f5"),
    _sn("谷歌降价", url="https://a.com/4"),
    _sn("完全独立事件", url="https://a.com/5"),
]
proc = NewsProcessor.__new__(NewsProcessor)   # 跳过 __init__（不需要真 client）
proc._log = lambda m: None
# 0 与 2 同事件；脏数据：[99](越界)、[3](单元素)、重叠组 [2,4]（2 已被占用）
proc.client = _FakeClient('{"duplicate_groups": [[0,2,99], [3], [2,4]]}')
out = proc._semantic_dedupe(news5)
check("同事件合并(5→4)", len(out) == 4)
check("保留官方源代表稿", any("anthropic.com" in n.url for n in out))
check("被合并的转载稿已删", not any("sogou" in n.url for n in out))
check("不相关事件不受影响",
      {n.event for n in out} >= {"OpenAI 提交 IPO", "谷歌降价", "完全独立事件"})
proc.client = _FakeClient('{"duplicate_groups": []}')
check("无重复时原样返回", len(proc._semantic_dedupe(news5)) == 5)
proc.client = _FakeClient('not json at all')
try:
    proc._semantic_dedupe(news5)
    check("LLM 返回非 JSON 抛 ValueError(由上层 fail-open 捕获)", False)
except ValueError:
    check("LLM 返回非 JSON 抛 ValueError(由上层 fail-open 捕获)", True)

print(f"\n===== 结果：{_passed} 通过 / {_failed} 失败 =====")
sys.exit(0 if _failed == 0 else 1)
