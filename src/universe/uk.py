#!/usr/bin/env python3
"""UK universe: LSE instrument list filtered to closed-ended investment funds,
cross-checked against the AIC where their terms allow it.

Primary is the LSE list because it is the exchange's own record and carries
ISINs, which are the dedupe key. The AIC list is the better *taxonomy* (its
sector scheme is what the market actually uses for peer groups), so it is used
to enrich sectors rather than to define membership.
"""

import re
from typing import List, Optional, Tuple

from .. import db, fetch, tabular
from ..util import parse_date, to_float, utcnow_iso
from .common import compile_exclusions, find_links, should_exclude

EXCHANGE = "LSE"
SOURCE = "lse-instrument-list"
AIC_SOURCE = "aic-member-list"

COLUMN_SPEC = {
    "ticker":     ["tidm", "mnemonic", "ticker", "epic", "symbol"],
    "name":       ["issuer name", "company name", "security name", "name", "issuer"],
    "isin":       ["isin", "isin code"],
    "sector":     ["icb sector", "ftse sector", "sector", "icb subsector",
                   "industry", "icb industry"],
    "icb_code":   ["icb code", "icb subsector code", "icb sub sector code"],
    "market":     ["market", "mkt", "segment", "market segment"],
    "currency":   ["currency", "trading currency", "ccy"],
    "listing_date": ["date of listing", "listing date", "admission date"],
    "market_cap": ["market cap", "market capitalisation", "market capitalization"],
    "country":    ["country of incorporation", "country"],
}

REQUIRED_HEADER_HINTS = ["isin"]

# An AIM-quoted investment company sits on a different market to a main-market
# closed-ended fund but is in scope for this screen.
_AIM_RE = re.compile(r"\baim\b", re.IGNORECASE)


def _is_closed_end(sector: str, icb_code: str, market: str, cfg) -> bool:
    """Membership test against the configured segment names and ICB codes."""
    names = [n.lower() for n in cfg.get("sources.uk.closed_end_segment_names")]
    codes = [str(c) for c in cfg.get("sources.uk.closed_end_icb_codes")]
    hay = f"{sector or ''} {market or ''}".lower()
    if any(n in hay for n in names):
        return True
    code = re.sub(r"\D", "", str(icb_code or ""))
    return bool(code) and any(code.startswith(c[:6]) for c in codes)


def discover_instrument_list(fetcher, cfg) -> Tuple[Optional[str], str]:
    landings = cfg.get("sources.uk.instrument_list_landing")
    last = fetch.SKIPPED
    for landing in landings:
        page = fetcher.get(landing, kind="lse-landing")
        last = page.status
        if not page.ok:
            continue
        links = find_links(
            page.text, landing, extensions=(".xlsx", ".xlsm", ".csv"),
            keywords=("issuer", "instrument", "list of", "companies", "securities"),
        )
        if links:
            return links[0], fetch.OK
    return None, last


def build(conn, fetcher, cfg, list_url: str = None) -> dict:
    stats = {"exchange": EXCHANGE, "fetched": 0, "kept": 0, "excluded": 0,
             "status": fetch.SKIPPED, "warnings": [], "url": list_url}

    candidates = [list_url] if list_url else []
    if not candidates:
        found, status = discover_instrument_list(fetcher, cfg)
        if found:
            candidates.append(found)
        candidates += list(cfg.get("sources.uk.instrument_list_fallbacks", []))
        if not candidates:
            stats["status"] = status
            stats["warnings"].append("could not locate the LSE instrument list")
            return stats

    doc = fetcher.get_first(candidates, kind="lse-instrument-list")
    stats["status"] = doc.status
    stats["url"] = doc.url
    if not doc.ok:
        stats["warnings"].append(f"instrument list fetch failed: {doc.detail or doc.status}")
        return stats

    try:
        sheets = tabular.read_sheets(doc.content, doc.url)
    except Exception as e:
        stats["status"] = fetch.PARSE_ERROR
        stats["warnings"].append(f"unreadable instrument list: {e}")
        return stats

    best = None
    for sheet_name, rows in sheets.items():
        idx = tabular.find_header(rows, REQUIRED_HEADER_HINTS)
        if idx is None:
            continue
        cmap = tabular.ColumnMap(rows[idx], COLUMN_SPEC)
        if not cmap.has("ticker") or not cmap.has("isin"):
            continue
        score = len(cmap.index)
        if best is None or score > best[0]:
            best = (score, sheet_name, rows, idx, cmap)

    if best is None:
        stats["status"] = fetch.PARSE_ERROR
        stats["warnings"].append(
            "no sheet with a recognisable instrument-list header. Workbook contains:\n"
            + tabular.describe(sheets))
        return stats

    _, sheet_name, rows, idx, cmap = best
    if cmap.missing:
        stats["warnings"].append(
            f"unmapped columns on '{sheet_name}': {', '.join(cmap.missing)}"
            f" | header seen: {tabular.header_row_text(cmap.raw_header)}")

    patterns = compile_exclusions(cfg)
    now = utcnow_iso()
    seen = set()
    for row in tabular.data_rows(rows, idx, cmap.index["ticker"]):
        ticker = str(cmap.get(row, "ticker") or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9.]{2,8}", ticker) or ticker in seen:
            continue
        sector = cmap.get(row, "sector")
        sector = str(sector).strip() if sector is not None else None
        market = cmap.get(row, "market")
        market = str(market).strip() if market is not None else None
        icb = cmap.get(row, "icb_code")

        stats["fetched"] += 1
        if not _is_closed_end(sector or "", icb, market or "", cfg):
            continue
        seen.add(ticker)

        name = cmap.get(row, "name")
        name = str(name).strip() if name is not None else None
        reason = should_exclude(name or "", f"{sector or ''} {market or ''}", patterns)
        d = parse_date(cmap.get(row, "listing_date"))
        isin = str(cmap.get(row, "isin") or "").strip().upper() or None

        db.upsert_fund(conn, {
            "fund_id": db.fund_id(EXCHANGE, ticker),
            "exchange": EXCHANGE,
            "ticker": ticker,
            "isin": isin,
            "name": name,
            "sector": normalise_sector(sector or "", name or ""),
            "sector_raw": sector,
            "currency": str(cmap.get(row, "currency") or "GBP").strip().upper()[:3] or "GBP",
            "structure": "aim_investment_company" if _AIM_RE.search(market or "")
                         else "investment_trust",
            "listing_date": d.isoformat() if d else None,
            "status": "excluded" if reason else "live",
            "status_reason": reason,
            "market_cap": to_float(cmap.get(row, "market_cap")),
            "source": SOURCE,
            "source_url": doc.url,
            "source_status": doc.status,
            "retrieved_at": now,
        })
        if reason:
            stats["excluded"] += 1
        else:
            stats["kept"] += 1
    conn.commit()
    return stats


# ---------------------------------------------------------------------------
# AIC cross-check
# ---------------------------------------------------------------------------

def cross_check_aic(conn, fetcher, cfg) -> dict:
    """Enrich sectors from the AIC list, and report funds each source misses.

    Deliberately conservative about scraping: the AIC is a membership body, not
    a public data provider. `http.respect_robots` is honoured by the fetcher, so
    if their robots.txt disallows this path we get ROBOTS_DENIED, record it, and
    fall back to LSE data alone — which is exactly what the spec asks for.
    """
    stats = {"status": fetch.SKIPPED, "matched": 0, "aic_only": 0,
             "warnings": [], "url": cfg.get("sources.uk.aic_landing")}
    page = fetcher.get(stats["url"], kind="aic-list")
    stats["status"] = page.status
    if page.status == fetch.ROBOTS_DENIED:
        stats["warnings"].append(
            "AIC robots.txt disallows this path — falling back to LSE data only, "
            "sectors left as published by LSE (see PROGRESS.md)"
        )
        return stats
    if not page.ok:
        stats["warnings"].append(f"AIC list unavailable: {page.detail or page.status}")
        return stats

    entries = _parse_aic(page.text)
    if not entries:
        stats["warnings"].append(
            "AIC page fetched but no fund rows parsed — the list is rendered "
            "client-side; a JSON endpoint or the AIC data licence is the fix"
        )
        return stats

    now = utcnow_iso()
    known = {r["ticker"]: r["fund_id"]
             for r in conn.execute("SELECT ticker, fund_id FROM funds WHERE exchange=?",
                                   (EXCHANGE,))}
    for e in entries:
        fid = known.get(e["ticker"])
        if not fid:
            stats["aic_only"] += 1
            continue
        conn.execute(
            "UPDATE funds SET sector=?, sector_raw=COALESCE(?, sector_raw), "
            "source_url=COALESCE(source_url, ?), retrieved_at=? WHERE fund_id=?",
            (normalise_sector(e["sector"], e["name"]), e["sector"], stats["url"], now, fid),
        )
        stats["matched"] += 1
    conn.commit()
    return stats


def _parse_aic(html: str) -> List[dict]:
    """Pull (ticker, name, sector) triples out of the AIC list page.

    Returns [] when the page is a client-side shell — which the caller reports
    honestly rather than papering over.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    out = []
    for tr in soup.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        if len(cells) < 2:
            continue
        ticker = next((c.upper() for c in cells
                       if re.fullmatch(r"[A-Z]{2,5}", c.strip().upper())), None)
        if not ticker:
            continue
        name = max((c for c in cells if c != ticker), key=len, default="")
        sector = next((c for c in cells if "sector" in c.lower()), "")
        out.append({"ticker": ticker, "name": name, "sector": sector})
    return out


# AIC-style sector strings -> the taxonomy the priors are keyed on.
_UK_SECTOR_PATTERNS = [
    ("private_equity",    r"private equity|growth capital|venture"),
    ("property",          r"propert|real estate"),
    ("infrastructure",    r"infrastructure|renewable|energy transition|utilit"),
    ("debt",              r"debt|credit|loan|bond|mortgage|leasing|royalt"),
    ("hedge_multi_asset", r"hedge|absolute return|multi[- ]asset|flexible investment|"
                          r"alternativ|diversified"),
    ("small_cap_equity",  r"smaller compan|small cap|micro cap"),
    ("equity",            r"equit|income|growth|global|uk |north america|europe|"
                          r"asia|japan|emerging|investment trust|closed end"),
]


def normalise_sector(sector_raw: str, name: str = "") -> str:
    text = f"{sector_raw or ''} {name or ''}".lower()
    if not text.strip():
        return "unknown"
    for sector, pattern in _UK_SECTOR_PATTERNS:
        if re.search(pattern, text):
            return sector
    return "unknown"
