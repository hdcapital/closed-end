#!/usr/bin/env python3
"""Conservative expected forward return.

    E[annual return] = g_conservative + r_discount + y_income - drag

over a horizon H (default 5 years). The specification is implemented exactly
as given. Three places where I think the maths or the labelling is arguable are
implemented as specified and flagged here rather than quietly "improved" —
see README "Known issues with the model" for the full argument:

1. **It is a total return, not a price return.** `g` is an NTA *total* return
   (distributions reinvested), so the sum estimates what a holder earns
   including distributions received in cash. Calling it a price return and then
   setting y_income = 0 to avoid double counting gets the right number under
   the wrong name. Left as specified: the number is right, the column heading
   in the report says "total return" and the README explains why.

2. **The terms are added, not compounded.** g + r is not (1+g)(1+r) - 1. The
   error is second-order (about 30bp at g=8%, r=4%) and always makes the
   estimate slightly *smaller*, so it is conservative in the intended
   direction. Left as specified.

3. **A missing z-score has no defined damping.** The spec damps positive
   reversion by min(1, |z|/1.5) but does not say what to do when z can't be
   computed (short or flat discount history). Granting full credit there is
   precisely the self-deception the damping exists to prevent, so the default
   damps it to zero — configurable via `z_missing_damping`, and always stated
   as a reason on the fund's decomposition.
"""

import json
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class GrowthEstimate:
    value: Optional[float] = None
    base: Optional[float] = None
    shrunk: Optional[float] = None
    prior: Optional[float] = None
    weight_on_data: Optional[float] = None
    regime: Optional[str] = None            # deteriorating | improving | single_window
    haircut_applied: Optional[float] = None
    capped: bool = False
    uplift_capped: bool = False
    reasons: List[str] = field(default_factory=list)


@dataclass
class DiscountReversion:
    value: Optional[float] = None
    d0: Optional[float] = None
    d_own: Optional[float] = None
    d_peer: Optional[float] = None
    d_star: Optional[float] = None
    d_horizon: Optional[float] = None
    raw_value: Optional[float] = None
    damping: Optional[float] = None
    peer_group: Optional[str] = None
    n_peers: Optional[int] = None
    reasons: List[str] = field(default_factory=list)


@dataclass
class ForwardReturn:
    total: Optional[float] = None
    growth: GrowthEstimate = field(default_factory=GrowthEstimate)
    reversion: DiscountReversion = field(default_factory=DiscountReversion)
    y_income: float = 0.0
    drag: float = 0.0
    horizon_years: float = 5.0
    windup_scenario: Optional[float] = None
    growth_provenance: str = "computed"
    rankable: bool = False
    exclusion_reason: Optional[str] = None
    reasons: List[str] = field(default_factory=list)

    def decomposition(self) -> dict:
        """The audit-by-eye view: every input to the headline number."""
        return {
            "g_conservative": self.growth.value,
            "g_base": self.growth.base,
            "g_shrunk": self.growth.shrunk,
            "g_prior": self.growth.prior,
            "g_weight_on_data": self.growth.weight_on_data,
            "g_regime": self.growth.regime,
            "g_haircut": self.growth.haircut_applied,
            "r_discount": self.reversion.value,
            "r_discount_undamped": self.reversion.raw_value,
            "d0": self.reversion.d0,
            "d_own": self.reversion.d_own,
            "d_peer": self.reversion.d_peer,
            "d_star": self.reversion.d_star,
            "d_horizon": self.reversion.d_horizon,
            "damping": self.reversion.damping,
            "peer_group": self.reversion.peer_group,
            "y_income": self.y_income,
            "drag": self.drag,
            "total": self.total,
            "windup_scenario": self.windup_scenario,
            # Persisted so nothing downstream can present a scored-but-
            # unrankable fund as a recommendation.
            "rankable": self.rankable,
            "exclusion_reason": self.exclusion_reason,
            "growth_provenance": self.growth_provenance,
            "reasons": self.reasons + self.growth.reasons + self.reversion.reasons,
        }

    def components_json(self) -> str:
        return json.dumps(self.decomposition(), sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# 1. Conservative NTA growth
# ---------------------------------------------------------------------------

def conservative_growth(cfg, *, g5, g10, g_all, n_years, sector,
                        ocr=None, has_performance_fee=False) -> GrowthEstimate:
    est = GrowthEstimate()

    # g10 is the long-run anchor; where a fund has no full 10-year window, the
    # since-inception figure stands in for it (it is the longest run available).
    long_run, long_label = (g10, "g10") if g10 is not None else (g_all, "gAll")

    if g5 is not None and long_run is not None:
        if g5 < long_run:
            w = cfg.get("forward_return.deteriorating_weights")
            est.regime = "deteriorating"
            est.base = w["g5"] * g5 + w["g10"] * long_run
            est.reasons.append(
                f"deteriorating: g5={g5:.2%} < {long_label}={long_run:.2%}, "
                f"weighted {w['g5']:.0%}/{w['g10']:.0%} toward the recent decay"
            )
        else:
            w = cfg.get("forward_return.improving_weights")
            est.regime = "improving"
            est.base = w["g5"] * g5 + w["g10"] * long_run
            cap = long_run + cfg.num("forward_return.improving_max_uplift_over_long_run")
            if est.base > cap:
                est.base = cap
                est.uplift_capped = True
                est.reasons.append(
                    f"improving: base capped at {long_label}+"
                    f"{cfg.num('forward_return.improving_max_uplift_over_long_run'):.2%}"
                    " — recent strength is not extrapolated"
                )
            else:
                est.reasons.append(
                    f"improving: g5={g5:.2%} >= {long_label}={long_run:.2%}, "
                    f"anchored {w['g10']:.0%} to the long run"
                )
    elif g5 is not None or long_run is not None:
        est.base = g5 if g5 is not None else long_run
        est.regime = "single_window"
        est.reasons.append(
            f"only one return window available ({'g5' if g5 is not None else long_label})"
        )
    else:
        est.reasons.append("no NTA total-return window available")
        return est

    # Shrink toward the sector prior.
    priors = cfg.get("forward_return.priors")
    est.prior = priors.get(sector, priors.get("unknown"))
    if sector not in priors:
        est.reasons.append(f"sector '{sector}' has no prior; used the 'unknown' prior")
    k = cfg.num("forward_return.shrinkage_k")
    ny = n_years if n_years and n_years > 0 else 0.0
    est.weight_on_data = ny / (ny + k)
    est.shrunk = est.weight_on_data * est.base + (1 - est.weight_on_data) * est.prior

    # Cap, then haircut.
    cap = cfg.num("forward_return.growth_cap")
    value = est.shrunk
    if value > cap:
        value = cap
        est.capped = True
        est.reasons.append(f"growth capped at {cap:.1%}")

    haircut = cfg.num("forward_return.haircut")
    fee_threshold = cfg.num("forward_return.fee_haircut_ocr_threshold")
    fee_applies = (ocr is not None and ocr > fee_threshold) or (
        bool(has_performance_fee) and bool(cfg.get("forward_return.fee_haircut_if_performance_fee"))
    )
    if fee_applies:
        haircut += cfg.num("forward_return.fee_haircut")
        est.reasons.append(
            "extra fee haircut: "
            + (f"OCR {ocr:.2%} > {fee_threshold:.2%}" if (ocr is not None and ocr > fee_threshold)
               else "performance fee present")
        )
    est.haircut_applied = haircut
    est.value = value - haircut
    return est


# ---------------------------------------------------------------------------
# 2. Discount reversion
# ---------------------------------------------------------------------------

def discount_reversion(cfg, *, d0, d_own, d_peer, z_score,
                       horizon_years, peer_group=None, n_peers=None) -> DiscountReversion:
    rev = DiscountReversion(d0=d0, d_own=d_own, d_peer=d_peer,
                            peer_group=peer_group, n_peers=n_peers)
    if d0 is None:
        rev.reasons.append("no current discount — reversion not estimated")
        return rev
    if d0 <= -1.0:
        rev.reasons.append(f"implausible current discount {d0:.2%} (price <= 0) — reversion skipped")
        return rev

    w = cfg.get("forward_return.d_star_weights")
    if d_own is not None and d_peer is not None:
        rev.d_star = w["own"] * d_own + w["peer"] * d_peer
    elif d_own is not None:
        rev.d_star = d_own
        rev.reasons.append("no peer median available — target is the fund's own long-run mean")
    elif d_peer is not None:
        rev.d_star = d_peer
        rev.reasons.append("no own long-run mean available — target is the peer median")
    else:
        rev.reasons.append("neither own history nor peer median available — no reversion")
        return rev

    if d0 < rev.d_star:
        frac = cfg.num("forward_return.reversion_fraction_when_cheap")
        rev.d_horizon = d0 + frac * (rev.d_star - d0)
        rev.reasons.append(
            f"cheaper than target: only {frac:.0%} of the gap assumed to close"
        )
    else:
        frac = cfg.num("forward_return.reversion_fraction_when_tight")
        rev.d_horizon = d0 + frac * (rev.d_star - d0)
        rev.reasons.append(
            f"tighter than target (or at a premium): {frac:.0%} of the gap assumed to close"
        )

    rev.raw_value = ((1.0 + rev.d_horizon) / (1.0 + d0)) ** (1.0 / horizon_years) - 1.0

    # Damp positive reversion by how anomalous the discount is. Negative
    # reversion — the premium deflating — is never damped.
    if rev.raw_value > 0:
        z_full = cfg.num("forward_return.z_full_credit")
        if z_score is None:
            rev.damping = cfg.num("forward_return.z_missing_damping", 0.0)
            rev.reasons.append(
                "discount z-score unavailable (short or flat discount history); "
                f"reversion upside damped to {rev.damping:.0%} — see README"
            )
        else:
            rev.damping = min(1.0, abs(z_score) / z_full)
            if rev.damping < 1.0:
                rev.reasons.append(
                    f"discount only {abs(z_score):.2f}sd from its own 5y mean; "
                    f"upside damped to {rev.damping:.0%}"
                )
        rev.value = rev.raw_value * rev.damping
    else:
        rev.damping = 1.0
        rev.value = rev.raw_value
    return rev


# ---------------------------------------------------------------------------
# 3. Assembly
# ---------------------------------------------------------------------------

def compute(cfg, *, g5, g10, g_all, n_years, sector, d0, d_own, d_peer, z_score,
            ocr=None, has_performance_fee=False, trailing_yield=None,
            peer_group=None, n_peers=None, has_long_window=None,
            growth_provenance="computed") -> ForwardReturn:
    H = cfg.num("run.horizon_years")
    fr = ForwardReturn(horizon_years=H, drag=cfg.num("forward_return.drag"))

    fr.growth = conservative_growth(
        cfg, g5=g5, g10=g10, g_all=g_all, n_years=n_years, sector=sector,
        ocr=ocr, has_performance_fee=has_performance_fee,
    )
    fr.reversion = discount_reversion(
        cfg, d0=d0, d_own=d_own, d_peer=d_peer, z_score=z_score,
        horizon_years=H, peer_group=peer_group, n_peers=n_peers,
    )

    if cfg.get("forward_return.include_yield_in_forward_return"):
        fr.y_income = trailing_yield or 0.0
        fr.reasons.append(
            "income included in the headline by config — check the double-counting "
            "argument in the README before trusting this"
        )
    else:
        fr.y_income = 0.0

    # Rankability: the spec's floor is five years of *data*, not five years of
    # data we recomputed ourselves. A publisher-stated 5-year total return is
    # five years of data; the spec's own rule about it is that stated and
    # computed figures never share a column, which provenance handles. Where no
    # long window exists from any source the fund goes to the appendix.
    min_years = cfg.num("run.min_years_history")
    if has_long_window is None:
        has_long_window = (n_years or 0) >= min_years
    fr.growth_provenance = growth_provenance
    if fr.growth.value is None:
        fr.exclusion_reason = "no NTA total-return estimate"
    elif not has_long_window:
        fr.exclusion_reason = (
            f"only {n_years:.1f}y of NTA history and no stated {min_years:g}y "
            f"return (need {min_years:g}y)"
            if n_years else "no NTA history and no stated long-window return"
        )
    elif fr.reversion.value is None:
        fr.exclusion_reason = "no discount reversion estimate"
    else:
        fr.rankable = True

    if fr.growth.value is not None:
        fr.total = fr.growth.value + (fr.reversion.value or 0.0) + fr.y_income - fr.drag

    fr.windup_scenario = windup_return(cfg, d0=d0, g_conservative=fr.growth.value)
    return fr


def windup_return(cfg, *, d0, g_conservative) -> Optional[float]:
    """Wind-up scenario: full discount capture to the terminal discount over the
    wind-up horizon, plus NAV growth.

        ((1 + terminal) / (1 + d0)) ** (1/3) - 1 + g_conservative

    A scenario, never an expectation: it assumes a campaign both happens and
    succeeds. The report labels it as such and keeps it out of the headline.
    """
    if d0 is None or g_conservative is None or d0 <= -1.0:
        return None
    h = cfg.num("forward_return.windup.horizon_years")
    terminal = cfg.num("forward_return.windup.terminal_discount")
    return ((1.0 + terminal) / (1.0 + d0)) ** (1.0 / h) - 1.0 + g_conservative
