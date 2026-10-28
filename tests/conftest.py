"""Shared pytest fixtures.

The *unit* tests need neither a server nor a browser.  The *integration* tests
use the local HTTP test site that is started here, so the suite never depends
on a live website or on internet access.  The *browser* tests reuse the same
local site and are skipped automatically when no browser is available.
"""

from __future__ import annotations

import http.server
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterator, List
from urllib.parse import urlparse

import pytest

from src.config.config import Config
from src.pages.webpage import PageLink, WebPage
from src.utils.link_checker import LinkChecker, LinkResult, LinkStatistics, LinkStatus

# Home page of the local test site: contains every situation the checker must
# survive (working, duplicate, 404, 403, 500, redirect, slow and non-web links).
INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>Broken link test site</title></head>
<body>
  <h1>Local test site</h1>
  <a href="/about.html">Working internal link</a>
  <a href="/about.html">Duplicate of the working link</a>
  <a href="/missing.html">Broken internal link (404)</a>
  <a href="/forbidden">Forbidden link (403)</a>
  <a href="/server-error">Server error link (500)</a>
  <a href="/redirect">Redirect link (302)</a>
  <a href="/head-not-allowed">HEAD not allowed link</a>
  <a href="/slow">Slow link</a>
  <a href="http://localhost:{port}/about.html">External host link (same server)</a>
  <a href="mailto:qa@example.com">Mail link</a>
  <a href="tel:+910000000000">Phone link</a>
  <a href="javascript:void(0)">JavaScript link</a>
  <a href="#section">Fragment only link</a>
  <a>Anchor without href</a>
</body>
</html>
"""

# A page whose links all work: used for the "quality gate passed" test.
CLEAN_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>Clean page</title></head>
<body>
  <a href="/about.html">Working link</a>
  <a href="/about.html">Same working link again</a>
  <a href="/redirect">Redirecting but alive</a>
</body>
</html>
"""


class _TestSiteHandler(http.server.BaseHTTPRequestHandler):
    """A deliberately imperfect website used by the integration tests."""
    server_version = "BrokenLinkTestSite/1.0"
    protocol_version = "HTTP/1.1"
    #: how often ``/flaky`` was requested (``< 3`` answers 503)
    flaky_hits: Dict[str, int] = {}

    # keep the pytest output readable
    def log_message(self, *args: object) -> None:  # noqa: D102
        return

    # -- helpers ---------------------------------------------------------
    def _send(
        self,
        code: int,
        body: str = "",
        content_type: str = "text/html; charset=utf-8",
        extra_headers: Dict[str, str] | None = None,
    ) -> None:
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._route()

    def do_HEAD(self) -> None:  # noqa: N802 - http.server API
        self._route()

    def _route(self) -> None:
        path = urlparse(self.path).path
        port = self.server.server_port

        if path in ("/", "/index.html"):
            return self._send(200, INDEX_HTML.format(port=port))
        if path == "/clean.html":
            return self._send(200, CLEAN_HTML)
        if path == "/about.html":
            return self._send(200, "<html><body><h1>About us</h1></body></html>")
        if path == "/missing.html":
            return self._send(404, "<html><body>Not found</body></html>")
        if path == "/forbidden":
            return self._send(403, "")
        if path == "/server-error":
            return self._send(500, "")
        if path == "/redirect":
            return self._send(302, "", extra_headers={"Location": "/about.html"})
        if path == "/head-not-allowed":
            if self.command == "HEAD":
                return self._send(405, "")
            return self._send(200, "<html><body>GET works</body></html>")
        if path == "/slow":
            time.sleep(2.0)
            return self._send(200, "<html><body>Slow but alive</body></html>")
        if path == "/flaky":
            hits = self.flaky_hits.get("/flaky", 0) + 1
            self.flaky_hits["/flaky"] = hits
            if hits < 3:
                return self._send(503, "")
            return self._send(200, "<html><body>Recovered</body></html>")
        return self._send(404, "<html><body>Unknown path</body></html>")


@dataclass
class LocalSite:
    """Handle for the running local test site."""

    base_url: str

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"


class QuietThreadingHTTPServer(http.server.ThreadingHTTPServer):
    """Test server that stays silent when a client drops a connection.

    Link checking closes idle connections (``stream=True``) and aborts slow
    requests, which makes ``socketserver`` print a traceback for every dropped
    socket. That noise would pollute the pytest output, so reset/abort errors
    are ignored here.
    """

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:  # noqa: D102
        import sys

        error = sys.exc_info()[1]
        if isinstance(
            error,
            (ConnectionError, ConnectionResetError, ConnectionAbortedError,
             BrokenPipeError, TimeoutError),
        ):
            return
        super().handle_error(request, client_address)


@pytest.fixture(scope="session")
def local_site() -> Iterator[LocalSite]:
    """Start the local HTTP test site for the whole test session."""

    _TestSiteHandler.flaky_hits = {}
    server = QuietThreadingHTTPServer(("127.0.0.1", 0), _TestSiteHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    site = LocalSite(base_url=f"http://127.0.0.1:{server.server_port}")
    try:
        yield site
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def flaky_url(local_site: LocalSite) -> str:
    """URL that answers 503 twice and then recovers; the counter is reset."""

    _TestSiteHandler.flaky_hits = {}
    return local_site.url("/flaky")


@pytest.fixture(scope="session")
def free_port() -> int:
    """A port nothing listens on -> used for connection-error tests."""

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------------------
# configuration / object fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def fast_config(tmp_path: Path, local_site: LocalSite) -> Config:
    """Config pointing at the local site, writing into a temporary directory.

    ``max_retries=0`` keeps the suite fast; the retry tests override it.
    """

    return Config(
        target_url=local_site.url("/"),
        max_retries=0,
        retry_backoff=0.0,
        request_timeout=5.0,
        connect_timeout=2.0,
        max_workers=4,
        max_links=0,
        page_load_timeout=25,
        element_timeout=10,
        headless=True,
        log_level="WARNING",
        reports_dir=tmp_path / "reports",
        screenshots_dir=tmp_path / "screenshots",
        logs_dir=tmp_path / "logs",
    )


@pytest.fixture
def checker(fast_config: Config) -> Iterator[LinkChecker]:
    """A ready to use :class:`LinkChecker` bound to the local site."""

    with LinkChecker(fast_config) as instance:
        yield instance


@pytest.fixture
def broken_page_links(local_site: LocalSite) -> List[PageLink]:
    """The kind of link list Selenium would return for the local test site."""

    port = urlparse(local_site.base_url).port
    hrefs = [
        local_site.url("/about.html"),
        local_site.url("/about.html"),  # duplicate -> checked once, counted twice
        local_site.url("/missing.html"),
        local_site.url("/redirect"),
        local_site.url("/about.html?page=2"),
        f"http://localhost:{port}/about.html",  # different host -> external
        "mailto:qa@example.com",
        "#section",
        "",
    ]
    return [PageLink(href=href, text=f"link {index}", index=index) for index, href in enumerate(hrefs)]


# ---------------------------------------------------------------------------
# report fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def sample_results(local_site: LocalSite) -> List[LinkResult]:
    """A mixed result set (working, broken, redirect, skipped, timeout)."""

    page = local_site.url("/")
    return [
        LinkResult(
            source_page=page,
            url=local_site.url("/about.html"),
            status=LinkStatus.WORKING,
            status_code=200,
            response_time=0.123,
            occurrences=2,
        ),
        LinkResult(
            source_page=page,
            url=local_site.url("/missing.html"),
            status=LinkStatus.CLIENT_ERROR,
            status_code=404,
            response_time=0.231,
            error="Not Found",
        ),
        LinkResult(
            source_page=page,
            url=local_site.url("/redirect"),
            final_url=local_site.url("/about.html"),
            status=LinkStatus.REDIRECT,
            status_code=302,
            response_time=0.05,
        ),
        LinkResult(
            source_page=page,
            url="mailto:qa@example.com",
            status=LinkStatus.SKIPPED,
            link_type="non-web",
            error="Skipped: empty, fragment-only or non-web link",
        ),
        LinkResult(
            source_page=page,
            url="http://unreachable.invalid/page",
            status=LinkStatus.TIMEOUT,
            link_type="external",
            error="Request timed out",
        ),
    ]


@pytest.fixture
def sample_stats(sample_results: List[LinkResult], local_site: LocalSite) -> LinkStatistics:
    """Statistics object built from :func:`sample_results`."""

    started = datetime(2026, 1, 1, 10, 0, 0)
    return LinkStatistics.from_results(
        sample_results,
        target_url=local_site.url("/"),
        total_links_found=9,
        unique_links_discovered=5,
        started_at=started,
        finished_at=started + timedelta(seconds=4),
    )


# ---------------------------------------------------------------------------
# browser fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def browser_available() -> bool:
    """``True`` when a headless browser can actually be started."""

    page = WebPage(Config(headless=True, log_level="CRITICAL"))
    try:
        page.start_browser()
        return True
    except Exception:  # noqa: BLE001 - any failure means "no browser here"
        return False
    finally:
        page.close()


@pytest.fixture
def webpage(fast_config: Config, browser_available: bool) -> Iterator[WebPage]:
    """A :class:`WebPage` with a running browser (skips when unavailable)."""

    if not browser_available:
        pytest.skip("No browser available for Selenium tests")
    page = WebPage(fast_config)
    page.start_browser()
    try:
        yield page
    finally:
        page.close()
