# AI每日大事件 Max

严格按照 PRD《AI新闻整理 - 产品需求文档》开发的全新独立项目（不覆盖既有项目）。
每日整理 AI 大模型行业重大新闻：全量抓取 → 自动覆盖评价 → LLM 结构化（事件/内容/潜在影响）→ 板块/公司筛选。

## 项目位置

```
/Users/bytedance/workspace/ai_daily_news_max/
├── config.py            # 全部 101 个信息源 + 8 个 LLM Provider 配置
├── scraper.py           # 多源抓取引擎（RSS/Web/SPA/Sogou/X syndication）
├── llm_client.py        # 多 Provider LLM 客户端（含 Bytedance ModelHub）
├── news_processor.py    # 去重 + LLM 结构化 + 覆盖评价
├── app/                 # FastAPI + Jinja2/HTMX + SSE 公开网页端
├── main.py              # 旧 Streamlit 本地界面（兼容保留）
├── test_logic.py        # 单元测试（200 项断言，含 PRD 逐项合规）
├── verify_full_run.py   # 全量实跑验收脚本
├── railway.toml         # Railway 部署配置
├── render.yaml          # Render 部署配置
└── requirements.txt     # 依赖（含版本要求与安装方式）
```

## 安装与启动

```bash
# Python 3.10+（推荐本机 framework 3.13，不要用 /usr/bin/python3）
PY=/Library/Frameworks/Python.framework/Versions/3.13/bin/python3

# 安装依赖
$PY -m pip install -r requirements.txt

# 启动新版公开网页端（推荐）
cd "/Users/bytedance/Desktop/每日AI新闻爬取 - 调试版本 - codex"
$PY -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 打开
open http://127.0.0.1:8000
```

新版网页端特性：
- FastAPI 服务端渲染页面，不依赖 Streamlit 运行时。
- SSE 实时推送抓取/分析进度。
- 每次抓取、覆盖报告、结构化结果、运行日志都会持久化。
- 每天北京时间 11:00 自动执行一次全量抓取，不自动调用大模型分析。
- 本地默认写入 `data/ai_news.sqlite3`；公开部署使用 `DATABASE_URL` 连接 PostgreSQL。

旧 Streamlit 入口仍可本地兼容运行：

```bash
$PY -m streamlit run main.py
```

## 公开部署（Railway / Render）

推荐 Railway，操作接近 Streamlit：连接 GitHub 仓库，添加 PostgreSQL，填环境变量，平台自动发布公网 URL。

必填环境变量：

```bash
DATABASE_URL=平台 PostgreSQL 自动生成
MODEL_PROVIDER=Bytedance ModelHub
MODEL_NAME=gemini-3.5-flash
MODEL_API_KEY=你的模型 Key
```

启动命令：

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

### 每日 11:00 自动抓取

- FastAPI 服务启动后会运行后台调度器，每天北京时间 11:00 自动创建一次全量抓取任务。
- 自动任务只抓取并保存原始新闻和覆盖报告，即使配置了 `MODEL_API_KEY` 也不会自动分析。
- 自动抓取结果会进入任务记录，用户可在任何时间手动发起新的抓取或对已有结果启动分析。
- 调度器使用数据库按北京时间日期做幂等声明；Railway 重启或多实例同时检查时，同一天最多创建一次自动任务。
- 如果服务在当天 11:00 后启动，会补发当天尚未执行的自动抓取。
- 不需要打开网页或保持浏览器在线，但 Railway Web Service 必须持续运行且可连接数据库和新闻网站。
- 如需临时关闭自动调度，可设置 `DAILY_CRAWL_ENABLED=0`；默认开启。

## 信息源覆盖（101 个，零遗漏）

| 类别 | 数量 | 说明 |
|---|---|---|
| 搜索聚合 | 2 | AnySearch / Tavily 多查询实时搜索 |
| 海外快讯/Newsletter | 3 | The Rundown AI / TLDR AI / The Decoder |
| 海外科技媒体 | 3 | The Information / TechCrunch / MIT Tech Review |
| 技术社区 | 2 | Hugging Face / Reddit r/LocalLLaMA |
| Agent智能体信息源 | 16 | Agent 行业、产品、开发工具与企业自动化官方 RSS/Atom |
| 国内媒体 | 4 | 机器之心 / 新智元 / 极客公园 / 钛媒体 |
| 公司-大模型企业 | 12 | PRD 13 家（Kimi/Moonshot 同公司合并为一条） |
| 公司-Agent | 5 | LangChain / Cognition / Adept / Sierra / Perplexity |
| 公司-图像生成 | 5 | Midjourney / Stability / BFL / Ideogram / Recraft |
| 公司-视频生成 | 6 | Runway / Pika / Luma / Sora / Kling / Hailuo |
| 公司-Research Lab | 6 | DeepMind / OpenAI / Anthropic / FAIR / AI2 / MSR |
| X.com KOL | 37 | PRD 四大类全量 handle |

## 全量抓取策略（探索所有获取途径）

每个信源按多级降级链抓取，确保「不漏任何一个信息源」：

1. **RSS**（最稳定、时间戳精确）— feedparser 解析 curl_cffi 抓回的字节
2. **ScrapeCreators 第三方爬虫 API**（https://app.scrapecreators.com/）—
   X.com KOL 一手推文主通道（`twitter/user-tweets`，彻底绕过 syndication 的
   IP 级 429 限频）+ Reddit（`reddit/subreddit`，绕过直抓 403）。
   按 credit 计费（1 调用 = 1 credit，全量一轮 ≈ 38 credits）；
   Key 用尽/失败时自动降级回 syndication/直抓链路
3. **直接网页抓取** — curl_cffi `impersonate="chrome120"` 绕过 TLS 指纹封锁
4. **动态站点 + SPA 状态解析** — 解析 JSON-LD、Next.js/Nuxt hydration JSON、
   DOM 锚点和 sitemap；列表页无日期的条目继续抓详情页二段核验
5. **正文与日期精确提取** — 结合 meta、结构化数据、`<time>` 与 trafilatura，
   窗口外或无可信发布时间的条目一律丢弃
6. **Sogou / Google News 搜索兜底** — 国内被封站与国内公司严格校验媒体归属

## 去重（两段式，借鉴 rss_agent 生产方案）

1. **规则去重**：URL 归一化（去 query/尾斜杠）+ 标题归一化完全一致
2. **近重复聚类**：标题相似度（SequenceMatcher ≥ 0.80）+ 完全链接聚类
   （簇间相似度取跨簇最小值，抑制链式误合并）；每簇代表稿按信源质量分
   选择 — 官方/一手源（openai.com、blog.google 等）优先于转载聚合源；
   聚类异常 fail-open 降级回规则去重，不阻塞主流程

## 时间窗口（PRD 硬约束）

所有信源统一严格「昨日 11am 至当日 11am」（CST）窗口：
- RSS/Web/SPA/Sogou/推文全部过滤窗口外内容（上界 +2h 容差吸收缓存漂移）
- 无可信时间戳条目一律丢弃，避免旧闻或栏目页污染当期结果
- 抓取完成后自动运行覆盖评价：101/101 信源都有报告行 → 「不存在遗漏」

## LLM Provider（8 个）

公开三方：OpenRouter / Gemini / OpenAI / Anthropic / Kimi / MiniMax / DeepSeek
Bytedance ModelHub（默认）：AzureOpenAI 形态，端点
`https://aidp.bytedance.net/api/modelhub/online/v2/crawl`，api_version
`2024-03-01-preview`，需 X-TT-LOGID 头；支持 gemini-3.5-flash（默认）/
gemini-3.1-p / gemini-3.1-p-priority / gpt-5.5-2026-04-24 /
gpt-5.6-terra / gpt-5.6-sol / gpt-5.6-luna / deepseek_v4_pro。
Gemini 模型自动注入 `{"thinking": {"budget_tokens": 0}}` 关闭 thinking
（否则正文 JSON 被截断为空）。

新版公开网页端推荐通过环境变量 `MODEL_API_KEY` 配置服务端 Key；也支持在首页或分析表单中临时输入 Key（不会落库）。首页填写 Key 时，抓取完成会自动进入分析；留空则保持“先抓取、再到详情页手动分析”的流程。模型既可以从常用下拉列表选择，也可以手动填写模型型号；手动填写优先。

## 已知现实约束（如实呈现，不伪造数据）

- **X.com KOL 数据新鲜度**：ScrapeCreators 通道已绕过 429 限频并能拿到一手推文，
  但部分 KOL（如 sama/simonw）上游返回的时间线仍是陈旧缓存（最新推文数月前），
  这类 KOL 窗口内为 0 条属上游数据现状；活跃 KOL（elonmusk/NousResearch 等）
  可正常抓到窗口内推文。诊断信息会标注「拿到 N 条，最新日期」便于区分。
- **ScrapeCreators credits**：按调用计费，余额 ≤5 时运行日志告警；用尽后
  KOL 自动降级 syndication（受 429 限制）、Reddit 降级 RSS/直抓（受 403 限制）。
- **Bytedance ModelHub** 端点为内网地址，需在 ByteDance 网络环境内使用。

## 验证

```bash
# 单元测试（200 项断言）
$PY test_logic.py

# 全量实跑验收（耗时取决于外部站点；KOL 批次有总预算和 API 熔断）
# 验收标准：REPORT_ROWS=101, ALL_COVERED=True, WINDOW_VIOLATIONS=0
$PY -u verify_full_run.py
```
