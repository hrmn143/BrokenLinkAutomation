"""Selenium page object: opens the target website and extracts its anchors.

Only this module talks to Selenium, and only one browser session is used for
the whole run - a new browser per link would be unusable in practice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Union

from selenium.common.exceptions import (
    SessionNotCreatedException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from src.config.config import VALID_BROWSERS, Config
from src.utils.logger import get_logger


class PageLoadError(RuntimeError):
    """Raised when the target page cannot be opened or read."""


@dataclass
class PageLink:
    """A single ``<a>`` element found on the page."""

    href: str
    text: str = ""
    index: int = 0
    visible: bool = True

    def __str__(self) -> str:
        return self.href or "(empty href)"


def build_browser_options(
    browser: str = "chrome",
    headless: bool = True,
    window_size: str = "1920,1080",
    user_agent: str = "",
) -> Any:
    """Create the browser options object without launching a browser."""

    name = (browser or "chrome").strip().lower()
    if name == "chrome":
        from selenium.webdriver.chrome.options import Options as ChromeOptions

        options = ChromeOptions()
        if headless:
            options.add_argument("--headless=new")
    elif name == "firefox":
        from selenium.webdriver.firefox.options import Options as FirefoxOptions

        options = FirefoxOptions()
        if headless:
            options.add_argument("--headless")
    elif name == "edge":
        from selenium.webdriver.edge.options import Options as EdgeOptions

        options = EdgeOptions()
        if headless:
            options.add_argument("--headless=new")
    else:
        raise PageLoadError(
            f"Unsupported browser '{browser}'. Supported: {', '.join(VALID_BROWSERS)}"
        )

    options.add_argument(f"--window-size={window_size}")
    options.add_argument("--disable-notifications")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    if user_agent:
        options.add_argument(f"--user-agent={user_agent}")
    if headless and name in {"chrome", "edge"}:
        options.add_argument("--disable-gpu")
    return options


def _create_driver(browser: str, options: Any) -> Any:
    """Instantiate the webdriver (Selenium Manager resolves the binary)."""

    name = (browser or "chrome").strip().lower()
    if name == "chrome":
        from selenium import webdriver

        return webdriver.Chrome(options=options)
    if name == "firefox":
        from selenium import webdriver

        return webdriver.Firefox(options=options)
    if name == "edge":
        from selenium import webdriver

        return webdriver.Edge(options=options)
    raise PageLoadError(f"Unsupported browser '{browser}'")


class WebPage:
    """Owns one browser session for the whole run and extracts its anchors.

    The browser is started lazily, reused for the page extraction step and
    always closed through :meth:`close` (the class is also a context manager, so
    the browser is released even when an exception is raised).
    """

    def __init__(self, config: Config, logger=None) -> None:
        self.config = config
        self.logger = logger or get_logger()
        self._driver: Any = None
        self._current_url = ""

    # -- browser lifecycle ------------------------------------------------
    @property
    def driver(self) -> Any:
        if self._driver is None:
            raise PageLoadError("Browser has not been started - call start_browser() first")
        return self._driver

    @property
    def is_open(self) -> bool:
        return self._driver is not None

    def start_browser(self) -> "WebPage":
        """Launch the configured browser once and set its timeouts."""

        if self._driver is not None:
            return self
        browser = (self.config.browser or "chrome").strip().lower()
        self.logger.info(
            "Starting %s browser (headless=%s, window=%s)",
            browser,
            self.config.headless,
            self.config.window_size,
        )
        options = build_browser_options(
            browser=browser,
            headless=self.config.headless,
            window_size=self.config.window_size,
            user_agent=self.config.user_agent,
        )
        try:
            self._driver = _create_driver(browser, options)
        except SessionNotCreatedException as error:
            raise PageLoadError(
                f"Could not start {browser}: {error}. Is the browser installed?"
            ) from error
        except WebDriverException as error:
            raise PageLoadError(f"WebDriver error while starting {browser}: {error}") from error
        self._driver.set_page_load_timeout(self.config.page_load_timeout)
        self.logger.debug("Browser started successfully")
        return self

    def close(self) -> None:
        """Quit the browser; never raises, so it is safe inside ``finally``."""

        if self._driver is None:
            return
        try:
            self._driver.quit()
            self.logger.debug("Browser closed")
        except WebDriverException as error:  # pragma: no cover - browser side
            self.logger.warning("Error while closing the browser: %s", error)
        finally:
            self._driver = None

    def __enter__(self) -> "WebPage":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    # -- navigation -------------------------------------------------------
    def open_page(self, url: Optional[str] = None) -> str:
        """Open ``url`` (default: the configured target) and wait until ready.

        Returns the final URL - relative links must be resolved against it
        because a redirect can land on a different host or path.
        """

        target = (url or self.config.target_url or "").strip()
        if not target:
            raise PageLoadError("No target URL configured (Config.target_url is empty)")

        self.start_browser()
        self.logger.info("Navigating to %s", target)
        try:
            self.driver.get(target)
        except TimeoutException as error:
            raise PageLoadError(
                f"Page load timed out after {self.config.page_load_timeout}s: {target}"
            ) from error
        except WebDriverException as error:
            message = getattr(error, "msg", None) or str(error)
            raise PageLoadError(f"Could not open {target}: {message}") from error

        self.wait_for_page_ready()
        self._current_url = self.driver.current_url or target
        self.logger.info("Page loaded: %s (title=%r)", self._current_url, self.title)
        return self._current_url

    def wait_for_page_ready(self, timeout: Optional[int] = None) -> None:
        """Explicit wait: JavaScript finished loading and a ``<body>`` exists."""

        wait_timeout = timeout or self.config.element_timeout
        wait = WebDriverWait(self.driver, wait_timeout)
        try:
            wait.until(
                lambda driver: driver.execute_script("return document.readyState") == "complete"
            )
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        except TimeoutException as error:
            raise PageLoadError(
                f"Page did not become ready within {wait_timeout}s (document.readyState)"
            ) from error

    @property
    def title(self) -> str:
        try:
            return (self.driver.title or "").strip()
        except WebDriverException:  # pragma: no cover - browser side
            return ""

    @property
    def current_url(self) -> str:
        if self.is_open:
            try:
                return self.driver.current_url or self._current_url
            except WebDriverException:  # pragma: no cover - browser side
                return self._current_url
        return self._current_url

    # -- extraction -------------------------------------------------------
    def get_links(self, max_links: int = 0) -> List[PageLink]:
        """Return every ``<a>`` element of the current page as :class:`PageLink`."""

        self.start_browser()
        self.logger.info("Extracting anchor elements ...")
        try:
            elements = self.driver.find_elements(By.TAG_NAME, "a")
        except WebDriverException as error:
            raise PageLoadError(f"Could not read the anchors of the page: {error}") from error

        links: List[PageLink] = []
        for index, element in enumerate(elements):
            if max_links and len(links) >= max_links:
                self.logger.warning("Stopped extracting after max_links=%s", max_links)
                break
            try:
                href = element.get_attribute("href") or ""
            except WebDriverException:  # pragma: no cover - stale element
                href = ""
            try:
                visible = element.is_displayed()
            except WebDriverException:  # pragma: no cover - stale element
                visible = True
            links.append(
                PageLink(
                    href=href,
                    text=(element.text or "").strip(),
                    index=index,
                    visible=visible,
                )
            )

        self.logger.info("Found %s anchor element(s)", len(links))
        return links

    # -- evidence ---------------------------------------------------------
    def save_screenshot(
        self,
        label: str,
        directory: Optional[Union[str, Path]] = None,
    ) -> Optional[Path]:
        """Save a PNG screenshot into ``screenshots/`` (used on failures only)."""

        if self._driver is None:
            self.logger.debug("Skipping screenshot '%s': no browser session", label)
            return None

        target_dir = Path(directory or self.config.screenshots_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_label = re.sub(r"[^A-Za-z0-9_-]+", "_", label).strip("_") or "failure"
        path = target_dir / f"{stamp}_{safe_label}.png"
        try:
            saved = self._driver.save_screenshot(str(path))
        except WebDriverException as error:
            self.logger.error("Could not save a screenshot: %s", error)
            return None
        if not saved:  # pragma: no cover - driver specific
            self.logger.warning("Screenshot was not written: %s", path)
            return None
        self.logger.info("Screenshot saved: %s", path)
        return path
