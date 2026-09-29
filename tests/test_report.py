"""日报模块测试：生成、覆盖、空数据、清理。"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.config import Settings
from app.db import session_scope
from app.models import DailyReport
from app.report.generator import generate_daily_report, render_markdown
from app.scheduler import build_scheduler, run_cleanup_job, run_report_job
from app.utils.text import now_local

from .conftest import make_article

TODAY = now_local().strftime("%Y-%m-%d")


def _seed(session, **kwargs):
    return make_article(session, **kwargs)


def test_generate_daily_report_filters_and_renders(seeded_db, settings: Settings):
    with session_scope() as session:
        _seed(session, title="相关的第一条", link="https://example.com/1", summary="摘要一", tags="AI,芯片")
        _seed(session, title="相关的第二条", link="https://example.com/2", summary="摘要二", tags="开源")
        _seed(session, title="不相关的", link="https://example.com/3", relevance=0)
        _seed(session, title="昨天的", link="https://example.com/4", published_at=now_local() - timedelta(days=1))
        _seed(session, title="还没处理的", link="https://example.com/5", status="pending")

    with session_scope() as session:
        result = generate_daily_report(TODAY, session=session, settings=settings)

    assert result["article_count"] == 2
    assert "相关的第一条" in result["content_md"]
    assert "调研方向：AI Agent, 芯片" in result["content_md"]
    assert "不相关的" not in result["content_md"]
    assert "<html" in result["content_html"]

    with session_scope() as session:
        rows = list(session.execute(select(DailyReport)).scalars())
        assert len(rows) == 1
        assert rows[0].date == TODAY


def test_generate_daily_report_overwrites_same_date(seeded_db, settings: Settings):
    with session_scope() as session:
        _seed(session, title="第一版", link="https://example.com/10")
    with session_scope() as session:
        generate_daily_report(TODAY, session=session, settings=settings)

    with session_scope() as session:
        _seed(session, title="第二版", link="https://example.com/11")
    with session_scope() as session:
        second = generate_daily_report(TODAY, session=session, settings=settings)

    assert second["article_count"] == 2
    with session_scope() as session:
        rows = list(session.execute(select(DailyReport)).scalars())
        assert len(rows) == 1  # 同一天只留一份
        assert "第二版" in rows[0].content_md


def test_generate_daily_report_empty_day(seeded_db, settings: Settings):
    with session_scope() as session:
        result = generate_daily_report("2000-01-01", session=session, settings=settings)
    assert result["article_count"] == 0
    assert "没有与调研方向相关" in result["content_md"]


def test_render_markdown_shape():
    markdown = render_markdown(
        "2026-09-29",
        "AI Agent",
        [{"title": "T", "source": "S", "summary": "SU", "tags": "TG", "link": "L", "published_at": ""}],
    )
    assert markdown.startswith("# Show Me the Money 日报 · 2026-09-29")
    assert "> 调研方向：AI Agent" in markdown
    assert "## 1. T" in markdown
    assert "- 链接：L" in markdown


def test_run_report_job_uses_global_session(seeded_db, settings: Settings):
    with session_scope() as session:
        _seed(session, title="定时任务生成的文章", link="https://example.com/20")
    result = run_report_job(settings)
    assert result["article_count"] == 1


def test_cleanup_removes_expired_rows(seeded_db, settings: Settings):
    old = now_local() - timedelta(days=settings.storage.retention_days + 5)
    with session_scope() as session:
        _seed(session, title="过期的", link="https://example.com/30", published_at=old, created_at=old)
        _seed(session, title="新鲜的", link="https://example.com/31")
        session.add(
            DailyReport(date="2000-01-01", content_md="旧", content_html="<p>旧</p>", article_count=1, created_at=old)
        )

    stats = run_cleanup_job(settings)
    assert stats["articles"] == 1
    assert stats["reports"] == 1

    with session_scope() as session:
        assert session.query(DailyReport).count() == 0


def test_scheduler_defaults_to_interval_triggers(settings: Settings):
    jobs = {job.id: type(job.trigger).__name__ for job in build_scheduler(settings).get_jobs()}
    assert jobs == {
        "fetch_job": "IntervalTrigger",
        "process_job": "IntervalTrigger",
        "report_job": "CronTrigger",
        "cleanup_job": "CronTrigger",
    }


def test_scheduler_switches_to_daily_cron_when_configured(settings: Settings):
    settings.schedule.fetch_cron = "0 7 * * *"
    settings.schedule.process_cron = "30 7 * * *"
    scheduler = build_scheduler(settings)
    jobs = {job.id: type(job.trigger).__name__ for job in scheduler.get_jobs()}
    assert jobs["fetch_job"] == "CronTrigger"
    assert jobs["process_job"] == "CronTrigger"
    # 抓取每天 07:00、处理 07:30，日报仍在 08:00
    assert "hour='7', minute='0'" in str(scheduler.get_job("fetch_job").trigger)
    assert "hour='7', minute='30'" in str(scheduler.get_job("process_job").trigger)
    assert "hour='8', minute='0'" in str(scheduler.get_job("report_job").trigger)
