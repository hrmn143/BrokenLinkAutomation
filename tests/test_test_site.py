"""Tests for the local demo website in ``test_site/``.

The demo server is started *in-process on a random free port* by the fixtures
below, so this module never depends on a permanently running server and never
conflicts with a demo instance the user may already have started on port 8000.
Everything here is offline: the external link of the demo page is exercised
with ``check_external_links=False``.
"""

from __future__ import annotations

import http.server
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, Iterator, List

import pytest
import requests

import run
import test_site.server as demo_server
from src.config.config import Config
from src.pages.webpage import PageLink
from src.utils.link_checker import LinkResult, LinkStatistics, LinkStatus
from src.utils.report_generator import ReportGenerator

#: the port the absolute links inside index.html point at
DEMO_PORT = 8000


@dataclass
class DemoSite:
    """URLs of the running demo site."""

    base_url: str
    index_url: str
    about_url: str
    missing_url: str
    redirect_url: str


class _QuietDemoServer(http.server.ThreadingHTTPServer):
    """The production demo server without traceback noise for dropped sockets."""

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:  # noqa: D102
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class _HrefCollector(HTMLParser):
    """Collect the ``href`` values of a page using the standard library."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[tuple]) -> None:  # noqa: D102
        if tag.lower() != "a":
            return
        for name, value in attrs:
            if name.lower() == "href" and value:
                self.hrefs.append(value.strip())


@pytest.fixture
def demo_site() -> Iterator[DemoSite]:
    """Serve ``test_site/`` on a random port for the duration of one test."""

    server = demo_server.build_server(
        host="127.0.0.1",
        port=0,
        site_dir=demo_server.SITE_DIR,
        server_class=_QuietDemoServer,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    site = DemoSite(
        base_url=base,
        index_url=f"{base}/",
        about_url=f"{base}/about.html",
        missing_url=f"{base}/does-not-exist",
        redirect_url=f"{base}/redirect",
    )
    try:
        yield site
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(url: str, allow_redirects: bool = True) -> requests.Response:
    return requests.get(url, timeout=5, allow_redirects=allow_redirects)


def _retarget(href: str, site: DemoSite) -> str:
    """Point the demo's fixed ``localhost:8000`` links at the test server.

    ``index.html`` deliberately contains absolute ``http://localhost:8000/...``
    URLs (that is what the demo documents). When the test server runs on a
    random port those links are rewritten, so the very same page can be
    validated without requiring port 8000 to be free.
    """

    return href.replace(f"http://localhost:{DEMO_PORT}", site.base_url)


# ===========================================================================
# unit tests - documented defaults and page content
# ===========================================================================
@pytest.mark.unit
class TestDemoSiteDefaults:
    """The documented settings and the static content of the demo site."""

    def test_defaults_match_the_documentation(self) -> None:
        assert demo_server.DEFAULT_HOST == "127.0.0.1"
        assert demo_server.DEFAULT_PORT == 8000
        assert demo_server.REDIRECT_PATH == "/redirect"
        assert demo_server.REDIRECT_TARGET == "/about.html"
        assert demo_server.REDIRECT_STATUS == 302

    def test_site_directory_contains_the_demo_files(self) -> None:
        assert (demo_server.SITE_DIR / "index.html").is_file()
        assert (demo_server.SITE_DIR / "about.html").is_file()
        assert (demo_server.SITE_DIR / "server.py").is_file()

    def test_host_and_port_are_configurable(self) -> None:
        parser = demo_server.build_arg_parser()
        defaults = parser.parse_args([])
        assert defaults.host == "127.0.0.1"
        assert defaults.port == 8000

        custom = parser.parse_args(["--host", "0.0.0.0", "--port", "9000"])
        assert custom.host == "0.0.0.0"
        assert custom.port == 9000

    def test_index_page_contains_every_required_link_type(self) -> None:
        html = (demo_server.SITE_DIR / "index.html").read_text(encoding="utf-8")
        assert "Broken Link Automation Test Page" in html
        for expected in (
            'href="http://localhost:8000/about.html"',  # working + duplicates
            'href="http://localhost:8000/does-not-exist"',  # broken (404)
            'href="http://localhost:8000/redirect"',  # redirect (302)
            'href="https://example.com"',  # external
            'href="mailto:test@example.com"',  # skipped
            'href="javascript:void(0)"',  # skipped
        ):
            assert expected in html, expected

        # 3 absolute + 1 relative link to about.html = 4 occurrences of one URL
        assert html.count('href="http://localhost:8000/about.html"') == 3
        assert 'href="about.html"' in html

    def test_about_page_contains_a_link_back_to_the_index(self) -> None:
        html = (demo_server.SITE_DIR / "about.html").read_text(encoding="utf-8")
        assert "About Test Page" in html
        assert 'href="http://localhost:8000/"' in html


# ===========================================================================
# integration tests - real requests against the demo server
# ===========================================================================
@pytest.mark.integration
class TestDemoSiteResponses:
    """The server really answers 200 / 302 / 404 / 403 as documented."""

    def test_index_page_returns_200(self, demo_site: DemoSite) -> None:
        response = _get(demo_site.index_url)
        assert response.status_code == 200
        assert response.headers["Content-Type"].startswith("text/html")
        assert "Broken Link Automation Test Page" in response.text

    def test_about_page_returns_200_and_links_back(self, demo_site: DemoSite) -> None:
        response = _get(demo_site.about_url)
        assert response.status_code == 200
        assert "About Test Page" in response.text
        assert 'href="http://localhost:8000/"' in response.text

    def test_missing_page_returns_404(self, demo_site: DemoSite) -> None:
        response = _get(demo_site.missing_url, allow_redirects=False)
        assert response.status_code == 404
        assert "404" in response.text

    def test_unknown_file_returns_404(self, demo_site: DemoSite) -> None:
        response = _get(f"{demo_site.base_url}/nope.html", allow_redirects=False)
        assert response.status_code == 404

    def test_redirect_returns_302_to_about(self, demo_site: DemoSite) -> None:
        response = _get(demo_site.redirect_url, allow_redirects=False)
        assert response.status_code == 302
        assert response.headers["Location"] == "/about.html"

    def test_redirect_target_is_reachable(self, demo_site: DemoSite) -> None:
        response = _get(demo_site.redirect_url)  # the redirect is followed
        assert response.status_code == 200
        assert "About Test Page" in response.text

    def test_head_requests_are_supported(self, demo_site: DemoSite) -> None:
        """The detector sends HEAD first, so the demo server must answer it."""

        assert requests.head(demo_site.index_url, timeout=5).status_code == 200
        assert requests.head(demo_site.missing_url, timeout=5).status_code == 404
        redirect = requests.head(demo_site.redirect_url, timeout=5, allow_redirects=False)
        assert redirect.status_code == 302
        assert redirect.headers["Location"] == "/about.html"

    def test_source_files_are_not_served(self, demo_site: DemoSite) -> None:
        assert _get(f"{demo_site.base_url}/server.py", allow_redirects=False).status_code == 403


# ===========================================================================
# integration tests - the real detector against the demo website
# ===========================================================================
@pytest.mark.integration
class TestDetectorAgainstDemoSite:
    """Proves the framework detects working, broken, redirect and skipped links."""

    @staticmethod
    def _page_links(site: DemoSite) -> List[PageLink]:
        """Read the served index page and build the links Selenium would return."""

        html = _get(site.index_url).text
        collector = _HrefCollector()
        collector.feed(html)
        collector.close()
        return [
            PageLink(href=_retarget(href, site), text=f"link {index}", index=index)
            for index, href in enumerate(collector.hrefs)
        ]

    @staticmethod
    def _config(site: DemoSite, tmp_path: Path) -> Config:
        return Config(
            target_url=site.index_url,
            check_external_links=False,  # keeps this test offline
            max_retries=0,
            retry_backoff=0.0,
            request_timeout=5.0,
            connect_timeout=2.0,
            max_workers=4,
            max_links=0,
            log_level="CRITICAL",
            reports_dir=tmp_path / "reports",
            screenshots_dir=tmp_path / "screenshots",
            logs_dir=tmp_path / "logs",
        )

    def test_every_demo_link_type_is_detected_correctly(
        self, demo_site: DemoSite, tmp_path: Path
    ) -> None:
        config = self._config(demo_site, tmp_path)
        logger = run.get_logger(level="CRITICAL", log_dir=config.logs_dir)

        page_links = self._page_links(demo_site)
        assert len(page_links) == 9  # the 8 required types; about.html appears 4x

        inventory = run.process_links(page_links, demo_site.index_url, config, logger)
        results = run.check_inventory(inventory, config, logger)
        by_url: Dict[str, LinkResult] = {result.url: result for result in results}

        # 1. working link -> 200 WORKING
        working = by_url[demo_site.about_url]
        assert working.status is LinkStatus.WORKING
        assert working.status_code == 200
        assert working.link_type == "internal"

        # 2. broken link -> 404 CLIENT_ERROR
        broken = by_url[demo_site.missing_url]
        assert broken.status is LinkStatus.CLIENT_ERROR
        assert broken.status_code == 404
        assert broken.is_broken is True

        # 3. redirect link -> 302 REDIRECT, resolved to about.html
        redirect = by_url[demo_site.redirect_url]
        assert redirect.status is LinkStatus.REDIRECT
        assert redirect.status_code == 302
        assert redirect.final_url.endswith("/about.html")
        assert redirect.is_broken is False

        # 4. + 6. internal and duplicate links share one normalised URL:
        #         1 working + 1 relative + 2 duplicates = 4 occurrences
        assert working.occurrences == 4
        assert inventory.occurrences[demo_site.about_url] == 4
        assert inventory.duplicates == 3

        # 5. external link -> external, skipped because external checks are off
        #    (a bare domain is normalised to its "/" URL)
        external = by_url["https://example.com/"]
        assert external.status is LinkStatus.SKIPPED
        assert external.link_type == "external"

        # 7. mailto -> skipped, never requested
        mail = by_url["mailto:test@example.com"]
        assert mail.status is LinkStatus.SKIPPED
        assert mail.error.startswith("Skipped")

        # 8. javascript -> skipped, never requested
        javascript = by_url["javascript:void(0)"]
        assert javascript.status is LinkStatus.SKIPPED

    def test_duplicate_urls_are_requested_only_once(
        self, demo_site: DemoSite, tmp_path: Path
    ) -> None:
        config = self._config(demo_site, tmp_path)
        logger = run.get_logger(level="CRITICAL", log_dir=config.logs_dir)

        inventory = run.process_links(
            self._page_links(demo_site), demo_site.index_url, config, logger
        )
        assert inventory.total_found == 9
        assert inventory.unique_links_to_check == 3  # about, does-not-exist, redirect
        assert inventory.urls == [
            demo_site.about_url,
            demo_site.missing_url,
            demo_site.redirect_url,
        ]
        assert inventory.urls.count(demo_site.about_url) == 1

    def test_reports_contain_the_expected_detection_rows(
        self, demo_site: DemoSite, tmp_path: Path
    ) -> None:
        config = self._config(demo_site, tmp_path)
        logger = run.get_logger(level="CRITICAL", log_dir=config.logs_dir)

        inventory = run.process_links(
            self._page_links(demo_site), demo_site.index_url, config, logger
        )
        results = run.check_inventory(inventory, config, logger)
        stats = LinkStatistics.from_results(
            results,
            target_url=demo_site.index_url,
            total_links_found=inventory.total_found,
            unique_links_discovered=inventory.discovered_unique,
            started_at=datetime.now(),
        )
        assert stats.unique_links_checked == 3
        assert stats.working == 1
        assert stats.broken == 1
        assert stats.redirects == 1
        assert stats.skipped == 3
        assert stats.passed is False

        paths = ReportGenerator(config).generate(results, stats)
        assert paths["csv"].exists() and paths["html"].exists()

        csv_text = paths["csv"].read_text(encoding="utf-8-sig")
        assert "CLIENT_ERROR,404" in csv_text  # the broken link
        assert "REDIRECT,302" in csv_text  # the redirect
        assert "SKIPPED" in csv_text  # mailto / javascript / external

        html = paths["html"].read_text(encoding="utf-8")
        assert demo_site.index_url in html
        assert "CLIENT_ERROR" in html and "REDIRECT" in html and "SKIPPED" in html
        assert "FAIL" in html  # one broken link -> failing quality gate