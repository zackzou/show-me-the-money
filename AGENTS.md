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
`SMTM_PUBLIC_URL`：站点对外地址，默认按浏览器访问的 Host 自动推导
（内网 `http://192.168.x.x:8000` 原样带上）；反代 https 终止、或浏览器走内网
而 Hermes 走公网时显式设置（Hermes 提示词 / llms.txt / RSS 都用它）。

## 配完之后：这些网页是主要入口

- `/sources` —— 加信源（填地址 → 试抓 → 添加）、就地改名、启停、软删除与撤回
- `/keywords` —— 屏蔽词（不收集）与特别关注词（加星标 + 优先排序），即时生效
- `/trash` —— 回收站：恢复 / 批量永久删除 / 清空
- `/reports` —— 数据报告：KPI、趋势与分布图表，每个元素可下钻明细
- `/brief` —— 早报配置：分节（每节一张微信长图）+ 筛选；每天 06:00 自动存档；
  输出 `/brief.txt`、`/brief.md`、`/api/brief`、`/api/brief/generate`（立即生成）
- `/agent` —— Agent 接入：Skill（`/skill.md`）/ MCP（`scripts/smtm_mcp.py`）/ REST / 早报；`/llms.txt` 是站点说明书
- `/settings` —— 换模型、填 Key（点眼睛才取明文）、读模型列表（`POST /settings/models`
  调 `{base}/models`，读不到可手动输入）、测连通性、看 Token 用量与调用日志
- `/status` —— 服务状态（人看）；`/api/health` 是给脚本的 JSON
- `/rss-guide` —— RSS 订阅说明页

命令行也能改信源（批量默认值）：`config/default_sources.yaml`。

## 管理功能的数据口径（改代码前必读）

- **可见性只有一个开关**：`models.visible_article_conditions()`（`duplicate_of IS NULL`
  + `deleted_at IS NULL`）。列表 / 日报 / 搜索 / RSS / 报告都从这里取条件，
  **新加展示入口也必须用它**，否则回收站里的文章会从那个口子漏出来。
- **删除是软删除**：`Article.deleted_at` 打时间戳，恢复是一次 UPDATE；
  只有 `/api/trash/purge` 才真 DELETE（并做孤儿图对账）。
- **关键词规则**在 `app/fetcher/keywords.py`：
  - 屏蔽：入库前看标题+摘要（`pipeline.block_hit`）；正文级命中在
    `scheduler.apply_keyword_rules`（content_job 尾部）软删到回收站；
  - 关注：同一处置 `Article.starred=1`，排序在 `routes._day_statement`（`starred DESC`）；
  - 英文按**词边界**匹配（`Muse` 不命中 `museum`），中文裸包含；改匹配逻辑先看
    `tests/test_manage.py::test_keyword_matches_latin_word_boundary`。
- **报告页纯服务端聚合 + CSS 图表**（`app/web/reports.py`），不引图表库；
  下钻统一走 `/reports/breakdown?dimension=&value=`。
- **早报**（`app/report/brief.py` + `app/web/brief.py`）：配置存 `brief_config`
  单行表（整表覆盖语义：字段缺席 = 清空，**例外是 `sections`** —— 缺席保留
  现有流水线，旧版页面/脚本不带这个字段时不会把节点清空；`sections` 存节点 JSON 数组，
  节点类型 news/text/weather，带 enabled 开关；停用节点不进产出但保留配置）。
  产出形态：news/text 节点 = 长图；**weather 节点 = 纯文本**（早安问候 +
  天气 + 穿衣建议，直接发手机）—— `/api/brief/image` 对 weather 返回 404
  是设计如此，Hermes 提示词据此只发文字；每个节点的 `text` 字段是一节一条
  的成稿（`build_brief` 里 `section_text()` 生成，存档时随 sections 落库，
  旧存档由 `issue_view` 补算）。成品存 `brief_issues`（每天一份，
  `run_brief_job` 在 06:00 生成，配置页可回看）。内容用 `brief_text()`
  （`Article.brief_zh` 优先，中文导读/推荐理由/正文节选兜底；
  `allow_placeholder=False` 时没素材的条目**剔除**），拼装零 LLM 调用。
  `app/web/api.py:_digest_with_fallback` 与它共用同一实现
  （单篇页传 `allow_placeholder=True`），别写第二份。
  长图渲染在 `brief.html` 的 `drawCard()`（Canvas 1080px，翻页跳过 weather）
  —— `wrapText` 的逐字累积是渲染正确性的关键，改动后必须肉眼验收长图。
  服务端另有一份渲染（`app/report/longimage.py`，`/api/brief/image`，
  Hermes 微信直发用）：Docker 里 emoji 字体是位图字体（NotoColorEmoji
  只有 109px 原生字面），`_emoji_tile` 先画原生大小再裁缩到正文行高，
  不能直接 `draw.text`；改动后跑 `tests/test_brief.py -k emoji` 并
  在容器里 curl 一张长图肉眼验收（本地 macOS 字体和容器行为不同）。
- **站点图标**：`app/web/static/` 下的 Z 字母图标（svg/ico/png），路由在
  `routes.py:site_icon`（白名单防目录穿越）。换图标直接替换 static 下的文件
  （svg 是矢量源，其余尺寸是它的栅格导出），并同步 `_ICON_TYPES` 白名单。
- **天气**（`app/report/weather.py`）：Open-Meteo 地理编码 + 预报（免费无 Key），
  WMO 码 → emoji + 中文描述，穿衣建议/大风台风提醒模板化生成；结果按
  「城市+小时」内存缓存 1 小时。取不到城市时如实输出「暂时取不到」。
- **Agent 接入**（`app/web/agent.py` + `scripts/smtm_mcp.py`）：llms.txt / skill.md 的
  内容在 `agent.py` 里生成（页面与文件共用，避免两处漂移）；MCP 是零依赖 stdio server，
  协议为行分隔 JSON-RPC（initialize / tools/list / tools/call）。

## 外观：两套皮肤（科技 / 经典）

- **科技风格（默认）**：`app/web/templates/_skin_aihot.css`（走 `/skin/aihot.css` 强缓存）+
  `base.html` 里的 `.sk-side` / `.sk-tabbar` 壳层。桌面 = 左侧栏（搜索框在内容区右上角），
  手机 = 顶栏 + 底部标签栏，暖白 + 青绿配色。深浅色默认「自适应」（按电脑时间，19:00~07:00 深色）。
- **经典风格**：顶栏横导航 + 顶栏搜索框，蓝主色。用户显式选择才启用。
- 切换是**纯前端偏好**（`localStorage` 的 `smtm-skin`，只存显式 `classic`；未设置 = 科技风格），
  服务端不渲染 `data-skin`。
- 两条硬约束（有测试守着，见 `tests/test_skin.py`）：
  1. 皮肤全部选择器锁在 `html[data-skin="aihot"]` 下，经典风格零影响；
  2. 只改样式不改行为 —— 新功能必须两种皮肤都能用。
- 图标按钮的悬停提示用 `data-tip` + CSS 伪元素（原生 title 在内嵌浏览器里不弹）。
- 加页面时注意：搜索框样式（`.search-field`）在 `base.html` 里统一维护，
  首页与搜索页共用；页内搜索框在「经典风格桌面」与「窄屏」由 CSS 隐藏（顶栏那个可见）。

## 质量门（改代码后必须全过）

```bash
ruff check app tests scripts
mypy app scripts
pytest                      # 当前基线：490 passed
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
6. **网关可能「通但把内容压坏」。** 中转网关（9router 等）会注入「回答尽量简短」
   类风格指令，把完整翻译压成电报体（「苹果发新AI模型Ferret。全端侧运行。」）。
   这种故障下所有调用都返回 200、长度也达标，只有虚词密度能识别
   （`app.utils.text.looks_telegraphic`，已接进翻译/摘要/导读/理由各条路径）。
   排查：`python scripts/init_check.py --ping` 会跑翻译质量探针；确认
   `LLM_EXTRA_HEADERS` 里的关闭开关真的到达网关 —— **中间任何一层代理
   转发时丢请求头，开关就失效**（实测 8890 的 OCR 桥接只转发 3 个固定头）。

## 数据维护脚本

都**默认只报告**，确认后加 `--apply` 才写库：

```bash
python scripts/cleanup_native_zh.py          # 中文原文却存了英文版（title_en/digest_en）
python scripts/repair_half_translated.py     # 中文侧小节里其实是英文原文（半截译文）
python scripts/repair_telegraphic.py         # 被网关风格注入压成「电报体」的文案/译文
```

**脚本必须在应用进程内跑**（`docker exec <容器> python scripts/...`），
不要从宿主机直接对着 `data/smtm.db` 跑 —— 应用持有 WAL 时外部进程写入会
把库写坏（见上面第 1 条）。

## 代码地图

```
app/main.py          create_app 工厂 + 中间件
app/config.py        .env / config/*.yaml 加载与校验
app/db.py            引擎、WAL、启动自愈迁移
app/models.py        Article / Source / DailyReport / KeywordRule / BriefConfig / BriefIssue
                     · visible_article_conditions
app/scheduler.py     APScheduler 任务编排 · apply_keyword_rules（正文级关键词规则）
app/fetcher/         rss 抓取 · content 正文抽取 · images 配图 · lang 语种判定
                     guard 元数据拦截 · dedup 去重 · media_store 图片本地化 · pipeline 主流程
                     keywords 关键词规则（匹配口径与执行）
app/ai/              client（HTTP+降级）· prompts · processor 处理流水线 · cluster 同题合并
app/report/          generator 日报生成与时间窗口 · brief 早报精选与渲染
                     weather 天气文案 · longimage 服务端长图（Pillow）
app/web/             routes 页面 · sources 信源管理 · settings 设置 · api JSON（含重新获取）
                     search · rss 订阅 · keywords 关键词管理 · trash 回收站 · reports 数据报告
                     brief 早报配置/输出 · agent llms.txt/skill.md/接入页
app/web/templates/   Jinja2；base.html 持有全站 CSS 与公共 JS
                     _skin_aihot.css 科技风格皮肤（/skin/aihot.css）· _icons.html 皮肤图标
app/web/static/      站点图标（favicon / apple-touch-icon / icon-192/512）
config/              可直接改的默认配置（挂载进容器，改完重启即生效）
scripts/             init_check（自检）· cleanup_native_zh · repair_half_translated · repair_telegraphic
                     smtm_mcp.py MCP server（零依赖 stdio）
tests/               pytest；tests/fixtures/bad_schedule 是「配置坏了」的测试夹具
```

## 改动纪律

- 外部调用一律带超时、重试与降级；不要引入无超时的网络调用。
- 时间统一北京时间 naive（`app/utils/text.py:now_local`）。
- 改动展示层就要同时看窄屏（≤680px）：悬浮条、语言切换、空槽这类固定元素最容易压字。
- 新增用户可见功能要同步 `README.md`，并考虑在 `/sources`、`/settings` 里给入口。
- 数据修复写成 `scripts/` 下的幂等脚本，默认 dry-run，别直接改存量数据。
