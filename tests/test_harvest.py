#!/usr/bin/env python3
"""The cleaning rules that make two spreadsheets into one uniform table."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.harvest.record import (Record, clean, normalise_code, nta_from,
                                unit_multiplier)


# --- stock codes -----------------------------------------------------------

def test_codes_are_stripped_of_listing_suffixes():
    """The AIC and data vendors attach listing suffixes; ASX does not. One
    uniform code means SMT.L, FCIT LN and LSE:SMT all reduce to the ticker."""
    assert normalise_code("smt.l") == "SMT"
    assert normalise_code("FCIT LN") == "FCIT"
    assert normalise_code("LSE:SMT") == "SMT"
    assert normalise_code("  afi  ") == "AFI"
    assert normalise_code("BRM.NZ") == "BRM"


def test_things_that_are_not_codes_are_rejected():
    for junk in ["", "   ", None, "Total", "3i Group plc",
                 "Global Smaller Companies", "—"]:
        assert normalise_code(junk) is None


# --- money units -----------------------------------------------------------

def test_scale_is_read_from_the_header_not_the_magnitude():
    """A £900m trust reported in thousands and one reported in millions have
    overlapping magnitudes, so guessing from the number is wrong. Read it."""
    assert unit_multiplier("Mkt Cap ($m)#") == 1e6
    assert unit_multiplier("Market capitalisation (£m)") == 1e6
    assert unit_multiplier("Total assets (£'000)") == 1e3
    assert unit_multiplier("Total assets (thousands)") == 1e3
    assert unit_multiplier("Market cap") == 1.0


# --- NAV / NTA units -------------------------------------------------------

def test_pence_navs_are_converted_and_labelled():
    v, unit = nta_from(342.5, "NAV per share (p)", "GBP")
    assert v == pytest.approx(3.425) and unit == "declared_pence"
    v, unit = nta_from(342.5, "NAV pence per share", "GBP")
    assert v == pytest.approx(3.425)


def test_declared_major_units_pass_through():
    v, unit = nta_from(7.63, "NTA Price ($)", "AUD")
    assert v == 7.63 and unit == "declared_major"


def test_an_undeclared_unit_is_kept_but_flagged_never_guessed():
    """The 100x error that looks like a 99% discount. Where the publisher does
    not state the unit we keep the number and mark the assumption, rather than
    inferring one from magnitude and burying it."""
    v, unit = nta_from(342.5, "NAV", "GBP")
    assert v == 342.5
    assert unit == "assumed_major"


# --- cleaning --------------------------------------------------------------

def _rec(**kw):
    base = dict(code="ABC", exchange="ASX", name="Alpha LIC",
                market_cap=1e8, nta_per_share=1.50)
    base.update(kw)
    return Record(**base)


def test_a_row_needs_a_code_and_at_least_one_figure():
    res = clean([
        _rec(),                                              # keep
        _rec(code="Total", name="Total"),                    # not a code
        _rec(code="DEF", market_cap=None, nta_per_share=None),  # no figures
        _rec(code="GHI", market_cap=None),                   # NTA only -> keep
        _rec(code="JKL", nta_per_share=None),                # cap only -> keep
    ])
    assert sorted(r.code for r in res.records) == ["ABC", "GHI", "JKL"]
    assert len(res.dropped) == 2
    assert all(d["reason"] for d in res.dropped)


def test_benchmark_and_aggregate_rows_are_dropped():
    res = clean([
        _rec(code="XSOAI", name="S&P/ASX Small Ordinaries Accumulation Index"),
        _rec(code="XJOAI", name="S&P/ASX 200 Accumulation Index"),
        _rec(code="TOT", name="Total"),
        _rec(code="AFI", name="Australian Foundation Investment Company"),
    ])
    assert [r.code for r in res.records] == ["AFI"]


def test_an_implausible_nta_is_discarded_without_losing_the_row():
    """A bad NAV should cost the NAV, not the fund — the market cap is still
    good data."""
    res = clean([_rec(code="MNO", nta_per_share=-12.5, market_cap=2e8)])
    assert len(res.records) == 1
    assert res.records[0].nta_per_share is None
    assert res.records[0].market_cap == 2e8
    assert any("plausible per-share" in d["reason"] for d in res.dropped)


def test_the_same_vehicle_from_two_sources_keeps_the_richer_row():
    thin = _rec(code="SMT", exchange="LSE", market_cap=1e9,
                nta_per_share=None, price=None)
    rich = _rec(code="SMT.L", exchange="LSE", market_cap=1e9,
                nta_per_share=10.5, price=9.8)
    res = clean([thin, rich])
    assert len(res.records) == 1
    assert res.records[0].nta_per_share == 10.5


def test_output_is_sorted_and_deduplicated():
    res = clean([_rec(code="ZZZ"), _rec(code="AAA"), _rec(code="AAA")])
    assert [r.code for r in res.records] == ["AAA", "ZZZ"]


def test_a_company_name_is_never_turned_into_a_ticker():
    """The AIC sheet may have no ticker column at all. Borrowing the name would
    mint codes that do not exist — 3i Group's TIDM is III, not 3I — so a
    multi-word string is refused outright and the row is dropped with a reason.
    A genuine vendor suffix is still allowed through."""
    assert normalise_code("3i Group plc") is None
    assert normalise_code("Scottish Mortgage Investment Trust") is None
    assert normalise_code("Alpha LIC") is None
    assert normalise_code("FCIT LN") == "FCIT"      # vendor suffix, kept
    assert normalise_code("SMT.L") == "SMT"


def test_a_negative_nta_is_treated_as_a_wrong_column_not_a_small_number():
    """The failure that reached a live ranking once already: a discount column
    read as the NAV series. Magnitude is not the test — sign is."""
    res = clean([_rec(code="PQR", nta_per_share=-0.21, market_cap=3e8)])
    assert res.records[0].nta_per_share is None
    assert res.records[0].market_cap == 3e8


def test_total_assets_is_never_reported_as_market_cap():
    """What the market pays and what the fund owns are different numbers, and
    the gap between them is the discount. Collapsing one into the other would
    make a trust on a 30% discount look fairly priced."""
    r = Record(code="HICL", exchange="LSE", name="HICL Infrastructure",
               market_cap=None, nta_total=3.0e9, nta_per_share=None)
    res = clean([r])
    assert len(res.records) == 1                 # kept: the NTA total is data
    assert res.records[0].market_cap is None     # but not pretended to be cap
    assert res.records[0].nta_total == 3.0e9


def test_a_row_with_only_an_nta_total_still_survives():
    res = clean([Record(code="XYZ", exchange="LSE", name="Some Trust",
                        nta_total=5e8)])
    assert [r.code for r in res.records] == ["XYZ"]


def test_discount_is_derived_from_aggregates_when_no_per_share_nav_exists():
    """The share count cancels, so aggregate figures give the same discount as
    per-share ones:  mcap / nta_total - 1  ==  price / nav - 1.
    A trust at GBP900m market cap against GBP1.0bn of assets is on -10%."""
    res = clean([Record(code="SMT", exchange="LSE", name="Scottish Mortgage",
                        market_cap=9.0e8, nta_total=1.0e9)])
    r = res.records[0]
    assert r.discount == pytest.approx(-0.10)
    assert r.discount_basis == "mcap_over_nta_total"


def test_a_published_discount_is_never_overwritten_by_a_derived_one():
    """The ASX publishes its own premium/discount. Where a source states it, we
    keep the source's figure and label it, rather than recomputing."""
    res = clean([Record(code="AFI", exchange="ASX", name="AFIC",
                        market_cap=8.7e9, nta_total=1.0e10,
                        discount=-0.156, discount_basis="published")])
    r = res.records[0]
    assert r.discount == pytest.approx(-0.156)
    assert r.discount_basis == "published"


def test_a_premium_survives_the_derivation():
    """3i trades above NAV. A derivation that only ever produces discounts is
    broken, so the positive case is pinned too."""
    res = clean([Record(code="III", exchange="LSE", name="3i Group",
                        market_cap=2.8e10, nta_total=2.0e10)])
    assert res.records[0].discount == pytest.approx(0.40)


def test_isin_is_carried_through_and_prefers_the_richer_duplicate():
    thin = Record(code="SMT", exchange="LSE", name="Scottish Mortgage",
                  market_cap=1.4e10)
    rich = Record(code="SMT", exchange="LSE", name="Scottish Mortgage",
                  market_cap=1.4e10, isin="GB00BLDYK618", nta_total=1.5e10)
    res = clean([thin, rich])
    assert len(res.records) == 1
    assert res.records[0].isin == "GB00BLDYK618"
