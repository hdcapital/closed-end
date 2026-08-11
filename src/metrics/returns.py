#!/usr/bin/env python3
"""Annualised NTA total returns.

"Total return" here means NAV growth with distributions reinvested. We hold a
per-share NTA series and a stream of cash distributions, so the index is built
the standard way:

    index_{i+1} = index_i * (nta_{i+1} + dividends paid in (t_i, t_{i+1}]) / nta_i

which assumes each distribution is reinvested at the *end-of-period* NTA rather
than at the NTA on its ex-date. With a monthly NTA series that is the best
available approximation and it slightly understates the return of a fund paying
large distributions into a rising NAV. It is applied identically to every fund,
so it does not reorder the screen; it is documented in the README because a
methodology you can't state isn't conservative, it's just vague.

Provenance is never mixed. A computed series and a manager-stated "5-year NAV
total return" are different objects and are stored under different provenance
values, which the report carries through to its own column.
"""

import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..util import annualise, parse_date, year_fraction

# The nta_type used as the primary series, in preference order. Pre-tax is
# primary for ASX (it is what the report leads with and what compares to a UK
# NAV); cum-income is primary for the UK.
PRIMARY_NTA_PREFERENCE = ["pre_tax", "cum_income", "unspecified", "ex_income", "post_tax"]


@dataclass
class ReturnSet:
    r5: Optional[float] = None
    r10: Optional[float] = None
    r_all: Optional[float] = None
    n_years: Optional[float] = None
    nta_type: Optional[str] = None
    n_observations: int = 0
    provenance: str = "computed"
    warnings: List[str] = field(default_factory=list)


def choose_series(conn, fund_id: str) -> Tuple[Optional[str], List[dict]]:
    """Pick one nta_type and return its observations, oldest first.

    Mixing pre-tax and post-tax NTA into one series would manufacture a return
    at every point the source changed which figure it published, so the series
    is chosen once and used whole.
    """
    rows = conn.execute(
        "SELECT date, nta_per_share, nta_type FROM nta_observations "
        "WHERE fund_id=? AND nta_per_share IS NOT NULL AND nta_per_share > 0 "
        "ORDER BY date",
        (fund_id,),
    ).fetchall()
    if not rows:
        return None, []
    by_type: Dict[str, list] = {}
    for r in rows:
        by_type.setdefault(r["nta_type"], []).append(
            {"date": r["date"], "nta": r["nta_per_share"]}
        )
    for t in PRIMARY_NTA_PREFERENCE:
        if t in by_type and len(by_type[t]) >= 2:
            return t, by_type[t]
    # Nothing preferred has two points: fall back to whichever has the most.
    best = max(by_type.items(), key=lambda kv: len(kv[1]))
    return best[0], best[1]


def dividends_by_date(conn, fund_id: str) -> List[dict]:
    rows = conn.execute(
        "SELECT date, SUM(COALESCE(dividend,0)) AS d FROM price_observations "
        "WHERE fund_id=? AND dividend IS NOT NULL AND dividend > 0 "
        "GROUP BY date ORDER BY date",
        (fund_id,),
    ).fetchall()
    return [{"date": r["date"], "dividend": r["d"]} for r in rows]


def build_total_return_index(series: List[dict], dividends: List[dict]) -> List[dict]:
    """Turn an NTA series + distributions into a total-return index."""
    if not series:
        return []
    divs = sorted(dividends, key=lambda d: d["date"])
    out = [{"date": series[0]["date"], "index": 1.0}]
    di = 0
    # Distributions before the first NTA observation belong to a period we
    # cannot measure, so they are skipped rather than credited to period one.
    while di < len(divs) and divs[di]["date"] <= series[0]["date"]:
        di += 1
    for prev, curr in zip(series, series[1:]):
        paid = 0.0
        while di < len(divs) and divs[di]["date"] <= curr["date"]:
            paid += divs[di]["dividend"]
            di += 1
        if prev["nta"] <= 0:
            continue
        growth = (curr["nta"] + paid) / prev["nta"]
        out.append({"date": curr["date"], "index": out[-1]["index"] * growth})
    return out


def _index_at_or_before(index: List[dict], target: datetime.date) -> Optional[dict]:
    best = None
    for pt in index:
        d = parse_date(pt["date"])
        if d is None or d > target:
            break
        best = pt
    return best


def annualised_over(index: List[dict], years: float,
                    as_of: str = None) -> Optional[float]:
    """Annualised return over the trailing `years`, or None if the history is
    too short. Never extrapolates a shorter window into a longer label."""
    if len(index) < 2:
        return None
    end = _index_at_or_before(index, parse_date(as_of)) if as_of else index[-1]
    if end is None:
        return None
    end_date = parse_date(end["date"])
    target = end_date - datetime.timedelta(days=int(round(years * 365.25)))
    first_date = parse_date(index[0]["date"])
    # Demand the history actually reaches back: a 5-year label computed off
    # 3 years of data is the exact self-deception this project exists to avoid.
    # 20 days of tolerance absorbs month-end/observation-date drift.
    if first_date > target + datetime.timedelta(days=20):
        return None
    start = _index_at_or_before(index, target)
    if start is None or start["index"] <= 0:
        return None
    actual_years = year_fraction(start["date"], end["date"])
    if not actual_years or actual_years <= 0:
        return None
    return annualise(end["index"] / start["index"], actual_years)


def compute(conn, fund_id: str, as_of: str = None) -> ReturnSet:
    """5y / 10y / since-inception annualised NTA total returns."""
    nta_type, series = choose_series(conn, fund_id)
    rs = ReturnSet(nta_type=nta_type, n_observations=len(series))
    if len(series) < 2:
        rs.warnings.append("fewer than two usable NTA observations")
        return rs

    index = build_total_return_index(series, dividends_by_date(conn, fund_id))
    if len(index) < 2:
        rs.warnings.append("total-return index could not be built")
        return rs

    rs.n_years = year_fraction(index[0]["date"], index[-1]["date"])
    rs.r5 = annualised_over(index, 5.0, as_of)
    rs.r10 = annualised_over(index, 10.0, as_of)
    if rs.n_years and rs.n_years > 0:
        rs.r_all = annualise(index[-1]["index"] / index[0]["index"], rs.n_years)

    if not any(d.get("dividend") for d in dividends_by_date(conn, fund_id)):
        rs.warnings.append(
            "no distributions found: the 'total return' equals NAV price growth "
            "for this fund and understates it if it in fact pays distributions"
        )
    return rs


def stated(conn, fund_id: str, r5: float = None, r10: float = None) -> ReturnSet:
    """Wrap manager/AIC-stated performance figures with provenance='stated'.

    Kept in a separate constructor so a stated figure can never be produced by
    the computed path, or land in the computed column, by accident.
    """
    rs = ReturnSet(r5=r5, r10=r10, provenance="stated")
    rs.warnings.append("figures as published by the source, not recomputed from a NAV series")
    return rs
