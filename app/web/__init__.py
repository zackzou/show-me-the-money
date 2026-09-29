"""Web 子包。"""

from app.web.api import api_router
from app.web.routes import page_router
from app.web.rss import rss_router

__all__ = ["api_router", "page_router", "rss_router"]
