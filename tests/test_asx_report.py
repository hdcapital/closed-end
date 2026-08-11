#!/usr/bin/env python3
"""ASX monthly report parsing, against the header the live report really has.

The fixture below reproduces the June 2026 header verbatim. It exists because
the first live run mis-mapped three columns at once and produced an NTA series
that was actually the discount column — a failure no synthetic fixture of my
own invention had caught, precisely because I had invented the header.
"""

import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.collectors import asx_monthly as m

# Verbatim from the live June 2026 report.
REAL_HEADER = [
    "ASX Code", "Type", "Fund Name", "MER (% p.a)", "Outperf Fee",
    "Mkt Cap ($m)#", "Mkt Cap ($m) Change", "Transacted Value ($)",
    "Transacted Volume", "Number of Transactions", "Monthly Liquidity %",
    "Prem/Disc % NTA (pre-tax) at N", "NTA Date", "NTA Price", "Last Close",
    "Year High", "Year Low", "Historical Distribution Yield",
    "1 Month Total Return", "1 Year Total Return",
    "3 Year Total Return (ann.)", "5 Year Total Return (ann.)",
]

# AFI: NTA $7.63, close $7.21 (a 5.5% discount), MER 0.15%, no outperformance fee.
ROW_AFI = ["AFI", "Domestic Equity", "Australian Foundation Investment Company",
           0.15, "No", 9450.2, 120.5, 12_345_678, 1_700_000, 4200, 0.9,
           -5.5, "30/06/2026", 7.63, 7.21, 8.10, 6.90, 3.4,
           1.2, 9.8, 7.1, 8.4]
# PE1: a LIT at a wide discount, with a performance fee.
ROW_PE1 = ["PE1", "Private Equity", "Pengana Private Equity Trust",
           1.75, "Yes", 310.4, -5.1, 900_000, 700_000, 300, 0.4,
           -21.3, "30/06/2026", 1.32, 1.04, 1.40, 0.98, 5.1,
           -0.4, 2.2, 4.0, 6.6]


def _workbook(header, rows, sheet="Spotlight LIC List"):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(["ASX Investment Products Monthly Report"])
    ws.append([])
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture(scope="module")
def parsed():
    return m.parse(_workbook(REAL_HEADER, [ROW_AFI, ROW_PE1]),
                   filename="asx-investment-products-jun-2026-abs.xlsx",
                   as_of="2026-06-30")


def test_all_rows_parsed(parsed):
    assert [r.ticker for r in parsed.records] == ["AFI", "PE1"]


def test_nta_is_the_level_not_the_discount(parsed):
    """The regression that motivated this file.

    "Prem/Disc % NTA (pre-tax) at N" contains the substring "nta pre tax", so a
    naive match put -5.5 into the NAV series and produced 100x month-on-month
    "returns". The NTA must be 7.63, and nothing may pick up -5.5 as a level.
    """
    afi = parsed.records[0]
    value, kind = afi.primary_nta
    assert value == 7.63
    assert kind in ("pre_tax", "unspecified")
    assert afi.nta_pre_tax != -5.5 and afi.nta_unspecified != -5.5


def test_price_is_last_close_not_nta_price(parsed):
    """"NTA Price" contains "price"; the tradeable quote is "Last Close"."""
    assert parsed.records[0].price == 7.21
    assert parsed.records[1].price == 1.04


def test_discount_comes_from_the_prem_disc_column(parsed):
    assert parsed.records[0].premium_discount == pytest.approx(-0.055)
    assert parsed.records[1].premium_discount == pytest.approx(-0.213)


def test_recomputed_discount_agrees_with_the_published_one(parsed):
    """Cross-check: price/NTA - 1 should land near the report's own figure.
    If the columns were mis-mapped these two would disagree wildly."""
    for rec in parsed.records:
        nta, _ = rec.primary_nta
        assert rec.price / nta - 1 == pytest.approx(rec.premium_discount, abs=0.01)


def test_market_cap_scaled_from_millions(parsed):
    assert parsed.records[0].market_cap == pytest.approx(9_450_200_000)
    # and never the adjacent "Mkt Cap ($m) Change" column
    assert parsed.records[0].market_cap != pytest.approx(120_500_000)


def test_fee_facts_collected(parsed):
    afi, pe1 = parsed.records
    assert afi.ocr == pytest.approx(0.0015)      # 0.15% p.a.
    assert afi.has_performance_fee is False
    assert pe1.ocr == pytest.approx(0.0175)
    assert pe1.has_performance_fee is True


def test_stated_returns_and_yield_collected(parsed):
    afi = parsed.records[0]
    assert afi.stated_r5y == pytest.approx(0.084)
    assert afi.stated_r3y == pytest.approx(0.071)
    assert afi.dist_yield == pytest.approx(0.034)


def test_mandate_read_from_the_type_column(parsed):
    assert parsed.records[0].mandate == "Domestic Equity"
    assert m.normalise_sector(parsed.records[0].mandate, "") == "equity"
    assert m.normalise_sector(parsed.records[1].mandate, "") == "private_equity"


def test_implausible_level_is_rejected_and_reported():
    """The backstop: even an unanticipated header cannot inject a percentage
    into the NAV series."""
    header = ["ASX Code", "Fund Name", "Some Odd NTA Heading", "Last Close"]
    rows = [["XYZ", "Odd Fund", -12.5, 1.00],     # a discount masquerading as NTA
            ["ABC", "Fine Fund", 2.50, 2.40]]
    res = m.parse(_workbook(header, rows))
    by_ticker = {r.ticker: r for r in res.records}
    assert by_ticker["XYZ"].primary_nta == (None, None)
    assert by_ticker["ABC"].primary_nta[0] == 2.50
    assert any("implausible per-share" in w for w in res.warnings)


def test_column_exclusions_are_honoured_directly():
    from src.tabular import ColumnMap
    cmap = ColumnMap(REAL_HEADER, m.COLUMN_SPEC)
    assert REAL_HEADER[cmap.index["nta"]] == "NTA Price"
    assert REAL_HEADER[cmap.index["price"]] == "Last Close"
    assert REAL_HEADER[cmap.index["market_cap"]] == "Mkt Cap ($m)#"
    assert REAL_HEADER[cmap.index["premium_disc"]] == "Prem/Disc % NTA (pre-tax) at N"
    assert REAL_HEADER[cmap.index["mandate"]] == "Type"
    assert REAL_HEADER[cmap.index["mer"]] == "MER (% p.a)"
    assert REAL_HEADER[cmap.index["ret_5y"]] == "5 Year Total Return (ann.)"
    # The trap column must not be claimed by any NTA *level* field.
    for f in ("nta", "nta_pre_tax", "nta_post_tax"):
        if cmap.has(f):
            assert "Prem/Disc" not in REAL_HEADER[cmap.index[f]]


# ---------------------------------------------------------------------------
# Archive URL construction
# ---------------------------------------------------------------------------

LIVE_URL = ("https://www.asx.com.au/content/dam/asx/issuers/"
            "asx-investment-products-reports/2026/excel/"
            "asx-investment-products-jun-2026-abs.xlsx")


def test_archive_template_substitutes_month_and_year_directory():
    """The landing page links only ~24 months of LIC reports, and most of the
    spreadsheets it does link are the ETF editions. History therefore has to be
    constructed from the pattern of a URL we have seen work."""
    from src.universe.asx import archive_url_template
    build = archive_url_template(LIVE_URL)
    assert build is not None
    assert build(2026, 6) == LIVE_URL
    assert build(2025, 3) == (
        "https://www.asx.com.au/content/dam/asx/issuers/"
        "asx-investment-products-reports/2025/excel/"
        "asx-investment-products-mar-2025-abs.xlsx")
    # December must not roll into the next year's directory.
    assert "/2019/" in build(2019, 12) and "dec-2019" in build(2019, 12)


def test_archive_template_declines_an_unrecognisable_url():
    from src.universe.asx import archive_url_template
    assert archive_url_template("https://example.invalid/report.xlsx") is None
    assert archive_url_template("") is None
    assert archive_url_template(None) is None


def test_constructed_archive_covers_the_requested_depth():
    """84 months requested must reach back seven years, not two."""
    from src.universe.asx import archive_url_template
    import datetime
    build = archive_url_template(LIVE_URL)
    today = datetime.date.today()
    year, month = today.year, today.month
    urls = []
    for _ in range(84):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        urls.append(build(year, month))
    assert len(set(urls)) == 84
    oldest_year = min(int(u.split("-")[-2]) for u in urls)
    assert today.year - oldest_year >= 6
