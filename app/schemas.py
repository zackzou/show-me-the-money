"""对外输出的 Pydantic schema。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ArticleOut(BaseModel):
    id: int
    source_id: int | None = None
    title: str
    link: str
    published_at: datetime | None = None
    summary: str | None = None
    digest: str | None = None
    reason: str | None = None
    score: int | None = None
    tags: str | None = None
    relevance: int | None = None
    status: str

    model_config = {"from_attributes": True}


class ArticleDetailOut(ArticleOut):
    """单篇页内预览用的详情：速览 + 正文纯文本 + 配图。"""

    content: str | None = None
    image_urls: list[str] = []
    source_name: str = "未知来源"

    model_config = {"from_attributes": True}


class ReportOut(BaseModel):
    date: str
    article_count: int
    created_at: datetime

    model_config = {"from_attributes": True}


class ReportDetail(ReportOut):
    content_md: str
    content_html: str


class HealthOut(BaseModel):
    status: str
    version: str
    database: str
    sources: int
    articles: int
    with_images: int = 0
    with_full_text: int = 0
    reports: int
    research_topics: list[str]


class FetchStats(BaseModel):
    fetched: int
    new: int
    duplicated: int
    failed: int
    details: list[dict[str, Any]] = []


class ProcessStats(BaseModel):
    pending: int
    processed: int
    irrelevant: int
    failed: int
