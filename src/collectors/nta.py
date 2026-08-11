#!/usr/bin/env python3
"""Build the NTA panel, cheapest layer first.

1. **ASX archived monthly reports.** One official file per month gives the
   whole LIC/LIT universe an NTA and a discount for that month. This is by far
   the best return on effort in the project: ~120 files for ~10 years of
   monthly history across the entire ASX universe.
2. **Per-fund NTA announcements.** Deeper and verifiable, read from the
   sibling lake where available. Both pre- and post-tax NTA are stored; pre-tax
   is the primary series and the report says so.
3. **UK NAV.** RNS announcements for a priority subset, plus stated
   performance figures where the raw series is unobtainable — tagged
   `stated` so it can never be mixed with a computed one.

Nothing here fabricates. A fund with no reachable NTA gets a NULL observation
carrying the reason, and shows up in the report's data-quality appendix.
"""

from typing import Dict, List, Optional

from .. import db, fetch
from ..util import parse_date, today_utc, utcnow_iso
from . import asx_monthly, nta_text
from .lake import HOLDER_TITLE_RE, NTA_TITLE_RE, LakeReader, date_range


def from_asx_archive(conn, fetcher, cfg, urls: List[str]) -> dict:
    """Parse archived monthly reports into a monthly NTA + discount panel."""
    stats = {"reports": 0, "failed": 0, "nta_rows": 0, "price_rows": 0,
             "warnings": [], "months": []}

    for url in urls:
        doc = fetcher.get(url, kind="asx-monthly-archive")
        if not doc.ok:
            stats["failed"] += 1
            db.log_source(conn, url=url, kind="asx-monthly-archive",
                          status=doc.status, detail=doc.detail)
            continue

        as_of = _month_from_url(url)
        parsed = asx_monthly.parse(doc.content, filename=url, as_of=as_of)
        if not parsed.records:
            stats["failed"] += 1
            stats["warnings"].append(f"{url}: {'; '.join(parsed.warnings) or 'no records'}")
            continue

        stats["reports"] += 1
        if as_of:
            stats["months"].append(as_of)
        now = utcnow_iso()
        nta_rows, price_rows = [], []

        for rec in parsed.records:
            fid = db.fund_id("ASX", rec.ticker)
            obs_date = rec.nta_date or as_of
            if not obs_date:
                # Without a date the observation cannot be placed in a series;
                # dropping it beats guessing one and smearing the panel.
                continue
            for value, ntype in ((rec.nta_pre_tax, "pre_tax"),
                                 (rec.nta_post_tax, "post_tax"),
                                 (rec.nta_unspecified, "unspecified")):
                if value is None:
                    continue
                nta_rows.append({
                    "fund_id": fid, "date": obs_date, "nta_per_share": value,
                    "nta_type": ntype, "currency": "AUD",
                    "source": asx_monthly.SOURCE, "source_url": url,
                    "source_status": doc.status, "retrieved_at": now,
                })
            # The report's own share price is stored as a second price source.
            # It is month-end and matches the NTA date exactly, which makes it
            # a cleaner discount input than a daily feed for historical months.
            if rec.price is not None:
                price_rows.append({
                    "fund_id": fid, "date": obs_date, "close": rec.price,
                    "currency": "AUD", "volume": None, "dividend": None,
                    "source": asx_monthly.SOURCE, "source_url": url,
                    "source_status": doc.status, "retrieved_at": now,
                })

        # Only store observations for funds already in the universe: the
        # foreign key is what stops a typo'd ticker inventing a fund.
        known = _known_fund_ids(conn)
        nta_rows = [r for r in nta_rows if r["fund_id"] in known]
        price_rows = [r for r in price_rows if r["fund_id"] in known]
        stats["nta_rows"] += db.insert_nta(conn, nta_rows)
        stats["price_rows"] += db.insert_prices(conn, price_rows)
        conn.commit()

    return stats


def _month_from_url(url: str) -> Optional[str]:
    """Infer the report month from the filename, e.g. ".../2026-07-lic.xlsx"
    or ".../July-2026-Investment-Products.xlsx"."""
    import re
    from ..util import month_end
    m = re.search(r"(20\d{2})[-_ ]?(0[1-9]|1[0-2])\b", url)
    if m:
        return month_end(int(m.group(1)), int(m.group(2))).isoformat()
    m = re.search(
        r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[-_ ]?(20\d{2})",
        url, re.IGNORECASE)
    if m:
        months = ["jan", "feb", "mar", "apr", "may", "jun",
                  "jul", "aug", "sep", "oct", "nov", "dec"]
        return month_end(int(m.group(2)), months.index(m.group(1)[:3].lower()) + 1).isoformat()
    return None


def _known_fund_ids(conn) -> set:
    return {r["fund_id"] for r in conn.execute("SELECT fund_id FROM funds")}


# ---------------------------------------------------------------------------
# Per-fund NTA announcements, read from the sibling lake
# ---------------------------------------------------------------------------

def from_lake(conn, cfg, market: str, start: str, end: str,
              reader: LakeReader = None) -> dict:
    """NTA observations parsed out of announcements already in the lake."""
    stats = {"market": market, "documents": 0, "parsed": 0, "rows": 0,
             "unparsed": 0, "status": "unavailable", "warnings": []}

    reader = reader or LakeReader()
    if not reader.status.available:
        stats["warnings"].append(f"lake unavailable: {reader.status.reason}")
        return stats
    stats["status"] = "ok"

    exchange = {"asx": "ASX", "uk": "LSE"}.get(market, market.upper())
    known = {r["ticker"]: r["fund_id"] for r in
             conn.execute("SELECT ticker, fund_id FROM funds WHERE exchange=?", (exchange,))}
    if not known:
        stats["warnings"].append(f"no {exchange} funds in the universe yet")
        return stats

    docs = reader.scan(market, date_range(start, end), NTA_TITLE_RE, set(known))
    stats["documents"] = len(docs)
    stats["warnings"].extend(reader.status.warnings)
    if reader.status.days_missing_marker:
        stats["warnings"].append(
            f"{reader.status.days_missing_marker} day(s) had no done-marker and were "
            "skipped — absent marker does not mean an empty day"
        )

    now = utcnow_iso()
    rows = []
    for doc in docs:
        ticker = ((doc.get("company") or {}).get("ticker") or "").upper()
        fid = known.get(ticker)
        if not fid:
            continue
        text = (doc.get("content") or {}).get("text") or ""
        ex = nta_text.extract(text)
        obs_date = nta_text.extract_as_at(text) or doc.get("published_date")
        if not ex.ok or not obs_date:
            stats["unparsed"] += 1
            continue
        stats["parsed"] += 1
        for ntype, value in ex.values.items():
            rows.append({
                "fund_id": fid, "date": obs_date, "nta_per_share": value,
                "nta_type": ntype,
                "currency": "AUD" if exchange == "ASX" else "GBP",
                "source": f"lake:{market}-announcement",
                "source_url": doc.get("url"),
                "source_status": "ok", "retrieved_at": now,
            })
    stats["rows"] = db.insert_nta(conn, rows)
    conn.commit()
    return stats


def record_missing(conn, fund_ids: List[str], reason: str,
                   source: str = "collector") -> int:
    """Write an explicit NULL observation for funds we could not source.

    This is the difference between "we looked and found nothing" and "we never
    looked", and the report distinguishes them.
    """
    now = utcnow_iso()
    rows = [{
        "fund_id": fid, "date": today_utc(), "nta_per_share": None,
        "nta_type": "unspecified", "currency": None, "source": source,
        "source_url": None, "source_status": reason, "retrieved_at": now,
    } for fid in fund_ids]
    n = db.insert_nta(conn, rows)
    conn.commit()
    return n
