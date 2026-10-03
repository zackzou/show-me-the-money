"""配置加载与校验。

用户只填两项：LLM_API_* 与 RESEARCH_TOPIC（见 .env.example）。
其余一切来自 config/*.yaml 的默认值，且不在代码里硬编码密钥 / URL / 模型名。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config"


class ConfigError(RuntimeError):
    """配置缺失或非法时抛出；消息面向用户，直接可读。"""


class UserSettings(BaseSettings):
    """用户侧配置（.env 或环境变量）。只有 llm_* 与 research_topic 必填。"""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_api_base: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    research_topic: str = ""
    fetch_on_startup: bool = True
    config_dir: str = "config"
    db_path: str = ""


class LLMSettings(BaseModel):
    api_base: str
    api_key: str
    model: str
    timeout_seconds: float = 60.0
    max_retries: int = 2
    temperature: float = 0.3


class SourceConfig(BaseModel):
    name: str
    url: str
    type: str = "rss"
    lang: str = "en"
    enabled: bool = True


class PromptsConfig(BaseModel):
    summary_prompt: str
    digest_prompt: str = ""
    tag_prompt: str
    relevance_prompt: str
    fallback_summary_chars: int = 200
    fallback_digest_chars: int = 180


class StorageSettings(BaseModel):
    retention_days: int = 30
    db_path: str = "data/smtm.db"
    dedup_recent_window: int = 500


class ScheduleSettings(BaseModel):
    fetch_interval_hours: float = 2.0
    daily_report_time: str = "08:00"
    cleanup_time: str = "03:00"
    # 想按「每天固定时刻」跑（而不是按小时轮询）就填 cron 表达式，例如 "0 7 * * *"；
    # 留空则退回 fetch_interval_hours 的间隔触发。
    fetch_cron: str = ""
    process_cron: str = ""


class WebSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000


class FetcherSettings(BaseModel):
    timeout_seconds: float = 15.0
    max_retries: int = 2
    user_agent: str = "ShowMeTheMoney/0.1"
    # 只收最近 N 天发布的内容。用来挡「全量归档型」feed：有些博客的 RSS 会一次
    # 返回整个历史（实测 Hugging Face Blog 单次 872 条且没有正文），不清掉的话
    # 首启会灌进几百条陈年文章，每条都要花 3 次 LLM 调用。
    max_age_days: int = 14
    # 单个信源单次最多入库多少条，作为最后一道成本闸门。
    max_items_per_source: int = 60
    # 正文短于该长度视为「只有标题」，直接丢弃；0 = 不限制。
    min_content_chars: int = 0


class AISettings(BaseModel):
    """单次处理任务的规模控制（每篇文章要花 2~3 次 LLM 调用）。"""

    # 每个调度周期最多处理多少篇 pending，剩下的留给下个周期，避免单次任务跑太久。
    batch_size: int = 60
    # 每处理多少篇提交一次，防止长任务中途失败把进度一起回滚。
    batch_checkpoint_every: int = 10


class Settings(BaseModel):
    """展开后的运行时配置（用户项 + 默认值 + 校验结果）。"""

    llm: LLMSettings
    research_topic: str
    research_topics: list[str]
    topics: list[str]
    sources: list[SourceConfig]
    prompts: PromptsConfig
    storage: StorageSettings
    schedule: ScheduleSettings
    web: WebSettings
    fetcher: FetcherSettings
    ai: AISettings = AISettings()
    fetch_on_startup: bool = True
    config_dir: Path = Field(default=DEFAULT_CONFIG_DIR)
    project_root: Path = Field(default=PROJECT_ROOT)

    @property
    def db_file(self) -> Path:
        """数据库绝对路径（相对路径按项目根解析）。"""
        raw = Path(self.storage.db_path)
        return raw if raw.is_absolute() else (self.project_root / raw)


def _read_yaml(path: Path, *, required: bool = True) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ConfigError(f"缺少默认配置文件：{path}")
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - 只在配置写坏时触发
        raise ConfigError(f"配置文件不是合法 YAML：{path}（{exc}）") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是映射：{path}")
    return data


def _validate_two_required(user: UserSettings) -> None:
    problems: list[str] = []
    base = (user.llm_api_base or "").strip()
    key = (user.llm_api_key or "").strip()
    model = (user.llm_model or "").strip()
    topic = (user.research_topic or "").strip()

    if not base:
        problems.append("LLM_API_BASE 未设置（示例：https://api.deepseek.com/v1）")
    else:
        parsed = urlparse(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            problems.append(f"LLM_API_BASE 不是合法 URL：{base!r}")
    if not key:
        problems.append("LLM_API_KEY 未设置（本地 Ollama 填 ollama）")
    if not model:
        problems.append("LLM_MODEL 未设置（示例：deepseek-chat / qwen2.5:3b）")
    if not topic:
        problems.append("RESEARCH_TOPIC 未设置（示例：AI Agent, 大模型商业化）")
    elif len(topic) > 200:
        problems.append(f"RESEARCH_TOPIC 过长（{len(topic)} > 200 字符）")
    elif not [part for part in topic.split(",") if part.strip()]:
        problems.append("RESEARCH_TOPIC 至少要有一个方向")

    if problems:
        detail = "\n".join(f"  - {item}" for item in problems)
        raise ConfigError("配置校验失败，进程不会启动：\n" + detail)


def load_settings(
    *,
    env: dict[str, str] | None = None,
    config_dir: Path | str | None = None,
) -> Settings:
    """加载并校验配置。``env`` / ``config_dir`` 便于测试注入。"""
    user = (
        UserSettings.model_validate({k.lower(): v for k, v in env.items()}) if env is not None else UserSettings()
    )
    _validate_two_required(user)

    cfg_dir = Path(config_dir) if config_dir else Path(user.config_dir or DEFAULT_CONFIG_DIR)
    if not cfg_dir.is_absolute():
        cfg_dir = PROJECT_ROOT / cfg_dir

    raw_settings = _read_yaml(cfg_dir / "settings.yaml")
    sources_raw = _read_yaml(cfg_dir / "default_sources.yaml").get("sources") or []
    topics_raw = _read_yaml(cfg_dir / "default_topics.yaml").get("topics") or []
    prompts_raw = _read_yaml(cfg_dir / "default_prompts.yaml")

    try:
        storage = StorageSettings(**(raw_settings.get("storage") or {}))
        schedule = ScheduleSettings(**(raw_settings.get("schedule") or {}))
        web = WebSettings(**(raw_settings.get("web") or {}))
        fetcher = FetcherSettings(**(raw_settings.get("fetcher") or {}))
        ai = AISettings(**(raw_settings.get("ai") or {}))
        prompts = PromptsConfig(**prompts_raw)
        sources = [SourceConfig(**item) for item in sources_raw]
    except (ValidationError, TypeError) as exc:
        raise ConfigError(f"config/*.yaml 内容不合法：{exc}") from exc

    for label, value in (("daily_report_time", schedule.daily_report_time), ("cleanup_time", schedule.cleanup_time)):
        hour, _, minute = value.partition(":")
        if not (hour.isdigit() and minute.isdigit() and 0 <= int(hour) < 24 and 0 <= int(minute) < 60):
            raise ConfigError(f"config/settings.yaml 里 {label} 不是合法的 HH:MM：{value!r}")

    if user.db_path.strip():
        storage.db_path = user.db_path.strip()

    llm = LLMSettings(
        api_base=user.llm_api_base.strip(),
        api_key=user.llm_api_key.strip(),
        model=user.llm_model.strip(),
        timeout_seconds=float((raw_settings.get("llm") or {}).get("timeout_seconds", 60)),
        max_retries=int((raw_settings.get("llm") or {}).get("max_retries", 2)),
        temperature=float((raw_settings.get("llm") or {}).get("temperature", 0.3)),
    )

    topic = user.research_topic.strip()
    return Settings(
        llm=llm,
        research_topic=topic,
        research_topics=[part.strip() for part in topic.split(",") if part.strip()],
        topics=[str(t) for t in topics_raw],
        sources=sources,
        prompts=prompts,
        storage=storage,
        schedule=schedule,
        web=web,
        fetcher=fetcher,
        ai=ai,
        fetch_on_startup=bool(user.fetch_on_startup) and os.environ.get("SMTM_DISABLE_STARTUP_FETCH") != "1",
        config_dir=cfg_dir,
        project_root=PROJECT_ROOT,
    )
