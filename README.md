# Show Me the Money

[![GitHub Pages](https://img.shields.io/badge/在线演示-GitHub%20Pages-blue)](https://zackzou.github.io/show-me-the-money/)

**只配两项，自动产出行业热点日报。** 你给出「大模型 API」和「调研方向」，项目自己去抓主流信源、按方向筛选、生成中文速览与标签；当天每 2 小时滚动刷新，每天 08:00 定稿前一日日报。

**页内就能读完，不用跳原站**：系统把原文正文抓回来，列表页给「速览 + 推荐理由 + 相关度评分」，
点进单篇页是**完整正文**（左栏来源信息，右栏逐段正文）—— 刷完热点不用来回切标签页。

- 必填配置只有 `LLM_API_*` + `RESEARCH_TOPIC`，其余全部内置默认值
- 默认信源池、分类、提示词、调度、信源过滤规则都在 `config/*.yaml`，改配置不用改代码
- 不绑定 OpenAI：任意 OpenAI 兼容接口、本地 Ollama 都可以（含返回 SSE 流的网关）
- 单机轻量：Python + FastAPI + SQLite，一个 `docker compose up -d` 跑起来

- 必填配置只有 `LLM_API_*` + `RESEARCH_TOPIC`，其余全部内置默认值
- 默认信源池、分类、提示词、调度、信源过滤规则都在 `config/*.yaml`，改配置不用改代码
- 不绑定 OpenAI：任意 OpenAI 兼容接口、本地 Ollama 都可以
- 单机轻量：Python + FastAPI + SQLite，一个 `docker compose up -d` 跑起来

---

## 目录

- [快速开始（Docker，推荐）](#快速开始docker推荐)
- [本地运行（不用 Docker）](#本地运行不用-docker)
- [配置](#配置)
- [它是怎么跑的](#它是怎么跑的)
- [接口一览](#接口一览)
- [预览产出长什么样](#预览产出长什么样)
- [工程约定与质量门](#工程约定与质量门)
- [安全须知](#安全须知)
- [常见问题](#常见问题)
- [扩展](#扩展)

---

## 快速开始（Docker，推荐）

```bash
git clone https://github.com/zackzou/show-me-the-money.git
cd show-me-the-money

cp .env.example .env
# 编辑 .env：填 LLM_API_BASE / LLM_API_KEY / LLM_MODEL / RESEARCH_TOPIC

docker compose up -d
# 打开 http://localhost:8000
```

用远程 API 时可以把 `docker-compose.yml` 里的 `ollama` 服务删掉；用本地模型则保留它，并把 `.env` 指向 `http://ollama:11434/v1`。

想先确认配置和信源是否正常，再起服务：

```bash
docker compose run --rm app python scripts/init_check.py --ping --fetch
```

首次启动会自动抓取 + 处理 + 生成当天日报，通常 1～3 分钟出结果。**首启不要把 `ai.batch_size` 调太大**：默认每轮处理 60 篇，剩下的下一轮继续，处理完才进日报。

---

## 本地运行（不用 Docker）

需要 Python 3.11+：

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/init_check.py --ping --fetch   # 启动自检：配置 / 数据库 / LLM / 抓取
python -m app.main                            # 等价于 uvicorn app.main:create_app --factory
```

---

## 配置

### 必填（`.env`）

| 变量 | 说明 |
| --- | --- |
| `LLM_API_BASE` | OpenAI 兼容接口地址，如 `https://api.deepseek.com/v1`；Ollama 填 `http://ollama:11434/v1` |
| `LLM_API_KEY` | 接口密钥；本地 Ollama 填 `ollama` |
| `LLM_MODEL` | 模型名，如 `deepseek-chat` / `qwen2.5:3b` |
| `RESEARCH_TOPIC` | 调研方向，英文逗号分隔，≤ 200 字符，如 `AI Agent, 大模型商业化, 开源生态, 芯片` |

校验不通过时进程直接报错退出，并逐条列出缺哪一项。

### 可选环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FETCH_ON_STARTUP` | `true` | 启动时是否立刻抓取一次 |
| `CONFIG_DIR` | `config` | 默认配置文件目录 |
| `DB_PATH` | `data/smtm.db` | SQLite 路径（相对路径按项目根解析） |
| `SMTM_DISABLE_STARTUP_FETCH` | 未设置 | 设为 `1` 可关闭启动抓取，临时省一次 API 调用 |

### 配置文件（一般不用改）

| 文件 | 内容 |
| --- | --- |
| `config/default_sources.yaml` | 默认信源池（9 个：HN / TechCrunch / The Verge / Ars Technica / MIT TR / HF Blog / 雷锋网 / 量子位 / InfoQ AI）。单个源失败自动跳过；抓到 0 条会被标记为异常并写进日志 |
| `config/default_topics.yaml` | 分类清单（顶部 tab + 每类的判定提示）与主题词表 |
| `config/default_prompts.yaml` | 摘要 / 标签 / 相关度提示词，以及降级摘要长度 |
| `config/settings.yaml` | 调度、保留天数、信源过滤、处理批量、端口、超时与重试 |

**信源会自动同步进数据库**：`config/default_sources.yaml` 里新增的地址下次启动自动生效；你手动关掉的信源（`enabled=0`）不会被配置改回来。

`config/settings.yaml` 里几个值得知道的项：

| 配置项 | 默认 | 作用 |
| --- | --- | --- |
| `schedule.daily_report_time` | `08:00` | **前一日**日报的定稿时间 |
| `schedule.fetch_interval_hours` | `2` | 抓取 / 处理间隔（小时） |
| `schedule.fetch_cron` / `process_cron` | 空 | 填了就改用 cron（如 `"0 7 * * *"`）每天固定时刻跑，留空则按间隔 |
| `fetcher.max_age_days` | `14` | 只收最近 N 天发布的内容。挡掉「一次返回整个历史」的归档型 feed |
| `fetcher.max_items_per_source` | `60` | 单个信源单次最多入库条数（成本闸门） |
| `fetcher.min_content_chars` | `0` | 正文短于该长度视为「只有标题」丢弃，`0` = 不限制 |
| `media.enabled` | `true` | 是否在处理后补齐配图 |
| `content.batch_size` | `20` | 每轮最多抓多少篇正文（每篇一次请求） |
| `content.min_chars` | `200` | 正文短于这个字数视为没抓到，保留短摘要 |
| `media.batch_size` | `20` | 每轮最多补多少篇配图（每篇一次请求） |
| `media.enabled` | `true` | 关掉就完全不请求文章页（省流量，也更礼貌） |
| `ai.batch_size` | `60` | 每轮最多处理多少篇 pending，每篇要花 2~3 次 LLM 调用 |
| `storage.retention_days` | `30` | 数据保留天数 |

---

## 它是怎么跑的

```
每 2 小时   抓取 RSS → 时间窗/条数过滤 → 抽正文配图 → 去重 → 入库（pending）
每 2 小时   抓取正文全文（content_job，必须跑在处理之前）
每 2 小时   相关度判断+评分 → 中文摘要 → 速览 → 推荐理由 → 分类+主题 → 标签（失败降级）
              └ 处理完顺手刷新「今天」这份日报，首页实时可见
每天 08:00  定稿「昨天」的日报（昨天已不再变化，所以出的是完整版）
启动时      补齐最近 7 天里缺失的日报（停机几天再起来也不会留空洞）
每天 03:00  清理超过保留期（默认 30 天）的数据
```

几个容易被忽略但很关键的设计：

- **日报按自然日切分，且一份日报只管一天。** 08:00 出的是「昨天」的定稿；今天那份每 2 小时滚动刷新到当前时刻。这样当天任何时间发布的文章都不会被漏掉（早先版本只出「当天 00:00 到生成时刻」的内容，下午和晚上的文章会永远不属于任何一份日报）。
- **LLM 不可用不会让日报消失。** 摘要会降级为正文开头 200 字，并标注「降级：LLM 不可用」。
- **正文会被抓回来。** RSS 的 description 大多只有一两句，靠它写出来必然是标题复读。
  所以在处理之前先跑 `content_job` 把原文正文抓下来，摘要 / 速览 / 推荐理由都基于完整正文。
  抓不到就退回短摘要，不会给残缺正文。
- **配图不再被丢掉。** 两级取图：① 入库时从 RSS 正文抽 `<img src/data-src/srcset>`；
  ② RSS 没给图时去文章页抓 `og:image`，每轮限量，查过没有的会标记、不重复请求。
  滤掉追踪像素、占位图、站点 logo，以及同一信源下反复出现的站点通用图。
  **升级旧库会自动补上 `content_full` / `reason` / `score` 等新列。**
- **停机后会自动补齐缺失日期的日报。**

---

## 接口一览

| 路径 | 说明 |
| --- | --- |
| `/` | 首页：热点资讯流（速览 + 配图 + 标签），每页 20 条，`?page=N` 翻页 |
| `/story/{id}` | 单篇页内详情：左栏来源 + 中栏完整正文 + 右栏推荐理由/主题/标签 |
| `/search?q=&scope=&cat=` | 站内搜索，`scope` 取 `meta`（标题与摘要）或 `full`（含正文） |
| `/saved` | 我的收藏（本地） |
| `/daily/{date}` | 指定日期（`YYYY-MM-DD`）的全部报道，没有日报则 404 |
| `/archive` | 历史日报列表 |
| `/api/articles?date=YYYY-MM-DD` | 当天文章 JSON（默认北京时间今天） |
| `/api/reports` `/api/reports/{date}` | 日报列表 / 指定日报（Markdown + HTML 全文） |
| `/health` `/api/health` | 健康检查（含信源数、文章数、日报数、调研方向） |
| `/rss` | 最新日报的 RSS，每篇文章一个条目；`?date=` 可指定某一天 |
| `/docs` | 自动生成的 OpenAPI 文档 |

---

## 预览产出长什么样

### 分类、搜索与收藏

- **顶部分类 tab**：全部 / 一手 / 模型 / 产品 / 行业 / 论文 / 教程 / 观点，
  定义在 `config/default_topics.yaml`，增删分类不用改代码。点 tab 走 `/?cat=xxx`。
- **搜索**：右上角搜索框，按 <kbd>/</kbd> 随时聚焦。搜标题、摘要、速览、推荐理由、主题；
  搜索结果页可切「最新（标题与摘要）」/「全文」。走 `/search?q=xxx`。
- **标签可点**：卡片与详情页的标签点进去就是 `/?tag=xxx` 过滤结果。
- **收藏**：卡片与详情页的 ☆ 存在**浏览器本地**（localStorage），不上传任何数据；
  `/saved` 页把收藏列出来。访问 `/api/articles` 最多取 500 条，所以收藏多了会受这个上限影响。
- **搜索实现**：只用 SQL `LIKE`，不引 FTS5（中文分词要额外 tokenizer，对小项目不划算）。
  只搜已入选的内容（`relevance=1`），否则会把判为不相关的噪音也翻出来。
  LIKE 通配符已转义（搜 `%` 不会变成匹配全部）。

### 热点资讯时间轴（首页 / `/daily/{date}`）

按时间倒序排列，左侧是时间点与竖线，右侧是卡片：

```
14:33  ●  ┌─ 来源 · 精选                [相关度 79] ─┐
          │ 标题（粗体，点进详情）                  │
          │ 速览：2~3 句导语，页内就能读完         │
          │ ─────────────────────────────────────  │
          │ 推荐理由：读者为什么要点开这条          │
          │ 时间 · 标签 · 读全文 →                 │
          └───────────────────────────────────────┘
10:10  ●  ...
```

卡片上的信息各自解决一个问题：

| 元素 | 作用 |
| --- | --- |
| **时间点 + 竖线** | 一眼看出这条是几点出来的，以及一天里热点的节奏 |
| **速览** | 2~3 句导语式概述（时间 + 谁 + 做了什么 + 关键数字），页内就能读完 |
| **分类 + 标签** | 分类可点（`/?cat=`），标签可点（`/?tag=`） |
| **推荐理由** | 回答「为什么要点开这条」，由模型指出最有信息量的地方 |
| **AI 评分 + 收藏** | 相关度 0~100（顺带产出，不额外消耗调用），☆ 收藏到本地 |

### 单篇页内详情（`/story/{id}`）

三栏布局：

- **左栏**：返回、来源、域名、发布时间、相对时间
- **中栏**：标题 → 分类/主题定位（斜体）→ **AI 导读** → 分隔线 → **完整正文**（逐段渲染）→ 文章配图
- **右栏**：打开原文 / 收藏 → 推荐理由（含分类与 AI 评分）→ 主题（可点，跳搜索）
  → 标签（可点，跳首页过滤）→ 相关阅读（同分类或同标签）

正文来自 `app/fetcher/content.py`：把原文页面抓下来，按「段落密度」提取纯文本。
实测 6 个默认信源全部抓取成功（量子位 2077 字、InfoQ 2739 字、TechCrunch 2862 字、
ArsTechnica 3611 字、雷锋网 2663 字、The Verge 1311 字），而 RSS 自带的只有 0~1663 字。
抓不到就退回 RSS 短摘要，不会给出残缺正文。

页面支持深浅色切换（跟随系统，可手动覆盖并记住），配图点击页内放大，窄屏自适应。

仓库 `docs/` 下还有两份样例：

- `docs/sample-report.md` —— 日报（Markdown）长什么样
- `docs/sample-report.html` —— 同一天的 HTML 版

日报结构：

```markdown
# Show Me the Money 日报 · 2026-09-29
> 调研方向：AI Agent, 大模型商业化, 开源生态, 芯片

## 1. 某条相关的热点标题
- 来源：量子位
- 摘要：（100 字以内，答案先行）
- 标签：AI、芯片、开源
- 链接：https://example.com/...
```

网页版把同一天的每篇文章渲染成卡片（标题 / 来源 / 时间 / 摘要 / 标签 / 原文链接），RSS 则是一条文章一个条目，方便丢进阅读器。

---

## 工程约定与质量门

- 所有外部调用都有超时、重试、降级
- 时间统一按**北京时间（UTC+8）**入库与分组；容器里也设了 `TZ=Asia/Shanghai`，日志时间和业务时间对得上
- 质量门：`ruff check .`、`mypy`、`pytest`（覆盖率门槛 70%）

```bash
pip install -r requirements-dev.txt   # 已包含 requirements.txt
ruff check . && mypy && pytest --cov=app
```

当前状态：`ruff` 无告警、`mypy` 28 个文件零告警、121 个测试全绿、覆盖率 89%。

---

## 安全须知

- **`.env` 已被 `.gitignore` 忽略，不要提交。** 仓库里只有 `.env.example` 占位符。
- **API Key 只在内存里和 `Authorization` 请求头中使用**，不会写进日志；万一上游网关把请求头回显在错误体里，也会先打码再记录（`app/ai/client.py` 的 `redact`）。
- **服务没有任何鉴权，默认监听 `0.0.0.0:8000`。** 只建议在本机或内网使用；要放到公网，请在前面加一层反向代理并加上鉴权。
- 数据库（`data/smtm.db`）同样在 `.gitignore` 里。
- 文章标题/摘要来自第三方 RSS，在网页和 RSS 输出里都经过转义，不会造成 HTML 注入。
- 搜索框输入会拼进 SQL `LIKE`，通配符已转义，不存在注入；搜索不出网关、不写任何数据。
- 收藏只存在浏览器 localStorage，**不会上传**；清浏览器数据就没了。
- **配图是原站外链**：页面用 `<img>` 直接引用第三方图片地址，浏览器会把 Referer 发给图片站。
  介意的话用反向代理缓存图片，或设 `media.enabled: false` 彻底不取图。
- 抓文章页取图时只发普通 GET、带明确 User-Agent、每轮限量 `media.batch_size` 篇；关掉
  `media.enabled` 就完全不请求。

---

## 常见问题

**页面显示「还没有日报」**
第一次启动要等抓取 + 处理跑完（1～3 分钟）。看日志：`docker compose logs -f app`，或先跑 `python scripts/init_check.py --ping --fetch`。

**日志里有「信源一条内容都没抓到」**
该 RSS 地址失效了（返回 HTML 或空 feed）。换一个能用的地址写进 `config/default_sources.yaml` 即可，其余逻辑不受影响。

**日报里某天文章特别少 / 某一类文章不见了**
多半是相关度判断把它判成不相关（`RESEARCH_TOPIC` 写窄了），或者前一天那批文章在定稿时还没处理完（会打 WARNING，提示调大 `ai.batch_size`）。

**想控制 API 花费**
把 `ai.batch_size` 调小、调大 `fetch_interval_hours`、删掉 `config/default_sources.yaml` 里用不到的源。相关度判断是每篇 1 次调用，摘要 + 标签各 1 次。

---

## 扩展

| 想做的事 | 改哪里 |
| --- | --- |
| 新增/停用信源 | `config/default_sources.yaml` |
| 换模型 | `.env` 的 `LLM_MODEL` |
| 改调度 | `config/settings.yaml` |
| 改提示词（含速览写法） | `config/default_prompts.yaml` |
| 邮件推送 | 新增 `app/notify/email.py`，在 `app/scheduler.py` 挂任务 |
| 事件聚类 | 新增 `app/ai/cluster.py`，在 `process_pending` 之后调用 |
| MCP 接口 | 新增 `app/web/mcp.py` |

扩展点都在边缘，不需要动核心抓取 / 处理 / 日报逻辑。

## 目录结构

```
app/
  main.py          FastAPI 入口（工厂函数 create_app）
  config.py        配置加载与两项必填校验
  db.py            连接、建表、信源同步
  models.py        Article / Source / DailyReport
  schemas.py       接口出参
  fetcher/         rss.py 抓取 · dedup.py 去重 · pipeline.py 主流程与过滤
                    content.py 正文全文抽取 · images.py og:image 补图
  ai/              client.py 纯 HTTP 客户端 · prompts.py · processor.py 处理与降级
  report/          generator.py 日报生成与时间窗口
  web/             routes.py 页面（热点流/单篇速览/归档）· api.py JSON · rss.py 订阅
  utils/           logger.py · text.py
config/            默认信源 / 分类 / 提示词 / 调度
tests/             test_config · test_fetcher · test_ai · test_report · test_api
                    test_media（配图抽取）· test_content（正文抽取）
                    test_search（搜索与分类过滤）· test_migration（老库补列）
scripts/           init_check.py 启动自检
docs/              样例日报（Markdown / HTML）
```

## License

MIT