#!/usr/bin/env python3
"""AIC industry overview -> uniform records.

Source: the statistics page in the brief. The AIC publishes a monthly Excel
snapshot of investment company assets broken down by individual company, on or
around the sixth working day for the prior month end. That file — not the fund
finder — is the parseable one: the finder renders client-side and yields
nothing, which is why earlier attempts at the UK leg came back empty.

Written blind (this session has no egress to theaic.co.uk), so it is tolerant
and self-describing: it locates the header by content, fuzzy-maps columns, and
when it cannot find what it needs it prints the sheets and rows it actually saw
rather than just failing. That pattern is what turned every previous ASX
mis-parse from a mystery into a one-line fix.
"""

import re
from typing import List, Tuple

from .. import tabular
from ..universe.common import find_links
from ..util import to_float
from .record import Record, nta_from, unit_multiplier

EXCHANGE = "LSE"
CURRENCY = "GBP"
SOURCE = "aic-industry-overview"

LANDING = "https://www.theaic.co.uk/aic/statistics/industry-overview"

# A sheet is the per-company table if it names a company and at least one of
# the two figures we need. Sector and manager breakdowns lack a company column.
REQUIRED_HEADER_HINTS = ["company"]

COLUMN_SPEC = {
    "code":        {"match": ["tidm", "ticker", "epic", "code", "sedol"],
                    "not": ["sector", "isin"]},
    "isin":        ["isin"],
    "listing":     {"match": ["listing"], "not": ["date"]},
    "name":        ["company name", "investment company", "company", "name"],
    "sector":      {"match": ["aic sector", "sector"], "not": ["code"]},
    "manager":     ["management group", "asset manager", "manager"],
    "market_cap":  {"match": ["market cap", "market capitalisation",
                              "market capitalization", "mkt cap"],
                    "not": ["change", "%"]},
    # The AIC's total-assets column is the fund's asset figure; paired with
    # market cap it gives the discount directly. See note on gearing in run.py.
    "nta_total":   {"match": ["total assets", "total net assets", "net assets"],
                    "not": ["change", "%", "per share"]},
    "nav":         {"match": ["nav per share", "net asset value per share",
                              "nav (p)", "nav pence", "nav"],
                    "not": ["change", "%", "total", "return", "discount"]},
    "price":       {"match": ["share price", "price"],
                    "not": ["nav", "change", "%", "return"]},
    "discount":    ["discount premium", "premium discount", "discount", "premium"],
}

# VCTs are closed-ended and belong in the set, but they are a different animal
# from a conventional trust and anyone screening will want to filter them.
_VCT_RE = re.compile(r"\bVCT\b|venture capital trust", re.IGNORECASE)


def find_file(fetcher) -> Tuple[str, str]:
    page = fetcher.get(LANDING, kind="aic-landing")
    if not page.ok:
        return None, page.status
    links = find_links(page.text, LANDING, extensions=(".xlsx", ".xlsm", ".xls", ".csv"),
                       keywords=("industry", "overview", "asset", "monthly", "company"))
    links = links or find_links(page.text, LANDING,
                                extensions=(".xlsx", ".xlsm", ".xls", ".csv"))
    return (links[0] if links else None), page.status


def _pct(value):
    """Discount as a signed fraction, negative = discount."""
    v = to_float(value)
    if v is None:
        return None
    return v / 100.0 if abs(v) > 1.0 else v


def harvest(fetcher) -> Tuple[List[Record], dict]:
    info = {"source": SOURCE, "status": "skipped", "url": None,
            "rows": 0, "warnings": []}

    url, status = find_file(fetcher)
    info["status"] = status
    if not url:
        info["warnings"].append(
            f"no downloadable file linked from {LANDING} (landing status {status}) — "
            "if the page fetched but linked nothing, the file is behind a login "
            "or rendered client-side")
        return [], info
    info["url"] = url

    doc = fetcher.get(url, kind="aic-industry-overview")
    info["status"] = doc.status
    if not doc.ok:
        info["warnings"].append(f"download failed: {doc.detail or doc.status}")
        return [], info

    try:
        sheets = tabular.read_sheets(doc.content, url)
    except Exception as e:
        info["status"] = "parse_error"
        info["warnings"].append(f"unreadable: {e}")
        return [], info

    best = None
    for name, rows in sheets.items():
        idx = tabular.find_header(rows, REQUIRED_HEADER_HINTS, max_scan=25)
        if idx is None:
            continue
        cmap = tabular.ColumnMap(rows[idx], COLUMN_SPEC)
        if not cmap.has("name"):
            continue
        score = sum(cmap.has(f) for f in ("code", "market_cap", "nav", "nta_total"))
        if best is None or score > best[0]:
            best = (score, name, rows, idx, cmap)

    if best is None:
        info["status"] = "parse_error"
        info["warnings"].append(
            "no per-company sheet found. Workbook contains:\n" + tabular.describe(sheets))
        return [], info

    _, sheet, rows, idx, cmap = best
    info["sheet"] = sheet
    info["header"] = tabular.header_row_text(cmap.raw_header)[:400]
    if cmap.missing:
        info["warnings"].append(
            f"unmapped on '{sheet}': {', '.join(cmap.missing)} | header: {info['header']}")
    if not cmap.has("code"):
        info["warnings"].append(
            "no ticker column on the AIC sheet — rows will be name-only and "
            "cannot be joined to a price feed; a TIDM source is needed")

    cap_mult = unit_multiplier(cmap.header_for("market_cap"))
    ta_mult = unit_multiplier(cmap.header_for("nta_total"))
    if not cmap.has("nav") and not cmap.has("nta_total"):
        # Stated plainly because it bounds what this source can deliver: the
        # industry overview carries assets and market cap, not NAV per share.
        # The AIC's Monthly Information Release is the file that has NAVs, but
        # it is distributed to data providers rather than published here.
        info["warnings"].append(
            "the AIC industry overview has no NAV-per-share column — UK rows "
            "will carry market cap and total assets but no NTA. The AIC's "
            "Monthly Information Release (MIR) is the file with NAVs and needs "
            "a licence or a data-provider relationship")
    key_col = cmap.index.get("code", cmap.index["name"])

    out = []
    for row in tabular.data_rows(rows, idx, key_col):
        name = cmap.get(row, "name")
        name = str(name).strip() if name is not None else None
        if not name:
            continue
        cap = to_float(cmap.get(row, "market_cap"))
        ta = to_float(cmap.get(row, "nta_total"))
        nav, nav_unit = nta_from(cmap.get(row, "nav"), cmap.header_for("nav"), CURRENCY)
        isin = cmap.get(row, "isin")
        isin = str(isin).strip().upper() if isin else None

        out.append(Record(
            code=cmap.get(row, "code"),   # never the name: "3i Group plc" -> "3I" is wrong
            exchange=EXCHANGE,
            name=name,
            vehicle_type="VCT" if _VCT_RE.search(name) else "investment_trust",
            sector=(str(cmap.get(row, "sector")).strip()
                    if cmap.get(row, "sector") is not None else None),
            currency=CURRENCY,
            isin=isin if (isin and len(isin) == 12) else None,
            market_cap=cap * cap_mult if cap is not None else None,
            nta_total=ta * ta_mult if ta is not None else None,
            # "Total assets (£m)" is before borrowings. Say so on the row.
            nta_basis="gross_assets" if ta is not None else None,
            nta_per_share=nav,
            nta_unit=nav_unit,
            price=to_float(cmap.get(row, "price")),
            discount=_pct(cmap.get(row, "discount")),
            source=SOURCE,
            source_url=url,
        ))
    info["rows"] = len(out)
    if not out:
        info["warnings"].append(
            f"header found on '{sheet}' but no data rows parsed | header: {info['header']}")
    return out, info
