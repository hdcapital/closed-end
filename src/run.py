#!/usr/bin/env python3
"""CLI entry point.

    python -m src.run --phase all
    python -m src.run --phase universe --exchange asx
    python -m src.run --phase prices
    python -m src.run --phase report

Phases are separable on purpose: the monthly job runs everything, the weekly
job refreshes only prices and re-scores, and a human debugging a parser runs
one exchange at a time against the cache.
"""

import argparse
import datetime
import os
import sys

from . import config, db, fetch, pipeline
from .collectors import holders as holders_mod
from .collectors import nta as nta_mod
from .collectors import prices as prices_mod
from .collectors.lake import LakeReader
from .report import build as report_build
from .universe import asx as uni_asx
from .universe import nz as uni_nz
from .universe import uk as uni_uk
from .util import today_utc

PHASES = ["universe", "history", "prices", "holders", "model", "report", "all"]


def _print_stats(label: str, stats: dict) -> None:
    warnings = stats.pop("warnings", []) if isinstance(stats, dict) else []
    body = ", ".join(f"{k}={v}" for k, v in stats.items()
                     if not isinstance(v, (list, dict)))
    print(f"   {label}: {body}")
    for w in warnings[:12]:
        print(f"      ⚠️  {w}")
    if len(warnings) > 12:
        print(f"      ⚠️  ... and {len(warnings) - 12} more")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Closed-end fund / LIC screening engine")
    ap.add_argument("--phase", default="all", choices=PHASES)
    ap.add_argument("--exchange", default="all", choices=["all", "asx", "uk", "nz"])
    ap.add_argument("--config", default=None)
    ap.add_argument("--db", default=None)
    ap.add_argument("--as-of", default=None, help="as-of date, YYYY-MM-DD")
    ap.add_argument("--offline", action="store_true",
                    help="use only cached documents; never hit the network")
    ap.add_argument("--lake-days", type=int, default=400,
                    help="days of sibling-lake history to scan for NTA/holder filings")
    ap.add_argument("--max-funds", type=int, default=None,
                    help="cap funds in the price phase (cheap manual tests)")
    args = ap.parse_args(argv)

    cfg = config.load(args.config)
    conn = db.connect(args.db)
    fetcher = fetch.Fetcher(cfg, conn=conn, offline=args.offline)
    as_of = args.as_of or today_utc()
    run_meta = {"blocked_sources": [], "source_counts": {}}

    do = lambda p: args.phase in (p, "all")   # noqa: E731

    # -- Phase 1: universe ---------------------------------------------------
    if do("universe"):
        print("== Phase 1: universe ==")
        if args.exchange in ("all", "asx"):
            s = uni_asx.build(conn, fetcher, cfg)
            _note_blocked(run_meta, "ASX monthly investment products report", s)
            _print_stats("ASX", s)
        if args.exchange in ("all", "uk"):
            s = uni_uk.build(conn, fetcher, cfg)
            _note_blocked(run_meta, "LSE instrument list", s)
            _print_stats("LSE", s)
            s2 = uni_uk.cross_check_aic(conn, fetcher, cfg)
            _note_blocked(run_meta, "AIC fund list", s2)
            _print_stats("AIC cross-check", s2)
        if args.exchange in ("all", "nz"):
            _print_stats("NZX", uni_nz.build(conn, fetcher, cfg))

    # -- Phase 2: NTA history ------------------------------------------------
    if do("history"):
        print("== Phase 2: NTA history ==")
        if args.exchange in ("all", "asx"):
            urls = uni_asx.archived_report_urls(fetcher, cfg)
            print(f"   ASX archive: {len(urls)} report link(s) discovered")
            if urls:
                _print_stats("ASX archive panel",
                             nta_mod.from_asx_archive(conn, fetcher, cfg, urls))
            else:
                run_meta["blocked_sources"].append(
                    "ASX archived monthly reports (no links discoverable)")
        reader = LakeReader()
        if reader.status.available:
            start = (datetime.date.fromisoformat(as_of)
                     - datetime.timedelta(days=args.lake_days)).isoformat()
            for market in (["asx"] if args.exchange == "asx" else
                           ["uk"] if args.exchange == "uk" else ["asx", "uk"]):
                _print_stats(f"lake NTA ({market})",
                             nta_mod.from_lake(conn, cfg, market, start, as_of, reader))
        else:
            print(f"   sibling lake unavailable: {reader.status.reason}")
            run_meta["blocked_sources"].append(
                f"market-ingestion lake ({reader.status.reason})")

    # -- Phase 2b: prices ----------------------------------------------------
    if do("prices"):
        print("== Phase 2b: prices ==")
        funds = pipeline.live_funds(conn)
        if args.exchange != "all":
            want = {"asx": "ASX", "uk": "LSE", "nz": "NZX"}[args.exchange]
            funds = [f for f in funds if f["exchange"] == want]
        if args.max_funds:
            funds = funds[:args.max_funds]
        _print_stats("prices", prices_mod.collect(conn, cfg, funds, offline=args.offline))

    # -- Phase 4 inputs: register -------------------------------------------
    if do("holders"):
        print("== Phase 4 inputs: register ==")
        reader = LakeReader()
        if reader.status.available:
            start = (datetime.date.fromisoformat(as_of)
                     - datetime.timedelta(days=args.lake_days)).isoformat()
            for market in ("asx", "uk"):
                _print_stats(f"lake holders ({market})",
                             holders_mod.from_lake(conn, cfg, market, start, as_of, reader))
        else:
            print(f"   sibling lake unavailable: {reader.status.reason}")

    # -- Phases 3+4: models --------------------------------------------------
    results = None
    if do("model") or do("report"):
        print("== Phases 3+4: models ==")
        results = pipeline.run(conn, cfg, as_of)
        rankable = sum(1 for r in results.values() if r.forward and r.forward.rankable)
        print(f"   scored {len(results)} live fund(s); {rankable} rankable")

    # -- Phase 5: report -----------------------------------------------------
    if do("report"):
        print("== Phase 5: report ==")
        out_dir = cfg.get("report.output_dir")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out_dir = os.path.join(root, out_dir)
        run_meta["source_counts"] = dict(fetcher.counts)
        csv_path = report_build.write_csv(results, os.path.join(out_dir, "screen.csv"))
        html_path = report_build.write_html(results, os.path.join(out_dir, "screen.html"),
                                            cfg, run_meta)
        print(f"   wrote {csv_path}")
        print(f"   wrote {html_path}")

    print(f"\nfetch summary: {fetcher.summary() or 'no network calls'}")
    if run_meta["blocked_sources"]:
        print("BLOCKED SOURCES (reported, not worked around):")
        for b in run_meta["blocked_sources"]:
            print(f"  - {b}")
    return 0


def _note_blocked(run_meta: dict, label: str, stats: dict) -> None:
    if stats.get("status") in (fetch.BLOCKED, fetch.ROBOTS_DENIED,
                               fetch.HTTP_ERROR, fetch.SKIPPED):
        run_meta["blocked_sources"].append(f"{label} ({stats['status']})")


if __name__ == "__main__":
    sys.exit(main())
