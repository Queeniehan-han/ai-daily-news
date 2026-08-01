# -*- coding: utf-8 -*-
"""verify_semantic_dedup.py — 语义去重端到端验证（真实 LLM）。

构造含语义重复的原始条目（同一事件不同措辞 + 不同信源），跑完整 process()
流水线，验证：1) 语义重复被合并且保留官方源代表稿；2) 不同事件不被误合并。
"""
from __future__ import annotations

import json
import os

from llm_client import LLMClient
from news_processor import NewsProcessor
from scraper import NewsItem

API_KEY = os.getenv("MODEL_API_KEY", "").strip()

samples = [
    # 语义重复对 1：同一事件（Fable 5 发布），不同措辞不同信源
    NewsItem(title="Anthropic's Fable 5 can make weirdly fun video games",
             url="https://techcrunch.com/2026/06/09/anthropic-fable-5-games/",
             source="TechCrunch (AI)", category="海外科技媒体",
             content="Anthropic released Fable 5, a new frontier model that can "
                     "generate fun playable video games with one click."),
    NewsItem(title="Anthropic 正式发布 Claude Fable 5 大模型，公众即日可用",
             url="https://www.anthropic.com/news/claude-fable-5",
             source="Anthropic", category="公司-大模型企业",
             content="Anthropic 今日发布新一代前沿大模型 Claude Fable 5，"
                     "支持交互式内容生成，即日起向公众开放使用。"),
    # 独立事件 2：谷歌降价（不应被合并）
    NewsItem(title="Google fired a warning shot in the AI subscription price wars",
             url="https://techcrunch.com/2026/06/09/google-ai-price/",
             source="TechCrunch (AI)", category="海外科技媒体",
             content="Google cut the price of its AI subscription tiers, "
                     "pressuring OpenAI and Anthropic in the consumer market."),
    # 独立事件 3：Anthropic IPO（同公司不同事件，不应与 Fable 5 合并）
    NewsItem(title="Anthropic files confidentially for IPO",
             url="https://techcrunch.com/2026/06/09/anthropic-ipo/",
             source="TechCrunch (AI)", category="海外科技媒体",
             content="Anthropic has confidentially filed for an initial public "
                     "offering, following rapid revenue growth."),
]

if not API_KEY:
    raise RuntimeError("请先通过 MODEL_API_KEY 环境变量配置模型 Key。")
client = LLMClient("Bytedance ModelHub", API_KEY, "gemini-3.5-flash")
proc = NewsProcessor(client, log_fn=lambda m: print(m, flush=True))
results = proc.process(samples)

print(f"\nSTRUCTURED_COUNT {len(results)}（输入 4 条原始，含 1 对语义重复）")
for r in results:
    print(f"  - [{r.news_type}] {r.event}  ({r.url[:60]})")

fable_count = sum(1 for r in results if "fable" in (r.event + r.url).lower()
                  or "fable" in r.detail.lower())
kept_official = any("anthropic.com" in r.url for r in results)
has_google = any("谷歌" in (r.event + r.detail) or "google" in
                 (r.event + r.detail + r.url).lower() for r in results)
has_ipo = any("ipo" in (r.event + r.detail).lower() for r in results)

print(f"\nFable5 相关条数（应为 1）: {fable_count}")
print(f"代表稿为官方源 anthropic.com: {kept_official}")
print(f"谷歌降价独立保留: {has_google}")
print(f"Anthropic IPO 独立保留（未与 Fable5 误合并）: {has_ipo}")
ok = fable_count == 1 and has_google and has_ipo
print("VERDICT", "PASS" if ok else "FAIL")
