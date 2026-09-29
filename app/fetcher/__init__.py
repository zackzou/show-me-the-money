"""抓取子包。"""

from app.fetcher.pipeline import run_fetch_pipeline
from app.fetcher.rss import fetch_feed

__all__ = ["fetch_feed", "run_fetch_pipeline"]
