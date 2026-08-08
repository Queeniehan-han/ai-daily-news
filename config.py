# -*- coding: utf-8 -*-
"""
config.py — 「AI每日大事件 Max」全局配置

严格按照 PRD《AI新闻整理 - 产品需求文档》落地全部信息源，不漏任何一个：
  - 抓取网站：搜索聚合(2) + 海外快讯/Newsletter(3) + 海外科技媒体(3) + 技术社区(2) + 国内媒体(4)
  - Agent 行业、产品与企业自动化信息源(16)
  - 抓取公司：大模型企业(12, Kimi/Moonshot 合并) + Agent(5) + 图像生成(5)
              + 视频生成(6) + 顶尖 Research Lab(6)
  - 抓取 X.com KOL：四大类共 37 个 handle
  - LLM Provider：7 个公开三方（OpenRouter/Gemini/OpenAI/Anthropic/Kimi/MiniMax/DeepSeek）
                  + Bytedance ModelHub（AzureOpenAI 形态）

设计原则（项目历史教训沉淀）：
  1) 每个信息源 url 字段保留 PRD 原始 URL（溯源/合规校验用）；RSS 走 rss_url
     二级字段，绝不用 RSS URL 替换 PRD URL。
  2) PRD 把 Kimi 与 Moonshot 分列，但二者是同一家公司，合并为 "Kimi / Moonshot"
     一条（名称同时含两个子串，PRD 合规子串匹配仍通过）。
  3) 所有信源带 category 字段，支撑 UI 按板块/公司筛选。
"""

from __future__ import annotations

from typing import Dict, List
from zoneinfo import ZoneInfo

# ──────────────────────────────────────────────────────────────────────────
# 时区与时间窗口
# ──────────────────────────────────────────────────────────────────────────
# PRD 硬约束：信息时间为「昨日 11am 至当日 11am」（中国标准时间）
CST = ZoneInfo("Asia/Shanghai")

# 时间窗口上界容差（小时）：吸收页面缓存/时区漂移，不放松下界
WINDOW_GRACE_HOURS = 2

# ──────────────────────────────────────────────────────────────────────────
# 新闻分类（PRD 规定）
# ──────────────────────────────────────────────────────────────────────────
NEWS_TYPES: List[str] = [
    "新产品发布",
    "产品功能更新",
    "新大模型发布",
    "Agent智能体",
    "具身机器人",
    "项目融资",
    "其他重大动态",  # 兜底：不属于上述分类但仍属重大 AI 行业动态
]

# ──────────────────────────────────────────────────────────────────────────
# AnySearch 搜索聚合源（产品新增：作为默认信息源，可在「信息源」页启停/删除）
# ──────────────────────────────────────────────────────────────────────────
ANYSEARCH_SOURCES: List[Dict] = [
    {
        "name": "AnySearch",
        "url": "https://anysearch.com/",
        "type": "anysearch",
        "category": "搜索聚合",
        "anysearch_queries": [
            "latest AI model release AI agent news OpenAI Anthropic Google DeepMind Meta xAI DeepSeek Qwen Kimi",
            "AI 大模型 最新发布 Agent 智能体 AIGC 图像生成 视频生成",
            "OpenAI Anthropic Google DeepMind Meta AI xAI DeepSeek Qwen Kimi latest news",
            "AI Agent Devin LangChain autonomous workflow latest news",
        ],
        "max_results": 10,
    },
]

# ──────────────────────────────────────────────────────────────────────────
# Tavily 搜索聚合源（默认信息源，可在「信息源」页配置 Key、启停/删除）
# ──────────────────────────────────────────────────────────────────────────
TAVILY_SOURCES: List[Dict] = [
    {
        "name": "Tavily",
        "url": "https://app.tavily.com/home",
        "type": "tavily",
        "category": "搜索聚合",
        "tavily_queries": [
            "latest AI model releases and major AI industry news",
            "AI agents autonomous workflows latest product releases",
            "OpenAI Anthropic Google DeepMind Meta xAI DeepSeek Qwen Kimi latest news",
            "AI 大模型 Agent 智能体 AIGC 最新发布与重大动态",
        ],
        "max_results": 10,
    },
]

# ──────────────────────────────────────────────────────────────────────────
# 一、海外 AI 独立快讯与 Newsletter 榜单（PRD 抓取网站 §一）
# ──────────────────────────────────────────────────────────────────────────
NEWSLETTER_SOURCES: List[Dict] = [
    {"name": "The Rundown AI", "url": "https://www.rundown.ai/", "type": "web",
     "crawl_url": "https://www.rundown.ai/articles",
     "allowed_hosts": ["www.rundown.ai", "rundown.ai", "app.therundown.ai"],
     "category": "海外快讯/Newsletter"},
    {"name": "TLDR AI", "url": "https://tldr.tech/ai", "type": "web",
     "rss_url": "https://tldr.tech/api/rss/ai", "category": "海外快讯/Newsletter"},
    {"name": "The Decoder", "url": "https://the-decoder.com/", "type": "web",
     "rss_url": "https://the-decoder.com/feed/", "category": "海外快讯/Newsletter"},
]

# ──────────────────────────────────────────────────────────────────────────
# 二、海外一线顶级科技媒体（PRD 抓取网站 §二）
# ──────────────────────────────────────────────────────────────────────────
TECH_MEDIA_SOURCES: List[Dict] = [
    {"name": "The Information (AI)", "url": "https://www.theinformation.com/", "type": "web",
     "sogou_fallback": True, "category": "海外科技媒体"},
    {"name": "TechCrunch (AI)", "url": "https://techcrunch.com/category/artificial-intelligence/",
     "type": "web", "rss_url": "https://techcrunch.com/feed/",
     "filter_ai_relevance": True, "category": "海外科技媒体"},
    {"name": "MIT Technology Review (AI)",
     "url": "https://www.technologyreview.com/topic/artificial-intelligence/", "type": "web",
     "rss_url": "https://www.technologyreview.com/feed/",
     "filter_ai_relevance": True, "category": "海外科技媒体"},
]

# ──────────────────────────────────────────────────────────────────────────
# 三、技术专家与研究社区（PRD 抓取网站 §三）
# ──────────────────────────────────────────────────────────────────────────
COMMUNITY_SOURCES: List[Dict] = [
    {"name": "Hugging Face Blog", "url": "https://huggingface.co/", "type": "web",
     "rss_url": "https://huggingface.co/blog/feed.xml", "category": "技术社区"},
    {"name": "Reddit r/LocalLLaMA", "url": "https://www.reddit.com/r/LocalLLaMA/", "type": "web",
     "rss_url": "https://www.reddit.com/r/LocalLLaMA/.rss",
     "subreddit": "LocalLLaMA", "category": "技术社区"},
]

# ──────────────────────────────────────────────────────────────────────────
# Agent 行业、产品与企业自动化（官方 RSS/Atom，2026-07-25 已验证）
# ──────────────────────────────────────────────────────────────────────────
AGENT_INDUSTRY_SOURCES: List[Dict] = [
    {"name": "Latent Space", "url": "https://www.latent.space/", "type": "web",
     "rss_url": "https://www.latent.space/feed", "category": "Agent智能体信息源"},
    {"name": "Import AI", "url": "https://importai.substack.com/", "type": "web",
     "rss_url": "https://jack-clark.net/feed/", "category": "Agent智能体信息源"},
    {"name": "Last Week in AI", "url": "https://lastweekin.ai/", "type": "web",
     "rss_url": "https://lastweekin.ai/feed", "category": "Agent智能体信息源"},
    {"name": "Ben's Bites", "url": "https://www.bensbites.com/", "type": "web",
     "rss_url": "https://www.bensbites.com/feed", "category": "Agent智能体信息源"},
    {"name": "Interconnects", "url": "https://www.interconnects.ai/", "type": "web",
     "rss_url": "https://www.interconnects.ai/feed", "category": "Agent智能体信息源"},
    {"name": "One Useful Thing", "url": "https://www.oneusefulthing.org/", "type": "web",
     "rss_url": "https://www.oneusefulthing.org/feed", "category": "Agent智能体信息源"},
    {"name": "Simon Willison", "url": "https://simonwillison.net/", "type": "web",
     "rss_url": "https://simonwillison.net/atom/everything/",
     "filter_ai_relevance": True, "category": "Agent智能体信息源"},
    {"name": "Microsoft Agent Framework Blog",
     "url": "https://devblogs.microsoft.com/agent-framework/", "type": "web",
     "rss_url": "https://devblogs.microsoft.com/agent-framework/feed/",
     "category": "Agent智能体信息源"},
    {"name": "AWS Machine Learning Blog",
     "url": "https://aws.amazon.com/blogs/machine-learning/", "type": "web",
     "rss_url": "https://aws.amazon.com/blogs/machine-learning/feed/",
     "filter_ai_relevance": True, "category": "Agent智能体信息源"},
    {"name": "GitHub Copilot Changelog",
     "url": "https://github.blog/changelog/label/copilot/", "type": "web",
     "rss_url": "https://github.blog/changelog/label/copilot/feed/",
     "category": "Agent智能体信息源"},
    {"name": "Vercel", "url": "https://vercel.com/", "type": "web",
     "rss_url": "https://vercel.com/atom",
     "filter_ai_relevance": True, "category": "Agent智能体信息源"},
    {"name": "n8n Blog", "url": "https://blog.n8n.io/", "type": "web",
     "rss_url": "https://blog.n8n.io/rss/",
     "filter_ai_relevance": True, "category": "Agent智能体信息源"},
    {"name": "Zapier Blog", "url": "https://zapier.com/blog/", "type": "web",
     "rss_url": "https://zapier.com/blog/feed/",
     "filter_ai_relevance": True, "category": "Agent智能体信息源"},
    {"name": "Salesforce AI Blog",
     "url": "https://www.salesforce.com/blog/category/ai/", "type": "web",
     "rss_url": "https://www.salesforce.com/blog/category/ai/feed/",
     "category": "Agent智能体信息源"},
    {"name": "UiPath Agent SDK", "url": "https://github.com/UiPath/uipath-python",
     "type": "web",
     "rss_url": "https://github.com/UiPath/uipath-python/releases.atom",
     "category": "Agent智能体信息源"},
    {"name": "Browser Use", "url": "https://www.browser-use.com/", "type": "web",
     "rss_url": "https://www.browser-use.com/rss.xml", "category": "Agent智能体信息源"},
]

# ──────────────────────────────────────────────────────────────────────────
# 四、国内 AI 垂直与一线科技媒体（PRD 抓取网站 §四）
# ──────────────────────────────────────────────────────────────────────────
DOMESTIC_SOURCES: List[Dict] = [
    {"name": "机器之心", "url": "https://www.jiqizhixin.com/", "type": "web",
     "sogou_fallback": True,
     "google_news_queries": ["机器之心 site:jiqizhixin.com when:7d", "机器之心 人工智能 when:2d"],
     "google_news_source_aliases": ["机器之心"],
     "google_news_domains": ["jiqizhixin.com"],
     "prefer_search_fallback": True,
     "category": "国内媒体"},
    {"name": "新智元", "url": "https://www.xinzhiyuan.com/", "type": "web",
     "sogou_fallback": True,
     "google_news_queries": ["新智元 site:xinzhiyuan.com when:7d", "\"新智元\" AI when:7d"],
     "google_news_source_aliases": ["新智元"],
     "google_news_domains": ["xinzhiyuan.com"],
     "prefer_search_fallback": True,
     "category": "国内媒体"},
    {"name": "极客公园", "url": "https://www.geekpark.net/", "type": "web",
     "sogou_fallback": True,
     "google_news_queries": ["site:geekpark.net AI when:7d", "极客公园 人工智能 when:2d"],
     "google_news_source_aliases": ["极客公园"],
     "google_news_domains": ["geekpark.net"],
     "prefer_search_fallback": True,
     "category": "国内媒体"},
    {"name": "钛媒体", "url": "https://www.tmtpost.com/", "type": "web",
     "rss_url": "https://www.tmtpost.com/feed", "sogou_fallback": True,
     "google_news_queries": ["site:tmtpost.com AI when:7d", "钛媒体 人工智能 when:2d"],
     "google_news_source_aliases": ["钛媒体"],
     "google_news_domains": ["tmtpost.com"],
     "prefer_search_fallback": True,
     "filter_ai_relevance": True,
     "category": "国内媒体"},
]

# ──────────────────────────────────────────────────────────────────────────
# 抓取公司 — 五大类公司官方渠道（PRD §抓取公司）
# ──────────────────────────────────────────────────────────────────────────
# 类别 1：大模型企业（PRD 列举 13 家，Kimi/Moonshot 同公司合并 → 12 条）
COMPANY_LLM: List[Dict] = [
    {"name": "OpenAI", "url": "https://openai.com/news/", "type": "web",
     "rss_url": "https://openai.com/news/rss.xml", "category": "公司-大模型企业"},
    {"name": "Anthropic", "url": "https://www.anthropic.com/news", "type": "web",
     "category": "公司-大模型企业"},
    {"name": "Nvidia", "url": "https://blogs.nvidia.com/blog/category/generative-ai/", "type": "web",
     "rss_url": "https://blogs.nvidia.com/feed/",
     "filter_ai_relevance": True, "category": "公司-大模型企业"},
    {"name": "Meta AI", "url": "https://ai.meta.com/blog/", "type": "web",
     "category": "公司-大模型企业"},
    {"name": "Google AI", "url": "https://blog.google/technology/ai/", "type": "web",
     "rss_url": "https://blog.google/technology/ai/rss/", "category": "公司-大模型企业"},
    {"name": "字节跳动 (Bytedance)", "url": "https://www.volcengine.com/docs/82379", "type": "web",
     "sogou_fallback": True, "sogou_query": "豆包大模型", "category": "公司-大模型企业"},
    {"name": "腾讯 (Tencent)", "url": "https://hunyuan.tencent.com/", "type": "web",
     "sogou_fallback": True, "sogou_query": "腾讯混元", "category": "公司-大模型企业"},
    {"name": "阿里巴巴 (Alibaba)", "url": "https://qwenlm.github.io/blog/", "type": "web",
     "rss_url": "https://qwenlm.github.io/blog/index.xml",
     "sogou_fallback": True, "sogou_query": "通义千问", "category": "公司-大模型企业"},
    {"name": "Kimi / Moonshot", "url": "https://www.moonshot.cn/", "type": "web",
     "sogou_fallback": True, "sogou_query": "Kimi 月之暗面", "category": "公司-大模型企业"},
    {"name": "智谱 GLM (Zhipu)", "url": "https://zhipuai.cn/", "type": "web",
     "sogou_fallback": True, "sogou_query": "智谱 GLM", "category": "公司-大模型企业"},
    {"name": "DeepSeek", "url": "https://www.deepseek.com/", "type": "web",
     "sogou_fallback": True, "sogou_query": "DeepSeek 深度求索", "category": "公司-大模型企业"},
    {"name": "Grok / xAI", "url": "https://x.ai/news", "type": "web",
     "category": "公司-大模型企业"},
]

# 类别 2：Agent 相关头部公司
COMPANY_AGENT: List[Dict] = [
    {"name": "LangChain", "url": "https://www.langchain.com/blog", "type": "web",
     "rss_url": "https://www.langchain.com/blog/rss.xml", "category": "公司-Agent"},
    {"name": "Cognition (Devin)", "url": "https://cognition.com/blog", "type": "web",
     "category": "公司-Agent"},
    {"name": "Adept", "url": "https://www.adept.ai/blog", "type": "web", "category": "公司-Agent"},
    {"name": "Sierra AI", "url": "https://sierra.ai/blog", "type": "web", "category": "公司-Agent"},
    {"name": "Perplexity", "url": "https://www.perplexity.ai/hub/blog", "type": "web",
     "category": "公司-Agent"},
]

# 类别 3：图像生成头部公司
COMPANY_IMAGE: List[Dict] = [
    {"name": "Midjourney", "url": "https://www.midjourney.com/updates", "type": "web",
     "category": "公司-图像生成"},
    {"name": "Stability AI", "url": "https://stability.ai/news", "type": "web",
     "category": "公司-图像生成"},
    {"name": "Black Forest Labs (Flux)", "url": "https://blackforestlabs.ai/", "type": "web",
     "category": "公司-图像生成"},
    {"name": "Ideogram", "url": "https://about.ideogram.ai/", "type": "web",
     "category": "公司-图像生成"},
    {"name": "Recraft", "url": "https://www.recraft.ai/blog", "type": "web",
     "category": "公司-图像生成"},
]

# 类别 4：视频生成头部公司
COMPANY_VIDEO: List[Dict] = [
    {"name": "Runway", "url": "https://runwayml.com/research", "type": "web",
     "category": "公司-视频生成"},
    {"name": "Pika Labs", "url": "https://pika.art/", "type": "web", "category": "公司-视频生成"},
    {"name": "Luma AI (Dream Machine)", "url": "https://lumalabs.ai/dream-machine", "type": "web",
     "category": "公司-视频生成"},
    {"name": "OpenAI Sora", "url": "https://openai.com/sora/", "type": "web",
     "sitemap_path_hints": ["/sora/"], "category": "公司-视频生成"},
    {"name": "Kling (快手可灵)", "url": "https://klingai.com/", "type": "web",
     "sogou_fallback": True, "sogou_query": "可灵 AI", "category": "公司-视频生成"},
    {"name": "Hailuo (MiniMax)", "url": "https://hailuoai.com/", "type": "web",
     "sogou_fallback": True, "sogou_query": "海螺AI MiniMax", "category": "公司-视频生成"},
]

# 类别 5：顶尖 Research Lab
COMPANY_RESEARCH: List[Dict] = [
    {"name": "Google DeepMind", "url": "https://deepmind.google/discover/blog/", "type": "web",
     "rss_url": "https://deepmind.google/blog/rss.xml", "category": "公司-Research Lab"},
    {"name": "OpenAI Research", "url": "https://openai.com/research/", "type": "web",
     "sitemap_path_hints": ["/research/"], "category": "公司-Research Lab"},
    {"name": "Anthropic Research", "url": "https://www.anthropic.com/research", "type": "web",
     "category": "公司-Research Lab"},
    {"name": "Meta FAIR", "url": "https://ai.meta.com/research/", "type": "web",
     "category": "公司-Research Lab"},
    {"name": "Allen Institute (AI2)", "url": "https://allenai.org/news", "type": "web",
     "category": "公司-Research Lab"},
    {"name": "Microsoft Research AI",
     "url": "https://www.microsoft.com/en-us/research/?msr-field-of-study=artificial-intelligence",
     "type": "web", "category": "公司-Research Lab"},
]

# ──────────────────────────────────────────────────────────────────────────
# 抓取 X.com 的 KOL（PRD §抓取 X.com 的 KOL，四大类共 37 个）
# ──────────────────────────────────────────────────────────────────────────
def _kol(handle: str, name: str, group: str) -> Dict:
    return {
        "name": f"{name} (@{handle})",
        "handle": handle,
        "url": f"https://x.com/{handle}",
        "type": "x_kol",
        "category": f"X KOL - {group}",
    }


X_KOL_SOURCES: List[Dict] = [
    # 一、顶尖学术泰斗与技术领袖（8）
    _kol("karpathy", "Andrej Karpathy", "学术领袖"),
    _kol("ylecun", "Yann LeCun", "学术领袖"),
    _kol("AndrewYNg", "Andrew Ng 吴恩达", "学术领袖"),
    _kol("drfeifei", "Fei-Fei Li 李飞飞", "学术领袖"),
    _kol("DrJimFan", "Jim Fan", "学术领袖"),
    _kol("denny_zhou", "Denny Zhou", "学术领袖"),
    _kol("tri_dao", "Tri Dao", "学术领袖"),
    _kol("SwaroopMishra_", "Swaroop Mishra", "学术领袖"),
    # 二、一线 AI 巨头与独角兽创始人/高管（8）
    _kol("sama", "Sam Altman", "创始人/高管"),
    _kol("gdb", "Greg Brockman", "创始人/高管"),
    _kol("demishassabis", "Demis Hassabis", "创始人/高管"),
    _kol("AravSrinivas", "Aravind Srinivas", "创始人/高管"),
    _kol("elonmusk", "Elon Musk", "创始人/高管"),
    _kol("ClementDelangue", "Clem Delangue", "创始人/高管"),
    _kol("hwchase17", "Harrison Chase", "创始人/高管"),
    _kol("bindureddy", "Bindu Reddy", "创始人/高管"),
    # 三、权威评测、基准与开源机构（6）
    _kol("ArtificialAnl", "Artificial Analysis", "评测/基准/开源"),
    _kol("lmsysorg", "LMSYS Org", "评测/基准/开源"),
    _kol("swe_bench", "SWE-bench", "评测/基准/开源"),
    _kol("NousResearch", "Nous Research", "评测/基准/开源"),
    _kol("TogetherAM", "Together AI", "评测/基准/开源"),
    _kol("LiveBenchAI", "LiveBench AI", "评测/基准/开源"),
    # 四、独立学者、硬核开发者与技术布道者（15）
    _kol("emollick", "Ethan Mollick", "独立学者/开发者"),
    _kol("simonw", "Simon Willison", "独立学者/开发者"),
    _kol("_akhaliq", "AK", "独立学者/开发者"),
    _kol("rasbt", "Sebastian Raschka", "独立学者/开发者"),
    _kol("natolambert", "Nathan Lambert", "独立学者/开发者"),
    _kol("_philipp_schmid", "Philipp Schmid", "独立学者/开发者"),
    _kol("kbindas", "Kbindas", "独立学者/开发者"),
    _kol("DrustZ", "Tanishq Mathew Abraham", "独立学者/开发者"),
    _kol("b_clavie", "Benjamin Clavié", "独立学者/开发者"),
    _kol("maximelabonne", "Maxime Labonne", "独立学者/开发者"),
    _kol("antonosika", "Anton", "独立学者/开发者"),
    _kol("anya_tw", "Anya", "独立学者/开发者"),
    _kol("rowancheung", "Rowan Cheung", "独立学者/开发者"),
    _kol("mattshumer_", "Matt Shumer", "独立学者/开发者"),
    _kol("OfficialLoganK", "Logan Kilpatrick", "独立学者/开发者"),
]

# ──────────────────────────────────────────────────────────────────────────
# 聚合访问器
# ──────────────────────────────────────────────────────────────────────────
WEB_SOURCES: List[Dict] = (
    ANYSEARCH_SOURCES
    + TAVILY_SOURCES
    + NEWSLETTER_SOURCES
    + TECH_MEDIA_SOURCES
    + COMMUNITY_SOURCES
    + AGENT_INDUSTRY_SOURCES
    + DOMESTIC_SOURCES
    + COMPANY_LLM
    + COMPANY_AGENT
    + COMPANY_IMAGE
    + COMPANY_VIDEO
    + COMPANY_RESEARCH
)

ALL_COMPANY_SOURCES: List[Dict] = (
    COMPANY_LLM + COMPANY_AGENT + COMPANY_IMAGE + COMPANY_VIDEO + COMPANY_RESEARCH
)


def get_all_sources() -> List[Dict]:
    """返回全部信息源（Web/RSS/搜索/公司 + X KOL），当前共 101 个。"""
    return WEB_SOURCES + X_KOL_SOURCES


def get_source_categories() -> List[str]:
    """返回所有信源板块（UI 板块筛选用），稳定顺序、去重。"""
    seen, ordered = set(), []
    for s in get_all_sources():
        c = s.get("category", "其他")
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    return ordered


def get_company_names() -> List[str]:
    """返回所有公司名（UI 公司筛选用）。"""
    return [s["name"] for s in ALL_COMPANY_SOURCES]


# ──────────────────────────────────────────────────────────────────────────
# LLM Provider 配置（PRD §用户可选择自己的大模型 api 调用和 api key）
# ──────────────────────────────────────────────────────────────────────────
# client_type 取值：
#   openai_compat — OpenAI 兼容 /chat/completions（OpenRouter/OpenAI/Kimi/DeepSeek/MiniMax）
#   gemini        — Google Gemini 原生 SDK（缺失时自动降级 OpenAI 兼容端点）
#   anthropic     — Anthropic Messages API
#   azure_openai  — Bytedance ModelHub（AzureOpenAI 形态，PRD 调用样例）
LLM_PROVIDERS: Dict[str, Dict] = {
    "OpenRouter": {
        "client_type": "openai_compat",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "google/gemini-3-flash-preview",
        "models": [
            "google/gemini-3-flash-preview",
            "google/gemini-3-pro-preview",
            "anthropic/claude-sonnet-4.6",
            "anthropic/claude-opus-4.6",
            "openai/gpt-5.5",
            "openai/gpt-5.5-pro",
            "deepseek/deepseek-v4-flash",
            "qwen/qwen3.6-flash",
        ],
        "key_help": "https://openrouter.ai/keys 获取 sk-or-... 开头的 Key",
    },
    "Gemini": {
        "client_type": "gemini",
        "base_url": "",
        "default_model": "gemini-2.5-flash",
        "models": ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"],
        "key_help": "https://aistudio.google.com/apikey 获取 Google AI Studio Key",
    },
    "OpenAI": {
        "client_type": "openai_compat",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "models": ["gpt-4o-mini", "gpt-4o", "gpt-4-turbo"],
        "key_help": "https://platform.openai.com/api-keys 获取 sk-... 开头的 Key",
    },
    "Anthropic": {
        "client_type": "anthropic",
        "base_url": "",
        "default_model": "claude-3-5-sonnet-latest",
        "models": ["claude-3-5-sonnet-latest", "claude-3-5-haiku-latest", "claude-3-opus-latest"],
        "key_help": "https://console.anthropic.com/ 获取 sk-ant-... 开头的 Key",
    },
    "Kimi": {
        "client_type": "openai_compat",
        "base_url": "https://api.moonshot.cn/v1",
        "default_model": "moonshot-v1-8k",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        "key_help": "https://platform.moonshot.cn/ 获取 sk-... 开头的 Key",
    },
    "MiniMax": {
        "client_type": "openai_compat",
        "base_url": "https://api.minimax.chat/v1",
        "default_model": "abab6.5s-chat",
        "models": ["abab6.5s-chat", "abab6.5-chat"],
        "key_help": "https://platform.minimaxi.com/ 获取 API Key",
    },
    "DeepSeek": {
        "client_type": "openai_compat",
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "key_help": "https://platform.deepseek.com/ 获取 sk-... 开头的 Key",
    },
    # PRD §支持 bytedance 调用方式（AzureOpenAI 形态，base_url 即完整 crawl 端点）
    "Bytedance ModelHub": {
        "client_type": "azure_openai",
        "base_url": "https://aidp.bytedance.net/api/modelhub/online/v2/crawl",
        "api_version": "2024-03-01-preview",
        "default_model": "gemini-3.5-flash",
        "models": [
            "gemini-3.5-flash",
            "gemini-3.1-p",
            "gemini-3.1-p-priority",
            "gpt-5.5-2026-04-24",
            "gpt-5.6-terra",
            "gpt-5.6-sol",
            "gpt-5.6-luna",
            "deepseek_v4_pro",
        ],
        "key_help": "ByteDance ModelHub Key（含 _GPT_AK 或以 dSx 开头）",
    },
}

# 默认 Provider / 模型（gemini-3.5-flash 普通账户更稳定有 quota，不要默认 -priority）
DEFAULT_PROVIDER = "OpenRouter"

# LLM 分析批大小（每批送多少条 raw item 给模型）
ANALYZE_BATCH_SIZE = 8

# 抓取并发与超时
WEB_MAX_WORKERS = 8
SOURCE_FUTURE_TIMEOUT = 90   # 单源抓取硬超时（秒）：含详情页日期核验的额外请求
RUN_DEADLINE_FACTOR = 6      # 全量 Web 抓取总 deadline = 单源超时 × 系数；优先完整性
HTTP_TIMEOUT = 20

# AnySearch 搜索 API：ANYSEARCH_API_KEY 可选；不配置时走匿名低额度。
ANYSEARCH_API_ENDPOINT = "https://api.anysearch.com/v1/search"
ANYSEARCH_DETAIL_VERIFY_LIMIT = 8
# Tavily Search API：通过信息源管理页或 TAVILY_API_KEY 环境变量配置。
TAVILY_API_ENDPOINT = "https://api.tavily.com/search"
TAVILY_DETAIL_VERIFY_LIMIT = 8
ARTICLE_META_TIMEOUT = 8
DETAIL_VERIFY_LIMIT = 5
SITEMAP_DETAIL_LIMIT = 12

# 按域名礼貌限速：参考 Scrapy AutoThrottle 的思路，不在同一域名上打突发请求。
# 这里是轻量版实现，避免引入 Scrapy 作为运行时依赖。
DOMAIN_MIN_INTERVAL = 0.35
DOMAIN_ERROR_BACKOFF = 2.5
DOMAIN_MAX_BACKOFF = 12.0

# ──────────────────────────────────────────────────────────────────────────
# ScrapeCreators 第三方爬虫 API（https://app.scrapecreators.com/）
# ──────────────────────────────────────────────────────────────────────────
# 用途（实测验证 2026-06-10）：
#   - X.com KOL 一手推文：twitter/user-tweets 端点，绕过 syndication 的
#     IP 级 429 限频（这是 KOL 抓取的根治方案）
#   - Reddit：reddit/subreddit 端点，绕过直抓 403
# 计费：按 credit（1 次调用 = 1 credit）。KOL 全量 37 个 + Reddit 1 个
#   ≈ 38 credits/轮。Key 余额不足时自动降级回 syndication/直抓链路。
# Key 只从环境变量读取，避免凭据进入源码和 Git 历史。
import os as _os

SCRAPECREATORS_API_KEY = _os.getenv("SCRAPECREATORS_API_KEY", "").strip()
SCRAPECREATORS_BASE = "https://api.scrapecreators.com/v1"
SCRAPECREATORS_ENABLED = True   # False 时完全回退旧链路（syndication/直抓）

# X KOL 免费 RSS 镜像兜底。仅在 ScrapeCreators + syndication 均未拿到窗口内
# 推文时尝试；不是官方 API，不承诺全量，只作为无付费 API 时的补充信号源。
_X_RSS_FALLBACK_RAW = _os.getenv(
    "X_KOL_RSS_FALLBACK_URLS",
    "https://rsshub.app/twitter/user/{handle}",
)
X_KOL_RSS_FALLBACK_URLS = [
    u.strip() for u in _X_RSS_FALLBACK_RAW.split(",") if u.strip()
]
X_KOL_RSS_TIMEOUT = int(_os.getenv("X_KOL_RSS_TIMEOUT", "8"))
X_KOL_RSS_MAX_PROVIDERS = int(_os.getenv("X_KOL_RSS_MAX_PROVIDERS", "1"))

# X KOL 调度策略：
# - 有 ScrapeCreators 这类付费/专业 API 且返回成功时，默认信任主链路，
#   不再强制访问匿名 syndication/RSSHub；否则一次 37 账号抓取会被 X 限频拖垮。
# - 当付费 API 返回鉴权/余额/限频错误时，本轮自动熔断，避免重复消耗 37 次失败请求。
# - 免费匿名链路只做 best-effort 兜底；若超过总预算，报告为疑似不全，不算代码缺陷。
X_KOL_CROSSCHECK_FREE_WHEN_SC_OK = (
    _os.getenv("X_KOL_CROSSCHECK_FREE_WHEN_SC_OK", "0").lower() in {"1", "true", "yes"}
)
X_KOL_FREE_FALLBACK_ON_SC_AUTH_ERROR = (
    _os.getenv("X_KOL_FREE_FALLBACK_ON_SC_AUTH_ERROR", "0").lower() in {"1", "true", "yes"}
)
X_KOL_BATCH_TIMEOUT = int(_os.getenv("X_KOL_BATCH_TIMEOUT", "180"))
LINKED_HUB_LIMIT = int(_os.getenv("LINKED_HUB_LIMIT", "3"))
GOOGLE_NEWS_TIMEOUT = int(_os.getenv("GOOGLE_NEWS_TIMEOUT", "12"))
GOOGLE_NEWS_MAX_ENTRIES = int(_os.getenv("GOOGLE_NEWS_MAX_ENTRIES", "60"))

# ──────────────────────────────────────────────────────────────────────────
# 去重配置（借鉴 rss_agent 项目 news_dedup.py 的两段式方案）
# ──────────────────────────────────────────────────────────────────────────
# 近重复聚类相似度阈值（标题归一化后 SequenceMatcher 比值；完全链接聚类）
DEDUP_NEAR_THRESHOLD = 0.80
# 代表稿选择的信源质量分（高质量官方/一手源优先保留）
DEDUP_HIGH_QUALITY_DOMAINS = (
    "openai.com", "anthropic.com", "blog.google", "deepmind.google",
    "ai.meta.com", "x.ai", "blogs.nvidia.com", "qwenlm.github.io",
    "techcrunch.com", "technologyreview.com", "theinformation.com",
)
DEDUP_LOW_QUALITY_DOMAINS = (
    "weixin.sogou.com", "mp.weixin.qq.com",
)
