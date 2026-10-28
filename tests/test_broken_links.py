"""Automated tests for the Automated Web Broken Link Detection framework.

Categories
----------
``unit``
    Pure logic without network or browser:
    :class:`TestUrlNormalisation`, :class:`TestStatusClassification`,
    :class:`TestUrlInventory`, :class:`TestLinkStatistics`,
    :class:`TestBrowserOptions`, :class:`TestReportGenerator`.
``integration``
    Real HTTP requests against the local test server started in ``conftest.py``:
    :class:`TestLinkCheckerOverHttp`, :class:`TestLinkCheckerRetries`,
    :class:`TestPipeline`.
``browser``
    Selenium against the local test server (skipped automatically when no
    browser is installed): :class:`TestWebPageSelenium`, :class:`TestRunEndToEnd`.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

import pytest
import requests

import run
from src.config.config import Config
from src.pages.webpage import PageLink, PageLoadError, WebPage, build_browser_options
from src.utils.link_checker import (
    FAILURE_STATUSES,
    LinkChecker,
    LinkResult,
    LinkStatistics,
    LinkStatus,
    UrlProcessor,
    classify_exception,
    classify_status_code,
    is_retryable,
)
from src.utils.report_generator import CSV_COLUMNS, ReportGenerator

PAGE_URL = "http://site.test/dir/page.html"


# ===========================================================================
# unit tests - URL normalisation and filtering
# ===========================================================================
@pytest.mark.unit
class TestUrlNormalisation:
    """Relative/absolute URLs, query strings, filters and de-duplication."""

    def test_normalisation_matrix(self) -> None:
        processor = UrlProcessor(base_url=PAGE_URL)
        cases = {
            "/about.html": "http://site.test/about.html",
            "about.html": "http://site.test/dir/about.html",
            "../team/": "http://site.test/team/",
            "./contact.html?ref=home": "http://site.test/dir/contact.html?ref=home",
            "https://other.test/x?y=1": "https://other.test/x?y=1",
            "//cdn.test/lib.js": "http://cdn.test/lib.js",
            "/search?q=broken+links&page=2": "http://site.test/search?q=broken+links&page=2",
            "/about.html#team": "http://site.test/about.html",
            "/double//slash//here": "http://site.test/double/slash/here",
        }
        for raw, expected in cases.items():
            assert processor.normalize(raw) == expected, raw

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "#",
            "#section",
            "mailto:qa@example.com",
            "tel:+910000000000",
            "sms:+910000000000",
            "javascript:void(0)",
            "javascript:;",
            "data:text/plain,hello",
            "ftp://files.test/x.zip",
            "http://",
            "https://",
            None,
        ],
    )
    def test_unusable_links_are_dropped(self, raw: str | None) -> None:
        assert UrlProcessor(base_url=PAGE_URL).normalize(raw) == ""

    def test_trailing_slash_is_preserved(self) -> None:
        """``/about`` and ``/about/`` may serve different resources."""

        processor = UrlProcessor(base_url=PAGE_URL)
        assert processor.normalize("/about") == "http://site.test/about"
        assert processor.normalize("/about/") == "http://site.test/about/"

    def test_scheme_and_host_are_lower_cased(self) -> None:
        processor = UrlProcessor(base_url=PAGE_URL)
        assert processor.normalize("HTTP://Site.TEST/About") == "http://site.test/About"

    def test_query_parameters_are_never_stripped(self) -> None:
        processor = UrlProcessor(base_url=PAGE_URL)
        url = processor.normalize("/filter?colour=red&size=42")
        assert url is not None
        assert "colour=red" in url and "size=42" in url


# ===========================================================================
# unit tests - status classification
# ===========================================================================
@pytest.mark.unit
class TestStatusClassification:
    """2xx -> WORKING, 3xx -> REDIRECT, 4xx/5xx -> errors, and so on."""

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            (200, LinkStatus.WORKING),
            (204, LinkStatus.WORKING),
            (301, LinkStatus.REDIRECT),
            (304, LinkStatus.REDIRECT),
            (400, LinkStatus.CLIENT_ERROR),
            (404, LinkStatus.CLIENT_ERROR),
            (408, LinkStatus.CLIENT_ERROR),
            (401, LinkStatus.BLOCKED),
            (403, LinkStatus.BLOCKED),
            (429, LinkStatus.BLOCKED),
            (500, LinkStatus.SERVER_ERROR),
            (503, LinkStatus.SERVER_ERROR),
            (None, LinkStatus.CONNECTION_ERROR),
        ],
    )
    def test_status_code_mapping(self, code: int | None, expected: LinkStatus) -> None:
        assert classify_status_code(code) == expected

    def test_failure_statuses_are_reported_as_broken(self) -> None:
        for status in FAILURE_STATUSES:
            result = LinkResult(source_page=PAGE_URL, url="http://site.test/x", status=status)
            assert result.is_broken is True
        for status in (
            LinkStatus.WORKING,
            LinkStatus.REDIRECT,
            LinkStatus.BLOCKED,
            LinkStatus.SKIPPED,
        ):
            result = LinkResult(source_page=PAGE_URL, url="http://site.test/x", status=status)
            assert result.is_broken is False

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (requests.exceptions.Timeout("slow"), LinkStatus.TIMEOUT),
            (requests.exceptions.ReadTimeout("slow"), LinkStatus.TIMEOUT),
            (requests.exceptions.ConnectTimeout("slow"), LinkStatus.TIMEOUT),
            (requests.exceptions.ConnectionError("refused"), LinkStatus.CONNECTION_ERROR),
            (requests.exceptions.SSLError("bad certificate"), LinkStatus.SSL_ERROR),
            (requests.exceptions.InvalidURL("broken"), LinkStatus.INVALID),
            (requests.exceptions.MissingSchema("no scheme"), LinkStatus.INVALID),
            (requests.exceptions.TooManyRedirects("loop"), LinkStatus.CLIENT_ERROR),
        ],
    )
    def test_exception_mapping(self, error: Exception, expected: LinkStatus) -> None:
        assert classify_exception(error) == expected

    def test_retryable_helper(self) -> None:
        assert is_retryable(LinkStatus.TIMEOUT) is True
        assert is_retryable(LinkStatus.CONNECTION_ERROR) is True
        assert is_retryable(LinkStatus.CLIENT_ERROR) is False
        assert is_retryable(LinkStatus.INVALID) is False

    def test_link_result_model_has_all_required_fields(self) -> None:
        result = LinkResult(
            source_page=PAGE_URL,
            url="http://site.test/x",
            status=LinkStatus.WORKING,
            status_code=200,
        )
        payload = result.to_dict()
        for field in (
            "source_page",
            "url",
            "final_url",
            "link_type",
            "status",
            "status_code",
            "response_time",
            "error",
            "checked_at",
            "occurrences",
        ):
            assert field in payload
        assert result.final_url == result.url  # defaulted to the checked URL
        assert result.checked_at  # filled automatically
        assert payload["status"] == "WORKING"  # StrEnum -> plain string


# ===========================================================================
# unit tests - inventory (duplicates, skipped links, internal/external)
# ===========================================================================
@pytest.mark.unit
class TestUrlInventory:
    """Duplicate handling, skip rules, limits, internal/external classification."""

    @staticmethod
    def processor(**kwargs: object) -> UrlProcessor:
        options = {"base_url": "http://site.test/"}
        options.update(kwargs)
        return UrlProcessor(**options)  # type: ignore[arg-type]

    def test_duplicates_are_checked_once_but_counted(self) -> None:
        inventory = self.processor().build_inventory([
            "http://site.test/about",
            "http://site.test/about",
            "http://site.test/about/",
            "http://site.test/about",
        ])
        assert inventory.total_found == 4
        assert inventory.unique_links_to_check == 2  # /about and /about/
        assert inventory.occurrences["http://site.test/about"] == 3
        assert inventory.duplicates == 2
        assert inventory.skipped == []

    def test_non_web_links_are_skipped_but_still_reported(self) -> None:
        inventory = self.processor().build_inventory([
            "mailto:qa@example.com",
            "mailto:qa@example.com",
            "tel:+911234567890",
            "#top",
            "",
            "/about",
        ])
        assert inventory.total_found == 6
        assert inventory.unique_links_to_check == 1
        skipped = {result.url: result for result in inventory.skipped}
        assert skipped["mailto:qa@example.com"].occurrences == 2
        assert skipped["mailto:qa@example.com"].status is LinkStatus.SKIPPED
        assert all(result.link_type == "non-web" for result in inventory.skipped)

    def test_internal_and_external_classification(self) -> None:
        inventory = self.processor().build_inventory([
            "http://site.test/a",
            "http://www.site.test/b",  # "www." is the same site
            "https://other.test/c",
            "http://localhost:8080/d",
        ])
        assert inventory.link_types["http://site.test/a"] == "internal"
        assert inventory.link_types["http://www.site.test/b"] == "internal"
        assert inventory.link_types["https://other.test/c"] == "external"
        assert inventory.internal_count == 2
        assert inventory.external_count == 2

    def test_external_links_can_be_disabled(self) -> None:
        inventory = self.processor(check_external_links=False).build_inventory([
            "http://site.test/a",
            "https://other.test/c",
        ])
        assert inventory.urls == ["http://site.test/a"]
        assert len(inventory.skipped) == 1
        assert "external link checking is disabled" in inventory.skipped[0].error

    def test_ignore_patterns_are_respected(self) -> None:
        inventory = self.processor().build_inventory([
            "http://site.test/a",
            "http://site.test/user/logout?logout=1",
            "http://site.test/cdn-cgi/trace",
        ])
        assert inventory.urls == ["http://site.test/a"]
        assert len(inventory.skipped) == 2

    def test_meaningful_query_parameters_are_still_checked(self) -> None:
        inventory = self.processor().build_inventory(["http://site.test/list?page=2"])
        assert inventory.urls == ["http://site.test/list?page=2"]

    def test_max_links_limits_the_run_and_reports_the_rest(self) -> None:
        inventory = self.processor().build_inventory(
            [f"http://site.test/{index}" for index in range(5)], max_links=2
        )
        assert inventory.unique_links_to_check == 2
        assert inventory.max_links_skipped == 3
        assert all("max_links" in result.error for result in inventory.skipped)
        assert len(inventory.plan) == 5  # nothing disappears from the report

    def test_merge_keeps_the_page_order(self) -> None:
        inventory = self.processor().build_inventory(
            ["http://site.test/a", "mailto:x@y.z", "http://site.test/b"]
        )
        checked = [
            LinkResult(source_page=inventory.source_page, url="http://site.test/a",
                       status=LinkStatus.WORKING, status_code=200),
            LinkResult(source_page=inventory.source_page, url="http://site.test/b",
                       status=LinkStatus.WORKING, status_code=200),
        ]
        merged = inventory.merge(checked)
        assert [result.url for result in merged] == [
            "http://site.test/a", "mailto:x@y.z", "http://site.test/b",
        ]
        assert merged[1].status is LinkStatus.SKIPPED

    def test_session_defaults(self, fast_config: Config) -> None:
        with LinkChecker(fast_config) as checker:
            assert checker.session.headers["User-Agent"] == fast_config.user_agent
            assert checker.session.verify is True


# ===========================================================================
# unit tests - statistics
# ===========================================================================
@pytest.mark.unit
class TestLinkStatistics:
    """The numbers that end up in the console summary and the dashboard."""

    def test_counts_and_success_rate(self, sample_stats: LinkStatistics) -> None:
        assert sample_stats.total_links_found == 9
        assert sample_stats.unique_links_discovered == 5
        assert sample_stats.unique_links_checked == 4
        assert sample_stats.working == 1
        assert sample_stats.broken == 2  # 404 + timeout
        assert sample_stats.redirects == 1
        assert sample_stats.skipped == 1
        assert sample_stats.timeouts == 1
        assert sample_stats.client_errors == 1
        assert sample_stats.internal_links == 3
        assert sample_stats.external_links == 1
        assert sample_stats.duplicate_occurrences == 1
        assert sample_stats.duration_seconds == 4.0
        assert sample_stats.success_rate == 25.0  # 1 working / 4 checked
        assert sample_stats.passed is False

    def test_to_dict_is_serialisable(self, sample_stats: LinkStatistics) -> None:
        payload = sample_stats.to_dict()
        assert payload["target_url"].startswith("http://127.0.0.1")
        assert payload["broken"] == 2
        assert payload["passed"] is False
        assert isinstance(payload["started_at"], str)

    def test_reachable_rate_counts_redirects_as_reachable(
        self, sample_stats: LinkStatistics
    ) -> None:
        # 1 working + 1 redirect out of 4 checked links
        assert sample_stats.success_rate == 25.0
        assert sample_stats.reachable_rate == 50.0


# ===========================================================================
# integration tests - real HTTP requests against the local test site
# ===========================================================================
@pytest.mark.integration
class TestLinkCheckerOverHttp:
    """Every real HTTP situation must be classified and never crash the run."""

    def test_working_link(self, checker: LinkChecker, local_site) -> None:
        result = checker.check_url(local_site.url("/about.html"))
        assert result.status is LinkStatus.WORKING
        assert result.status_code == 200
        assert result.is_broken is False
        assert result.response_time > 0

    def test_missing_page_is_broken(self, checker: LinkChecker, local_site) -> None:
        result = checker.check_url(local_site.url("/missing.html"))
        assert result.status is LinkStatus.CLIENT_ERROR
        assert result.status_code == 404
        assert result.is_broken is True
        assert result.error  # reason phrase, e.g. "Not Found"

    def test_forbidden_link_is_blocked_not_broken(self, checker: LinkChecker, local_site) -> None:
        result = checker.check_url(local_site.url("/forbidden"))
        assert result.status is LinkStatus.BLOCKED
        assert result.status_code == 403
        assert result.is_broken is False

    def test_server_error_is_broken(self, checker: LinkChecker, local_site) -> None:
        result = checker.check_url(local_site.url("/server-error"))
        assert result.status is LinkStatus.SERVER_ERROR
        assert result.status_code == 500
        assert result.is_broken is True

    def test_redirect_is_followed_and_reported(self, checker: LinkChecker, local_site) -> None:
        result = checker.check_url(local_site.url("/redirect"))
        assert result.status is LinkStatus.REDIRECT
        assert result.status_code == 302
        assert result.final_url.endswith("/about.html")
        assert result.is_broken is False

    def test_head_is_preferred_with_get_fallback(self, checker: LinkChecker, local_site) -> None:
        result = checker.check_url(local_site.url("/head-not-allowed"))
        assert result.status is LinkStatus.WORKING
        assert result.method == "GET"  # 405 on HEAD -> retried with GET
        assert result.status_code == 200

    def test_timeout_is_reported(self, fast_config: Config, local_site) -> None:
        config = fast_config.with_overrides(
            request_timeout=0.5, connect_timeout=0.5, max_retries=0
        )
        with LinkChecker(config) as checker:
            result = checker.check_url(local_site.url("/slow"))
        assert result.status is LinkStatus.TIMEOUT
        assert result.is_broken is True

    def test_unreachable_host_is_reported_and_is_broken(
        self, checker: LinkChecker, free_port: int
    ) -> None:
        """A closed port surfaces as CONNECTION_ERROR (refused) or TIMEOUT
        (packets dropped by a firewall) - both are failures."""

        result = checker.check_url(f"http://127.0.0.1:{free_port}/")
        assert result.status in (LinkStatus.CONNECTION_ERROR, LinkStatus.TIMEOUT)
        assert result.status_code is None
        assert result.is_broken is True
        assert result.error  # a human readable reason is always recorded

    def test_check_many_keeps_order_and_annotates_results(
        self, checker: LinkChecker, local_site, broken_page_links: List[PageLink]
    ) -> None:
        page_url = local_site.url("/")
        inventory = UrlProcessor(base_url=page_url).build_inventory(
            [link.href for link in broken_page_links], source_page=page_url
        )
        results = checker.check_links(
            inventory.urls,
            source_page=page_url,
            occurrences=inventory.occurrences,
            link_types=inventory.link_types,
        )
        assert [result.url for result in results] == inventory.urls
        assert results[0].occurrences == 2  # /about.html appeared twice
        assert all(result.source_page == page_url for result in results)
        assert any(result.link_type == "external" for result in results)

    def test_no_links_means_no_requests(self, checker: LinkChecker) -> None:
        assert checker.check_links([]) == []


# ===========================================================================
# integration tests - retry behaviour
# ===========================================================================
@pytest.mark.integration
class TestLinkCheckerRetries:
    """Retries: transient failures are retried, permanent ones are not."""

    def test_transient_failure_is_retried_until_it_recovers(
        self, fast_config: Config, flaky_url: str
    ) -> None:
        config = fast_config.with_overrides(max_retries=3, retry_backoff=0.0)
        with LinkChecker(config) as checker:
            result = checker.check_url(flaky_url)
        assert result.status is LinkStatus.WORKING
        assert result.status_code == 200
        assert result.attempts >= 2  # the earlier 503 answers were retried

    def test_retries_are_limited_and_the_last_failure_is_reported(
        self, fast_config: Config, flaky_url: str
    ) -> None:
        config = fast_config.with_overrides(max_retries=1, retry_backoff=0.0)
        with LinkChecker(config) as checker:
            result = checker.check_url(flaky_url)
        assert result.attempts == 2  # initial attempt + exactly one retry
        assert result.status is LinkStatus.SERVER_ERROR
        assert result.is_broken is True

    def test_retries_can_be_disabled(self, checker: LinkChecker, flaky_url: str) -> None:
        result = checker.check_url(flaky_url)  # fast_config -> max_retries=0
        assert result.attempts == 1
        assert result.status is LinkStatus.SERVER_ERROR

    def test_unreachable_hosts_are_retried(self, fast_config: Config, free_port: int) -> None:
        config = fast_config.with_overrides(max_retries=2, retry_backoff=0.0, connect_timeout=0.5)
        with LinkChecker(config) as checker:
            result = checker.check_url(f"http://127.0.0.1:{free_port}/")
        assert result.attempts == 3  # 1 attempt + 2 retries
        assert result.status in (LinkStatus.CONNECTION_ERROR, LinkStatus.TIMEOUT)
        assert result.is_broken is True

    def test_invalid_urls_are_not_retried(self, fast_config: Config) -> None:
        config = fast_config.with_overrides(max_retries=3, retry_backoff=0.0)
        with LinkChecker(config) as checker:
            result = checker.check_url("http://")
        assert result.status is LinkStatus.INVALID
        assert result.attempts == 1

    def test_client_errors_are_not_retried(self, checker: LinkChecker, local_site) -> None:
        result = checker.check_url(local_site.url("/missing.html"))
        assert result.attempts == 1
        assert result.status is LinkStatus.CLIENT_ERROR


# ===========================================================================
# unit tests - report generation
# ===========================================================================
@pytest.mark.unit
class TestReportGenerator:
    """CSV export, HTML dashboard and the console summary."""

    def test_csv_has_header_and_one_row_per_link(
        self, fast_config: Config, sample_results: List[LinkResult], sample_stats: LinkStatistics
    ) -> None:
        path = ReportGenerator(fast_config).write_csv(sample_results)
        assert path.exists()
        rows = list(csv.reader(path.read_text(encoding="utf-8-sig").splitlines()))
        assert rows[0] == list(CSV_COLUMNS)
        assert len(rows) == len(sample_results) + 1
        broken_row = next(row for row in rows if row[3] == "CLIENT_ERROR")
        assert "404" in broken_row

    def test_generate_writes_both_reports(
        self, fast_config: Config, sample_results: List[LinkResult], sample_stats: LinkStatistics
    ) -> None:
        paths = ReportGenerator(fast_config).generate(sample_results, sample_stats)
        assert set(paths) == {"csv", "html"}
        assert paths["csv"] == Path(fast_config.csv_report_path)
        assert paths["html"] == Path(fast_config.html_report_path)
        assert all(path.exists() for path in paths.values())

    def test_html_dashboard_contains_every_required_section(
        self, fast_config: Config, sample_results: List[LinkResult], sample_stats: LinkStatistics
    ) -> None:
        html = ReportGenerator(fast_config).write_html(sample_results, sample_stats).read_text(
            encoding="utf-8"
        )
        for expected in (
            "Broken Link Detection Report",
            sample_stats.target_url,
            "Total links found",
            "Unique links checked",
            "Working links",
            "Broken links",
            "Redirects",
            "Skipped links",
            "Timeout errors",
            "Connection errors",
            "Internal links",
            "External links",
            "Success rate",
            "Reachable (incl. redirects)",
            "FAIL",  # verdict badge
            'id="search"',  # search box
            'data-filter="broken"',  # filter buttons
            "data-status=",  # row attribute used by the JavaScript
            "querySelectorAll('#links tbody tr')",  # embedded filtering logic
        ):
            assert expected in html, expected

    def test_html_escapes_untrusted_values(self, fast_config: Config, sample_stats: LinkStatistics) -> None:
        nasty = LinkResult(
            source_page=sample_stats.target_url,
            url='http://site.test/?q="><script>alert(1)</script>',
            status=LinkStatus.CLIENT_ERROR,
            status_code=404,
            error="<b>boom</b>",
        )
        html = ReportGenerator(fast_config).write_html([nasty], sample_stats).read_text(encoding="utf-8")
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html
        assert "<b>boom</b>" not in html

    def test_empty_result_set_is_handled(self, fast_config: Config, sample_stats: LinkStatistics) -> None:
        html = ReportGenerator(fast_config).write_html([], sample_stats).read_text(encoding="utf-8")
        assert "No links were found on this page." in html

    def test_console_summary_prints_the_key_numbers(
        self,
        fast_config: Config,
        sample_results: List[LinkResult],
        sample_stats: LinkStatistics,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        generator = ReportGenerator(fast_config)
        paths = generator.generate(sample_results, sample_stats)
        generator.print_console_summary(sample_stats, paths)
        output = capsys.readouterr().out
        lines = output.splitlines()
        assert "BROKEN LINK AUTOMATION TEST" in output
        assert any(line.startswith("Total Links Found:") and line.endswith("9") for line in lines)
        assert any(line.startswith("Broken:") and line.endswith("2") for line in lines)
        assert "Success Rate: 25.0%" in output
        assert "Broken links:" in output
        assert sample_results[1].url in output  # the 404 is listed
        assert str(paths["csv"]) in output


# ===========================================================================
# integration tests - the whole pipeline without a browser
# ===========================================================================
@pytest.mark.integration
class TestPipeline:
    """extraction output -> inventory -> HTTP checks -> reports."""

    def test_pipeline_produces_reports_and_statistics(
        self,
        fast_config: Config,
        local_site,
        broken_page_links: List[PageLink],
    ) -> None:
        logger = run.get_logger(level="CRITICAL", log_dir=fast_config.logs_dir)
        final_url = local_site.url("/")

        inventory = run.process_links(broken_page_links, final_url, fast_config, logger)
        assert inventory.total_found == len(broken_page_links) == 9
        assert inventory.unique_links_to_check == 5
        assert inventory.skipped and len(inventory.skipped) == 3  # mailto, #, empty
        assert inventory.duplicates == 1

        results = run.check_inventory(inventory, fast_config, logger)
        assert len(results) == 8  # 5 checked + 3 skipped, in page order

        stats = LinkStatistics.from_results(
            results,
            target_url=final_url,
            total_links_found=inventory.total_found,
            unique_links_discovered=inventory.discovered_unique,
            started_at=datetime.now() - timedelta(seconds=1),
        )
        assert stats.unique_links_checked == 5
        assert stats.working == 3
        assert stats.broken == 1
        assert stats.redirects == 1
        assert stats.skipped == 3
        assert stats.external_links == 1
        assert stats.success_rate == 60.0  # 3 working / 5 checked
        assert stats.passed is False

        statuses = {result.url: result.status for result in results}
        assert statuses[local_site.url("/missing.html")] is LinkStatus.CLIENT_ERROR
        assert statuses[local_site.url("/redirect")] is LinkStatus.REDIRECT

        paths = ReportGenerator(fast_config).generate(results, stats)
        html = paths["html"].read_text(encoding="utf-8")
        assert "Broken links" in html  # KPI card
        assert "CLIENT_ERROR" in html and "REDIRECT" in html and "SKIPPED" in html

        with paths["csv"].open(encoding="utf-8-sig", newline="") as handle:
            csv_rows = list(csv.DictReader(handle))
        assert len(csv_rows) == 8
        broken = [row for row in csv_rows if row["status"] == "CLIENT_ERROR"]
        assert len(broken) == 1
        assert broken[0]["status_code"] == "404"


# ===========================================================================
# unit tests - browser options and the command line
# ===========================================================================
@pytest.mark.unit
class TestBrowserOptions:
    """Selenium options are built without launching a browser."""

    def test_chrome_headless_options(self) -> None:
        options = build_browser_options(browser="chrome", headless=True, window_size="1280,720")
        assert "--headless=new" in options.arguments
        assert "--window-size=1280,720" in options.arguments

    def test_headless_can_be_disabled(self) -> None:
        options = build_browser_options(browser="chrome", headless=False)
        assert all("headless" not in argument for argument in options.arguments)

    def test_user_agent_is_forwarded(self) -> None:
        options = build_browser_options(browser="chrome", user_agent="QA-Agent/1.0")
        assert "--user-agent=QA-Agent/1.0" in options.arguments

    def test_unsupported_browser_is_rejected(self) -> None:
        with pytest.raises(PageLoadError):
            build_browser_options(browser="netscape")

    def test_driver_requires_a_started_browser(self, fast_config: Config) -> None:
        page = WebPage(fast_config)
        with pytest.raises(PageLoadError):
            _ = page.driver
        assert page.is_open is False
        page.close()  # must be a safe no-op


@pytest.mark.unit
class TestCommandLine:
    """CLI flags override configuration without touching the defaults."""

    def test_flags_override_the_config(self) -> None:
        args = run.parse_args([
            "--url", "https://cli.test/x",
            "--retries", "5",
            "--no-external",
            "--max-links", "7",
            "--no-headless",
            "--timeout", "3.5",
            "--max-workers", "2",
            "--log-level", "debug",
        ])
        config = run.config_from_args(args)
        assert config.target_url == "https://cli.test/x"
        assert config.max_retries == 5
        assert config.check_external_links is False
        assert config.max_links == 7
        assert config.headless is False
        assert config.request_timeout == 3.5
        assert config.max_workers == 2
        assert config.log_level == "DEBUG"

    def test_unspecified_flags_keep_the_defaults(self) -> None:
        config = run.config_from_args(run.parse_args([]))
        assert config.target_url  # never empty
        assert config.max_retries == Config().max_retries
        assert config.headless == Config().headless

    def test_reports_dir_flag_changes_the_output_paths(self, tmp_path: Path) -> None:
        target = tmp_path / "custom-reports"
        config = run.config_from_args(run.parse_args(["--reports-dir", str(target)]))
        assert Path(config.reports_dir) == target
        assert Path(config.csv_report_path) == target / "broken_links.csv"
        assert Path(config.html_report_path) == target / "report.html"


# ===========================================================================
# browser (UI) tests - a real Selenium session against the local test site
# ===========================================================================
@pytest.mark.browser
class TestWebPageSelenium:
    """Selenium extraction: page load, anchors and failure evidence."""

    def test_page_loads_with_expected_title(self, webpage: WebPage, local_site) -> None:
        final_url = webpage.open_page(local_site.url("/"))
        assert final_url.startswith(local_site.base_url)
        assert webpage.title == "Broken link test site"
        assert webpage.current_url.startswith(local_site.base_url)

    def test_get_links_returns_structured_links(self, webpage: WebPage, local_site) -> None:
        webpage.open_page(local_site.url("/"))
        links = webpage.get_links()
        hrefs = [link.href for link in links]

        assert len(links) >= 10
        assert all(isinstance(link, PageLink) for link in links)
        assert local_site.url("/about.html") in hrefs  # resolved to absolute by the browser
        assert local_site.url("/missing.html") in hrefs
        assert any(href.startswith("mailto:") for href in hrefs)
        assert any(link.text for link in links)  # anchor text is captured
        assert [link.index for link in links] == list(range(len(links)))

    def test_max_links_limits_the_extraction(self, webpage: WebPage, local_site) -> None:
        webpage.open_page(local_site.url("/"))
        assert len(webpage.get_links(max_links=3)) == 3

    def test_one_browser_session_is_reused(self, webpage: WebPage, local_site) -> None:
        first_driver = webpage.driver
        webpage.open_page(local_site.url("/about.html"))
        assert webpage.driver is first_driver  # never a new browser per URL

    def test_page_load_failure_raises_and_saves_a_screenshot(
        self, fast_config: Config, free_port: int
    ) -> None:
        config = fast_config.with_overrides(page_load_timeout=5, element_timeout=5)
        page = WebPage(config)
        screenshot = None
        try:
            page.start_browser()
            with pytest.raises(PageLoadError):
                page.open_page(f"http://127.0.0.1:{free_port}/")
            screenshot = page.save_screenshot("page_load_failure")
        finally:
            page.close()

        assert screenshot is not None
        assert screenshot.exists()
        assert screenshot.parent == Path(config.screenshots_dir)
        assert "page_load_failure" in screenshot.name

    def test_context_manager_closes_the_browser(self, fast_config: Config, browser_available: bool) -> None:
        if not browser_available:
            pytest.skip("No browser available for Selenium tests")
        with WebPage(fast_config) as page:
            page.start_browser()
            assert page.is_open is True
        assert page.is_open is False


@pytest.mark.browser
class TestRunEndToEnd:
    """The complete ``python run.py`` flow against the local test site."""

    def test_run_reports_broken_links_and_writes_evidence(
        self, fast_config: Config, local_site, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = run.run(
            fast_config, run.get_logger(level="CRITICAL", log_dir=fast_config.logs_dir)
        )
        output = capsys.readouterr().out

        assert exit_code == run.EXIT_BROKEN_LINKS_FOUND  # 1 -> failing quality gate
        assert "BROKEN LINK AUTOMATION TEST" in output
        assert any(
            line.startswith("Broken:") and not line.endswith("0")
            for line in output.splitlines()
        )

        csv_path = Path(fast_config.csv_report_path)
        html_path = Path(fast_config.html_report_path)
        assert csv_path.exists() and html_path.exists()
        assert csv_path.stat().st_size > 0

        html = html_path.read_text(encoding="utf-8")
        assert local_site.base_url in html
        assert "FAIL" in html

        screenshots = list(Path(fast_config.screenshots_dir).glob("*.png"))
        assert screenshots, "a screenshot should be captured when broken links are found"

    def test_run_passes_on_a_clean_page(self, fast_config: Config, local_site) -> None:
        config = fast_config.with_overrides(target_url=local_site.url("/clean.html"))
        exit_code = run.run(config, run.get_logger(level="CRITICAL", log_dir=config.logs_dir))
        assert exit_code == run.EXIT_SUCCESS  # 0 -> quality gate passed
        html = Path(config.html_report_path).read_text(encoding="utf-8")
        assert "PASS" in html

    def test_run_returns_error_code_when_the_page_cannot_be_opened(
        self, fast_config: Config, free_port: int
    ) -> None:
        config = fast_config.with_overrides(
            target_url=f"http://127.0.0.1:{free_port}/",
            page_load_timeout=5,
            element_timeout=5,
        )
        assert run.run(config, run.get_logger(level="CRITICAL", log_dir=config.logs_dir)) == (
            run.EXIT_RUNTIME_ERROR
        )