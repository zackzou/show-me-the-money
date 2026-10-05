"""数据库连接与初始化。"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import SourceConfig
from app.models import Base, Source
from app.utils.logger import get_logger
from app.utils.text import now_local

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
_db_file: Path | None = None

log = get_logger(__name__)


def _apply_sqlite_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_connection: object, _record: object) -> None:  # pragma: no cover - 驱动回调
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


# 抓取与处理是两个独立任务，可能同时写 SQLite；默认 5 秒拿不到写锁就直接抛
# "database is locked"，让整个抓取任务失败。放到 30 秒，让它排队而不是报错。
SQLITE_BUSY_TIMEOUT_SECONDS = 30

# 新增列的轻量迁移表：列名 -> DDL 片段。
# Base.metadata.create_all() 只建新表，**不会给已存在的表加列**，
# 所以老用户升级后第一次启动会直接报 "no such column"。这里幂等补齐。
_COLUMN_MIGRATIONS: dict[str, str] = {
    "articles.digest": "TEXT",
    "articles.image_urls": "TEXT",
    "articles.content_full": "TEXT",
    "articles.reason": "TEXT",
    "articles.score": "INTEGER",
    "articles.category": "VARCHAR(20)",
    "articles.topics": "TEXT",
    "articles.title_en": "VARCHAR(500)",
    "articles.title_zh": "VARCHAR(500)",
    "articles.digest_en": "TEXT",
    "articles.digest_zh": "TEXT",
    "articles.content_zh": "TEXT",
    "articles.body_images": "TEXT",
    "articles.i18n_attempts": "INTEGER NOT NULL DEFAULT 0",
    "articles.duplicate_of": "INTEGER",
    "articles.process_attempts": "INTEGER NOT NULL DEFAULT 0",
    "articles.process_last_at": "DATETIME",
    "articles.degraded_reason": "VARCHAR(300)",
    "articles.media_map": "TEXT",
    "articles.brief_zh": "TEXT",
    "articles.body_sections": "TEXT",
    "articles.body_sections_zh": "TEXT",
    # 信源软删除（保留行以免抹掉历史文章的来源归属）
    "sources.deleted": "INTEGER NOT NULL DEFAULT 0",
}

# 索引迁移表：跟 _COLUMN_MIGRATIONS 同一个理由，但补的是索引。
# ``Base.metadata.create_all()`` 只为**新建**的表建索引，老库升级上来时
# 模型里新加的 index=True 一个都不会出现。实测：ix_articles_duplicate_of
# 在所有升级上来的库里都缺失，导致 duplicate_of 查询退化成全表扫描
# （3 万篇时 12ms），而新建的库有。索引名与 SQLAlchemy 生成的一致，
# 所以 CREATE INDEX IF NOT EXISTS 对两者都幂等。
_INDEX_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    (
        "articles", "ix_articles_duplicate_of",
        "CREATE INDEX IF NOT EXISTS ix_articles_duplicate_of ON articles (duplicate_of)",
    ),
    # articles.source_id 是外键但没建索引，SQLite 不会自动建。
    # 信源页要按 source_id 统计篇数 + 取最新一篇，这个复合索引把它从
    # 全表扫描（12ms @3万篇）降到索引查找（0.05ms）。
    (
        "articles", "ix_articles_source_id",
        "CREATE INDEX IF NOT EXISTS ix_articles_source_id "
        "ON articles (source_id, published_at DESC)",
    ),
)


def _migrate_columns(engine: Engine) -> list[str]:
    """给已存在的表补齐新列，返回实际补上的列名。"""
    from sqlalchemy import inspect
    from sqlalchemy import text as sql_text

    inspector = inspect(engine)
    added: list[str] = []
    with engine.begin() as conn:
        existing_tables = set(inspector.get_table_names())
        for qualified, ddl in _COLUMN_MIGRATIONS.items():
            table, column = qualified.split(".", 1)
            if table not in existing_tables:
                continue
            if column in {row["name"] for row in inspector.get_columns(table)}:
                continue
            conn.execute(sql_text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            added.append(qualified)
    return added


def _migrate_indexes(engine: Engine) -> list[str]:
    """给已存在的表补齐缺失的索引，返回实际新建的索引名。"""
    from sqlalchemy import inspect
    from sqlalchemy import text as sql_text

    inspector = inspect(engine)
    created: list[str] = []
    with engine.begin() as conn:
        tables = set(inspector.get_table_names())
        # 表名与索引列都显式写在元组里：从 DDL 字符串里 split 出来的最后一个词
        # 对复合索引会得到 "(source_id," 这种东西，于是索引静默地一个都没建。
        for table, name, ddl in _INDEX_MIGRATIONS:
            if table not in tables:
                continue
            if name in {idx["name"] for idx in inspector.get_indexes(table)}:
                continue
            columns = {row["name"] for row in inspector.get_columns(table)}
            if not _index_columns_ok(ddl, columns):
                # 列还不齐（老库正在补列的过程中）就跳过；下次启动列齐了自然会建。
                log.debug("跳过索引 %s：%s 上缺少它需要的列", name, table)
                continue
            conn.execute(sql_text(ddl))
            created.append(name)
    return created


def _index_columns_ok(ddl: str, columns: set[str]) -> bool:
    """索引 DDL 用到的列是否都已存在。

    老库是分批补列的：``init_db`` 补完列之后表结构就齐了，但迁移本身也可能
    在中途失败或被中断。建索引前确认一下列在，少了就跳过而不是让整个启动
    抛 ``no such column``。
    """
    open_paren = ddl.rfind("(")
    close_paren = ddl.rfind(")")
    if open_paren == -1 or close_paren <= open_paren:
        return True
    inner = ddl[open_paren + 1: close_paren]
    for part in inner.split(","):
        token = part.strip().split()
        if not token:
            continue
        # 去掉 ASC/DESC 这类修饰与 COLLATE ...
        column = token[0]
        if column.upper() in ("ASC", "DESC", "COLLATE", "TEXT", "NOCASE"):
            continue
        if column not in columns:
            return False
    return True


def _analyze(engine: Engine) -> None:
    """收集统计信息，让查询规划器知道各索引的真实选择性。

    不跑 ANALYZE 就没有 ``sqlite_stat1``，规划器只能靠默认值猜。结果是
    首页那条查询选了**低选择性的** ``ix_articles_relevance`` 当驱动索引，
    再把命中的每一行丢进临时 B 树排序。同一句 SQL、同样的列，实测 3 万篇时
    176.56ms；跑了 ANALYZE 之后改走 ``ix_articles_published_at``，56.91ms；
    再补上投影列与 LIMIT 就是 0.07ms（2500 倍）。

    这是一次性的写操作（读全部索引统计、写入 sqlite_stat1），表大时值得。
    """
    from sqlalchemy import text as sql_text

    try:
        with engine.begin() as conn:
            conn.execute(sql_text("ANALYZE"))
    except Exception as exc:  # pragma: no cover - 统计信息缺失不影响正确性
        log.debug("ANALYZE 失败（不影响功能）：%r", exc)


def init_db(db_file: Path | str) -> Engine:
    """建库建表 + 补齐新列，返回 Engine（同时设置为进程默认连接）。"""
    global _engine, _session_factory, _db_file
    path = Path(db_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path}",
        future=True,
        connect_args={"check_same_thread": False, "timeout": SQLITE_BUSY_TIMEOUT_SECONDS},
    )
    _apply_sqlite_pragmas(engine)
    Base.metadata.create_all(engine)
    added = _migrate_columns(engine)
    if added:
        log.info("数据库已补齐新列：%s", "、".join(added))
    created = _migrate_indexes(engine)
    if created:
        log.info("数据库已补齐索引：%s", "、".join(created))
    _analyze(engine)
    _engine = engine
    _session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    _db_file = path
    return engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("数据库尚未初始化，请先调用 init_db()")
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    if _session_factory is None:
        raise RuntimeError("数据库尚未初始化，请先调用 init_db()")
    return _session_factory


def get_session() -> Iterator[Session]:
    """FastAPI 依赖：每请求一个 Session。"""
    with get_session_factory()() as session:
        yield session


@contextmanager
def session_scope() -> Iterator[Session]:
    """脚本 / 调度任务用的上下文管理器。"""
    with get_session_factory()() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def seed_sources(sources: Iterable[SourceConfig]) -> int:
    """把配置里的信源同步进 ``sources`` 表，返回**新增**条数。

    只按 url 做增量新增，不改已有记录 —— 这样升级项目后新增的信源会自动生效，
    而用户手动关掉的信源（``enabled=0``）不会被配置文件覆盖回去。
    """
    added = 0
    with session_scope() as session:
        known = set(session.execute(select(Source.url)).scalars())
        for item in sources:
            if item.url in known:
                continue
            session.add(
                Source(
                    name=item.name,
                    url=item.url,
                    type=item.type,
                    lang=item.lang,
                    enabled=1 if item.enabled else 0,
                    created_at=now_local(),
                )
            )
            known.add(item.url)
            added += 1
    return added
