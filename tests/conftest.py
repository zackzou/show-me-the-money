"""共享 fixture：临时数据库 + 注入式配置 + TestClient。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, load_settings
from app.db import init_db, session_scope
from app.models import Source
from app.utils.text import now_local

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"

ENV = {
    "LLM_API_BASE": "http://llm.local/v1",
    "LLM_API_KEY": "test-key",
    "LLM_MODEL": "test-model",
    "RESEARCH_TOPIC": "AI Agent, 芯片",
}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """测试用配置：db 指到临时目录，把派生的文件也一起隔离。

    不隔离的话 ``load_stored()`` / ``_settings_path()`` 会读到真实的
    ``data/llm_settings.json`` —— 开发机上有这个文件时，设置页测试会
    读写真实配置（实测把 api_base 写成了测试里的 attacker.example），
    测试之间也会互相污染。
    """
    resolved = load_settings(env=ENV, config_dir=CONFIG_DIR)
    resolved.storage.db_path = str(tmp_path / "settings.db")
    return resolved


@pytest.fixture
def db(tmp_path: Path) -> str:
    """每个测试一个独立的 SQLite 文件，并把全局 session factory 指向它。"""
    db_file = tmp_path / "test.db"
    init_db(db_file)
    return str(db_file)


@pytest.fixture
def seeded_db(db: str) -> str:
    with session_scope() as session:
        session.add(Source(name="测试源", url="https://example.com/feed", type="rss", lang="zh", enabled=1))
    return db


@pytest.fixture
def client(settings: Settings, db: str) -> Iterator[TestClient]:
    from app.main import create_app

    app = create_app(settings, bootstrap=False)
    with TestClient(app) as test_client:
        yield test_client


def make_article(session, **kwargs):
    """构造一篇文章（默认：今天、相关、已处理）。"""
    from app.models import Article

    payload = {
        "title": "标题",
        "link": f"https://example.com/{now_local().timestamp()}",
        "content": "正文内容",
        "published_at": now_local(),
        "summary": "摘要",
        "tags": "标签A,标签B",
        "relevance": 1,
        "status": "processed",
    }
    payload.update(kwargs)
    article = Article(**payload)
    session.add(article)
    session.flush()
    return article
