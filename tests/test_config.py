"""配置模块测试：两项必填校验、默认值合并、路径解析。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import ConfigError, load_settings

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
    assert settings.topics[:3] == ["AI", "大模型", "开源"]
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
