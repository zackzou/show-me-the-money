"""调度：抓取 / 处理 / 日报 / 清理（APScheduler BackgroundScheduler）。"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import delete, func, select

from app.ai.client import LLMClient
from app.ai.cluster import merge_duplicates
from app.ai.processor import backfill_translations, process_pending, retry_degraded
from app.config import Settings
from app.db import session_scope
from app.fetcher.content import backfill_body_images, backfill_content, strip_shared_openings
from app.fetcher.images import backfill_images
from app.fetcher.pipeline import run_fetch_pipeline
from app.models import Article, DailyReport
from app.report.generator import STATUS_REPORTABLE, day_window, generate_daily_report
from app.utils.logger import get_logger, setup_logging
from app.utils.text import now_local

log = get_logger(__name__)

_scheduler: BackgroundScheduler | None = None


def _llm_client(settings: Settings) -> LLMClient:
    """按当前配置建一个 LLM 客户端。

    统一在这里建，是为了让 ``extra_headers`` 这类网关参数只写一处 ——
    调度里一共有三个地方要发 LLM 请求，漏掉任何一个，那一路就会静默地
    用不到网关要求的请求头。
    """
    return LLMClient(
        settings.llm.api_base,
        settings.llm.api_key,
        settings.llm.model,
        timeout=settings.llm.timeout_seconds,
        retries=settings.llm.max_retries,
        temperature=settings.llm.temperature,
        extra_headers=settings.llm.extra_headers,
    )


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
    client = _llm_client(settings)
    # 先把「因 LLM 不可用而降级」的文章放回 pending。上游限流会让整篇停在英文，
    # 而 process_pending 只捞 pending —— 不放回来就永远等不到中文版。
    try:
        with session_scope() as session:
            requeued = retry_degraded(session, max_attempts=settings.ai.retry_max_attempts)
        log.info("降级文章重排队：%s", requeued)
    except Exception as exc:
        log.warning("重排降级文章失败：%s", exc)
    try:
        with session_scope() as session:
            stats = process_pending(session, client, settings, limit=settings.ai.batch_size)
    finally:
        client.close()
    log.info("处理完成：%s", stats)
    # 处理完顺手补图（只给相关文章补，每轮限量）与刷新今日日报；任何一步失败都不影响处理结果。
    try:
        run_media_job(settings)
    except Exception as exc:
        log.warning("补齐配图失败：%s", exc)
    # 合并跨源重复：正文已经抓回来了，这时才有足够信号判断「是不是同一件事」
    try:
        run_merge_job(settings)
    except Exception as exc:
        log.warning("合并重复内容失败：%s", exc)
    # 补译正文：正文翻译是可选步骤，限流或抖动失败的文章到这里再试一次
    try:
        if settings.i18n.enabled and settings.i18n.translate_content:
            translator = _llm_client(settings)
            try:
                with session_scope() as session:
                    log.info("补译中文正文：%s", backfill_translations(session, translator, settings))
            finally:
                translator.close()
    except Exception as exc:
        log.warning("补译中文正文失败：%s", exc)
    try:
        run_today_report_job(settings)
    except Exception as exc:  # 日报失败不该把处理统计一起丢掉
        log.warning("今日日报刷新失败：%s", exc)
    return stats


def run_content_job(settings: Settings) -> dict[str, Any]:
    """抓取正文全文 —— 必须跑在处理之前，速览和推荐理由的质量都靠它。"""
    if not settings.content.enabled:
        return {"candidates": 0, "filled": 0, "short": 0, "failed": 0}
    with session_scope() as session:
        return backfill_content(
            session,
            limit=settings.content.batch_size,
            timeout=settings.content.timeout_seconds,
            min_chars=settings.content.min_chars,
        )


def run_media_job(settings: Settings) -> dict[str, Any]:
    """给「相关但没图」的文章补首图（RSS 没带图时抓 og:image）。"""
    if not settings.media.enabled:
        return {"candidates": 0, "filled": 0, "not_found": 0, "failed": 0}
    with session_scope() as session:
        stats = backfill_images(
            session,
            limit=settings.media.batch_size,
            timeout=settings.media.timeout_seconds,
        )
        # 站点通栏广告（InfoQ 每篇都顶着同一段大会宣传）不算正文，删掉
        with session_scope() as session:
            strip_shared_openings(session)
        # 正文内联配图：老数据都没有位置，顺手补齐，详情页才能像原站那样把图插进正文
        inline = backfill_body_images(
            session,
            limit=settings.media.batch_size,
            timeout=settings.media.timeout_seconds,
        )
    return {"cover": stats, "inline": inline}


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
            Article.duplicate_of.is_(None),
                    Article.published_at >= start,
                    Article.published_at < end,
                )
            ).scalar()
        )


def run_merge_job(settings: Settings) -> dict[str, Any]:
    """把「多家源报道同一件事」的文章合并成一条。

    入库时的去重只比 link 与标题，跨源转载的标题差别很大（Apple 磁盘访问那条
    被TechCrunch / The Verge / Ars Technica 各写了一遍），只能等处理完、正文
    抓回来之后再判一次。
    """
    if not settings.merge.enabled:
        return {"candidates": 0, "pairs": 0, "merged": 0, "kept": 0}
    client = _llm_client(settings)
    try:
        with session_scope() as session:
            stats = merge_duplicates(session, client, settings, limit=settings.merge.window)
    except Exception as exc:
        log.warning("合并重复内容失败：%s", exc)
        return {"candidates": 0, "pairs": 0, "merged": 0, "kept": 0}
    finally:
        client.close()
    if stats["merged"]:
        log.info("合并重复内容 %d 条（候选 %d 篇、判定 %d 对）", stats["merged"], stats["candidates"], stats["pairs"])
    return stats


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
        run_content_job, _fetch_trigger(settings), args=[settings], id="content_job", replace_existing=True
    )
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
