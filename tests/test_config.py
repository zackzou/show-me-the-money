"""配置模块测试：两项必填校验、默认值合并、路径解析。"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import ConfigError, Settings, SourceConfig, load_settings
from app.db import seed_sources, session_scope
from app.models import Source

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

VALID = {
    "LLM_API_BASE": "https://api.example.com/v1",
    "LLM_API_KEY": "sk-test",
    "LLM_MODEL": "deepseek-chat",
    "RESEARCH_TOPIC": "AI Agent, 芯片",
}


def test_load_settings_happy_path():
    settings = load_settings(env=VALID, config_dir=CONFIG_DIR)
    assert settings.llm.model == "deepseek-chat"
    assert settings.research_topics == ["AI Agent", "芯片"]
    assert len(settings.sources) == 9
    assert settings.topics[:3] == ["AI Agent / 智能体", "大模型", "开源生态"]
    assert [c.name for c in settings.categories] == [
        "一手", "模型", "产品", "行业", "论文", "教程", "观点",
    ]
    assert settings.schedule.daily_report_time == "08:00"
    assert settings.db_file.name == "smtm.db"


def test_db_path_override_is_resolved_against_project_root():
    settings = load_settings(env={**VALID, "DB_PATH": "data/custom.db"}, config_dir=CONFIG_DIR)
    assert settings.db_file == settings.project_root / "data" / "custom.db"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("LLM_API_BASE", ""),
        ("LLM_API_BASE", "not-a-url"),
        ("LLM_API_KEY", ""),
        ("LLM_MODEL", ""),
        ("RESEARCH_TOPIC", ""),
        ("RESEARCH_TOPIC", "方向" * 120),
    ],
)
def test_invalid_config_raises_with_clear_message(key: str, value: str):
    with pytest.raises(ConfigError) as excinfo:
        load_settings(env={**VALID, key: value}, config_dir=CONFIG_DIR)
    message = str(excinfo.value)
    assert "配置校验失败" in message
    assert key in message


def test_missing_config_dir_raises(tmp_path: Path):
    with pytest.raises(ConfigError) as excinfo:
        load_settings(env=VALID, config_dir=tmp_path / "nope")
    assert "缺少默认配置文件" in str(excinfo.value)


def test_sources_sync_adds_new_without_touching_existing(seeded_db, settings: Settings):
    """升级项目后新增的信源要自动生效；用户手动关掉的不能被配置改回来。"""
    with session_scope() as session:
        session.execute(Source.__table__.update().values(enabled=0))
        session.commit()

    added = seed_sources(
        [
            SourceConfig(name="测试源", url="https://example.com/feed", lang="zh"),  # 已存在
            SourceConfig(name="新信源", url="https://example.com/new"),  # 新增
        ]
    )

    assert added == 1
    with session_scope() as session:
        by_url = {row.url: row.enabled for row in session.execute(select(Source)).scalars()}
    assert by_url["https://example.com/new"] == 1
    assert by_url["https://example.com/feed"] == 0  # 仍保持用户关掉的状态
