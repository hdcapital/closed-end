#!/usr/bin/env python3
"""ASX monthly Investment Products report -> uniform records.

Source: the landing page in the brief. The report is one spreadsheet carrying
every LIC and LIT with its code, name, mandate, market cap, NTA and last close.

The column mapping lives in `src/collectors/asx_monthly.py` and is pinned by
`tests/test_asx_report.py` against the header the live June 2026 file really
has — including the three traps that bit on the first live run ("Prem/Disc %
NTA (pre-tax)" masquerading as the NTA level, "NTA Price" masquerading as the
share price, and "Mkt Cap ($m) Change" sitting next to "Mkt Cap ($m)").
"""

from typing import List, Tuple

from ..collectors import asx_monthly
from ..universe.common import find_links
from .record import Record

EXCHANGE = "ASX"
CURRENCY = "AUD"
SOURCE = "asx-investment-products-monthly"

LANDINGS = [
    "https://www.asx.com.au/issuers/investment-products/asx-investment-products-monthly-report",
    "https://www2.asx.com.au/issuers/investment-products/asx-investment-products-monthly-report",
]


def find_report(fetcher) -> Tuple[str, str, str]:
    """(url, status, detail). The download link is scraped from the landing
    page rather than guessed: ASX has relocated this file more than once."""
    last = "skipped"
    for landing in LANDINGS:
        page = fetcher.get(landing, kind="asx-landing")
        last = page.status
        if not page.ok:
            continue
        links = find_links(page.text, landing, extensions=(".xlsx", ".xlsm"),
                           keywords=("investment", "product", "lic", "lit", "monthly"))
        links = links or find_links(page.text, landing, extensions=(".xlsx", ".xlsm"))
        if links:
            return links[0], "ok", f"discovered from {landing}"
    return None, last, "no spreadsheet link on any landing page"


def harvest(fetcher) -> Tuple[List[Record], dict]:
    info = {"source": SOURCE, "status": "skipped", "url": None,
            "rows": 0, "warnings": []}

    url, status, detail = find_report(fetcher)
    info["url"], info["status"] = url, status
    if not url:
        info["warnings"].append(detail)
        return [], info

    doc = fetcher.get(url, kind="asx-report")
    info["status"] = doc.status
    if not doc.ok:
        info["warnings"].append(f"download failed: {doc.detail or doc.status}")
        return [], info

    parsed = asx_monthly.parse(doc.content, filename=url)
    info["warnings"].extend(parsed.warnings)
    info["sheet"] = parsed.sheet

    out = []
    for rec in parsed.records:
        nta, _kind = rec.primary_nta
        out.append(Record(
            code=rec.ticker,
            exchange=EXCHANGE,
            name=rec.name,
            vehicle_type=rec.structure,          # LIC | LIT
            sector=asx_monthly.normalise_sector(rec.mandate, rec.name or ""),
            currency=CURRENCY,
            market_cap=rec.market_cap,           # already scaled from $m
            nta_per_share=nta,
            # The report quotes NTA in dollars per share and says so in the
            # column it comes from ("NTA Price"), alongside a Last Close in the
            # same unit — the two agreeing with the published discount is what
            # test_recomputed_discount_agrees_with_the_published_one checks.
            nta_unit="declared_major" if nta is not None else None,
            price=rec.price,
            discount=rec.premium_discount,
            nta_date=rec.nta_date,
            as_of=rec.nta_date,
            source=SOURCE,
            source_url=url,
        ))
    info["rows"] = len(out)
    return out, info
