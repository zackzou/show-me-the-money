"""调度：抓取 / 处理 / 日报 / 清理（APScheduler BackgroundScheduler）。"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import delete

from app.ai.client import LLMClient
from app.ai.processor import process_pending
from app.config import Settings
from app.db import session_scope
from app.fetcher.pipeline import run_fetch_pipeline
from app.models import Article, DailyReport
from app.report.generator import generate_daily_report
from app.utils.logger import get_logger, setup_logging
from app.utils.text import now_local

log = get_logger(__name__)

_scheduler: BackgroundScheduler | None = None


def run_fetch_job(settings: Settings) -> dict[str, Any]:
    """抓取所有启用的信源并入库。"""
    stats = run_fetch_pipeline(
        timeout=settings.fetcher.timeout_seconds,
        retries=settings.fetcher.max_retries,
        user_agent=settings.fetcher.user_agent,
        dedup_window=settings.storage.dedup_recent_window,
    )
    log.info("抓取完成：%s", stats)
    return stats


def run_process_job(settings: Settings) -> dict[str, Any]:
    """处理 status=pending 的文章（相关度 → 摘要 → 标签）。"""
    client = LLMClient(
        settings.llm.api_base,
        settings.llm.api_key,
        settings.llm.model,
        timeout=settings.llm.timeout_seconds,
        retries=settings.llm.max_retries,
        temperature=settings.llm.temperature,
    )
    try:
        with session_scope() as session:
            stats = process_pending(session, client, settings)
    finally:
        client.close()
    log.info("处理完成：%s", stats)
    return stats


def run_report_job(settings: Settings) -> dict[str, Any]:
    """生成当天日报。"""
    return generate_daily_report(settings=settings)


def run_cleanup_job(settings: Settings) -> dict[str, int]:
    """删除超过保留期的文章与日报。"""
    cutoff_date = (now_local() - timedelta(days=settings.storage.retention_days)).strftime("%Y-%m-%d")
    with session_scope() as session:
        articles_result = session.execute(delete(Article).where(Article.created_at < cutoff_date))
        reports_result = session.execute(delete(DailyReport).where(DailyReport.date < cutoff_date))
        removed_articles = int(getattr(articles_result, "rowcount", 0) or 0)
        removed_reports = int(getattr(reports_result, "rowcount", 0) or 0)
    log.info("清理完成：文章 %d 条、日报 %d 份（早于 %s）", removed_articles, removed_reports, cutoff_date)
    return {"articles": removed_articles, "reports": removed_reports}


def _daily_trigger(hhmm: str) -> CronTrigger:
    hour, _, minute = hhmm.partition(":")
    return CronTrigger(hour=int(hour or 8), minute=int(minute or 0))


def build_scheduler(settings: Settings) -> BackgroundScheduler:
    """按配置装配四个任务（不启动）。"""
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    interval = IntervalTrigger(hours=settings.schedule.fetch_interval_hours)
    scheduler.add_job(run_fetch_job, interval, args=[settings], id="fetch_job", replace_existing=True)
    scheduler.add_job(
        run_process_job,
        IntervalTrigger(hours=settings.schedule.fetch_interval_hours),
        args=[settings],
        id="process_job",
        replace_existing=True,
    )
    scheduler.add_job(
        run_report_job,
        _daily_trigger(settings.schedule.daily_report_time),
        args=[settings],
        id="report_job",
        replace_existing=True,
    )
    scheduler.add_job(
        run_cleanup_job,
        _daily_trigger(settings.schedule.cleanup_time),
        args=[settings],
        id="cleanup_job",
        replace_existing=True,
    )
    return scheduler


def start_scheduler(settings: Settings, *, log_file: object | None = None) -> BackgroundScheduler:
    global _scheduler
    setup_logging(log_file=log_file)  # type: ignore[arg-type]
    if _scheduler is not None and _scheduler.running:
        return _scheduler
    _scheduler = build_scheduler(settings)
    _scheduler.start()
    log.info("调度器已启动：抓取/处理每 %s 小时，日报 %s，清理 %s",
             settings.schedule.fetch_interval_hours,
             settings.schedule.daily_report_time,
             settings.schedule.cleanup_time)
    return _scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("调度器已停止")
    _scheduler = None
