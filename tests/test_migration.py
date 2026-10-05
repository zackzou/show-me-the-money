"""v0.2 新增：老库升级时自动补齐新列。

Base.metadata.create_all() 只建新表，不会给已存在的表加列 ——
没有这段迁移，老用户升级后第一次启动就会 "no such column: articles.digest"。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from sqlalchemy import create_engine, inspect, select, text

from app.config import Settings
from app.db import init_db
from app.models import Base

from .conftest import make_article


def _old_schema_db(path: Path) -> None:
    """造一个「v0.1 的库」：articles 表没有 digest / image_urls。"""
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE articles (id INTEGER PRIMARY KEY, title VARCHAR(500) NOT NULL)"))
    engine.dispose()


def test_migration_adds_missing_columns(tmp_path: Path):
    db_file = tmp_path / "old.db"
    _old_schema_db(db_file)

    init_db(db_file)  # 旧代码路径：只有 create_all

    engine = create_engine(f"sqlite:///{db_file}")
    cols = {row["name"] for row in inspect(engine).get_columns("articles")}
    engine.dispose()
    assert {"digest", "image_urls"} <= cols


def test_migration_is_idempotent(tmp_path: Path):
    db_file = tmp_path / "twice.db"
    init_db(db_file)
    init_db(db_file)  # 再跑一次不能报错
    engine = create_engine(f"sqlite:///{db_file}")
    cols = [row["name"] for row in inspect(engine).get_columns("articles")]
    engine.dispose()
    assert cols.count("digest") == 1


def test_index_migration_creates_missing_indexes(tmp_path):
    """升级上来的老库必须补齐索引 —— create_all 只为新表建索引。

    实测：ix_articles_duplicate_of 在所有升级上来的库里都缺失（模型里写了
    index=True 也没用），duplicate_of 查询因此退化成全表扫描；而新建的库
    是有的。同一个模型，两种部署行为不一致。
    """
    from sqlalchemy import create_engine, inspect

    from app.db import _INDEX_MIGRATIONS, _migrate_indexes

    db_file = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    # 模拟老库：把索引删掉，模拟「模型加了 index 但库是旧的」
    with engine.begin() as conn:
        for _table, name, _ddl in _INDEX_MIGRATIONS:
            conn.exec_driver_sql(f"DROP INDEX IF EXISTS {name}")

    created = _migrate_indexes(engine)
    assert set(created) == {name for _t, name, _d in _INDEX_MIGRATIONS}

    names = {idx["name"] for idx in inspect(engine).get_indexes("articles")}
    for _table, name, _ddl in _INDEX_MIGRATIONS:
        assert name in names, f"{name} 没有被补上"

    # 幂等：再跑一次不应该重复建、也不应该报错
    assert _migrate_indexes(engine) == []


def test_analyze_populates_statistics(tmp_path):
    """没有 ANALYZE 就没有 sqlite_stat1，规划器只能靠默认值猜。

    实测首页那条查询因此选了低选择性的 ix_articles_relevance 当驱动索引，
    再把所有命中行丢进临时 B 树排序：3 万篇时 176.56ms → ANALYZE 后 56.91ms
    → 补上投影列与 LIMIT 后 0.07ms。
    """
    from sqlalchemy import create_engine, text

    from app.db import _analyze

    engine = create_engine(f"sqlite:///{tmp_path / 'stats.db'}")
    Base.metadata.create_all(engine)
    assert not list(engine.connect().execute(
        text("select name from sqlite_master where name='sqlite_stat1'")))

    _analyze(engine)
    with engine.connect() as conn:
        assert list(conn.execute(text("select name from sqlite_master where name='sqlite_stat1'")))


# ── 历史遗留的矛盾数据 ────────────────────────────────────────────────

def test_repair_clears_score_on_irrelevant_rows(seeded_db):
    """relevance=0 却留着分数的行要清掉。

    模型回 "no 90" 时会写出这种行：页面显示「AI 评分 90」，而任何按 score
    排序或筛选的下游都会把一篇已经不进日报的文章当成高价值内容。
    """
    from app.db import session_scope
    from app.models import Article
    from app.scheduler import repair_inconsistent_rows

    with session_scope() as session:
        bad = Article(title="irrelevant", link="https://example.com/bad-score",
                      relevance=0, score=90, status="processed", content="x")
        good = Article(title="relevant", link="https://example.com/good-score",
                       relevance=1, score=85, status="processed", content="x")
        session.add_all([bad, good])
        session.flush()
        bad_id, good_id = bad.id, good.id

    with session_scope() as session:
        stats = repair_inconsistent_rows(session)

    assert stats.get("score_cleared", 0) >= 1
    with session_scope() as session:
        assert session.get(Article, bad_id).score is None
        assert session.get(Article, good_id).score == 85, "相关文章的分数不能被动"


def test_repair_clamps_impossible_future_dates(seeded_db):
    """published_at 晚于 created_at 是逻辑上不可能的。

    成因是某些源把本地时间当成 GMT（实测 InfoQ 会让文章凭空提前 7.5 小时），
    于��它被算进了错误的那一天日报。
    """
    from datetime import timedelta

    from app.db import session_scope
    from app.models import Article
    from app.scheduler import repair_inconsistent_rows
    from app.utils.text import now_local

    created = now_local() - timedelta(hours=2)
    with session_scope() as session:
        article = Article(title="future dated", link="https://example.com/future-date",
                          relevance=1, status="processed", content="x",
                          created_at=created,
                          published_at=created + timedelta(hours=8))
        session.add(article)
        session.flush()
        article_id = article.id

    with session_scope() as session:
        stats = repair_inconsistent_rows(session)

    assert stats.get("future_dated_clamped", 0) >= 1
    with session_scope() as session:
        stored = session.get(Article, article_id)
        assert stored.published_at <= stored.created_at


def test_backfill_refreshes_stale_report_counts(seeded_db, settings: Settings):
    """已有的日报如果条数对不上，要重新生成而不是跳过。

    早先对已有的日期一律跳过，于是定稿之后再补处理完的文章、或者把重复项
    合并掉之后，存下来的 article_count 与实际能进日报的条数就永久对不上。
    """
    from app.db import session_scope
    from app.models import DailyReport
    from app.scheduler import run_backfill_reports
    from app.utils.text import now_local

    yesterday = (now_local() - timedelta(days=1)).strftime("%Y-%m-%d")
    with session_scope() as session:
        make_article(session, title="当天新闻", published_at=now_local() - timedelta(days=1))
        session.add(DailyReport(date=yesterday, content_md="旧内容", content_html="<p>旧</p>",
                                article_count=99))

    run_backfill_reports(settings, days=3)

    with session_scope() as session:
        report = session.execute(
            select(DailyReport).where(DailyReport.date == yesterday)).scalar_one()
        assert report.article_count == 1, "条数对不上时应该重新生成"
