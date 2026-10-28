#!/usr/bin/env python
"""Legacy entry point - kept for backwards compatibility.

This file used to be the whole project (a ~20 line Selenium script that only
printed link URLs).  It is now a thin shim, because the real, tested
implementation lives in ``run.py`` and ``src/``.

Both entries behave identically and accept the same flags::

    python run.py --url https://example.com
    python broken_link_checker.py --url https://example.com
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from run import main  # noqa: E402 - sys.path must be fixed before importing

if __name__ == "__main__":
    sys.exit(main())
