"""日志配置：控制台 + 可选文件（重复调用只生效一次）。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_LOG_CONFIGURED = False


def setup_logging(level: int = logging.INFO, log_file: Path | None = None) -> None:
    global _LOG_CONFIGURED
    if _LOG_CONFIGURED:
        return
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    _LOG_CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
