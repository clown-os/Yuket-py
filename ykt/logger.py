# -*- coding: utf-8 -*-
"""日志：控制台彩色输出 + 按天滚动文件。"""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger


def setup_logger(log_dir: str | Path, level: str = "INFO"):
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        colorize=True,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | {message}",
    )
    logger.add(
        log_path / "yuketang_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        encoding="utf-8",
        rotation="5 MB",
        retention=3,
        enqueue=False,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <7} | {message}",
    )
    return logger
