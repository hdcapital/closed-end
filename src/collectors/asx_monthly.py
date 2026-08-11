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

# Logical field -> match rules. Written against the header the live June 2026
# report actually carries:
#
#   ASX Code | Type | Fund Name | MER (% p.a) | Outperf Fee | Mkt Cap ($m)# |
#   Mkt Cap ($m) Change | Transacted Value ($) | Transacted Volume | Number of
#   Transactions | Monthly Liquidity % | Prem/Disc % NTA (pre-tax) at N |
#   NTA Date | NTA Price | Last Close | Year High | Year Low |
#   Historical Distribution Yield | 1 Month Total Return | 1 Year Total Return |
#   3 Year Total Return (ann.) | 5 Year Total Return (ann.)
#
# Three traps in that header, all of which bit on the first live run:
#   * "Prem/Disc % NTA (pre-tax)" contains "nta pre tax" and will masquerade
#     as the NTA level column unless percentage columns are excluded.
#   * "NTA Price" contains "price" and will masquerade as the share price;
#     the real quote is "Last Close".
#   * "Mkt Cap ($m) Change" sits next to "Mkt Cap ($m)#".
_LEVEL_NOT = ["prem", "disc", "%", "return", "change", "yield", "date"]

COLUMN_SPEC = {
    "ticker":       {"match": ["asx code", "asx ticker", "code", "ticker"],
                     "not": ["type", "name"]},
    "name":         ["fund name", "company name", "entity name", "name"],
    # The report calls the mandate "Type" (Domestic Equity, Global Equity,
    # Fixed Income, ...). The exact-match pass picks it up safely.
    "mandate":      {"match": ["type", "investment mandate", "mandate",
                               "asset class", "category", "strategy"],
                     "not": ["issuer"]},
    "market_cap":   {"match": ["mkt cap", "market cap", "market capitalisation",
                               "market capitalization"],
                     "not": ["change", "%"]},
    "nta_pre_tax":  {"match": ["nta price pre tax", "pre tax nta", "nta before tax",
                               "nta pre tax"],
                     "not": _LEVEL_NOT},
    "nta_post_tax": {"match": ["post tax nta", "nta after tax", "nta post tax"],
                     "not": _LEVEL_NOT},
    "nta":          {"match": ["nta price", "nta per share", "nta per unit",
                               "net tangible assets", "nta"],
                     "not": _LEVEL_NOT},
    "price":        {"match": ["last close", "closing price", "share price",
                               "last price", "close"],
                     "not": ["nta", "year", "high", "low", "change", "%"]},
    "premium_disc": ["prem disc", "premium discount", "premium/discount",
                     "discount premium", "premium", "discount"],
    "shares":       ["shares on issue", "units on issue", "securities on issue"],
    "listing_date": ["listing date", "date listed", "quotation date"],
    "nta_date":     ["nta date", "as at date", "as at"],
    # Fee facts, which drive the extra growth haircut. Previously uncollected,
    # so the haircut could never fire.
    "mer":          {"match": ["mer", "management expense ratio", "ongoing charge",
                               "management fee"],
                     "not": ["outperf", "performance"]},
    "perf_fee":     ["outperf fee", "outperformance fee", "performance fee"],
    # Manager-stated performance. Stored with provenance='stated' and never
    # mixed into a computed column.
    "yield":        ["historical distribution yield", "distribution yield", "yield"],
    "ret_1y":       ["1 year total return"],
    "ret_3y":       ["3 year total return"],
    "ret_5y":       ["5 year total return"],
}

# Market cap is published in millions.
MARKET_CAP_MULTIPLIER = 1_000_000

# A per-share NTA outside this range is not a per-share NTA. This is the
# backstop for the whole class of "wrong column" bugs: even a header spelling
# nobody anticipated cannot inject a discount percentage into the NAV series.
MIN_NTA, MAX_NTA = 0.005, 1000.0

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
    ocr: Optional[float] = None
    has_performance_fee: Optional[bool] = None
    dist_yield: Optional[float] = None
    stated_r1y: Optional[float] = None
    stated_r3y: Optional[float] = None
    stated_r5y: Optional[float] = None
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


def _nta_level(value, rejected: List[str], label: str) -> Optional[float]:
    """A per-share NTA, or None with a recorded reason.

    Belt to the column-exclusion braces: a value outside the plausible
    per-share range means the column is wrong, whatever its header said.
    """
    v = to_float(value)
    if v is None:
        return None
    if not (MIN_NTA <= v <= MAX_NTA):
        rejected.append(f"{label}={v}")
        return None
    return v


def _percent(value, header: str = "") -> Optional[float]:
    """A column published in percent -> a fraction.

    Units come from the header wherever the publisher declares them: "MER
    (% p.a)" holding 0.15 is fifteen basis points, and a magnitude test would
    read it as 15% — a hundredfold error in the fee haircut. Only where the
    header is silent do we fall back to the magnitude heuristic, which stays
    ambiguous for genuinely sub-1% returns and is documented as such.
    """
    v = to_float(value)
    if v is None:
        return None
    if "%" in (header or ""):
        return v / 100.0
    return v / 100.0 if abs(v) > 1.0 else v


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

    seen, rejected = set(), []
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

        mcap = to_float(cmap.get(row, "market_cap"))
        perf = cmap.get(row, "perf_fee")
        perf_str = str(perf).strip().lower() if perf is not None else ""

        rec = AsxRecord(
            ticker=ticker,
            name=name,
            mandate=mandate,
            market_cap=mcap * MARKET_CAP_MULTIPLIER if mcap is not None else None,
            shares_on_issue=to_float(cmap.get(row, "shares")),
            price=_nta_level(cmap.get(row, "price"), rejected, f"{ticker}.price"),
            nta_pre_tax=_nta_level(cmap.get(row, "nta_pre_tax"), rejected,
                                   f"{ticker}.nta_pre_tax"),
            nta_post_tax=_nta_level(cmap.get(row, "nta_post_tax"), rejected,
                                    f"{ticker}.nta_post_tax"),
            premium_discount=_normalise_pct(cmap.get(row, "premium_disc")),
            ocr=_percent(cmap.get(row, "mer"), cmap.header_for("mer")),
            has_performance_fee=(perf_str in {"yes", "y", "true", "1"}
                                 if perf_str else None),
            dist_yield=_percent(cmap.get(row, "yield"), cmap.header_for("yield")),
            stated_r1y=_percent(cmap.get(row, "ret_1y"), cmap.header_for("ret_1y")),
            stated_r3y=_percent(cmap.get(row, "ret_3y"), cmap.header_for("ret_3y")),
            stated_r5y=_percent(cmap.get(row, "ret_5y"), cmap.header_for("ret_5y")),
        )
        # A generic "NTA" column only counts when there is no explicit
        # pre/post-tax pair, so the two never get conflated.
        if rec.nta_pre_tax is None and rec.nta_post_tax is None:
            rec.nta_unspecified = _nta_level(cmap.get(row, "nta"), rejected,
                                             f"{ticker}.nta")

        d = parse_date(cmap.get(row, "listing_date"))
        rec.listing_date = d.isoformat() if d else None
        nd = parse_date(cmap.get(row, "nta_date")) or parse_date(as_of)
        rec.nta_date = nd.isoformat() if nd else None
        rec.structure = "LIT" if _LIT_RE.search(f"{name or ''} {mandate or ''}") else "LIC"

        result.records.append(rec)

    if rejected:
        # A handful is dirty data; hundreds means a mis-mapped column.
        result.warnings.append(
            f"{len(rejected)} value(s) rejected as implausible per-share levels "
            f"(e.g. {', '.join(rejected[:5])}) — if this is most of the sheet, "
            "the column mapping is wrong, not the data")
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
