#!/usr/bin/env python
"""Entry point of the *Automated Web Broken Link Detection and Reporting System*.

Usage::

    python run.py
    python run.py --url https://example.com
    python run.py --url https://example.com --no-headless --max-links 100 --no-external

High level flow implemented here (details in ``README.md``)::

    1. load configuration          6. normalise + de-duplicate links
    2. initialise logging          7. validate links over HTTP (retries)
    3. start Selenium              8. write CSV + HTML reports
    4. open the target website     9. print the summary
    5. extract the <a href> links 10. release the browser

Exit codes: ``0`` all links working, ``1`` broken links found, ``2`` runtime error.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

# Running "python run.py" from any working directory must import src.*
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:  # pragma: no cover - import guard
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config.config import VALID_BROWSERS, Config
from src.pages.webpage import PageLink, PageLoadError, WebPage
from src.utils.link_checker import LinkChecker, LinkInventory, LinkResult, LinkStatistics, UrlProcessor
from src.utils.logger import get_logger
from src.utils.report_generator import ReportGenerator

EXIT_SUCCESS = 0
EXIT_BROKEN_LINKS_FOUND = 1
EXIT_RUNTIME_ERROR = 2


def build_arg_parser() -> argparse.ArgumentParser:
    """Command line flags; every flag is optional and overrides the config."""

    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Detect broken links on a website and generate QA reports.",
    )
    parser.add_argument("--url", help="target website (default: config / BLA_TARGET_URL)")
    parser.add_argument("--browser", choices=list(VALID_BROWSERS), help="browser used by Selenium")
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="run the browser without a visible window",
    )
    parser.add_argument("--max-links", type=int, help="maximum number of unique links to check")
    parser.add_argument(
        "--external",
        dest="check_external_links",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="also check links that point to other domains",
    )
    parser.add_argument("--timeout", type=float, help="HTTP read timeout in seconds")
    parser.add_argument("--retries", type=int, help="retries for transient failures")
    parser.add_argument("--max-workers", type=int, help="parallel HTTP workers")
    parser.add_argument(
        "--screenshot",
        dest="screenshot_on_failure",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="save a screenshot when a page level failure or broken links occur",
    )
    parser.add_argument("--reports-dir", help="folder that receives the reports")
    parser.add_argument("--log-level", help="DEBUG, INFO, WARNING or ERROR")
    return parser


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    return build_arg_parser().parse_args(argv)


def config_from_args(args: argparse.Namespace) -> Config:
    """Environment config with the command line flags applied on top."""

    return Config.from_env().with_overrides(
        target_url=args.url,
        browser=args.browser,
        headless=args.headless,
        max_links=args.max_links,
        check_external_links=args.check_external_links,
        request_timeout=args.timeout,
        max_retries=args.retries,
        max_workers=args.max_workers,
        screenshot_on_failure=args.screenshot_on_failure,
        reports_dir=Path(args.reports_dir) if args.reports_dir else None,
        log_level=args.log_level.upper() if args.log_level else None,
    )


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------
def collect_page_links(page: WebPage, logger) -> Tuple[str, List[PageLink]]:
    """Open the target website and extract every anchor element (Selenium)."""

    final_url = page.open_page()
    links = page.get_links()
    logger.info("Page title: %s", page.title or "(no title)")
    logger.info("Total <a> elements found: %s", len(links))
    return final_url, links


def process_links(
    page_links: Sequence[PageLink],
    final_url: str,
    config: Config,
    logger,
) -> LinkInventory:
    """Normalise, filter, de-duplicate and count the extracted links."""

    processor = UrlProcessor(
        base_url=final_url,
        ignore_patterns=config.ignore_patterns,
        check_external_links=config.check_external_links,
        logger=logger,
    )
    inventory = processor.build_inventory(
        [link.href for link in page_links],
        source_page=final_url,
        max_links=config.max_links,
    )
    logger.info("Unique links discovered: %s", inventory.discovered_unique)
    logger.info("Duplicate occurrences skipped: %s", inventory.duplicates)
    logger.info(
        "Links to check: %s (internal=%s, external=%s, skipped=%s)",
        inventory.unique_links_to_check,
        inventory.internal_count,
        inventory.external_count,
        len(inventory.skipped),
    )
    return inventory


def check_inventory(inventory: LinkInventory, config: Config, logger) -> List[LinkResult]:
    """Validate every unique link over HTTP and return them in page order."""

    if not inventory.urls:
        logger.warning("No checkable links found on the page")
        return inventory.merge([])
    with LinkChecker(config, logger) as checker:
        checked = checker.check_links(
            inventory.urls,
            source_page=inventory.source_page,
            occurrences=inventory.occurrences,
            link_types=inventory.link_types,
            max_workers=config.max_workers,
        )
    return inventory.merge(checked)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run(config: Config, logger=None) -> int:
    """Execute one complete run and return the process exit code."""

    logger = logger or get_logger(level=config.log_level, log_dir=config.logs_dir)
    started_at = datetime.now()
    config.prepare_directories()

    logger.info("=" * 62)
    logger.info("Broken link automation started")
    for line in config.describe():
        logger.info("Config: %s", line)
    logger.info("Log file: %s", config.log_file_path)

    page = WebPage(config, logger)
    try:
        final_url, page_links = collect_page_links(page, logger)
        inventory = process_links(page_links, final_url, config, logger)
        results = check_inventory(inventory, config, logger)

        stats = LinkStatistics.from_results(
            results,
            target_url=final_url,
            total_links_found=inventory.total_found,
            unique_links_discovered=inventory.discovered_unique,
            started_at=started_at,
            finished_at=datetime.now(),
        )

        generator = ReportGenerator(config, logger)
        report_paths = generator.generate(results, stats)

        if stats.broken:
            logger.warning("%s broken link(s) detected", stats.broken)
            if config.screenshot_on_failure:
                page.save_screenshot("broken_links_detected")

        generator.print_console_summary(stats, report_paths)
        logger.info("Evidence: CSV + HTML report written to %s", config.reports_dir)
        return EXIT_SUCCESS if stats.passed else EXIT_BROKEN_LINKS_FOUND

    except PageLoadError as error:
        logger.error("Page level failure: %s", error)
        if config.screenshot_on_failure:
            page.save_screenshot("page_load_failure")
        return EXIT_RUNTIME_ERROR
    except Exception as error:  # pragma: no cover - defensive
        logger.exception("Unexpected failure during the run: %s", error)
        if config.screenshot_on_failure:
            page.save_screenshot("unexpected_failure")
        return EXIT_RUNTIME_ERROR
    finally:
        page.close()
        logger.info(
            "Execution finished in %.2fs",
            (datetime.now() - started_at).total_seconds(),
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point: parse the flags, configure logging and run once."""

    args = parse_args(argv)
    config = config_from_args(args)
    logger = get_logger(level=config.log_level, log_dir=config.logs_dir)
    try:
        return run(config, logger)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        logger.warning("Interrupted by the user")
        return EXIT_RUNTIME_ERROR


if __name__ == "__main__":
    sys.exit(main())