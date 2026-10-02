# Show Me the Money

[![GitHub Pages](https://img.shields.io/badge/在线演示-GitHub%20Pages-blue)](https://zackzou.github.io/show-me-the-money/)

**只配两项，自动产出行业热点日报。** 你给出「大模型 API」和「调研方向」，项目自己去抓主流信源、按方向筛选、生成中文摘要与标签，每天 08:00 出一份可订阅的日报。

- 用户只有两项必填配置：`LLM_API_*` + `RESEARCH_TOPIC`
- 默认信源池、分类、提示词、调度全部内置，开箱即用
- 不绑定 OpenAI：任意 OpenAI 兼容接口、本地 Ollama 都可以
- 单机轻量：Python + FastAPI + SQLite，一个 `docker compose up -d` 跑起来

## 快速开始

```bash
git clone https://github.com/zackzou/show-me-the-money.git
cd show-me-the-money

cp .env.example .env
# 编辑 .env：填 LLM_API_BASE / LLM_API_KEY / LLM_MODEL / RESEARCH_TOPIC

docker compose up -d
# 打开 http://localhost:8000
```

用远程 API 时可以把 `docker-compose.yml` 里的 `ollama` 服务删掉；本地模型则保留，并把 `.env` 指向 `http://ollama:11434/v1`。

不想用 Docker（本地开发）：

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/init_check.py --ping --fetch   # 启动自检：配置 / 数据库 / LLM / 抓取
python -m app.main                            # 等价于 uvicorn app.main:create_app --factory
```

## 配置

### 两项必填（`.env`）

| 变量 | 说明 |
| --- | --- |
| `LLM_API_BASE` | OpenAI 兼容接口地址，如 `https://api.deepseek.com/v1`；Ollama 填 `http://ollama:11434/v1` |
| `LLM_API_KEY` | 接口密钥；本地 Ollama 填 `ollama` |
| `LLM_MODEL` | 模型名，如 `deepseek-chat` / `qwen2.5:3b` |
| `RESEARCH_TOPIC` | 调研方向，英文逗号分隔，≤ 200 字符，如 `AI Agent, 大模型商业化, 开源生态, 芯片` |

校验不通过时进程直接报错退出，并逐条列出缺哪一项。

### 默认值（不用改，可覆盖）

| 文件 | 内容 |
| --- | --- |
| `config/default_sources.yaml` | 9 个默认信源（HN / TechCrunch / The Verge / Ars Technica / MIT TR / HF Blog / 机器之心 / 量子位 / InfoQ AI），单个源失败自动跳过 |
| `config/default_topics.yaml` | 默认分类标签 |
| `config/default_prompts.yaml` | 摘要 / 标签 / 相关度提示词，以及降级摘要长度 |
| `config/settings.yaml` | 调度、保留天数、端口、超时与重试。抓取默认按 `fetch_interval_hours` 轮询；想让它在每天固定时刻跑，就填 `fetch_cron` / `process_cron`（cron 表达式，如 `"0 7 * * *"`），留空则回到间隔触发 |

环境变量 `CONFIG_DIR`、`DB_PATH`、`FETCH_ON_STARTUP` 可覆盖默认路径与行为。

## 运行方式

```
每 2 小时  抓取 RSS → 去重 → 入库（pending）
每 2 小时  相关度判断 → 中文摘要 → 标签（processed / 失败降级）
每天 08:00 汇总当天相关文章 → Markdown + HTML 日报
每天 03:00 清理超过保留期（默认 30 天）的数据
```

接口一览：

| 路径 | 说明 |
| --- | --- |
| `/` `/daily/{date}` `/archive` | 首页最新日报 / 指定日期 / 历史列表 |
| `/api/articles?date=YYYY-MM-DD` | 当日文章 JSON |
| `/api/reports` `/api/reports/{date}` | 日报列表 / 指定日报 |
| `/health` `/api/health` | 健康检查 |
| `/rss` | 最新日报的 RSS（可用 `?date=` 指定日期） |

## 工程约定

- 所有外部调用都有超时、重试、降级；LLM 不可用时摘要退化为正文前 200 字，不影响日报产出
- 时间统一按**北京时间**入库，日报按自然日分组
- 质量门：`ruff check .`、`mypy`、`pytest`（覆盖率门槛 70%，当前 88%）

```bash
pip install -r requirements-dev.txt
ruff check . && mypy && pytest --cov=app
```

## 扩展

| 想做的事 | 改哪里 |
| --- | --- |
| 新增/停用信源 | `config/default_sources.yaml`（或挂载自定义 yaml） |
| 换模型 | `.env` 的 `LLM_MODEL` |
| 改调度 | `config/settings.yaml` |
| 改提示词 | `config/default_prompts.yaml` |
| 邮件推送 | 新增 `app/notify/email.py`，在 `app/scheduler.py` 挂任务 |
| 事件聚类 | 新增 `app/ai/cluster.py`，在 `process_pending` 之后调用 |
| MCP 接口 | 新增 `app/web/mcp.py` |

扩展点都在边缘，不需要动核心抓取 / 处理 / 日报逻辑。

## 目录结构

```
app/
  main.py          FastAPI 入口（工厂函数 create_app）
  config.py        配置加载与两项必填校验
  db.py models.py schemas.py
  fetcher/         rss.py 抓取 · dedup.py 去重 · pipeline.py 主流程
  ai/              client.py 纯 HTTP 客户端 · prompts.py · processor.py 处理与降级
  report/          generator.py 日报生成
  web/             routes.py 页面 · api.py JSON · rss.py 订阅
  utils/           logger.py · text.py
config/            默认信源 / 分类 / 提示词 / 调度
tests/             test_fetcher · test_ai · test_report · test_api · test_config
scripts/           init_check.py 启动自检
```

## License

MIT
