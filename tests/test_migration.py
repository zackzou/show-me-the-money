"""v0.2 新增：老库升级时自动补齐新列。

Base.metadata.create_all() 只建新表，不会给已存在的表加列 ——
没有这段迁移，老用户升级后第一次启动就会 "no such column: articles.digest"。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from app.db import init_db


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
