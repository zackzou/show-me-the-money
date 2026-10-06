# AGENTS.md

给 AI agent 的操作说明。目标是让你**第一次就把项目正确跑起来，并且不把数据或服务搞坏**。
人读的完整文档在 `README.md`；这里只放 agent 真正需要的东西。

## 这是什么

一个自托管的「只配两项（大模型 API + 调研方向）」的资讯订阅系统：定时抓 RSS、按调研方向
筛选、用大模型生成中文日报，并提供热点流、单篇页、RSS 输出。Python 3.11+ / FastAPI /
SQLite / APScheduler。

## 部署（Docker，推荐，命令已实测）

```bash
git clone https://github.com/zackzou/show-me-the-money.git
cd show-me-the-money
cp .env.example .env
# 必填四项：LLM_API_BASE / LLM_API_KEY / LLM_MODEL / RESEARCH_TOPIC
docker compose up -d
# http://localhost:8000
```

`docker compose up -d` **只启动 `app`**：本地模型那套放在 `local-llm` profile 后面，需要时
用 `docker compose --profile local-llm up -d` 显式开启（会拉约 7GB 的 ollama 镜像）。
默认就指向远程 API，所以远程用户不会白下这个镜像。

不用 Docker：

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/init_check.py --ping --fetch
python -m app.main
```

**先自检再起服务**：`python scripts/init_check.py`（`--ping` 真发一次 LLM 请求，`--fetch`
跑一次抓取）。它会打印配置、数据库、信源现状。

**版本**：支持 3.11～3.13（`pyproject.toml` 声明 `>=3.11,<3.14`，Docker 镜像用的 3.11）。
用 3.9 会在 `import app.models` 时报 `MappedAnnotationError: Could not resolve ... "Mapped[int | None]"`
——报错完全不提版本，很容易被误判成依赖装坏了。3.10 能导入，但不在声明的支持范围内。

## 环境变量

必填四项在 `.env.example` 里有。**这三个真实存在但过去没写进示例文件**：

| 变量 | 作用 |
| --- | --- |
| `LLM_FALLBACK_MODELS` | 备用模型，英文逗号分隔。主模型 429/503/401 时按顺序顺延 |
| `LLM_EXTRA_HEADERS` | 网关自定义头，JSON 字符串，如 `{"x-9router-token-saver":"off"}` |
| `SMTM_DISABLE_STARTUP_FETCH` | 设 `1` 则启动不抓取（离线部署、CI、只想看界面时用） |

其余可选：`FETCH_ON_STARTUP` / `CONFIG_DIR` / `DB_PATH`（默认 `config`、`data/smtm.db`）。

## 配完之后：三个网页是主要入口

- `/sources` —— 加信源（填地址 → 试抓 → 添加）、就地改名、启停、软删除与撤回
- `/settings` —— 换模型、填 Key（点眼睛才取明文）、测连通性、看 Token 用量与调用日志
- `/rss-guide` —— RSS 订阅说明页

命令行也能改信源（批量默认值）：`config/default_sources.yaml`。

## 质量门（改代码后必须全过）

```bash
ruff check app tests scripts
mypy app scripts
pytest                      # 当前基线：388 passed
```

## 会让你踩坑的几件事

1. **绝不要在应用运行时用外部 `sqlite3` 客户端写 `data/smtm.db`。**
   实测：在应用持有 WAL 时另开进程写入并关闭，会把 `-wal` 清成全零，应用随即全站
   500（`file is not a database`）。**只读**（`mode=ro`）是安全的。要改数据走网页接口或
   `scripts/` 里的脚本（它们在应用进程内跑）。
2. **后台 AI 任务运行时，网页写操作可能 500。**
   长 LLM 调用期间 SQLite 写锁被占，网页的「先读后写」会踩 `SQLITE_BUSY_SNAPSHOT`
   ——这种升级失败**不遵守 `busy_timeout`，重试也没用**。前端会显示「操作失败」而不是
   静默刷新。根治要改事务模式或给长任务加检查点提交，**目前是已知未修项**。
   遇到时等任务跑完再点，别当成 bug 反复重试。
3. **LLM 超时/限流是设计内的降级**，不是故障：对应字段留空、文章标 `failed`，
   调度器会把它放回队列重试。单篇页上的「重新获取」按钮可以手动重跑一篇。
4. **语言是「这一篇」的属性**：切换不写 localStorage，刷新回到中文。信源语言默认
   `auto`（按正文内容判定），中文原文不产出英文版。改语言相关代码前先读
   `README.md` 的「页面与语言策略」。
5. **首次启动别把 `ai.batch_size` 调大**（默认 60）：处理不完就不会进日报。

## 数据维护脚本

都**默认只报告**，确认后加 `--apply` 才写库：

```bash
python scripts/cleanup_native_zh.py          # 中文原文却存了英文版（title_en/digest_en）
python scripts/repair_half_translated.py     # 中文侧小节里其实是英文原文（半截译文）
```

## 代码地图

```
app/main.py          create_app 工厂 + 中间件
app/config.py        .env / config/*.yaml 加载与校验
app/db.py            引擎、WAL、启动自愈迁移
app/models.py        Article / Source / DailyReport
app/scheduler.py     APScheduler 任务编排
app/fetcher/         rss 抓取 · content 正文抽取 · images 配图 · lang 语种判定
                     guard 元数据拦截 · dedup 去重 · media_store 图片本地化 · pipeline 主流程
app/ai/              client（HTTP+降级）· prompts · processor 处理流水线 · cluster 同题合并
app/report/          generator 日报生成与时间窗口
app/web/             routes 页面 · sources 信源管理 · settings 设置 · api JSON（含重新获取）
                     search · rss 订阅
app/web/templates/   Jinja2；base.html 持有全站 CSS 与公共 JS
config/              可直接改的默认配置（挂载进容器，改完重启即生效）
scripts/             init_check（自检）· cleanup_native_zh · repair_half_translated
tests/               pytest；tests/fixtures/bad_schedule 是「配置坏了」的测试夹具
```

## 改动纪律

- 外部调用一律带超时、重试与降级；不要引入无超时的网络调用。
- 时间统一北京时间 naive（`app/utils/text.py:now_local`）。
- 改动展示层就要同时看窄屏（≤680px）：悬浮条、语言切换、空槽这类固定元素最容易压字。
- 新增用户可见功能要同步 `README.md`，并考虑在 `/sources`、`/settings` 里给入口。
- 数据修复写成 `scripts/` 下的幂等脚本，默认 dry-run，别直接改存量数据。
