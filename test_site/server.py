#!/usr/bin/env python
"""Local demo website for the *Automated Web Broken Link Detection* framework.

It is a deliberately imperfect little site - one working page, one redirect
endpoint and real 404 responses - so the detector can be demonstrated and
validated without touching the internet or anyone's production website.

Start it with::

    python test_site/server.py                 # http://localhost:8000
    python test_site/server.py --port 9000     # another port
    python test_site/server.py --port 0        # random free port

Only the Python standard library is used on purpose: a demo site must not add
dependencies to the framework.

Routes
------
``/``                 -> ``index.html``               (200 OK)
``/about.html``       -> ``about.html``               (200 OK)
``/redirect``         -> 302 redirect to ``/about.html``
``/does-not-exist``   -> 404 Not Found (on purpose)
anything else         -> 404 Not Found
"""

from __future__ import annotations

import argparse
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlsplit

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
SITE_DIR = Path(__file__).resolve().parent

REDIRECT_PATH = "/redirect"
REDIRECT_TARGET = "/about.html"
REDIRECT_STATUS = 302

#: source files of the demo must never be downloadable
BLOCKED_SUFFIXES = (".py", ".pyc", ".pyo")

#: BaseHTTPRequestHandler fills %(code)d / %(message)s / %(explain)s
NOT_FOUND_PAGE = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>%(code)d Not Found</title></head>
<body style="font-family:'Segoe UI',Arial,sans-serif;padding:40px;max-width:640px">
  <h1>%(code)d - %(message)s</h1>
  <p style="color:#6b7280">
    This URL is missing on purpose: it is the broken link of the demo page and the
    detector must report it as <strong>CLIENT_ERROR / %(code)d</strong>.
  </p>
  <p><a href="/">Back to the test page</a></p>
</body>
</html>
"""


class TestSiteHandler(SimpleHTTPRequestHandler):
    """Static file handler plus the ``/redirect`` endpoint and a friendly 404."""

    server_version = "BrokenLinkDemoSite/1.0"
    error_message_format = NOT_FOUND_PAGE

    # -- routing ---------------------------------------------------------
    @staticmethod
    def _path_of(raw_path: str) -> str:
        """Path without query string and without a trailing slash."""

        return urlsplit(raw_path).path.rstrip("/") or "/"

    def _is_redirect(self) -> bool:
        return self._path_of(self.path) == REDIRECT_PATH

    def _is_blocked(self) -> bool:
        path = self._path_of(self.path).lower()
        return path.endswith(BLOCKED_SUFFIXES) or "__pycache__" in path

    def _send_redirect(self) -> None:
        """Answer ``/redirect`` with a 302 to ``/about.html``."""

        self.log_message("redirecting %s -> %s (%s)", self.path, REDIRECT_TARGET, REDIRECT_STATUS)
        body = (
            '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            "<title>302 Found</title></head><body>"
            f'<p>Redirecting to <a href="{REDIRECT_TARGET}">{REDIRECT_TARGET}</a></p>'
            "</body></html>"
        ).encode("utf-8")
        self.send_response(REDIRECT_STATUS)
        self.send_header("Location", REDIRECT_TARGET)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _dispatch(self) -> None:
        if self._is_redirect():
            self._send_redirect()
            return
        if self._is_blocked():
            self.send_error(403, "Not part of the demo site")
            return
        if self.command == "HEAD":
            return SimpleHTTPRequestHandler.do_HEAD(self)
        return SimpleHTTPRequestHandler.do_GET(self)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._dispatch()

    def do_HEAD(self) -> None:  # noqa: N802 - http.server API
        self._dispatch()

    # -- logging ---------------------------------------------------------
    def log_message(self, fmt: str, *args: object) -> None:
        """One readable line per request instead of the default stderr noise."""

        sys.stdout.write(f"[test_site] {self.address_string()} {fmt % args}\n")
        sys.stdout.flush()


def build_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    site_dir: Path = SITE_DIR,
    server_class: type = ThreadingHTTPServer,
) -> ThreadingHTTPServer:
    """Create (but do not start) the demo server.

    ``site_dir`` and ``server_class`` are injectable so the test suite can serve
    the very same pages on a random port without touching this file.
    """

    handler = partial(TestSiteHandler, directory=str(site_dir))
    return server_class((host, port), handler)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="test_site/server.py",
        description="Serve the local BrokenLinkAutomation demo website (stdlib only).",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"interface to bind (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"TCP port, 0 picks a free port (default: {DEFAULT_PORT})",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Start the demo server and print a clear banner with the URLs."""

    args = build_arg_parser().parse_args(argv)
    try:
        server = build_server(args.host, args.port)
    except OSError as error:
        print(
            f"Could not start the demo server on {args.host}:{args.port} -> {error}",
            file=sys.stderr,
        )
        print(
            "Hint: is the port already in use? Try "
            "python test_site/server.py --port 8001",
            file=sys.stderr,
        )
        return 1

    host, port = server.server_address[0], server.server_address[1]
    banner = "=" * 64
    print(banner)
    print("Broken Link Automation - local demo website")
    print(banner)
    print(f"Serving files from : {SITE_DIR}")
    print(f"Open in Chrome     : http://localhost:{port}")
    print(f"                     http://{host}:{port}")
    print("Routes             : /               -> index.html   (200 OK)")
    print("                     /about.html     -> about.html   (200 OK)")
    print("                     /redirect       -> 302 -> /about.html")
    print("                     /does-not-exist -> 404 Not Found (on purpose)")
    print("")
    print(f"Scan it with       : python run.py --url http://localhost:{port}")
    print("Stop the server    : Ctrl+C")
    print(banner)
    sys.stdout.flush()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[test_site] stopped by the user")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
