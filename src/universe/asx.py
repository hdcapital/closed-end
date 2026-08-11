#!/usr/bin/env python3
"""ASX universe: every LIC and LIT in the current monthly Investment Products
report.

Built first, because it is the best-sourced leg of the project: one official
file gives identity, mandate, size, NTA and discount in one pass.
"""

import re
from typing import List, Optional, Tuple

from .. import db, fetch
from ..collectors import asx_monthly
from ..util import utcnow_iso
from .common import compile_exclusions, compile_ticker_exclusions, should_exclude

EXCHANGE = "ASX"
CURRENCY = "AUD"


def discover_report_url(fetcher, cfg) -> Tuple[Optional[str], str]:
    """Find the current monthly report download link from the landing page.

    Returns (url, status). We scrape the link rather than guessing a filename
    because ASX has relocated this file more than once (asx.com.au ->
    www2.asx.com.au, /documents/ -> /content/dam/).
    """
    from .common import find_links
    landings = cfg.get("sources.asx.monthly_report_landing")
    last_status = fetch.SKIPPED
    for landing in landings:
        page = fetcher.get(landing, kind="asx-monthly-landing")
        last_status = page.status
        if not page.ok:
            continue
        links = find_links(
            page.text, landing,
            extensions=(".xlsx", ".xlsm", ".csv"),
            keywords=("investment", "product", "lic", "lit", "nta", "monthly"),
        )
        if links:
            return links[0], fetch.OK
        # Some editions link the spreadsheet only from a PDF-first page;
        # fall back to any spreadsheet on the page.
        links = find_links(page.text, landing, extensions=(".xlsx", ".xlsm", ".csv"))
        if links:
            return links[0], fetch.OK
    return None, last_status


def build(conn, fetcher, cfg, report_url: str = None) -> dict:
    """Populate `funds` from the current ASX monthly report.

    Returns a stats dict. When the source can't be fetched, nothing is
    invented: the stats carry the failure status and the caller reports it.
    """
    stats = {"exchange": EXCHANGE, "fetched": 0, "kept": 0, "excluded": 0,
             "status": fetch.SKIPPED, "warnings": [], "url": report_url}

    if not report_url:
        report_url, status = discover_report_url(fetcher, cfg)
        if not report_url:
            stats["status"] = status
            stats["warnings"].append(
                "could not locate the ASX monthly report download link"
            )
            return stats
        stats["url"] = report_url

    doc = fetcher.get(report_url, kind="asx-monthly-report")
    stats["status"] = doc.status
    if not doc.ok:
        stats["warnings"].append(f"report fetch failed: {doc.detail or doc.status}")
        return stats

    parsed = asx_monthly.parse(doc.content, filename=report_url)
    stats["warnings"].extend(parsed.warnings)
    stats["fetched"] = len(parsed.records)

    patterns = compile_exclusions(cfg)
    ticker_patterns = compile_ticker_exclusions(cfg)
    now = utcnow_iso()
    for rec in parsed.records:
        reason = should_exclude(rec.name or "", rec.mandate or "", patterns,
                                ticker=rec.ticker, ticker_patterns=ticker_patterns)
        row = {
            "fund_id": db.fund_id(EXCHANGE, rec.ticker),
            "exchange": EXCHANGE,
            "ticker": rec.ticker,
            "isin": None,                    # not carried by this report
            "name": rec.name,
            "sector": asx_monthly.normalise_sector(rec.mandate, rec.name or ""),
            "sector_raw": rec.mandate,
            "currency": CURRENCY,
            "structure": rec.structure,
            "listing_date": rec.listing_date,
            "status": "excluded" if reason else "live",
            "status_reason": reason,
            "market_cap": rec.market_cap,
            "shares_on_issue": rec.shares_on_issue,
            # Newly collected from the report's MER / Outperf Fee columns.
            # Until the live run exposed these the fee haircut could never fire.
            "ocr": rec.ocr,
            "has_performance_fee": None if rec.has_performance_fee is None
                                   else int(rec.has_performance_fee),
            "source": asx_monthly.SOURCE,
            "source_url": report_url,
            "source_status": doc.status,
            "retrieved_at": now,
        }
        db.upsert_fund(conn, row)
        _store_stated(conn, row["fund_id"], rec, report_url, now)
        if reason:
            stats["excluded"] += 1
        else:
            stats["kept"] += 1
    conn.commit()
    return stats


def _store_stated(conn, fund_id: str, rec, url: str, now: str) -> None:
    """Manager-stated performance and yield, kept strictly apart from anything
    computed here. The report publishes 1/3/5-year total returns; they are the
    only performance figures available for a fund with too little archive
    history, and the screen must never present them as its own calculation."""
    from .. import db as _db
    for metric, value in (("stated_total_return_1y", rec.stated_r1y),
                          ("stated_total_return_3y", rec.stated_r3y),
                          ("stated_total_return_5y", rec.stated_r5y),
                          ("stated_distribution_yield", rec.dist_yield)):
        if value is not None:
            _db.put_metric(conn, fund_id, now[:10], metric, value,
                           provenance="stated",
                           detail=f"as published by ASX: {url}")


_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec"]

# The live current-report URL looks like
#   .../asx-investment-products-reports/2026/excel/asx-investment-products-jun-2026-abs.xlsx
# so both the year directory and the "mon-year" stem are substitutable.
_STEM_RE = re.compile(
    r"(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[-_](?P<year>20\d{2})",
    re.IGNORECASE,
)


def archive_url_template(url: str):
    """Turn a known report URL into a `(year, month) -> url` builder.

    The landing page only links about two years of monthly reports, and most of
    the spreadsheets it *does* link are the ETF and structured-product editions
    of the same file — they parse perfectly and contribute nothing, because
    their tickers are not LICs. Scraping alone therefore caps the panel at ~24
    months, which is below the model's own 5-year floor and would leave the
    screen permanently unrankable.

    So the archive is built by substituting into the pattern of a URL we have
    actually seen work, rather than by inventing one. Months that were never
    published simply 404 and are recorded as such.
    """
    m = _STEM_RE.search(url or "")
    if not m:
        return None
    stem_year = m.group("year")

    def build(year: int, month: int) -> str:
        out = url[:m.start()] + f"{_MONTHS[month - 1]}-{year}" + url[m.end():]
        # The path also carries a year directory; swap it when it matches the
        # year we are replacing, and leave anything else alone.
        return out.replace(f"/{stem_year}/", f"/{year}/")

    return build


def archived_report_urls(fetcher, cfg, current_url: str = None) -> List[str]:
    """URLs for the historical monthly panel, newest first.

    Two sources, deduped: whatever the landing pages link, plus URLs
    constructed from the pattern of the current report. An empty list means the
    archive wasn't reachable — not that no archive exists.
    """
    import datetime
    from .common import find_links

    urls, seen = [], set()

    def add(u):
        if u and u not in seen:
            seen.add(u)
            urls.append(u)

    if not current_url:
        current_url, _ = discover_report_url(fetcher, cfg)
    add(current_url)

    # Constructed history first: these are the files that actually carry LIC
    # rows for months the landing page no longer links.
    limit = int(cfg.num("sources.asx.archive_months"))
    build = archive_url_template(current_url) if current_url else None
    if build:
        today = datetime.date.today()
        year, month = today.year, today.month
        for _ in range(limit):
            month -= 1
            if month == 0:
                year, month = year - 1, 12
            add(build(year, month))

    # Then anything the landing pages link that we haven't already got.
    landings = list(cfg.get("sources.asx.monthly_report_landing"))
    landings += list(cfg.get("sources.asx.funds_statistics_landing", []))
    for landing in landings:
        page = fetcher.get(landing, kind="asx-archive-landing")
        if not page.ok:
            continue
        for u in find_links(page.text, landing, extensions=(".xlsx", ".xlsm", ".csv")):
            add(u)

    return urls[:max(limit, 1) + 24]
