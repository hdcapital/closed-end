#!/usr/bin/env python3
"""ASX monthly Investment Products report.

This is the single best source in the whole project: official, monthly, and it
carries code, name, mandate, market cap, NTA per share and premium/discount for
every LIC and LIT in one file. One archived file per month is a whole-universe
monthly panel — roughly ten years of it if the archive goes back that far.

Two consumers, one parser: `src/universe/asx.py` takes the identities,
`src/collectors/nta.py` takes the NTA and discount observations.

The report is a human-facing spreadsheet whose column names drift between
editions, so everything goes through the fuzzy header mapping in tabular.py.
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional

from .. import tabular
from ..util import parse_date, to_float

SOURCE = "asx-investment-products-monthly"

# Logical field -> candidate header spellings, in precedence order.
COLUMN_SPEC = {
    "ticker":       ["asx code", "asx ticker", "code", "ticker"],
    "name":         ["company name", "fund name", "entity name", "name", "company"],
    "mandate":      ["investment mandate", "mandate", "asset class", "investment type",
                     "category", "sector", "strategy"],
    "market_cap":   ["market capitalisation", "market capitalization", "market cap"],
    "nta_pre_tax":  ["pre tax nta", "nta before tax", "nta pre tax", "pre-tax nta"],
    "nta_post_tax": ["post tax nta", "nta after tax", "nta post tax", "post-tax nta"],
    "nta":          ["nta per share", "nta per unit", "net tangible assets", "nta"],
    "price":        ["share price", "closing price", "last price", "price"],
    "premium_disc": ["premium discount", "premium/discount", "discount premium",
                     "prem disc", "premium", "discount"],
    "shares":       ["shares on issue", "units on issue", "securities on issue"],
    "listing_date": ["listing date", "date listed", "quotation date"],
    "nta_date":     ["nta date", "as at", "as at date"],
}

# The header must contain a code column and an NTA column; anything else is a
# different table (the report also carries ETF and structured-product sections).
REQUIRED_HEADER_HINTS = ["code", "nta"]

# Structure is inferable from the report's own wording.
_LIT_RE = re.compile(r"\b(trust|LIT)\b", re.IGNORECASE)


@dataclass
class AsxRecord:
    ticker: str
    name: Optional[str] = None
    mandate: Optional[str] = None
    market_cap: Optional[float] = None
    shares_on_issue: Optional[float] = None
    price: Optional[float] = None
    nta_pre_tax: Optional[float] = None
    nta_post_tax: Optional[float] = None
    nta_unspecified: Optional[float] = None
    premium_discount: Optional[float] = None
    listing_date: Optional[str] = None
    nta_date: Optional[str] = None
    structure: Optional[str] = None
    warnings: List[str] = field(default_factory=list)

    @property
    def primary_nta(self):
        """Pre-tax is the primary series — it is the figure the ASX report
        leads with and the one comparable to a UK trust's NAV. Post-tax is
        stored too and never averaged into the same series."""
        if self.nta_pre_tax is not None:
            return self.nta_pre_tax, "pre_tax"
        if self.nta_unspecified is not None:
            return self.nta_unspecified, "unspecified"
        if self.nta_post_tax is not None:
            return self.nta_post_tax, "post_tax"
        return None, None


@dataclass
class ParseResult:
    records: List[AsxRecord] = field(default_factory=list)
    as_of: Optional[str] = None
    sheet: Optional[str] = None
    warnings: List[str] = field(default_factory=list)


def _normalise_pct(value) -> Optional[float]:
    """Premium/discount as a signed fraction, negative = discount.

    The report has published this column both as "-12.3" (percent) and as
    "-0.123" (fraction) over the years. Anything beyond +/-1.0 must be percent:
    a fund does not trade at a 120% discount, and a genuine +150% premium is
    rare enough that mis-scaling it is the safer error. Values inside +/-1.0
    are ambiguous, so treat them as already-fractional and flag nothing —
    the discount recomputed from price/NTA is what the model actually uses.
    """
    v = to_float(value)
    if v is None:
        return None
    if abs(v) > 1.0:
        return v / 100.0
    return v


def parse(content: bytes, filename: str = "", as_of: str = None) -> ParseResult:
    """Parse one monthly report file into records."""
    result = ParseResult(as_of=as_of)
    try:
        sheets = tabular.read_sheets(content, filename)
    except Exception as e:
        result.warnings.append(f"unreadable spreadsheet: {e}")
        return result

    best = None
    for sheet_name, rows in sheets.items():
        idx = tabular.find_header(rows, REQUIRED_HEADER_HINTS)
        if idx is None:
            continue
        cmap = tabular.ColumnMap(rows[idx], COLUMN_SPEC)
        if not cmap.has("ticker"):
            continue
        # Prefer the sheet with the most mapped columns — the LIC/LIT table.
        score = len(cmap.index)
        if best is None or score > best[0]:
            best = (score, sheet_name, rows, idx, cmap)

    if best is None:
        result.warnings.append(
            "no sheet with a recognisable LIC/LIT header. Workbook contains:\n"
            + tabular.describe(sheets)
        )
        return result

    _, sheet_name, rows, idx, cmap = best
    result.sheet = sheet_name
    if cmap.missing:
        # Name the header we actually got: an unmapped column is usually a
        # renamed one, and the fix is a new entry in COLUMN_SPEC.
        result.warnings.append(
            f"unmapped columns on '{sheet_name}': {', '.join(cmap.missing)}"
            f" | header seen: {tabular.header_row_text(cmap.raw_header)}")

    seen = set()
    for row in tabular.data_rows(rows, idx, cmap.index["ticker"]):
        ticker = str(cmap.get(row, "ticker") or "").strip().upper()
        # ASX codes are 3-6 alphanumerics; this drops sub-heading rows
        # ("Domestic Equity", "Total") that share the code column.
        if not re.fullmatch(r"[A-Z0-9]{2,6}", ticker):
            continue
        if ticker in seen:
            continue
        seen.add(ticker)

        name = cmap.get(row, "name")
        name = str(name).strip() if name is not None else None
        mandate = cmap.get(row, "mandate")
        mandate = str(mandate).strip() if mandate is not None else None

        rec = AsxRecord(
            ticker=ticker,
            name=name,
            mandate=mandate,
            market_cap=to_float(cmap.get(row, "market_cap")),
            shares_on_issue=to_float(cmap.get(row, "shares")),
            price=to_float(cmap.get(row, "price")),
            nta_pre_tax=to_float(cmap.get(row, "nta_pre_tax")),
            nta_post_tax=to_float(cmap.get(row, "nta_post_tax")),
            premium_discount=_normalise_pct(cmap.get(row, "premium_disc")),
        )
        # A generic "NTA" column only counts when there is no explicit
        # pre/post-tax pair, so the two never get conflated.
        if rec.nta_pre_tax is None and rec.nta_post_tax is None:
            rec.nta_unspecified = to_float(cmap.get(row, "nta"))

        d = parse_date(cmap.get(row, "listing_date"))
        rec.listing_date = d.isoformat() if d else None
        nd = parse_date(cmap.get(row, "nta_date")) or parse_date(as_of)
        rec.nta_date = nd.isoformat() if nd else None
        rec.structure = "LIT" if _LIT_RE.search(f"{name or ''} {mandate or ''}") else "LIC"

        result.records.append(rec)

    if not result.records:
        result.warnings.append(f"header found on '{sheet_name}' but no data rows parsed")
    return result


# ---------------------------------------------------------------------------
# Mandate -> normalised sector
# ---------------------------------------------------------------------------

_SECTOR_PATTERNS = [
    ("private_equity",    r"private equity|venture|unlisted|pre[- ]?ipo"),
    ("property",          r"propert|real estate"),
    ("infrastructure",    r"infrastructure|utilit"),
    ("debt",              r"debt|credit|income|fixed interest|fixed income|loan|bond|mortgage"),
    ("hedge_multi_asset", r"absolute return|hedge|multi[- ]asset|diversified|alternativ|market neutral|long short"),
    ("small_cap_equity",  r"small|micro|emerging compan"),
    ("equity",            r"equit|share|stock|growth|value|australian|global|international|asia|europe|us |listed compan"),
]


def normalise_sector(mandate: Optional[str], name: str = "") -> str:
    """Map a free-text mandate onto the sector taxonomy the priors use.

    Order matters: "global listed infrastructure" is infrastructure, not
    equity, so infrastructure is tested first.
    """
    text = f"{mandate or ''} {name or ''}".lower()
    if not text.strip():
        return "unknown"
    for sector, pattern in _SECTOR_PATTERNS:
        if re.search(pattern, text):
            return sector
    return "unknown"
