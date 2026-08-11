#!/usr/bin/env python3
"""Returns and discount statistics, against hand-computed fixtures."""

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import config, db
from src.metrics import discounts, returns
from src.util import annualise, stdev, to_float


def approx(x, y, tol=1e-9):
    return x is not None and math.isclose(x, y, rel_tol=0, abs_tol=tol)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.sqlite"))
    db.upsert_fund(c, {
        "fund_id": "ASX:TST", "exchange": "ASX", "ticker": "TST", "name": "Test LIC",
        "sector": "equity", "currency": "AUD", "status": "live",
        "source": "test", "retrieved_at": "2026-01-01T00:00:00Z",
    })
    c.commit()
    return c


@pytest.fixture(scope="module")
def cfg():
    return config.load()


def _nta(fund, date, value, t="pre_tax"):
    return {"fund_id": fund, "date": date, "nta_per_share": value, "nta_type": t,
            "currency": "AUD", "source": "test", "source_url": "http://x",
            "source_status": "ok", "retrieved_at": "2026-01-01T00:00:00Z"}


def _price(fund, date, close, dividend=None):
    return {"fund_id": fund, "date": date, "close": close, "currency": "AUD",
            "volume": 1000, "dividend": dividend, "source": "test",
            "source_url": "http://x", "source_status": "ok",
            "retrieved_at": "2026-01-01T00:00:00Z"}


# ---------------------------------------------------------------------------
# util
# ---------------------------------------------------------------------------

def test_annualise_doubling_over_ten_years():
    """2^(1/10) - 1 = 7.1773463...%"""
    assert approx(annualise(2.0, 10.0), 2 ** 0.1 - 1)


def test_annualise_rejects_wipeout():
    # A NAV that went to zero has no real annualised rate; None, not a crash.
    assert annualise(0.0, 5.0) is None
    assert annualise(-1.0, 5.0) is None


def test_to_float_handles_document_shapes():
    assert to_float("$1,234.50") == 1234.50
    assert to_float("(0.4)") == -0.4          # accounting negative
    assert to_float("12.5%") == 0.125
    assert to_float("n/a") is None
    assert to_float("-") is None
    # A missing value must never come back as 0.0 — a zero NTA and an unknown
    # NTA are different facts.
    assert to_float("") is None


def test_stdev_needs_two_points():
    assert stdev([0.1]) is None
    assert approx(stdev([0.0, 2.0]), math.sqrt(2.0))   # sample sd of {0,2} = √2


# ---------------------------------------------------------------------------
# Total-return index
# ---------------------------------------------------------------------------

def test_total_return_index_reinvests_distributions():
    """NTA 1.00 -> 1.10 with a 0.05 distribution paid in the period.

      growth = (1.10 + 0.05) / 1.00 = 1.15
    """
    series = [{"date": "2025-01-01", "nta": 1.00}, {"date": "2026-01-01", "nta": 1.10}]
    divs = [{"date": "2025-07-01", "dividend": 0.05}]
    idx = returns.build_total_return_index(series, divs)
    assert approx(idx[-1]["index"], 1.15)


def test_distribution_before_first_observation_is_not_credited():
    series = [{"date": "2025-01-01", "nta": 1.00}, {"date": "2026-01-01", "nta": 1.10}]
    divs = [{"date": "2024-06-01", "dividend": 0.50}]
    idx = returns.build_total_return_index(series, divs)
    assert approx(idx[-1]["index"], 1.10)


def test_five_year_return_computed_from_stored_series(conn):
    """NTA doubles over exactly 5 years, no distributions.

      2^(1/5) - 1 = 14.869835...%
    """
    db.insert_nta(conn, [_nta("ASX:TST", "2021-01-01", 1.00),
                         _nta("ASX:TST", "2026-01-01", 2.00)])
    conn.commit()
    rs = returns.compute(conn, "ASX:TST")
    assert rs.nta_type == "pre_tax"
    assert approx(rs.r5, 2 ** 0.2 - 1, tol=1e-3)
    assert approx(rs.r_all, 2 ** 0.2 - 1, tol=1e-3)
    assert rs.r10 is None                 # only 5 years of history exists


def test_short_history_never_fills_a_longer_window(conn):
    """The self-deception this guard exists to prevent: a 5-year label
    computed off 3 years of data."""
    db.insert_nta(conn, [_nta("ASX:TST", "2023-01-01", 1.00),
                         _nta("ASX:TST", "2026-01-01", 1.50)])
    conn.commit()
    rs = returns.compute(conn, "ASX:TST")
    assert rs.r5 is None
    assert rs.r_all is not None


def test_series_never_mixes_pre_and_post_tax(conn):
    db.insert_nta(conn, [
        _nta("ASX:TST", "2021-01-01", 1.00, "pre_tax"),
        _nta("ASX:TST", "2026-01-01", 2.00, "pre_tax"),
        _nta("ASX:TST", "2021-01-01", 0.90, "post_tax"),
        _nta("ASX:TST", "2026-01-01", 1.70, "post_tax"),
    ])
    conn.commit()
    nta_type, series = returns.choose_series(conn, "ASX:TST")
    assert nta_type == "pre_tax"
    assert [s["nta"] for s in series] == [1.00, 2.00]


# ---------------------------------------------------------------------------
# Discounts
# ---------------------------------------------------------------------------

def test_discount_is_price_over_nta_minus_one(conn):
    """price 0.90 against NTA 1.00 is a 10% discount, stored negative."""
    db.insert_nta(conn, [_nta("ASX:TST", "2026-01-31", 1.00)])
    db.insert_prices(conn, [_price("ASX:TST", "2026-02-02", 0.90)])
    conn.commit()
    st = discounts.compute(conn, "ASX:TST", cfg=config.load())
    assert approx(st.current, -0.10)


def test_stale_nta_beyond_the_window_is_not_matched(conn):
    """A price 100 days after the last NTA must not produce a discount:
    the 45-day rule is what keeps a stale NAV from manufacturing one."""
    db.insert_nta(conn, [_nta("ASX:TST", "2025-01-01", 1.00)])
    db.insert_prices(conn, [_price("ASX:TST", "2025-04-30", 0.90)])
    conn.commit()
    st = discounts.compute(conn, "ASX:TST", cfg=config.load())
    assert st.current is None
    assert any("45-day window" in w for w in st.warnings)


def test_discount_never_uses_a_future_nta(conn):
    """Lookahead check: a price dated before any NTA publication yields nothing."""
    db.insert_nta(conn, [_nta("ASX:TST", "2026-02-28", 1.00)])
    db.insert_prices(conn, [_price("ASX:TST", "2026-02-01", 0.80)])
    conn.commit()
    st = discounts.compute(conn, "ASX:TST", cfg=config.load())
    assert st.current is None


def test_discount_means_and_zscore(conn, cfg):
    """Two years of month-end observations alternating -10% and -20%.

      mean = -0.15
      sample sd of an equal split between -0.10 and -0.20 over 24 points:
        each deviates 0.05 from the mean, so
        sd = sqrt(24 * 0.05^2 / 23) = 0.05 * sqrt(24/23) = 0.0510754...
      final point is -0.20 (24th month, even index)
        z = (-0.20 - -0.15) / 0.0510754 = -0.97895...
    """
    ntas, prices = [], []
    for i in range(24):
        year = 2024 + i // 12
        month = i % 12 + 1
        d = f"{year}-{month:02d}-15"
        ntas.append(_nta("ASX:TST", d, 1.00))
        prices.append(_price("ASX:TST", d, 0.90 if i % 2 == 0 else 0.80))
    db.insert_nta(conn, ntas)
    db.insert_prices(conn, prices)
    conn.commit()

    st = discounts.compute(conn, "ASX:TST", cfg)
    assert st.n_observations == 24
    assert approx(st.mean_5y, -0.15, tol=1e-12)
    expected_sd = 0.05 * math.sqrt(24 / 23)
    assert approx(st.stdev_5y, expected_sd, tol=1e-12)
    assert approx(st.current, -0.20, tol=1e-12)
    assert approx(st.z_score, (-0.20 + 0.15) / expected_sd, tol=1e-9)
    # Half the observations sit wider than the -10% persistence threshold.
    assert approx(st.pct_time_wider_than_threshold, 0.5)


def test_zscore_withheld_on_thin_history(conn, cfg):
    """Fewer than 24 observations: a z-score off six points is noise wearing
    a statistic's clothing."""
    for i in range(6):
        d = f"2026-{i + 1:02d}-15"
        db.insert_nta(conn, [_nta("ASX:TST", d, 1.00)])
        db.insert_prices(conn, [_price("ASX:TST", d, 0.85)])
    conn.commit()
    st = discounts.compute(conn, "ASX:TST", cfg)
    assert st.current is not None
    assert st.z_score is None
    assert any("z-score withheld" in w for w in st.warnings)


def test_peer_median_excludes_the_fund_itself():
    """A peer group containing the subject drags its own target toward itself."""
    class S:
        def __init__(self, c):
            self.current = c

    stats = {"A": S(-0.40), "B": S(-0.10), "C": S(-0.20), "D": S(-0.30)}
    sectors = {k: "equity" for k in stats}
    regions = {k: "ASX" for k in stats}
    med, label, n = discounts.peer_median_current(
        stats, "A", "equity", "ASX", sectors, regions, min_peers=3)
    # Peers are B, C, D -> median of (-0.30, -0.20, -0.10) = -0.20
    assert approx(med, -0.20)
    assert n == 3
    assert label == "equity@ASX"


def test_peer_group_falls_back_to_global_when_local_is_thin():
    class S:
        def __init__(self, c):
            self.current = c

    stats = {"A": S(-0.40), "B": S(-0.10), "C": S(-0.20)}
    sectors = {k: "equity" for k in stats}
    regions = {"A": "ASX", "B": "LSE", "C": "LSE"}
    med, label, n = discounts.peer_median_current(
        stats, "A", "equity", "ASX", sectors, regions, min_peers=5)
    assert label == "equity@global"
    assert approx(med, -0.15)      # median of (-0.20, -0.10)
    assert n == 2
