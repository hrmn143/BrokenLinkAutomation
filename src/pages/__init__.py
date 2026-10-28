"""Page objects: the Selenium layer that turns a website into data."""

from src.pages.webpage import PageLink, PageLoadError, WebPage

__all__ = ["WebPage", "PageLink", "PageLoadError"]
