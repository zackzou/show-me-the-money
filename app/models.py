"""数据模型（SQLAlchemy 2.0）。时间统一为北京时间 naive（见 app/utils/text.py）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.utils.text import now_local


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    type: Mapped[str] = mapped_column(String(20), nullable=False, default="rss")
    lang: Mapped[str] = mapped_column(String(10), nullable=False, default="en")
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=now_local)

    articles: Mapped[list[Article]] = relationship(back_populates="source")


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    link: Mapped[str] = mapped_column(String(1000), nullable=False, unique=True, index=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 速览：两三句导语式概述，让读者在页内就能读完，不用跳原站
    digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 正文全文：从文章页抓回来的纯文本，详情页直接展示（页内读完）
    content_full: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 推荐理由：为什么值得看这条
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 相关度评分 0~100（由相关度判断顺带产出，不额外消耗调用）
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 分类：来自 config/default_topics.yaml 的 categories（顶部 tab 与卡片显示用）
    category: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    # 主题（JSON 数组）：详情页右栏的「主题」区
    topics: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 英文版标题与速览：页面可在 中文 / English / 双语 之间切换
    title_en: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 中文版标题：英文信源译过来，中文模式（以及早报片段）才不至于顶着英文标题
    title_zh: Mapped[str | None] = mapped_column(String(500), nullable=True)
    digest_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 中文版正文：英文原文整篇译过来，中文模式下不至于整页英文
    content_zh: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 配图地址（JSON 数组），来自 RSS 正文；页内直接展示，不再跳原站看图
    image_urls: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[str | None] = mapped_column(String(500), nullable=True)
    relevance: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=now_local)

    source: Mapped[Source | None] = relationship(back_populates="articles")


class DailyReport(Base):
    __tablename__ = "daily_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[str] = mapped_column(String(10), nullable=False, unique=True, index=True)
    content_md: Mapped[str] = mapped_column(Text, nullable=False)
    content_html: Mapped[str] = mapped_column(Text, nullable=False)
    article_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=now_local)
