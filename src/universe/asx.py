#!/usr/bin/env python3
"""ASX universe: every LIC and LIT in the current monthly Investment Products
report.

Built first, because it is the best-sourced leg of the project: one official
file gives identity, mandate, size, NTA and discount in one pass.
"""

from typing import List, Optional, Tuple

from .. import db, fetch
from ..collectors import asx_monthly
from ..util import utcnow_iso
from .common import compile_exclusions, should_exclude

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
    now = utcnow_iso()
    for rec in parsed.records:
        reason = should_exclude(rec.name or "", rec.mandate or "", patterns)
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
            "source": asx_monthly.SOURCE,
            "source_url": report_url,
            "source_status": doc.status,
            "retrieved_at": now,
        }
        db.upsert_fund(conn, row)
        if reason:
            stats["excluded"] += 1
        else:
            stats["kept"] += 1
    conn.commit()
    return stats


def archived_report_urls(fetcher, cfg) -> List[str]:
    """Links to prior months' reports, for the historical panel.

    Scraped from the landing pages rather than constructed, for the same
    reason as the current report. An empty list means the archive wasn't
    reachable — not that no archive exists.
    """
    from .common import find_links
    urls, seen = [], set()
    landings = list(cfg.get("sources.asx.monthly_report_landing"))
    landings += list(cfg.get("sources.asx.funds_statistics_landing", []))
    for landing in landings:
        page = fetcher.get(landing, kind="asx-archive-landing")
        if not page.ok:
            continue
        for u in find_links(page.text, landing, extensions=(".xlsx", ".xlsm", ".csv")):
            if u not in seen:
                seen.add(u)
                urls.append(u)
    limit = int(cfg.num("sources.asx.archive_months"))
    return urls[:limit]
