"""HTML 页面路由（Jinja2 + Tailwind CDN）。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import DailyReport

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

page_router = APIRouter()


@page_router.get("/", response_class=HTMLResponse)
def index(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    latest = session.execute(select(DailyReport).order_by(DailyReport.date.desc()).limit(1)).scalar_one_or_none()
    return templates.TemplateResponse(
        request,
        "index.html",
        {"report": latest, "title": "最新日报"},
    )


@page_router.get("/daily/{date}", response_class=HTMLResponse)
def daily(request: Request, date: str, session: Session = Depends(get_session)) -> HTMLResponse:
    report = session.execute(select(DailyReport).where(DailyReport.date == date)).scalar_one_or_none()
    if report is None:
        return templates.TemplateResponse(
            request,
            "daily.html",
            {"report": None, "date": date, "title": f"{date} 日报"},
            status_code=404,
        )
    return templates.TemplateResponse(request, "daily.html", {"report": report, "date": date, "title": f"{date} 日报"})


@page_router.get("/archive", response_class=HTMLResponse)
def archive(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    reports = list(
        session.execute(select(DailyReport).order_by(DailyReport.date.desc()).limit(100)).scalars()
    )
    return templates.TemplateResponse(request, "archive.html", {"reports": reports, "title": "历史日报"})
