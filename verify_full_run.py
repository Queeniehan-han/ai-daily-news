# -*- coding: utf-8 -*-
"""verify_full_run.py — 第三重检查：全量实跑验证

验收标准（来自项目验证纪律）：
  - REPORT_ROWS == 配置内信源总数（每个启用信源都有报告行，不存在遗漏）
  - ALL_COVERED == True
  - WINDOW_VIOLATIONS == 0（无窗口外条目泄漏）
  - 抽样内容质量检查（不只看计数）

运行：
  /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 -u verify_full_run.py
"""
from __future__ import annotations

import time
from datetime import timedelta

from dateutil import parser as dtp

import config
from scraper import Scraper, get_strict_window
from news_processor import dedupe

t0 = time.time()
start, end = get_strict_window()
print(f"窗口: {start} → {end}")
print(f"信源总数: {len(config.get_all_sources())} "
      f"(web={len(config.WEB_SOURCES)} kol={len(config.X_KOL_SOURCES)})")

sc = Scraper(log_fn=lambda m: print(m, flush=True))
items, reports = sc.run_all(
    progress_fn=lambda d, t, n: print(f"[{d}/{t}] {n}", flush=True))

from scraper import verify_coverage
cov = verify_coverage(reports)

print("\n================ 验收指标 ================")
print(f"SOURCE_COUNTS web={len(config.WEB_SOURCES)} "
      f"kol={len(config.X_KOL_SOURCES)} total={len(config.get_all_sources())}")
print(f"REPORT_ROWS {cov['report_rows']}")
print(f"ALL_COVERED {cov['all_covered']}")
print(f"MISSING {cov['missing_sources']}")
print(f"RAW_ITEMS {len(items)}  (去重后 {len(dedupe(items))})")
print(f"SUCCESS {cov['success_count']} / EMPTY {cov['empty_count']} "
      f"/ ERROR+TIMEOUT {cov['error_count']}")

# 时间窗口合规审计（用户 #1 不变量）
violations = 0
no_ts = 0
for it in items:
    if not it.published_at:
        no_ts += 1
        continue
    dt = dtp.parse(it.published_at)
    if not (start <= dt <= end + timedelta(hours=config.WINDOW_GRACE_HOURS)):
        violations += 1
        print(f"  VIOLATION: {it.source} | {it.title[:60]} ({dt})")
print(f"WINDOW_VIOLATIONS {violations}")
print(f"NO_TIMESTAMP_ITEMS {no_ts}  ← 严格模式下必须为 0")

# 内容质量抽样（不只看计数）
print("\n================ 内容抽样（前 12 条）================")
for it in items[:12]:
    print(f"  [{it.scrape_strategy}] {it.source} | {it.title[:70]}")
    print(f"      {it.url[:90]}  @{it.published_at}")

# 各信源明细
print("\n================ 各信源明细 ================")
for r in reports:
    mark = {"success": "✅", "empty": "⚪", "error": "❌", "timeout": "⏱"}.get(r.status, "?")
    print(f"  {mark} [{r.strategy or '-':>11}] {r.name:<40} {r.count:>3} 条  {r.error[:60]}")

print(f"\n总耗时 {time.time()-t0:.0f}s")
ok = (cov["report_rows"] == len(config.get_all_sources())
      and cov["all_covered"] and violations == 0 and no_ts == 0)
print("VERDICT", "PASS" if ok else "FAIL")
