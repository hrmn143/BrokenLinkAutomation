"""Logging configuration for the framework.

Every run writes to ``logs/execution.log`` (rotating, so the file never grows
forever) *and* to the console.  Framework code calls :func:`get_logger` instead
of using ``print``.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, Optional, Union

from src.config.config import LOG_FILE_NAME, LOGS_DIR

LOGGER_NAME = "broken_link_automation"
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
MAX_BYTES = 1_000_000  # ~1 MB per file
BACKUP_COUNT = 5

#: loggers that were already wired up in this process -> logger name to log path
_configured: Dict[str, Optional[Path]] = {}


def resolve_level(level: Union[int, str]) -> int:
    """Accept ``"debug"``/``"DEBUG"``/``logging.DEBUG`` and return the level int."""

    if isinstance(level, int):
        return level
    return getattr(logging, str(level).upper(), logging.INFO)


def get_logger(
    name: str = LOGGER_NAME,
    level: Union[int, str] = "INFO",
    log_dir: Union[str, Path] = LOGS_DIR,
    log_file: str = LOG_FILE_NAME,
) -> logging.Logger:
    """Return a logger that writes to the console and to ``logs/execution.log``.

    Calling this twice with the same ``name`` returns the same logger, so
    handlers are never duplicated (important inside pytest, which imports
    modules repeatedly).
    """

    logger = logging.getLogger(name)
    log_level = resolve_level(level)

    if name in _configured:
        set_level(logger, log_level)
        return logger

    logger.setLevel(log_level)
    logger.propagate = False
    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    log_path: Optional[Path] = None
    if log_file:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        log_path = directory / log_file
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    _configured[name] = log_path
    return logger


def set_level(logger: logging.Logger, level: Union[int, str]) -> None:
    """Apply ``level`` to the logger and to all of its handlers."""

    log_level = resolve_level(level)
    logger.setLevel(log_level)
    for handler in logger.handlers:
        handler.setLevel(log_level)


def log_file_path(name: str = LOGGER_NAME) -> Optional[Path]:
    """Path of the log file that was configured for ``name`` (``None`` if any)."""

    return _configured.get(name)
