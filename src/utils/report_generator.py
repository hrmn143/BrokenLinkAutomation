"""CSV and HTML dashboard generation.

* ``reports/broken_links.csv`` - machine readable, one row per unique link
* ``reports/report.html``      - self contained QA dashboard (no CDN and no
  frontend framework) with KPI cards, status filters, search and sorting
* the final console summary is printed by :meth:`ReportGenerator.print_console_summary`
"""

from __future__ import annotations

import csv
from html import escape
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from src.config.config import Config
from src.utils.link_checker import LinkResult, LinkStatistics
from src.utils.logger import get_logger

#: CSV column order (documented in the README as well).
CSV_COLUMNS: Tuple[str, ...] = (
    "source_page",
    "url",
    "link_type",
    "status",
    "status_code",
    "response_time",
    "occurrences",
    "final_url",
    "error",
    "checked_at",
)

#: status -> css class used by rows, chips and filter buttons
STATUS_CSS: Dict[str, str] = {
    "WORKING": "ok",
    "REDIRECT": "redirect",
    "BLOCKED": "blocked",
    "CLIENT_ERROR": "broken",
    "SERVER_ERROR": "broken",
    "TIMEOUT": "broken",
    "CONNECTION_ERROR": "broken",
    "SSL_ERROR": "broken",
    "INVALID": "broken",
    "SKIPPED": "skipped",
}


class ReportGenerator:
    """Builds the CSV file and the HTML dashboard for one run."""

    def __init__(self, config: Config, logger=None) -> None:
        self.config = config
        self.logger = logger or get_logger()
        self.reports_dir = Path(config.reports_dir)

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def generate(
        self,
        results: Iterable[LinkResult],
        stats: LinkStatistics,
    ) -> Dict[str, Path]:
        """Write both reports and return their paths."""

        self.reports_dir.mkdir(parents=True, exist_ok=True)
        rows = list(results)
        paths = {
            "csv": self.write_csv(rows),
            "html": self.write_html(rows, stats),
        }
        self.logger.info("Reports generated: %s", ", ".join(str(p) for p in paths.values()))
        return paths

    def write_csv(self, results: Iterable[LinkResult]) -> Path:
        """Write the flat CSV export (``utf-8-sig`` keeps Excel happy)."""

        path = Path(self.config.csv_report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS))
            writer.writeheader()
            for result in results:
                row = result.to_dict()
                writer.writerow({column: row.get(column, "") for column in CSV_COLUMNS})
                count += 1
        self.logger.info("CSV report written: %s (%s row(s))", path, count)
        return path

    def write_html(self, results: Iterable[LinkResult], stats: LinkStatistics) -> Path:
        """Write the HTML dashboard."""

        path = Path(self.config.html_report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render_html(list(results), stats), encoding="utf-8")
        self.logger.info("HTML dashboard written: %s", path)
        return path

    # ------------------------------------------------------------------
    # rendering
    # ------------------------------------------------------------------
    def render_html(self, results: List[LinkResult], stats: LinkStatistics) -> str:
        """Return the complete HTML document (also called directly by the tests).

        CSS and JavaScript are injected as variables, which keeps the document
        skeleton free of brace escaping problems.
        """

        rows = "\n".join(self._row(result) for result in results) or EMPTY_ROW
        return (
            '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
            '<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f"<title>Broken Link Report - {escape(stats.target_url)}</title>\n"
            f"<style>\n{CSS}\n</style>\n</head>\n<body>\n"
            f"{self._header(stats)}\n"
            f"{self._kpis(stats)}\n"
            f"{self._table(rows, stats)}\n"
            f"{self._footer(stats)}\n"
            f"<script>\n{JAVASCRIPT}\n</script>\n</body>\n</html>\n"
        )

    def _header(self, stats: LinkStatistics) -> str:
        outcome = "PASS" if stats.passed else "FAIL"
        outcome_class = "pass" if stats.passed else "fail"
        return (
            '<header class="topbar">\n'
            '  <div class="brand">\n'
            '    <span class="logo">QA</span>\n'
            "    <div>\n"
            "      <h1>Broken Link Detection Report</h1>\n"
            '      <p class="sub">Automated Web Broken Link Detection and Reporting System</p>\n'
            "    </div>\n"
            "  </div>\n"
            f'  <div class="outcome {outcome_class}">{outcome}</div>\n'
            "</header>\n"
            '<section class="meta">\n'
            f"  <div><span>Target website</span><strong>{escape(stats.target_url)}</strong></div>\n"
            "  <div><span>Execution date/time</span><strong>"
            f"{stats.started_at.strftime('%d %b %Y, %H:%M:%S')}</strong></div>\n"
            f"  <div><span>Total execution time</span><strong>{stats.duration_seconds:.2f} s</strong></div>\n"
            f"  <div><span>Success rate</span><strong>{stats.success_rate}%</strong></div>\n"
            f"  <div><span>Reachable (incl. redirects)</span><strong>{stats.reachable_rate}%</strong></div>\n"
            "</section>"
        )

    def _kpis(self, stats: LinkStatistics) -> str:
        cards = (
            ("total", "Total links found", stats.total_links_found),
            ("total", "Unique links discovered", stats.unique_links_discovered),
            ("total", "Unique links checked", stats.unique_links_checked),
            ("ok", "Working links", stats.working),
            ("broken", "Broken links", stats.broken),
            ("redirect", "Redirects", stats.redirects),
            ("warn", "Blocked / restricted", stats.blocked),
            ("skipped", "Skipped links", stats.skipped),
            ("broken", "Timeout errors", stats.timeouts),
            ("broken", "Connection errors", stats.connection_errors),
            ("total", "Internal links", stats.internal_links),
            ("total", "External links", stats.external_links),
        )
        items = "\n".join(
            f'    <div class="card {css}"><span class="value">{value}</span>'
            f'<span class="label">{label}</span></div>'
            for css, label, value in cards
        )
        return f'<section class="cards">\n{items}\n</section>'

    def _footer(self, stats: LinkStatistics) -> str:
        return (
            '<footer class="footer">\n'
            f"  <p>Generated by <strong>BrokenLinkAutomation</strong> on "
            f"{stats.finished_at.strftime('%d %b %Y, %H:%M:%S')} &middot; "
            f"duplicate occurrences skipped: {stats.duplicate_occurrences}</p>\n"
            "</footer>"
        )

    def _table(self, rows: str, stats: LinkStatistics) -> str:
        filters = (
            ("ok", "Working", stats.working),
            ("broken", "Broken", stats.broken),
            ("redirect", "Redirects", stats.redirects),
            ("blocked", "Blocked", stats.blocked),
            ("skipped", "Skipped", stats.skipped),
        )
        buttons = "\n".join(
            f'      <button type="button" class="filter {css}" data-filter="{css}">{label}'
            f' <span class="count">{count}</span></button>'
            for css, label, count in filters
        )
        headers = "".join(
            f'<th data-sort="{key}">{label}</th>' for key, label in TABLE_HEADERS
        )
        return (
            '<section class="panel">\n'
            '  <div class="toolbar">\n'
            '    <div class="filters" id="filters">\n'
            '      <button type="button" class="filter all active" data-filter="all">All '
            f'<span class="count">{len(stats.results)}</span></button>\n'
            f"{buttons}\n"
            "    </div>\n"
            '    <input id="search" type="search" autocomplete="off" '
            'placeholder="Search URL, status, error ...">\n'
            "  </div>\n"
            '  <div class="table-wrap">\n'
            '    <table id="links">\n'
            f"      <thead><tr>{headers}</tr></thead>\n"
            f"      <tbody>\n{rows}\n      </tbody>\n"
            "    </table>\n"
            "  </div>\n"
            f'  <p class="hint" id="visible">Showing {len(stats.results)} of '
            f"{len(stats.results)} link(s)</p>\n"
            "</section>"
        )

    def _row(self, result: LinkResult) -> str:
        css = STATUS_CSS.get(str(result.status), "broken")
        code = result.status_code if result.status_code is not None else "-"
        final_url = result.final_url if result.final_url != result.url else "-"
        time_taken = f"{result.response_time:.3f} s" if result.response_time else "-"
        return (
            f'    <tr class="{css}" data-status="{css}" '
            f'title="found on {escape(result.source_page)}">\n'
            f'      <td class="url"><a href="{escape(result.url)}" target="_blank" '
            f'rel="noreferrer">{escape(result.url)}</a></td>\n'
            f'      <td class="type">{escape(result.link_type)}</td>\n'
            f'      <td><span class="chip {css}">{escape(str(result.status))}</span></td>\n'
            f'      <td class="num">{code}</td>\n'
            f'      <td class="url final">{escape(final_url)}</td>\n'
            f'      <td class="num" data-value="{result.response_time:.3f}">{time_taken}</td>\n'
            f'      <td class="error">{escape(result.error or "")}</td>\n'
            f'      <td class="num" data-value="{result.occurrences}">{result.occurrences}</td>\n'
            "    </tr>"
        )

    # ------------------------------------------------------------------
    # console output
    # ------------------------------------------------------------------
    def print_console_summary(
        self,
        stats: LinkStatistics,
        report_paths: Dict[str, Path],
    ) -> None:
        """Print the final summary block (the one intentional ``print``)."""

        rule = "=" * 40
        lines = [
            rule,
            "BROKEN LINK AUTOMATION TEST",
            rule,
            "",
            f"Target:  {stats.target_url}",
            f"Started: {stats.started_at.strftime('%Y-%m-%d %H:%M:%S')}",
            "Mode:    Selenium extraction + requests validation",
            "",
            f"Total Links Found:    {stats.total_links_found}",
            f"Unique Links Checked: {stats.unique_links_checked}",
            f"Working:              {stats.working}",
            f"Broken:               {stats.broken}",
            f"Redirects:            {stats.redirects}",
            f"Skipped:              {stats.skipped}",
        ]
        if stats.blocked:
            lines.append(f"Blocked:              {stats.blocked}")
        if stats.timeouts:
            lines.append(f"Timeout Errors:       {stats.timeouts}")
        if stats.connection_errors:
            lines.append(f"Connection Errors:    {stats.connection_errors}")
        lines += [
            "",
            f"Success Rate: {stats.success_rate}%   (reachable incl. redirects: {stats.reachable_rate}%)",
            f"Execution Time: {stats.duration_seconds:.2f}s",
            "",
            "Broken links:",
        ]

        if stats.broken_results:
            for result in stats.broken_results[:SUMMARY_BROKEN_LIMIT]:
                code = result.status_code if result.status_code is not None else result.status
                lines.append(f"  - [{code}] {result.url} ({result.error})")
            remaining = len(stats.broken_results) - SUMMARY_BROKEN_LIMIT
            if remaining > 0:
                lines.append(f"  ... and {remaining} more broken link(s), see the report")
        else:
            lines.append("  - none, every link responded correctly")

        lines.append("")
        lines.append("Reports:")
        lines.extend(f"  {path}" for path in report_paths.values())
        lines.extend([rule, ""])
        print("\n".join(lines))  # noqa: T201 - final user facing output

        self.logger.info(
            "Run summary: found=%s checked=%s working=%s broken=%s redirects=%s "
            "skipped=%s success_rate=%s%% duration=%.2fs",
            stats.total_links_found,
            stats.unique_links_checked,
            stats.working,
            stats.broken,
            stats.redirects,
            stats.skipped,
            stats.success_rate,
            stats.duration_seconds,
        )


# ---------------------------------------------------------------------------
# Module level constants used while rendering
# ---------------------------------------------------------------------------
#: table columns: (sort key, header label)
TABLE_HEADERS: Tuple[Tuple[str, str], ...] = (
    ("url", "URL"),
    ("link_type", "Type"),
    ("status", "Status"),
    ("status_code", "HTTP Code"),
    ("final_url", "Final URL"),
    ("response_time", "Response Time"),
    ("error", "Error"),
    ("occurrences", "Occurrences"),
)

EMPTY_ROW = '<tr><td colspan="8" class="empty">No links were found on this page.</td></tr>'

#: how many broken links are echoed in the console summary
SUMMARY_BROKEN_LIMIT = 10

CSS = """
:root {
  --bg: #f4f6fb; --panel: #ffffff; --text: #1f2937; --muted: #6b7280; --line: #e5e7eb;
  --ok: #15803d; --ok-bg: #dcfce7; --broken: #b91c1c; --broken-bg: #fee2e2;
  --redirect: #b45309; --redirect-bg: #fef3c7; --blocked: #6d28d9; --blocked-bg: #ede9fe;
  --skipped: #475569; --skipped-bg: #e2e8f0;
}
* { box-sizing: border-box; }
body { margin: 0; padding: 24px; background: var(--bg); color: var(--text); font: 14px/1.5 "Segoe UI", Roboto, Arial, sans-serif; }
.topbar { display: flex; justify-content: space-between; align-items: center; gap: 16px;
  background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 18px 22px; }
.brand { display: flex; align-items: center; gap: 14px; }
.logo { width: 44px; height: 44px; border-radius: 12px; background: #1d4ed8; color: #fff;
  display: grid; place-items: center; font-weight: 700; letter-spacing: .5px; }
h1 { margin: 0; font-size: 20px; }
.sub { margin: 2px 0 0; color: var(--muted); font-size: 12px; }
.outcome { padding: 8px 18px; border-radius: 999px; font-weight: 700; letter-spacing: .6px; }
.outcome.pass { background: var(--ok-bg); color: var(--ok); }
.outcome.fail { background: var(--broken-bg); color: var(--broken); }
.meta { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin: 18px 0; }
.meta div { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 12px 16px; }
.meta span { display: block; color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .6px; }
.meta strong { display: block; margin-top: 6px; word-break: break-all; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }
.card { background: var(--panel); border: 1px solid var(--line); border-left: 4px solid var(--line);
  border-radius: 12px; padding: 14px 16px; }
.card .value { display: block; font-size: 26px; font-weight: 700; }
.card .label { display: block; color: var(--muted); font-size: 12px; margin-top: 4px; }
.card.ok { border-left-color: var(--ok); }
.card.broken { border-left-color: var(--broken); }
.card.redirect { border-left-color: var(--redirect); }
.card.warn { border-left-color: var(--blocked); }
.card.skipped { border-left-color: var(--skipped); }
.panel { margin-top: 18px; background: var(--panel); border: 1px solid var(--line); border-radius: 14px; overflow: hidden; }
.toolbar { display: flex; flex-wrap: wrap; gap: 12px; justify-content: space-between;
  align-items: center; padding: 16px; border-bottom: 1px solid var(--line); }
.filters { display: flex; flex-wrap: wrap; gap: 8px; }
.filter { border: 1px solid var(--line); background: #fff; color: var(--text);
  border-radius: 999px; padding: 7px 14px; cursor: pointer; font-size: 13px; }
.filter .count { color: var(--muted); font-size: 12px; }
.filter.active { border-color: #1d4ed8; background: #eff6ff; color: #1d4ed8; font-weight: 600; }
#search { min-width: 260px; padding: 9px 12px; border: 1px solid var(--line); border-radius: 10px; font-size: 13px; }
.table-wrap { overflow-x: auto; max-height: 70vh; }
table { border-collapse: collapse; width: 100%; }
th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--line); vertical-align: top; }
th { position: sticky; top: 0; background: #f8fafc; cursor: pointer; color: var(--muted);
  font-size: 12px; text-transform: uppercase; letter-spacing: .5px; white-space: nowrap; }
tbody tr:hover { background: #f9fafb; }
td.url { max-width: 420px; word-break: break-all; }
td.url a { color: #1d4ed8; text-decoration: none; }
td.url a:hover { text-decoration: underline; }
td.final { color: var(--muted); }
td.num { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
td.error { color: var(--broken); max-width: 260px; }
.chip { padding: 4px 10px; border-radius: 999px; font-size: 11px; font-weight: 700; white-space: nowrap; }
.chip.ok { background: var(--ok-bg); color: var(--ok); }
.chip.broken { background: var(--broken-bg); color: var(--broken); }
.chip.redirect { background: var(--redirect-bg); color: var(--redirect); }
.chip.blocked { background: var(--blocked-bg); color: var(--blocked); }
.chip.skipped { background: var(--skipped-bg); color: var(--skipped); }
.hint { margin: 0; padding: 12px 16px; color: var(--muted); font-size: 12px; }
.footer { margin-top: 18px; color: var(--muted); text-align: center; font-size: 12px; }
.empty { text-align: center; color: var(--muted); padding: 24px; }
"""

JAVASCRIPT = """
(function () {
  'use strict';
  var rows = Array.prototype.slice.call(document.querySelectorAll('#links tbody tr'));
  var buttons = Array.prototype.slice.call(document.querySelectorAll('#filters .filter'));
  var search = document.getElementById('search');
  var counter = document.getElementById('visible');
  var active = 'all';

  function refresh() {
    var term = (search && search.value ? search.value : '').trim().toLowerCase();
    var shown = 0;
    rows.forEach(function (row) {
      var statusOk = active === 'all' || row.getAttribute('data-status') === active;
      var textOk = !term || row.textContent.toLowerCase().indexOf(term) !== -1;
      var show = statusOk && textOk;
      row.style.display = show ? '' : 'none';
      if (show) { shown += 1; }
    });
    if (counter) {
      counter.textContent = 'Showing ' + shown + ' of ' + rows.length + ' link(s)';
    }
  }

  buttons.forEach(function (button) {
    button.addEventListener('click', function () {
      active = button.getAttribute('data-filter') || 'all';
      buttons.forEach(function (other) {
        other.classList.toggle('active', other === button);
      });
      refresh();
    });
  });

  if (search) { search.addEventListener('input', refresh); }

  // click a column header to sort by that column (numeric when possible)
  var headers = Array.prototype.slice.call(document.querySelectorAll('#links thead th'));
  headers.forEach(function (header, index) {
    header.addEventListener('click', function () {
      var body = document.querySelector('#links tbody');
      var ascending = header.getAttribute('data-order') !== 'asc';
      headers.forEach(function (other) { other.removeAttribute('data-order'); });
      header.setAttribute('data-order', ascending ? 'asc' : 'desc');
      var sorted = rows.slice().sort(function (a, b) {
        var cellA = a.cells ? a.cells[index] : null;
        var cellB = b.cells ? b.cells[index] : null;
        var rawA = cellA ? cellA.getAttribute('data-value') : null;
        var rawB = cellB ? cellB.getAttribute('data-value') : null;
        var numA = parseFloat(rawA);
        var numB = parseFloat(rawB);
        if (rawA !== null && rawB !== null && !isNaN(numA) && !isNaN(numB)) {
          return ascending ? numA - numB : numB - numA;
        }
        var textA = cellA ? cellA.textContent.trim().toLowerCase() : '';
        var textB = cellB ? cellB.textContent.trim().toLowerCase() : '';
        return ascending ? textA.localeCompare(textB) : textB.localeCompare(textA);
      });
      sorted.forEach(function (row) { body.appendChild(row); });
    });
  });

  refresh();
})();
"""