#!/usr/bin/env python3
"""Forward-return model, against fixtures computed by hand.

Every expected value below is worked out longhand in its comment. That is the
point: if a future edit changes a weight, the test fails with a number a human
can re-derive on paper rather than one regenerated from the code it is meant
to be checking.
"""

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import config
from src.models import forward_return as fr


@pytest.fixture(scope="module")
def cfg():
    return config.load()


def approx(x, y, tol=1e-9):
    return math.isclose(x, y, rel_tol=0, abs_tol=tol)


# ---------------------------------------------------------------------------
# Conservative growth
# ---------------------------------------------------------------------------

def test_deteriorating_weights_recent_decay(cfg):
    """g5=4%, g10=8%, 20y history, equity prior 6.5%.

    Hand-computed:
      deteriorating (4% < 8%): base = 0.6(0.04) + 0.4(0.08)
                                    = 0.024 + 0.032 = 0.056
      w = 20/(20+10) = 0.666666...
      shrunk = 0.6666667(0.056) + 0.3333333(0.065)
             = 0.0373333... + 0.0216666... = 0.059
      under the 12% cap; haircut 1.0% (no fee trigger)
      g_conservative = 0.059 - 0.010 = 0.049
    """
    g = fr.conservative_growth(cfg, g5=0.04, g10=0.08, g_all=0.08,
                               n_years=20, sector="equity")
    assert g.regime == "deteriorating"
    assert approx(g.base, 0.056)
    assert approx(g.weight_on_data, 2 / 3)
    assert approx(g.shrunk, 0.059)
    assert approx(g.value, 0.049)


def test_improving_anchors_to_long_run(cfg):
    """g5=9%, g10=7%, 20y history, equity prior 6.5%.

      improving (9% >= 7%): base = 0.3(0.09) + 0.7(0.07)
                                 = 0.027 + 0.049 = 0.076
      uplift cap = g10 + 1% = 0.08; 0.076 < 0.08 so the cap does not bind
      w = 2/3; shrunk = 0.6666667(0.076) + 0.3333333(0.065)
                      = 0.0506667 + 0.0216667 = 0.0723333...
      g_conservative = 0.0723333 - 0.01 = 0.0623333...
    """
    g = fr.conservative_growth(cfg, g5=0.09, g10=0.07, g_all=0.07,
                               n_years=20, sector="equity")
    assert g.regime == "improving"
    assert approx(g.base, 0.076)
    assert not g.uplift_capped
    assert approx(g.shrunk, 0.076 * 2 / 3 + 0.065 / 3)
    assert approx(g.value, 0.076 * 2 / 3 + 0.065 / 3 - 0.01)


def test_improving_uplift_cap_binds(cfg):
    """Hot recent performance must not lift the estimate more than 100bp
    above the long run.

      g5=20%, g10=5%: base would be 0.3(0.20) + 0.7(0.05) = 0.06 + 0.035
                                   = 0.095, which is 4.5pp above g10.
      cap = 0.05 + 0.01 = 0.06  ->  base = 0.06
    """
    g = fr.conservative_growth(cfg, g5=0.20, g10=0.05, g_all=0.05,
                               n_years=20, sector="equity")
    assert g.uplift_capped
    assert approx(g.base, 0.06)


def test_shrinkage_is_half_at_ten_years(cfg):
    """The spec's worked example: 10 years of history weights 50/50 with the prior.

      w = 10/(10+10) = 0.5
      base = deteriorating: 0.6(0.02) + 0.4(0.04) = 0.012 + 0.016 = 0.028
      shrunk = 0.5(0.028) + 0.5(0.065) = 0.014 + 0.0325 = 0.0465
    """
    g = fr.conservative_growth(cfg, g5=0.02, g10=0.04, g_all=0.04,
                               n_years=10, sector="equity")
    assert approx(g.weight_on_data, 0.5)
    assert approx(g.shrunk, 0.0465)


def test_growth_cap_and_fee_haircut(cfg):
    """A high-growth private equity trust with an expensive fee base.

      g5=g10=25%, 30y history, PE prior 7%
      base = improving branch, but the uplift cap binds: 0.25 + 0.01 = 0.26
      w = 30/40 = 0.75
      shrunk = 0.75(0.26) + 0.25(0.07) = 0.195 + 0.0175 = 0.2125
      capped at 0.12
      haircut = 1.0% + 0.5% (OCR 2% > 1.5%) = 1.5%
      g_conservative = 0.12 - 0.015 = 0.105
    """
    g = fr.conservative_growth(cfg, g5=0.25, g10=0.25, g_all=0.25, n_years=30,
                               sector="private_equity", ocr=0.02)
    assert g.capped
    assert approx(g.haircut_applied, 0.015)
    assert approx(g.value, 0.105)


def test_performance_fee_triggers_haircut_even_on_low_ocr(cfg):
    g = fr.conservative_growth(cfg, g5=0.06, g10=0.06, g_all=0.06, n_years=20,
                               sector="equity", ocr=0.008, has_performance_fee=True)
    assert approx(g.haircut_applied, 0.015)


def test_no_history_yields_no_estimate(cfg):
    g = fr.conservative_growth(cfg, g5=None, g10=None, g_all=None,
                               n_years=None, sector="equity")
    assert g.value is None


# ---------------------------------------------------------------------------
# Discount reversion
# ---------------------------------------------------------------------------

def test_cheap_fund_gets_only_half_the_gap(cfg):
    """d0 = -30%, own mean -20%, peer -10%, z = -2.0 (fully anomalous), H = 5.

      d_star = 0.5(-0.20) + 0.5(-0.10) = -0.15
      d0 < d_star, so only half the gap closes:
        d_H = -0.30 + 0.5(-0.15 - -0.30) = -0.30 + 0.075 = -0.225
      r = ((1 - 0.225)/(1 - 0.30))^(1/5) - 1
        = (0.775/0.70)^0.2 - 1 = (1.1071428...)^0.2 - 1
        = 0.020536...
      |z|/1.5 = 1.333 -> damping capped at 1.0, so no damping.
    """
    rev = fr.discount_reversion(cfg, d0=-0.30, d_own=-0.20, d_peer=-0.10,
                                z_score=-2.0, horizon_years=5)
    assert approx(rev.d_star, -0.15)
    assert approx(rev.d_horizon, -0.225)
    expected = (0.775 / 0.70) ** 0.2 - 1
    assert approx(rev.raw_value, expected)
    assert approx(rev.damping, 1.0)
    assert approx(rev.value, expected)


def test_premium_fully_deflates_and_is_never_damped(cfg):
    """A fund at a premium: the whole gap closes, and the negative result is
    never damped no matter how unremarkable the z-score.

      d0 = +10%, own -5%, peer -5% -> d_star = -0.05
      d0 > d_star so the full gap closes: d_H = -0.05
      r = ((0.95)/(1.10))^(1/5) - 1 = (0.8636363...)^0.2 - 1 = -0.028862...
      z = 0.1 would damp a positive result to 6.7%, but must not touch this one.
    """
    rev = fr.discount_reversion(cfg, d0=0.10, d_own=-0.05, d_peer=-0.05,
                                z_score=0.1, horizon_years=5)
    assert approx(rev.d_horizon, -0.05)
    expected = (0.95 / 1.10) ** 0.2 - 1
    assert expected < 0
    assert approx(rev.value, expected)
    assert approx(rev.damping, 1.0)


def test_positive_reversion_damped_by_z(cfg):
    """z = -0.75 -> damping = 0.75/1.5 = 0.5, halving the reversion upside."""
    rev = fr.discount_reversion(cfg, d0=-0.30, d_own=-0.20, d_peer=-0.10,
                                z_score=-0.75, horizon_years=5)
    assert approx(rev.damping, 0.5)
    assert approx(rev.value, rev.raw_value * 0.5)


def test_missing_z_withholds_upside_by_default(cfg):
    rev = fr.discount_reversion(cfg, d0=-0.30, d_own=-0.20, d_peer=-0.10,
                                z_score=None, horizon_years=5)
    assert rev.raw_value > 0
    assert approx(rev.value, 0.0)
    assert any("z-score unavailable" in r for r in rev.reasons)


def test_d_star_blends_own_and_peer_equally(cfg):
    """The point of the 50/50 blend: a trust that once traded at par but whose
    illiquid peers all sit at -25% is not assumed to close to par."""
    rev = fr.discount_reversion(cfg, d0=-0.35, d_own=0.0, d_peer=-0.25,
                                z_score=-3.0, horizon_years=5)
    assert approx(rev.d_star, -0.125)
    assert approx(rev.d_horizon, -0.35 + 0.5 * (-0.125 + 0.35))


def test_reversion_needs_a_target(cfg):
    rev = fr.discount_reversion(cfg, d0=-0.30, d_own=None, d_peer=None,
                                z_score=-2.0, horizon_years=5)
    assert rev.value is None


# ---------------------------------------------------------------------------
# Assembly and the wind-up scenario
# ---------------------------------------------------------------------------

def test_total_is_growth_plus_reversion_minus_drag(cfg):
    """End to end, all numbers from the cases above.

      g_conservative = 0.049      (deteriorating case)
      r_discount     = (0.775/0.70)^0.2 - 1
      y_income       = 0          (excluded from the headline by default)
      drag           = 0.005
      total = 0.049 + r - 0.005
    """
    out = fr.compute(cfg, g5=0.04, g10=0.08, g_all=0.08, n_years=20,
                     sector="equity", d0=-0.30, d_own=-0.20, d_peer=-0.10,
                     z_score=-2.0, trailing_yield=0.05)
    r = (0.775 / 0.70) ** 0.2 - 1
    assert approx(out.growth.value, 0.049)
    assert approx(out.y_income, 0.0)                 # yield excluded by default
    assert approx(out.total, 0.049 + r - 0.005)
    assert out.rankable


def test_short_history_is_excluded_not_ranked(cfg):
    out = fr.compute(cfg, g5=None, g10=None, g_all=0.08, n_years=3,
                     sector="equity", d0=-0.30, d_own=-0.20, d_peer=-0.10,
                     z_score=-2.0)
    assert not out.rankable
    assert "3.0y of NTA history" in out.exclusion_reason


def test_windup_scenario(cfg):
    """d0 = -40%, g_conservative = 5%.

      ((1 - 0.02) / (1 - 0.40))^(1/3) - 1 + 0.05
        = (0.98/0.60)^(1/3) - 1 + 0.05
        = (1.6333...)^0.33333 - 1 + 0.05
        = 0.177435... + 0.05 = 0.227435...
    """
    v = fr.windup_return(cfg, d0=-0.40, g_conservative=0.05)
    assert approx(v, (0.98 / 0.60) ** (1 / 3) - 1 + 0.05)


def test_windup_needs_a_discount(cfg):
    assert fr.windup_return(cfg, d0=None, g_conservative=0.05) is None
