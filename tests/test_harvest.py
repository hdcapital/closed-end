#!/usr/bin/env python3
"""The cleaning rules that make two spreadsheets into one uniform table."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.harvest import aic_mir
from src.harvest.record import (Record, clean, link_by_isin, merge,
                                normalise_code, nta_from, unit_multiplier)


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
    assert r.discount_basis == "mcap_over_gross_assets"


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


# --- the AIC Monthly Information Release -----------------------------------
#
# Layout copied from the real July 2026 file: a banner row above the field
# names, a header block that repeats once per sector, "Name" appearing three
# times in the header, aggregate shareholders' funds hiding behind a column
# labelled "per share", and cp1252 text.

_MIR_BANNER = ["AIC", "Fund", "Share", "MonthEnd", "Code",
               "Total Assets incl CYR", "MonthEnd Price", "Shares",
               "NetGearing", "SHF per share inc CYR", "SHF per share exc CYR",
               "Flags", "Unit1", "Reported Currency"]
_MIR_FIELDS = ["Category", "Name", "Type", "Date", "ISIN",
               "Total Assets incl CYR", "MonthEnd Price", "Number",
               "NetGearing", "SHF per share inc CYR", "SHF per share exc CYR",
               "Flags", "Name", "Reported Currency"]

_MIR_ROWS = [
    # Real figures. NAV/share = 511112477 / 114262507 = 4.4731, which is the
    # 447.31p the AIC states in its own per-share column. Ungeared.
    ["Asia Pacific", "Pacific Assets Trust", "Ordinary Share", "31/07/2026",
     "GB0006674385", "511112477", "412", "114262507", "0", "511112477",
     "506177031", "", "", "GBX"],
    # The header block, back again a few sectors later.
    _MIR_FIELDS,
    # Geared and on a premium: net NAV GBP 2.00 against a 206p price is +3%,
    # while gross assets of 212m against a 206m market cap read -2.8%.
    ["UK Equity Income", "Geared Income Trust", "Ordinary Share", "31/07/2026",
     "GB0001990497", "212000000", "206", "100000000", "6", "200000000",
     "198000000", "", "", "GBX"],
    # cp1252: an en dash in the name, which utf-8 cannot decode.
    ["Global", "Abrdn – Global Trust", "Ordinary Share", "31/07/2026",
     "GB00B0CNHZ21", "300000000", "250", "100000000", "0", "250000000",
     "249000000", "", "", "GBX"],
    # Not ordinary equity: its own economics, kept out of a discount screen.
    ["UK Smaller", "Aberforth ZDP", "Zero Dividend Preference share",
     "31/07/2026", "GB00B4ZQRQ81", "50000000", "120", "40000000", "0",
     "48000000", "48000000", "", "", "GBX"],
    # One of the 145 rows in the real file that carry a name and an ISIN and
    # nothing else at all.
    ["Biotechnology & Healthcare", "Syncona", "Ordinary Share", "31/07/2026",
     "GB00BYSRYT02", "", "", "", "", "", "", "", "", ""],
]


class _Doc:
    def __init__(self, content):
        self.content, self.ok, self.status, self.detail = content, True, 200, None
        self.text = content.decode("cp1252")


class _Fetcher:
    def __init__(self, content):
        self.content = content

    def get(self, url, kind=None):
        return _Doc(self.content)


def _mir_fixture():
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerows([_MIR_BANNER, _MIR_FIELDS] + _MIR_ROWS)
    return _Fetcher(buf.getvalue().encode("cp1252"))


def _mir_harvest():
    recs, info = aic_mir.harvest(_mir_fixture(), url="http://x/mir.csv")
    return {r.name: r for r in recs}, info


def test_mir_reads_past_the_repeated_header_block():
    """~45 of the real file's 327 rows are the header saying itself again."""
    by_name, info = _mir_harvest()
    assert info["header_repeats"] == 1
    assert "Name" not in by_name and "Category" not in by_name


def test_mir_nav_per_share_is_aggregate_shf_divided_by_shares():
    """The column is labelled "SHF per share" and holds the aggregate. Taking
    the label at its word gives a NAV per share in the hundreds of millions and
    a discount of -100%."""
    by_name, _ = _mir_harvest()
    pat = by_name["Pacific Assets Trust"]
    assert pat.nta_per_share == pytest.approx(4.4731, abs=1e-4)
    assert pat.nta_total == 511112477
    assert pat.discount == pytest.approx(4.12 / (511112477 / 114262507) - 1)


def test_a_net_discount_flips_the_sign_a_gross_one_gets_wrong():
    """The whole reason for reading this file. A trust geared 6% and trading
    3% above net asset value reads as a 2.8% discount off gross assets — the
    error is the gearing, and it changes cheap into expensive."""
    by_name, _ = _mir_harvest()
    r = by_name["Geared Income Trust"]
    assert r.discount == pytest.approx(0.03)
    assert r.discount_basis == "price_over_nav_net"
    gross = (2.06 * 100e6) / 212e6 - 1
    assert gross == pytest.approx(-0.0283, abs=1e-4)


def test_mir_is_decoded_as_cp1252_not_utf8():
    """The file is not valid utf-8; decoding it as such mangles fund names."""
    by_name, _ = _mir_harvest()
    assert "Abrdn – Global Trust" in by_name


def test_mir_leaves_out_share_classes_that_are_not_ordinary():
    by_name, info = _mir_harvest()
    assert "Aberforth ZDP" not in by_name
    assert info["non_ordinary"] == 1


def test_mir_rows_with_no_figures_are_kept_empty_never_filled_in():
    """145 of 282 vehicles in the real file report nothing. They stay blank."""
    by_name, info = _mir_harvest()
    syncona = by_name["Syncona"]
    assert syncona.discount is None and syncona.nta_total is None
    assert syncona.isin == "GB00BYSRYT02"
    assert info["priced"] == 3 and info["incomplete"] == 1


# --- joining the two AIC files ---------------------------------------------

def test_a_code_less_row_takes_the_ticker_its_isin_already_has():
    """The MIR publishes no TIDM. Without the join every one of its rows fails
    the stock-code test and the only exact discount in the project is lost."""
    overview = Record(code="SMT", exchange="LSE", isin="GB00BLDYK618",
                      name="Scottish Mortgage", market_cap=1.3e10)
    mir = Record(code=None, exchange="LSE", isin="GB00BLDYK618",
                 name="Scottish Mortgage", nta_total=1.4e10)
    linked, unlinkable = link_by_isin([overview, mir])
    assert (linked, unlinkable) == (1, 0)
    assert mir.code == "SMT"


def test_an_isin_that_matches_nothing_does_not_get_invented_a_ticker():
    orphan = Record(code=None, exchange="LSE", isin="GB00XXXXXXX9",
                    name="Unlisted Something", nta_total=1e8)
    linked, unlinkable = link_by_isin([orphan])
    assert (linked, unlinkable) == (0, 1)
    assert orphan.code is None
    assert clean([orphan]).records == []


def test_an_exact_net_discount_beats_a_gross_assets_estimate():
    overview = Record(code="CTY", exchange="LSE", isin="GB0001990497",
                      name="City of London", market_cap=2.06e8,
                      nta_total=2.12e8, source="aic")
    mir = Record(code=None, exchange="LSE", isin="GB0001990497",
                 name="City of London", nta_total=2.0e8, nta_per_share=2.00,
                 price=2.06, discount=0.03,
                 discount_basis="price_over_nav_net", source="aic-mir")
    res = clean([overview, mir])
    assert len(res.records) == 1 and res.merged == 1
    r = res.records[0]
    assert r.code == "CTY"
    assert r.discount == pytest.approx(0.03)
    assert r.discount_basis == "price_over_nav_net"
    # The market cap only the overview has still comes along.
    assert r.market_cap == 2.06e8
    assert r.source == "aic-mir+aic"


def test_a_merge_never_mixes_a_net_nav_with_a_gross_one():
    """Filling every empty cell from whichever source has one would leave a row
    quoting a net discount beside the gross asset figure it contradicts."""
    overview = Record(code="HICL", exchange="LSE", isin="GB00B0T4LH64",
                      market_cap=2.0e9, nta_total=3.0e9, source="aic")
    mir = Record(code="HICL", exchange="LSE", isin="GB00B0T4LH64",
                 nta_total=2.2e9, nta_per_share=1.10, price=1.00,
                 discount=-0.0909, discount_basis="price_over_nav_net",
                 source="aic-mir")
    r = merge(overview, mir)
    assert r.nta_total == 2.2e9            # net, matching the discount
    assert r.discount_basis == "price_over_nav_net"


def test_a_source_with_no_discount_at_all_still_lends_its_figures():
    thin = Record(code="AAA", exchange="LSE", market_cap=1e8, source="aic")
    other = Record(code="AAA", exchange="LSE", nta_per_share=2.5,
                   price=2.2, source="aic-mir")
    r = merge(thin, other)
    assert r.market_cap == 1e8 and r.nta_per_share == 2.5 and r.price == 2.2


def test_isin_is_carried_through_and_prefers_the_richer_duplicate():
    thin = Record(code="SMT", exchange="LSE", name="Scottish Mortgage",
                  market_cap=1.4e10)
    rich = Record(code="SMT", exchange="LSE", name="Scottish Mortgage",
                  market_cap=1.4e10, isin="GB00BLDYK618", nta_total=1.5e10)
    res = clean([thin, rich])
    assert len(res.records) == 1
    assert res.records[0].isin == "GB00BLDYK618"
