"""v0.2 新增：老库升级时自动补齐新列。

Base.metadata.create_all() 只建新表，不会给已存在的表加列 ——
没有这段迁移，老用户升级后第一次启动就会 "no such column: articles.digest"。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from app.db import init_db
from app.models import Base


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
