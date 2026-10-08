"""新增管理功能回归：回收站、关键词规则、数据报告。

这些功能的共同口径是「**展示层只看到想看的**」：
- 回收站里的文章从列表 / 日报 / 搜索 / RSS 全部消失（``deleted_at``）；
- 屏蔽词命中不收集（入库前拦）或移入回收站（正文级）；
- 关注词命中置 ``starred``，列表优先排序并带标记；
- 报告页聚合的数字与这些状态一致。
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.db import session_scope
from app.fetcher.keywords import KIND_BLOCK, KIND_STAR, load_rules, matches
from app.models import Article, KeywordRule
from app.report.generator import generate_daily_report
from app.utils.text import now_local
from tests.conftest import make_article


def _today() -> str:
    return now_local().strftime("%Y-%m-%d")


# ── 关键词匹配口径 ───────────────────────────────────────────


def test_keyword_matches_latin_word_boundary(seeded_db):
    """英文词按词边界：屏蔽 Muse 不该误伤 museum / amuse。"""
    with session_scope() as session:
        session.add(KeywordRule(keyword="Muse", kind=KIND_BLOCK))
        session.add(KeywordRule(keyword="OpenAI", kind=KIND_STAR))
        session.commit()
        blocks = load_rules(session, kind=KIND_BLOCK)

    assert matches("Meta 发布 Muse 智能体", blocks) is not None
    assert matches("MUSE 大写也命中", blocks) is not None
    # 词边界：museum / amuse 不算命中 —— 实测踩过的坑（裸 substring 全拦了）
    assert matches("The museum opens at nine", blocks) is None
    assert matches("I am amused", blocks) is None
    # 中文直接包含
    with session_scope() as session:
        session.add(KeywordRule(keyword="芯片", kind=KIND_BLOCK))
        session.commit()
        blocks = load_rules(session, kind=KIND_BLOCK)
    assert matches("最新芯片发布", blocks) is not None
    assert matches("没有那个词", blocks) is None


def test_star_rule_marks_article(seeded_db):
    """关注词命中：starred 置 1，命中计数累加。"""
    from app.fetcher.keywords import apply_rules

    with session_scope() as session:
        session.add(KeywordRule(keyword="OpenAI", kind=KIND_STAR))
        session.commit()
        rules = load_rules(session, kind=KIND_STAR)
        article = make_article(
            session, title="OpenAI 发布新模型", link="https://example.com/star1",
            relevance=1, status="processed",
        )
        result = apply_rules(session, article, star_rules=rules)
        assert result == "star"
        assert article.starred == 1
    with session_scope() as session:
        rule = session.execute(select(KeywordRule)).scalar_one()
        assert rule.hits == 1


def test_block_rule_soft_deletes_article(seeded_db):
    """正文级屏蔽：命中即软删（进回收站），不是物理删除。"""
    from app.fetcher.keywords import apply_rules

    with session_scope() as session:
        session.add(KeywordRule(keyword="Muse", kind=KIND_BLOCK))
        session.commit()
        rules = load_rules(session, kind=KIND_BLOCK)
        article = make_article(
            session, title="Meta Muse 智能体实测", link="https://example.com/block1",
            relevance=1, status="processed",
        )
        result = apply_rules(session, article, block_rules=rules)
        assert result == "block"
        assert article.deleted_at is not None


# ── 回收站 ───────────────────────────────────────────────────


def test_deleted_article_hidden_from_lists(client: TestClient, settings: Settings, seeded_db):
    """软删除后：首页/搜索/日报/RSS 都不再出现，回收站里能看到。"""
    with session_scope() as session:
        article = make_article(
            session, title="将被删除的文章", link="https://example.com/del1",
            digest="导语内容", relevance=1, status="processed",
        )
        article_id = article.id
    with session_scope() as session:
        generate_daily_report(_today(), session=session, settings=settings)

    # 删除前可见
    assert "将被删除的文章" in client.get("/").text
    # 软删除
    response = client.post(f"/api/articles/{article_id}/delete")
    assert response.status_code == 200
    assert response.json()["ok"] is True

    # 所有展示入口都不可见
    assert "将被删除的文章" not in client.get("/").text
    assert "将被删除的文章" not in client.get("/search?q=将被删除").text
    assert "将被删除的文章" not in client.get("/rss").text
    # 回收站可见
    trash = client.get("/trash")
    assert "将被删除的文章" in trash.text


def test_restore_brings_article_back(client: TestClient, settings: Settings, seeded_db):
    with session_scope() as session:
        article = make_article(
            session, title="恢复测试文章", link="https://example.com/restore1",
            digest="导语", relevance=1, status="processed",
        )
        article_id = article.id
    client.post(f"/api/articles/{article_id}/delete")
    assert "恢复测试文章" not in client.get("/").text
    assert client.post(f"/api/articles/{article_id}/restore").json()["ok"] is True
    assert "恢复测试文章" in client.get("/").text


def test_purge_removes_permanently(client: TestClient, seeded_db):
    with session_scope() as session:
        article = make_article(
            session, title="永久删除测试", link="https://example.com/purge1",
            digest="导语", relevance=1, status="processed",
        )
        article_id = article.id
    client.post(f"/api/articles/{article_id}/delete")
    response = client.post("/api/trash/purge", json={"ids": str(article_id)})
    assert response.json()["ok"] is True
    assert response.json()["removed"] == 1
    with session_scope() as session:
        assert session.get(Article, article_id) is None


def test_empty_trash_purges_everything(client: TestClient, seeded_db):
    with session_scope() as session:
        for index in range(3):
            article = make_article(
                session, title=f"批量删除 {index}", link=f"https://example.com/bulk{index}",
                digest="导语", relevance=1, status="processed",
            )
            session.flush()
            article.deleted_at = now_local()
    response = client.post("/api/trash/purge", json={})
    assert response.json()["removed"] == 3
    with session_scope() as session:
        assert session.execute(select(Article).where(Article.deleted_at.isnot(None))).first() is None


# ── 关键词管理页 ─────────────────────────────────────────────


def test_keywords_page_add_toggle_delete(client: TestClient, seeded_db):
    page = client.get("/keywords")
    assert page.status_code == 200
    assert "关键词" in page.text

    # 添加屏蔽词
    response = client.post("/keywords", data={"keyword": "Muse", "kind": "block"})
    assert response.status_code == 200
    assert "已添加屏蔽" in response.text
    assert "Muse" in response.text

    # 重复添加：提示而不是报错
    response = client.post("/keywords", data={"keyword": "Muse", "kind": "block"})
    assert "已经在" in response.text

    # 添加关注词
    client.post("/keywords", data={"keyword": "OpenAI", "kind": "star"})
    page = client.get("/keywords")
    assert "OpenAI" in page.text

    with session_scope() as session:
        rule = session.execute(
            select(KeywordRule).where(KeywordRule.keyword == "Muse")
        ).scalar_one()
        rule_id = rule.id

    # 停用
    response = client.post(f"/keywords/{rule_id}/toggle")
    assert response.json()["enabled"] is False
    # 删除
    response = client.post(f"/keywords/{rule_id}/delete")
    assert response.json()["ok"] is True
    with session_scope() as session:
        assert session.get(KeywordRule, rule_id) is None


# ── 报告页 ───────────────────────────────────────────────────


def test_reports_page_renders_with_data(client: TestClient, settings: Settings, seeded_db):
    with session_scope() as session:
        make_article(session, title="报告文章一", link="https://example.com/rep1",
                     digest="导语", relevance=1, status="processed",
                     category="模型", tags="OpenAI,Agent", score=88)
        make_article(session, title="报告文章二", link="https://example.com/rep2",
                     digest="导语", relevance=1, status="processed",
                     category="行业", tags="芯片", score=65)

    page = client.get("/reports")
    assert page.status_code == 200
    assert "数据报告" in page.text
    # KPI 与分布区块都在
    assert "收录文章" in page.text
    assert "分类分布" in page.text
    assert "来源分布" in page.text
    assert "评分分布" in page.text
    assert "发布时段分布" in page.text
    # 分类出现在图表里（可点击下钻）
    assert "模型" in page.text
    assert "/reports/breakdown" in page.text


def test_reports_breakdown_drilldown(client: TestClient, settings: Settings, seeded_db):
    with session_scope() as session:
        make_article(session, title="模型类明细文章", link="https://example.com/bd1",
                     digest="导语", relevance=1, status="processed", category="模型")
        make_article(session, title="行业类明细文章", link="https://example.com/bd2",
                     digest="导语", relevance=1, status="processed", category="行业")

    page = client.get("/reports/breakdown", params={"dimension": "category", "value": "模型"})
    assert page.status_code == 200
    assert "模型类明细文章" in page.text
    assert "行业类明细文章" not in page.text
    assert "返回报告" in page.text


def test_reports_range_normalized(client: TestClient, seeded_db):
    """非法的 days 值回退到 30，不报错。"""
    assert client.get("/reports?days=999").status_code == 200
    assert client.get("/reports?days=abc").status_code == 200
    assert client.get("/reports?days=7").status_code == 200


# ── 状态页 ───────────────────────────────────────────────────


def test_status_page_is_themed(client: TestClient, seeded_db):
    """服务状态是站点样式的页面，不是裸 JSON（原来点「⋯」是一屏黑底 JSON）。"""
    page = client.get("/status")
    assert page.status_code == 200
    assert "服务状态" in page.text
    # 站点样式（引用了 base 的变量与外壳）
    assert "stat-grid" in page.text
    assert "回收站" in page.text
    # JSON 接口仍然保留给脚本与监控
    assert client.get("/api/health").json()["status"] == "ok"


# ── 关注标记在列表与排序 ─────────────────────────────────────


def test_starred_article_sorted_first_and_marked(client: TestClient, settings: Settings, seeded_db):
    """关注的文章排在当天最前，并带「★ 关注」标记。"""
    from datetime import timedelta

    now = now_local()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # 跨午夜保护：刚过 00:0x 时 now-3h 会落到昨天，掉出今天的时间窗。
    # 关注文章取「今天零点之后尽量早」的时刻，普通文章取现在。
    starred_at = max(midnight, now - timedelta(hours=3))
    normal_at = max(starred_at + timedelta(minutes=1), now)
    with session_scope() as session:
        make_article(session, title="普通文章", link="https://example.com/normal",
                     digest="导语", relevance=1, status="processed",
                     published_at=normal_at)
        make_article(session, title="关注文章", link="https://example.com/starred",
                     digest="导语", relevance=1, status="processed",
                     published_at=starred_at, starred=1)
    with session_scope() as session:
        generate_daily_report(_today(), session=session, settings=settings)

    text = client.get("/").text
    assert "★ 关注" in text
    # 关注文章排在普通文章前面（虽然发布时间更早或相同）
    assert text.index("关注文章") < text.index("普通文章")
