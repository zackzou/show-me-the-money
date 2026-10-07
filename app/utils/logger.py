"""日志配置：控制台 + 可选文件（重复调用只生效一次）。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_CONFIGURED = False

# 单个日志文件 8MB、留 5 份备份（最多占 40MB）。此前是普通 FileHandler：
# 只增不减，实测一个活跃实例的 smtm.log 半天就有 5MB，几个月不重启能到几百 MB，
# 而它所在的数据目录是要被备份/迁移的。
LOG_MAX_BYTES = 8 * 1024 * 1024
LOG_BACKUP_COUNT = 5


def setup_logging(level: int = logging.INFO, log_file: Path | None = None) -> None:
    global _LOG_CONFIGURED
    if _LOG_CONFIGURED:
        return
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_file,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
        )
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    _LOG_CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
