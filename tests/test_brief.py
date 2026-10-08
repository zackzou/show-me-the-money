"""早报配置与 Agent 接入测试：筛选、排序、渲染、保存、llms.txt/skill.md。"""

from __future__ import annotations

import json
from datetime import timedelta

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import session_scope
from app.models import BriefConfig
from app.report.brief import (
    brief_text,
    build_brief,
    collect_brief_articles,
    config_view,
    load_brief_config,
    render_markdown,
    render_text,
    save_brief_config,
)
from app.utils.text import now_local
from tests.conftest import make_article


def _seed_articles(session):
    """三篇今天、相关、已处理的文章：两篇模型一篇产品；一篇特别关注。"""
    make_article(
        session, title="模型文章A", link="https://example.com/brief-a",
        category="模型", tags="大模型,开源", score=90, starred=1,
        brief_zh="这是模型文章A的早报片段，一段完整的中文话，用于早报推送。",
    )
    make_article(
        session, title="模型文章B", link="https://example.com/brief-b",
        category="模型", tags="芯片", score=80,
        digest_zh="这是模型文章B的中文导读，长度足够撑起早报片段的下限要求，用于测试。",
    )
    make_article(
        session, title="产品文章C", link="https://example.com/brief-c",
        category="产品", tags="芯片,英伟达", score=95,
        brief_zh="这是产品文章C的早报片段，聚焦英伟达与芯片产品。",
    )


def test_brief_default_top_n_and_order(seeded_db, settings: Settings):
    """默认 TOP 10：特别关注最前，其余按评分倒序。"""
    with session_scope() as session:
        _seed_articles(session)

    with session_scope() as session:
        data = build_brief(session)

    assert data["config"]["top_n"] == 10
    assert data["config"]["days"] == 1
    titles = [row["title"] for row in data["rows"]]
    assert titles[0] == "模型文章A"          # 特别关注最前
    assert titles[1] == "产品文章C"          # 95 分
    assert titles[2] == "模型文章B"          # 80 分


def test_brief_category_and_keyword_filters(seeded_db, settings: Settings):
    with session_scope() as session:
        _seed_articles(session)

    with session_scope() as session:
        cfg = config_view(load_brief_config(session))
        cfg["categories"] = ["产品"]
        rows = collect_brief_articles(session, cfg)
    assert [r.title for r in rows] == ["产品文章C"]

    with session_scope() as session:
        cfg = config_view(load_brief_config(session))
        cfg["keywords"] = ["英伟达"]
        rows = collect_brief_articles(session, cfg)
    assert [r.title for r in rows] == ["产品文章C"]

    with session_scope() as session:
        cfg = config_view(load_brief_config(session))
        cfg["tags"] = ["大模型"]
        rows = collect_brief_articles(session, cfg)
    assert [r.title for r in rows] == ["模型文章A"]


def test_brief_exclude_and_starred_only(seeded_db, settings: Settings):
    with session_scope() as session:
        _seed_articles(session)

    with session_scope() as session:
        cfg = config_view(load_brief_config(session))
        cfg["exclude"] = ["模型文章A"]
        rows = collect_brief_articles(session, cfg)
    assert "模型文章A" not in [r.title for r in rows]

    with session_scope() as session:
        cfg = config_view(load_brief_config(session))
        cfg["starred_only"] = True
        rows = collect_brief_articles(session, cfg)
    assert [r.title for r in rows] == ["模型文章A"]


def test_brief_top_n_limits(seeded_db, settings: Settings):
    with session_scope() as session:
        _seed_articles(session)

    with session_scope() as session:
        cfg = config_view(load_brief_config(session))
        cfg["top_n"] = 2
        rows = collect_brief_articles(session, cfg)
    assert len(rows) == 2


def test_brief_days_window(seeded_db, settings: Settings):
    """days=1 不含昨天的文章；days=2 含。"""
    with session_scope() as session:
        make_article(
            session, title="昨天的文章", link="https://example.com/brief-y",
            published_at=now_local() - timedelta(days=1, hours=1),
            brief_zh="昨天文章的早报片段，测试时间窗。",
        )

    with session_scope() as session:
        cfg = config_view(load_brief_config(session))
        cfg["days"] = 1
        assert "昨天的文章" not in [r.title for r in collect_brief_articles(session, cfg)]
        cfg["days"] = 2
        assert "昨天的文章" in [r.title for r in collect_brief_articles(session, cfg)]


def test_brief_text_falls_back_to_digest(seeded_db, settings: Settings):
    """没有 brief_zh 的文章用中文导读兜底（不推英文）。"""
    with session_scope() as session:
        article = make_article(
            session, title="英文源文章", link="https://example.com/brief-en",
            title_zh="英文源文章", digest_zh="", digest="English only digest",
            summary=None, reason=None, brief_zh=None,
        )
        out = brief_text(article)
    assert "English only" not in out
    assert "英文源文章" in out


def test_brief_save_is_full_overwrite(seeded_db, settings: Settings):
    """保存是整表覆盖：字段缺席 = 清空（取消勾选全部后旧值不能残留）。"""
    with session_scope() as session:
        save_brief_config(session, {"top_n": "5", "days": "2",
                                    "categories": ["模型"], "keywords": "芯片,英伟达",
                                    "sort": "time", "starred_only": "1"})
    with session_scope() as session:
        cfg = config_view(load_brief_config(session))
        assert cfg["top_n"] == 5
        assert cfg["days"] == 2
        assert cfg["categories"] == ["模型"]
        assert cfg["keywords"] == ["芯片", "英伟达"]
        assert cfg["sort"] == "time"
        assert cfg["starred_only"] is True

    with session_scope() as session:
        save_brief_config(session, {"top_n": "10", "days": "1", "sort": "score"})
    with session_scope() as session:
        cfg = config_view(load_brief_config(session))
        assert cfg["categories"] == []
        assert cfg["keywords"] == []
        assert cfg["starred_only"] is False


def test_brief_render_formats(seeded_db, settings: Settings):
    with session_scope() as session:
        _seed_articles(session)

    with session_scope() as session:
        data = build_brief(session)

    text = data["text"]
    assert "早报 · " in text
    assert "1. ★" in text            # 特别关注带星
    assert "—— " in text             # 来源行
    md = data["markdown"]
    assert md.startswith("# 早报")
    assert "## 1. ★" in md
    assert "- 原文：https://" in md


def test_brief_page_and_outputs(client: TestClient, seeded_db: str, settings: Settings):
    with session_scope() as session:
        _seed_articles(session)
    page = client.get("/brief")
    assert page.status_code == 200
    assert "早报配置" in page.text
    assert "条数 TOP N" in page.text

    txt = client.get("/brief.txt")
    assert txt.status_code == 200
    assert "早报" in txt.text

    md = client.get("/brief.md")
    assert md.status_code == 200
    assert md.text.startswith("# 早报")

    api = client.get("/api/brief")
    payload = api.json()
    assert payload["total"] >= 1
    assert payload["rows"][0]["brief"]
    assert payload["text"]


def test_brief_form_roundtrip(client: TestClient, seeded_db: str, settings: Settings):
    """表单保存 → 配置生效（含多选分类）。"""
    with session_scope() as session:
        _seed_articles(session)
    # 同名多值字段用 urlencode(doseq=True)，与浏览器多选 checkbox 的编码一致
    from urllib.parse import urlencode

    body = urlencode(
        [("top_n", "2"), ("days", "1"), ("sort", "score"),
         ("categories", "模型"), ("categories", "产品"), ("keywords", "芯片")],
        doseq=True,
    )
    response = client.post(
        "/brief",
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "X-Requested-With": "fetch"},
    )
    assert response.status_code == 200
    assert "已保存" in response.text
    with session_scope() as session:
        cfg = config_view(load_brief_config(session))
    assert cfg["top_n"] == 2
    assert set(cfg["categories"]) == {"模型", "产品"}
    assert cfg["keywords"] == ["芯片"]


def test_brief_config_row_is_singleton(seeded_db, settings: Settings):
    with session_scope() as session:
        load_brief_config(session)
        load_brief_config(session)
        session.commit()
    with session_scope() as session:
        rows = session.query(BriefConfig).all()
        assert len(rows) == 1


def test_brief_preview_api_does_not_persist(client: TestClient, seeded_db: str, settings: Settings):
    with session_scope() as session:
        _seed_articles(session)
    response = client.get("/api/brief/preview?top_n=1&categories=产品")
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["title"] == "产品文章C"
    # 预览不落库：正式配置仍是默认
    with session_scope() as session:
        assert config_view(load_brief_config(session))["top_n"] == 10


# ── Agent 接入 ──────────────────────────────────────────────────────────


def test_llms_txt_lists_endpoints(client: TestClient):
    response = client.get("/llms.txt")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    body = response.text
    assert "# Show Me the Money" in body
    assert "/api/brief" in body
    assert "/api/articles" in body
    assert "/skill.md" in body


def test_skill_md_is_installable(client: TestClient):
    response = client.get("/skill.md")
    assert response.status_code == 200
    body = response.text
    assert body.startswith("---")
    assert "name: show-me-the-money" in body
    assert "description:" in body


def test_agent_page(client: TestClient):
    response = client.get("/agent")
    assert response.status_code == 200
    text = response.text
    assert "Agent 接入" in text
    assert "skill.md" in text
    assert "mcpServers" in text
    assert "/api/brief" in text


def test_api_search_endpoint(client: TestClient, seeded_db: str, settings: Settings):
    with session_scope() as session:
        _seed_articles(session)
    # 搜索口径覆盖标题/摘要/导读等文本列（tags 不在其中），用标题里的词
    response = client.get("/api/search?q=产品文章C")
    assert response.status_code == 200
    rows = response.json()
    assert any("产品文章C" in (row.get("title") or "") for row in rows)
    assert client.get("/api/search?q=&limit=0").json() == []


def test_api_sources_endpoint(client: TestClient, seeded_db: str):
    response = client.get("/api/sources")
    assert response.status_code == 200
    rows = response.json()
    assert rows
    assert rows[0]["name"] == "测试源"


def test_brief_api_rows_are_json_serializable(seeded_db, settings: Settings):
    with session_scope() as session:
        _seed_articles(session)
    with session_scope() as session:
        data = build_brief(session)
    # 确保 JSON 可序列化（时间都已格式化为字符串）
    json.dumps(data, ensure_ascii=False)


def test_render_helpers_are_pure():
    items = [{
        "id": 1, "title": "标题", "brief": "片段。", "source": "来源",
        "category": "模型", "starred": False, "score": 80,
        "link": "https://example.com/x", "published": "2026-01-01 08:00",
    }]
    assert "1. 标题" in render_text(items)
    assert "## 1. 标题" in render_markdown(items)


# ── MCP server（stdio） ─────────────────────────────────────────────────


def test_mcp_server_handshake_and_tools():
    """MCP server：initialize / tools/list 两个基本方法可用。"""
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent / "scripts" / "smtm_mcp.py"
    lines = [
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
        '{"jsonrpc":"2.0","method":"notifications/initialized"}',
        '{"jsonrpc":"2.0","id":2,"method":"tools/list"}',
        '{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"nope","arguments":{}}}',
    ]
    proc = subprocess.run(
        [sys.executable, str(script)],
        input="\n".join(lines) + "\n",
        capture_output=True, text=True, timeout=30,
    )
    responses = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    by_id = {row.get("id"): row for row in responses}
    assert by_id[1]["result"]["serverInfo"]["name"] == "show-me-the-money"
    tools = [t["name"] for t in by_id[2]["result"]["tools"]]
    assert "get_brief" in tools
    assert "search_articles" in tools
    assert by_id[9]["result"]["isError"] is True


# ── 早报片段质量（用户反馈：不能只有标题） ──────────────────────────────


def test_brief_uses_chinese_digest_with_product_names(seeded_db, settings: Settings):
    """导读里混着大量产品名（拉丁）时不能被判成「没翻」而退回标题。

    实测踩过：微软那条导读里 Surface Laptop Ultra / RTX Spark SoC /
    Windows 11 把单句拉丁占比拉到 0.25，逐句检查把它当英文丢掉，
    早报里只剩标题 —— 一段好好的中文导读被浪费。
    """
    from app.report.brief import brief_text

    with session_scope() as session:
        article = make_article(
            session,
            title="微软发布AI新硬件与Windows更新",
            link="https://example.com/brief-product",
            digest=(
                "今天微软发布Surface Laptop Ultra（2599美元起，首发RTX Spark SoC）"
                "及5999美元RTX Spark Dev Box，并推Windows 11变更。"
                "此举聚焦芯片与AI Agent展示本地AI工作流。"
            ),
            brief_zh=None,
        )
        out = brief_text(article)
    assert "Surface" in out, "导读应该被采用，而不是退回标题"
    assert len(out) > 40


def test_brief_falls_back_to_body_when_no_digest(seeded_db, settings: Settings):
    """没有导读时从正文取材（用户反馈：只有标题没有意义）。"""
    from app.report.brief import brief_text

    with session_scope() as session:
        article = make_article(
            session,
            title="只有标题的文章",
            link="https://example.com/brief-body",
            digest=None,
            digest_zh=None,
            reason=None,
            summary=None,
            content_zh="这是一段正文开头，讲述了某个新产品发布的消息，包含关键数字与背景信息，足够支撑起一条早报文案的阅读需求。",
            brief_zh=None,
        )
        out = brief_text(article)
    assert "新产品发布" in out
    assert "只有标题的文章" not in out


# ── 分节（多张长图） ────────────────────────────────────────────────────


def test_brief_sections_filter_per_section(seeded_db, settings: Settings):
    """每节独立筛选：模型节只收模型，产品节只收产品。"""
    from app.report.brief import build_brief

    with session_scope() as session:
        make_article(session, title="模型甲", link="https://example.com/sec-m1",
                     category="模型", brief_zh="模型甲的早报片段，一段完整的中文话。")
        make_article(session, title="模型乙", link="https://example.com/sec-m2",
                     category="模型", brief_zh="模型乙的早报片段，一段完整的中文话。")
        make_article(session, title="产品甲", link="https://example.com/sec-p1",
                     category="产品", brief_zh="产品甲的早报片段，一段完整的中文话。")
        save_brief_config(session, {
            "top_n": "10", "days": "1", "sort": "score",
            "sections": json.dumps([
                {"name": "模型精选", "top_n": 5, "categories": ["模型"]},
                {"name": "产品速递", "top_n": 5, "categories": ["产品"]},
            ], ensure_ascii=False),
        })
    with session_scope() as session:
        data = build_brief(session)
    assert len(data["sections"]) == 2
    names = [s["name"] for s in data["sections"]]
    assert names == ["模型精选", "产品速递"]
    assert {e["title"] for e in data["sections"][0]["entries"]} == {"模型甲", "模型乙"}
    assert [e["title"] for e in data["sections"][1]["entries"]] == ["产品甲"]
    # 分节标题进入两种渲染
    assert "【模型精选】" in data["text"]
    assert "## 模型精选" in data["markdown"]


def test_brief_sections_fall_back_to_single(seeded_db, settings: Settings):
    """没有分节配置时是单节（name 为空），兼容旧行为。"""
    from app.report.brief import build_brief

    with session_scope() as session:
        make_article(session, title="普通条目", link="https://example.com/sec-single",
                     brief_zh="普通条目的早报片段。")
    with session_scope() as session:
        data = build_brief(session)
    assert len(data["sections"]) == 1
    assert data["sections"][0]["name"] == ""


def test_brief_sections_save_roundtrip(seeded_db, settings: Settings):
    from app.report.brief import config_view, load_brief_config

    sections = [
        {"name": "AI 芯片", "top_n": 6, "categories": ["模型"], "topics": [],
         "tags": ["芯片"], "keywords": ["英伟达"]},
        {"name": "行业动态", "top_n": 4, "categories": ["行业"], "topics": [],
         "tags": [], "keywords": []},
    ]
    with session_scope() as session:
        save_brief_config(session, {"top_n": "10", "days": "1", "sort": "score",
                                    "sections": json.dumps(sections, ensure_ascii=False)})
    with session_scope() as session:
        cfg = config_view(load_brief_config(session))
    assert [s["name"] for s in cfg["sections"]] == ["AI 芯片", "行业动态"]
    assert cfg["sections"][0]["top_n"] == 6
    assert cfg["sections"][0]["tags"] == ["芯片"]


def test_brief_sections_bad_json_is_ignored(seeded_db, settings: Settings):
    from app.report.brief import config_view, load_brief_config

    with session_scope() as session:
        save_brief_config(session, {"top_n": "10", "days": "1", "sort": "score",
                                    "sections": "{not json"})
    with session_scope() as session:
        cfg = config_view(load_brief_config(session))
    assert cfg["sections"] == []


# ── 成品存档 + 立即生成 + 每天 6 点 ─────────────────────────────────────


def test_brief_issue_saved_and_listed(client: TestClient, seeded_db: str, settings: Settings):
    with session_scope() as session:
        _seed_articles(session)
    response = client.post("/api/brief/generate", headers={"X-Requested-With": "fetch"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["total"] >= 1
    date = payload["date"]

    # 按日期读回成品
    stored = client.get(f"/api/brief/issue?date={date}")
    assert stored.status_code == 200
    body = stored.json()
    assert body["date"] == date
    assert body["sections"]

    # 配置页历史里能看到
    page = client.get("/brief")
    assert date in page.text


def test_brief_issue_snapshot_not_affected_by_config_change(
    client: TestClient, seeded_db: str, settings: Settings
):
    """成品是快照：改配置不回溯改写历史。"""
    with session_scope() as session:
        _seed_articles(session)
    date = client.post("/api/brief/generate",
                       headers={"X-Requested-With": "fetch"}).json()["date"]
    before = client.get(f"/api/brief/issue?date={date}").json()

    # 改成只收产品
    with session_scope() as session:
        save_brief_config(session, {"top_n": "1", "days": "1", "sort": "score",
                                    "categories": ["产品"]})
    after = client.get(f"/api/brief/issue?date={date}").json()
    assert after["article_count"] == before["article_count"]
    assert after["text"] == before["text"]


def test_brief_api_with_date_reads_issue(client: TestClient, seeded_db: str, settings: Settings):
    with session_scope() as session:
        _seed_articles(session)
    date = client.post("/api/brief/generate",
                       headers={"X-Requested-With": "fetch"}).json()["date"]
    stored = client.get(f"/api/brief?date={date}").json()
    assert stored["stored"] is True
    assert stored["text"]
    assert client.get("/api/brief?date=1999-01-01").status_code == 404


def test_run_brief_job_creates_issue(seeded_db, settings: Settings):
    from app.models import BriefIssue
    from app.scheduler import run_brief_job

    with session_scope() as session:
        _seed_articles(session)
    result = run_brief_job(settings)
    assert result["articles"] >= 1
    with session_scope() as session:
        rows = session.query(BriefIssue).all()
        assert len(rows) == 1
        assert rows[0].date == result["date"]


def test_schedule_has_brief_time_default(settings: Settings):
    assert settings.schedule.brief_time == "06:00"


# ── 站点图标（favicon） ────────────────────────────────────────────────


def test_favicon_assets_are_served(client: TestClient):
    svg = client.get("/favicon.svg")
    assert svg.status_code == 200
    assert "image/svg" in svg.headers["content-type"]
    assert "<svg" in svg.text
    ico = client.get("/favicon.ico")
    assert ico.status_code == 200
    assert client.get("/apple-touch-icon.png").status_code == 200


def test_base_html_links_favicon(client: TestClient):
    text = client.get("/").text
    assert 'rel="icon"' in text
    assert "/favicon.svg" in text
    assert "/apple-touch-icon.png" in text


def test_brief_excludes_placeholder_entries(seeded_db, settings: Settings):
    """没素材的条目从早报剔除，而不是推一条「译文整理中」占位符。

    单篇页的早报片段浮层允许占位（页面要有话说），早报成稿不允许。
    """
    from app.report.brief import brief_items, brief_text

    with session_scope() as session:
        good = make_article(session, title="有素材", link="https://example.com/ph-good",
                            brief_zh="有素材的早报片段，一段完整的中文话，读得下去。")
        bad = make_article(session, title="English Only Article", link="https://example.com/ph-bad",
                           title_zh=None, digest=None, digest_zh=None, reason=None,
                           summary=None, content_zh=None, content_full=None, content=None,
                           brief_zh=None)
        items = brief_items([good, bad])
    assert [i["title"] for i in items] == ["有素材"]
    # 单篇页（allow_placeholder）仍有兜底文案
    with session_scope() as session:
        article = session.get(type(bad), bad.id)
        assert brief_text(article, allow_placeholder=True)
        assert brief_text(article) == ""


# ── 流水线节点类型（news / text / weather） ─────────────────────────────


def test_brief_text_and_weather_nodes(seeded_db, settings: Settings):
    """文字与天气节点：固定内容，没有文章，也能进两种渲染。"""
    from app.report.brief import build_brief

    with session_scope() as session:
        make_article(session, title="新闻甲", link="https://example.com/node-news",
                     brief_zh="新闻甲的早报片段。")
        save_brief_config(session, {
            "top_n": "10", "days": "1", "sort": "score",
            "sections": json.dumps([
                {"type": "weather", "name": "今日天气", "cities": ["上海"],
                 "text": "晴，18~26°C，适合出行。", "enabled": True},
                {"type": "text", "name": "编者按", "title": "编者按",
                 "text": "今天重点关注芯片与 Agent 方向。", "enabled": True},
                {"type": "news", "name": "要闻", "top_n": 5,
                 "categories": [], "topics": [], "tags": [], "keywords": [], "enabled": True},
            ], ensure_ascii=False),
        })
    with session_scope() as session:
        data = build_brief(session)

    types = [s["type"] for s in data["sections"]]
    assert types == ["weather", "text", "news"]
    assert data["sections"][0]["meta"]["cities"] == ["上海"]
    assert data["sections"][2]["entries"][0]["title"] == "新闻甲"
    # 天气节点渲染的是实时天气（emoji 格式）或「取不到」的明确提示，
    # 都带城市名；用户补充说明原样保留
    assert "上海" in data["sections"][0]["meta"]["text"]
    assert "18~26" in data["text"]
    assert "编者按" in data["markdown"]
    assert "【今日天气】" in data["text"]


def test_weather_section_is_plain_text(seeded_db, settings: Settings):
    """天气节点按纯文本推送：单节 text 无 ** 标记、不重复节点名。"""
    from app.report.brief import build_brief

    with session_scope() as session:
        save_brief_config(session, {
            "top_n": "10", "days": "1", "sort": "score",
            "sections": json.dumps([
                {"type": "weather", "name": "今日天气", "cities": [],
                 "text": "晴，18~26°C。", "enabled": True},
                {"type": "text", "name": "编者按", "title": "编者按",
                 "text": "今天重点关注芯片。", "enabled": True},
            ], ensure_ascii=False),
        })
    with session_scope() as session:
        data = build_brief(session)

    sec = data["sections"][0]
    # 单节 text：直接可发手机/Hermes（没有 ** 标记）
    assert sec["text"]
    assert "**" not in sec["text"]
    # 整体成稿里【今日天气】抬头只出现一次（节点名不重复进正文）
    assert data["text"].count("今日天气") == 1
    assert "【今日天气】" in data["text"]
    # 文字节点标题与节点名相同时也不重复
    assert data["text"].count("编者按") == 1


def test_brief_image_endpoint_skips_weather(client: TestClient, seeded_db: str, settings: Settings):
    """天气节点没有长图：请求图片返回 404 并说明（Hermes 据此只发文字）。"""
    with session_scope() as session:
        save_brief_config(session, {
            "top_n": "10", "days": "1", "sort": "score",
            "sections": json.dumps([
                {"type": "weather", "name": "今日天气", "cities": [],
                 "text": "晴，18~26°C。", "enabled": True},
            ], ensure_ascii=False),
        })
    response = client.get("/api/brief/image?section=0")
    assert response.status_code == 404
    assert "纯文本" in response.text


def test_api_brief_sections_have_text_field(client: TestClient, seeded_db: str, settings: Settings):
    """/api/brief 每个节点带 text 字段：Hermes 一节一条发文字。"""
    with session_scope() as session:
        _seed_articles(session)
    data = client.get("/api/brief").json()
    assert data["sections"]
    for section in data["sections"]:
        assert "text" in section
        assert isinstance(section["text"], str)
    news = [s for s in data["sections"] if s["type"] == "news"][0]
    assert "模型文章A" in news["text"]


def test_brief_disabled_node_is_skipped(seeded_db, settings: Settings):
    """停用的节点不进产出（预览与成稿都不出现），但配置里保留。"""
    from app.report.brief import build_brief, config_view, load_brief_config

    with session_scope() as session:
        make_article(session, title="新闻乙", link="https://example.com/node-off",
                     brief_zh="新闻乙的早报片段。")
        save_brief_config(session, {
            "top_n": "10", "days": "1", "sort": "score",
            "sections": json.dumps([
                {"type": "text", "name": "停用卡", "title": "不该出现", "text": "xxx",
                 "enabled": False},
                {"type": "news", "name": "要闻", "top_n": 5,
                 "categories": [], "topics": [], "tags": [], "keywords": [], "enabled": True},
            ], ensure_ascii=False),
        })
    with session_scope() as session:
        data = build_brief(session)
    assert [s["name"] for s in data["sections"]] == ["要闻"]
    assert "不该出现" not in data["text"]
    # 配置里保留（停用不是删除）
    with session_scope() as session:
        cfg = config_view(load_brief_config(session))
    assert len(cfg["sections"]) == 2
    assert cfg["sections"][0]["enabled"] is False


def test_brief_node_type_defaults_to_news(seeded_db, settings: Settings):
    """老配置没有 type 字段时按新闻节点处理（向后兼容）。"""
    from app.report.brief import config_view, load_brief_config

    with session_scope() as session:
        save_brief_config(session, {
            "top_n": "10", "days": "1", "sort": "score",
            "sections": json.dumps([{"name": "旧节点", "top_n": 3}], ensure_ascii=False),
        })
    with session_scope() as session:
        cfg = config_view(load_brief_config(session))
    assert cfg["sections"][0]["type"] == "news"
    assert cfg["sections"][0]["enabled"] is True


def test_brief_page_has_flow_ui(client: TestClient, seeded_db: str, settings: Settings):
    """配置页含流水线节点 UI、tab 预览与长图预览按钮。"""
    page = client.get("/brief")
    assert page.status_code == 200
    text = page.text
    assert 'id="bf-flow"' in text
    assert "新闻节点" in text
    assert "文字节点" in text
    assert "天气节点" in text
    assert 'id="bf-tabs"' in text
    assert "预览长图" in text
    assert 'id="bf-save-top"' in text


# ── 天气模块 ───────────────────────────────────────────────────────────


def test_weather_markdown_renders_emoji_and_advice(monkeypatch):
    """天气渲染：emoji + 城市 + 温度 + 穿衣/大风提示（不打真网络）。"""
    import httpx

    from app.report import weather as W

    def fake_get(self, url, **kw):
        if "geocoding" in url:
            payload = {"results": [{"name": "上海", "latitude": 31.2, "longitude": 121.5,
                                    "admin1": "上海市"}]}
        else:
            payload = {
                "current": {"temperature_2m": 33.0, "weather_code": 95,
                            "wind_speed_10m": 90.0, "precipitation": 1.2},
                "daily": {"temperature_2m_max": [35.0], "temperature_2m_min": [28.0],
                          "precipitation_probability_max": [80]},
            }
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    W._cache.clear()  # 缓存按小时生效，避免其它用例先缓存了真实数据
    md = W.render_weather_markdown(["上海"])
    assert "⛈️" in md          # 雷暴 emoji
    assert "**上海**" in md
    assert "28~35" in md or "28~35°C" in md
    assert "带伞" in md          # 降水概率 80% → 提醒带伞
    assert "台风" in md          # 风速 90km/h → 台风级提醒
    # 晨间格式：早安问候 + 穿衣推荐 + 心情语录（用户指定）
    assert "早安" in md
    assert "穿衣推荐" in md
    assert "✨" in md


def test_weather_unknown_city_is_explicit(monkeypatch):
    import httpx

    from app.report import weather as W

    def fake_get(self, url, **kw):
        return httpx.Response(200, json={"results": []}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    W._cache.clear()
    md = W.render_weather_markdown(["不存在城"])
    assert "不存在城" in md
    assert "取不到" in md


# ── 空节点提示 ─────────────────────────────────────────────────────────


def test_empty_news_node_has_hint(seeded_db, settings: Settings):
    """空新闻节点给出明确原因（用户反馈：不能一片空白）。"""
    from app.report.brief import build_brief

    with session_scope() as session:
        make_article(session, title="普通文章", link="https://example.com/hint-1",
                     category="模型", brief_zh="普通文章的早报片段。")
        save_brief_config(session, {
            "top_n": "10", "days": "1", "sort": "score",
            "sections": json.dumps([
                {"type": "news", "name": "论文节", "top_n": 5,
                 "categories": ["论文"], "topics": [], "tags": [], "keywords": [],
                 "enabled": True},
            ], ensure_ascii=False),
        })
    with session_scope() as session:
        data = build_brief(session)
    assert data["sections"][0]["entries"] == []
    hint = data["sections"][0]["hint"]
    assert "分类：论文" in hint
    assert "没有一篇同时满足" in hint


def test_empty_hint_when_no_articles_at_all(seeded_db, settings: Settings):
    """时间范围内一篇都没有时，提示指向「放宽时间/等抓取」。"""
    from app.report.brief import build_brief

    with session_scope() as session:
        save_brief_config(session, {"top_n": "10", "days": "1", "sort": "score"})
    with session_scope() as session:
        data = build_brief(session)
    assert data["total"] == 0
    assert "没有任何已处理的文章" in data["sections"][0]["hint"]


# ── 服务端长图 + Hermes 提示词 ─────────────────────────────────────────


def test_brief_image_endpoint_renders_png(client: TestClient, seeded_db: str, settings: Settings):
    """节点长图接口：返回 PNG（Hermes 微信直发的图片源）。"""
    with session_scope() as session:
        _seed_articles(session)
    response = client.get("/api/brief/image?section=0")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    # PNG 魔数
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(response.content) > 1000


def test_emoji_tile_is_colorful_and_scaled_to_text():
    """emoji 长图渲染：彩色（非黑/白）、缩放到正文行高以内。

    回归点：容器里的 NotoColorEmoji 是位图字体，只能在原生 109px
    加载；之前按请求字号加载失败直接回退普通字体 → 全变方框，
    或画出 109px 巨图压住正文。瓦片方案两处都要守住。
    """
    from app.report.longimage import _emoji_font, _emoji_tile

    emoji = _emoji_font(30)
    if emoji is None:  # 环境里没有任何 emoji 字体（CI 精简镜像）时跳过
        import pytest

        pytest.skip("没有可用 emoji 字体")
    font, native = emoji
    tile = _emoji_tile("☀", font, native, 30)
    assert tile is not None
    # 缩放到目标字号附近（正文 30px），不允许原生 109px 直接上屏
    assert tile.width <= 45
    assert tile.height <= 45
    # 彩色：不透明的像素里至少有明显的红/黄分量（不是黑白方框）
    raw = tile.convert("RGBA").tobytes()
    opaque = [raw[i:i + 4] for i in range(0, len(raw), 4) if raw[i + 3] > 200]
    assert opaque, "emoji 瓦片全透明"
    assert any(px[0] > 150 and px[1] > 100 for px in opaque)


def test_draw_text_skips_variation_selector_without_tofu():
    """变体选择符 FE0F 跟随前一个 emoji 绘制，不单独画方框。"""
    from PIL import Image

    from app.report.longimage import _draw_text, _emoji_font, _font

    if _emoji_font(30) is None:
        import pytest

        pytest.skip("没有可用 emoji 字体")

    def ink_span(text: str) -> int:
        img = Image.new("RGB", (300, 60), "white")
        _draw_text(img, (10, 10), text, _font(30), "#000000")
        xs = [x for x in range(300) for y in range(60) if img.getpixel((x, y)) != (255, 255, 255)]
        assert xs, "文字没画上"
        return max(xs) - min(xs)

    # "XY" 与 "X️Y" 占宽应几乎一致；单独把 FE0F 画成方框会多出一个字符宽
    assert abs(ink_span("X\ufe0fY") - ink_span("XY")) < 10


def test_brief_image_subtitle_reflects_filters(seeded_db, settings: Settings):
    """长图副标题：不限时显示排序方式；选了条件逐项列出。"""
    from app.report.brief import _section_subtitle

    node = {"categories": ["模型"], "topics": [], "tags": ["芯片"], "keywords": ["OpenAI"]}
    cfg = {"starred_only": False, "sort": "score"}
    sub = _section_subtitle(node, cfg)
    assert "分类：模型" in sub
    assert "标签：#芯片" in sub
    assert "关键词：OpenAI" in sub
    assert "评分优先" in sub

    empty = _section_subtitle({"categories": [], "topics": [], "tags": [], "keywords": []}, cfg)
    assert empty == "全部相关新闻 · 评分优先"

    time_cfg = {"starred_only": True, "sort": "time"}
    sub2 = _section_subtitle({"categories": [], "topics": [], "tags": [], "keywords": []}, time_cfg)
    assert "时间优先" in sub2
    assert "只看特别关注" in sub2


def test_agent_page_has_hermes_prompt(client: TestClient):
    """Agent 接入页有 Hermes 早报提示词（可整段复制）与口令说明。"""
    page = client.get("/agent")
    assert page.status_code == 200
    text = page.text
    assert "Hermes 早报推送" in text
    assert 'id="hermes-prompt"' in text
    # 提示词关键内容：分条发送、图片直链、点播口令
    assert "sleep 1.5" in text or "1.5 秒" in text
    assert "/api/brief/image?section=N" in text
    assert "早报任务" in text
    assert "iLink" in text or "冷却" in text


def test_hermes_prompt_contains_base_url(client: TestClient):
    page = client.get("/agent")
    # 提示词里必须有站点地址（复制走就能直接用）
    assert "/api/brief" in page.text


def test_hermes_prompt_uses_configured_public_url(db: str):
    """SMTM_PUBLIC_URL 配置后，提示词/llms.txt/RSS 用配置的地址。

    场景：反代 https 终止在 nginx/Caddy，或浏览器走内网而 Hermes 走公网 ——
    按请求 Host 推导会给出 Hermes 到不了的地址，必须能显式覆盖。
    """
    from app.config import load_settings
    from app.main import create_app
    from tests.conftest import CONFIG_DIR, ENV

    settings = load_settings(
        env={**ENV, "SMTM_PUBLIC_URL": "https://news.example.com"},
        config_dir=CONFIG_DIR,
    )
    app = create_app(settings, bootstrap=False)
    with TestClient(app) as test_client:
        # 浏览器用内网地址访问，提示词仍用配置的公网地址
        page = test_client.get("/agent", headers={"Host": "192.168.1.50:8000"})
        assert "https://news.example.com" in page.text
        assert "192.168.1.50" not in page.text
        llms = test_client.get("/llms.txt", headers={"Host": "192.168.1.50:8000"})
        assert "https://news.example.com" in llms.text
        assert "192.168.1.50" not in llms.text


def test_hermes_prompt_falls_back_to_request_host(client: TestClient):
    """未配置 SMTM_PUBLIC_URL 时按浏览器访问的 Host 推导（内网 IP 原样带上）。"""
    page = client.get("/agent", headers={"Host": "192.168.1.50:8000"})
    assert "http://192.168.1.50:8000" in page.text


def test_weather_morning_format(monkeypatch):
    """天气是晨间格式：早安问候 + 天气 + 穿衣推荐 + 心情语录。"""
    import httpx

    from app.report import weather as W

    def fake_get(self, url, **kw):
        if "geocoding" in url:
            return httpx.Response(200, json={"results": [
                {"name": "上海", "latitude": 31.2, "longitude": 121.5, "admin1": "上海市"}]},
                request=httpx.Request("GET", url))
        return httpx.Response(200, json={
            "current": {"temperature_2m": 20.0, "weather_code": 1,
                        "wind_speed_10m": 10.0, "precipitation": 0.0},
            "daily": {"temperature_2m_max": [24.0], "temperature_2m_min": [15.0],
                      "precipitation_probability_max": [10]},
        }, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    W._cache.clear()
    md = W.render_weather_markdown(["上海"])
    assert "🌅 早安" in md
    assert "**上海**" in md
    assert "👕 穿衣推荐" in md
    assert "✨" in md  # 心情语录
