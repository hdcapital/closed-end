#!/usr/bin/env python3
"""Activist-target score.

A closed-end fund is a target when (a) the prize is large, (b) the register
makes a campaign winnable, and (c) the endgame is executable. Three pillars,
each 0-100, combined 40/35/25.

**Missing data is renormalised, not defaulted.** If a component can't be
computed it is dropped and the remaining weights in that pillar are rescaled.
Scoring a missing component 0 would punish thin coverage as though it were bad
news; scoring it 50 would invent a fact. Renormalising says only what the
evidence supports — and every result carries a `coverage` fraction, so a score
resting on a third of the evidence is visible as one.

Every pillar keeps the raw evidence that drove it (matched activist names,
insider %, top-20 %), because a score a human can't check is a score a human
shouldn't act on.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..util import clamp, ramp


@dataclass
class PillarScore:
    value: Optional[float] = None
    components: Dict[str, float] = field(default_factory=dict)
    coverage: float = 0.0
    evidence: Dict[str, object] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)


@dataclass
class ActivistScore:
    total: Optional[float] = None
    prize: PillarScore = field(default_factory=PillarScore)
    register: PillarScore = field(default_factory=PillarScore)
    endgame: PillarScore = field(default_factory=PillarScore)
    coverage: float = 0.0

    def decomposition(self) -> dict:
        return {
            "total": self.total,
            "coverage": self.coverage,
            "prize": {"score": self.prize.value, "components": self.prize.components,
                      "coverage": self.prize.coverage, "evidence": self.prize.evidence,
                      "reasons": self.prize.reasons},
            "register": {"score": self.register.value, "components": self.register.components,
                         "coverage": self.register.coverage, "evidence": self.register.evidence,
                         "reasons": self.register.reasons},
            "endgame": {"score": self.endgame.value, "components": self.endgame.components,
                        "coverage": self.endgame.coverage, "evidence": self.endgame.evidence,
                        "reasons": self.endgame.reasons},
        }

    def components_json(self) -> str:
        return json.dumps(self.decomposition(), sort_keys=True, default=str)


def _combine(weights: Dict[str, float], parts: Dict[str, Optional[float]]) -> tuple:
    """Weighted mean over the components that exist. Returns (value, coverage)."""
    usable = {k: v for k, v in parts.items() if v is not None and k in weights}
    total_w = sum(weights[k] for k in usable)
    if total_w <= 0:
        return None, 0.0
    value = sum(weights[k] * usable[k] for k in usable) / total_w
    coverage = total_w / sum(weights.values())
    return clamp(value, 0.0, 100.0), coverage


# ---------------------------------------------------------------------------
# Pillar A — Prize
# ---------------------------------------------------------------------------

def score_prize(cfg, *, d0, d_star, pct_time_wide, market_cap, currency,
                g5, sector_median_g5) -> PillarScore:
    p = PillarScore()
    w = cfg.get("activist.prize.weights")
    parts: Dict[str, Optional[float]] = {}

    # Depth: how far below the target discount the fund trades.
    if d0 is not None and d_star is not None:
        gap = d_star - d0                       # > 0 when cheaper than target
        full = cfg.num("activist.prize.depth_full_credit_gap")
        parts["discount_depth"] = ramp(gap, 0.0, full) * 100.0
        p.evidence["discount_gap_vs_target"] = gap
    else:
        p.reasons.append("discount depth unavailable")

    # Persistence: a discount that has been wide for years is structural, and
    # structural is what makes holders angry enough to vote.
    if pct_time_wide is not None:
        parts["persistence"] = pct_time_wide * 100.0
        p.evidence["pct_time_wider_than_threshold"] = pct_time_wide
    else:
        p.reasons.append("discount persistence unavailable")

    # Size sweet spot.
    if market_cap is not None and currency:
        band = cfg.get("activist.prize.size_sweet_spot").get(currency.upper())
        if band:
            parts["size"] = _size_score(market_cap, band)
            p.evidence["market_cap"] = market_cap
            p.evidence["size_band"] = currency.upper()
        else:
            p.reasons.append(f"no size band configured for currency {currency}")
    else:
        p.reasons.append("market cap unavailable")

    # Underperformance vs sector median weakens the board's defence.
    if g5 is not None and sector_median_g5 is not None:
        behind = sector_median_g5 - g5
        full = cfg.num("activist.prize.underperformance_full_credit")
        parts["underperformance"] = ramp(behind, 0.0, full) * 100.0
        p.evidence["nav_5y_vs_sector_median"] = -behind
    else:
        p.reasons.append("5y NAV vs sector median unavailable")

    p.components = {k: v for k, v in parts.items() if v is not None}
    p.value, p.coverage = _combine(w, parts)
    return p


def _size_score(market_cap: float, band: dict) -> float:
    """0 outside [lo, hi], 100 inside [lo_full, hi_full], linear between.

    Too small and a campaign can't repay its own costs; too big and an activist
    can't accumulate a stake that matters.
    """
    lo, lo_full = float(band["lo"]), float(band["lo_full"])
    hi_full, hi = float(band["hi_full"]), float(band["hi"])
    if market_cap <= lo or market_cap >= hi:
        return 0.0
    if market_cap < lo_full:
        return ramp(market_cap, lo, lo_full) * 100.0
    if market_cap > hi_full:
        return (1.0 - ramp(market_cap, hi_full, hi)) * 100.0
    return 100.0


# ---------------------------------------------------------------------------
# Pillar B — Winnable register
# ---------------------------------------------------------------------------

def _activist_names(cfg, exchange: str) -> List[str]:
    conf = cfg.get("activist.register.known_activists")
    names = list(conf.get("global", []))
    key = {"ASX": "asx", "LSE": "uk", "NZX": "nz"}.get((exchange or "").upper())
    if key and key in conf:
        names += list(conf[key])
    return names


def match_activists(cfg, exchange: str, holders: List[dict]) -> List[dict]:
    """Holders whose name matches the configured activist list.

    Matching is on a normalised substring: registers spell the same manager
    "Saba Capital Management, L.P.", "SABA CAPITAL MANAGEMENT LP" and
    "Saba Capital Management" in three different filings.
    """
    names = _activist_names(cfg, exchange)
    patterns = [(n, re.compile(re.escape(re.sub(r"\s+", " ", n.strip().lower()))))
                for n in names]
    out = []
    for h in holders:
        hn = re.sub(r"[^a-z0-9 ]+", " ", (h.get("holder_name") or "").lower())
        hn = re.sub(r"\s+", " ", hn).strip()
        for label, pat in patterns:
            if pat.search(hn):
                out.append({"matched": label, "holder_name": h.get("holder_name"),
                            "pct": h.get("pct"), "date": h.get("date")})
                break
    return out


def score_register(cfg, *, exchange, holders, insider_pct=None,
                   institutional_filing_count=None) -> PillarScore:
    p = PillarScore()
    w = cfg.get("activist.register.weights")
    parts: Dict[str, Optional[float]] = {}
    holders = holders or []

    # 1. Known activist already on the register — the strongest single signal.
    matched = match_activists(cfg, exchange, holders)
    if holders:
        parts["known_activist"] = (
            cfg.num("activist.register.known_activist_score") if matched else 0.0
        )
        p.evidence["matched_activists"] = matched
    else:
        p.reasons.append("no holder data — activist presence unknown")

    # 2. Concentration: fragmented register, no blocking stake.
    pcts = [h["pct"] for h in holders if h.get("pct") is not None]
    if pcts:
        top20 = sum(sorted(pcts, reverse=True)[:20])
        largest = max(pcts)
        conf = cfg.get("activist.register.concentration")
        lo, hi = float(conf["top20_ideal_lo"]), float(conf["top20_ideal_hi"])
        if lo <= top20 <= hi:
            conc = 100.0
        elif top20 < lo:
            conc = ramp(top20, 0.0, lo) * 100.0
        else:
            conc = (1.0 - ramp(top20, hi, 1.0)) * 100.0
        if largest >= float(conf["single_holder_block"]):
            conc -= float(conf["single_holder_penalty"])
            p.reasons.append(
                f"largest holder {largest:.1%} is at or above the "
                f"{float(conf['single_holder_block']):.0%} blocking threshold"
            )
        parts["concentration"] = clamp(conc, 0.0, 100.0)
        p.evidence["top20_pct"] = top20
        p.evidence["largest_holder_pct"] = largest
        p.evidence["n_holders_known"] = len(pcts)
    else:
        p.reasons.append("no holder percentages — concentration unknown")

    # 3. Insider/manager alignment.
    if insider_pct is not None:
        conf = cfg.get("activist.register.insider")
        if insider_pct < float(conf["low_threshold"]):
            parts["insider"] = float(conf["low_score"])
        elif insider_pct <= float(conf["neutral_hi"]):
            parts["insider"] = float(conf["neutral_score"])
        elif insider_pct >= float(conf["block_threshold"]):
            parts["insider"] = float(conf["block_score"])
            p.reasons.append(
                f"insiders hold {insider_pct:.1%} — a blocking stake makes a "
                "campaign near-unwinnable"
            )
        else:
            # Between neutral_hi and block_threshold, ramp down to the block score.
            frac = ramp(insider_pct, float(conf["neutral_hi"]), float(conf["block_threshold"]))
            parts["insider"] = (float(conf["neutral_score"]) * (1 - frac)
                                + float(conf["block_score"]) * frac)
        p.evidence["insider_pct"] = insider_pct
    else:
        p.reasons.append("insider ownership unknown")

    # 4. Retail-heavy registers organise slowly — a mild negative, not a veto.
    if institutional_filing_count is not None:
        threshold = cfg.num("activist.register.retail_filing_count_threshold")
        retail_heavy = institutional_filing_count < threshold
        parts["retail"] = (cfg.num("activist.register.retail_heavy_score") if retail_heavy
                           else cfg.num("activist.register.retail_neutral_score"))
        p.evidence["institutional_filings"] = institutional_filing_count
        if retail_heavy:
            p.reasons.append(
                "few institutional filings implies a retail-heavy register: slower to "
                "organise, though WAM-style campaigns specifically target these"
            )
    else:
        p.reasons.append("institutional filing count unknown")

    p.components = {k: v for k, v in parts.items() if v is not None}
    p.value, p.coverage = _combine(w, parts)
    return p


# ---------------------------------------------------------------------------
# Pillar C — Executable endgame
# ---------------------------------------------------------------------------

def score_endgame(cfg, *, sector, events=None, externally_managed=None,
                  fee_on_gross_assets=None, staggered_board=None,
                  chair_tenure_years=None) -> PillarScore:
    p = PillarScore()
    w = cfg.get("activist.endgame.weights")
    parts: Dict[str, Optional[float]] = {}
    events = events or []

    # 1. Can the portfolio be liquidated near NAV? Where it can't, part of the
    # discount is deserved and the endgame is not really available.
    liq = cfg.get("activist.endgame.liquidity_by_sector")
    if sector in liq:
        parts["liquidity"] = float(liq[sector])
    else:
        parts["liquidity"] = float(liq.get("unknown", 50))
        p.reasons.append(f"sector '{sector}' has no liquidity score; used 'unknown'")
    p.evidence["sector"] = sector

    # 2. Trigger events.
    conf = cfg.get("activist.endgame.triggers")
    present = [e for e in events if e.get("event_type") in conf]
    if events:
        total = sum(float(conf[e["event_type"]]) for e in present)
        parts["triggers"] = clamp(total, 0.0, 100.0)
        p.evidence["triggers"] = [e.get("event_type") for e in present]
    else:
        p.reasons.append(
            "no trigger events collected (continuation votes, manager agreements, "
            "buyback authorities) — this is the thinnest evidence in the score"
        )

    # 3. Governance friction.
    gov_conf = cfg.get("activist.endgame.governance")
    gov_inputs = [externally_managed, fee_on_gross_assets, staggered_board, chair_tenure_years]
    if any(v is not None for v in gov_inputs):
        gov = float(gov_conf["base"])
        if externally_managed and fee_on_gross_assets:
            gov = float(gov_conf["external_manager_gross_assets"])
            p.reasons.append(
                "externally managed on gross assets: entrenched, but a clear villain "
                "for a campaign narrative"
            )
        if staggered_board:
            gov += float(gov_conf["staggered_board"])
        if chair_tenure_years is not None and \
                chair_tenure_years >= float(gov_conf["long_tenured_chair_years"]):
            gov += float(gov_conf["long_tenured_chair_penalty"])
            p.evidence["chair_tenure_years"] = chair_tenure_years
        parts["governance"] = clamp(gov, 0.0, 100.0)
    else:
        p.reasons.append("governance facts unknown")

    p.components = {k: v for k, v in parts.items() if v is not None}
    p.value, p.coverage = _combine(w, parts)
    return p


# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------

def compute(cfg, *, d0, d_star, pct_time_wide, market_cap, currency, g5,
            sector_median_g5, exchange, holders, insider_pct,
            institutional_filing_count, sector, events=None,
            externally_managed=None, fee_on_gross_assets=None,
            staggered_board=None, chair_tenure_years=None) -> ActivistScore:
    s = ActivistScore()
    s.prize = score_prize(cfg, d0=d0, d_star=d_star, pct_time_wide=pct_time_wide,
                          market_cap=market_cap, currency=currency, g5=g5,
                          sector_median_g5=sector_median_g5)
    s.register = score_register(cfg, exchange=exchange, holders=holders,
                                insider_pct=insider_pct,
                                institutional_filing_count=institutional_filing_count)
    s.endgame = score_endgame(cfg, sector=sector, events=events,
                              externally_managed=externally_managed,
                              fee_on_gross_assets=fee_on_gross_assets,
                              staggered_board=staggered_board,
                              chair_tenure_years=chair_tenure_years)

    w = cfg.get("activist.pillar_weights")
    parts = {"prize": s.prize.value, "register": s.register.value,
             "endgame": s.endgame.value}
    s.total, pillar_cov = _combine(w, parts)
    # Overall coverage is the weighted product of pillar presence and each
    # pillar's own internal coverage — a pillar scored off one of four
    # components should not read as fully evidenced.
    weighted = sum(w[k] * getattr(s, k).coverage for k in w if parts.get(k) is not None)
    s.coverage = weighted / sum(w.values()) if pillar_cov else 0.0
    return s
