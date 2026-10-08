"""Agent 接入：把站点变成 Agent 可读的数据源。

四种接入方式共用同一份数据（与页面同一套可见性口径）：

- **llms.txt**（``/llms.txt``）：给大模型的站点说明，列全部接口；
- **Agent Skill**（``/skill.md``）：一份可安装的 Skill 说明文件，
  Claude Code / Codex / Gemini CLI 等支持 Agent Skills 的工具读它；
- **REST API**（``/api/*``）：匿名 GET、无需 Key，任何脚本可用；
- **MCP**（``scripts/smtm_mcp.py``）：stdio 型 MCP server，零依赖。

页面本身在 ``/agent``：安装步骤、可复制的提示词、接口一览。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import __version__
from app.db import get_session
from app.models import Source
from app.schemas import ArticleOut
from app.web.routes import _ctx, public_base, templates
from app.web.search import SEARCH_RESULT_LIMIT, normalize_scope, search_articles

agent_router = APIRouter()

# llms.txt / skill.md 的内容在这里生成，页面与文件共用，避免两处漂移。
SKILL_NAME = "show-me-the-money"


def _base(request: Request) -> str:
    """站点对外地址（SMTM_PUBLIC_URL 优先，否则按请求 Host 推导）。"""
    return public_base(request)


def _llms_txt(base: str) -> str:
    """llms.txt：站点说明书（https://llmstxt.org 的约定格式）。"""
    return f"""# Show Me the Money

> 自托管的行业热点资讯站：定时抓 RSS、按调研方向筛选、AI 生成中文日报与早报。
> 全部接口匿名只读，无需注册、无需 API Key。

## 数据入口（全部 GET，返回 JSON，除非注明）

- [{base}/api/articles]({base}/api/articles)：按日期列出进日报的文章（默认今天，`?date=YYYY-MM-DD&limit=N`）
- [{base}/api/articles/{{id}}]({base}/api/articles/1)：单篇详情（含 `digest_brief` 早报片段、`topics_list`、正文）
- [{base}/api/reports]({base}/api/reports)：日报列表（`?limit=N`）
- [{base}/api/reports/{{date}}]({base}/api/reports/2026-01-01)：某天的日报（含 Markdown 全文）
- [{base}/api/brief]({base}/api/brief)：早报（按用户配置精选的 TOP N；每个节点含 text 成稿与 type，天气节点是纯文本）
- [{base}/api/search]({base}/api/search?q=AI)：站内搜索（`?q=关键词&scope=meta|full&limit=N`）
- [{base}/api/sources]({base}/api/sources)：信源列表
- [{base}/api/health]({base}/api/health)：服务状态

## 纯文本入口

- [{base}/brief.txt]({base}/brief.txt)：早报纯文本（直接可读，适合推送）
- [{base}/brief.md]({base}/brief.md)：早报 Markdown
- [{base}/rss]({base}/rss)：最新日报的 RSS（`?date=YYYY-MM-DD` 指定某天）
- [{base}/skill.md]({base}/skill.md)：Agent Skill 安装说明

## 使用建议

- 想要「今天最重要的几条」→ 用 `/api/brief`（用户已配置好精选规则）
- 想要按关键词找历史文章 → 用 `/api/search`
- 想要完整的一天 → 用 `/api/articles?date=...` 或 `/api/reports/{{date}}`
- 文章字段里的 `title_zh` / `digest_zh` 是中文版；`brief_zh` 是适合手机阅读的短摘要
"""


def _skill_md(base: str) -> str:
    """Agent Skill 说明（供支持 Agent Skills 的工具安装）。"""
    return f"""---
name: {SKILL_NAME}
description: 查询 Show Me the Money 行业热点资讯站：今日热点、日报、早报精选、关键词搜索。
  当用户问「今天有什么 AI 新闻」「最近的行业热点」「读一下今天的早报」时使用。
---

# Show Me the Money

一个自托管的行业热点资讯站。数据接口全部匿名只读，无需 Key。

## 何时使用

- 用户想看今天 / 某天的行业热点、AI 新闻、日报、早报；
- 用户想搜索站内文章（标题、摘要、全文）；
- 用户想要「精选几条」推送给别人。

## 接口

基础地址：`{base}`

1. **早报精选（最常用）**：`GET {base}/api/brief`
   返回按用户配置精选的 TOP N（默认 10 条）。`sections` 数组按流水线顺序，
   每节含 `type`（news/text/weather）与 `text`（该节成稿，一节一条直接可发）；
   news 节的条目另有 `title`、`brief`（早报片段）、`source`、`link`。
   天气节（weather）是纯文本，没有长图。

2. **今天的文章**：`GET {base}/api/articles?limit=20`
   返回文章数组；每篇的 `title_zh`（中文标题）、`digest_zh`（中文导读）、
   `brief_zh`（早报片段）都是中文。

3. **某天的日报**：`GET {base}/api/reports/{{date}}`（如 `{base}/api/reports/2026-01-01`）
   `content_md` 是完整 Markdown。

4. **搜索**：`GET {base}/api/search?q=关键词&scope=full`
   `scope=meta`（默认）搜标题与摘要，`scope=full` 连正文一起搜。

## 回答约定

- 引用文章时给出标题与链接；
- 时间统一北京时间；
- 早报片段（`brief`）是用户为手机阅读准备的成品文案，优先直接使用。
"""


@agent_router.get("/llms.txt")
def llms_txt(request: Request) -> PlainTextResponse:
    return PlainTextResponse(_llms_txt(_base(request)), media_type="text/plain; charset=utf-8")


@agent_router.get("/skill.md")
def skill_md(request: Request) -> PlainTextResponse:
    return PlainTextResponse(_skill_md(_base(request)), media_type="text/markdown; charset=utf-8")


def _hermes_prompt(base: str) -> str:
    """给 Hermes 的早报推送提示词（可整段复制发给 Hermes）。

    关键约束（用户实测踩出来的）：
    1. **一条一条发**：微信 iLink 连续发多条会触发 10 秒冷却报错，
       所以文字与每张图各是一条独立消息，发一条等 1.5 秒；
    2. **完全按流水线顺序**：`/api/brief` 的 sections 数组顺序就是流水线顺序；
    3. 图片用 `/api/brief/image?section=N` 直链（服务端渲染好的 PNG），
       文字用每个节点的 `text` 字段（成稿）；
    4. **天气节点只发文字**：它是纯文本（早安问候 + 天气 + 穿衣建议），
       没有长图 —— 请求它的图片会得到 404，不要重试、不要自行制图；
    5. 支持随时点播与任务查询（见提示词里的口令）。
    """
    return f"""【Show Me the Money 早报推送任务】

站点：{base}（全部接口匿名只读，无需 Key）

一、获取当天早报
GET {base}/api/brief
返回 JSON：sections 数组（顺序 = 早报流水线顺序），每个节点含
name（标题）、type（news/text/weather）、text（该节点的成稿纯文本）、
entries（新闻条目）或 meta.text（文字/天气内容）。

二、发送方式（微信 iLink，必须严格遵守）
按 sections 顺序，一次只发一条消息，每条之间 sleep 1.5 秒：
1. 先发该节点的**文字**：直接取该节点的 text 字段（一节一条）；
2. 再发该节点的**图片**：图片地址 {base}/api/brief/image?section=N（N 从 0 开始，
   与 sections 下标一一对应），直接作为图片消息发送，不要转成链接文本；
   **天气节点（type=weather）例外：只发文字，不发图**（该接口对它返回 404，
   这是设计如此，不是错误）；
3. 不要合并成一条、不要并发发送 —— iLink 连续发送会触发 10 秒冷却报错。

三、点播口令（用户在微信/飞书/TG 里说这些话时执行）
- 「早报」/「今日早报」/「发早报」→ 执行上面第一、二步，发今天的早报；
- 「早报 2026-10-07」→ 发指定日期的成品：
  文字 GET {base}/api/brief?date=2026-10-07（stored=true，每个节点的 text 字段），
  图片 {base}/api/brief/image?section=N&date=2026-10-07（weather 节点跳过）；
- 「早报任务」/「有哪些早报」→ GET {base}/api/brief 的 sections 字段，
  列出每个节点的名称与类型（如：1. 今日天气（天气·纯文本）2. 要闻精选（新闻 10 条））；
- 「早报配置」→ 提示用户到 {base}/brief 调整节点与筛选。

四、注意
- 图文都来自上述接口，不要自行改写标题与摘要；
- 若某节点没有内容（entries 为空），跳过该节点的图片，只发一行文字说明。"""


@agent_router.get("/agent", response_class=HTMLResponse)
def agent_page(request: Request) -> HTMLResponse:
    """Agent 接入页：安装步骤 + 可复制提示词 + 接口一览。"""
    base = _base(request)
    return templates.TemplateResponse(
        request,
        "agent.html",
        _ctx(
            request,
            nav="agent",
            title="Agent 接入",
            base=base,
            version=__version__,
            install_prompt=f"请安装 Show Me the Money Skill：{base}/skill.md\n"
            f"装完告诉我是否需要开启新会话。",
            try_prompt="过去 24 小时行业里最重要的 5 件事是什么？",
            hermes_prompt=_hermes_prompt(base),
            endpoints=[
                {"path": "/api/brief", "desc": "早报精选（节点 + 成稿文本；?date= 读成品）"},
                {"path": "/api/brief/image?section=0", "desc": "节点长图 PNG（微信直发；weather 节点无图返回 404）"},
                {"path": "/api/brief/generate", "desc": "POST：立即生成并存档"},
                {"path": "/api/articles", "desc": "今天的文章（?date=&limit=）"},
                {"path": "/api/articles/{id}", "desc": "单篇详情（含早报片段）"},
                {"path": "/api/reports/{date}", "desc": "某天日报（Markdown 全文）"},
                {"path": "/api/search?q=", "desc": "搜索（?scope=meta|full）"},
                {"path": "/api/sources", "desc": "信源列表"},
                {"path": "/api/health", "desc": "服务状态"},
                {"path": "/brief.txt", "desc": "早报纯文本（推送用）"},
                {"path": "/brief.md", "desc": "早报 Markdown"},
                {"path": "/rss", "desc": "最新日报 RSS"},
            ],
        ),
    )


@agent_router.get("/api/search", response_model=list[ArticleOut])
def api_search(
    q: str = Query("", max_length=100),
    scope: str = "meta",
    limit: int = Query(SEARCH_RESULT_LIMIT, ge=0, le=SEARCH_RESULT_LIMIT),
    session: Session = Depends(get_session),
):
    """搜索（与站内搜索同一套口径）。limit=0 返回空列表。"""
    if limit == 0:
        return []
    return search_articles(session, q, scope=normalize_scope(scope), limit=limit)


@agent_router.get("/api/sources")
def api_sources(session: Session = Depends(get_session)) -> list[dict[str, object]]:
    """信源列表（只列未删除的）。"""
    rows = session.execute(
        select(Source).where(Source.deleted == 0).order_by(Source.name)
    ).scalars()
    return [
        {
            "id": row.id,
            "name": row.name,
            "url": row.url,
            "type": row.type,
            "lang": row.lang,
            "enabled": bool(row.enabled),
        }
        for row in rows
    ]
