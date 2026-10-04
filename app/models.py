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
    # 中文版速览（AI 导读）：英文信源的导读默认也是英文的，中文模式必须有中文版
    digest_zh: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 中文版正文：英文原文整篇译过来，中文模式下不至于整页英文
    content_zh: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 配图地址（JSON 数组），来自 RSS 正文；页内直接展示，不再跳原站看图
    image_urls: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 正文内联配图（JSON 数组 [{"i": 接在第几段之后, "url": 地址}]）：
    # 按原站的做法插在段落之间，而不是另开一个配图区块
    body_images: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 中英双语的翻译重试次数：限流失败的候选要能轮换出去，不然永远轮不到它们
    i18n_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tags: Mapped[str | None] = mapped_column(String(500), nullable=True)
    relevance: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # 同一则新闻被多个源转载时的「主条目」id；非空表示这条是重复内容，
    # 列表页 / 日报 / 搜索都不再展示它（详情页仍可直达）
    duplicate_of: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # 完整处理（含中英双版本）已经尝试过几次。上游 LLM 限流会让整篇降级，
    # 没有这个计数就永远重试；有了它就能「重试到成功为止，但别无限重试」
    process_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 最后一次尝试处理的时间，用于退避：别每 2 小时就去撞同一堵墙
    process_last_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 降级原因（LLM 不可用之类）。页面上要能说清「为什么这篇是英文」，
    # 排查时也要一眼看出是数据问题还是上游问题
    degraded_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
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
