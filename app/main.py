from __future__ import annotations

import asyncio
import json
import os
import re
from difflib import SequenceMatcher
from datetime import timezone
from pathlib import Path
from typing import List
from urllib.parse import parse_qsl, urlencode, urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

import config
from app.db import SessionLocal, get_db, init_db
from app.jobs import (
    create_run,
    json_loads,
    launch_analyze,
    launch_crawl,
    mark_interrupted_runs,
    start_daily_crawl_scheduler,
    stop_daily_crawl_scheduler,
)
from app.models import (
    CustomSource,
    RawNewsItemRecord,
    Run,
    RunLog,
    SourceReportRecord,
    StructuredNewsRecord,
    utcnow,
)
from scraper import get_strict_window
from news_processor import _near_dup_clusters, _norm_title_for_sim


BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _utc_iso(value) -> str:
    if not value:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


templates.env.filters["utc_iso"] = _utc_iso

app = FastAPI(title="AI每日大事件")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.middleware("http")
async def no_cache_html(request: Request, call_next):
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("text/html"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


LIVE_STATUSES = {"pending", "running", "analyzing"}
STATUS_LABELS = {
    "pending": "等待启动",
    "running": "抓取中",
    "crawled": "抓取完成",
    "analyzing": "分析中",
    "completed": "已完成",
    "failed": "失败",
}
ISSUE_LABELS = {
    "ok": "正常命中",
    "window_empty": "当期无新内容",
    "parser": "页面结构待适配",
    "blocked": "访问受限",
    "invalid_url": "入口失效",
    "network": "网络异常",
    "rate_limited": "访问限频",
    "undated": "时间不可验证",
    "suspected_partial": "完整性受限",
    "unknown_empty": "无法确认空窗",
    "failed": "抓取失败",
    "empty_unknown": "结果待确认",
}
AGENT_TOPIC = "Agent智能体"
AGENT_CATEGORY = "公司-Agent"
EMBODIED_ROBOT_TOPIC = "具身机器人"
PROJECT_FINANCING_TOPIC = "项目融资"
AGENT_KEYWORDS = (
    "agent",
    "agentic",
    "智能体",
    "langchain",
    "devin",
)
CATEGORY_LABELS = {
    AGENT_CATEGORY: AGENT_TOPIC,
    "公司-大模型企业": "大模型企业",
    "公司-图像生成": "图像生成",
    "公司-视频生成": "视频生成",
    "公司-Research Lab": "Research Lab",
}
COMPANY_ALIAS_GROUPS = {
    "Apple": ("Apple", "苹果"),
    "NVIDIA": ("NVIDIA", "Nvidia", "英伟达"),
    "Meta": ("Meta", "Meta AI"),
    "Google": ("Google", "Google AI", "Google DeepMind", "DeepMind", "谷歌"),
    "xAI": ("xAI", "Grok"),
    "阿里巴巴": ("阿里巴巴", "阿里", "Alibaba", "阿里巴巴 (Alibaba)"),
    "字节跳动": ("字节跳动", "Bytedance", "字节跳动 (Bytedance)"),
    "腾讯": ("腾讯", "Tencent", "腾讯 (Tencent)"),
    "月之暗面": ("月之暗面", "Kimi", "Moonshot", "Moonshot AI"),
    "智谱 AI": (
        "智谱",
        "智谱AI",
        "智谱 GLM",
        "智谱 GLM (Zhipu)",
        "智谱GLM(Zhipu)",
        "Zhipu",
        "Zhipu AI",
    ),
    "Allen Institute (AI2)": (
        "Allen Institute",
        "Allen Institute (AI2)",
        "AI2",
    ),
    "Cognition": ("Cognition", "Cognition (Devin)"),
    "Thinking Machines Lab": ("Thinking Machines", "Thinking Machines Lab"),
    "商汤科技": ("商汤", "商汤科技", "SenseTime"),
    "阶跃星辰": ("阶跃 AI", "阶跃星辰", "StepFun"),
    "蚂蚁集团": ("蚂蚁集团", "蚂蚁数科", "蚂蚁安全", "Ant Group"),
}
COMPANY_SPLIT_RE = re.compile(r"\s*(?:/|／|、)\s*")


def _company_alias_key(value: str) -> str:
    return re.sub(r"[\W_]+", "", (value or "").casefold())


COMPANY_ALIAS_LOOKUP = {
    _company_alias_key(alias): canonical
    for canonical, aliases in COMPANY_ALIAS_GROUPS.items()
    for alias in (canonical, *aliases)
}


def _company_filter_values(value: str) -> list[str]:
    """Split multi-company labels and collapse known aliases to one display name."""
    parts = COMPANY_SPLIT_RE.split(value or "")
    companies: list[str] = []
    seen: set[str] = set()
    for part in parts:
        cleaned = part.strip()
        if not cleaned:
            continue
        canonical = COMPANY_ALIAS_LOOKUP.get(_company_alias_key(cleaned), cleaned)
        key = _company_alias_key(canonical)
        if key and key not in seen:
            seen.add(key)
            companies.append(canonical)
    return companies


def _company_filter_search_terms(company: str) -> str:
    aliases = COMPANY_ALIAS_GROUPS.get(company, ())
    return " ".join(dict.fromkeys((company, *aliases)))


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    mark_interrupted_runs()
    start_daily_crawl_scheduler()


@app.on_event("shutdown")
def on_shutdown() -> None:
    stop_daily_crawl_scheduler()


def _provider_models() -> dict:
    return {name: conf["models"] for name, conf in config.LLM_PROVIDERS.items()}


def _default_provider() -> str:
    provider = os.getenv("MODEL_PROVIDER") or config.DEFAULT_PROVIDER
    return provider if provider in config.LLM_PROVIDERS else config.DEFAULT_PROVIDER


def _default_model(provider: str) -> str:
    conf = config.LLM_PROVIDERS.get(provider) or config.LLM_PROVIDERS[config.DEFAULT_PROVIDER]
    return os.getenv("MODEL_NAME") or conf["default_model"]


def _selected_model(model: str, custom_model: str, *, required: bool = False) -> str:
    """Use manual model id first; fall back to the selected preset model."""
    selected = (custom_model or model or "").strip()
    if required and not selected:
        raise HTTPException(
            status_code=400,
            detail="选择“不选择”时，请填写手动型号。",
        )
    return selected


def _all_source_categories() -> list[str]:
    """PRD 板块 + 用户自定义信源板块（去重、稳定顺序，自定义排在后面）。"""
    ordered = list(config.get_source_categories())
    seen = set(ordered)
    db = SessionLocal()
    try:
        rows = db.query(CustomSource).order_by(CustomSource.id.asc()).all()
    finally:
        db.close()
    for row in rows:
        category = (row.category or "自定义信源").strip() or "自定义信源"
        if category not in seen:
            seen.add(category)
            ordered.append(category)
    return ordered


def _source_counts() -> dict:
    db = SessionLocal()
    try:
        rows = db.query(CustomSource).all()
    finally:
        db.close()
    disabled_builtin = {
        row.source_key
        for row in rows
        if row.is_builtin and row.source_key and not row.enabled
    }
    enabled_custom = sum(1 for row in rows if not row.is_builtin and row.enabled)
    disabled_web_builtin = {
        key
        for key in disabled_builtin
        if any(source["name"] == key for source in config.WEB_SOURCES)
    }
    return {
        "source_total": len(config.get_all_sources()) - len(disabled_builtin) + enabled_custom,
        "web_total": len(config.WEB_SOURCES) - len(disabled_web_builtin) + enabled_custom,
        "kol_total": len(config.X_KOL_SOURCES) - (
            len(disabled_builtin) - len(disabled_web_builtin)
        ),
    }


def _template_context(request: Request, **extra) -> dict:
    start, end = get_strict_window()
    provider = _default_provider()
    source_counts = _source_counts()
    ctx = {
        "request": request,
        "source_categories": _all_source_categories(),
        "providers": list(config.LLM_PROVIDERS.keys()),
        "provider_models": _provider_models(),
        "default_provider": provider,
        "default_model": _default_model(provider),
        "env_key_configured": bool(os.getenv("MODEL_API_KEY", "").strip()),
        "status_labels": STATUS_LABELS,
        "issue_labels": ISSUE_LABELS,
        "live_statuses": LIVE_STATUSES,
        "window_start": start.strftime("%Y-%m-%d %H:%M"),
        "window_end": end.strftime("%Y-%m-%d %H:%M"),
        "source_total": source_counts["source_total"],
        "web_total": source_counts["web_total"],
        "kol_total": source_counts["kol_total"],
    }
    ctx.update(extra)
    return ctx


def _get_run_or_404(db: Session, run_id: str) -> Run:
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="任务不存在。")
    return run


def _category_label(category: str) -> str:
    return CATEGORY_LABELS.get(category, category or "未分板块")


def _is_agent_item(item: StructuredNewsRecord) -> bool:
    if item.category == AGENT_CATEGORY or item.news_type == AGENT_TOPIC:
        return True
    text = " ".join([
        item.event or "",
        item.detail or "",
        item.impact or "",
        item.company or "",
        item.source or "",
    ]).lower()
    return any(keyword in text for keyword in AGENT_KEYWORDS)


def _filter_topic(item: StructuredNewsRecord) -> str:
    # Explicit model classification wins over the broader Agent keyword heuristic.
    if item.news_type in {EMBODIED_ROBOT_TOPIC, PROJECT_FINANCING_TOPIC}:
        return item.news_type
    if _is_agent_item(item):
        return AGENT_TOPIC
    return item.news_type or "未分类"


def _filter_date(item: StructuredNewsRecord) -> str:
    published_at = item.published_at or ""
    return published_at[:10] if len(published_at) >= 10 else ""


def _prepare_structured_items(items: list[StructuredNewsRecord]) -> list[StructuredNewsRecord]:
    for item in items:
        item.filter_topic = _filter_topic(item)
        item.filter_category = item.category or ""
        item.filter_category_label = _category_label(item.category or "")
        item.filter_date = _filter_date(item)
        item.filter_companies = _company_filter_values(item.company or "")
        item.filter_company = item.filter_companies[0] if item.filter_companies else ""
    return items


def _structured_filter_options(items: list[StructuredNewsRecord]) -> dict:
    def sorted_values(values: set[str]) -> list[str]:
        return sorted((value for value in values if value), key=str.casefold)

    topic_values = {getattr(item, "filter_topic", item.news_type or "") for item in items}
    always_visible_topics = {
        AGENT_TOPIC,
        EMBODIED_ROBOT_TOPIC,
        PROJECT_FINANCING_TOPIC,
    }
    ordered_topics = [
        topic
        for topic in config.NEWS_TYPES
        if topic in topic_values or topic in always_visible_topics
    ]
    ordered_topics.extend(
        topic for topic in sorted_values(topic_values) if topic not in ordered_topics
    )
    dates = sorted_values({getattr(item, "filter_date", _filter_date(item)) for item in items})
    dates.reverse()
    companies = sorted_values({
        company
        for item in items
        for company in getattr(
            item,
            "filter_companies",
            _company_filter_values(item.company or ""),
        )
    })
    return {
        "topics": ordered_topics,
        "companies": companies,
        "company_search_terms": {
            company: _company_filter_search_terms(company)
            for company in companies
        },
        "sources": sorted_values({item.source for item in items}),
        "dates": dates,
    }


_SEMANTIC_ACTION_PATTERNS = (
    ("funding", re.compile(
        r"融资|募资|筹资|投资|注资|估值|种子轮|天使轮|[A-Ha-h]\s*轮|"
        r"\bfunding\b|\bfinancing\b|\bseries\s+[a-h]\b",
        re.IGNORECASE)),
    ("acquisition", re.compile(
        r"收购|并购|控股|出售|交易估值|\bacqui(?:re|sition)\b|\bmerger\b",
        re.IGNORECASE)),
    ("security_incident", re.compile(
        r"攻击|入侵|泄露|误删|删除(?:用户|文件|目录|数据)|安全警报|配置失误|漏洞"
        r"|\bbreach\b|\bhack(?:ed|ing)?\b|\bdelet(?:e|ed|ing)\b",
        re.IGNORECASE)),
    ("legal_settlement", re.compile(
        r"和解|诉讼|版权案|盗版作品|获最终批准|法院批准"
        r"|\bsettlement\b|\blawsuit\b|\bcopyright\s+case\b",
        re.IGNORECASE)),
    ("free_extension", re.compile(
        r"延长.{0,12}(?:免费|订阅)|继续纳入订阅|免费期|免费额度"
        r"|\bextend(?:s|ed)?\b.{0,20}\bfree\b",
        re.IGNORECASE)),
    ("quota_change", re.compile(
        r"重置.{0,12}额度|额度.{0,12}重置|调整.{0,12}额度|额度调整",
        re.IGNORECASE)),
    ("personnel_change", re.compile(
        r"离职|辞职|卸任|加入.{0,12}(?:公司|团队)|任命|裁员"
        r"|\bresign|\bdepart|\bleav(?:e|es|ing)\b|\blayoff",
        re.IGNORECASE)),
    ("competitive_response", re.compile(
        r"促使.{0,20}(?:重置|调整|回应)|应对.{0,20}(?:竞争|发布)|"
        r"竞争压力|价格战|军备竞赛",
        re.IGNORECASE)),
    ("industry_discussion", re.compile(
        r"传闻|引发.{0,20}(?:讨论|关注|担忧|争议|竞逐)|"
        r"竞争格局|算力讨论|中美竞逐|外界关注",
        re.IGNORECASE)),
    ("benchmark", re.compile(
        r"评测|基准|榜单|登顶|排名|性能测试|跑分"
        r"|\bbenchmark\b|\bleaderboard\b|\branking\b",
        re.IGNORECASE)),
    ("pricing_restriction", re.compile(
        r"额度减半|限制下调|按量付费|API\s*计费|涨价|降价|定价|价格"
        r"|\bpricing\b|\bprice\b|\bpaid\b",
        re.IGNORECASE)),
    ("research_discovery", re.compile(
        r"发现.{0,20}(?:机制|空间|推理|内部|方法)|窥探|剖析.{0,12}(?:机制|内部)|"
        r"内部工作机制|可解释性|研究揭示|论文|探索.{0,12}(?:模型|机制)|"
        r"改造为.{0,12}模型|\binterpretability\b|\bresearch\b|\bstudy\b",
        re.IGNORECASE)),
    ("release", re.compile(
        r"发布|推出|上线|开源|首发|正式开放|正式亮相"
        r"|\breleas(?:e|ed|es)\b|\blaunch(?:ed|es)?\b|\bunveil(?:ed|s)?\b",
        re.IGNORECASE)),
    ("feature_update", re.compile(
        r"新增|升级|更新|支持|接入|集成|功能|工具"
        r"|\bfeature\b|\bupdate(?:d|s)?\b|\bintegrat(?:e|ed|ion)\b",
        re.IGNORECASE)),
    ("partnership", re.compile(
        r"合作|联合|携手|伙伴关系|\bpartner(?:ship|ed)?\b|\bcollaborat",
        re.IGNORECASE)),
    ("policy", re.compile(
        r"监管|政策|限制|禁令|制裁|调查|合规"
        r"|\bregulat|\bpolicy\b|\bsanction|\binvestigat",
        re.IGNORECASE)),
)

_SEMANTIC_ANCHOR_STOPWORDS = {
    "ai", "the", "a", "an", "and", "for", "with", "to", "of", "in",
    "on", "new", "model", "models", "agent", "agents", "company",
    "openai", "anthropic", "google", "deepmind", "meta", "microsoft",
    "nvidia", "alibaba", "tencent", "bytedance", "moonshot",
}
_SEMANTIC_PRODUCT_FAMILIES = {
    "chatgpt", "claude", "gemini", "grok", "kimi", "llama", "qwen",
    "sensenova", "notebooklm",
}


def _normalize_cross_date_url(url: str) -> str:
    """Normalize article URLs without collapsing distinct redirect targets."""
    value = (url or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    host = (parsed.hostname or "").casefold()
    query = ""
    if host in {"weixin.sogou.com", "www.sogou.com"}:
        identity = [
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=False)
            if key.casefold() in {"url", "target", "destination"}
        ]
        query = urlencode(identity)
    normalized = parsed._replace(
        scheme=parsed.scheme.casefold(),
        netloc=parsed.netloc.casefold(),
        query=query,
        fragment="",
    ).geturl()
    return normalized.rstrip("/")


def _semantic_action(text: str) -> str:
    for name, pattern in _SEMANTIC_ACTION_PATTERNS:
        if pattern.search(text or ""):
            return name
    return ""


def _semantic_anchors(text: str) -> set[str]:
    anchors = {
        token
        for token in re.findall(
            r"[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*|[a-z]*\d+(?:\.\d+)*(?:[a-z]+)?",
            (text or "").casefold(),
        )
        if len(token) >= 2 and token not in _SEMANTIC_ANCHOR_STOPWORDS
    }
    return anchors


def _semantic_model_keys(text: str) -> set[str]:
    """Normalize model identifiers such as Grok 4.5/Grok4.5 and GPT-5.6."""
    keys: set[str] = set()
    for chunk in re.findall(r"[a-z0-9][a-z0-9 .+\-]{1,32}", (text or "").casefold()):
        if not re.search(r"\d", chunk):
            continue
        tokens = re.findall(r"[a-z]+\d*(?:\.\d+)*|\d+(?:\.\d+)*", chunk)
        for index, token in enumerate(tokens):
            if not re.search(r"\d", token):
                continue
            compact_token = re.sub(r"[^a-z0-9.]+", "", token)
            if compact_token:
                keys.add(compact_token)
            if index > 0 and re.search(r"[a-z]", tokens[index - 1]):
                keys.add(re.sub(
                    r"[^a-z0-9.]+", "", tokens[index - 1] + token))
            if index + 1 < len(tokens) and re.search(r"[a-z]", tokens[index + 1]):
                keys.add(re.sub(
                    r"[^a-z0-9.]+", "", token + tokens[index + 1]))
    return keys


def _semantic_named_entities(text: str) -> set[str]:
    entities = set()
    for token in re.findall(
            r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]{3,})(?![A-Za-z0-9])",
            text or ""):
        if token.isupper() or token.islower():
            continue
        key = token.casefold()
        if key not in _SEMANTIC_ANCHOR_STOPWORDS:
            entities.add(key)
    return entities


def _semantic_number_facts(text: str) -> set[str]:
    return {
        re.sub(r"\s+", "", match).casefold()
        for match in re.findall(
            r"\d+(?:\.\d+)?\s*(?:万|亿|兆|[kmbt](?![a-z]))?\s*"
            r"(?:美元|人民币|欧元|英镑|元|参数|token|tokens|%|亿美元|亿元)?"
            r"(?![a-z])",
            text or "",
            re.IGNORECASE,
        )
        if re.search(r"\d", match)
    }


def _semantic_company_keys(item: StructuredNewsRecord) -> set[str]:
    values = _company_filter_values(item.company or "")
    return {_company_alias_key(value) for value in values if value}


def _semantic_event_profile(item: StructuredNewsRecord) -> dict:
    event = item.event or ""
    company_anchor_text = " ".join(
        _company_filter_search_terms(value)
        for value in _company_filter_values(item.company or "")
    )
    company_anchors = _semantic_anchors(company_anchor_text)
    event_anchors = _semantic_anchors(event)
    anchors_to_remove = company_anchors - _SEMANTIC_PRODUCT_FAMILIES
    security_facts = {
        name
        for name, pattern in (
            ("file_deletion", r"误删|删除.{0,8}(?:用户|文件|目录|数据)|清空.{0,8}(?:目录|文件)"),
            ("intrusion", r"攻击|入侵|\bbreach\b|\bhack(?:ed|ing)?\b"),
            ("data_leak", r"泄露|数据外泄|\bleak\b"),
            ("misconfiguration", r"配置失误|错误配置|misconfig"),
        )
        if re.search(pattern, event, re.IGNORECASE)
    }
    return {
        "action": _semantic_action(event),
        "companies": _semantic_company_keys(item),
        "anchors": (
            (event_anchors - anchors_to_remove)
            | _semantic_named_entities(event)
            | _semantic_named_entities(item.detail or "")
        ),
        "model_keys": _semantic_model_keys(event),
        "numbers": _semantic_number_facts(event),
        "security_facts": security_facts,
        "normalized": _norm_title_for_sim(event),
        "mentions_release": bool(re.search(
            r"发布|推出|上线|开源|首发|正式开放|正式亮相"
            r"|\breleas(?:e|ed|es)\b|\blaunch(?:ed|es)?\b|\bunveil(?:ed|s)?\b",
            event,
            re.IGNORECASE,
        )),
        "is_rumor": bool(re.search(r"传闻|据传|消息称|rumou?r", event, re.IGNORECASE)),
        "multi_event": bool(re.search(
            r"等多款|多款.{0,12}(?:模型|产品)|盘点|汇总|一览|并推出|同时推出|"
            r"及.{0,24}(?:发布|推出|上线|预览)|同步.{0,12}(?:发布|推出|上线)",
            event,
            re.IGNORECASE,
        )),
    }


def _semantic_profile_match(left: dict, right: dict) -> tuple[bool, float]:
    action_pair = {left["action"], right["action"]}
    release_discussion = (
        action_pair == {"release", "industry_discussion"}
        and left["mentions_release"]
        and right["mentions_release"]
        and not left["is_rumor"]
        and not right["is_rumor"]
    )
    if not left["action"] or (
            left["action"] != right["action"] and not release_discussion):
        return False, 0.0
    if left["multi_event"] != right["multi_event"]:
        return False, 0.0

    companies_overlap = bool(left["companies"] & right["companies"])
    if left["companies"] and right["companies"] and not companies_overlap:
        return False, 0.0

    title_ratio = SequenceMatcher(
        None, left["normalized"], right["normalized"]).ratio()
    if (left["action"] == "legal_settlement"
            and companies_overlap and title_ratio >= 0.55):
        return True, min(1.0, title_ratio + 0.08)

    shared_anchors = left["anchors"] & right["anchors"]
    shared_model_keys = left["model_keys"] & right["model_keys"]
    if (left["anchors"] and right["anchors"]
            and not shared_anchors and not shared_model_keys):
        return False, 0.0
    if (left["numbers"] and right["numbers"]
            and left["numbers"].isdisjoint(right["numbers"])):
        return False, 0.0

    shared_version_anchor = bool(shared_model_keys) or any(
        re.search(r"[a-z]", anchor) and re.search(r"\d", anchor)
        for anchor in shared_anchors
    )
    if (left["action"] == right["action"] == "release" and shared_version_anchor
            and title_ratio < 0.52):
        extra_models = (
            left["model_keys"] ^ right["model_keys"]) - {
                key for key in shared_model_keys if re.fullmatch(r"\d+(?:\.\d+)*", key)
            }
        if len(extra_models) > 1:
            return False, title_ratio
    if release_discussion and shared_version_anchor and companies_overlap:
        return True, min(1.0, title_ratio + 0.12)
    if (len(shared_anchors) >= 2 and title_ratio >= 0.60
            and (companies_overlap or title_ratio >= 0.68)):
        return True, min(1.0, title_ratio + min(len(shared_anchors), 3) * 0.08)
    if shared_version_anchor and companies_overlap and title_ratio >= 0.32:
        return True, min(1.0, title_ratio + 0.14)
    if (len(shared_anchors) == 1 and companies_overlap
            and title_ratio >= 0.68):
        return True, min(1.0, title_ratio + 0.08)
    if (left["action"] in {"research_discovery", "security_incident"}
            and shared_anchors and companies_overlap and title_ratio >= 0.60):
        return True, min(1.0, title_ratio + 0.08)
    if (left["action"] == "security_incident" and companies_overlap
            and shared_model_keys and title_ratio >= 0.25):
        return True, min(1.0, title_ratio + 0.14)
    shared_security_facts = left["security_facts"] & right["security_facts"]
    if (left["action"] == "security_incident" and companies_overlap
            and shared_security_facts and title_ratio >= 0.20):
        return True, min(1.0, title_ratio + 0.12)
    shared_numbers = left["numbers"] & right["numbers"]
    if (left["action"] in {"funding", "legal_settlement"}
            and companies_overlap and shared_numbers and title_ratio >= 0.50):
        return True, min(1.0, title_ratio + 0.10)
    if companies_overlap and title_ratio >= 0.78:
        return True, title_ratio
    return False, title_ratio


def _semantic_same_event(
    left: StructuredNewsRecord,
    right: StructuredNewsRecord,
) -> bool:
    """High-precision semantic match for differently worded event summaries."""
    matched, _score = _semantic_profile_match(
        _semantic_event_profile(left),
        _semantic_event_profile(right),
    )
    return matched


def _semantic_complete_linkage_clusters(
    items: list[StructuredNewsRecord],
) -> list[list[int]]:
    """Build complete-linkage semantic clusters from high-confidence pairs."""
    profiles = [_semantic_event_profile(item) for item in items]
    action_buckets: dict[str, list[int]] = {}
    for index, profile in enumerate(profiles):
        if profile["action"]:
            action_buckets.setdefault(profile["action"], []).append(index)

    matched_pairs: dict[tuple[int, int], float] = {}
    for bucket in action_buckets.values():
        for position, left in enumerate(bucket):
            for right in bucket[position + 1:]:
                matched, score = _semantic_profile_match(
                    profiles[left], profiles[right])
                if matched:
                    matched_pairs[(left, right)] = score
    for left in action_buckets.get("release", []):
        for right in action_buckets.get("industry_discussion", []):
            matched, score = _semantic_profile_match(
                profiles[left], profiles[right])
            if matched:
                matched_pairs[(min(left, right), max(left, right))] = score

    clusters: dict[int, list[int]] = {
        index: [index] for index in range(len(items))
    }
    while True:
        candidate_roots: set[tuple[int, int]] = set()
        index_to_root = {
            index: root
            for root, members in clusters.items()
            for index in members
        }
        for left, right in matched_pairs:
            left_root, right_root = index_to_root[left], index_to_root[right]
            if left_root != right_root:
                candidate_roots.add(tuple(sorted((left_root, right_root))))

        best_pair = None
        best_score = -1.0
        for left_root, right_root in candidate_roots:
            cross_scores = [
                matched_pairs.get((min(left, right), max(left, right)))
                for left in clusters[left_root]
                for right in clusters[right_root]
            ]
            if any(score is None for score in cross_scores):
                continue
            complete_linkage_score = min(cross_scores)
            if complete_linkage_score > best_score:
                best_pair = (left_root, right_root)
                best_score = complete_linkage_score
        if best_pair is None:
            break
        left_root, right_root = best_pair
        clusters[left_root].extend(clusters.pop(right_root))

    return sorted(
        (sorted(group) for group in clusters.values()),
        key=lambda group: group[0],
    )


def _dedupe_dashboard_structured_items(
    items: list[StructuredNewsRecord],
) -> list[StructuredNewsRecord]:
    """Mark cross-date duplicates; the dashboard applies them only on request."""
    for item in items:
        item.cross_date_duplicate = False
        item.cross_date_group = ""
        item.cross_date_rank = 0
    if len(items) <= 1:
        return items

    try:
        clusters = _near_dup_clusters(
            [_norm_title_for_sim(item.event) for item in items],
            config.DEDUP_NEAR_THRESHOLD,
        )
    except Exception:
        clusters = [[index] for index in range(len(items))]
    try:
        semantic_clusters = _semantic_complete_linkage_clusters(items)
    except Exception:
        semantic_clusters = [[index] for index in range(len(items))]

    # Exact normalized URLs join the same duplicate group even when event
    # wording differs enough to miss the title similarity threshold.
    url_groups: dict[str, list[int]] = {}
    for index, item in enumerate(items):
        normalized_url = _normalize_cross_date_url(item.url or "")
        if normalized_url:
            url_groups.setdefault(normalized_url, []).append(index)

    parent = list(range(len(items)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for cluster in clusters:
        for index in cluster[1:]:
            union(cluster[0], index)
    for cluster in semantic_clusters:
        for index in cluster[1:]:
            union(cluster[0], index)
    for group in url_groups.values():
        for index in group[1:]:
            union(group[0], index)

    merged: dict[int, list[int]] = {}
    for index in range(len(items)):
        merged.setdefault(find(index), []).append(index)

    official_hosts = {
        (urlparse(source.get("url", "")).hostname or "").casefold()
        for source in config.ALL_COMPANY_SOURCES
        if source.get("url")
    }

    def representative_key(index: int) -> tuple:
        item = items[index]
        url = item.url or ""
        host = (urlparse(url).hostname or "").casefold()
        source_score = 1.0
        if any(
            host == official_host or host.endswith("." + official_host)
            for official_host in official_hosts
            if official_host
        ):
            source_score += 1.0
        elif any(domain in url.casefold() for domain in config.DEDUP_HIGH_QUALITY_DOMAINS):
            source_score += 0.5
        elif any(domain in url.casefold() for domain in config.DEDUP_LOW_QUALITY_DOMAINS):
            source_score -= 0.3
        published_date = _filter_date(item) or "9999-12-31"
        return (-source_score, published_date, item.id or index)

    duplicate_group_number = 0
    for group in merged.values():
        group_dates = {_filter_date(items[index]) for index in group}
        group_dates.discard("")
        if len(group) < 2 or len(group_dates) < 2:
            continue
        duplicate_group_number += 1
        ranked_group = sorted(group, key=representative_key)
        group_id = f"cross-date-{duplicate_group_number}"
        for rank, index in enumerate(ranked_group):
            items[index].cross_date_group = group_id
            items[index].cross_date_rank = rank
            items[index].cross_date_duplicate = rank > 0
    return items


def _historical_structured_items(
    db: Session,
    *,
    mark_cross_date_duplicates: bool = False,
) -> list[StructuredNewsRecord]:
    items = _prepare_structured_items(
        db.query(StructuredNewsRecord)
        .join(Run, StructuredNewsRecord.run_id == Run.id)
        .order_by(Run.created_at.desc(), StructuredNewsRecord.id.asc())
        .limit(600)
        .all()
    )
    if mark_cross_date_duplicates:
        return _dedupe_dashboard_structured_items(items)
    return items


def _latest_filter_date(items: list[StructuredNewsRecord]) -> str:
    dates = [getattr(item, "filter_date", _filter_date(item)) for item in items]
    return max((date for date in dates if date), default="")


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    latest = db.query(Run).order_by(Run.created_at.desc()).first()
    structured_items = []
    selected_categories = []
    if latest:
        structured_items = _historical_structured_items(
            db,
            mark_cross_date_duplicates=True,
        )
        selected_categories = json_loads(latest.categories_json, [])
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _template_context(
            request,
            run=latest,
            structured_items=structured_items,
            filter_options=_structured_filter_options(structured_items),
            default_filters={"date": _latest_filter_date(structured_items)},
            selected_categories=selected_categories,
            coverage=json_loads(latest.coverage_json, {}) if latest else {},
            is_live=(latest.status in LIVE_STATUSES) if latest else False,
        ),
    )


@app.head("/")
def index_head():
    return Response(status_code=200)


@app.get("/new", response_class=HTMLResponse)
def new_run(request: Request):
    return templates.TemplateResponse(
        request,
        "new.html",
        _template_context(request),
    )


def _clean_source_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=400, detail="网址格式不正确，请填写完整域名。")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="网址中不能包含账号或密码。")
    return url


def _validate_source_form(name: str, url: str, rss_url: str) -> tuple[str, str, str]:
    name = (name or "").strip()
    url = _clean_source_url(url)
    rss_url = _clean_source_url(rss_url)
    if not name:
        raise HTTPException(status_code=400, detail="信息源名称不能为空。")
    if len(name) > 200:
        raise HTTPException(status_code=400, detail="信息源名称过长（最多 200 字）。")
    if not url:
        raise HTTPException(status_code=400, detail="请填写有效的主页/网址。")
    return name, url, rss_url


def _source_rss_value(source: dict) -> str:
    rss_url = source.get("rss_url", "") or ""
    if rss_url:
        return rss_url
    rss_urls = source.get("rss_urls") or []
    return rss_urls[0] if rss_urls else ""


def _builtin_source_or_404(source_index: int) -> tuple[int, dict]:
    sources = config.get_all_sources()
    if source_index < 0 or source_index >= len(sources):
        raise HTTPException(status_code=404, detail="默认信息源不存在。")
    return source_index, sources[source_index]


def _builtin_override(db: Session, source_key: str) -> CustomSource | None:
    return (
        db.query(CustomSource)
        .filter_by(is_builtin=True, source_key=source_key)
        .first()
    )


def _custom_name_clash(db: Session, name: str, current_id: int | None = None) -> bool:
    query = db.query(CustomSource).filter(CustomSource.name == name)
    if current_id is not None:
        query = query.filter(CustomSource.id != current_id)
    return query.first() is not None


def _validate_source_name_available(
    db: Session,
    name: str,
    *,
    current_id: int | None = None,
    builtin_source_key: str = "",
) -> None:
    builtin_names = {source["name"] for source in config.get_all_sources()}
    if name in builtin_names and name != builtin_source_key:
        raise HTTPException(status_code=409, detail="已存在同名默认信息源，请换一个名称。")
    if _custom_name_clash(db, name, current_id=current_id):
        raise HTTPException(status_code=409, detail="已存在同名信息源，请换一个名称。")


def _upsert_builtin_override(
    db: Session,
    source_index: int,
    *,
    name: str,
    url: str,
    rss_url: str,
    category: str,
    enabled: bool,
    api_key: str | None = None,
) -> CustomSource:
    _, base = _builtin_source_or_404(source_index)
    source_key = base["name"]
    override = _builtin_override(db, source_key)
    current_id = override.id if override else None
    _validate_source_name_available(
        db,
        name,
        current_id=current_id,
        builtin_source_key=source_key,
    )
    if not override:
        override = CustomSource(source_key=source_key, is_builtin=True)
        db.add(override)
    override.name = name
    override.url = url
    override.rss_url = rss_url
    if api_key is not None:
        override.api_key = api_key
    override.category = category
    override.enabled = enabled
    override.updated_at = utcnow()
    return override


def _managed_source_rows(db: Session) -> list[dict]:
    custom_rows = db.query(CustomSource).order_by(CustomSource.id.asc()).all()
    overrides = {
        row.source_key: row
        for row in custom_rows
        if row.is_builtin and row.source_key
    }
    rows = []
    for index, base in enumerate(config.get_all_sources()):
        source_key = base["name"]
        override = overrides.get(source_key)
        if override:
            name = override.name
            url = override.url
            rss_url = override.rss_url
            category = override.category
            enabled = override.enabled
            origin_label = "默认（已修改）" if enabled else "默认（已停用）"
        else:
            name = base["name"]
            url = base["url"]
            rss_url = _source_rss_value(base)
            category = base.get("category", "其他")
            enabled = True
            origin_label = "默认"
        source_type = base.get("type", "web")
        is_search_api = source_type in {"anysearch", "tavily"}
        search_api_name = {
            "anysearch": "AnySearch",
            "tavily": "Tavily",
        }.get(source_type, "")
        search_api_slug = source_type if is_search_api else ""
        saved_search_api_key = (
            override.api_key
            if is_search_api and override and override.api_key
            else ""
        )
        env_key_name = {
            "anysearch": "ANYSEARCH_API_KEY",
            "tavily": "TAVILY_API_KEY",
        }.get(source_type, "")
        rows.append({
            "name": name,
            "url": url,
            "rss_url": rss_url,
            "is_search_api": is_search_api,
            "search_api_name": search_api_name,
            "search_api_slug": search_api_slug,
            "search_api_key": saved_search_api_key,
            "search_api_key_configured": bool(
                is_search_api
                and (saved_search_api_key or os.getenv(env_key_name, "").strip())
            ),
            "category": category,
            "enabled": enabled,
            "origin_label": origin_label,
            "edit_action": f"/sources/default/{index}/edit",
            "toggle_action": f"/sources/default/{index}/toggle",
            "delete_action": f"/sources/default/{index}/delete",
            "delete_label": "删除",
            "delete_confirm": f"确定从抓取中删除默认信息源「{name}」吗？之后可用“启用”恢复。",
        })
    for source in custom_rows:
        if source.is_builtin:
            continue
        rows.append({
            "name": source.name,
            "url": source.url,
            "rss_url": source.rss_url,
            "is_search_api": False,
            "search_api_name": "",
            "search_api_slug": "",
            "search_api_key": "",
            "search_api_key_configured": False,
            "category": source.category,
            "enabled": source.enabled,
            "origin_label": "自定义",
            "edit_action": f"/sources/{source.id}/edit",
            "toggle_action": f"/sources/{source.id}/toggle",
            "delete_action": f"/sources/{source.id}/delete",
            "delete_label": "删除",
            "delete_confirm": f"确定删除自定义信息源「{source.name}」吗？",
        })
    return rows


@app.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request, db: Session = Depends(get_db)):
    source_rows = _managed_source_rows(db)
    return templates.TemplateResponse(
        request,
        "sources.html",
        _template_context(
            request,
            source_rows=source_rows,
            enabled_source_total=sum(1 for source in source_rows if source["enabled"]),
            builtin_total=len(config.get_all_sources()),
        ),
    )


@app.post("/sources")
def create_source(
    name: str = Form(default=""),
    url: str = Form(default=""),
    rss_url: str = Form(default=""),
    category: str = Form(default=""),
    db: Session = Depends(get_db),
):
    name, url, rss_url = _validate_source_form(name, url, rss_url)
    category = (category or "").strip() or "自定义信源"
    _validate_source_name_available(db, name)
    db.add(CustomSource(
        name=name,
        url=url,
        rss_url=rss_url,
        category=category,
        enabled=True,
        is_builtin=False,
        source_key="",
    ))
    db.commit()
    return RedirectResponse(url="/sources", status_code=303)


@app.post("/sources/default/{source_index}/edit")
def edit_builtin_source(
    source_index: int,
    name: str = Form(default=""),
    url: str = Form(default=""),
    rss_url: str = Form(default=""),
    anysearch_api_key: str = Form(default=""),
    clear_anysearch_api_key: str = Form(default=""),
    tavily_api_key: str = Form(default=""),
    clear_tavily_api_key: str = Form(default=""),
    category: str = Form(default=""),
    db: Session = Depends(get_db),
):
    _, base = _builtin_source_or_404(source_index)
    name, url, rss_url = _validate_source_form(name, url, rss_url)
    category = (category or "").strip() or base.get("category", "其他")
    next_api_key = None
    if base.get("type") == "anysearch":
        if clear_anysearch_api_key:
            next_api_key = ""
        elif anysearch_api_key.strip():
            next_api_key = anysearch_api_key.strip()
    elif base.get("type") == "tavily":
        if clear_tavily_api_key:
            next_api_key = ""
        elif tavily_api_key.strip():
            next_api_key = tavily_api_key.strip()
    _upsert_builtin_override(
        db,
        source_index,
        name=name,
        url=url,
        rss_url=rss_url,
        category=category,
        enabled=True,
        api_key=next_api_key,
    )
    db.commit()
    return RedirectResponse(url="/sources", status_code=303)


@app.post("/sources/default/{source_index}/toggle")
def toggle_builtin_source(
    source_index: int,
    db: Session = Depends(get_db),
):
    _, base = _builtin_source_or_404(source_index)
    source_key = base["name"]
    override = _builtin_override(db, source_key)
    if override:
        override.enabled = not override.enabled
        override.updated_at = utcnow()
    else:
        _upsert_builtin_override(
            db,
            source_index,
            name=base["name"],
            url=base["url"],
            rss_url=_source_rss_value(base),
            category=base.get("category", "其他"),
            enabled=False,
        )
    db.commit()
    return RedirectResponse(url="/sources", status_code=303)


@app.post("/sources/default/{source_index}/delete")
def delete_builtin_source(
    source_index: int,
    db: Session = Depends(get_db),
):
    _, base = _builtin_source_or_404(source_index)
    override = _builtin_override(db, base["name"])
    name = override.name if override else base["name"]
    url = override.url if override else base["url"]
    rss_url = override.rss_url if override else _source_rss_value(base)
    category = override.category if override else base.get("category", "其他")
    _upsert_builtin_override(
        db,
        source_index,
        name=name,
        url=url,
        rss_url=rss_url,
        category=category,
        enabled=False,
    )
    db.commit()
    return RedirectResponse(url="/sources", status_code=303)


@app.post("/sources/{source_id}/edit")
def edit_source(
    source_id: int,
    name: str = Form(default=""),
    url: str = Form(default=""),
    rss_url: str = Form(default=""),
    category: str = Form(default=""),
    db: Session = Depends(get_db),
):
    source = db.get(CustomSource, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="信息源不存在。")
    if source.is_builtin:
        raise HTTPException(status_code=400, detail="默认信息源请在默认源入口修改。")
    name, url, rss_url = _validate_source_form(name, url, rss_url)
    category = (category or "").strip() or "自定义信源"
    _validate_source_name_available(db, name, current_id=source_id)
    source.name = name
    source.url = url
    source.rss_url = rss_url
    source.category = category
    source.updated_at = utcnow()
    db.commit()
    return RedirectResponse(url="/sources", status_code=303)


@app.post("/sources/{source_id}/toggle")
def toggle_source(
    source_id: int,
    db: Session = Depends(get_db),
):
    source = db.get(CustomSource, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="信息源不存在。")
    if source.is_builtin:
        raise HTTPException(status_code=400, detail="默认信息源请在默认源入口启停。")
    source.enabled = not source.enabled
    source.updated_at = utcnow()
    db.commit()
    return RedirectResponse(url="/sources", status_code=303)


@app.post("/sources/{source_id}/delete")
def delete_source(
    source_id: int,
    db: Session = Depends(get_db),
):
    source = db.get(CustomSource, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="信息源不存在。")
    if source.is_builtin:
        raise HTTPException(status_code=400, detail="默认信息源请在默认源入口删除。")
    db.delete(source)
    db.commit()
    return RedirectResponse(url="/sources", status_code=303)


@app.post("/runs")
def create_crawl_run(
    categories: List[str] = Form(default=[]),
    provider: str = Form(default=""),
    model: str = Form(default=""),
    custom_model: str = Form(default=""),
    api_key: str = Form(default=""),
):
    selected_model = _selected_model(
        model,
        custom_model,
        required=bool(api_key.strip()),
    )
    run_id = create_run(categories, provider=provider, model=selected_model)
    launched = launch_crawl(run_id, provider, selected_model, api_key)
    if not launched:
        raise HTTPException(status_code=409, detail="该任务已在运行。")
    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)


@app.get("/runs", response_class=HTMLResponse)
def history(request: Request, db: Session = Depends(get_db)):
    runs = db.query(Run).order_by(Run.created_at.desc()).all()
    return templates.TemplateResponse(
        request,
        "history.html",
        _template_context(request, runs=runs),
    )


@app.post("/runs/{run_id}/delete")
def delete_run(
    run_id: str,
    db: Session = Depends(get_db),
):
    run = _get_run_or_404(db, run_id)
    if run.status in LIVE_STATUSES:
        raise HTTPException(status_code=409, detail="任务仍在运行，完成或失败后再删除。")
    db.query(StructuredNewsRecord).filter_by(run_id=run_id).delete()
    db.query(RawNewsItemRecord).filter_by(run_id=run_id).delete()
    db.query(SourceReportRecord).filter_by(run_id=run_id).delete()
    db.query(RunLog).filter_by(run_id=run_id).delete()
    db.delete(run)
    db.commit()
    return RedirectResponse(url="/runs", status_code=303)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: str, db: Session = Depends(get_db)):
    run = _get_run_or_404(db, run_id)
    run_structured_items = _prepare_structured_items(
        db.query(StructuredNewsRecord)
        .filter_by(run_id=run_id)
        .order_by(StructuredNewsRecord.id.asc())
        .all()
    )
    structured_items = _historical_structured_items(db)
    default_date = _latest_filter_date(run_structured_items) or _latest_filter_date(structured_items)
    logs = (
        db.query(RunLog)
        .filter_by(run_id=run_id)
        .order_by(RunLog.id.asc())
        .limit(300)
        .all()
    )
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        _template_context(
            request,
            run=run,
            structured_items=structured_items,
            filter_options=_structured_filter_options(structured_items),
            default_filters={"date": default_date},
            logs=logs,
            selected_categories=json_loads(run.categories_json, []),
            is_live=run.status in LIVE_STATUSES,
        ),
    )


@app.get("/runs/{run_id}/details", response_class=HTMLResponse)
def run_details(request: Request, run_id: str, db: Session = Depends(get_db)):
    run = _get_run_or_404(db, run_id)
    reports = (
        db.query(SourceReportRecord)
        .filter_by(run_id=run_id)
        .order_by(SourceReportRecord.id.asc())
        .all()
    )
    raw_items = (
        db.query(RawNewsItemRecord)
        .filter_by(run_id=run_id)
        .order_by(RawNewsItemRecord.id.asc())
        .limit(200)
        .all()
    )
    logs = (
        db.query(RunLog)
        .filter_by(run_id=run_id)
        .order_by(RunLog.id.asc())
        .limit(500)
        .all()
    )
    return templates.TemplateResponse(
        request,
        "run_details_full.html",
        _template_context(
            request,
            run=run,
            reports=reports,
            raw_items=raw_items,
            logs=logs,
            coverage=json_loads(run.coverage_json, {}),
            selected_categories=json_loads(run.categories_json, []),
        ),
    )


@app.post("/runs/{run_id}/analyze")
def analyze_run(
    run_id: str,
    provider: str = Form(default=""),
    model: str = Form(default=""),
    custom_model: str = Form(default=""),
    api_key: str = Form(default=""),
    db: Session = Depends(get_db),
):
    run = _get_run_or_404(db, run_id)
    if run.status in LIVE_STATUSES:
        raise HTTPException(status_code=409, detail="任务仍在运行，请稍后再试。")
    if run.raw_count <= 0:
        raise HTTPException(status_code=400, detail="没有可分析的原始条目，请先完成网页爬取。")
    selected_model = _selected_model(model, custom_model, required=True)
    launched = launch_analyze(run_id, provider, selected_model, api_key)
    if not launched:
        raise HTTPException(status_code=409, detail="该任务已在运行。")
    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)


@app.get("/runs/{run_id}/events")
async def run_events(run_id: str):
    async def event_stream():
        last_log_id = 0
        while True:
            db = SessionLocal()
            try:
                run = db.get(Run, run_id)
                if not run:
                    payload = {"type": "error", "message": "任务不存在。", "done": True}
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    break
                logs = (
                    db.query(RunLog)
                    .filter(RunLog.run_id == run_id, RunLog.id > last_log_id)
                    .order_by(RunLog.id.asc())
                    .limit(100)
                    .all()
                )
                for log in logs:
                    last_log_id = log.id
                    payload = {
                        "type": "log",
                        "id": log.id,
                        "message": log.message,
                    }
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

                done = run.status not in LIVE_STATUSES
                payload = {
                    "type": "status",
                    "status": run.status,
                    "label": STATUS_LABELS.get(run.status, run.status),
                    "phase": run.phase,
                    "raw_count": run.raw_count,
                    "structured_count": run.structured_count,
                    "error_text": run.error_text,
                    "done": done,
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                if done:
                    break
            finally:
                db.close()
            await asyncio.sleep(1)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/healthz")
def healthz(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"ok": True, "database": "reachable"}


# ──────────────────────────────────────────────────────────────────────────
# PDF 报告导出
# ──────────────────────────────────────────────────────────────────────────

@app.get("/pdf-report")
def pdf_report(db: Session = Depends(get_db)):
    """导出当日结构化新闻 PDF 报告，触发浏览器下载。"""
    import io
    from datetime import datetime
    from app.pdf_report import generate_pdf_report

    latest = db.query(Run).order_by(Run.created_at.desc()).first()

    # 获取最新任务的已结构化新闻
    items: list[StructuredNewsRecord] = []
    if latest:
        items = _prepare_structured_items(
            db.query(StructuredNewsRecord)
            .filter_by(run_id=latest.id)
            .order_by(StructuredNewsRecord.id.asc())
            .all()
        )

    # 若最新任务暂无结构化结果，回退到最近有结果的任务
    if not items:
        latest_with_items = (
            db.query(Run)
            .filter(Run.structured_count > 0)
            .order_by(Run.created_at.desc())
            .first()
        )
        if latest_with_items:
            items = _prepare_structured_items(
                db.query(StructuredNewsRecord)
                .filter_by(run_id=latest_with_items.id)
                .order_by(StructuredNewsRecord.id.asc())
                .all()
            )
            latest = latest_with_items

    start, end = get_strict_window()
    pdf_bytes = generate_pdf_report(
        latest,
        items,
        window_start=start.strftime("%Y-%m-%d %H:%M"),
        window_end=end.strftime("%Y-%m-%d %H:%M"),
    )

    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    filename = f"AI-Daily-News-Report-{date_str}.pdf"

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
