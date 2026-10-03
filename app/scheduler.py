"""调度：抓取 / 处理 / 日报 / 清理（APScheduler BackgroundScheduler）。"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import delete, func, select

from app.ai.client import LLMClient
from app.ai.processor import process_pending
from app.config import Settings
from app.db import session_scope
from app.fetcher.pipeline import run_fetch_pipeline
from app.models import Article, DailyReport
from app.report.generator import STATUS_REPORTABLE, day_window, generate_daily_report
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
        max_age_days=settings.fetcher.max_age_days,
        max_items_per_source=settings.fetcher.max_items_per_source,
        min_content_chars=settings.fetcher.min_content_chars,
    )
    log.info("抓取完成：%s", {k: v for k, v in stats.items() if k != "details"})
    return stats


def run_process_job(settings: Settings) -> dict[str, Any]:
    """处理 status=pending 的文章（相关度 → 摘要 → 标签），随后刷新今天的实时日报。"""
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
            stats = process_pending(session, client, settings, limit=settings.ai.batch_size)
    finally:
        client.close()
    log.info("处理完成：%s", stats)
    # 处理完顺手刷新「今天」这份日报，首页就能实时看到当天热点；失败不影响处理结果。
    try:
        run_today_report_job(settings)
    except Exception as exc:  # 日报失败不该把处理统计一起丢掉
        log.warning("今日日报刷新失败：%s", exc)
    return stats


def run_report_job(settings: Settings) -> dict[str, Any]:
    """生成**昨天**的完整日报。

    08:00 跑的时候昨天已经不会再变化，所以出的是一份定稿；今天那份由
    ``run_today_report_job`` 滚动刷新。
    """
    date_str = (now_local() - timedelta(days=1)).strftime("%Y-%m-%d")
    _warn_if_unprocessed(date_str)
    result = generate_daily_report(date_str, settings=settings)
    log.info("日报定稿：%s（%d 篇）", date_str, result["article_count"])
    return result


def _warn_if_unprocessed(date_str: str) -> None:
    """定稿前提醒：这一天还有没处理完的文章吗？

    处理有每轮条数上限（``ai.batch_size``），冷启动或信源暴增时可能上一轮没跑完；
    这些文章既进不了当天的定稿（时间已过），也不会出现在第二天的日报里 —— 只能靠提醒。
    """
    start, end = day_window(date_str)
    with session_scope() as session:
        pending = session.execute(
            select(func.count(Article.id)).where(
                Article.status == "pending",
                Article.published_at >= start,
                Article.published_at < end,
            )
        ).scalar()
    if pending:
        log.warning(
            "%s 还有 %d 篇文章没处理完，这份日报不包含它们；可调大 config/settings.yaml 的 ai.batch_size",
            date_str,
            pending,
        )


def run_today_report_job(settings: Settings) -> dict[str, Any]:
    """滚动刷新**今天**的日报（只统计到当前时刻）。"""
    now = now_local()
    return generate_daily_report(now.strftime("%Y-%m-%d"), until=now, settings=settings)


def run_backfill_reports(settings: Settings, *, days: int = 7) -> list[str]:
    """补齐最近几天里缺失、且**当天确有内容**的日报，返回补上的日期列表。

    服务停机几天再起来时，只有 08:00 那一次定时任务，中间的日期会永远缺一份日报
    （文章还在库里，只是没人去生成）。启动时补一次就齐了。

    刻意跳过「当天没有任何进日报内容」的日期：刚装好的空库不该在历史列表里
    摆出一串 0 篇的日报。
    """
    today = now_local()
    with session_scope() as session:
        since = (today - timedelta(days=days)).strftime("%Y-%m-%d")
        existing = set(
            session.execute(select(DailyReport.date).where(DailyReport.date >= since)).scalars()
        )
    missing = [
        (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(1, days + 1)
        if (today - timedelta(days=offset)).strftime("%Y-%m-%d") not in existing
    ]
    filled: list[str] = []
    for date_str in missing:
        if not _has_reportable_articles(date_str):
            continue
        try:
            generate_daily_report(date_str, settings=settings)
        except Exception as exc:  # 补不齐不该影响启动
            log.warning("补齐日报失败：%s（%s）", date_str, exc)
            continue
        filled.append(date_str)
    if filled:
        log.info("已补齐 %d 天的日报：%s", len(filled), "、".join(filled))
    return filled


def _has_reportable_articles(date_str: str) -> bool:
    """这一天有没有「本该进日报」的文章。"""
    start, end = day_window(date_str)
    with session_scope() as session:
        return bool(
            session.execute(
                select(func.count(Article.id)).where(
                    Article.relevance == 1,
                    Article.status.in_(STATUS_REPORTABLE),
                    Article.published_at >= start,
                    Article.published_at < end,
                )
            ).scalar()
        )


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


def _fetch_trigger(settings: Settings) -> CronTrigger | IntervalTrigger:
    """配了 fetch_cron 就按每天固定时刻跑，否则按小时轮询。"""
    if settings.schedule.fetch_cron.strip():
        return CronTrigger.from_crontab(settings.schedule.fetch_cron.strip(), timezone="Asia/Shanghai")
    return IntervalTrigger(seconds=int(settings.schedule.fetch_interval_hours * 3600))


def _process_trigger(settings: Settings) -> CronTrigger | IntervalTrigger:
    if settings.schedule.process_cron.strip():
        return CronTrigger.from_crontab(settings.schedule.process_cron.strip(), timezone="Asia/Shanghai")
    return IntervalTrigger(seconds=int(settings.schedule.fetch_interval_hours * 3600))


def build_scheduler(settings: Settings) -> BackgroundScheduler:
    """按配置装配四个任务（不启动）。"""
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(run_fetch_job, _fetch_trigger(settings), args=[settings], id="fetch_job", replace_existing=True)
    scheduler.add_job(
        run_process_job, _process_trigger(settings), args=[settings], id="process_job", replace_existing=True
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
    fetch_plan = settings.schedule.fetch_cron.strip() or f"每 {settings.schedule.fetch_interval_hours} 小时"
    log.info(
        "调度器已启动：抓取/处理 %s，处理后刷新今日日报；昨天日报定稿 %s；清理 %s",
        fetch_plan,
        settings.schedule.daily_report_time,
        settings.schedule.cleanup_time,
    )
    return _scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("调度器已停止")
    _scheduler = None
