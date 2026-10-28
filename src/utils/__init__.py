"""Reusable utilities: logging, URL/link checking and report generation."""

from src.utils.link_checker import (
    BLOCKED_STATUS_CODES,
    FAILURE_STATUSES,
    HEAD_FALLBACK_STATUS_CODES,
    RETRYABLE_STATUS_CODES,
    LinkChecker,
    LinkInventory,
    LinkResult,
    LinkStatistics,
    LinkStatus,
    UrlProcessor,
    classify_exception,
    classify_status_code,
    is_retryable,
)
from src.utils.logger import get_logger, log_file_path, resolve_level, set_level
from src.utils.report_generator import ReportGenerator

__all__ = [
    "LinkChecker",
    "LinkInventory",
    "LinkResult",
    "LinkStatistics",
    "LinkStatus",
    "UrlProcessor",
    "classify_exception",
    "classify_status_code",
    "is_retryable",
    "ReportGenerator",
    "get_logger",
    "set_level",
    "resolve_level",
    "log_file_path",
    "FAILURE_STATUSES",
    "RETRYABLE_STATUS_CODES",
    "BLOCKED_STATUS_CODES",
    "HEAD_FALLBACK_STATUS_CODES",
]
