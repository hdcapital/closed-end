#!/usr/bin/env python3
"""The uniform record, and the cleaning rules that produce it.

One job: turn two very different spreadsheets into one table where a stock
code, a market cap and an NTA mean the same thing on every row.

The three things that actually need cleaning, and how each is handled:

* **Stock codes.** ASX publishes 3-letter codes; the AIC publishes TIDMs that
  sometimes carry a listing suffix ("SMT.L", "FCIT LN"). Both are uppercased,
  stripped of suffixes and validated against a shape.
* **Market caps.** ASX publishes millions ("Mkt Cap ($m)"); the AIC may publish
  millions or thousands. The multiplier is read from the *header*, never
  guessed from magnitude, and stored in actual currency units.
* **NTA per share.** ASX quotes dollars; UK vehicles usually quote pence. Where
  the header declares the unit we use it. Where it does not, the row is kept
  with `nta_unit` = "assumed_major" so the assumption is visible rather than
  buried — a silently mis-scaled NAV is a 100x error that looks like a 99%
  discount.
"""

import re
from dataclasses import asdict, dataclass, field
from typing import List, Optional

# The columns of the output table, in order.
COLUMNS = [
    "code", "isin", "exchange", "name", "vehicle_type", "sector",
    "currency", "market_cap", "nta_total", "nta_per_share", "nta_unit",
    "price", "discount", "discount_basis", "nta_date", "as_of",
    "source", "source_url",
]

# Per-share values outside this band are not per-share values.
MIN_NTA, MAX_NTA = 0.005, 10000.0

_CODE_RE = re.compile(r"^[A-Z0-9]{2,6}$")
# ASX index codes are X + four letters (XJOAI, XSOAI); LSE has no such shape.
_INDEX_CODE_RE = re.compile(r"^X[A-Z]{4}$")
_NON_FUND_RE = re.compile(
    r"\bindex\b|accumulation index|\bbenchmark\b|\bETF\b|exchange traded|"
    r"\btotal\b|\baverage\b|\bsector\b$",
    re.IGNORECASE,
)


@dataclass
class Record:
    code: str
    exchange: str
    isin: Optional[str] = None
    name: Optional[str] = None
    vehicle_type: Optional[str] = None      # LIC | LIT | investment_trust | VCT
    sector: Optional[str] = None
    currency: Optional[str] = None
    market_cap: Optional[float] = None      # currency units, not millions
    # Aggregate net tangible assets: what the fund owns, in currency units.
    # Kept apart from market_cap because the gap between the two IS the
    # discount — but the two together are all a discount needs.
    nta_total: Optional[float] = None
    nta_per_share: Optional[float] = None   # major unit (dollars / pounds)
    nta_unit: Optional[str] = None          # declared_major | declared_pence | assumed_major
    price: Optional[float] = None
    discount: Optional[float] = None        # fraction, negative = discount
    discount_basis: Optional[str] = None    # published | price_over_nta | mcap_over_nta_total
    nta_date: Optional[str] = None
    as_of: Optional[str] = None
    source: Optional[str] = None
    source_url: Optional[str] = None

    def as_row(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k in COLUMNS}


@dataclass
class CleanResult:
    records: List[Record] = field(default_factory=list)
    dropped: List[dict] = field(default_factory=list)

    def drop(self, code: str, name: str, reason: str,
             market_cap=None, nta=None) -> None:
        # The figures ride along: a row dropped for lacking a ticker still has
        # a usable market cap, and discarding it entirely would lose real data.
        self.dropped.append({"code": code, "name": name, "reason": reason,
                             "market_cap": market_cap, "nta_per_share": nta})


# Words that pass the ticker shape test but are labels, not securities. These
# appear as totals and sub-headings inside both publishers' tables.
# Vendor listing suffixes that legitimately follow a ticker.
_LISTING_SUFFIXES = {"L", "LN", "AU", "AX", "NZ", "NA", "US", "EQUITY",
                     "GR", "SW", "SJ", "HK", "T", "TO"}

_NOT_A_CODE = {
    "TOTAL", "TOTALS", "SUM", "AVERAGE", "AVG", "MEDIAN", "SECTOR", "ALL",
    "NA", "N A", "NONE", "NIL", "TBC", "OTHER", "OTHERS", "GROUP", "PLC",
    "LTD", "LIMITED", "TRUST", "FUND", "INDEX",
}


def normalise_code(raw) -> Optional[str]:
    """A bare exchange ticker, or None if it isn't one.

    Handles what the two publishers and the vendors in between attach:
    suffixes (SMT.L, FCIT LN, BRM.NZ) and exchange *prefixes* (LSE:SMT) — the
    prefix sits before the separator and the ticker after it, so splitting and
    taking the first part turns SMT into LSE.

    Shape alone is not enough: "Total" is five letters and looks exactly like a
    ticker, so aggregate labels are denied by name.
    """
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if ":" in s:                             # LSE:SMT, XLON:FCIT
        s = s.split(":")[-1]
    s = s.replace(".", " ")                  # SMT.L -> SMT L
    tokens = [t for t in s.split() if t]
    if not tokens:
        return None
    # A ticker is one token, optionally followed by a vendor listing suffix.
    # Anything else is a company name, and turning "3i Group plc" into the code
    # "3I" invents a security — 3i Group's TIDM is III.
    if len(tokens) > 2 or (len(tokens) == 2 and tokens[1] not in _LISTING_SUFFIXES):
        return None
    s = re.sub(r"[^A-Z0-9]", "", tokens[0])
    if not _CODE_RE.match(s) or s in _NOT_A_CODE:
        return None
    return s


def unit_multiplier(header: str) -> float:
    """Read the scale a money column is published in from its header.

    "Mkt Cap ($m)" -> 1e6, "Total assets (£'000)" -> 1e3, bare -> 1. Read, not
    guessed: magnitude tests get this wrong for a £900m trust reported in
    thousands, and a 1000x error in market cap silently destroys the size band.
    """
    h = (header or "").lower()
    if re.search(r"\bm\b|\(£m\)|\(\$m\)|million|\bmn\b|\bm\)", h):
        return 1e6
    if re.search(r"000|\bk\b|thousand", h):
        return 1e3
    return 1.0


def nta_from(value, header: str, currency: str):
    """(value_in_major_unit, unit_label) for a per-share NAV/NTA column."""
    from ..util import to_float
    v = to_float(value)
    if v is None:
        return None, None
    h = (header or "").lower()
    if re.search(r"pence|\(p\)|\bp\b(?!\w)", h):
        return v / 100.0, "declared_pence"
    if re.search(r"\(£\)|\(\$\)|pound|dollar", h):
        return v, "declared_major"
    # No declared unit. Keep the number as published and say so; the alternative
    # is a magnitude guess, and guessing here is the 100x error.
    return v, "assumed_major"


def clean(raw_records: List[Record]) -> CleanResult:
    """Validate and deduplicate into the uniform set.

    A row survives only if it has a real stock code and at least one of the two
    figures the exercise is actually about — a market cap or an NTA. Everything
    dropped is reported with a reason; nothing disappears quietly.
    """
    out = CleanResult()
    seen = {}
    for r in raw_records:
        code = normalise_code(r.code)
        if not code:
            out.drop(str(r.code), r.name or "", "not a recognisable stock code",
                     r.market_cap, r.nta_per_share)
            continue
        if _INDEX_CODE_RE.match(code):
            out.drop(code, r.name or "", "index code shape (X + four letters)",
                     r.market_cap, r.nta_per_share)
            continue
        if r.name and _NON_FUND_RE.search(r.name):
            out.drop(code, r.name, "name matches a non-fund / aggregate row",
                     r.market_cap, r.nta_per_share)
            continue
        # No abs(): a negative NTA is not an out-of-range NAV, it is a
        # discount column wearing the NAV's name. That mistake has already
        # happened once on the ASX file and must not survive here.
        if r.nta_per_share is not None and not (MIN_NTA <= r.nta_per_share <= MAX_NTA):
            out.drop(code, r.name or "",
                     f"NTA {r.nta_per_share} outside plausible per-share range")
            r.nta_per_share, r.nta_unit = None, None
        if r.market_cap is not None and r.market_cap <= 0:
            r.market_cap = None
        # A discount needs a price and a NAV *at the same scale*. Per-share is
        # the familiar way; aggregate works identically, because market cap and
        # total NTA both carry the same share count and it cancels:
        #   mcap / nta_total - 1 == (price x shares) / (nav x shares) - 1
        # So a source with no per-share NAV can still yield a real discount.
        if r.discount is None and r.market_cap and r.nta_total:
            r.discount = r.market_cap / r.nta_total - 1.0
            r.discount_basis = "mcap_over_nta_total"
        elif r.discount is not None and r.discount_basis is None:
            r.discount_basis = "published"

        if r.market_cap is None and r.nta_per_share is None and r.nta_total is None:
            out.drop(code, r.name or "", "no market cap and no NTA")
            continue

        r.code = code
        key = f"{r.exchange}:{code}"
        if key in seen:
            # Same vehicle from two sources: keep the richer row.
            prev = seen[key]
            richer = max((prev, r), key=lambda x: sum(
                v is not None for v in (x.market_cap, x.nta_per_share,
                                        x.price, x.isin, x.nta_total)))
            seen[key] = richer
            continue
        seen[key] = r

    out.records = sorted(seen.values(), key=lambda x: (x.exchange, x.code))
    return out
