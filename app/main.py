"""FastAPI 入口。

启动顺序：加载 .env 并校验两项必填 → 加载 config/*.yaml 默认值 → 初始化 SQLite 建表
→ sources 为空时导入默认信源 → 启动 APScheduler → （可选）立即抓取一次 → 提供 Web 服务。

用工厂形式暴露 app，便于测试与 uvicorn --factory：
    uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.config import ConfigError, Settings, load_settings
from app.db import init_db, seed_sources
from app.scheduler import (
    run_backfill_reports,
    run_content_job,
    run_fetch_job,
    run_process_job,
    shutdown_scheduler,
    start_scheduler,
)
from app.utils.logger import get_logger, setup_logging
from app.web.api import api_router
from app.web.routes import page_router
from app.web.rss import rss_router

log = get_logger(__name__)


def _bootstrap(settings: Settings) -> None:
    """进程级初始化：日志、数据库、默认信源、调度器。"""
    setup_logging(log_file=settings.project_root / "data" / "smtm.log")
    init_db(settings.db_file)
    added = seed_sources(settings.sources)
    if added:
        log.info("按配置新增了 %d 个信源", added)
    run_backfill_reports(settings)
    start_scheduler(settings, log_file=settings.project_root / "data" / "smtm.log")
    if settings.fetch_on_startup:
        threading.Thread(target=_safe_startup_run, args=(settings,), daemon=True).start()
        log.info("已触发一次启动抓取（后台线程）")


def _safe_startup_run(settings: Settings) -> None:
    """启动时先抓取、再处理、最后刷新今天的日报；任何一步失败都不拖垮服务。"""
    try:
        run_fetch_job(settings)
    except Exception as exc:
        log.warning("启动抓取失败：%s", exc)
        return
    try:
        # 先把正文抓回来：速览与推荐理由的质量都依赖正文
        run_content_job(settings)
    except Exception as exc:
        log.warning("启动抓正文失败：%s", exc)
    try:
        # run_process_job 内部已经会刷新「今天」这份日报，不再重复调用
        run_process_job(settings)
    except Exception as exc:
        log.warning("启动处理失败：%s", exc)


def create_app(settings: Settings | None = None, *, bootstrap: bool = True) -> FastAPI:
    """构建 FastAPI 应用。``bootstrap=False`` 时不建库/不起调度（测试用）。"""
    try:
        resolved = settings or load_settings()
    except ConfigError as exc:
        # uvicorn --factory 走不到 main()，配置错了应该看到人话而不是裸 traceback
        print(f"\n❌ {exc}\n", flush=True)
        raise SystemExit(2) from exc

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if bootstrap:
            _bootstrap(resolved)
        yield
        if bootstrap:
            shutdown_scheduler()

    app = FastAPI(title="Show Me the Money", version=__version__, lifespan=lifespan)
    app.state.settings = resolved
    app.include_router(page_router)
    app.include_router(api_router)
    app.include_router(rss_router)
    return app


def main() -> int:  # pragma: no cover - 手动/容器启动入口
    """命令行启动：python -m app.main"""
    import uvicorn

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"\n❌ {exc}\n")
        return 2
    uvicorn.run(create_app(settings), host=settings.web.host, port=settings.web.port, log_level="info")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
