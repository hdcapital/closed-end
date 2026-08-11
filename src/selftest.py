#!/usr/bin/env python3
"""Offline end-to-end selftest — no network, no S3, no credentials.

Mirrors the sibling `market-ingestion` convention: CI runs this before any
live fetching, so a broken schema or a broken model fails in ten seconds
instead of after a twenty-minute scrape.

It drives the whole path a real run takes — universe row, NTA panel, prices,
register, metrics, both models, CSV and HTML — against synthetic data whose
answers are known.
"""

import os
import shutil
import sys
import tempfile

from . import config, db, pipeline
from .collectors import asx_monthly, nta_text
from .report import build as report_build
from .util import utcnow_iso


def _fixture_workbook() -> bytes:
    """An ASX-monthly-report-shaped spreadsheet: title banner, real header a
    few rows down, footnotes below the data."""
    import io
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "LIC LIT"
    ws.append(["ASX Investment Products Monthly Report"])
    ws.append([])
    ws.append(["ASX Code", "Company Name", "Investment Mandate",
               "Market Capitalisation", "Pre-Tax NTA", "Post-Tax NTA",
               "Share Price", "Premium/Discount"])
    ws.append(["AAA", "Alpha Investment Company", "Australian Equity",
               400_000_000, 2.00, 1.90, 1.60, -20.0])
    ws.append(["BBB", "Beta Private Equity Trust", "Private Equity",
               250_000_000, 1.50, None, 1.20, -20.0])
    ws.append(["ZZZ", "Zeta ETF Trust", "Exchange Traded Fund",
               90_000_000, 1.00, None, 1.00, 0.0])
    ws.append([])
    ws.append(["Source: ASX."])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def main() -> int:
    root = tempfile.mkdtemp(prefix="closed-end-selftest-")
    failures = []

    def check(name, cond):
        print(f"  {'✅' if cond else '❌'} {name}")
        if not cond:
            failures.append(name)

    try:
        cfg = config.load()
        conn = db.connect(os.path.join(root, "t.sqlite"))

        # -- schema ------------------------------------------------------
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        check("schema has every required table",
              {"funds", "nta_observations", "price_observations", "holders",
               "derived_metrics", "scores", "source_log"} <= tables)

        # -- provenance is structural, not optional ----------------------
        try:
            conn.execute("INSERT INTO nta_observations "
                         "(fund_id,date,nta_per_share,nta_type,retrieved_at) "
                         "VALUES ('X','2026-01-01',1.0,'pre_tax','now')")
            untraceable_rejected = False
        except Exception:
            untraceable_rejected = True
        conn.rollback()
        check("a figure with no source is rejected by the schema", untraceable_rejected)

        # -- report parsing ----------------------------------------------
        parsed = asx_monthly.parse(_fixture_workbook(), "report.xlsx", as_of="2026-06-30")
        check("report parser finds the header under the banner", len(parsed.records) == 3)
        check("mandate maps onto the sector taxonomy",
              asx_monthly.normalise_sector("Private Equity", "") == "private_equity")
        check("premium/discount normalised to a fraction",
              abs(parsed.records[0].premium_discount + 0.20) < 1e-9)

        # -- NTA text extraction -----------------------------------------
        ex = nta_text.extract("NTA before tax: $1.7234\nNTA after tax: $1.6501")
        check("pre and post tax NTA extracted separately",
              ex.values.get("pre_tax") == 1.7234 and ex.values.get("post_tax") == 1.6501)
        check("a totals-only announcement yields no per-share figure",
              not nta_text.extract("Net assets were $412,345,678.").ok)

        # -- universe + panel --------------------------------------------
        now = utcnow_iso()
        for ticker, name, sector, mcap in [
                ("AAA", "Alpha Investment Company", "equity", 400_000_000),
                ("BBB", "Beta Private Equity Trust", "private_equity", 250_000_000)]:
            db.upsert_fund(conn, {
                "fund_id": db.fund_id("ASX", ticker), "exchange": "ASX",
                "ticker": ticker, "name": name, "sector": sector, "currency": "AUD",
                "market_cap": mcap, "status": "live", "source": "selftest",
                "retrieved_at": now,
            })
        db.upsert_fund(conn, {
            "fund_id": db.fund_id("ASX", "ZZZ"), "exchange": "ASX", "ticker": "ZZZ",
            "name": "Zeta ETF Trust", "sector": "unknown", "currency": "AUD",
            "status": "excluded", "status_reason": "ETF", "source": "selftest",
            "retrieved_at": now,
        })
        conn.commit()

        # Eight years of month-end NTA and price for both funds. AAA compounds
        # at ~7%/yr and sits at a 20% discount; BBB is flat and also at 20%.
        nta_rows, price_rows = [], []
        for i in range(97):
            year, month = 2018 + i // 12, i % 12 + 1
            date = f"{year}-{month:02d}-28"
            aaa = 1.00 * (1.07 ** (i / 12.0))
            for fid, nta, disc in (("ASX:AAA", aaa, -0.20), ("ASX:BBB", 1.00, -0.20)):
                nta_rows.append({"fund_id": fid, "date": date, "nta_per_share": nta,
                                 "nta_type": "pre_tax", "currency": "AUD",
                                 "source": "selftest", "retrieved_at": now})
                price_rows.append({"fund_id": fid, "date": date,
                                   "close": nta * (1 + disc), "currency": "AUD",
                                   "source": "selftest", "retrieved_at": now})
        db.insert_nta(conn, nta_rows)
        db.insert_prices(conn, price_rows)
        db.insert_holders(conn, [
            {"fund_id": "ASX:AAA", "date": "2026-05-01",
             "holder_name": "Sandon Capital Pty Ltd", "holder_type": "activist",
             "pct": 0.08, "source": "selftest", "retrieved_at": now},
            {"fund_id": "ASX:AAA", "date": "2026-05-01", "holder_name": "Inst A",
             "holder_type": "institution", "pct": 0.12, "source": "selftest",
             "retrieved_at": now},
        ])
        conn.commit()

        # -- pipeline ----------------------------------------------------
        results = pipeline.run(conn, cfg, as_of="2026-01-28")
        check("excluded funds are not scored", "ASX:ZZZ" not in results)
        aaa = results["ASX:AAA"]
        check("5y NTA return recovered from the panel",
              aaa.returns.r5 is not None and abs(aaa.returns.r5 - 0.07) < 0.005)
        check("current discount recovered",
              aaa.discounts.current is not None
              and abs(aaa.discounts.current + 0.20) < 1e-6)
        check("fund with 8y of history is rankable", aaa.forward.rankable)
        check("forward return decomposes to its parts",
              abs(aaa.forward.total
                  - (aaa.forward.growth.value + (aaa.forward.reversion.value or 0)
                     - aaa.forward.drag)) < 1e-12)
        check("a perfectly flat discount withholds the z-score",
              aaa.discounts.z_score is None)
        check("activist score sees the matched activist",
              any(m["matched"] == "Sandon Capital"
                  for m in (aaa.activist.register.evidence.get("matched_activists") or [])))
        check("scores persisted with an auditable decomposition",
              conn.execute("SELECT components FROM scores WHERE fund_id='ASX:AAA' "
                           "AND score_name='forward_return'").fetchone()[0] is not None)

        # -- report ------------------------------------------------------
        out = os.path.join(root, "report")
        csv_path = report_build.write_csv(results, os.path.join(out, "screen.csv"))
        html_path = report_build.write_html(
            results, os.path.join(out, "screen.html"), cfg,
            {"blocked_sources": ["selftest: no live sources"], "source_counts": {}})
        check("CSV written", os.path.getsize(csv_path) > 0)
        with open(html_path, encoding="utf-8") as fh:
            page = fh.read()
        check("HTML written", len(page) > 1000)
        check("HTML surfaces blocked sources above the rankings",
              "Sources unavailable this run" in page)
        check("HTML states the survivorship caveat", "Survivorship" in page)
        check("HTML states the pre/post-tax NTA inconsistency",
              "NTA basis differs by market" in page)

    finally:
        shutil.rmtree(root, ignore_errors=True)

    print(f"\nSELFTEST: {'PASS' if not failures else 'FAIL ' + str(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
