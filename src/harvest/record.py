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
    "currency", "market_cap", "nta_total", "nta_basis", "nta_per_share",
    "nta_unit", "price", "discount", "discount_basis", "nta_date", "as_of",
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
    # WHICH asset figure nta_total actually is. Two publishers use the word
    # "assets" for two different numbers and the gap between them is the debt:
    #   net_shareholders_funds — AIC MIR. What the shareholders own. The NTA.
    #   gross_assets           — AIC industry overview. Before borrowings, so
    #                            larger, and larger by more the more a fund
    #                            gears. Not an NTA, and not comparable to one.
    #   published_nta          — the ASX states its own NTA per share.
    # A column that mixes the first two is not the uniform NTA it looks like.
    nta_basis: Optional[str] = None
    nta_per_share: Optional[float] = None   # major unit (dollars / pounds)
    nta_unit: Optional[str] = None          # declared_major | declared_pence | assumed_major
    price: Optional[float] = None
    discount: Optional[float] = None        # fraction, negative = discount
    # published            — the source states it (ASX does)
    # mcap_over_gross_assets — derived, and BIASED WIDE by any gearing. See the
    #                          gearing note in run.py: the AIC asset figure is
    #                          gross, so a geared fund reads cheaper than it is.
    discount_basis: Optional[str] = None
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
    linked: int = 0             # code-less rows given a ticker by ISIN
    unlinkable: int = 0         # code-less rows whose ISIN matched nothing
    merged: int = 0             # vehicles assembled from more than one source

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


def link_by_isin(records: List[Record]):
    """Give code-less rows the ticker their ISIN already carries elsewhere.

    The AIC publishes two files and only one of them has a ticker. The industry
    overview has both ISIN and TIDM; the Monthly Information Release — the file
    with the *net* assets, and so the only exact discount — is keyed by ISIN
    alone. Without this step every MIR row fails the stock-code test and the
    better of the two sources is thrown away.

    An ISIN identifies a share class, which is exactly the granularity wanted:
    it will not let an ordinary share borrow a C share's ticker.
    """
    by_isin = {}
    for r in records:
        code = normalise_code(r.code)
        if code and r.isin:
            by_isin.setdefault(str(r.isin).strip().upper(), code)
    linked = unlinkable = 0
    for r in records:
        if normalise_code(r.code):
            continue
        code = by_isin.get(str(r.isin or "").strip().upper())
        if code:
            r.code = code
            linked += 1
        elif r.isin:
            unlinkable += 1
    return linked, unlinkable


# How much a discount is worth believing, by how it was arrived at.
#   price_over_nav_net     — the AIC MIR: net shareholders' funds. Exact.
#   published              — the ASX states its own; trust the publisher.
#   mcap_over_gross_assets — derived off gross assets, so biased wide by
#                            whatever the fund borrows. An estimate, last.
_BASIS_RANK = {"price_over_nav_net": 3, "published": 2,
               "mcap_over_gross_assets": 1}

# Fields that describe one valuation and are only true together. A net NAV and
# a gross NAV must never end up in the same row, so these move as a block.
_VALUATION = ("nta_total", "nta_basis", "nta_per_share", "nta_unit", "price",
              "discount", "discount_basis", "nta_date")


def _basis_rank(r: Record) -> int:
    return _BASIS_RANK.get(r.discount_basis, 0) if r.discount is not None else 0


def merge(a: Record, b: Record) -> Record:
    """One vehicle, two sources: take the better valuation, fill the gaps.

    Not "keep the row with more cells filled" — that would let a gross-assets
    discount beat an exact one just for carrying a market cap alongside. The
    valuation is decided on its own merits and everything else is filled in
    around it.
    """
    from dataclasses import replace
    keep, other = (a, b) if _basis_rank(a) >= _basis_rank(b) else (b, a)
    out = replace(keep)
    # Only when the winner has no valuation at all is it worth borrowing the
    # loser's — otherwise the block stands as the source published it.
    if _basis_rank(keep) == 0:
        for f in _VALUATION:
            if getattr(out, f) is None:
                setattr(out, f, getattr(other, f))
    for f in ("isin", "name", "sector", "currency", "vehicle_type",
              "market_cap", "as_of"):
        if getattr(out, f) is None:
            setattr(out, f, getattr(other, f))
    out.source = "+".join(dict.fromkeys(
        s for s in (keep.source, other.source) if s))
    out.source_url = " | ".join(dict.fromkeys(
        u for u in (keep.source_url, other.source_url) if u))
    return out


def clean(raw_records: List[Record]) -> CleanResult:
    """Validate and deduplicate into the uniform set.

    A row survives only if it has a real stock code and at least one of the two
    figures the exercise is actually about — a market cap or an NTA. Everything
    dropped is reported with a reason; nothing disappears quietly.
    """
    out = CleanResult()
    out.linked, out.unlinkable = link_by_isin(raw_records)
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
            r.discount_basis = "mcap_over_gross_assets"
        elif r.discount is not None and r.discount_basis is None:
            r.discount_basis = "published"

        r.code = code
        key = f"{r.exchange}:{code}"
        if r.market_cap is None and r.nta_per_share is None and r.nta_total is None:
            # An empty row is only a loss if nobody else has the vehicle. The
            # MIR lists 142 members that report no figures at all; almost all
            # of them are in the industry overview, so saying "no market cap
            # and no NTA" 137 times overstates what was actually lost.
            out.drop(code, r.name or "",
                     "no figures; already covered by another source"
                     if key in seen else "no market cap and no NTA")
            continue

        if key in seen:
            seen[key] = merge(seen[key], r)
            out.merged += 1
            continue
        seen[key] = r

    out.records = sorted(seen.values(), key=lambda x: (x.exchange, x.code))
    return out
