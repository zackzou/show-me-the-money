"""Web / API 测试：JSON 接口、页面路由、RSS 输出。"""

from __future__ import annotations

from app.config import Settings
from app.db import session_scope
from app.report.generator import generate_daily_report
from app.utils.text import now_local

from .conftest import make_article

TODAY = now_local().strftime("%Y-%m-%d")


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["research_topics"] == ["AI Agent", "芯片"]
    assert payload["database"] == "ok"
    assert set(payload) >= {"version", "sources", "articles", "reports"}


def test_articles_endpoint(client, seeded_db):
    with session_scope() as session:
        make_article(session, title="接口测试文章", link="https://example.com/api-1")

    response = client.get(f"/api/articles?date={TODAY}")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["title"] == "接口测试文章"
    assert payload[0]["status"] == "processed"

    assert client.get("/api/articles?date=1999-01-01").json() == []


def test_reports_endpoints(client, settings: Settings, seeded_db):
    assert client.get("/api/reports").json() == []
    assert client.get(f"/api/reports/{TODAY}").status_code == 404

    with session_scope() as session:
        make_article(session, title="报告用文章", link="https://example.com/api-2")
    with session_scope() as session:
        generate_daily_report(TODAY, session=session, settings=settings)

    listing = client.get("/api/reports").json()
    assert len(listing) == 1
    assert listing[0]["date"] == TODAY

    detail = client.get(f"/api/reports/{TODAY}")
    assert detail.status_code == 200
    assert "报告用文章" in detail.json()["content_md"]


def test_html_pages(client, settings: Settings, seeded_db):
    home = client.get("/")
    assert home.status_code == 200
    assert "Show Me the Money" in home.text
    assert "还没有日报" in home.text

    assert client.get("/daily/1999-01-01").status_code == 404
    assert client.get("/archive").status_code == 200

    with session_scope() as session:
        make_article(session, title="页面用文章", link="https://example.com/api-3")
    with session_scope() as session:
        generate_daily_report(TODAY, session=session, settings=settings)

    assert "页面用文章" in client.get("/").text
    assert "页面用文章" in client.get(f"/daily/{TODAY}").text
    assert TODAY in client.get("/archive").text


def test_rss_output(client, settings: Settings, seeded_db):
    assert client.get("/rss").status_code == 404

    with session_scope() as session:
        make_article(session, title="RSS 用文章", link="https://example.com/api-4")
    with session_scope() as session:
        generate_daily_report(TODAY, session=session, settings=settings)

    response = client.get("/rss")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/rss+xml")
    assert response.text.startswith("<?xml")
    # 每篇文章一个条目，而不是整份日报塞进一个 item
    assert "RSS 用文章（标签A、标签B）" in response.text
    assert response.text.count("<item>") == 1


def test_unknown_report_date_returns_404(client):
    assert client.get("/api/reports/1999-01-01").status_code == 404


def test_api_and_pages_include_degraded_articles(client, seeded_db):
    """页面、JSON 接口与日报口径一致：降级文章都该出现。"""
    with session_scope() as session:
        make_article(session, title="降级接口文章", link="https://example.com/api-5", status="failed")

    payload = client.get(f"/api/articles?date={TODAY}").json()
    assert [row["title"] for row in payload] == ["降级接口文章"]

    with session_scope() as session:
        generate_daily_report(TODAY, session=session, settings=client.app.state.settings)
    assert "降级接口文章" in client.get(f"/daily/{TODAY}").text


def test_articles_endpoint_defaults_to_beijing_today(client, seeded_db):
    """服务器本地时区不是北京时间时（如容器 UTC），也不能把「今天」算错。"""
    with session_scope() as session:
        make_article(session, title="默认日期文章", link="https://example.com/api-6")

    response = client.get("/api/articles")
    assert response.status_code == 200
    assert [row["title"] for row in response.json()] == ["默认日期文章"]


def test_invalid_date_does_not_return_500(client, seeded_db):
    """外部能直接触发的参数：非法日期必须 4xx，不能 500。"""
    assert client.get("/api/articles?date=2026-13-45").status_code == 400
    assert client.get("/daily/2026-13-45").status_code == 404
    assert client.get("/rss?date=2026-13-45").status_code == 404
