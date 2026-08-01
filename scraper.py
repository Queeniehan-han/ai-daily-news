# -*- coding: utf-8 -*-
"""
scraper.py — 多源抓取引擎（AI每日大事件 Max）

实现 PRD 两条硬约束：
  1) 「全量摘取以下所有信息源的信息」—— 84 个信源每个都有报告行，绝不遗漏；
  2) 「确保信息时间为昨日 11am 至当日 11am」—— 所有源统一严格 CST 窗口过滤。

抓取策略（融合项目历史全部踩坑修复）：
  - HTTP 客户端：curl_cffi(impersonate=chrome120) 为主（解决 X.com/Cloudflare
    按 TLS 指纹拒绝 python-requests 的问题）；requests 兜底。
  - RSS：feedparser 解析 curl_cffi 抓回的字节（不要用 feedparser 自带抓取器）。
  - SPA 公司官网：<article>/<h1-3><a> DOM 提取 → 全锚点启发式扫描兜底
    （多数 AI 公司站是 React/Vue SPA，日期常与标题粘连，见 _DATE_PATTERNS）。
  - 国内被封站点 / 国内公司：Sogou 微信搜索兜底（必带 Referer header）。
  - X.com KOL：ScrapeCreators → syndication → 免费 RSS 镜像，多链路合并；
    无付费官方 API 时不伪造“全量”，用 completeness 明确标注尽力/疑似不全。

关键护栏：
  - _http_get 把 403/404/410/429 当作「返回 response」而非 None，便于上层诊断
    （区分「网络挂了」与「被限频/被拒」）。
  - 全量抓取用 ThreadPoolExecutor + 硬超时，shutdown(wait=False, cancel_futures=True)
    防单个卡死的 HTTP future 拖住整轮（with 语句的隐式 shutdown(wait=True) 是坑）。
  - KOL 不进 Web executor（syndication 有 IP 级限频，必须串行 + 退避）。
"""

from __future__ import annotations

import json
import gzip
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutTimeout
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urljoin, urlparse

import config

# ── 可选依赖（带降级守卫）────────────────────────────────────────────────
try:
    from curl_cffi import requests as cffi_requests  # type: ignore
    _HAS_CURL_CFFI = True
except Exception:  # pragma: no cover
    _HAS_CURL_CFFI = False

import requests as _requests  # 兜底 HTTP 客户端

try:
    import feedparser  # type: ignore
    _HAS_FEEDPARSER = True
except Exception:  # pragma: no cover
    _HAS_FEEDPARSER = False

try:
    import trafilatura  # type: ignore
    _HAS_TRAFILATURA = True
except Exception:  # pragma: no cover
    _HAS_TRAFILATURA = False

from bs4 import BeautifulSoup  # type: ignore


CST = config.CST
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


# ──────────────────────────────────────────────────────────────────────────
# 数据类型
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class NewsItem:
    """单条抓取到的原始新闻条目。"""
    title: str
    url: str
    source: str                          # 信源显示名
    category: str = ""                   # 板块（来自 config）
    content: str = ""                    # 摘要/正文片段
    published_at: Optional[str] = None   # ISO 字符串
    scrape_strategy: str = ""            # rss / web / spa / sogou / syndication

    def to_dict(self) -> Dict:
        return {
            "title": self.title, "url": self.url, "source": self.source,
            "category": self.category, "content": self.content,
            "published_at": self.published_at, "scrape_strategy": self.scrape_strategy,
        }


@dataclass
class SourceReport:
    """单个信源的抓取报告行（PRD：第一轮抓取后自动评价是否有遗漏的依据）。"""
    name: str
    category: str
    type: str
    count: int = 0
    strategy: str = ""
    status: str = ""                     # success / empty / error / timeout
    error: str = ""
    items: List[NewsItem] = field(default_factory=list)
    issue_type: str = ""                 # ok / window_empty / parser / blocked / invalid_url / network / suspected_partial
    completeness: str = ""               # X KOL: best_effort / suspected_partial / unknown_empty / failed
    latest_seen_at: Optional[str] = None  # X KOL: 免费链路可见的最新推文时间
    oldest_seen_at: Optional[str] = None  # X KOL: 免费链路可见的最旧推文时间


@dataclass
class KolFetchResult:
    """单个 X KOL 免费链路的抓取结果，用于合并与完整性判断。"""
    provider: str
    items: List[NewsItem] = field(default_factory=list)
    note: str = ""
    ok: bool = False
    fetched_count: int = 0
    latest_dt: Optional[datetime] = None
    oldest_dt: Optional[datetime] = None
    limited: bool = False


# ──────────────────────────────────────────────────────────────────────────
# 时间窗口（单一真相，所有抓取路径共用）
# ──────────────────────────────────────────────────────────────────────────
def get_strict_window() -> Tuple[datetime, datetime]:
    """返回 PRD 规定的严格窗口 [昨日 11am, 当日 11am]（CST）。

    PRD 原文：「确保信息时间为昨日 11am 至当日 11am」——窗口恒定为
    [昨天 11:00, 今天 11:00]，与当前运行时刻无关。

    注意：不要写成滚动窗口（过了 11am 就变成 [今天 11am, 明天 11am)）。
    那样在 11am 后不久运行时窗口内只有 1-2 小时的内容，全部信源都会
    「empty」——这是 2026-06-10 用户实测踩中的 bug，已修复为 PRD 字面语义。
    上界有 WINDOW_GRACE_HOURS 容差，今天 11am 后刚发布的内容不会被误杀。
    """
    now = datetime.now(CST)
    today_11 = now.replace(hour=11, minute=0, second=0, microsecond=0)
    return today_11 - timedelta(days=1), today_11


def within_window(dt: Optional[datetime]) -> bool:
    """时间是否落在严格窗口内。

    无时间戳 → **丢弃**（PRD 硬约束：信息时间必须为昨日 11am 至当日 11am。
    无法确认时间的条目一律不放行；Web/SPA 条目会先经过文章详情页二段日期
    核验（_finalize_web_items）再到这里，因此不会整源误杀）。
    """
    if dt is None:
        return False
    start, end = get_strict_window()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return start <= dt <= end + timedelta(hours=config.WINDOW_GRACE_HOURS)


# ──────────────────────────────────────────────────────────────────────────
# 日期提取（SPA 锚文本「数字粘字母」陷阱：用 (?=\D|$) / (?<!\d) 替代 \b）
# ──────────────────────────────────────────────────────────────────────────
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_DATE_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # "Jun 3, 2026Grok..." — 结尾 (?=\D|$) 吸收年份后紧贴标题首字母的情况
    (re.compile(
        r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
        r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
        r"Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2}),?\s+(\d{4})(?=\D|$)",
        re.IGNORECASE), "month_day_year"),
    # "2026-06-09" / "2026/06/09"
    (re.compile(r"(?<!\d)(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?=\D|$)"), "ymd"),
    # "06.08.26" / "6/8/2026"（美式 mm/dd/yy[yy]，过于通用 → 下游加护栏）
    (re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})[./](\d{2,4})(?=\D|$)"), "mdy"),
    # "2026年6月9日"
    (re.compile(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日"), "cn_ymd"),
]


def _build_dt(year: int, month: int, day: int) -> Optional[datetime]:
    if not (2000 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31):
        return None
    try:
        # 取中午 12 点为默认时刻，避开窗口边界歧义
        return datetime(year, month, day, 12, 0, 0, tzinfo=CST)
    except ValueError:
        return None


def parse_date_from_text(text: str) -> Optional[datetime]:
    """从文本（取前 200 字符）中提取日期，提取不到返回 None。"""
    if not text:
        return None
    snippet = text[:200]
    for pat, kind in _DATE_PATTERNS:
        m = pat.search(snippet)
        if not m:
            continue
        try:
            if kind == "month_day_year":
                mon = _MONTHS[m.group(1)[:3].lower()]
                return _build_dt(int(m.group(3)), mon, int(m.group(2)))
            if kind == "ymd":
                return _build_dt(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if kind == "mdy":
                mon, day, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if yr < 100:
                    yr += 2000
                if mon > 12 or day > 31:   # 护栏：美式模式误匹配丢弃
                    continue
                return _build_dt(yr, mon, day)
            if kind == "cn_ymd":
                return _build_dt(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except (ValueError, KeyError):
            continue
    return None


def parse_iso_or_struct(value) -> Optional[datetime]:
    """解析 feedparser struct_time / ISO 字符串 / Twitter created_at。"""
    if value is None:
        return None
    if hasattr(value, "tm_year"):        # feedparser struct_time（UTC）
        try:
            from datetime import timezone
            return datetime(*value[:6], tzinfo=timezone.utc).astimezone(CST)
        except Exception:
            return None
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None
        # Twitter 格式: "Tue Jan 24 20:14:18 +0000 2023"
        try:
            return datetime.strptime(v, "%a %b %d %H:%M:%S %z %Y").astimezone(CST)
        except ValueError:
            pass
        try:
            from dateutil import parser as dtp  # 局部导入，避免硬依赖
            dt = dtp.parse(v)
            return dt.astimezone(CST) if dt.tzinfo else dt.replace(tzinfo=CST)
        except Exception:
            return parse_date_from_text(v)
    return None


def parse_relative_age(text: str) -> Optional[datetime]:
    """Parse lightweight relative timestamps returned by search APIs."""
    low = (text or "").lower()
    patterns = (
        (r"(\d+)\s*(?:minute|minutes|min|mins)\s+ago", "minutes"),
        (r"(\d+)\s*(?:hour|hours|hr|hrs)\s+ago", "hours"),
        (r"(\d+)\s*(?:day|days)\s+ago", "days"),
    )
    now = datetime.now(CST)
    for pattern, unit in patterns:
        m = re.search(pattern, low)
        if not m:
            continue
        amount = int(m.group(1))
        if unit == "minutes":
            return now - timedelta(minutes=amount)
        if unit == "hours":
            return now - timedelta(hours=amount)
        if unit == "days":
            return now - timedelta(days=amount)
    return None


# ──────────────────────────────────────────────────────────────────────────
# 抓取器
# ──────────────────────────────────────────────────────────────────────────
# SPA 兜底的文章 URL 关键词（必须够宽：OpenAI 用 /index/<slug> 这种意外前缀）
_SPA_URL_HINTS = (
    "/blog", "/news", "/post", "/article", "/research", "/release",
    "/announce", "/updates", "/stories", "/p/", "/posts/", "/articles/",
    "/papers/", "/discover/", "/insights/", "/announcement",
    "/news-updates", "/news-and-events", "/news_and_events", "/topic/",
    "/category/", "/2026/", "/2025/", "/feature", "/launch", "/index/",
    "/customer-stories/", "/safety/", "/about/", "/announcements/",
)

# 导航/目录链接黑名单（不是文章）
_ANCHOR_BLACKLIST = {
    "research", "news", "blog", "articles", "posts", "updates",
    "global affairs", "company", "publications", "all posts", "see all",
    "view all", "load more", "more", "learn more", "read more", "home",
    "about", "contact", "careers", "pricing", "docs", "documentation",
}

_LISTING_SLUGS = {
    "article", "articles", "blog", "blogs", "category", "customer-stories",
    "discover", "insights", "news", "post", "posts", "press", "publications",
    "release-notes", "releases", "research", "resources", "stories", "tag",
    "topic", "updates",
}

_GENERIC_PAGE_TITLES = {
    "all articles", "all news", "all posts", "articles", "blog",
    "customer stories", "discover", "latest news", "news", "press",
    "publications", "release notes", "research", "resources", "updates",
}

_AI_TEXT_MARKERS = (
    "artificial intelligence", "generative ai", "machine learning",
    "large language model", "language model", "foundation model",
    "multimodal", "neural network", "transformer", "inference",
    "agentic", "ai agent", "coding agent", "world model",
    "openai", "chatgpt", "anthropic", "claude", "gemini", "deepmind",
    "deepseek", "hugging face", "midjourney", "stable diffusion",
    "sora", "grok", "llama", "qwen", "copilot", "langchain",
    "人工智能", "生成式", "大模型", "语言模型", "基础模型", "多模态",
    "智能体", "机器学习", "深度学习", "神经网络", "模型推理", "模型训练",
    "算力", "英伟达", "芯片", "机器人", "具身智能", "世界模型",
    "豆包", "千问", "通义", "智谱", "可灵", "海螺",
)

_FEED_MIME_HINTS = (
    "rss", "atom", "feed", "xml",
)

_COMMON_FEED_PATHS = (
    "/feed", "/feed.xml", "/rss.xml", "/atom.xml", "/index.xml",
)

_COMMON_SITEMAP_PATHS = (
    "/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
    "/sitemap-news.xml", "/news-sitemap.xml",
)

_META_DATE_KEYS = {
    "article:published_time", "article:published", "og:published_time",
    "datepublished", "datepublished", "date", "dc.date", "dcterms.date",
    "pubdate", "publishdate", "publish-date", "publish_date",
    "parsely-pub-date", "sailthru.date", "bt:pubdate", "timestamp",
}

_JSON_DATE_KEYS = (
    "datePublished", "dateCreated", "publishedAt", "firstPublishedAt",
    "publishDate", "published_time", "publicationDate", "date",
)


class Scraper:
    """多源抓取器。X.com syndication 部分线程安全（串行 + 自适应退避）。"""

    def __init__(self, log_fn: Optional[Callable[[str], None]] = None):
        self._log = log_fn or (lambda m: None)
        # 每信源诊断信息（key=信源名；同一信源只在一个线程中抓取，无竞争）
        self._diag: Dict[str, List[str]] = {}
        # X.com 自适应退避状态
        self._x_lock = threading.Lock()
        self._x_last_ts = 0.0
        self._x_min_interval = 5.0      # 实测 5s 间隔冷启动稳定（<3s 必 429）
        self._x_backoff = 1.0
        self._x_consec_429 = 0
        # ScrapeCreators 鉴权/余额/限频类失败本轮熔断；这类错误对所有 KOL
        # 都会重复发生，继续并发请求只会拖慢整轮抓取。
        self._sc_lock = threading.Lock()
        self._sc_disabled = False
        self._sc_last_error = ""
        # 轻量域名级限速，避免同一站点在 sitemap/详情页核验时被并发打爆。
        self._domain_lock = threading.Lock()
        self._domain_state: Dict[str, Dict[str, float]] = {}

    def _add_diag(self, name: str, msg: str):
        if name:
            self._diag.setdefault(name, []).append(msg)

    def _reserve_domain_slot(self, url: str):
        host = urlparse(url).netloc.lower()
        if not host:
            return
        base_delay = float(getattr(config, "DOMAIN_MIN_INTERVAL", 0.35))
        with self._domain_lock:
            state = self._domain_state.setdefault(host, {"next_at": 0.0, "backoff": 1.0})
            now = time.time()
            wait = max(0.0, state["next_at"] - now)
            state["next_at"] = max(state["next_at"], now) + base_delay * state["backoff"]
        if wait > 0:
            time.sleep(wait)

    def _record_domain_result(self, url: str, status_code: Optional[int]):
        host = urlparse(url).netloc.lower()
        if not host:
            return
        with self._domain_lock:
            state = self._domain_state.setdefault(host, {"next_at": 0.0, "backoff": 1.0})
            if status_code in (403, 429) or (status_code is not None and status_code >= 500):
                state["backoff"] = min(
                    float(getattr(config, "DOMAIN_MAX_BACKOFF", 12.0)),
                    max(1.0, state["backoff"]) * float(getattr(config, "DOMAIN_ERROR_BACKOFF", 2.5)),
                )
                state["next_at"] = max(state["next_at"], time.time() + state["backoff"])
            elif status_code == 200:
                state["backoff"] = max(1.0, state["backoff"] * 0.7)

    # ── HTTP ──────────────────────────────────────────────────────────────
    def _http_get(self, url: str, *, referer: str = "", max_retries: int = 1,
                  use_cffi: bool = True, timeout: Optional[int] = None):
        """统一 HTTP GET。

        返回 response（含 403/404/410/429 —— 这四种重试无意义，直接交上层诊断）
        或 None（网络异常/重试耗尽）。
        """
        headers = {"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
        if referer:
            headers["Referer"] = referer
        attempt = 0
        last_exc = None
        while attempt <= max_retries:
            attempt += 1
            try:
                req_timeout = timeout or config.HTTP_TIMEOUT
                self._reserve_domain_slot(url)
                if use_cffi and _HAS_CURL_CFFI:
                    resp = cffi_requests.get(
                        url, headers=headers, timeout=req_timeout,
                        impersonate="chrome120", allow_redirects=True)
                else:
                    resp = _requests.get(
                        url, headers=headers, timeout=req_timeout,
                        allow_redirects=True)
                self._record_domain_result(url, resp.status_code)
                if resp.status_code in (200, 403, 404, 410, 429):
                    return resp
                last_exc = f"HTTP {resp.status_code}"   # 5xx 等可重试
            except Exception as e:  # noqa: BLE001
                last_exc = str(e)
            time.sleep(0.6)
        if last_exc:
            self._log(f"    _http_get 失败 {url}: {last_exc}")
        return None

    # ── RSS / Atom / JSON Feed ────────────────────────────────────────────
    def _parse_json_feed(self, name: str, feed_url: str, category: str,
                         text: str) -> List[NewsItem]:
        try:
            data = json.loads(text, strict=False)
        except ValueError:
            return []
        entries = data.get("items") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return []

        items: List[NewsItem] = []
        seen_entries = 0
        for entry in entries[:60]:
            if not isinstance(entry, dict):
                continue
            title = (entry.get("title") or "").strip()
            link = (entry.get("url") or entry.get("external_url") or entry.get("id") or "").strip()
            if not title or not link:
                continue
            seen_entries += 1
            raw_content = (entry.get("summary") or entry.get("content_text")
                           or entry.get("content_html") or title)
            content = BeautifulSoup(str(raw_content), "html.parser").get_text(" ", strip=True)
            dt = parse_iso_or_struct(entry.get("date_published") or entry.get("date_modified"))
            if not within_window(dt):
                continue
            items.append(NewsItem(
                title=title[:300], url=self._join_url(feed_url, link), source=name,
                category=category, content=(content or title)[:1500],
                published_at=dt.isoformat() if dt else None,
                scrape_strategy="json_feed"))
        if not items and seen_entries:
            self._add_diag(name, f"JSON Feed: {feed_url} 有 {seen_entries} 条但均在窗口外")
        return items

    def scrape_rss(self, name: str, rss_url: str, category: str,
                   timeout: Optional[int] = None,
                   max_retries: int = 1) -> List[NewsItem]:
        resp = self._http_get(rss_url, timeout=timeout, max_retries=max_retries)
        if resp is None:
            self._add_diag(name, f"RSS: {rss_url} 网络不可达/超时")
            return []
        if resp.status_code != 200:
            self._add_diag(name, f"RSS: {rss_url} HTTP {resp.status_code}")
            return []
        text_head = (resp.text or "").lstrip()[:1]
        if text_head == "{":
            return self._parse_json_feed(name, rss_url, category, resp.text)
        if not _HAS_FEEDPARSER:
            self._add_diag(name, "RSS: feedparser 未安装")
            return []
        try:
            feed = feedparser.parse(resp.content)
        except Exception as e:
            self._add_diag(name, f"RSS: {rss_url} 解析失败 {e}")
            return []
        if not feed.entries:
            self._add_diag(name, f"RSS: {rss_url} feed 为空(0 entries)")
            return []
        items: List[NewsItem] = []
        in_feed = 0
        for entry in feed.entries[:40]:
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            if not title or not link:
                continue
            in_feed += 1
            content = ""
            for key in ("summary", "description"):
                if entry.get(key):
                    content = BeautifulSoup(entry[key], "html.parser").get_text(" ", strip=True)
                    break
            if not content and entry.get("content"):
                try:
                    content = BeautifulSoup(entry["content"][0]["value"],
                                            "html.parser").get_text(" ", strip=True)
                except Exception:
                    content = ""
            # 标题兜底（HF Blog 等「仅标题」RSS，min_content 阈值过高会整源归零）
            if not content or len(content) < 20:
                content = title if len(title) > 10 else content
            if not content:
                continue
            pub = (entry.get("published_parsed") or entry.get("updated_parsed")
                   or entry.get("published") or entry.get("updated"))
            dt = parse_iso_or_struct(pub)
            if not within_window(dt):
                continue
            items.append(NewsItem(
                title=title[:300], url=link, source=name, category=category,
                content=content[:1500],
                published_at=dt.isoformat() if dt else None,
                scrape_strategy="rss"))
        if not items and in_feed:
            self._add_diag(name, f"RSS: {rss_url} feed 有 {in_feed} 条但均在窗口外")
        return items

    def _origin(self, url: str) -> str:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else url

    def _join_url(self, base_url: str, raw: str) -> str:
        """安全拼接 URL，修复 /https://example.com/path 这类站点脏链接。"""
        href = (raw or "").strip()
        if not href:
            return ""
        embedded_full = re.match(r"^https?://[^/]+/(https?://.+)$", href)
        if embedded_full:
            return embedded_full.group(1)
        if href.startswith(("http://", "https://")):
            return href
        if href.startswith("//"):
            scheme = urlparse(base_url).scheme or "https"
            return f"{scheme}:{href}"
        m = re.match(r"^/+((?:https?:)//.+)$", href)
        if m:
            fixed = m.group(1)
            return fixed.replace("https//", "https://").replace("http//", "http://")
        m = re.match(r"^/+((?:https?:)://.+)$", href)
        if m:
            return m.group(1)
        embedded = re.search(r"https?://[^\s\"'<>]+", href)
        if embedded and href[:embedded.start()].strip("/") == "":
            return embedded.group(0)
        return urljoin(base_url, href)

    def _candidate_feed_urls(self, base_url: str, html: str = "") -> List[str]:
        """从页面声明与常见路径发现 RSS/Atom/JSON Feed。"""
        declared: List[str] = []
        common: List[str] = []
        seen = set()

        def add(raw: str, bucket: List[str]):
            if not raw:
                return
            full = self._join_url(base_url, raw.strip())
            if not full.startswith(("http://", "https://")):
                return
            key = full.split("#", 1)[0]
            if key not in seen:
                seen.add(key)
                bucket.append(key)

        if html:
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup.find_all("link", href=True):
                rel = " ".join(tag.get("rel") or []).lower()
                typ = (tag.get("type") or "").lower()
                title = (tag.get("title") or "").lower()
                if ("alternate" in rel and any(h in typ for h in _FEED_MIME_HINTS)) or (
                        any(h in typ for h in ("rss", "atom", "feed+json"))
                        or any(h in title for h in ("rss", "atom", "feed"))):
                    add(tag["href"], declared)
            for a in soup.find_all("a", href=True):
                href = str(a.get("href") or "")
                text = a.get_text(" ", strip=True).lower()
                low = href.lower()
                if any(k in low for k in ("/feed", "rss", "atom.xml", "feed.xml")) or (
                        text in {"rss", "atom", "feed"}):
                    add(href, declared)

        origin = self._origin(base_url)
        parsed = urlparse(base_url)
        path = parsed.path.rstrip("/")
        for p in _COMMON_FEED_PATHS:
            add(urljoin(origin, p), common)
        if path and path != "/":
            for suffix in ("/feed", "/feed.xml", "/rss.xml"):
                add(urljoin(origin, path + suffix), common)
        return declared[:8] + common[:5 if declared else 4]

    def _candidate_hub_urls(self, base_url: str, html: str,
                            allowed_hosts: Optional[List[str]] = None) -> List[str]:
        """从官网导航发现更适合抓新闻的同域入口（blog/news/research 等）。"""
        if not html:
            return []
        base_path = urlparse(base_url).path.lower()
        if any(k in base_path for k in (
                "/blog", "/news", "/research", "/updates", "/article",
                "/articles", "/press", "/release", "/announcements")):
            return []
        soup = BeautifulSoup(html, "html.parser")
        scored: List[Tuple[int, str]] = []
        seen = {base_url.rstrip("/")}
        strong = (
            "blog", "news", "research", "updates", "announcements",
            "press", "release", "articles", "stories", "insights",
        )
        weak = ("ai", "product", "company", "about")
        for a in soup.find_all("a", href=True):
            raw = str(a.get("href") or "")
            text = a.get_text(" ", strip=True).lower()
            full = self._join_url(base_url, raw).split("#", 1)[0].rstrip("/")
            if not full.startswith(("http://", "https://")) or full in seen:
                continue
            parsed = urlparse(full)
            if not self._host_allowed(parsed.netloc, base_url, allowed_hosts):
                continue
            path_text = f"{parsed.path.lower()} {text}"
            if re.search(r"\.(?:png|jpg|jpeg|gif|svg|webp|pdf|zip|css|js)$", parsed.path.lower()):
                continue
            score = 0
            for idx, key in enumerate(strong):
                if key in path_text:
                    score += 30 - idx
            for key in weak:
                if key in path_text:
                    score += 3
            if score <= 0:
                continue
            seen.add(full)
            scored.append((score, full))
        scored.sort(key=lambda x: (-x[0], len(urlparse(x[1]).path)))
        limit = max(0, int(getattr(config, "LINKED_HUB_LIMIT", 3)))
        return [u for _, u in scored[:limit]]

    def scrape_linked_hubs(self, name: str, base_url: str, category: str,
                           allowed_hosts: Optional[List[str]] = None) -> List[NewsItem]:
        """产品/公司首页没有文章列表时，自动转到导航中的新闻/博客入口。"""
        resp = self._http_get(base_url, max_retries=0, timeout=12)
        if resp is None or resp.status_code != 200:
            return []
        hubs = self._candidate_hub_urls(base_url, resp.text[:300_000], allowed_hosts)
        tried = 0
        for hub_url in hubs:
            tried += 1
            items = self.scrape_discovered_feeds(name, hub_url, category, [])
            if not items:
                items = self.scrape_web(name, hub_url, category, allowed_hosts)
            if items:
                for it in items:
                    if it.scrape_strategy in ("web", "spa"):
                        it.scrape_strategy = "linked_hub"
                self._add_diag(name, f"导航入口: {hub_url} 窗口内 {len(items)} 条")
                return items
        if tried:
            self._add_diag(name, f"导航入口: 尝试 {tried} 个入口均无窗口内结果")
        return []

    def scrape_discovered_feeds(self, name: str, base_url: str, category: str,
                                known_urls: Optional[List[str]] = None) -> List[NewsItem]:
        """自动发现并尝试 RSS/Atom/JSON Feed，作为官方结构化入口优先级。"""
        known = {(u or "").rstrip("/") for u in (known_urls or []) if u}
        html = ""
        resp = self._http_get(base_url, max_retries=0, timeout=12)
        if resp is not None and resp.status_code == 200:
            html = resp.text[:300_000]
        feed_urls = [u for u in self._candidate_feed_urls(base_url, html)
                     if u.rstrip("/") not in known]
        tried = 0
        for feed_url in feed_urls:
            tried += 1
            items = self.scrape_rss(name, feed_url, category, timeout=8, max_retries=0)
            if items:
                for it in items:
                    if it.scrape_strategy == "rss":
                        it.scrape_strategy = "feed_discovery"
                self._add_diag(name, f"自动发现 Feed: {feed_url} 窗口内 {len(items)} 条")
                return items
        if tried:
            self._add_diag(name, f"自动发现 Feed: 尝试 {tried} 个入口均无窗口内结果")
        return []

    # ── 直接网页结构化兜底（不含 sitemap；sitemap 在调度层优先执行）────────────
    def scrape_web(self, name: str, url: str, category: str,
                   allowed_hosts: Optional[List[str]] = None) -> List[NewsItem]:
        resp = self._http_get(url)
        if resp is None:
            self._add_diag(name, "直抓: 网络不可达/超时")
            return []
        if resp.status_code != 200:
            self._add_diag(name, f"直抓: HTTP {resp.status_code}")
            return []
        html = resp.text
        items, undated = self._extract_articles_dom(html, url, name, category, allowed_hosts)
        json_items, json_undated = self._extract_via_structured_data(
            html, url, name, category, allowed_hosts)
        self._merge_extracted(items, undated, json_items, json_undated)

        if len(items) < 20:
            anchor_items, anchor_undated = self._extract_via_anchors(
                html, url, name, category, allowed_hosts)
            self._merge_extracted(items, undated, anchor_items, anchor_undated)

        if not items and not undated:
            self._add_diag(name, "直抓: 页面 200 但未提取到任何文章链接(SPA/结构不识别)")
            return []
        # 列表页提取不到日期的条目 → 抓文章详情页二段核验真实发布时间
        verified = self._verify_undated_items(undated)
        self._merge_extracted(items, [], verified, [])
        if not items:
            self._add_diag(
                name,
                f"直抓: 提取到 {len(undated)} 条候选, 详情页核验后均在窗口外")
        return items[:20]

    def _item_key(self, item: NewsItem) -> str:
        url = (item.url or "").split("?")[0].rstrip("/")
        if url:
            return f"url:{url.lower()}"
        title = re.sub(r"\s+", " ", (item.title or "").strip().lower())[:120]
        return f"title:{title}"

    def _merge_extracted(self, items: List[NewsItem], undated: List[NewsItem],
                         new_items: List[NewsItem], new_undated: List[NewsItem]):
        seen = {self._item_key(it) for it in items + undated}
        for it in new_items:
            key = self._item_key(it)
            if key not in seen:
                seen.add(key)
                items.append(it)
        for it in new_undated:
            key = self._item_key(it)
            if key not in seen:
                seen.add(key)
                undated.append(it)

    # 详情页日期核验：每源最多核验条数（控制耗时；列表页通常按时间倒序，
    # 取前若干条核验足以覆盖窗口内新文）
    _DETAIL_VERIFY_LIMIT = int(getattr(config, "DETAIL_VERIFY_LIMIT", 5))

    def _verify_undated_items(self, undated: List[NewsItem]) -> List[NewsItem]:
        """对列表页无日期的条目逐条抓详情页提取发布时间，仅保留窗口内的。

        PRD 红线：无法确认发布时间的条目一律丢弃（宁缺毋滥，绝不放行旧文）。
        """
        kept: List[NewsItem] = []
        for it in undated[:self._DETAIL_VERIFY_LIMIT]:
            dt, title, desc = self._fetch_article_meta(it.url)
            if dt is None or not within_window(dt):
                continue
            it.published_at = dt.isoformat()
            if title and len(title) >= 10:
                it.title = title[:300]
            if desc and len(desc) >= 20:
                it.content = desc[:1500]
            kept.append(it)
        return kept

    def _audit_items(self, name: str, items: List[NewsItem]) -> List[NewsItem]:
        """最终质量闸门：无可信时间、窗外、空标题/空链接、重复项一律不出源。

        这是抓取准确性的最后一道线，保证后续覆盖统计和 LLM 分析不会混入
        日期不可信或明显错误的条目。
        """
        out: List[NewsItem] = []
        seen = set()
        dropped_no_ts = dropped_window = dropped_bad = dropped_dup = 0
        for it in items:
            title = re.sub(r"\s+", " ", (it.title or "").strip())
            url = (it.url or "").strip()
            if len(title) < 4 or not url.startswith(("http://", "https://")):
                dropped_bad += 1
                continue
            dt = parse_iso_or_struct(it.published_at)
            if dt is None:
                dropped_no_ts += 1
                continue
            if not within_window(dt):
                dropped_window += 1
                continue
            key = self._item_key(it)
            if key in seen:
                dropped_dup += 1
                continue
            seen.add(key)
            it.title = title[:300]
            it.content = re.sub(r"\s+", " ", (it.content or title).strip())[:1500]
            it.published_at = dt.isoformat()
            out.append(it)
        parts = []
        if dropped_no_ts:
            parts.append(f"无可信时间 {dropped_no_ts}")
        if dropped_window:
            parts.append(f"窗外 {dropped_window}")
        if dropped_bad:
            parts.append(f"坏数据 {dropped_bad}")
        if dropped_dup:
            parts.append(f"重复 {dropped_dup}")
        if parts:
            self._add_diag(name, "最终质量闸门丢弃: " + " / ".join(parts))
        return out

    def _is_ai_relevant(self, item: NewsItem) -> bool:
        """High-recall relevance check for broad, non-AI-specific feeds."""
        text = re.sub(
            r"\s+", " ", f"{item.title or ''} {item.content or ''}"
        ).lower()
        if re.search(r"(?<![a-z])ai(?![a-z])", text):
            return True
        if re.search(r"(?<![a-z])(?:llm|aigc|agi|gpt(?:-\d+)?)(?![a-z])", text):
            return True
        return any(marker in text for marker in _AI_TEXT_MARKERS)

    def _filter_ai_relevant(self, name: str,
                            items: List[NewsItem]) -> List[NewsItem]:
        kept = [item for item in items if self._is_ai_relevant(item)]
        dropped = len(items) - len(kept)
        if dropped:
            self._add_diag(name, f"AI 相关性闸门丢弃: 非相关 {dropped} 条")
        return kept

    def _fetch_article_date(self, url: str) -> Optional[datetime]:
        dt, _, _ = self._fetch_article_meta(url)
        return dt

    def _meta_content(self, soup: BeautifulSoup, *names: str) -> str:
        wanted = {n.lower() for n in names}
        for tag in soup.find_all("meta"):
            key = (tag.get("property") or tag.get("name") or tag.get("itemprop") or "").lower()
            if key in wanted and tag.get("content"):
                return str(tag.get("content", "")).strip()
        return ""

    def _meta_date(self, soup: BeautifulSoup) -> Optional[datetime]:
        wanted = set(_META_DATE_KEYS)
        wanted.update(k.replace("_", "-") for k in _META_DATE_KEYS)
        wanted.update(k.replace("-", "_") for k in _META_DATE_KEYS)
        wanted.update(re.sub(r"[-_]", "", k) for k in _META_DATE_KEYS)
        for tag in soup.find_all("meta"):
            key = (tag.get("property") or tag.get("name") or tag.get("itemprop") or "").lower()
            raw = str(tag.get("content") or "").strip()
            if not raw:
                continue
            variants = {
                key, key.replace("_", "-"), key.replace("-", "_"),
                re.sub(r"[-_]", "", key),
            }
            if variants & wanted:
                dt = parse_iso_or_struct(raw)
                if dt:
                    return dt
        return None

    def _json_date(self, obj: Dict) -> Optional[datetime]:
        for key in _JSON_DATE_KEYS:
            if obj.get(key):
                dt = parse_iso_or_struct(obj.get(key))
                if dt:
                    return dt
        return None

    def _article_text(self, soup: BeautifulSoup) -> str:
        """提取文章正文摘要，避免 Web/SPA 路径只把标题送给 LLM。"""
        scopes = []
        for selector in (
                "[itemprop='articleBody']", "article", "main",
                "[class*='article']", "[class*='post']", "[class*='content']"):
            node = soup.select_one(selector)
            if node is not None:
                scopes.append(node)
        scopes.append(soup.body or soup)

        for scope in scopes:
            paras = [
                p.get_text(" ", strip=True)
                for p in scope.find_all(["p", "li"])
                if len(p.get_text(" ", strip=True)) >= 30
            ]
            text = " ".join(paras)
            if len(text) >= 80:
                return re.sub(r"\s+", " ", text)[:1800]
        return ""

    def _trafilatura_meta(self, html: str, url: str) -> Tuple[Optional[datetime], str, str]:
        """用 trafilatura 抽取非标准页面的标题/日期/正文，作为详情页兜底。"""
        if not _HAS_TRAFILATURA:
            return None, "", ""
        try:
            raw = trafilatura.extract(
                html, url=url, output_format="json", with_metadata=True,
                include_comments=False, favor_precision=True)
        except Exception:
            return None, "", ""
        if not raw:
            return None, "", ""
        try:
            data = json.loads(raw, strict=False)
        except ValueError:
            return None, "", ""
        title = str(data.get("title") or "").strip()
        text = str(data.get("text") or data.get("description") or "").strip()
        dt = parse_iso_or_struct(data.get("date") or data.get("pubdate"))
        return dt, title, re.sub(r"\s+", " ", text)[:1800]

    def _fetch_article_meta(self, url: str) -> Tuple[Optional[datetime], str, str]:
        """抓文章详情页，按优先级提取发布时间：
        meta(article:published_time 等) → JSON-LD datePublished → <time> → URL 路径日期。
        """
        resp = self._http_get(
            url, max_retries=0,
            timeout=int(getattr(config, "ARTICLE_META_TIMEOUT", 8)))
        if resp is None or resp.status_code != 200:
            return None, "", ""
        html = resp.text[:200_000]
        soup = BeautifulSoup(html, "html.parser")
        title = (self._meta_content(soup, "og:title", "twitter:title")
                 or (soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else "")
                 or (soup.title.get_text(" ", strip=True) if soup.title else ""))
        desc = self._meta_content(
            soup, "og:description", "twitter:description", "description")
        body_text = self._article_text(soup)
        if len(body_text) > len(desc or ""):
            desc = body_text
        dt: Optional[datetime] = None
        tra_dt, tra_title, tra_desc = self._trafilatura_meta(html, url)
        if tra_title and not title:
            title = tra_title
        if len(tra_desc or "") > len(desc or ""):
            desc = tra_desc
        # 1. meta 标签（property/name/itemprop 多种写法）
        dt = self._meta_date(soup)
        if dt:
            return dt, title, desc
        if tra_dt:
            return tra_dt, title, desc
        # 2. JSON-LD / __NEXT_DATA__ / application/json 里的发布时间
        for script in soup.find_all("script"):
            text = script.string or script.get_text("", strip=False)
            if not text or "{" not in text:
                continue
            stype = (script.get("type") or "").lower()
            sid = (script.get("id") or "").lower()
            if "json" in stype or "ld+json" in stype or sid == "__next_data__":
                try:
                    payload = json.loads(text.strip(), strict=False)
                except ValueError:
                    payload = None
                if payload is not None:
                    for obj in self._iter_json_dicts(payload):
                        dt = self._json_date(obj)
                        if dt:
                            if not title:
                                title = self._json_text(obj, ("headline", "title", "name"))
                            if not desc:
                                desc = self._json_text(obj, ("description", "summary", "excerpt"))
                            return dt, title, desc
            for key in _JSON_DATE_KEYS:
                m = re.search(rf'"{re.escape(key)}"\s*:\s*"([^"]+)"', text)
                if m:
                    dt = parse_iso_or_struct(m.group(1))
                    if dt:
                        return dt, title, desc
        # 3. <time datetime="..."> / <time>文本 / 常见日期类节点
        for t in soup.find_all("time"):
            dt = parse_iso_or_struct(t.get("datetime") or t.get("content") or t.get_text(" ", strip=True))
            if dt:
                return dt, title, desc
        for node in soup.select("[class*='date'], [class*='time'], [itemprop*='date']"):
            dt = parse_iso_or_struct(node.get("datetime") or node.get("content")
                                     or node.get_text(" ", strip=True))
            if dt:
                return dt, title, desc
        # 4. URL 路径里的日期（/2026/06/09/ 等）
        m = re.search(r"/(\d{4})/(\d{1,2})(?:/(\d{1,2}))?/", url)
        if m:
            return _build_dt(int(m.group(1)), int(m.group(2)), int(m.group(3) or 15)), title, desc
        # 5. 正文首屏文本兜底
        text = soup.get_text(" ", strip=True)
        return parse_date_from_text(text[:3000]), title, desc

    def _iter_json_dicts(self, value):
        if isinstance(value, dict):
            yield value
            for v in value.values():
                yield from self._iter_json_dicts(v)
        elif isinstance(value, list):
            for v in value:
                yield from self._iter_json_dicts(v)

    def _json_text(self, obj: Dict, keys: Tuple[str, ...]) -> str:
        for key in keys:
            v = obj.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, dict):
                nested = self._json_text(v, ("name", "title", "headline", "@id", "url"))
                if nested:
                    return nested
        return ""

    def _json_url(self, obj: Dict, base_url: str) -> str:
        raw = obj.get("url") or obj.get("permalink") or obj.get("link")
        if isinstance(raw, dict):
            raw = raw.get("@id") or raw.get("url")
        if not raw:
            page = obj.get("mainEntityOfPage")
            if isinstance(page, dict):
                raw = page.get("@id") or page.get("url")
            elif isinstance(page, str):
                raw = page
        if not raw:
            slug = obj.get("slug") or obj.get("path")
            if isinstance(slug, str) and slug.strip():
                raw = slug
        if not isinstance(raw, str) or not raw.strip():
            return ""
        return self._join_url(base_url, raw.strip())

    def _host_allowed(self, host: str, base_url: str,
                      allowed_hosts: Optional[List[str]] = None) -> bool:
        host = (host or "").replace("www.", "").lower()
        base_host = urlparse(base_url).netloc.replace("www.", "").lower()
        allowed = {h.replace("www.", "").lower() for h in (allowed_hosts or [])}
        return host == base_host or host in allowed

    def _looks_like_article_url(self, full: str, base_url: str,
                                require_hint: bool = False,
                                allowed_hosts: Optional[List[str]] = None) -> bool:
        if not full:
            return False
        parsed = urlparse(full)
        if not self._host_allowed(parsed.netloc, base_url, allowed_hosts):
            return False
        path = parsed.path.lower()
        if not path or path == "/":
            return False
        if re.search(r"\.(?:png|jpg|jpeg|gif|svg|webp|pdf|zip|css|js)$", path):
            return False
        segments = [p for p in path.split("/") if p]
        if segments and segments[-1].lower() in _LISTING_SLUGS:
            return False
        has_hint = any(h in path for h in _SPA_URL_HINTS) or bool(
            re.search(r"/20\d{2}[/-]", path))
        if require_hint and not has_hint:
            return False
        if has_hint or len(segments) >= 2:
            return True
        return (not require_hint and len(segments) == 1
                and len(segments[0]) >= 8
                and segments[0].lower() not in _ANCHOR_BLACKLIST)

    def _is_generic_page_title(self, title: str) -> bool:
        clean = re.sub(r"\s+", " ", (title or "")).strip().lower()
        if not clean:
            return True
        primary = re.split(r"\s*[|｜—–]\s*", clean, maxsplit=1)[0].strip()
        return primary in _GENERIC_PAGE_TITLES

    def _json_obj_to_item(self, obj: Dict, base_url: str, name: str,
                          category: str,
                          allowed_hosts: Optional[List[str]] = None) -> Optional[NewsItem]:
        title = self._json_text(obj, ("headline", "title", "name", "seoTitle"))
        if not title or len(title) < 12:
            return None
        full = self._json_url(obj, base_url)
        if not self._looks_like_article_url(full, base_url, allowed_hosts=allowed_hosts):
            return None
        typ = obj.get("@type") or obj.get("type") or ""
        if isinstance(typ, list):
            typ = " ".join(str(x) for x in typ)
        typ = str(typ).lower()
        articleish = any(k in typ for k in ("article", "blog", "post", "news"))
        if not articleish and not any(k in obj for k in (
                "datePublished", "dateCreated", "publishedAt", "publishDate")):
            return None
        date_raw = None
        for key in _JSON_DATE_KEYS + ("dateModified", "updatedAt", "createdAt"):
            if obj.get(key):
                date_raw = obj.get(key)
                break
        dt = parse_iso_or_struct(date_raw) or parse_date_from_text(title)
        desc = self._json_text(obj, (
            "description", "summary", "excerpt", "articleBody", "text", "abstract"))
        return NewsItem(
            title=title[:300], url=full, source=name, category=category,
            content=(desc or title)[:1500],
            published_at=dt.isoformat() if dt else None,
            scrape_strategy="jsonld")

    def _extract_via_structured_data(self, html: str, base_url: str, name: str,
                                     category: str,
                                     allowed_hosts: Optional[List[str]] = None
                                     ) -> Tuple[List[NewsItem], List[NewsItem]]:
        """从 JSON-LD 和常见 SPA hydration JSON 中提取文章候选。"""
        soup = BeautifulSoup(html, "html.parser")
        payloads = []
        for script in soup.find_all("script"):
            text = script.string or script.get_text("", strip=False)
            if not text or "{" not in text or len(text) > 1_500_000:
                continue
            stype = (script.get("type") or "").lower()
            sid = (script.get("id") or "").lower()
            is_json_script = (
                "ld+json" in stype
                or stype in {"application/json", "application/ld+json"}
                or sid in {"__next_data__", "__nuxt_data__", "__apollo_state__"}
            )
            if not is_json_script:
                continue
            try:
                payloads.append(json.loads(text.strip(), strict=False))
            except ValueError:
                continue

        items: List[NewsItem] = []
        undated: List[NewsItem] = []
        seen = set()
        for payload in payloads:
            for obj in self._iter_json_dicts(payload):
                item = self._json_obj_to_item(obj, base_url, name, category, allowed_hosts)
                if item is None:
                    continue
                key = self._item_key(item)
                if key in seen:
                    continue
                seen.add(key)
                dt = parse_iso_or_struct(item.published_at)
                if dt is None:
                    undated.append(item)
                elif within_window(dt):
                    items.append(item)
                if len(items) >= 20:
                    break
            if len(items) >= 20:
                break
        if items:
            self._add_diag(name, f"结构化数据: 提取到窗口内 {len(items)} 条")
        return items, undated

    def _extract_articles_dom(self, html: str, base_url: str, name: str,
                              category: str,
                              allowed_hosts: Optional[List[str]] = None
                              ) -> Tuple[List[NewsItem], List[NewsItem]]:
        """传统 <article>/<h1-3><a> 结构提取（RSS 友好型站点）。

        返回 (有日期且在窗口内的条目, 无日期待详情页核验的条目)。
        有日期但窗外 → 直接丢弃。
        """
        soup = BeautifulSoup(html, "html.parser")
        items: List[NewsItem] = []
        undated: List[NewsItem] = []
        seen = set()
        candidates = soup.select("article") or soup.select("h1 a, h2 a, h3 a")
        for node in candidates[:60]:
            a = node if node.name == "a" else (node.find("a") if hasattr(node, "find") else None)
            if a is None:
                continue
            title = a.get_text(" ", strip=True)
            href = a.get("href", "")
            if not title or len(title) < 15 or not href:
                continue
            full = self._join_url(base_url, href)
            if not self._looks_like_article_url(full, base_url, allowed_hosts=allowed_hosts):
                continue
            if full in seen:
                continue
            seen.add(full)
            dt = self._date_for_node(node)
            item = NewsItem(
                title=title[:300], url=full, source=name, category=category,
                content=title[:1500],
                published_at=dt.isoformat() if dt else None,
                scrape_strategy="web")
            if dt is None:
                undated.append(item)        # 待详情页二段核验
            elif within_window(dt):
                items.append(item)
            if len(items) >= 20:
                break
        return items, undated

    def _extract_via_anchors(self, html: str, base_url: str, name: str,
                             category: str,
                             allowed_hosts: Optional[List[str]] = None
                             ) -> Tuple[List[NewsItem], List[NewsItem]]:
        """SPA 兜底：扫描全部 <a href>，按启发式筛文章链接 + 锚文本日期过滤。

        返回 (有日期且在窗口内的条目, 无日期待详情页核验的条目)。
        """
        soup = BeautifulSoup(html, "html.parser")
        items: List[NewsItem] = []
        undated: List[NewsItem] = []
        seen = set()
        for a in soup.find_all("a", href=True):
            title = a.get_text(" ", strip=True)
            if len(title) < 15:
                continue
            if title.strip().lower() in _ANCHOR_BLACKLIST:
                continue
            full = self._join_url(base_url, a["href"])
            if not self._looks_like_article_url(
                    full, base_url, require_hint=True, allowed_hosts=allowed_hosts):
                continue
            if full in seen:
                continue
            seen.add(full)
            dt = self._date_for_node(a) or parse_date_from_text(title)
            item = NewsItem(
                title=title[:300], url=full, source=name, category=category,
                content=title[:1500],
                published_at=dt.isoformat() if dt else None,
                scrape_strategy="spa")
            if dt is None:
                undated.append(item)        # 待详情页二段核验
            elif within_window(dt):
                items.append(item)
            if len(items) >= 20:
                break
        return items, undated

    def _date_for_node(self, node) -> Optional[datetime]:
        """从锚/文章节点向外找日期：<time> → [class*=date] → 锚文本 → 兄弟节点。"""
        if node is None or not hasattr(node, "find_parent"):
            return None
        # 1. 自身/父级中的 <time datetime=...>
        for scope in (node, node.parent if node.parent else None):
            if scope is None or not hasattr(scope, "find"):
                continue
            t = scope.find("time")
            if t is not None:
                dt = parse_iso_or_struct(t.get("datetime") or t.get_text(strip=True))
                if dt:
                    return dt
        # 2. 向上 3 层父级里的日期类元素
        parent = node.find_parent()
        for _ in range(3):
            if parent is None:
                break
            dnode = parent.select_one("[class*='date'], .posted-on, .publish-date, .meta time")
            if dnode is not None:
                dt = (parse_iso_or_struct(dnode.get("datetime"))
                      or parse_date_from_text(dnode.get_text(" ", strip=True)))
                if dt:
                    return dt
            parent = parent.find_parent()
        # 3. 锚文本本身（SPA 常把日期粘在标题前后）
        dt = parse_date_from_text(node.get_text(" ", strip=True))
        if dt:
            return dt
        # 4. 前后兄弟文本
        for sib in (node.previous_sibling, node.next_sibling):
            if sib is not None and hasattr(sib, "get_text"):
                dt = parse_date_from_text(sib.get_text(" ", strip=True))
                if dt:
                    return dt
        return None

    def _sitemap_roots(self, base_url: str) -> List[str]:
        parsed = urlparse(base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        roots = [urljoin(origin, p) for p in _COMMON_SITEMAP_PATHS]
        robots = self._http_get(urljoin(origin, "/robots.txt"), max_retries=0, timeout=8)
        if robots is not None and robots.status_code == 200:
            for line in robots.text.splitlines():
                if line.lower().startswith("sitemap:"):
                    roots.insert(0, line.split(":", 1)[1].strip())
        out, seen = [], set()
        for u in roots:
            if u and u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _xml_bytes(self, resp, url: str) -> bytes:
        data = resp.content or b""
        enc = (getattr(resp, "headers", {}) or {}).get("Content-Encoding", "")
        if url.lower().endswith(".gz") or "gzip" in enc.lower():
            try:
                return gzip.decompress(data)
            except OSError:
                return data
        return data

    def _tag_text(self, node, *names: str) -> str:
        wanted = {n.lower() for n in names}
        for tag in node.find_all(True):
            name = (tag.name or "").lower()
            local = name.rsplit(":", 1)[-1]
            if name in wanted or local in wanted:
                return tag.get_text(strip=True)
        return ""

    def _title_from_url(self, url: str) -> str:
        path = urlparse(url).path.rstrip("/")
        slug = path.rsplit("/", 1)[-1] if path else ""
        slug = re.sub(r"[-_]+", " ", slug).strip()
        return slug.title() if slug else url

    def scrape_sitemap(self, name: str, base_url: str, category: str,
                       allowed_hosts: Optional[List[str]] = None,
                       path_hints: Optional[List[str]] = None) -> List[NewsItem]:
        """sitemap fallback with strict source and publication-date boundaries.

        ``lastmod`` only prioritizes candidates. It is not accepted as the
        publication time because CMSs routinely refresh it for old pages.
        """
        queue = self._sitemap_roots(base_url)
        seen_maps, candidates = set(), []
        normalized_hints = [
            hint.lower() for hint in (path_hints or []) if str(hint).strip()
        ]
        while queue and len(seen_maps) < 10 and len(candidates) < 60:
            sm_url = queue.pop(0)
            if sm_url in seen_maps:
                continue
            seen_maps.add(sm_url)
            resp = self._http_get(sm_url, max_retries=0, timeout=10)
            if resp is None or resp.status_code != 200:
                continue
            soup = BeautifulSoup(self._xml_bytes(resp, sm_url), "xml")

            for sm in soup.find_all("sitemap"):
                loc = sm.find("loc")
                loc_text = loc.get_text(strip=True) if loc else ""
                low = loc_text.lower()
                if loc_text and any(k in low for k in (
                        "post", "blog", "news", "article", "story", "sitemap",
                        ".xml", ".xml.gz")):
                    queue.append(loc_text)

            for node in soup.find_all("url"):
                loc = node.find("loc")
                loc_text = loc.get_text(strip=True) if loc else ""
                if not self._looks_like_article_url(
                        loc_text, base_url, require_hint=True,
                        allowed_hosts=allowed_hosts):
                    continue
                loc_path = urlparse(loc_text).path.lower()
                if normalized_hints and not any(
                        hint in loc_path for hint in normalized_hints):
                    continue
                news_dt = parse_iso_or_struct(self._tag_text(node, "publication_date"))
                lastmod_node = node.find("lastmod")
                lastmod_dt = parse_iso_or_struct(
                    lastmod_node.get_text(strip=True) if lastmod_node else None)
                if news_dt is not None and not within_window(news_dt):
                    continue
                # Old lastmod values can be discarded early; recent lastmod is
                # only a sorting signal and still requires detail-page proof.
                if news_dt is None and lastmod_dt is not None:
                    start, _ = get_strict_window()
                    if lastmod_dt < start - timedelta(days=2):
                        continue
                candidates.append((news_dt, lastmod_dt, loc_text))
                if len(candidates) >= 60:
                    break

        if not candidates:
            if seen_maps:
                self._add_diag(name, f"sitemap: 检查 {len(seen_maps)} 个 sitemap，窗口内 0 条")
            return []

        floor_dt = datetime(1970, 1, 1, tzinfo=CST)
        candidates.sort(key=lambda x: x[0] or x[1] or floor_dt, reverse=True)
        items: List[NewsItem] = []
        seen = set()
        limit = int(getattr(config, "SITEMAP_DETAIL_LIMIT", 12))
        for publication_dt, _, loc in candidates[:limit]:
            if loc in seen:
                continue
            seen.add(loc)
            meta_dt, title, desc = self._fetch_article_meta(loc)
            dt = meta_dt or publication_dt
            if not within_window(dt):
                continue
            title = title or self._title_from_url(loc)
            if len(title.strip()) < 10 or self._is_generic_page_title(title):
                continue
            items.append(NewsItem(
                title=title[:300], url=loc, source=name, category=category,
                content=(desc or title)[:1500],
                published_at=dt.isoformat() if dt else None,
                scrape_strategy="sitemap"))
        if items:
            self._add_diag(name, f"sitemap: 窗口内补抓 {len(items)} 条")
        else:
            self._add_diag(name, f"sitemap: 找到 {len(candidates)} 条候选但详情页核验后为空")
        return items

    # ── Sogou 微信搜索兜底 ──────────────────────────────────────────────────
    def scrape_sogou(self, query: str, name: str, category: str,
                     top_n: int = 10) -> List[NewsItem]:
        """Sogou 微信搜索兜底。

        - 必带 Referer: https://weixin.sogou.com/ （否则返回空页）
        - source 字段取 .s-p 中的公众号名（如「机器之心」），不是搜索关键词
        - 严格时间窗：Sogou 默认返回 2017-2019 旧文。有时间戳且窗外 → 丢弃；
          **无时间戳 → 同样丢弃**（PRD 硬约束：无法确认时间的条目不放行）
        - AI 垂直媒体关键词直接用媒体名，不加「AI 大模型」后缀（会命中率归零）
        """
        url = f"https://weixin.sogou.com/weixin?type=2&query={quote(query)}"
        resp = self._http_get(url, referer="https://weixin.sogou.com/")
        if resp is None:
            self._add_diag(name, "Sogou: 网络不可达/超时")
            return []
        if resp.status_code != 200:
            self._add_diag(name, f"Sogou: HTTP {resp.status_code}(可能触发验证码)")
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        items: List[NewsItem] = []
        boxes = soup.select(".news-box .news-list li, .news-list li")
        if not boxes:
            self._add_diag(name, "Sogou: 搜索结果为空/页面结构变化")
            return []
        dropped_old = 0
        for box in boxes:
            a = box.select_one("h3 a") or box.select_one("a")
            if a is None:
                continue
            title = a.get_text(" ", strip=True)
            href = a.get("href", "")
            if not title or not href:
                continue
            full = urljoin("https://weixin.sogou.com", href)
            account = name
            s_p = box.select_one(".s-p")
            if s_p is not None:
                acc = s_p.select_one("a.account") or s_p.select_one("a")
                if acc is not None and acc.get_text(strip=True):
                    account = acc.get_text(strip=True)
            # 时间戳：timeConvert('unix_ts')
            dt = None
            if s_p is not None:
                m = re.search(r"timeConvert\(['\"](\d+)['\"]\)", str(s_p))
                if m:
                    try:
                        dt = datetime.fromtimestamp(int(m.group(1)), tz=CST)
                    except (ValueError, OverflowError, OSError):
                        dt = None
            # 严格窗口：无时间戳或窗外 → 丢弃（within_window(None) 已为 False）
            if not within_window(dt):
                dropped_old += 1
                continue
            summary = ""
            stxt = box.select_one(".txt-info")
            if stxt is not None:
                summary = stxt.get_text(" ", strip=True)
            items.append(NewsItem(
                title=title[:300], url=full, source=account, category=category,
                content=(summary or title)[:1500],
                published_at=dt.isoformat() if dt else None,
                scrape_strategy="sogou"))
        # 有时间戳的优先按时间倒序，取 top_n
        items.sort(key=lambda x: x.published_at or "", reverse=True)
        if not items and dropped_old:
            self._add_diag(name, f"Sogou: {dropped_old} 条结果均在窗口外/无时间戳(索引延迟)")
        return items[:top_n]

    # ── Google News RSS 兜底（国内媒体官网不可达时的严格校验索引）────────────
    def _google_news_matches_source(self, entry, aliases: List[str],
                                    domains: List[str]) -> bool:
        src = entry.get("source") or {}
        src_title = ""
        src_href = ""
        if isinstance(src, dict):
            src_title = str(src.get("title") or "")
            src_href = str(src.get("href") or "")
        title = str(entry.get("title") or "")
        summary = BeautifulSoup(
            str(entry.get("summary") or ""), "html.parser").get_text(" ", strip=True)
        hay = f"{src_title} {title} {summary}".lower()
        if any(a and a.lower() in hay for a in aliases):
            return True
        host = urlparse(src_href).netloc.replace("www.", "").lower()
        return any(d and (host == d.lower() or host.endswith("." + d.lower()))
                   for d in domains)

    def _strip_google_news_title(self, title: str, aliases: List[str]) -> str:
        clean = re.sub(r"\s+", " ", (title or "")).strip()
        for alias in sorted([a for a in aliases if a], key=len, reverse=True):
            clean = re.sub(rf"\s+[-|｜]\s*{re.escape(alias)}\s*$", "", clean)
        return clean.strip() or title

    def _google_news_item_relevant(self, title: str, summary: str) -> bool:
        text = f"{title} {summary}".lower()
        spam_markers = (
            "热门标签", "谷歌留痕", "蜘蛛池", "电报", "免费试用",
            "霸屏", "seo", ".kpr", ".yrd", ".zsk", ".arc",
        )
        if any(m.lower() in text for m in spam_markers):
            return False
        ai_markers = (
            " ai", "ai ", "ai+", "ai-", "人工智能", "大模型", "模型",
            "智能体", "agent", "openai", "anthropic", "claude", "gpt",
            "gemini", "deepseek", "智谱", "glm", "豆包", "千问",
            "通义", "kimi", "可灵", "sora", "机器人", "具身",
            "推理", "算力", "芯片", "英伟达", "nvidia", "llm",
        )
        return any(m in text for m in ai_markers)

    def scrape_google_news(self, queries: List[str], name: str, category: str,
                           aliases: Optional[List[str]] = None,
                           domains: Optional[List[str]] = None) -> List[NewsItem]:
        """Google News RSS 兜底。

        只接收 source.title/source.href/标题摘要能匹配目标媒体的条目；
        仍然执行严格窗口过滤，避免把聚合搜索结果当作当天原文。
        """
        if not _HAS_FEEDPARSER:
            self._add_diag(name, "Google News: feedparser 未安装")
            return []
        aliases = aliases or [name]
        domains = domains or []
        items: List[NewsItem] = []
        seen = set()
        checked = matched = dropped_window = 0
        timeout = int(getattr(config, "GOOGLE_NEWS_TIMEOUT", 12))
        max_entries = int(getattr(config, "GOOGLE_NEWS_MAX_ENTRIES", 60))
        for query in [q for q in queries if q]:
            url = ("https://news.google.com/rss/search?q="
                   f"{quote(query)}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans")
            resp = self._http_get(url, timeout=timeout, max_retries=0)
            if resp is None:
                self._add_diag(name, f"Google News: {query} 网络不可达/超时")
                continue
            if resp.status_code != 200:
                self._add_diag(name, f"Google News: {query} HTTP {resp.status_code}")
                continue
            try:
                feed = feedparser.parse(resp.content)
            except Exception as e:  # noqa: BLE001
                self._add_diag(name, f"Google News: {query} 解析失败 {e}")
                continue
            for entry in (feed.entries or [])[:max_entries]:
                checked += 1
                if not self._google_news_matches_source(entry, aliases, domains):
                    continue
                matched += 1
                dt = parse_iso_or_struct(
                    entry.get("published_parsed") or entry.get("updated_parsed")
                    or entry.get("published") or entry.get("updated"))
                if not within_window(dt):
                    dropped_window += 1
                    continue
                raw_title = str(entry.get("title") or "")
                title = self._strip_google_news_title(raw_title, aliases)
                link = str(entry.get("link") or "")
                if not title or not link:
                    continue
                key = link.split("?")[0].rstrip("/") or title
                if key in seen:
                    continue
                summary = BeautifulSoup(
                    str(entry.get("summary") or ""), "html.parser").get_text(" ", strip=True)
                if not self._google_news_item_relevant(title, summary):
                    continue
                seen.add(key)
                items.append(NewsItem(
                    title=title[:300], url=link, source=name, category=category,
                    content=(summary or title)[:1500],
                    published_at=dt.isoformat() if dt else None,
                    scrape_strategy="google_news"))
                if len(items) >= 20:
                    break
            if items:
                break
        if items:
            self._add_diag(name, f"Google News: 匹配窗口内 {len(items)} 条")
        elif checked:
            self._add_diag(
                name,
                f"Google News: 检查 {checked} 条，匹配媒体 {matched} 条，"
                f"窗口外 {dropped_window} 条")
        return items

    # ── AnySearch 搜索聚合源 ───────────────────────────────────────────────
    def _anysearch_post(self, endpoint: str, payload: Dict,
                        headers: Dict[str, str]) -> Optional[Dict]:
        try:
            if _HAS_CURL_CFFI:
                resp = cffi_requests.post(
                    endpoint, json=payload, headers=headers,
                    timeout=config.HTTP_TIMEOUT + 10,
                    impersonate="chrome120")
            else:
                resp = _requests.post(
                    endpoint, json=payload, headers=headers,
                    timeout=config.HTTP_TIMEOUT + 10)
        except Exception as e:  # noqa: BLE001
            self._log(f"    AnySearch 请求失败: {e}")
            return None
        if resp.status_code != 200:
            return {"code": -1, "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        try:
            return resp.json()
        except ValueError:
            return {"code": -1, "message": "返回不是 JSON"}

    def _anysearch_result_date(self, result: Dict) -> Optional[datetime]:
        for key in (
                "published_at", "publishedAt", "published_time",
                "publication_date", "date", "datetime", "time"):
            dt = parse_iso_or_struct(result.get(key))
            if dt:
                return dt
        text = " ".join(
            str(result.get(key) or "")
            for key in ("title", "snippet", "content", "description")
        )[:3000]
        return parse_iso_or_struct(text) or parse_relative_age(text)

    def scrape_anysearch(self, source: Dict) -> List[NewsItem]:
        """Call AnySearch API and return window-verified AI news candidates."""
        name = source.get("name", "AnySearch")
        category = source.get("category", "搜索聚合")
        endpoint = source.get("anysearch_endpoint") or config.ANYSEARCH_API_ENDPOINT
        max_results = int(source.get("max_results") or 6)
        queries = [q for q in source.get("anysearch_queries", []) if q]
        if not queries:
            queries = ["latest AI model release AI agent news"]

        api_key = (source.get("api_key") or os.getenv("ANYSEARCH_API_KEY") or "").strip()
        headers = {
            "User-Agent": _UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        start, end = get_strict_window()
        seen = set()
        items: List[NewsItem] = []
        checked = dropped_window = missing_date = 0
        detail_limit = int(getattr(config, "ANYSEARCH_DETAIL_VERIFY_LIMIT", 8))
        detail_used = 0
        for base_query in queries[:5]:
            query = (
                f"{base_query} after:{start.date().isoformat()} "
                f"before:{(end + timedelta(days=1)).date().isoformat()}"
            )
            payload = {"query": query, "max_results": max(1, min(max_results, 10))}
            data = self._anysearch_post(endpoint, payload, headers)
            if not data:
                self._add_diag(name, f"AnySearch: {base_query} 网络不可达/超时")
                continue
            if data.get("code") != 0:
                self._add_diag(name, f"AnySearch: {base_query} {data.get('message') or '调用失败'}")
                continue
            results = ((data.get("data") or {}).get("results") or [])[:max_results]
            for result in results:
                if not isinstance(result, dict):
                    continue
                checked += 1
                title = re.sub(r"\s+", " ", str(result.get("title") or "").strip())
                url = str(result.get("url") or "").strip()
                if not title or not url.startswith(("http://", "https://")):
                    continue
                key = url.split("?", 1)[0].rstrip("/").lower() or title.lower()
                if key in seen:
                    continue
                raw_content = str(result.get("content") or result.get("snippet") or title)[:5000]
                content = re.sub(r"\s+", " ", raw_content.strip())
                dt = self._anysearch_result_date(result)
                if dt is None and detail_used < detail_limit:
                    detail_used += 1
                    fetched_dt, fetched_title, fetched_desc = self._fetch_article_meta(url)
                    dt = fetched_dt
                    if fetched_title and len(fetched_title) >= 10:
                        title = fetched_title
                    if fetched_desc and len(fetched_desc) > len(content):
                        content = fetched_desc
                if dt is None:
                    missing_date += 1
                    continue
                if not within_window(dt):
                    dropped_window += 1
                    continue
                seen.add(key)
                items.append(NewsItem(
                    title=title[:300], url=url, source=name, category=category,
                    content=(content or title)[:1500],
                    published_at=dt.isoformat(),
                    scrape_strategy="anysearch"))
        self._add_diag(
            name,
            f"AnySearch: 检查 {checked} 条，窗口内 {len(items)} 条，"
            f"无可信时间 {missing_date} 条，窗口外 {dropped_window} 条")
        return items

    # ── Tavily 搜索聚合源 ─────────────────────────────────────────────────
    def _tavily_post(self, endpoint: str, payload: Dict,
                     headers: Dict[str, str]) -> Optional[Dict]:
        try:
            if _HAS_CURL_CFFI:
                resp = cffi_requests.post(
                    endpoint, json=payload, headers=headers,
                    timeout=config.HTTP_TIMEOUT + 10,
                    impersonate="chrome120")
            else:
                resp = _requests.post(
                    endpoint, json=payload, headers=headers,
                    timeout=config.HTTP_TIMEOUT + 10)
        except Exception as e:  # noqa: BLE001
            self._log(f"    Tavily 请求失败: {e}")
            return None
        if resp.status_code != 200:
            try:
                detail = resp.json().get("detail")
            except (AttributeError, ValueError):
                detail = resp.text[:200]
            return {"_error": f"HTTP {resp.status_code}: {detail}"}
        try:
            return resp.json()
        except ValueError:
            return {"_error": "返回不是 JSON"}

    def _tavily_result_date(self, result: Dict) -> Optional[datetime]:
        for key in (
                "published_date", "publishedDate", "published_at",
                "publishedAt", "publication_date", "date", "datetime", "time"):
            dt = parse_iso_or_struct(result.get(key))
            if dt:
                return dt
        text = " ".join(
            str(result.get(key) or "")
            for key in ("title", "content", "raw_content")
        )[:5000]
        return parse_iso_or_struct(text) or parse_relative_age(text)

    def scrape_tavily(self, source: Dict) -> List[NewsItem]:
        """Call Tavily Search and retain only strictly window-verified news."""
        name = source.get("name", "Tavily")
        category = source.get("category", "搜索聚合")
        endpoint = source.get("tavily_endpoint") or config.TAVILY_API_ENDPOINT
        max_results = int(source.get("max_results") or 6)
        queries = [q for q in source.get("tavily_queries", []) if q]
        if not queries:
            queries = ["latest AI model releases and major AI industry news"]

        api_key = (source.get("api_key") or os.getenv("TAVILY_API_KEY") or "").strip()
        if not api_key:
            self._add_diag(
                name,
                "Tavily: 未配置 API Key，请在信息源管理页保存或设置 TAVILY_API_KEY")
            return []
        headers = {
            "User-Agent": _UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        start, end = get_strict_window()
        seen = set()
        items: List[NewsItem] = []
        checked = dropped_window = missing_date = 0
        detail_limit = int(getattr(config, "TAVILY_DETAIL_VERIFY_LIMIT", 8))
        detail_used = 0
        for query in queries[:5]:
            payload = {
                "query": query,
                "topic": "news",
                "search_depth": "basic",
                "max_results": max(1, min(max_results, 20)),
                "start_date": start.date().isoformat(),
                # Tavily end_date is an exclusive date boundary. The product's
                # precise intraday boundary is enforced again by within_window.
                "end_date": (end + timedelta(days=1)).date().isoformat(),
                "include_answer": False,
                "include_raw_content": False,
            }
            data = self._tavily_post(endpoint, payload, headers)
            if not data:
                self._add_diag(name, f"Tavily: {query} 网络不可达/超时")
                continue
            if data.get("_error"):
                self._add_diag(name, f"Tavily: {query} {data['_error']}")
                continue
            results = (data.get("results") or [])[:max_results]
            for result in results:
                if not isinstance(result, dict):
                    continue
                checked += 1
                title = re.sub(r"\s+", " ", str(result.get("title") or "").strip())
                url = str(result.get("url") or "").strip()
                if not title or not url.startswith(("http://", "https://")):
                    continue
                key = url.split("?", 1)[0].rstrip("/").lower() or title.lower()
                if key in seen:
                    continue
                raw_content = str(
                    result.get("content") or result.get("raw_content") or title
                )[:5000]
                content = re.sub(r"\s+", " ", raw_content.strip())
                dt = self._tavily_result_date(result)
                if dt is None and detail_used < detail_limit:
                    detail_used += 1
                    fetched_dt, fetched_title, fetched_desc = self._fetch_article_meta(url)
                    dt = fetched_dt
                    if fetched_title and len(fetched_title) >= 10:
                        title = fetched_title
                    if fetched_desc and len(fetched_desc) > len(content):
                        content = fetched_desc
                if dt is None:
                    missing_date += 1
                    continue
                if not within_window(dt):
                    dropped_window += 1
                    continue
                seen.add(key)
                items.append(NewsItem(
                    title=title[:300], url=url, source=name, category=category,
                    content=(content or title)[:1500],
                    published_at=dt.isoformat(),
                    scrape_strategy="tavily"))
        self._add_diag(
            name,
            f"Tavily: 检查 {checked} 条，窗口内 {len(items)} 条，"
            f"无可信时间 {missing_date} 条，窗口外 {dropped_window} 条")
        return items

    # ── ScrapeCreators 第三方爬虫 API ──────────────────────────────────────
    # 实测验证（2026-06-10）：twitter/user-tweets 返回 100 条一手推文（含最新），
    # 彻底绕过 syndication 的 IP 级 429；reddit/subreddit 绕过直抓 403。
    def _sc_get(self, path: str, params: Dict) -> Optional[Dict]:
        """调用 ScrapeCreators API。返回 JSON dict 或 None（未启用/失败）。"""
        if not (config.SCRAPECREATORS_ENABLED and config.SCRAPECREATORS_API_KEY):
            self._sc_last_error = "未启用或未配置 API Key"
            return None
        with self._sc_lock:
            if self._sc_disabled:
                return None
        from urllib.parse import urlencode
        url = f"{config.SCRAPECREATORS_BASE}/{path}?{urlencode(params)}"
        headers = {"x-api-key": config.SCRAPECREATORS_API_KEY}
        try:
            if _HAS_CURL_CFFI:
                resp = cffi_requests.get(url, headers=headers,
                                         timeout=config.HTTP_TIMEOUT + 15)
            else:
                resp = _requests.get(url, headers=headers,
                                     timeout=config.HTTP_TIMEOUT + 15)
        except Exception as e:  # noqa: BLE001
            self._sc_last_error = f"网络异常: {e}"
            self._log(f"    ScrapeCreators {self._sc_last_error}")
            return None
        if resp.status_code != 200:
            body = (resp.text or "")[:120]
            self._sc_last_error = f"HTTP {resp.status_code}: {body}"
            self._log(f"    ScrapeCreators {self._sc_last_error}")
            if resp.status_code in (401, 402, 403, 429):
                with self._sc_lock:
                    self._sc_disabled = True
            return None
        try:
            data = json.loads(resp.text, strict=False)
        except ValueError:
            self._sc_last_error = "返回非 JSON"
            return None
        if not data.get("success"):
            self._sc_last_error = f"业务失败: {str(data)[:120]}"
            self._log(f"    ScrapeCreators {self._sc_last_error}")
            return None
        credits = data.get("credits_remaining")
        if isinstance(credits, int) and credits <= 5:
            self._log(f"    ⚠️ ScrapeCreators 余额仅剩 {credits} credits")
        return data

    def _kol_fetch_via_sc(self, handle: str, name: str, category: str) -> KolFetchResult:
        """ScrapeCreators twitter/user-tweets 抓取 KOL 一手推文（主路径）。"""
        data = self._sc_get("twitter/user-tweets", {"handle": handle})
        if data is None:
            detail = f"（{self._sc_last_error}）" if self._sc_last_error else ""
            return KolFetchResult(
                provider="scrapecreators",
                note=f"ScrapeCreators 未启用/调用失败{detail}",
                ok=False)
        tweets = data.get("tweets") or []
        items: List[NewsItem] = []
        latest_dt: Optional[datetime] = None
        oldest_dt: Optional[datetime] = None
        for t in tweets:
            if not isinstance(t, dict):
                continue
            legacy = t.get("legacy") or {}
            full_text = (legacy.get("full_text") or "").strip()
            tid = legacy.get("id_str") or t.get("rest_id") or ""
            dt = parse_iso_or_struct(legacy.get("created_at"))
            if dt and (latest_dt is None or dt > latest_dt):
                latest_dt = dt
            if dt and (oldest_dt is None or dt < oldest_dt):
                oldest_dt = dt
            if not full_text:
                continue
            if not within_window(dt):
                continue
            tweet_url = (f"https://x.com/{handle}/status/{tid}" if tid
                         else f"https://x.com/{handle}")
            items.append(NewsItem(
                title=full_text[:120], url=tweet_url, source=name, category=category,
                content=full_text[:1500],
                published_at=dt.isoformat() if dt else None,
                scrape_strategy="scrapecreators"))
        if items:
            return KolFetchResult(
                provider="scrapecreators", items=items,
                note=f"ScrapeCreators 窗口内 {len(items)} 条/返回 {len(tweets)} 条",
                ok=True, fetched_count=len(tweets),
                latest_dt=latest_dt, oldest_dt=oldest_dt)
        if latest_dt is not None:
            age_h = int((datetime.now(CST) - latest_dt).total_seconds() // 3600)
            return KolFetchResult(
                provider="scrapecreators",
                note=(f"ScrapeCreators 窗口内 0 条/返回 {len(tweets)} 条，"
                      f"最新 {latest_dt.date()}，{age_h}h 前"),
                ok=True, fetched_count=len(tweets),
                latest_dt=latest_dt, oldest_dt=oldest_dt)
        return KolFetchResult(
            provider="scrapecreators",
            note=f"ScrapeCreators 窗口内 0 条/返回 {len(tweets)} 条，均无时间戳",
            ok=True, fetched_count=len(tweets))

    def scrape_x_kol_via_sc(self, handle: str, name: str, category: str
                            ) -> Tuple[List[NewsItem], str]:
        """兼容旧调用：返回 (items, note)。note 为空表示 API 未启用/失败。"""
        result = self._kol_fetch_via_sc(handle, name, category)
        return result.items, (result.note if result.ok else "")

    def scrape_reddit_via_sc(self, subreddit: str, name: str, category: str
                             ) -> List[NewsItem]:
        """ScrapeCreators reddit/subreddit 抓取（绕过 Reddit 直抓 403）。"""
        data = self._sc_get("reddit/subreddit", {"subreddit": subreddit, "sort": "new"})
        if data is None:
            return []
        items: List[NewsItem] = []
        for p in (data.get("posts") or []):
            if not isinstance(p, dict):
                continue
            title = (p.get("title") or "").strip()
            url = (p.get("url") or "").strip()
            if not title or not url:
                continue
            dt = None
            ts = p.get("created_utc") or p.get("created")
            if ts:
                try:
                    from datetime import timezone as _tz
                    dt = datetime.fromtimestamp(float(ts), tz=_tz.utc).astimezone(CST)
                except (ValueError, OverflowError, OSError):
                    dt = None
            if not within_window(dt):
                continue
            content = (p.get("selftext") or "").strip() or title
            items.append(NewsItem(
                title=title[:300], url=url, source=name, category=category,
                content=content[:1500],
                published_at=dt.isoformat() if dt else None,
                scrape_strategy="scrapecreators"))
            if len(items) >= 20:
                break
        return items

    # ── X.com KOL（syndication，串行 + 自适应退避）───────────────────────────
    def _kol_fetch_via_syndication(self, handle: str, name: str,
                                   category: str) -> KolFetchResult:
        """通过 syndication 抓取单个 KOL 可见推文。

        2026 现状（实测）：syndication 是唯一可用匿名通道，但 IP 级限频严苛，
        且即使 HTTP 200 数据也可能严重陈旧（Twitter 策略性降级未登录时间线）。
        窗口内 0 条是常见现实，诊断信息必须如实呈现而非伪造数据。
        """
        url = (f"https://syndication.twitter.com/srv/timeline-profile/"
               f"screen-name/{handle}?count=100")
        with self._x_lock:
            eff = self._x_min_interval * self._x_backoff
            wait = eff - (time.time() - self._x_last_ts)
            if wait > 0:
                time.sleep(wait)
            resp = self._http_get(url, referer="https://platform.twitter.com/",
                                  max_retries=0)
            self._x_last_ts = time.time()
            if resp is not None and resp.status_code == 429:
                self._x_consec_429 += 1
                self._x_backoff = min(5.0, 1.0 + (self._x_consec_429 // 3))
            elif resp is not None and resp.status_code == 200:
                self._x_consec_429 = 0
                self._x_backoff = 1.0

        if resp is None:
            return KolFetchResult(
                provider="syndication", note="syndication 网络不可达/超时", ok=False)
        if resp.status_code == 429:
            return KolFetchResult(
                provider="syndication",
                note="syndication IP 级限频 429",
                ok=False, limited=True)
        if resp.status_code != 200:
            return KolFetchResult(
                provider="syndication",
                note=f"syndication HTTP {resp.status_code}",
                ok=False)

        html = resp.text
        try:
            start = html.find("__NEXT_DATA__")
            if start == -1:
                return KolFetchResult(
                    provider="syndication",
                    note="syndication 未找到 __NEXT_DATA__（可能被登录墙拦截）",
                    ok=False)
            tag_end = html.find("</script>", start)
            json_str = html[html.index(">", start) + 1:tag_end]
            data = json.loads(json_str, strict=False)
            entries = data["props"]["pageProps"]["timeline"]["entries"]
        except (KeyError, ValueError, IndexError, TypeError) as e:
            return KolFetchResult(
                provider="syndication", note=f"syndication 解析失败: {e}", ok=False)

        items: List[NewsItem] = []
        latest_dt: Optional[datetime] = None
        oldest_dt: Optional[datetime] = None
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("type") != "tweet":
                continue
            try:
                tw = entry["content"]["tweet"]
            except (KeyError, TypeError):
                continue
            if isinstance(tw, dict):
                tw = tw.get("item", tw)
            if not isinstance(tw, dict):
                continue
            full_text = (tw.get("full_text") or tw.get("text") or "").strip()
            tid = tw.get("id_str") or tw.get("id") or ""
            dt = parse_iso_or_struct(tw.get("created_at"))
            if dt and (latest_dt is None or dt > latest_dt):
                latest_dt = dt
            if dt and (oldest_dt is None or dt < oldest_dt):
                oldest_dt = dt
            if not full_text:
                continue
            if not within_window(dt):
                continue
            tweet_url = (f"https://x.com/{handle}/status/{tid}" if tid
                         else f"https://x.com/{handle}")
            items.append(NewsItem(
                title=full_text[:120], url=tweet_url, source=name, category=category,
                content=full_text[:1500],
                published_at=dt.isoformat() if dt else None,
                scrape_strategy="syndication"))

        if items:
            return KolFetchResult(
                provider="syndication", items=items,
                note=f"syndication 窗口内 {len(items)} 条/返回 {len(entries)} 条",
                ok=True, fetched_count=len(entries),
                latest_dt=latest_dt, oldest_dt=oldest_dt)
        if latest_dt is not None:
            age_h = int((datetime.now(CST) - latest_dt).total_seconds() // 3600)
            return KolFetchResult(
                provider="syndication",
                note=(f"syndication 窗口内 0 条，最新 {latest_dt.date()}，"
                      f"{age_h}h 前"),
                ok=True, fetched_count=len(entries),
                latest_dt=latest_dt, oldest_dt=oldest_dt)
        return KolFetchResult(
            provider="syndication",
            note="syndication 窗口内 0 条（无可解析推文）",
            ok=True, fetched_count=len(entries))

    def scrape_x_kol(self, handle: str, name: str, category: str
                     ) -> Tuple[List[NewsItem], str]:
        """兼容旧调用：抓取单个 KOL 一手推文，返回 (items, 诊断说明)。"""
        result = self._kol_fetch_via_syndication(handle, name, category)
        return result.items, result.note

    def _kol_fetch_via_rss_mirrors(self, handle: str, name: str,
                                   category: str) -> List[KolFetchResult]:
        """免费 RSS 镜像兜底（如 RSSHub/Nitter RSS）。

        这些链路不是官方 API，稳定性取决于公共实例；只在主免费链路都没拿到
        窗口内推文时补充尝试，避免把整轮抓取拖慢。
        """
        templates = list(getattr(config, "X_KOL_RSS_FALLBACK_URLS", []))
        max_providers = max(0, int(getattr(config, "X_KOL_RSS_MAX_PROVIDERS", 1)))
        templates = templates[:max_providers]
        if not templates:
            return []
        if not _HAS_FEEDPARSER:
            return [KolFetchResult(
                provider="rss_mirror", note="RSS 镜像: feedparser 未安装", ok=False)]

        results: List[KolFetchResult] = []
        timeout = int(getattr(config, "X_KOL_RSS_TIMEOUT", 8))
        for tpl in templates:
            try:
                url = tpl.format(handle=quote(handle, safe=""), handle_raw=handle)
            except (KeyError, ValueError):
                results.append(KolFetchResult(
                    provider="rss_mirror", note=f"RSS 镜像模板非法: {tpl}", ok=False))
                continue
            provider = urlparse(url).netloc or "rss_mirror"
            resp = self._http_get(url, max_retries=0, timeout=timeout)
            if resp is None:
                results.append(KolFetchResult(
                    provider=provider, note=f"{provider} RSS 网络不可达/超时", ok=False))
                continue
            if resp.status_code != 200:
                results.append(KolFetchResult(
                    provider=provider, note=f"{provider} RSS HTTP {resp.status_code}", ok=False))
                continue
            try:
                feed = feedparser.parse(resp.content)
            except Exception as e:  # noqa: BLE001
                results.append(KolFetchResult(
                    provider=provider, note=f"{provider} RSS 解析失败: {e}", ok=False))
                continue

            entries = feed.entries or []
            items: List[NewsItem] = []
            latest_dt: Optional[datetime] = None
            oldest_dt: Optional[datetime] = None
            for entry in entries[:80]:
                title = (entry.get("title") or "").strip()
                link = (entry.get("link") or "").strip()
                content = ""
                for key in ("summary", "description"):
                    if entry.get(key):
                        content = BeautifulSoup(entry[key], "html.parser").get_text(
                            " ", strip=True)
                        break
                if not content and entry.get("content"):
                    try:
                        content = BeautifulSoup(entry["content"][0]["value"],
                                                "html.parser").get_text(" ", strip=True)
                    except Exception:
                        content = ""
                content = (content or title).strip()
                title = title or content[:120]
                if not title or not link:
                    continue
                pub = (entry.get("published_parsed") or entry.get("updated_parsed")
                       or entry.get("published") or entry.get("updated"))
                dt = parse_iso_or_struct(pub)
                if dt and (latest_dt is None or dt > latest_dt):
                    latest_dt = dt
                if dt and (oldest_dt is None or dt < oldest_dt):
                    oldest_dt = dt
                if not within_window(dt):
                    continue
                items.append(NewsItem(
                    title=title[:120], url=link, source=name, category=category,
                    content=content[:1500],
                    published_at=dt.isoformat() if dt else None,
                    scrape_strategy=f"rss_mirror:{provider}"))

            if items:
                note = f"{provider} RSS 窗口内 {len(items)} 条/返回 {len(entries)} 条"
            elif latest_dt is not None:
                age_h = int((datetime.now(CST) - latest_dt).total_seconds() // 3600)
                note = (f"{provider} RSS 窗口内 0 条/返回 {len(entries)} 条，"
                        f"最新 {latest_dt.date()}，{age_h}h 前")
            else:
                note = f"{provider} RSS 窗口内 0 条/返回 {len(entries)} 条"
            results.append(KolFetchResult(
                provider=provider, items=items, note=note, ok=True,
                fetched_count=len(entries), latest_dt=latest_dt, oldest_dt=oldest_dt))
        return results

    def _tweet_key(self, item: NewsItem) -> str:
        """跨免费链路合并同一推文。优先使用 status id，没有 id 时退回链接/标题。"""
        url = (item.url or "").split("?")[0].rstrip("/")
        m = re.search(r"/status(?:es)?/(\d+)", url)
        if m:
            return f"id:{m.group(1)}"
        if url:
            return f"url:{url.lower()}"
        title_key = re.sub(r"\s+", " ", (item.title or "").strip().lower())[:120]
        return f"title:{title_key}"

    def _merge_kol_items(self, results: List[KolFetchResult]) -> List[NewsItem]:
        seen, merged = set(), []
        for result in results:
            for item in result.items:
                key = self._tweet_key(item)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(item)
        merged.sort(key=lambda x: x.published_at or "", reverse=True)
        return merged

    def _kol_bounds(self, results: List[KolFetchResult]
                    ) -> Tuple[Optional[datetime], Optional[datetime]]:
        latest_dt: Optional[datetime] = None
        oldest_dt: Optional[datetime] = None
        for result in results:
            if result.latest_dt and (latest_dt is None or result.latest_dt > latest_dt):
                latest_dt = result.latest_dt
            if result.oldest_dt and (oldest_dt is None or result.oldest_dt < oldest_dt):
                oldest_dt = result.oldest_dt
        return latest_dt, oldest_dt

    def scrape_x_kol_multi(self, handle: str, name: str, category: str
                           ) -> Tuple[List[NewsItem], str, str, str,
                                      Optional[datetime], Optional[datetime]]:
        """无付费 X API 模式下的 KOL 多免费链路合并。

        返回: items, strategy, note, completeness, latest_seen, oldest_seen。
        completeness 不会标为 complete：免费/匿名链路无法证明官方全量。
        """
        results: List[KolFetchResult] = []
        sc_result = self._kol_fetch_via_sc(handle, name, category)
        results.append(sc_result)

        sc_auth_error = any(
            marker in sc_result.note
            for marker in ("HTTP 401", "HTTP 402", "HTTP 403", "HTTP 429")
        )
        should_try_free = (
            not sc_result.ok
            and (not sc_auth_error or getattr(config, "X_KOL_FREE_FALLBACK_ON_SC_AUTH_ERROR", False))
        )
        should_crosscheck = (
            sc_result.ok
            and getattr(config, "X_KOL_CROSSCHECK_FREE_WHEN_SC_OK", False)
        )

        # 专业 API 成功时默认不再强制交叉访问匿名链路。匿名 syndication/RSSHub
        # 在 2026 年已高度限频且经常返回旧缓存，强行验证会显著拖慢 37 个账号。
        if should_try_free or should_crosscheck:
            syn_result = self._kol_fetch_via_syndication(handle, name, category)
            results.append(syn_result)

        merged = self._merge_kol_items(results)
        if not merged and (should_try_free or should_crosscheck):
            results.extend(self._kol_fetch_via_rss_mirrors(handle, name, category))
            merged = self._merge_kol_items(results)

        latest_dt, oldest_dt = self._kol_bounds(results)
        attempted = [r.provider for r in results if r.note or r.ok]
        providers_with_items = [r.provider for r in results if r.items]
        strategy = "+".join(dict.fromkeys(providers_with_items or attempted)) or "x_free"
        notes = [r.note for r in results if r.note]

        if merged:
            completeness = "best_effort"
            note = ("免费链路尽力合并，非官方 API 不能证明全量；"
                    + "；".join(notes))
        elif any(r.limited or not r.ok for r in results):
            completeness = "suspected_partial"
            note = "免费链路未拿到窗口内推文，且存在失败/限流；" + "；".join(notes)
        elif latest_dt is not None:
            completeness = "suspected_partial"
            note = "免费链路仅能看到窗口外旧数据，疑似缓存陈旧或窗口内未发推；" + "；".join(notes)
        else:
            completeness = "unknown_empty"
            note = "免费链路均未返回可验证时间戳，无法判断是否全量；" + "；".join(notes)
        return merged, strategy, note, completeness, latest_dt, oldest_dt

    # ── 单源调度 ────────────────────────────────────────────────────────────
    def scrape_source(self, source: Dict) -> SourceReport:
        name = source["name"]
        category = source.get("category", "")
        stype = source.get("type", "web")
        crawl_url = source.get("crawl_url") or source["url"]
        allowed_hosts = source.get("allowed_hosts") or []
        rep = SourceReport(name=name, category=category, type=stype)

        try:
            if stype == "x_kol":
                # 无付费 X API 场景：多免费链路合并 + 完整性标注，不伪造全量。
                items, strategy, note, completeness, latest_dt, oldest_dt = (
                    self.scrape_x_kol_multi(source["handle"], name, category))
                items = self._audit_items(name, items)
                rep.items = items
                rep.strategy = strategy
                rep.count = len(items)
                rep.status = "success" if items else "empty"
                rep.error = note
                rep.completeness = completeness
                rep.latest_seen_at = latest_dt.isoformat() if latest_dt else None
                rep.oldest_seen_at = oldest_dt.isoformat() if oldest_dt else None
                return rep
            if stype == "anysearch":
                items = self._audit_items(name, self.scrape_anysearch(source))
                rep.items = items
                rep.strategy = "anysearch"
                rep.count = len(items)
                rep.status = "success" if items else "empty"
                if not items:
                    diag = self._diag.get(name) or []
                    rep.error = ("; ".join(diag[-4:]) if diag
                                 else "AnySearch 未返回窗口内可验证结果")
                return rep
            if stype == "tavily":
                items = self._audit_items(name, self.scrape_tavily(source))
                rep.items = items
                rep.strategy = "tavily"
                rep.count = len(items)
                rep.status = "success" if items else "empty"
                if not items:
                    diag = self._diag.get(name) or []
                    rep.error = ("; ".join(diag[-4:]) if diag
                                 else "Tavily 未返回窗口内可验证结果")
                return rep

            # RSS First，多源兜底：
            # 配置 RSS → 自动发现 Feed → Reddit 专用兜底 → sitemap → 网页结构化/SPA → Sogou。
            items: List[NewsItem] = []
            google_news_tried = False
            skip_expensive = False
            rss_urls = []
            if source.get("rss_urls"):
                rss_urls.extend(source.get("rss_urls") or [])
            if source.get("rss_url"):
                rss_urls.append(source["rss_url"])
            for rss_url in dict.fromkeys([u for u in rss_urls if u]):
                items = self.scrape_rss(name, rss_url, category)
                if items:
                    rep.strategy = items[0].scrape_strategy or "rss"
                    break
            if not items and source.get("google_news_queries"):
                google_news_tried = True
                items = self.scrape_google_news(
                    list(source.get("google_news_queries") or []), name, category,
                    aliases=list(source.get("google_news_source_aliases") or [name]),
                    domains=list(source.get("google_news_domains") or []))
                if items:
                    rep.strategy = "google_news"
            skip_expensive = bool(source.get("prefer_search_fallback") and google_news_tried)
            if not items and not skip_expensive:
                items = self.scrape_discovered_feeds(name, crawl_url, category, rss_urls)
                if items:
                    rep.strategy = items[0].scrape_strategy or "feed_discovery"
            if not items and not skip_expensive:
                items = self.scrape_linked_hubs(name, crawl_url, category, allowed_hosts)
                if items:
                    rep.strategy = items[0].scrape_strategy or "linked_hub"
            if not items and source.get("subreddit") and not skip_expensive:
                items = self.scrape_reddit_via_sc(source["subreddit"], name, category)
                if items:
                    rep.strategy = "scrapecreators"
            if not items and not skip_expensive:
                sitemap_options = {}
                if allowed_hosts:
                    sitemap_options["allowed_hosts"] = allowed_hosts
                if source.get("sitemap_path_hints"):
                    sitemap_options["path_hints"] = list(
                        source.get("sitemap_path_hints") or [])
                items = self.scrape_sitemap(
                    name, crawl_url, category, **sitemap_options)
                if items:
                    rep.strategy = "sitemap"
            if not items and not skip_expensive:
                items = self.scrape_web(name, crawl_url, category, allowed_hosts)
                if items:
                    rep.strategy = items[0].scrape_strategy
            if not items and source.get("google_news_queries") and not google_news_tried:
                items = self.scrape_google_news(
                    list(source.get("google_news_queries") or []), name, category,
                    aliases=list(source.get("google_news_source_aliases") or [name]),
                    domains=list(source.get("google_news_domains") or []))
                if items:
                    rep.strategy = "google_news"
            if not items and source.get("sogou_fallback"):
                items = self.scrape_sogou(source.get("sogou_query", name),
                                          name, category)
                if items:
                    rep.strategy = "sogou"

            items = self._audit_items(name, items)
            if source.get("filter_ai_relevance"):
                items = self._filter_ai_relevant(name, items)
            rep.items = items
            rep.count = len(items)
            rep.status = "success" if items else "empty"
            if not items:
                diag = self._diag.get(name) or []
                rep.error = ("; ".join(diag[-4:]) if diag
                             else "窗口内 0 条（昨日11am-当日11am 无新发布）")
            return rep
        except Exception as e:  # noqa: BLE001
            rep.status = "error"
            rep.error = str(e)
            return rep

    # ── 全量抓取（PRD：全量摘取，不漏任何信源；带硬超时防卡死）────────────────
    def run_all(self, sources: Optional[List[Dict]] = None,
                progress_fn: Optional[Callable[[int, int, str], None]] = None
                ) -> Tuple[List[NewsItem], List[SourceReport]]:
        """抓取全部信源。返回 (所有原始条目, 每信源报告——行数恒等于信源数)。"""
        sources = sources if sources is not None else config.get_all_sources()
        web_sources = [s for s in sources if s.get("type") != "x_kol"]
        kol_sources = [s for s in sources if s.get("type") == "x_kol"]

        reports: List[SourceReport] = []
        total = len(sources)
        done = 0

        # 1) Web/RSS/公司 → 并发 + 硬超时（KOL 不进此 executor）
        if web_sources:
            pool = ThreadPoolExecutor(max_workers=config.WEB_MAX_WORKERS)
            try:
                futures = {pool.submit(self.scrape_source, s): s for s in web_sources}
                pending = set(futures)
                deadline = time.time() + (config.SOURCE_FUTURE_TIMEOUT
                                          * config.RUN_DEADLINE_FACTOR)
                while pending:
                    remaining = max(0.0, deadline - time.time())
                    if remaining <= 0:
                        break
                    try:
                        for fut in as_completed(list(pending), timeout=remaining):
                            pending.remove(fut)
                            src = futures[fut]
                            try:
                                rep = fut.result(timeout=0)
                            except Exception as e:  # noqa: BLE001
                                rep = SourceReport(
                                    name=src["name"],
                                    category=src.get("category", ""),
                                    type=src.get("type", "web"),
                                    status="error", error=str(e))
                            reports.append(rep)
                            done += 1
                            if progress_fn:
                                progress_fn(done, total, rep.name)
                    except FutTimeout:
                        break
                # 超时未完成的源补 timeout 报告行（保证不漏任何信源）
                for fut in list(pending):
                    src = futures[fut]
                    fut.cancel()
                    reports.append(SourceReport(
                        name=src["name"], category=src.get("category", ""),
                        type=src.get("type", "web"), status="timeout",
                        error=f"单源抓取超过总 deadline（单源上限 "
                              f"{config.SOURCE_FUTURE_TIMEOUT}s），已跳过"))
                    done += 1
                    if progress_fn:
                        progress_fn(done, total, src["name"])
            finally:
                # 关键：不等卡死的网络调用（with 语句的 shutdown(wait=True) 是坑）
                pool.shutdown(wait=False, cancel_futures=True)

        # 2) X.com KOL
        #    - ScrapeCreators 启用时：API 无 IP 限频，可小并发（大幅提速）；
        #      降级 syndication 的调用天然被 _x_lock 串行化，不会触发 429 风暴
        #    - 未启用时：纯串行 + 自适应退避（syndication IP 级限频）
        if kol_sources:
            sc_on = bool(config.SCRAPECREATORS_ENABLED and config.SCRAPECREATORS_API_KEY)
            kol_timeout = max(15, int(getattr(config, "X_KOL_BATCH_TIMEOUT", 180)))
            kol_deadline = time.time() + kol_timeout
            if sc_on:
                kol_pool = ThreadPoolExecutor(max_workers=4)
                try:
                    kol_futs = {kol_pool.submit(self.scrape_source, s): s
                                for s in kol_sources}
                    pending = set(kol_futs)
                    while pending:
                        remaining = max(0.0, kol_deadline - time.time())
                        if remaining <= 0:
                            break
                        try:
                            for fut in as_completed(list(pending), timeout=remaining):
                                pending.remove(fut)
                                src = kol_futs[fut]
                                try:
                                    rep = fut.result(timeout=0)
                                except Exception as e:  # noqa: BLE001
                                    rep = SourceReport(
                                        name=src["name"], category=src.get("category", ""),
                                        type="x_kol", status="empty",
                                        strategy="x_kol_error",
                                        error=f"X KOL 链路异常: {e}",
                                        completeness="suspected_partial")
                                reports.append(rep)
                                done += 1
                                if progress_fn:
                                    progress_fn(done, total, rep.name)
                        except FutTimeout:
                            break
                    for fut in list(pending):
                        src = kol_futs[fut]
                        fut.cancel()
                        rep = SourceReport(
                            name=src["name"], category=src.get("category", ""),
                            type="x_kol", status="empty",
                            strategy="x_kol_deadline",
                            error=(f"X KOL 批次超过 {kol_timeout}s 总预算；"
                                   "未完成账号标记为疑似不全，需付费 API/登录态保证全量"),
                            completeness="suspected_partial")
                        reports.append(rep)
                        done += 1
                        if progress_fn:
                            progress_fn(done, total, rep.name)
                finally:
                    kol_pool.shutdown(wait=False, cancel_futures=True)
            else:
                for idx, src in enumerate(kol_sources):
                    if time.time() >= kol_deadline:
                        for skipped in kol_sources[idx:]:
                            rep = SourceReport(
                                name=skipped["name"], category=skipped.get("category", ""),
                                type="x_kol", status="empty",
                                strategy="x_kol_deadline",
                                error=(f"X KOL 免费链路超过 {kol_timeout}s 总预算；"
                                       "未完成账号标记为疑似不全"),
                                completeness="suspected_partial")
                            reports.append(rep)
                            done += 1
                            if progress_fn:
                                progress_fn(done, total, rep.name)
                        break
                    rep = self.scrape_source(src)
                    reports.append(rep)
                    done += 1
                    if progress_fn:
                        progress_fn(done, total, rep.name)

        all_items: List[NewsItem] = []
        for rep in reports:
            all_items.extend(rep.items)
        return all_items, reports


# ──────────────────────────────────────────────────────────────────────────
# 覆盖校验（PRD：第一轮抓取完毕后自动评价，验证所有信源都被摘取，不存在遗漏）
# ──────────────────────────────────────────────────────────────────────────
def classify_report_issue(report: SourceReport) -> str:
    """把报告归因到用户可理解的原因桶，避免把 empty 和 error 混在一起。"""
    if report.status == "success":
        return "ok"
    if report.type == "x_kol" and report.completeness in (
            "suspected_partial", "unknown_empty", "failed"):
        return report.completeness
    if report.type == "x_kol" and report.status in ("empty", "error", "timeout"):
        return "suspected_partial"
    if report.status == "timeout":
        return "network"

    text = f"{report.error or ''} {report.strategy or ''}".lower()
    window_empty_markers = (
        "窗口内 0", "窗口内0", "无新发布", "均在窗口外",
        "详情页核验后均在窗口外", "详情页核验后为空",
        "检查 ", "窗口内 0 条", "索引延迟",
    )
    hard_failure_markers = (
        "http 403", "forbidden", "验证码", "http 429", "限频",
        "http 404", "http 410", "解析失败", "未提取到任何文章链接",
        "结构不识别",
    )
    verified_empty_markers = (
        "rss: feed 有", "详情页核验后均在窗口外", "详情页核验后为空",
        "sitemap: 找到", "sitemap: 检查", "直抓: 提取到",
    )

    # 多级兜底链路下，某个备用入口超时/404 很常见；只要报告里已有明确
    # “窗口内没有可验证新内容”的证据，就不应把整个信源归为需要修复。
    if report.status == "empty" and any(m in text for m in window_empty_markers):
        if any(m in text for m in verified_empty_markers):
            return "window_empty"
        if not any(m in text for m in hard_failure_markers):
            return "window_empty"
        if "sitemap" in text and "窗口内 0" in text:
            return "window_empty"
        if "sogou" in text and ("均在窗口外" in text or "索引延迟" in text):
            return "window_empty"

    if "http 404" in text or "410" in text:
        return "invalid_url"
    if "http 403" in text or "验证码" in text or "forbidden" in text:
        return "blocked"
    if "429" in text or "限频" in text:
        return "rate_limited"
    if "网络不可达" in text or "超时" in text or "timeout" in text:
        return "network"
    if ("rss: feed 有" in text and "均在窗口外" in text) or (
            "详情页核验后均在窗口外" in text):
        return "window_empty"
    if "未提取到任何文章链接" in text or "结构不识别" in text or "解析失败" in text:
        return "parser"
    if "无时间戳" in text:
        return "undated"
    if "窗口外" in text or "无新发布" in text or "窗口内 0" in text:
        return "window_empty"
    if report.status == "empty":
        return "window_empty"
    if report.status == "error":
        return "failed"
    return "empty_unknown"


def verify_coverage(reports: List[SourceReport],
                    expected: Optional[List[Dict]] = None) -> Dict:
    """校验 PRD 全部信源是否都有抓取报告行（即都被尝试，不存在遗漏）。"""
    expected = expected if expected is not None else config.get_all_sources()
    reported_names = {r.name for r in reports}
    missing = [s["name"] for s in expected if s["name"] not in reported_names]
    issue_counts: Dict[str, int] = {}
    for r in reports:
        r.issue_type = classify_report_issue(r)
        issue_counts[r.issue_type] = issue_counts.get(r.issue_type, 0) + 1
    success = [r for r in reports if r.status == "success"]
    empty = [r for r in reports if r.status == "empty"]
    errored = [r for r in reports if r.status in ("error", "timeout")]
    return {
        "expected_total": len(expected),
        "report_rows": len(reports),
        "missing_sources": missing,           # 完全没被尝试的源（必须为空）
        "success_count": len(success),
        "empty_count": len(empty),
        "error_count": len(errored),
        "issue_counts": issue_counts,
        "total_items": sum(r.count for r in reports),
        "all_covered": len(missing) == 0 and len(reports) >= len(expected),
    }
