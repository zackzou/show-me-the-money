"""数据模型（SQLAlchemy 2.0）。时间统一为北京时间 naive（见 app/utils/text.py）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.utils.text import now_local


def visible_article_conditions() -> tuple[Any, ...]:
    """「能出现在列表/日报/搜索/RSS」的公共过滤条件。

    抽出来是防漏：展示口径散在七八个查询里，加一列（如 ``deleted_at``）
    时挨个补，漏一个就会让回收站里的文章从那个入口漏出来。
    新查询一律从这里取，加新条件也只改一处。

    刻意**不包含** ``relevance`` / 状态 / 时间窗 —— 那些各查询不同；
    这里只管「这条内容该不该被读者看到」。
    """
    return (
        Article.duplicate_of.is_(None),
        Article.deleted_at.is_(None),
    )


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
    # 用户在管理页「删除」一个源时置 1，**不真的删行**。
    # 真删会让 SQLAlchemy 把 articles.source_id 置成 NULL（关系默认行为），
    # 已经抓到的那些文章的「来源」就全变成了「未知来源」—— 用户的历史信息
    # 被悄悄抹掉了。软删除保留归属，列表与抓取都不再包含它。
    deleted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=now_local)
    # 停用原因与时间：列表里一排「已停用」时，用户最想知道的就是「什么时候、
    # 为什么停的」。手动停用与「添加时未启用」都记录在案；升级前停用的存量行
    # 没有记录（NULL），页面显示「历史停用，时间未记录」而不是装作知道。
    disabled_reason: Mapped[str | None] = mapped_column(String(300))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime)

    articles: Mapped[list[Article]] = relationship(back_populates="source")


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("sources.id"), index=True, nullable=True
    )
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
    # AI 章节结构（JSON 数组 [{"h": 小标题或"", "t": 段落}]）：NYT 总编视角的
    # 智能分段，直排原文段落太碎时才用；没有就不分，模板回退普通段落。
    body_sections: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 中文版章节结构：与原文逐节对照，段落一一对应
    body_sections_zh: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 配图地址（JSON 数组），来自 RSS 正文；页内直接展示，不再跳原站看图
    image_urls: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 远端图 → 本地 /img/ 的映射（JSON 对象）：原站防盗链经常裂图，
    # 下载到本地再展示。缺了就回退原地址，不影响老数据。
    media_map: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 早报一句话：推送到手机端读的一段 fluent 汇总（AI 写，非截断拼凑）
    brief_zh: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    # 特别关注：标题/摘要/正文命中「关注关键词」时置 1。列表页优先排序、
    # 卡片上加「关注」标记；查询时是热路径（首页/日报/搜索都过这一列），
    # 所以建索引。
    starred: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    # 软删除（回收站）：非空表示用户手动删过，列表/日报/搜索/RSS 都不再展示，
    # 详情页仍可直达（和 duplicate_of 同一套展示口径）。保留行是为了让
    # 「恢复」只是一次 UPDATE，不用在删除时把正文/译文整篇搬走。
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=now_local)

    source: Mapped[Source | None] = relationship(back_populates="articles")


class KeywordRule(Base):
    """关键词规则：屏蔽（block）或特别关注（star）。

    为什么不用配置文件：屏蔽词是**随热点演化**的（这周屏蔽 Muse，下周可能
    换成别的），写进 YAML 每次都要重启才生效；而且用户希望看到每条规则的
    「命中计数」，配置文件承载不了这种状态。

    ``kind``：
      - ``block``：抓取入库前拦截，标题/摘要命中即**不入库**（默认行为）；
        正文命中在入库后判定（正文要抓回来才有），命中即软删到回收站。
      - ``star``：入库后判定，标题/摘要/正文命中即置 ``Article.starred=1``，
        列表优先排序并加标记。
    ``hits`` 是累计命中计数（屏蔽+关注各算各的），页面用来展示「这条规则拦了
    多少 / 标了多少」，也方便判断一条规则是不是写得太宽泛（拦了一大片）。
    """

    __tablename__ = "keyword_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    keyword: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(10), nullable=False, default="block")
    hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=now_local)


class DailyReport(Base):
    __tablename__ = "daily_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[str] = mapped_column(String(10), nullable=False, unique=True, index=True)
    content_md: Mapped[str] = mapped_column(Text, nullable=False)
    content_html: Mapped[str] = mapped_column(Text, nullable=False)
    article_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=now_local)


class BriefConfig(Base):
    """早报配置（单行表，id 恒为 1）。

    早报与「日报」是两种产物：日报是整天全量快照，早报是**按用户口味精选的
    TOP N**（分类 / 主题 / 标签 / 关键词自由定制），内容直接继承每篇文章的
    「早报片段」（``Article.brief_zh``，处理阶段就写好了），拼装成稿时不再
    调用模型。配置存表而不是 JSON 文件：与关键词规则同一套「网页可改、
    即时生效」的模式，而且后续要加字段时走列迁移就行。
    """

    __tablename__ = "brief_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 精选条数（1~50，默认 10）
    top_n: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    # 取最近多少天内的文章（1~7，默认 1 = 今天）
    days: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 以下过滤条件都是 JSON 数组字符串（空 = 不限）
    categories: Mapped[str | None] = mapped_column(Text)
    topics: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[str | None] = mapped_column(Text)
    # 命中关键词（标题/摘要/导读/片段任一包含即入选）
    keywords: Mapped[str | None] = mapped_column(Text)
    # 排除词（命中任一即剔除）
    exclude: Mapped[str | None] = mapped_column(Text)
    # 1 = 只收「特别关注」命中过的文章
    starred_only: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 组内排序：score（评分优先）或 time（时间优先）；特别关注永远排最前
    sort: Mapped[str] = mapped_column(String(10), nullable=False, default="score")
    # 分节配置（JSON 数组）：每节 = 一张微信长图 / 一组新闻，各自带
    # 名称、条数与筛选（分类/主题/标签/关键词）。空 = 用全局筛选出单节。
    # 例：[{"name":"AI 大模型","top_n":5,"categories":["模型"],"topics":[],
    #      "tags":[],"keywords":[]}]
    sections: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)


class BriefIssue(Base):
    """早报成品：每天早上定时生成的一份存档（按日期唯一）。

    与 ``BriefConfig``（规则）分开存：规则随时可改，成品是「当时按那套
    规则产出的内容」快照 —— 配置页要能回看每一天的早报，改规则不能
    改写历史。内容存 JSON（分节 + 条目），text/markdown 是渲染好的成品。
    """

    __tablename__ = "brief_issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[str] = mapped_column(String(10), nullable=False, unique=True, index=True)
    content_json: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    markdown: Mapped[str] = mapped_column(Text, nullable=False)
    section_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    article_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=now_local)
