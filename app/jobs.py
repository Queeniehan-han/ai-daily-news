from __future__ import annotations

import json
import logging
import os
import threading
import traceback
import uuid
from dataclasses import asdict
from datetime import date, datetime
from typing import Iterable, List, Optional

from sqlalchemy.exc import IntegrityError

import config
from app.db import SessionLocal
from app.models import (
    CustomSource,
    DailyCrawlClaim,
    RawNewsItemRecord,
    Run,
    RunLog,
    SourceReportRecord,
    StructuredNewsRecord,
    utcnow,
)
from llm_client import LLMClient
from news_processor import NewsProcessor, StructuredNews, build_coverage_summary
from scraper import NewsItem, Scraper, SourceReport


_ACTIVE_LOCK = threading.Lock()
_ACTIVE_JOBS: set[str] = set()
_SCHEDULER_LOCK = threading.Lock()
_SCHEDULER_STOP_EVENT: Optional[threading.Event] = None
_SCHEDULER_THREAD: Optional[threading.Thread] = None
_LOGGER = logging.getLogger(__name__)

DAILY_CRAWL_HOUR = 11
DAILY_CRAWL_MINUTE = 0
DAILY_CRAWL_POLL_SECONDS = 30.0


def json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def json_loads(value: str, fallback):
    try:
        return json.loads(value or "")
    except Exception:
        return fallback


def create_run(categories: Iterable[str], provider: str = "", model: str = "") -> str:
    run_id = uuid.uuid4().hex[:16]
    db = SessionLocal()
    try:
        run = Run(
            id=run_id,
            status="pending",
            phase="created",
            provider=provider or "",
            model=model or "",
            categories_json=json_dumps(list(categories)),
        )
        db.add(run)
        db.commit()
    finally:
        db.close()
    append_log(run_id, "任务已创建。")
    return run_id


def scheduled_crawl_date(now: Optional[datetime] = None) -> Optional[date]:
    """Return the CST date whose 11:00 automatic crawl is currently due."""
    current = now or datetime.now(config.CST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=config.CST)
    else:
        current = current.astimezone(config.CST)
    scheduled_at = current.replace(
        hour=DAILY_CRAWL_HOUR,
        minute=DAILY_CRAWL_MINUTE,
        second=0,
        microsecond=0,
    )
    return current.date() if current >= scheduled_at else None


def claim_daily_crawl(scheduled_date: date) -> Optional[str]:
    """Atomically create one scheduled crawl run per CST calendar date."""
    date_key = scheduled_date.isoformat()
    run_id = uuid.uuid4().hex[:16]
    db = SessionLocal()
    try:
        db.add(Run(
            id=run_id,
            status="pending",
            phase="scheduled",
            provider="",
            model="",
            categories_json="[]",
        ))
        db.add(DailyCrawlClaim(
            scheduled_date=date_key,
            run_id=run_id,
        ))
        db.commit()
    except IntegrityError:
        db.rollback()
        if db.get(DailyCrawlClaim, date_key):
            return None
        raise
    finally:
        db.close()
    append_log(run_id, f"北京时间 {date_key} 11:00 自动抓取任务已创建。")
    return run_id


def append_log(run_id: str, message: str) -> None:
    db = SessionLocal()
    try:
        db.add(RunLog(run_id=run_id, message=message))
        run = db.get(Run, run_id)
        if run:
            run.updated_at = utcnow()
        db.commit()
    finally:
        db.close()


def _set_run(run_id: str, **fields) -> None:
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        if not run:
            return
        for key, value in fields.items():
            setattr(run, key, value)
        run.updated_at = utcnow()
        db.commit()
    finally:
        db.close()


def _safe_error_message(exc: BaseException) -> str:
    msg = str(exc).strip()
    return msg or exc.__class__.__name__


def _source_categories_for_run(run_id: str) -> List[str]:
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        if not run:
            return []
        return json_loads(run.categories_json, [])
    finally:
        db.close()


def _save_crawl_result(run_id: str, raw_items: List[NewsItem],
                       reports: List[SourceReport], coverage: dict,
                       status: str = "crawled",
                       phase: str = "crawl_done") -> None:
    db = SessionLocal()
    try:
        db.query(RawNewsItemRecord).filter_by(run_id=run_id).delete()
        db.query(SourceReportRecord).filter_by(run_id=run_id).delete()

        for item in raw_items:
            payload = item.to_dict()
            db.add(RawNewsItemRecord(
                run_id=run_id,
                title=item.title or "",
                url=item.url or "",
                source=item.source or "",
                category=item.category or "",
                content=item.content or "",
                published_at=item.published_at or "",
                scrape_strategy=item.scrape_strategy or "",
                payload_json=json_dumps(payload),
            ))

        for report in reports:
            payload = asdict(report)
            db.add(SourceReportRecord(
                run_id=run_id,
                name=report.name or "",
                category=report.category or "",
                source_type=report.type or "",
                count=report.count or 0,
                strategy=report.strategy or "",
                status=report.status or "",
                error=report.error or "",
                issue_type=getattr(report, "issue_type", "") or "",
                completeness=getattr(report, "completeness", "") or "",
                latest_seen_at=getattr(report, "latest_seen_at", "") or "",
                oldest_seen_at=getattr(report, "oldest_seen_at", "") or "",
                payload_json=json_dumps(payload),
            ))

        run = db.get(Run, run_id)
        if run:
            run.status = status
            run.phase = phase
            run.raw_count = len(raw_items)
            run.report_count = len(reports)
            run.coverage_json = json_dumps(coverage)
            run.error_text = ""
            run.updated_at = utcnow()
        db.commit()
    finally:
        db.close()


def _load_raw_items(run_id: str) -> List[NewsItem]:
    db = SessionLocal()
    try:
        rows = (
            db.query(RawNewsItemRecord)
            .filter_by(run_id=run_id)
            .order_by(RawNewsItemRecord.id.asc())
            .all()
        )
        return [
            NewsItem(
                title=row.title,
                url=row.url,
                source=row.source,
                category=row.category,
                content=row.content,
                published_at=row.published_at or None,
                scrape_strategy=row.scrape_strategy,
            )
            for row in rows
        ]
    finally:
        db.close()


def _save_structured_result(run_id: str, structured: List[StructuredNews],
                            provider: str, model: str) -> None:
    db = SessionLocal()
    try:
        db.query(StructuredNewsRecord).filter_by(run_id=run_id).delete()
        for item in structured:
            payload = item.to_dict()
            db.add(StructuredNewsRecord(
                run_id=run_id,
                event=item.event or "",
                detail=item.detail or "",
                impact=item.impact or "",
                news_type=item.news_type or "",
                company=item.company or "",
                source=item.source or "",
                url=item.url or "",
                published_at=item.published_at or "",
                category=item.category or "",
                payload_json=json_dumps(payload),
            ))
        run = db.get(Run, run_id)
        if run:
            run.status = "completed"
            run.phase = "analyze_done"
            run.provider = provider
            run.model = model
            run.structured_count = len(structured)
            run.error_text = ""
            run.updated_at = utcnow()
        db.commit()
    finally:
        db.close()


def _load_custom_source_rows() -> List[CustomSource]:
    db = SessionLocal()
    try:
        return db.query(CustomSource).order_by(CustomSource.id.asc()).all()
    finally:
        db.close()


def _merge_builtin_override(base: dict, override: CustomSource) -> dict:
    source = dict(base)
    source["name"] = override.name
    source["url"] = override.url
    source["category"] = override.category or base.get("category", "自定义信源")
    if base.get("type") in {"anysearch", "tavily"} and getattr(override, "api_key", ""):
        source["api_key"] = override.api_key
    if override.rss_url:
        source["rss_url"] = override.rss_url
        source.pop("rss_urls", None)
    else:
        source.pop("rss_url", None)
        source.pop("rss_urls", None)
    if override.url != base.get("url"):
        source.pop("crawl_url", None)
        source.pop("allowed_hosts", None)
    return source


def _all_sources_with_custom() -> List[dict]:
    """PRD 固定信源 + 默认源覆盖配置 + 用户自定义信源。"""
    rows = _load_custom_source_rows()
    overrides = {
        row.source_key: row
        for row in rows
        if getattr(row, "is_builtin", False) and getattr(row, "source_key", "")
    }
    custom_rows = [row for row in rows if not getattr(row, "is_builtin", False)]

    sources: List[dict] = []
    seen: set[str] = set()
    for base in config.get_all_sources():
        source_key = base["name"]
        override = overrides.get(source_key)
        if override:
            if not override.enabled:
                continue
            source = _merge_builtin_override(base, override)
        else:
            source = dict(base)
        if source["name"] not in seen:
            seen.add(source["name"])
            sources.append(source)

    for custom in custom_rows:
        if custom.enabled and custom.name not in seen:
            seen.add(custom.name)
            sources.append(custom.to_source_dict())
    return sources


def _filter_sources(categories: List[str]) -> List[dict]:
    sources = _all_sources_with_custom()
    if categories:
        selected = set(categories)
        sources = [s for s in sources if s.get("category") in selected]
    return sources


def run_crawl(
    run_id: str,
    provider: str = "",
    model: str = "",
    api_key: str = "",
) -> None:
    categories = _source_categories_for_run(run_id)
    _set_run(run_id, status="running", phase="crawl")
    append_log(run_id, "开始网页爬取。")
    try:
        sources = _filter_sources(categories)
        if not sources:
            raise ValueError("当前板块筛选下没有任何信息源，请调整筛选条件。")

        append_log(run_id, f"本轮信息源数量：{len(sources)}。")
        scraper = Scraper(log_fn=lambda msg: append_log(run_id, msg))

        def on_progress(done: int, total: int, name: str) -> None:
            _set_run(run_id, phase=f"crawl {done}/{total}")
            append_log(run_id, f"抓取进度 {done}/{total} · {name}")

        raw_items, reports = scraper.run_all(sources, progress_fn=on_progress)
        coverage = build_coverage_summary(reports, sources)
        will_analyze = bool(api_key.strip())
        _save_crawl_result(
            run_id,
            raw_items,
            reports,
            coverage,
            status="running" if will_analyze else "crawled",
            phase="crawl_done_pending_analyze" if will_analyze else "crawl_done",
        )
        append_log(
            run_id,
            f"抓取完成：{len(raw_items)} 条原始条目，"
            f"覆盖 {len(reports)}/{coverage.get('expected_total', len(sources))} 个信息源。",
        )
        append_log(run_id, "系统提示：信息抓取任务完成。")
        if will_analyze:
            append_log(run_id, "已填写 API Key，自动进入 AI 结构化分析。")
            run_analyze(run_id, provider, model, api_key)
    except Exception as exc:  # noqa: BLE001
        message = _safe_error_message(exc)
        _set_run(run_id, status="failed", phase="crawl_failed", error_text=message)
        append_log(run_id, f"抓取失败：{message}")
        append_log(run_id, traceback.format_exc(limit=3))


def _resolve_model_config(provider: str, model: str, api_key: str) -> tuple[str, str, str]:
    provider = (provider or os.getenv("MODEL_PROVIDER") or config.DEFAULT_PROVIDER).strip()
    if provider not in config.LLM_PROVIDERS:
        provider = config.DEFAULT_PROVIDER
    conf = config.LLM_PROVIDERS[provider]
    model = (model or os.getenv("MODEL_NAME") or conf["default_model"]).strip()
    api_key = (api_key or os.getenv("MODEL_API_KEY") or "").strip()
    if not api_key:
        raise ValueError("请填写 API Key，或在部署环境变量中配置 MODEL_API_KEY。")
    return provider, model, api_key


def run_analyze(run_id: str, provider: str, model: str, api_key: str) -> None:
    try:
        provider, model, api_key = _resolve_model_config(provider, model, api_key)
    except Exception as exc:  # noqa: BLE001
        message = _safe_error_message(exc)
        _set_run(run_id, status="crawled", phase="analyze_failed", error_text=message)
        append_log(run_id, f"分析未启动：{message}")
        return

    _set_run(run_id, status="analyzing", phase="analyze", provider=provider, model=model)
    append_log(run_id, f"开始 AI 结构化分析：{provider} / {model}。")
    try:
        raw_items = _load_raw_items(run_id)
        if not raw_items:
            raise ValueError("没有可分析的原始条目，请先完成网页爬取。")
        client = LLMClient(provider, api_key, model)
        processor = NewsProcessor(client, log_fn=lambda msg: append_log(run_id, msg))
        structured = processor.process(raw_items)
        _save_structured_result(run_id, structured, provider, model)
        append_log(run_id, f"分析完成：产出 {len(structured)} 条结构化新闻。")
        append_log(run_id, "系统提示：新闻分析完成。")
    except Exception as exc:  # noqa: BLE001
        message = _safe_error_message(exc)
        _set_run(run_id, status="crawled", phase="analyze_failed", error_text=message)
        append_log(run_id, f"分析失败：{message}")
        append_log(run_id, traceback.format_exc(limit=3))


def _launch(run_id: str, target, *args) -> bool:
    with _ACTIVE_LOCK:
        if run_id in _ACTIVE_JOBS:
            return False
        _ACTIVE_JOBS.add(run_id)

    def wrapped() -> None:
        try:
            target(run_id, *args)
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE_JOBS.discard(run_id)

    thread = threading.Thread(target=wrapped, daemon=True)
    thread.start()
    return True


def launch_crawl(
    run_id: str,
    provider: str = "",
    model: str = "",
    api_key: str = "",
) -> bool:
    return _launch(run_id, run_crawl, provider, model, api_key)


def launch_analyze(run_id: str, provider: str, model: str, api_key: str) -> bool:
    return _launch(run_id, run_analyze, provider, model, api_key)


def run_daily_crawl_scheduler_tick(
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Launch the due automatic crawl without ever starting LLM analysis."""
    due_date = scheduled_crawl_date(now)
    if due_date is None:
        return None
    run_id = claim_daily_crawl(due_date)
    if run_id is None:
        return None

    # Keep all model arguments empty. run_crawl only chains into analysis when
    # an explicit API key is passed, regardless of MODEL_API_KEY in the env.
    launched = launch_crawl(run_id, "", "", "")
    if launched:
        return run_id

    _set_run(
        run_id,
        status="failed",
        phase="schedule_launch_failed",
        error_text="自动抓取任务启动失败，请检查服务运行状态。",
    )
    append_log(run_id, "自动抓取任务启动失败。")
    return None


def _daily_crawl_scheduler_loop(stop_event: threading.Event) -> None:
    _LOGGER.info("每日自动抓取调度器已启动：北京时间 11:00，仅抓取不分析。")
    checked_date: Optional[date] = None
    while not stop_event.is_set():
        due_date = scheduled_crawl_date()
        if due_date is not None and due_date != checked_date:
            try:
                run_id = run_daily_crawl_scheduler_tick()
                checked_date = due_date
                if run_id:
                    _LOGGER.info("已启动每日自动抓取任务：%s", run_id)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("每日自动抓取调度检查失败，将在下一轮重试。")
        stop_event.wait(DAILY_CRAWL_POLL_SECONDS)


def start_daily_crawl_scheduler() -> bool:
    """Start one scheduler thread for this application process."""
    enabled = os.getenv("DAILY_CRAWL_ENABLED", "1").strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        _LOGGER.info("每日自动抓取调度器已通过 DAILY_CRAWL_ENABLED 关闭。")
        return False

    global _SCHEDULER_STOP_EVENT, _SCHEDULER_THREAD
    with _SCHEDULER_LOCK:
        if _SCHEDULER_THREAD and _SCHEDULER_THREAD.is_alive():
            return False
        _SCHEDULER_STOP_EVENT = threading.Event()
        _SCHEDULER_THREAD = threading.Thread(
            target=_daily_crawl_scheduler_loop,
            args=(_SCHEDULER_STOP_EVENT,),
            name="daily-crawl-scheduler",
            daemon=True,
        )
        _SCHEDULER_THREAD.start()
        return True


def stop_daily_crawl_scheduler() -> bool:
    """Stop the in-process scheduler during graceful application shutdown."""
    global _SCHEDULER_STOP_EVENT, _SCHEDULER_THREAD
    with _SCHEDULER_LOCK:
        stop_event = _SCHEDULER_STOP_EVENT
        thread = _SCHEDULER_THREAD
        if not stop_event or not thread:
            return False
        stop_event.set()
    if thread is not threading.current_thread():
        thread.join(timeout=5.0)
    with _SCHEDULER_LOCK:
        if _SCHEDULER_THREAD is thread:
            _SCHEDULER_STOP_EVENT = None
            _SCHEDULER_THREAD = None
    return True


def mark_interrupted_runs() -> None:
    """Fail stale in-process jobs after a deploy/restart.

    Hosted platforms can restart the web process during deploys. Threads do not
    survive that, so old running/analyzing rows must not stay forever in progress.
    """
    db = SessionLocal()
    try:
        stale = (
            db.query(Run)
            .filter(Run.status.in_(["pending", "running", "analyzing"]))
            .all()
        )
        for run in stale:
            run.status = "failed"
            run.phase = "interrupted"
            run.error_text = "服务重启或重新部署导致任务中断，请重新发起。"
            run.updated_at = utcnow()
            db.add(RunLog(run_id=run.id, message=run.error_text))
        db.commit()
    finally:
        db.close()
