#!/usr/bin/env python3
"""Metric -> model -> score pipeline over the whole universe.

Order matters and is not arbitrary: discount statistics for *every* fund have
to exist before any fund's peer-group median can be taken, and peer medians
have to exist before d_star, which the forward return and the activist prize
pillar both consume. So this runs in two passes rather than one loop.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import db
from .collectors import holders as holders_mod
from .collectors import prices as prices_mod
from .metrics import discounts, returns
from .metrics.discounts import DiscountStats
from .metrics.returns import ReturnSet
from .models import activist as activist_mod
from .models import forward_return as fr_mod
from .models.activist import ActivistScore
from .models.forward_return import ForwardReturn
from .util import median, today_utc, utcnow_iso


@dataclass
class FundResult:
    # The classes are imported directly rather than referenced through their
    # modules: in an annotated assignment CPython binds the field name before
    # evaluating the annotation, so `returns: returns.ReturnSet` would look the
    # attribute up on the freshly-bound None.
    fund: dict
    returns: ReturnSet = None
    discounts: DiscountStats = None
    forward: ForwardReturn = None
    activist: ActivistScore = None
    trailing_yield: Optional[float] = None
    register: List[dict] = field(default_factory=list)
    d_star: Optional[float] = None
    warnings: List[str] = field(default_factory=list)


def live_funds(conn) -> List[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM funds WHERE status='live' ORDER BY exchange, ticker"
    )]


def run(conn, cfg, as_of: str = None) -> Dict[str, FundResult]:
    as_of = as_of or today_utc()
    funds = live_funds(conn)
    results: Dict[str, FundResult] = {}

    # ---- Pass 1: per-fund metrics that need no cross-sectional context -----
    for f in funds:
        fid = f["fund_id"]
        r = FundResult(fund=f)
        r.returns = returns.compute(conn, fid, as_of)
        _apply_stated_returns(conn, fid, r.returns, cfg)
        r.discounts = discounts.compute(conn, fid, cfg, as_of)
        r.trailing_yield = prices_mod.trailing_yield(conn, fid, as_of)
        r.register = holders_mod.latest_register(conn, fid)
        r.warnings = list(r.returns.warnings) + list(r.discounts.warnings)
        results[fid] = r

    # ---- Cross-sectional context ------------------------------------------
    sectors = {fid: (r.fund.get("sector") or "unknown") for fid, r in results.items()}
    regions = {fid: r.fund["exchange"] for fid, r in results.items()}
    disc_stats = {fid: r.discounts for fid, r in results.items()}
    min_peers = int(cfg.num("forward_return.min_peer_group"))

    # Sector median 5y NAV return, for the prize pillar's underperformance test.
    sector_r5: Dict[str, List[float]] = {}
    for fid, r in results.items():
        if r.returns and r.returns.r5 is not None:
            sector_r5.setdefault(sectors[fid], []).append(r.returns.r5)
    sector_median_r5 = {s: median(v) for s, v in sector_r5.items()}

    # ---- Pass 2: models ---------------------------------------------------
    for fid, r in results.items():
        d_peer, peer_label, n_peers = discounts.peer_median_current(
            disc_stats, fid, sectors[fid], regions[fid], sectors, regions, min_peers)

        # d_own is the 10-year mean where it exists, else all available history.
        d_own = r.discounts.mean_10y if r.discounts.mean_10y is not None \
            else r.discounts.mean_all

        r.forward = fr_mod.compute(
            cfg,
            g5=r.returns.r5, g10=r.returns.r10, g_all=r.returns.r_all,
            n_years=r.returns.n_years, sector=sectors[fid],
            d0=r.discounts.current, d_own=d_own, d_peer=d_peer,
            z_score=r.discounts.z_score,
            ocr=r.fund.get("ocr"),
            has_performance_fee=bool(r.fund.get("has_performance_fee")),
            trailing_yield=r.trailing_yield,
            peer_group=peer_label, n_peers=n_peers,
            has_long_window=r.returns.has_long_window,
            growth_provenance=r.returns.r5_source or r.returns.r10_source
                              or r.returns.r_all_source or "computed",
        )
        r.d_star = r.forward.reversion.d_star

        events = [dict(e) for e in conn.execute(
            "SELECT event_type, event_date, detail FROM fund_events WHERE fund_id=?",
            (fid,))]

        r.activist = activist_mod.compute(
            cfg,
            d0=r.discounts.current, d_star=r.d_star,
            pct_time_wide=r.discounts.pct_time_wider_than_threshold,
            market_cap=r.fund.get("market_cap"), currency=r.fund.get("currency"),
            g5=r.returns.r5, sector_median_g5=sector_median_r5.get(sectors[fid]),
            exchange=r.fund["exchange"], holders=r.register,
            insider_pct=holders_mod.insider_pct(r.register),
            institutional_filing_count=holders_mod.institutional_filing_count(conn, fid),
            sector=sectors[fid], events=events,
            externally_managed=_tri_state(r.fund.get("externally_managed")),
            fee_on_gross_assets=_tri_state(r.fund.get("fee_on_gross_assets")),
        )

    _apply_windup_policy(cfg, results)
    persist(conn, results, as_of)
    return results


def _apply_stated_returns(conn, fund_id: str, rs, cfg) -> None:
    """Fill return windows we could not compute with the publisher's own figures.

    The ASX monthly report publishes 1/3/5-year total returns, and its archive
    only reaches back about two years — so for most funds the 5-year window is
    available as a *stated* number and not as a computable one. The spec
    anticipates exactly this ("store the stated 5y/10y total return where the
    raw series is unobtainable, tagged source=stated vs source=computed"), and
    its real rule is that the two never share a column. They don't: every window
    carries its own source, which the CSV and HTML both print.

    Computed always wins where it exists; this only fills holes.
    """
    if not cfg.get("run.allow_stated_returns_for_ranking", True):
        return
    rows = {r["metric"]: r["value"] for r in conn.execute(
        "SELECT metric, value FROM derived_metrics "
        "WHERE fund_id=? AND provenance='stated' AND value IS NOT NULL", (fund_id,))}
    stated5 = rows.get("stated_total_return_5y")
    if rs.r5 is None and stated5 is not None:
        rs.r5 = stated5
        rs.r5_source = "stated"
        rs.warnings.append(
            "5y return is the publisher's stated figure, not recomputed here "
            "(the ASX archive is shallower than its performance table)")
    rs.mark_sources(cfg.num("run.min_years_history"))


def _tri_state(v):
    """SQLite has no boolean: NULL means unknown and must stay unknown, not
    become False. The governance component renormalises on None."""
    return None if v is None else bool(v)


def _apply_windup_policy(cfg, results: Dict[str, FundResult]) -> None:
    """The wind-up scenario is reported only for the top decile of activist
    score, per spec — it presumes a campaign that most funds will never see."""
    if not cfg.get("forward_return.windup.top_decile_only"):
        return
    scored = sorted(
        (r.activist.total for r in results.values() if r.activist.total is not None),
        reverse=True)
    if not scored:
        for r in results.values():
            r.forward.windup_scenario = None
        return
    cutoff_idx = max(0, int(len(scored) * 0.10) - 1)
    cutoff = scored[cutoff_idx]
    for r in results.values():
        if r.activist.total is None or r.activist.total < cutoff:
            r.forward.windup_scenario = None


def persist(conn, results: Dict[str, FundResult], as_of: str) -> None:
    now = utcnow_iso()
    for fid, r in results.items():
        prov = r.returns.provenance if r.returns else "unavailable"
        for metric, value, provenance in [
            ("nta_total_return_5y", r.returns.r5, r.returns.r5_source or prov),
            ("nta_total_return_10y", r.returns.r10, r.returns.r10_source or prov),
            ("nta_total_return_since_inception", r.returns.r_all,
             r.returns.r_all_source or prov),
            ("nta_history_years", r.returns.n_years, prov),
            ("discount_current", r.discounts.current, "computed"),
            ("discount_mean_5y", r.discounts.mean_5y, "computed"),
            ("discount_mean_10y", r.discounts.mean_10y, "computed"),
            ("discount_mean_all", r.discounts.mean_all, "computed"),
            ("discount_stdev_5y", r.discounts.stdev_5y, "computed"),
            ("discount_zscore", r.discounts.z_score, "computed"),
            ("discount_pct_time_wide", r.discounts.pct_time_wider_than_threshold, "computed"),
            ("trailing_dividend_yield", r.trailing_yield, "computed"),
            ("d_star", r.d_star, "computed"),
        ]:
            db.put_metric(conn, fid, as_of, metric, value,
                          provenance=provenance if value is not None else "unavailable",
                          computed_at=now)

        db.put_score(conn, fid, as_of, "forward_return",
                     r.forward.total if r.forward else None,
                     components=r.forward.components_json() if r.forward else None,
                     computed_at=now)
        db.put_score(conn, fid, as_of, "activist",
                     r.activist.total if r.activist else None,
                     components=r.activist.components_json() if r.activist else None,
                     computed_at=now)
    conn.commit()
