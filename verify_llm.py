# -*- coding: utf-8 -*-
"""verify_llm.py — LLM 结构化分析端到端验证（用 PRD 样例 Key + gemini-3.5-flash）。"""
from __future__ import annotations

import json
import os

from llm_client import LLMClient
from news_processor import NewsProcessor
from scraper import NewsItem

API_KEY = os.getenv("MODEL_API_KEY", "").strip()

samples = [
    NewsItem(title="Google just fired a warning shot in the AI subscription price wars",
             url="https://techcrunch.com/2026/06/09/google-ai-price/",
             source="TechCrunch (AI)", category="海外科技媒体",
             content="Google cut the price of its AI subscription tiers, pressuring "
                     "OpenAI and Anthropic to respond in the consumer AI market."),
    NewsItem(title="Anthropic's Claude Fable 5 is a version of Mythos the public can access",
             url="https://techcrunch.com/2026/06/09/anthropic-fable-5/",
             source="TechCrunch (AI)", category="海外科技媒体",
             content="Anthropic released Claude Fable 5, a new frontier model focused on "
                     "interactive generation, available to the public."),
    NewsItem(title="Rivian starts deliveries of its all-important R2 SUV",
             url="https://techcrunch.com/2026/06/09/rivian-r2/",
             source="TechCrunch (AI)", category="海外科技媒体",
             content="Rivian began deliveries of the R2 electric SUV."),  # 非 AI，应被过滤
]

if not API_KEY:
    raise RuntimeError("请先通过 MODEL_API_KEY 环境变量配置模型 Key。")
client = LLMClient("Bytedance ModelHub", API_KEY, "gemini-3.5-flash")
print("PING:", client.ping())

proc = NewsProcessor(client, log_fn=lambda m: print(m, flush=True))
results = proc.process(samples)
print(f"\nSTRUCTURED_COUNT {len(results)}")
for r in results:
    print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))

ai_urls = {r.url for r in results}
print("非AI条目(Rivian)被过滤:", "https://techcrunch.com/2026/06/09/rivian-r2/" not in ai_urls)
print("VERDICT", "PASS" if results and all(r.event and r.detail and r.impact for r in results) else "FAIL")
