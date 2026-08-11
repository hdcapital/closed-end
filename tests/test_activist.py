#!/usr/bin/env python3
"""Activist-target score, against hand-computed fixtures."""

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import config
from src.models import activist


def approx(x, y, tol=1e-9):
    return x is not None and math.isclose(x, y, rel_tol=0, abs_tol=tol)


@pytest.fixture(scope="module")
def cfg():
    return config.load()


# ---------------------------------------------------------------------------
# Pillar A — Prize
# ---------------------------------------------------------------------------

def test_prize_full_marks(cfg):
    """A maximally attractive prize scores 100 on every component.

      depth: d_star -0.10 vs d0 -0.30 -> gap 0.20 >= 0.15 full credit -> 100
      persistence: 100% of the last 3y wider than -10%          -> 100
      size: A$400m sits inside the AUD [100m, 1bn] plateau       -> 100
      underperformance: 5pp behind the sector >= 3pp full credit -> 100
    """
    p = activist.score_prize(cfg, d0=-0.30, d_star=-0.10, pct_time_wide=1.0,
                             market_cap=400_000_000, currency="AUD",
                             g5=0.02, sector_median_g5=0.07)
    assert approx(p.value, 100.0)
    assert approx(p.coverage, 1.0)


def test_prize_depth_ramp_is_linear(cfg):
    """Half the full-credit gap scores half the points.

      gap = d_star - d0 = -0.10 - -0.175 = 0.075, half of 0.15 -> 50
    """
    p = activist.score_prize(cfg, d0=-0.175, d_star=-0.10, pct_time_wide=None,
                             market_cap=None, currency=None, g5=None,
                             sector_median_g5=None)
    assert approx(p.components["discount_depth"], 50.0)
    # Only one of four components available: 0.35 of the pillar weight.
    assert approx(p.coverage, 0.35)


def test_prize_no_credit_when_tighter_than_target(cfg):
    p = activist.score_prize(cfg, d0=-0.05, d_star=-0.20, pct_time_wide=None,
                             market_cap=None, currency=None, g5=None,
                             sector_median_g5=None)
    assert approx(p.components["discount_depth"], 0.0)


def test_size_sweet_spot_shape(cfg):
    """Too small to repay campaign costs, and too big to accumulate into,
    both score zero; the middle is a plateau."""
    band = cfg.get("activist.prize.size_sweet_spot")["GBP"]
    assert approx(activist._size_score(10_000_000, band), 0.0)      # below lo
    assert approx(activist._size_score(35_000_000, band), 50.0)     # midway up the ramp
    assert approx(activist._size_score(50_000_000, band), 100.0)    # plateau starts
    assert approx(activist._size_score(300_000_000, band), 100.0)   # plateau
    assert approx(activist._size_score(600_000_000, band), 100.0)   # plateau ends
    assert approx(activist._size_score(1_300_000_000, band), 50.0)  # midway down
    assert approx(activist._size_score(2_500_000_000, band), 0.0)   # above hi


def test_outperformer_gets_no_underperformance_credit(cfg):
    """A fund beating its sector hands the board its defence."""
    p = activist.score_prize(cfg, d0=None, d_star=None, pct_time_wide=None,
                             market_cap=None, currency=None,
                             g5=0.12, sector_median_g5=0.07)
    assert approx(p.components["underperformance"], 0.0)


def test_prize_renormalises_when_data_is_missing(cfg):
    """Two of four components present: the score is their weighted mean, not
    a figure dragged toward zero by the missing ones.

      depth 100 (weight 0.35), persistence 50 (weight 0.25)
      value = (0.35*100 + 0.25*50) / 0.60 = (35 + 12.5) / 0.60 = 79.1666...
      coverage = 0.60
    """
    p = activist.score_prize(cfg, d0=-0.30, d_star=-0.10, pct_time_wide=0.5,
                             market_cap=None, currency=None, g5=None,
                             sector_median_g5=None)
    assert approx(p.value, (0.35 * 100 + 0.25 * 50) / 0.60)
    assert approx(p.coverage, 0.60)


# ---------------------------------------------------------------------------
# Pillar B — Register
# ---------------------------------------------------------------------------

def test_known_activist_is_matched_across_filing_spellings(cfg):
    holders = [{"holder_name": "SABA CAPITAL MANAGEMENT, L.P.", "pct": 0.09},
               {"holder_name": "Vanguard Group Inc", "pct": 0.03}]
    matched = activist.match_activists(cfg, "LSE", holders)
    assert len(matched) == 1
    assert matched[0]["matched"] == "Saba Capital"


def test_asx_activist_list_applies_only_to_asx(cfg):
    holders = [{"holder_name": "Wilson Asset Management (International) Pty Ltd", "pct": 0.06}]
    assert len(activist.match_activists(cfg, "ASX", holders)) == 1
    # The ASX-specific list must not be applied to an LSE register.
    assert activist.match_activists(cfg, "LSE", holders) == []


def test_blocking_stake_penalises_concentration(cfg):
    """A family-controlled LIC: top-20 in the ideal band, but one holder at
    35% makes the campaign near-unwinnable.

      top20 = 0.55 sits inside [0.30, 0.60] -> 100
      largest 0.35 >= 0.20 blocking threshold -> minus 60 -> 40
    """
    holders = [{"holder_name": "Founder Family Pty Ltd", "pct": 0.35},
               {"holder_name": "Inst A", "pct": 0.10},
               {"holder_name": "Inst B", "pct": 0.10}]
    p = activist.score_register(cfg, exchange="ASX", holders=holders,
                                insider_pct=None, institutional_filing_count=None)
    assert approx(p.components["concentration"], 40.0)
    assert any("blocking threshold" in r for r in p.reasons)


def test_custodian_line_is_not_a_blocking_stake(cfg):
    """A nominee holds for many unrelated beneficiaries and cannot block
    anything. Counting one as a control block would wrongly rule out a
    perfectly winnable register — the commonest way to misread an ASX top-20.

      top20 = 0.22 + 0.10 + 0.08 = 0.40, inside the ideal band -> 100
      largest *votable* holder is 0.10, below the 20% threshold -> no penalty
    """
    holders = [{"holder_name": "HSBC Custody Nominees", "holder_type": "nominee", "pct": 0.22},
               {"holder_name": "Inst A", "holder_type": "institution", "pct": 0.10},
               {"holder_name": "Inst B", "holder_type": "institution", "pct": 0.08}]
    p = activist.score_register(cfg, exchange="ASX", holders=holders,
                                insider_pct=None, institutional_filing_count=None)
    assert approx(p.components["concentration"], 100.0)
    assert approx(p.evidence["largest_holder_pct"], 0.10)
    # The custodian is still visible in the evidence, just not treated as a block.
    assert approx(p.evidence["largest_including_nominees"], 0.22)
    assert not any("blocking threshold" in r for r in p.reasons)


def test_insider_ownership_bands(cfg):
    def insider(pct):
        return activist.score_register(cfg, exchange="ASX", holders=[],
                                       insider_pct=pct,
                                       institutional_filing_count=None
                                       ).components["insider"]

    assert approx(insider(0.01), 100.0)    # < 2%: nobody entrenched
    assert approx(insider(0.05), 50.0)     # 2-10%: neutral
    assert approx(insider(0.30), 0.0)      # > 25%: blocking
    # Between 10% and 25% the score ramps from neutral down to the block score.
    assert approx(insider(0.175), 25.0)


def test_register_scores_zero_activists_but_only_with_holder_data(cfg):
    """No activist on a register we can see is evidence; no register at all
    is not. The two must not produce the same score."""
    seen = activist.score_register(cfg, exchange="ASX",
                                   holders=[{"holder_name": "Nominee Co", "pct": 0.05}],
                                   insider_pct=None, institutional_filing_count=None)
    assert approx(seen.components["known_activist"], 0.0)

    unseen = activist.score_register(cfg, exchange="ASX", holders=[],
                                     insider_pct=None, institutional_filing_count=None)
    assert "known_activist" not in unseen.components
    assert any("no holder data" in r for r in unseen.reasons)


# ---------------------------------------------------------------------------
# Pillar C — Endgame
# ---------------------------------------------------------------------------

def test_liquidity_separates_listed_equity_from_private_equity(cfg):
    eq = activist.score_endgame(cfg, sector="equity")
    pe = activist.score_endgame(cfg, sector="private_equity")
    assert approx(eq.components["liquidity"], 95.0)
    assert approx(pe.components["liquidity"], 20.0)
    # The whole point: a PE trust's discount is partly deserved because the
    # endgame of liquidating near NAV isn't actually available.
    assert eq.value > pe.value


def test_triggers_accumulate_and_cap(cfg):
    """continuation vote 40 + missed continuation 30 + buyback 20 = 90."""
    events = [{"event_type": "continuation_vote_within_2y"},
              {"event_type": "recent_missed_continuation"},
              {"event_type": "unused_buyback_authority"}]
    p = activist.score_endgame(cfg, sector="equity", events=events)
    assert approx(p.components["triggers"], 90.0)

    everything = [{"event_type": k} for k in cfg.get("activist.endgame.triggers")]
    capped = activist.score_endgame(cfg, sector="equity", events=everything)
    assert approx(capped.components["triggers"], 100.0)


def test_governance_external_manager_on_gross_assets(cfg):
    """Entrenched, but a clear villain: net mildly positive vs the base 50."""
    p = activist.score_endgame(cfg, sector="equity", externally_managed=True,
                               fee_on_gross_assets=True)
    assert approx(p.components["governance"], 60.0)

    friction = activist.score_endgame(cfg, sector="equity", externally_managed=True,
                                      fee_on_gross_assets=True, staggered_board=True,
                                      chair_tenure_years=12)
    # 60 - 15 (staggered) - 10 (long-tenured chair) = 35
    assert approx(friction.components["governance"], 35.0)


# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------

def test_composite_weights_forty_thirty_five_twenty_five(cfg):
    """The ideal target: deep persistent discount, fragmented register with an
    activist already aboard, listed-equity portfolio and a continuation vote.

      prize    = 100 (all four components maxed)
      register = 96: activist matched (100), top-20 = 0.15+0.12+0.10+0.08 = 0.45
                 inside the [0.30, 0.60] ideal band with no holder at or above
                 20% (100), insiders 1% < 2% (100), and 10 institutional
                 filings scoring the *neutral* retail value of 60 —
                 0.45(100) + 0.25(100) + 0.20(100) + 0.10(60)
                 = 45 + 25 + 20 + 6 = 96.
                 The register pillar therefore tops out at 96, not 100: a
                 register that is merely not-retail-heavy earns 60 on that
                 component, never 100, because being institutional is not
                 itself a positive. Deliberate — see README.
      endgame  = 0.45(95 liquidity) + 0.35(40 continuation vote) + 0.20(50 base
                 governance, externally managed but not on gross assets)
               = 42.75 + 14 + 10 = 66.75
      total    = 0.40(100) + 0.35(96) + 0.25(66.75) = 40 + 33.6 + 16.6875
               = 90.2875
    """
    s = activist.compute(
        cfg, d0=-0.30, d_star=-0.10, pct_time_wide=1.0, market_cap=400_000_000,
        currency="AUD", g5=0.02, sector_median_g5=0.07, exchange="ASX",
        holders=[{"holder_name": "Sandon Capital", "pct": 0.15},
                 {"holder_name": "Inst A", "pct": 0.12},
                 {"holder_name": "Inst B", "pct": 0.10},
                 {"holder_name": "Inst C", "pct": 0.08}],
        insider_pct=0.01, institutional_filing_count=10, sector="equity",
        events=[{"event_type": "continuation_vote_within_2y"}],
        externally_managed=True, fee_on_gross_assets=False,
    )
    assert approx(s.prize.value, 100.0)
    assert approx(s.register.value, 96.0)
    assert approx(s.endgame.value, 66.75)
    assert approx(s.total, 90.2875)
    # Full evidence on every component of every pillar.
    assert approx(s.coverage, 1.0)


def test_coverage_reflects_thin_evidence(cfg):
    """A score resting on a fraction of the evidence must say so."""
    s = activist.compute(
        cfg, d0=-0.30, d_star=-0.10, pct_time_wide=None, market_cap=None,
        currency=None, g5=None, sector_median_g5=None, exchange="ASX",
        holders=[], insider_pct=None, institutional_filing_count=None,
        sector="equity", events=[],
    )
    assert s.total is not None
    assert s.coverage < 0.5
    assert s.register.value is None      # nothing at all known about the register
