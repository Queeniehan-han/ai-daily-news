from __future__ import annotations

import os
import sys
import time
from datetime import date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import config
from app import jobs
from app.db import Base
from app.models import DailyCrawlClaim, Run, RunLog


passed = 0
failed = 0


def check(name: str, condition: bool) -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS {name}")
    else:
        failed += 1
        print(f"  FAIL {name}")


print("=== 1. 北京时间 11:00 触发边界 ===")
before = datetime(2026, 8, 1, 10, 59, 59, tzinfo=config.CST)
at_time = datetime(2026, 8, 1, 11, 0, 0, tzinfo=config.CST)
after = datetime(2026, 8, 1, 23, 30, 0, tzinfo=config.CST)
check("11:00 前不触发", jobs.scheduled_crawl_date(before) is None)
check("11:00 准时触发", jobs.scheduled_crawl_date(at_time) == date(2026, 8, 1))
check("11:00 后支持当日补跑", jobs.scheduled_crawl_date(after) == date(2026, 8, 1))


print("=== 2. 数据库幂等声明 ===")
engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)
Base.metadata.create_all(bind=engine)
original_session_local = jobs.SessionLocal
jobs.SessionLocal = TestingSessionLocal
try:
    first_run_id = jobs.claim_daily_crawl(date(2026, 8, 1))
    second_run_id = jobs.claim_daily_crawl(date(2026, 8, 1))
    third_run_id = jobs.claim_daily_crawl(date(2026, 8, 2))

    db = TestingSessionLocal()
    try:
        claims = db.query(DailyCrawlClaim).order_by(
            DailyCrawlClaim.scheduled_date.asc()
        ).all()
        runs = db.query(Run).order_by(Run.created_at.asc()).all()
        logs = db.query(RunLog).all()
    finally:
        db.close()

    check("同一天只创建一个自动任务", bool(first_run_id) and second_run_id is None)
    check("不同日期可分别创建任务", bool(third_run_id) and third_run_id != first_run_id)
    check("声明表按日期保留两行", len(claims) == 2)
    check("重复声明不会产生孤立任务", len(runs) == 2)
    check("每个自动任务写入创建日志", len(logs) == 2)
    check("自动任务不保存模型配置",
          all(not run.provider and not run.model for run in runs))
finally:
    jobs.SessionLocal = original_session_local
    engine.dispose()


print("=== 3. 自动任务严格禁止模型分析 ===")
original_claim = jobs.claim_daily_crawl
original_launch = jobs.launch_crawl
launch_calls = []
try:
    jobs.claim_daily_crawl = lambda _scheduled_date: "scheduled-test-run"
    jobs.launch_crawl = lambda run_id, provider="", model="", api_key="": (
        launch_calls.append((run_id, provider, model, api_key)) or True
    )
    launched_run_id = jobs.run_daily_crawl_scheduler_tick(after)
    check("到点后启动自动任务", launched_run_id == "scheduled-test-run")
    check("Provider、模型和 API Key 均为空",
          launch_calls == [("scheduled-test-run", "", "", "")])

    launch_calls.clear()
    check("未到点不创建或启动任务",
          jobs.run_daily_crawl_scheduler_tick(before) is None
          and launch_calls == [])
finally:
    jobs.claim_daily_crawl = original_claim
    jobs.launch_crawl = original_launch


print("=== 4. 调度线程生命周期 ===")
original_tick = jobs.run_daily_crawl_scheduler_tick
original_scheduled_crawl_date = jobs.scheduled_crawl_date
original_poll_seconds = jobs.DAILY_CRAWL_POLL_SECONDS
original_enabled = os.environ.get("DAILY_CRAWL_ENABLED")
tick_calls = []
try:
    jobs.scheduled_crawl_date = lambda now=None: date(2026, 8, 1)
    jobs.run_daily_crawl_scheduler_tick = lambda now=None: tick_calls.append(now)
    jobs.DAILY_CRAWL_POLL_SECONDS = 0.01
    os.environ["DAILY_CRAWL_ENABLED"] = "1"
    jobs.stop_daily_crawl_scheduler()
    started = jobs.start_daily_crawl_scheduler()
    duplicate_start = jobs.start_daily_crawl_scheduler()
    time.sleep(0.03)
    stopped = jobs.stop_daily_crawl_scheduler()
    duplicate_stop = jobs.stop_daily_crawl_scheduler()
    check("调度线程可启动和停止", started and stopped)
    check("同一进程不会重复启动调度线程", not duplicate_start)
    check("同一天只执行一次数据库调度检查", len(tick_calls) == 1)
    check("重复停止安全返回", not duplicate_stop)

    os.environ["DAILY_CRAWL_ENABLED"] = "0"
    check("环境变量可临时关闭调度器",
          jobs.start_daily_crawl_scheduler() is False)
finally:
    jobs.stop_daily_crawl_scheduler()
    jobs.run_daily_crawl_scheduler_tick = original_tick
    jobs.scheduled_crawl_date = original_scheduled_crawl_date
    jobs.DAILY_CRAWL_POLL_SECONDS = original_poll_seconds
    if original_enabled is None:
        os.environ.pop("DAILY_CRAWL_ENABLED", None)
    else:
        os.environ["DAILY_CRAWL_ENABLED"] = original_enabled


print(f"\n===== 结果：{passed} 通过 / {failed} 失败 =====")
sys.exit(0 if failed == 0 else 1)
