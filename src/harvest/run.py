#!/usr/bin/env python3
"""Collect the closed-end universe from ASX + AIC into one clean table.

    python -m src.harvest.run              # writes data/universe.csv
    python -m src.harvest.run --offline    # cached documents only

Two sources in, one uniform table out: stock code, market cap, NTA per share —
plus the provenance needed to check any row against the document it came from.
Everything dropped in cleaning is listed with a reason; nothing vanishes.
"""

import argparse
import csv
import os
import sys

from .. import config, db, fetch
from . import aic, asx
from .record import COLUMNS, clean

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_OUT = os.path.join(ROOT, "data", "universe.csv")
DROPPED_OUT = os.path.join(ROOT, "data", "universe_dropped.csv")


def _write(path, rows, columns):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


def _fmt_money(v, cur):
    if v is None:
        return "—"
    sym = {"AUD": "A$", "GBP": "£", "NZD": "NZ$"}.get(cur, "")
    for unit, div in (("bn", 1e9), ("m", 1e6), ("k", 1e3)):
        if abs(v) >= div:
            return f"{sym}{v / div:,.1f}{unit}"
    return f"{sym}{v:,.0f}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Harvest the closed-end universe")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--source", default="all", choices=["all", "asx", "aic"])
    args = ap.parse_args(argv)

    cfg = config.load()
    conn = db.connect()
    fetcher = fetch.Fetcher(cfg, conn=conn, offline=args.offline)

    raw, infos = [], []
    if args.source in ("all", "asx"):
        recs, info = asx.harvest(fetcher)
        raw += recs
        infos.append(info)
    if args.source in ("all", "aic"):
        recs, info = aic.harvest(fetcher)
        raw += recs
        infos.append(info)

    print("=" * 78)
    print("SOURCES")
    print("=" * 78)
    for i in infos:
        print(f"  {i['source']:34} status={i['status']:12} rows={i['rows']}")
        if i.get("sheet"):
            print(f"      sheet: {i['sheet']}")
        if i.get("url"):
            print(f"      url:   {i['url'][:110]}")
        for w in i["warnings"][:6]:
            print(f"      !  {str(w)[:300]}")

    result = clean(raw)

    print("\n" + "=" * 78)
    print("CLEANED UNIVERSE")
    print("=" * 78)
    print(f"  raw rows in      {len(raw)}")
    print(f"  clean rows out   {len(result.records)}")
    print(f"  dropped          {len(result.dropped)}")

    by_ex = {}
    for r in result.records:
        b = by_ex.setdefault(r.exchange, {"n": 0, "cap": 0, "nta": 0})
        b["n"] += 1
        b["cap"] += r.market_cap is not None
        b["nta"] += r.nta_per_share is not None
        b["isin"] = b.get("isin", 0) + (r.isin is not None)
        b["ta"] = b.get("ta", 0) + (r.total_assets is not None)
    print(f"\n  {'exchange':10}{'funds':>7}{'mkt cap':>10}{'NTA':>7}"
          f"{'tot assets':>12}{'ISIN':>7}")
    for ex, b in sorted(by_ex.items()):
        print(f"  {ex:10}{b['n']:>7}{b['cap']:>10}{b['nta']:>7}"
              f"{b['ta']:>12}{b['isin']:>7}")

    if result.dropped:
        reasons = {}
        for d in result.dropped:
            reasons[d["reason"]] = reasons.get(d["reason"], 0) + 1
        print("\n  dropped by reason:")
        for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"    {n:>5}  {reason}")

    # A look at the actual table, biggest first — the point of the exercise.
    print("\n" + "=" * 78)
    print("SAMPLE — 25 largest by market cap")
    print("=" * 78)
    print(f"  {'code':7}{'exch':6}{'name':34}{'mkt cap':>11}{'tot assets':>12}"
          f"{'NTA':>10}  {'sector':18}")
    ranked = sorted(result.records,
                    key=lambda r: -(r.market_cap or 0))[:25]
    for r in ranked:
        nta = f"{r.nta_per_share:,.4f}" if r.nta_per_share is not None else "—"
        print(f"  {r.code:7}{r.exchange:6}{(r.name or '')[:32]:34}"
              f"{_fmt_money(r.market_cap, r.currency):>11}"
              f"{_fmt_money(r.total_assets, r.currency):>12}{nta:>10}  "
              f"{(r.sector or '')[:16]:18}")

    out = _write(args.out, [r.as_row() for r in result.records], COLUMNS)
    dropped = _write(DROPPED_OUT, result.dropped,
                     ["code", "name", "reason", "market_cap", "nta_per_share"])
    print(f"\nwrote {out}")
    print(f"wrote {dropped}")
    print(f"fetch: {fetcher.summary() or 'no network calls'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
