"""Centralised configuration for the Broken Link Automation framework.

No module in this project hardcodes a URL, timeout or output path: every
component receives a :class:`Config` instance built here.

Resolution order (later wins):

1. the defaults below
2. ``BLA_*`` environment variables
3. command line flags of ``run.py``
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Tuple

# ---------------------------------------------------------------------------
# Project paths (src/config/config.py -> parents[2] == project root)
# ---------------------------------------------------------------------------
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
SRC_DIR: Path = PROJECT_ROOT / "src"
TESTS_DIR: Path = PROJECT_ROOT / "tests"
REPORTS_DIR: Path = PROJECT_ROOT / "reports"
SCREENSHOTS_DIR: Path = PROJECT_ROOT / "screenshots"
LOGS_DIR: Path = PROJECT_ROOT / "logs"

OUTPUT_DIRECTORIES: Tuple[Path, ...] = (REPORTS_DIR, SCREENSHOTS_DIR, LOGS_DIR)

# ---------------------------------------------------------------------------
# Defaults - change these to retarget the framework
# ---------------------------------------------------------------------------
#: Website that is checked when nothing else is configured.
DEFAULT_TARGET_URL = "https://example.com"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 BrokenLinkAutomation/1.0"
)

#: Supported Selenium browsers.
VALID_BROWSERS: Tuple[str, ...] = ("chrome", "firefox", "edge")

#: Protocols that can never be validated with an HTTP request -> SKIPPED.
NON_HTTP_PREFIXES: Tuple[str, ...] = (
    "mailto:",
    "tel:",
    "sms:",
    "callto:",
    "javascript:",
    "data:",
    "blob:",
    "about:",
    "file:",
    "ftp:",
    "whatsapp:",
    "viber:",
    "skype:",
)

#: Regexes matched against the normalised URL; a match means the link is
#: skipped on purpose (logout/delete endpoints, cdn internals, ...).
DEFAULT_IGNORE_PATTERNS: Tuple[str, ...] = (
    r"^#",
    r"/cdn-cgi/",
    r"[?&](logout|signout|delete|cancel)=",
)

CSV_REPORT_NAME = "broken_links.csv"
HTML_REPORT_NAME = "report.html"
LOG_FILE_NAME = "execution.log"


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------
def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value.strip())
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_tuple(name: str, default: Tuple[str, ...]) -> Tuple[str, ...]:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return tuple(item.strip() for item in value.split(",") if item.strip())


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    """Immutable run configuration shared by every component.

    Build one with :meth:`from_env` and apply CLI flags on top using
    :meth:`with_overrides`.
    """

    # target website -----------------------------------------------------
    target_url: str = DEFAULT_TARGET_URL
    check_external_links: bool = True
    max_links: int = 500          # 0 = no limit (safety valve against huge pages)

    # Selenium -----------------------------------------------------------
    browser: str = "chrome"
    headless: bool = True
    window_size: str = "1920,1080"
    page_load_timeout: int = 30
    element_timeout: int = 15

    # HTTP checking ------------------------------------------------------
    request_timeout: float = 15.0
    connect_timeout: float = 7.5
    max_retries: int = 2
    retry_backoff: float = 0.5
    max_workers: int = 8
    verify_ssl: bool = True
    user_agent: str = DEFAULT_USER_AGENT

    # evidence + output --------------------------------------------------
    screenshot_on_failure: bool = True
    ignore_patterns: Tuple[str, ...] = DEFAULT_IGNORE_PATTERNS
    reports_dir: Path = REPORTS_DIR
    screenshots_dir: Path = SCREENSHOTS_DIR
    logs_dir: Path = LOGS_DIR
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        """Build a config from the ``BLA_*`` environment variables."""

        return cls(
            target_url=_env_str("BLA_TARGET_URL", cls.target_url),
            check_external_links=_env_bool("BLA_CHECK_EXTERNAL_LINKS", cls.check_external_links),
            max_links=_env_int("BLA_MAX_LINKS", cls.max_links),
            browser=_env_str("BLA_BROWSER", cls.browser).lower(),
            headless=_env_bool("BLA_HEADLESS", cls.headless),
            window_size=_env_str("BLA_WINDOW_SIZE", cls.window_size),
            page_load_timeout=_env_int("BLA_PAGE_LOAD_TIMEOUT", cls.page_load_timeout),
            element_timeout=_env_int("BLA_ELEMENT_TIMEOUT", cls.element_timeout),
            request_timeout=_env_float("BLA_REQUEST_TIMEOUT", cls.request_timeout),
            connect_timeout=_env_float("BLA_CONNECT_TIMEOUT", cls.connect_timeout),
            max_retries=_env_int("BLA_MAX_RETRIES", cls.max_retries),
            retry_backoff=_env_float("BLA_RETRY_BACKOFF", cls.retry_backoff),
            max_workers=_env_int("BLA_MAX_WORKERS", cls.max_workers),
            verify_ssl=_env_bool("BLA_VERIFY_SSL", cls.verify_ssl),
            user_agent=_env_str("BLA_USER_AGENT", cls.user_agent),
            screenshot_on_failure=_env_bool("BLA_SCREENSHOT_ON_FAILURE", cls.screenshot_on_failure),
            ignore_patterns=_env_tuple("BLA_IGNORE_PATTERNS", cls.ignore_patterns),
            log_level=_env_str("BLA_LOG_LEVEL", cls.log_level).upper(),
        )

    def with_overrides(self, **overrides: object) -> "Config":
        """Return a copy with ``overrides`` applied; ``None`` values are ignored."""

        clean = {
            key: value
            for key, value in overrides.items()
            if value is not None and hasattr(self, key)
        }
        return replace(self, **clean)

    # -- derived values --------------------------------------------------
    @property
    def csv_report_path(self) -> Path:
        return Path(self.reports_dir) / CSV_REPORT_NAME

    @property
    def html_report_path(self) -> Path:
        return Path(self.reports_dir) / HTML_REPORT_NAME

    @property
    def log_file_path(self) -> Path:
        return Path(self.logs_dir) / LOG_FILE_NAME

    def prepare_directories(self) -> Tuple[Path, ...]:
        """Create the report, screenshot and log folders if they are missing."""

        directories = (Path(self.reports_dir), Path(self.screenshots_dir), Path(self.logs_dir))
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
        return directories

    def describe(self) -> Iterable[str]:
        """Readable key=value pairs, handy for the execution log."""

        return (
            f"target_url={self.target_url}",
            f"browser={self.browser}",
            f"headless={self.headless}",
            f"check_external_links={self.check_external_links}",
            f"max_links={self.max_links}",
            f"request_timeout={self.request_timeout}",
            f"max_retries={self.max_retries}",
            f"max_workers={self.max_workers}",
            f"verify_ssl={self.verify_ssl}",
            f"screenshot_on_failure={self.screenshot_on_failure}",
            f"reports_dir={self.reports_dir}",
        )


def ensure_directories() -> Tuple[Path, ...]:
    """Create ``reports/``, ``screenshots/`` and ``logs/`` for default configs."""

    for directory in OUTPUT_DIRECTORIES:
        directory.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIRECTORIES
