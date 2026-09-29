"""数据库连接与初始化。"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import SourceConfig
from app.models import Base, Source
from app.utils.text import now_local

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
_db_file: Path | None = None


def _apply_sqlite_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_connection: object, _record: object) -> None:  # pragma: no cover - 驱动回调
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def init_db(db_file: Path | str) -> Engine:
    """建库建表，返回 Engine（同时设置为进程默认连接）。"""
    global _engine, _session_factory, _db_file
    path = Path(db_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}", future=True, connect_args={"check_same_thread": False})
    _apply_sqlite_pragmas(engine)
    Base.metadata.create_all(engine)
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
    """若 sources 表为空，导入默认信源池，返回导入条数。"""
    with session_scope() as session:
        existing = session.query(Source).count()
        if existing:
            return 0
        created = 0
        for item in sources:
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
            created += 1
        return created
