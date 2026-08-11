#!/usr/bin/env python3
"""Discount-to-NTA statistics.

    discount_t = price_t / nta_t - 1        (negative = trading at a discount)

Two implementation choices worth stating, because both change the numbers:

**Matching.** The spec says match a price to the nearest NTA observation no
more than 45 days stale. "Nearest" is implemented as *nearest preceding*: a
discount computed against an NTA that had not yet been published is lookahead
bias, and in a backtest it is the flattering kind. Where a fund publishes
monthly, that makes the discount series slightly stale by construction —
correct, and the same for every fund.

**Weighting.** The series is at price frequency (daily where prices are daily),
so historical means are trading-day-weighted. A month with a suspended quote
contributes less than a fully traded month, which is the desired behaviour.
"""

import datetime
from dataclasses import dataclass, field
from typing import List, Optional

from ..util import mean, parse_date, stdev

from .returns import choose_series


@dataclass
class DiscountStats:
    current: Optional[float] = None
    current_date: Optional[str] = None
    mean_5y: Optional[float] = None
    mean_10y: Optional[float] = None
    mean_all: Optional[float] = None
    stdev_5y: Optional[float] = None
    z_score: Optional[float] = None
    pct_time_wider_than_threshold: Optional[float] = None
    n_observations: int = 0
    nta_type: Optional[str] = None
    series: List[dict] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def build_series(conn, fund_id: str, max_gap_days: int,
                 nta_type: str = None, nta_series: List[dict] = None) -> List[dict]:
    """Discount observations from matched (price, NTA) pairs."""
    if nta_series is None:
        nta_type, nta_series = choose_series(conn, fund_id)
    if not nta_series:
        return []

    prices = conn.execute(
        "SELECT date, close FROM price_observations "
        "WHERE fund_id=? AND close IS NOT NULL AND close > 0 ORDER BY date",
        (fund_id,),
    ).fetchall()
    if not prices:
        return []

    nta_dates = [(parse_date(o["date"]), o["nta"]) for o in nta_series]
    nta_dates = [(d, v) for d, v in nta_dates if d is not None and v and v > 0]
    if not nta_dates:
        return []

    out = []
    j = 0
    for p in prices:
        pd_ = parse_date(p["date"])
        if pd_ is None:
            continue
        # Advance to the latest NTA at or before this price date.
        while j + 1 < len(nta_dates) and nta_dates[j + 1][0] <= pd_:
            j += 1
        nta_date, nta_val = nta_dates[j]
        if nta_date > pd_:
            continue                                  # price predates any NTA
        gap = (pd_ - nta_date).days
        if gap > max_gap_days:
            continue                                  # NTA too stale to match
        out.append({
            "date": p["date"],
            "discount": p["close"] / nta_val - 1.0,
            "nta_date": nta_date.isoformat(),
            "gap_days": gap,
        })
    return out


def compute(conn, fund_id: str, cfg, as_of: str = None,
            persistence_threshold: float = None) -> DiscountStats:
    max_gap = int(cfg.num("run.max_price_nta_gap_days"))
    nta_type, nta_series = choose_series(conn, fund_id)
    series = build_series(conn, fund_id, max_gap, nta_type, nta_series)

    st = DiscountStats(nta_type=nta_type, series=series, n_observations=len(series))
    if not series:
        st.warnings.append(
            "no price/NTA pair inside the "
            f"{max_gap}-day window — discount not computable"
        )
        return st

    end = parse_date(as_of) if as_of else parse_date(series[-1]["date"])
    usable = [o for o in series if parse_date(o["date"]) <= end]
    if not usable:
        st.warnings.append("no discount observations at or before the as-of date")
        return st

    st.current = usable[-1]["discount"]
    st.current_date = usable[-1]["date"]

    def window(years: float) -> List[float]:
        cutoff = end - datetime.timedelta(days=int(round(years * 365.25)))
        return [o["discount"] for o in usable if parse_date(o["date"]) >= cutoff]

    w5, w10 = window(5), window(10)
    allv = [o["discount"] for o in usable]

    st.mean_5y = mean(w5)
    st.mean_10y = mean(w10)
    st.mean_all = mean(allv)
    st.stdev_5y = stdev(w5)

    # A z-score off a near-flat history is arithmetically huge and
    # informationally empty, so it needs a floor on dispersion, not just on n.
    if st.stdev_5y and st.stdev_5y > 1e-6 and st.mean_5y is not None and len(w5) >= 24:
        st.z_score = (st.current - st.mean_5y) / st.stdev_5y
    elif len(w5) < 24:
        st.warnings.append(
            f"only {len(w5)} discount observations in the 5y window — z-score withheld"
        )
    else:
        st.warnings.append("5y discount standard deviation ~0 — z-score withheld")

    # Share of the recent past spent wider than the persistence threshold.
    if persistence_threshold is None:
        persistence_threshold = cfg.num("activist.prize.persistence_discount_threshold")
    p_years = cfg.num("activist.prize.persistence_years")
    cutoff = end - datetime.timedelta(days=int(round(p_years * 365.25)))
    recent = [o["discount"] for o in usable if parse_date(o["date"]) >= cutoff]
    if recent:
        st.pct_time_wider_than_threshold = (
            sum(1 for d in recent if d < persistence_threshold) / len(recent)
        )
    else:
        st.warnings.append(f"no discount observations in the last {p_years:g} years")

    return st


def peer_median_current(stats_by_fund: dict, fund_id: str, sector: str,
                        region: str, sectors: dict, regions: dict,
                        min_peers: int) -> tuple:
    """Peer-group median current discount.

    Peer group is same sector + same exchange region where at least `min_peers`
    others exist, else the same sector globally. The fund itself is excluded —
    a peer group that contains the subject pulls its own target toward itself,
    which quietly damps the very reversion the model is trying to measure.

    Returns (median, peer_group_label, n_peers).
    """
    from ..util import median as _median

    def collect(same_region: bool):
        out = []
        for fid, st in stats_by_fund.items():
            if fid == fund_id or st.current is None:
                continue
            if sectors.get(fid) != sector:
                continue
            if same_region and regions.get(fid) != region:
                continue
            out.append(st.current)
        return out

    local = collect(True)
    if len(local) >= min_peers:
        return _median(local), f"{sector}@{region}", len(local)
    glob = collect(False)
    if glob:
        return _median(glob), f"{sector}@global", len(glob)
    return None, f"{sector}@none", 0
