"""FastAPI 入口。

启动顺序：加载 .env 并校验两项必填 → 加载 config/*.yaml 默认值 → 初始化 SQLite 建表
→ sources 为空时导入默认信源 → 启动 APScheduler → （可选）立即抓取一次 → 提供 Web 服务。

用工厂形式暴露 app，便于测试与 uvicorn --factory：
    uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.config import ConfigError, Settings, load_settings
from app.db import init_db, seed_sources
from app.scheduler import (
    run_backfill_reports,
    run_content_job,
    run_fetch_job,
    run_process_job,
    shutdown_scheduler,
    start_scheduler,
)
from app.utils.logger import get_logger, setup_logging
from app.web.api import api_router
from app.web.routes import page_router
from app.web.rss import rss_router
from app.web.settings import apply_stored, load_stored, settings_router
from app.web.sources import sources_router

log = get_logger(__name__)

# 会被改动的路径（POST/PUT/PATCH/DELETE）。这些必须做同源检查。
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# 请求头里带这些的可以放行：非浏览器客户端（curl、脚本、TestClient）不会
# 带 Origin，而它们本来就不是 CSRF 的受害者 —— CSRF 的前提是「浏览器自动
# 带上用户的 cookie/凭据」。设置页的「取明文 key」也走 POST，用的是自定义
# 头 + same-origin，同样放行。
_SAME_ORIGIN_HEADERS = frozenset({"x-requested-with", "x-smtt-csrf"})


async def _same_origin_guard(request: Request, call_next):
    """改状态的请求必须同源。

    这个服务**没有任何鉴权**，而且 README 建议监听 0.0.0.0。于是任何一个
    恶意网页都能用表单 POST 驱动这 8 个接口：改掉模型设置（把 api_base 指向
    攻击者的端点，后续每一次调用的提示词与回复都会发过去）、删光信源。

    ``application/x-www-form-urlencoded`` 是「CORS 简单请求」，浏览器**不会**
    预检，所以只靠 CORS 挡不住 —— 必须服务端自己检查。
    判断依据用 Origin，其次 Referer；两者都没有就放行，因为非浏览器客户端
    （curl、脚本、测试）本来就不构成 CSRF —— CSRF 的前提是浏览器自动带上
    用户的凭据。
    """
    if request.method not in _MUTATING_METHODS:
        return await call_next(request)
    if any(name in request.headers for name in _SAME_ORIGIN_HEADERS):
        return await call_next(request)
    host = request.headers.get("host", "")
    present = False
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if not value:
            continue
        present = True
        # Origin: null 出现在 file:// 页面发起的请求里，一律拒绝
        if value == "null":
            break
        parsed = urlsplit(value)
        if parsed.netloc and parsed.netloc == host:
            return await call_next(request)
        break
    if not present:
        # 既没有 Origin 也没有 Referer = 非浏览器客户端（curl、脚本、测试），
        # 本来就不构成 CSRF，放行。
        return await call_next(request)
    log.warning("拒绝跨站请求：%s %s（Origin=%s）", request.method, request.url.path,
                request.headers.get("origin", ""))
    return JSONResponse(
        {"detail": "跨站请求已被拒绝：改状态的接口只接受同源提交"},
        status_code=403,
    )


def _bootstrap(settings: Settings) -> None:
    setup_logging(log_file=settings.project_root / "data" / "smtm.log")
    init_db(settings.db_file)
    added = seed_sources(settings.sources)
    if added:
        log.info("按配置新增了 %d 个信源", added)
    run_backfill_reports(settings)
    start_scheduler(settings, log_file=settings.project_root / "data" / "smtm.log")
    if settings.fetch_on_startup:
        threading.Thread(target=_safe_startup_run, args=(settings,), daemon=True).start()
        log.info("已触发一次启动抓取（后台线程）")


def _safe_startup_run(settings: Settings) -> None:
    """启动时先抓取、再处理、最后刷新今天的日报；任何一步失败都不拖垮服务。"""
    try:
        run_fetch_job(settings)
    except Exception as exc:
        log.warning("启动抓取失败：%s", exc)
        return
    try:
        # 先把正文抓回来：速览与推荐理由的质量都依赖正文
        run_content_job(settings)
    except Exception as exc:
        log.warning("启动抓正文失败：%s", exc)
    try:
        # run_process_job 内部已经会刷新「今天」这份日报，不再重复调用
        run_process_job(settings)
    except Exception as exc:
        log.warning("启动处理失败：%s", exc)


def create_app(settings: Settings | None = None, *, bootstrap: bool = True) -> FastAPI:
    """构建 FastAPI 应用。``bootstrap=False`` 时不建库/不起调度（测试用）。"""
    try:
        resolved = settings or load_settings()
    except ConfigError as exc:
        # uvicorn --factory 走不到 main()，配置错了应该看到人话而不是裸 traceback
        print(f"\n❌ {exc}\n", flush=True)
        raise SystemExit(2) from exc

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if bootstrap:
            _bootstrap(resolved)
        yield
        if bootstrap:
            shutdown_scheduler()

    app = FastAPI(title="Show Me the Money", version=__version__, lifespan=lifespan)
    # 全站没有任何压缩。实测页面体积：/search?scope=full 144KB、
    # /story/29 91KB、/settings 67KB、/sources 56KB，而 base.html 一张皮就
    # 占 41KB 的其中 38KB。HTML 是高度可压缩的文本，这一层几乎是白捡的。
    app.add_middleware(GZipMiddleware, minimum_size=800)
    app.middleware("http")(_same_origin_guard)
    app.state.settings = resolved
    # 页面保存过的模型配置优先于环境变量（启动时就套上，别等用户点保存）
    apply_stored(app.state, load_stored(resolved))
    app.include_router(page_router)
    app.include_router(api_router)
    app.include_router(rss_router)
    app.include_router(sources_router)
    app.include_router(settings_router)
    return app


def main() -> int:  # pragma: no cover - 手动/容器启动入口
    """命令行启动：python -m app.main"""
    import uvicorn

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"\n❌ {exc}\n")
        return 2
    uvicorn.run(create_app(settings), host=settings.web.host, port=settings.web.port, log_level="info")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
