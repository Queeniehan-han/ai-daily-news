# -*- coding: utf-8 -*-
"""run_analysis.py — 爬取 + LLM 结构化分析，一步完成"""
from __future__ import annotations

import json
import os
import sys
import time

import config
from scraper import Scraper
from llm_client import LLMClient
from news_processor import NewsProcessor

API_KEY = os.getenv("MODEL_API_KEY", "").strip()
PROVIDER = "Bytedance ModelHub"
MODEL = "gemini-3.5-flash"

t0 = time.time()
print("=" * 60)
print(f"步骤 1: 全量爬取（{len(config.get_all_sources())} 源）…")
print("=" * 60)

sc = Scraper(log_fn=lambda m: print(m, flush=True))
items, reports = sc.run_all(
    progress_fn=lambda d, t, n: print(f"[{d}/{t}] {n}", flush=True))

print(f"\n爬取完成: {len(items)} 条原始条目")
print(f"耗时: {time.time()-t0:.0f}s")

print("\n" + "=" * 60)
print("步骤 2: LLM 结构化分析…")
print("=" * 60)

if not API_KEY:
    raise RuntimeError("请先通过 MODEL_API_KEY 环境变量配置模型 Key。")
client = LLMClient(PROVIDER, API_KEY, MODEL)
processor = NewsProcessor(client)
structured = processor.process(items)

t1 = time.time()
print(f"\n分析完成: {len(structured)} 条结构化新闻")
print(f"LLM 耗时: {t1-t0:.0f}s")

# 按分类分组
from collections import defaultdict
by_type = defaultdict(list)
for sn in structured:
    by_type[sn.news_type].append(sn)

# 输出 JSON
output = {
    "window": f"{sc._window_start.isoformat() if hasattr(sc, '_window_start') else 'N/A'}",
    "total_raw": len(items),
    "total_structured": len(structured),
    "by_type": {k: len(v) for k, v in by_type.items()},
    "news": [sn.to_dict() for sn in structured],
}

with open("analysis_results.json", "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f"\n结果已保存至 analysis_results.json")
print(f"总耗时: {time.time()-t0:.0f}s")
print("DONE")
