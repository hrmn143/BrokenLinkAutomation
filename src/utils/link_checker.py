"""Link discovery post-processing, HTTP validation, retries and classification.

The module has four responsibilities, each in its own small class so that it
can be unit tested without a browser and without the internet:

``LinkStatus``     - the vocabulary used everywhere (WORKING, REDIRECT, ...)
``LinkResult``     - one structured row per discovered link (link result model)
``UrlProcessor``   - normalisation, filtering, de-duplication, internal/external
``LinkChecker``    - HEAD/GET validation with retries and graceful error handling
``LinkStatistics`` - the numbers used by the console summary and the dashboard
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

import requests
import urllib3
from requests.adapters import HTTPAdapter

from src.config.config import (
    DEFAULT_IGNORE_PATTERNS,
    DEFAULT_USER_AGENT,
    NON_HTTP_PREFIXES,
    Config,
)
from src.utils.logger import get_logger


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------
class LinkStatus(StrEnum):
    """Result category of a single link.

    ``StrEnum`` means a status can be used directly inside f-strings, CSV cells
    and JSON payloads without calling ``.value``.
    """

    WORKING = "WORKING"                    # 2xx
    REDIRECT = "REDIRECT"                  # 3xx (final response after redirects)
    CLIENT_ERROR = "CLIENT_ERROR"          # 4xx -> broken
    SERVER_ERROR = "SERVER_ERROR"          # 5xx -> broken
    BLOCKED = "BLOCKED"                    # 401/403/429 - exists but refuses bots
    TIMEOUT = "TIMEOUT"                    # request exceeded the timeout
    CONNECTION_ERROR = "CONNECTION_ERROR"  # DNS failure, refused connection, ...
    SSL_ERROR = "SSL_ERROR"                # certificate / TLS problem
    INVALID = "INVALID"                    # not a usable http(s) URL
    SKIPPED = "SKIPPED"                    # intentionally not checked


#: Statuses that make a link "broken" in the report.
FAILURE_STATUSES = frozenset({
    LinkStatus.CLIENT_ERROR,
    LinkStatus.SERVER_ERROR,
    LinkStatus.TIMEOUT,
    LinkStatus.CONNECTION_ERROR,
    LinkStatus.SSL_ERROR,
    LinkStatus.INVALID,
})

#: Statuses where trying again can realistically help.
RETRYABLE_STATUSES = frozenset({
    LinkStatus.TIMEOUT,
    LinkStatus.CONNECTION_ERROR,
    LinkStatus.SERVER_ERROR,
    LinkStatus.BLOCKED,  # only 429 can reach this state, see RETRYABLE_STATUS_CODES
})

#: Status codes that mean "the server exists but does not let us in".
BLOCKED_STATUS_CODES = frozenset({401, 403, 429})

#: Transient HTTP status codes worth a retry (408 request timeout, 425/429 rate
#: limiting, 5xx server hiccups).
RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Servers that reject HEAD -> retry the same URL with GET.
HEAD_FALLBACK_STATUS_CODES = frozenset({400, 403, 405, 406, 501})

#: Never sleep longer than this between two attempts.
MAX_RETRY_DELAY_SECONDS = 5.0


def classify_status_code(status_code: Optional[int]) -> LinkStatus:
    """Map an HTTP status code to a :class:`LinkStatus`.

    2xx -> WORKING, 3xx -> REDIRECT, 401/403/429 -> BLOCKED, other 4xx ->
    CLIENT_ERROR, 5xx -> SERVER_ERROR.  ``None`` means the request never
    produced a response.
    """

    if status_code is None:
        return LinkStatus.CONNECTION_ERROR
    if 200 <= status_code <= 299:
        return LinkStatus.WORKING
    if 300 <= status_code <= 399:
        return LinkStatus.REDIRECT
    if status_code in BLOCKED_STATUS_CODES:
        return LinkStatus.BLOCKED
    if 400 <= status_code <= 499:
        return LinkStatus.CLIENT_ERROR
    if 500 <= status_code <= 599:
        return LinkStatus.SERVER_ERROR
    return LinkStatus.CONNECTION_ERROR


def classify_exception(error: BaseException) -> LinkStatus:
    """Map a ``requests`` exception to a :class:`LinkStatus`."""

    if isinstance(error, requests.exceptions.SSLError):
        return LinkStatus.SSL_ERROR
    if isinstance(error, requests.exceptions.Timeout):
        return LinkStatus.TIMEOUT
    if isinstance(error, (
        requests.exceptions.InvalidURL,
        requests.exceptions.MissingSchema,
        requests.exceptions.InvalidSchema,
    )):
        return LinkStatus.INVALID
    if isinstance(error, requests.exceptions.TooManyRedirects):
        return LinkStatus.CLIENT_ERROR
    return LinkStatus.CONNECTION_ERROR


def is_retryable(status: LinkStatus) -> bool:
    """``True`` when ``status`` justifies another attempt."""

    return status in RETRYABLE_STATUSES


# ---------------------------------------------------------------------------
# Link inventory - "what did the page contain, what will we check"
# ---------------------------------------------------------------------------
@dataclass
class LinkInventory:
    """Result of processing the raw ``href`` values of a page.

    ``plan`` keeps the order in which links appeared on the page and holds a
    ``str`` for URLs that still have to be checked and a finished
    :class:`LinkResult` for links that were skipped.
    """

    source_page: str
    total_found: int = 0
    discovered_unique: int = 0
    duplicates: int = 0
    max_links_skipped: int = 0
    urls: List[str] = field(default_factory=list)
    occurrences: Dict[str, int] = field(default_factory=dict)
    link_types: Dict[str, str] = field(default_factory=dict)
    skipped: List[LinkResult] = field(default_factory=list)
    plan: List[Union[str, LinkResult]] = field(default_factory=list)

    @property
    def unique_links_to_check(self) -> int:
        return len(self.urls)

    @property
    def internal_count(self) -> int:
        return sum(1 for link_type in self.link_types.values() if link_type == "internal")

    @property
    def external_count(self) -> int:
        return sum(1 for link_type in self.link_types.values() if link_type == "external")

    def merge(self, checked_results: Iterable[LinkResult]) -> List[LinkResult]:
        """Combine the page order with the checked results into one list."""

        by_url = {result.url: result for result in checked_results}
        merged: List[LinkResult] = []
        for item in self.plan:
            if isinstance(item, LinkResult):
                merged.append(item)
            elif item in by_url:
                merged.append(by_url[item])
        return merged


# ---------------------------------------------------------------------------
# URL processing
# ---------------------------------------------------------------------------
class UrlProcessor:
    """Turns raw ``href`` values into absolute, de-duplicated http(s) URLs.

    * empty / fragment-only links are dropped
    * ``mailto:``, ``tel:``, ``javascript:`` ... are dropped (return ``""``)
    * relative URLs are resolved against the page URL
    * duplicate path separators are collapsed, query strings are preserved
    * trailing slashes are kept on purpose: ``/about`` and ``/about/`` can
      resolve to different resources on real servers, so removing one could
      invent a broken link that never existed
    """

    def __init__(
        self,
        base_url: str,
        ignore_patterns: Iterable[str] = DEFAULT_IGNORE_PATTERNS,
        check_external_links: bool = True,
        logger=None,
    ) -> None:
        self.base_url = base_url or ""
        self.ignore_patterns = tuple(ignore_patterns or ())
        self.check_external_links = check_external_links
        self.logger = logger or get_logger()

    # -- normalisation ---------------------------------------------------
    def normalize(self, raw_url: Optional[str]) -> str:
        """Return an absolute http(s) URL without fragment, or ``""``."""

        if not raw_url:
            return ""
        candidate = raw_url.strip().replace("\n", "").replace("\t", "")
        if not candidate or candidate.startswith("#"):
            return ""
        if candidate.lower().startswith(NON_HTTP_PREFIXES):
            return ""
        if "://" in candidate and not urlsplit(candidate).netloc:
            # things like "http://" (scheme but no host) can never be requested
            return ""

        absolute, _fragment = urldefrag(urljoin(self.base_url, candidate))
        parts = urlsplit(absolute)
        if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
            return ""
        return urlunsplit((
            parts.scheme.lower(),
            parts.netloc.lower(),  # host names are case insensitive -> better de-dup
            self._normalise_path(parts.path),
            parts.query,
            "",
        ))

    @staticmethod
    def _normalise_path(path: str) -> str:
        if not path:
            return "/"
        while "//" in path:
            path = path.replace("//", "/")
        return path

    # -- classification --------------------------------------------------
    @staticmethod
    def _host(value: str) -> str:
        return (urlsplit(value).netloc or "").lower().removeprefix("www.")

    def is_internal(self, url: str) -> bool:
        """``True`` when ``url`` points at the same host as the target site."""

        base_host = self._host(self.base_url)
        return bool(base_host) and self._host(url) == base_host

    def link_type(self, url: str) -> str:
        return "internal" if self.is_internal(url) else "external"

    def skip_reason(self, url: str) -> Optional[str]:
        """Reason why ``url`` must not be requested, or ``None``."""

        for pattern in self.ignore_patterns:
            if re.search(pattern, url):
                return f"Skipped: matched ignore pattern '{pattern}'"
        if not self.check_external_links and not self.is_internal(url):
            return "Skipped: external link checking is disabled"
        return None

    # -- inventory -------------------------------------------------------
    def build_inventory(
        self,
        raw_links: Iterable[str],
        source_page: str = "",
        max_links: int = 0,
    ) -> LinkInventory:
        """Normalise, filter, de-duplicate and count the raw ``href`` values.

        Only unique URLs end up in ``inventory.urls`` (each will be requested
        once), while ``occurrences`` remembers how often every link was present
        on the page.  Filtered links become SKIPPED results so that nothing
        disappears silently from the report.
        """

        inventory = LinkInventory(source_page=source_page or self.base_url)
        skipped_index: Dict[str, LinkResult] = {}
        occurrences: Dict[str, int] = {}
        candidates: List[str] = []

        for raw in raw_links:
            inventory.total_found += 1
            url = self.normalize(raw)
            if not url:
                self._record_skipped(
                    inventory,
                    skipped_index,
                    (raw or "").strip() or "(empty href)",
                    "Skipped: empty, fragment-only or non-web link",
                    "non-web",
                    occurrences,
                )
                continue
            if url in occurrences:
                # duplicate of a URL we already know -> only count it
                occurrences[url] += 1
                known = skipped_index.get(url)
                if known is not None:
                    known.occurrences = occurrences[url]
                continue
            reason = self.skip_reason(url)
            if reason:
                self._record_skipped(
                    inventory, skipped_index, url, reason, self.link_type(url), occurrences
                )
                continue
            occurrences[url] = 1
            inventory.link_types[url] = self.link_type(url)
            candidates.append(url)
            inventory.plan.append(url)

        self._apply_link_limit(inventory, candidates, occurrences, max_links)
        inventory.urls = candidates
        inventory.occurrences = occurrences
        inventory.discovered_unique = len(candidates) + len(inventory.skipped)
        inventory.duplicates = max(inventory.total_found - inventory.discovered_unique, 0)
        self.logger.debug(
            "Inventory: %s raw link(s), %s unique, %s duplicate(s), %s skipped, %s to check",
            inventory.total_found,
            inventory.discovered_unique,
            inventory.duplicates,
            len(inventory.skipped),
            inventory.unique_links_to_check,
        )
        return inventory

    @staticmethod
    def _record_skipped(
        inventory: LinkInventory,
        index: Dict[str, LinkResult],
        url: str,
        reason: str,
        link_type: str,
        occurrences: Dict[str, int],
    ) -> LinkResult:
        """Create (or update) the SKIPPED result for ``url``."""

        count = occurrences.get(url, 0) + 1
        occurrences[url] = count
        existing = index.get(url)
        if existing is not None:
            existing.occurrences = count
            return existing

        result = LinkResult(
            source_page=inventory.source_page,
            url=url,
            link_type=link_type,
            status=LinkStatus.SKIPPED,
            occurrences=count,
            error=reason,
        )
        index[url] = result
        inventory.skipped.append(result)
        inventory.plan.append(result)
        return result

    def _apply_link_limit(
        self,
        inventory: LinkInventory,
        candidates: List[str],
        occurrences: Dict[str, int],
        max_links: int,
    ) -> None:
        """Enforce ``max_links`` and turn the overflow into SKIPPED results."""

        if not max_links or max_links <= 0 or len(candidates) <= max_links:
            return
        overflow = candidates[max_links:]
        del candidates[max_links:]
        for url in overflow:
            result = LinkResult(
                source_page=inventory.source_page,
                url=url,
                link_type=inventory.link_types.get(url, "external"),
                status=LinkStatus.SKIPPED,
                occurrences=occurrences.get(url, 1),
                error=f"Skipped: link limit reached (max_links={max_links})",
            )
            inventory.skipped.append(result)
            try:
                inventory.plan[inventory.plan.index(url)] = result
            except ValueError:  # pragma: no cover - defensive
                inventory.plan.append(result)
        inventory.max_links_skipped = len(overflow)
        self.logger.warning(
            "%s link(s) were not checked because max_links=%s", len(overflow), max_links
        )


# ---------------------------------------------------------------------------
# Link checking
# ---------------------------------------------------------------------------
class LinkChecker:
    """Validates URLs over HTTP with retries, redirects and full error handling.

    Retries are implemented explicitly (instead of relying on ``urllib3``) so
    every attempt can be logged, counted and asserted in the unit tests.  One
    failing URL can never stop the run: it is reported as a failed
    :class:`LinkResult`.
    """

    PROGRESS_EVERY = 25

    def __init__(self, config: Config, logger=None) -> None:
        self.config = config
        self.max_retries = max(int(config.max_retries), 0)
        self.retry_backoff = max(float(config.retry_backoff), 0.0)
        self.max_workers = max(int(config.max_workers), 1)
        self.logger = logger or get_logger()
        self._local = threading.local()
        self._progress_lock = threading.Lock()
        self.checked_so_far = 0
        if not config.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # -- session handling -------------------------------------------------
    @property
    def session(self) -> requests.Session:
        """A ``requests`` session per worker thread, created on first use."""

        session = getattr(self._local, "session", None)
        if session is None:
            session = self._build_session()
            self._local.session = session
        return session

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.verify = self.config.verify_ssl
        session.headers.update({
            "User-Agent": self.config.user_agent,
            "Accept": "*/*",
            "Connection": "keep-alive",
        })
        # No retries in the adapter: check_url() owns the retry loop.
        adapter = HTTPAdapter(
            pool_connections=self.max_workers,
            pool_maxsize=self.max_workers,
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def close(self) -> None:
        session = getattr(self._local, "session", None)
        if session is not None:
            session.close()
            self._local.session = None

    def __enter__(self) -> "LinkChecker":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    # -- requests ---------------------------------------------------------
    @property
    def _timeouts(self) -> Tuple[float, float]:
        return (self.config.connect_timeout, self.config.request_timeout)

    def _request(self, method: str, url: str) -> requests.Response:
        return self.session.request(
            method,
            url,
            timeout=self._timeouts,
            allow_redirects=True,
            stream=True,
            verify=self.config.verify_ssl,
        )

    def _request(self, method: str, url: str) -> requests.Response:
        return self.session.request(
            method,
            url,
            timeout=self._timeouts,
            allow_redirects=True,
            stream=True,
            verify=self.config.verify_ssl,
        )
    def _perform(self, url: str) -> Tuple[LinkStatus, Optional[int], str, str, str]:

        method = "HEAD"
        response = self._request(method, url)

        # Try GET if HEAD returns 404 or is rejected
        if response.status_code in HEAD_FALLBACK_STATUS_CODES or response.status_code == 404:
            response.close()
            method = "GET"
            response = self._request(method, url)

        try:
            final_code = response.status_code
            history = list(response.history)

            if history and final_code < 400:
                first_hop = history[0]

                return (
                    LinkStatus.REDIRECT,
                    first_hop.status_code,
                    response.url or url,
                    (first_hop.reason or "").strip(),
                    method,
                )

            return (
                classify_status_code(final_code),
                final_code,
                response.url or url,
                (response.reason or "").strip(),
                method,
            )

        finally:
            response.close()

    @staticmethod
    def _should_retry(status: LinkStatus, status_code: Optional[int]) -> bool:
        """Retry timeouts, connection problems and transient HTTP status codes."""

        if status in (LinkStatus.TIMEOUT, LinkStatus.CONNECTION_ERROR):
            return True
        return status_code is not None and status_code in RETRYABLE_STATUS_CODES

    def _sleep_before_retry(self, attempt: int) -> None:
        delay = min(self.retry_backoff * attempt, MAX_RETRY_DELAY_SECONDS)
        if delay > 0:
            time.sleep(delay)

    # -- single URL -------------------------------------------------------
    def check_url(self, url: str) -> LinkResult:
        """Check one URL, retrying transient failures up to ``max_retries`` times."""

        started = time.perf_counter()
        result = LinkResult(source_page="", url=url)
        attempts = 0

        for attempt in range(1, self.max_retries + 2):
            attempts = attempt
            try:
                status, status_code, final_url, reason, method = self._perform(url)
                error_text = ""
            except requests.exceptions.RequestException as error:
                status = classify_exception(error)
                status_code, final_url, method = None, url, ""
                reason = f"{type(error).__name__}: {error}"
                error_text = reason

            result.status = status
            result.status_code = status_code
            result.final_url = final_url or url
            result.method = method
            result.error = "" if status in (LinkStatus.WORKING, LinkStatus.REDIRECT) else (
                error_text or reason or str(status)
            )

            if attempt <= self.max_retries and self._should_retry(status, status_code):
                self.logger.warning(
                    "Attempt %s/%s failed for %s (%s) - retrying",
                    attempt,
                    self.max_retries + 1,
                    url,
                    status,
                )
                self._sleep_before_retry(attempt)
                continue
            break

        result.attempts = attempts
        result.response_time = time.perf_counter() - started
        self._note_progress(url, result)
        return result

    def _note_progress(self, url: str, result: LinkResult) -> None:
        """Log the outcome of one link and a periodic progress line."""

        with self._progress_lock:
            self.checked_so_far += 1
            done = self.checked_so_far

        if result.is_broken:
            self.logger.warning(
                "BROKEN  [%s] %s (%s)", result.status_code or "-", url, result.error
            )
        else:
            self.logger.debug(
                "%-8s [%s] %s in %.3fs",
                result.status,
                result.status_code or "-",
                url,
                result.response_time,
            )

        if done % self.PROGRESS_EVERY == 0:
            self.logger.info("Progress: %s link(s) checked so far", done)

    def _safe_check(self, url: str) -> LinkResult:
        """``check_url`` that can never raise, so a thread pool stays healthy."""

        try:
            return self.check_url(url)
        except Exception as error:  # pragma: no cover - defensive
            self.logger.error("Unexpected error while checking %s: %s", url, error)
            return LinkResult(
                source_page="",
                url=url,
                status=classify_exception(error),
                error=f"{type(error).__name__}: {error}",
            )

    # -- many URLs --------------------------------------------------------
    def check_links(
        self,
        urls: Iterable[str],
        source_page: str = "",
        occurrences: Optional[Dict[str, int]] = None,
        link_types: Optional[Dict[str, str]] = None,
        max_workers: Optional[int] = None,
    ) -> List[LinkResult]:
        """Check every URL concurrently and keep the input order."""

        url_list = [url for url in urls if url]
        if not url_list:
            self.logger.info("No links to check")
            return []

        occurrences = occurrences or {}
        link_types = link_types or {}
        workers = max(1, min(int(max_workers or self.max_workers), len(url_list)))
        self.logger.info("Checking %s unique link(s) with %s worker(s)", len(url_list), workers)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(self._safe_check, url_list))

        summary: Dict[str, int] = {}
        for result in results:
            result.source_page = source_page or result.source_page
            result.occurrences = occurrences.get(result.url, 1)
            result.link_type = link_types.get(result.url, result.link_type)
            key = str(result.status)
            summary[key] = summary.get(key, 0) + 1

        self.logger.info(
            "Link checking finished: %s",
            ", ".join(f"{name}={count}" for name, count in sorted(summary.items())),
        )
        return results


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
@dataclass
class LinkStatistics:
    """Aggregated numbers used by the console summary and the HTML dashboard."""

    target_url: str
    total_links_found: int
    unique_links_discovered: int
    results: List[LinkResult]
    started_at: datetime
    finished_at: datetime

    @classmethod
    def from_results(
        cls,
        results: Iterable[LinkResult],
        *,
        target_url: str,
        total_links_found: int,
        unique_links_discovered: int,
        started_at: datetime,
        finished_at: Optional[datetime] = None,
    ) -> "LinkStatistics":
        return cls(
            target_url=target_url,
            total_links_found=total_links_found,
            unique_links_discovered=unique_links_discovered,
            results=list(results),
            started_at=started_at,
            finished_at=finished_at or datetime.now(),
        )

    # -- helpers ----------------------------------------------------------
    def count(self, *statuses: LinkStatus) -> int:
        wanted = set(statuses)
        return sum(1 for result in self.results if result.status in wanted)

    @property
    def working(self) -> int:
        return self.count(LinkStatus.WORKING)

    @property
    def redirects(self) -> int:
        return self.count(LinkStatus.REDIRECT)

    @property
    def blocked(self) -> int:
        return self.count(LinkStatus.BLOCKED)

    @property
    def skipped(self) -> int:
        return self.count(LinkStatus.SKIPPED)

    @property
    def client_errors(self) -> int:
        return self.count(LinkStatus.CLIENT_ERROR)

    @property
    def server_errors(self) -> int:
        return self.count(LinkStatus.SERVER_ERROR)

    @property
    def timeouts(self) -> int:
        return self.count(LinkStatus.TIMEOUT)

    @property
    def connection_errors(self) -> int:
        return self.count(LinkStatus.CONNECTION_ERROR)

    @property
    def ssl_errors(self) -> int:
        return self.count(LinkStatus.SSL_ERROR)

    @property
    def invalid(self) -> int:
        return self.count(LinkStatus.INVALID)

    @property
    def broken(self) -> int:
        return sum(1 for result in self.results if result.is_broken)

    @property
    def broken_results(self) -> List[LinkResult]:
        return [result for result in self.results if result.is_broken]

    @property
    def unique_links_checked(self) -> int:
        return sum(1 for result in self.results if result.was_checked)

    @property
    def internal_links(self) -> int:
        return sum(1 for result in self.results if result.link_type == "internal")

    @property
    def external_links(self) -> int:
        return sum(1 for result in self.results if result.link_type == "external")

    @property
    def duplicate_occurrences(self) -> int:
        """How many extra occurrences the duplicated URLs had in total."""

        return sum(max(result.occurrences - 1, 0) for result in self.results)

    @property
    def duration_seconds(self) -> float:
        return max((self.finished_at - self.started_at).total_seconds(), 0.0)

    @property
    def success_rate(self) -> float:
        """Working links divided by the links that were actually requested."""

        checked = self.unique_links_checked
        if not checked:
            return 0.0
        return round(self.working / checked * 100, 2)

    @property
    def reachable_rate(self) -> float:
        """Working **and** redirecting links over the requested links.

        A redirect is not a broken link, so this figure answers "how many links
        actually lead somewhere valid?" - useful when a site redirects a lot.
        """

        checked = self.unique_links_checked
        if not checked:
            return 0.0
        return round((self.working + self.redirects) / checked * 100, 2)

    @property
    def passed(self) -> bool:
        return self.broken == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_url": self.target_url,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": self.finished_at.isoformat(timespec="seconds"),
            "duration_seconds": round(self.duration_seconds, 3),
            "total_links_found": self.total_links_found,
            "unique_links_discovered": self.unique_links_discovered,
            "unique_links_checked": self.unique_links_checked,
            "working": self.working,
            "redirects": self.redirects,
            "broken": self.broken,
            "blocked": self.blocked,
            "skipped": self.skipped,
            "timeouts": self.timeouts,
            "connection_errors": self.connection_errors,
            "ssl_errors": self.ssl_errors,
            "client_errors": self.client_errors,
            "server_errors": self.server_errors,
            "invalid": self.invalid,
            "internal_links": self.internal_links,
            "external_links": self.external_links,
            "duplicate_occurrences": self.duplicate_occurrences,
            "success_rate": self.success_rate,
            "reachable_rate": self.reachable_rate,
            "passed": self.passed,
        }



# ---------------------------------------------------------------------------
# Link result model
# ---------------------------------------------------------------------------
@dataclass
class LinkResult:
    """Structured outcome for one discovered link.

    ``url`` is the normalised URL that was checked (de-duplicated), while
    ``occurrences`` remembers how often it appeared on the source page.
    """

    source_page: str
    url: str
    final_url: str = ""
    link_type: str = "internal"          # internal | external
    status: LinkStatus = LinkStatus.INVALID
    status_code: Optional[int] = None
    response_time: float = 0.0
    occurrences: int = 1
    method: str = ""
    attempts: int = 0
    error: str = ""
    checked_at: str = ""

    def __post_init__(self) -> None:
        if not self.final_url:
            self.final_url = self.url
        if not self.checked_at:
            self.checked_at = datetime.now().isoformat(timespec="seconds")

    # -- predicates used by the report layer ------------------------------
    @property
    def is_working(self) -> bool:
        return self.status is LinkStatus.WORKING

    @property
    def is_redirect(self) -> bool:
        return self.status is LinkStatus.REDIRECT

    @property
    def is_broken(self) -> bool:
        return self.status in FAILURE_STATUSES

    @property
    def is_skipped(self) -> bool:
        return self.status is LinkStatus.SKIPPED

    @property
    def was_checked(self) -> bool:
        return not self.is_skipped

    def to_dict(self) -> Dict[str, Any]:
        """Serialisable representation used by the CSV/JSON exports and tests."""

        return {
            "source_page": self.source_page,
            "url": self.url,
            "link_type": self.link_type,
            "status": str(self.status),
            "status_code": self.status_code if self.status_code is not None else "",
            "response_time": round(self.response_time, 3),
            "occurrences": self.occurrences,
            "final_url": self.final_url,
            "error": self.error,
            "checked_at": self.checked_at,
        }
