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
from . import aic, aic_mir, asx
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


# Trusts whose discount is well known and stable enough to act as a canary.
# 3i has traded at a large PREMIUM to NAV for years; if it comes out at a deep
# discount, the asset figure is not the net one and the whole UK column is
# biased. City of London sits near par. Scottish Mortgage runs a modest
# discount.
CANARIES = {"III": "historically a large premium",
            "CTY": "historically near par",
            "SMT": "historically a modest discount",
            "FCIT": "historically a modest discount"}


def _discount_sanity(records) -> None:
    """Check the derived UK discounts against known behaviour.

    Deriving a discount from mcap / total assets is exact *if* the asset figure
    is net. If the publisher means gross assets — before gearing — then every
    geared fund looks cheaper than it is, and infrastructure, property and
    private equity look cheapest of all precisely because they gear most. That
    is a systematic bias pointed straight at the sectors a discount screen
    cares about, so it gets checked rather than assumed.
    """
    from ..util import median
    priced = [r for r in records if r.discount is not None]
    if not priced:
        return
    print("\n  discounts by how they were arrived at:")
    print(f"    {'basis':24}{'n':>5}{'p10':>9}{'median':>9}{'p90':>9}"
          f"{'premiums':>10}")
    for basis in sorted({r.discount_basis for r in priced}):
        g = sorted(r.discount for r in priced if r.discount_basis == basis)
        pct = lambda q: g[max(0, min(len(g) - 1, int(len(g) * q) - 1))] * 100
        print(f"    {basis or '—':24}{len(g):>5}{pct(0.10):>8.1f}%"
              f"{median(g) * 100:>8.1f}%{pct(0.90):>8.1f}%"
              f"{sum(1 for d in g if d > 0):>10}")

    print("\n  canaries:")
    for code, expected in CANARIES.items():
        hit = next((r for r in priced if r.code == code), None)
        if hit:
            print(f"    {code:6}{hit.discount * 100:>8.1f}%  "
                  f"{(hit.discount_basis or '—'):24}expected: {expected}")
        else:
            print(f"    {code:6}{'—':>8}   not priced")

    derived = [r for r in priced
               if r.discount_basis == "mcap_over_gross_assets"]
    if not derived:
        return
    # Verdict from the 2026-08-11 run: the AIC figure IS gross. Greencoat UK
    # Wind read -39.9% against a real discount nearer -20%, Pershing Square
    # -43.9%, HarbourVest -30.6% — all heavily geared, all overstated, and the
    # p10 of -52.8% is not a distribution UK trusts actually have. The median
    # test alone missed it (-14.5%), because gearing skews the tail, not the
    # middle. Ungeared equity trusts are unaffected and exact.
    p10 = sorted(r.discount for r in derived)[max(0, int(len(derived) * 0.10) - 1)]
    if p10 < -0.45:
        print(f"    !! p10 = {p10 * 100:.1f}%: too wide to be real. The asset "
              "figure is GROSS of gearing, so geared funds — infrastructure, "
              "property, private equity — read cheaper than they are, by "
              "roughly whatever they borrow. Ungeared trusts are exact.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Harvest the closed-end universe")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--source", default="all",
                    choices=["all", "asx", "aic", "mir"])
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
    # The MIR goes last so its rows can borrow a ticker from the industry
    # overview by ISIN — it publishes no TIDM of its own.
    if args.source in ("all", "mir"):
        recs, info = aic_mir.harvest(fetcher)
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
    print(f"  ISIN-linked      {result.linked} code-less rows given a ticker "
          f"({result.unlinkable} ISINs matched nothing)")
    print(f"  merged           {result.merged} vehicles seen in >1 source")

    by_ex = {}
    for r in result.records:
        b = by_ex.setdefault(r.exchange, {"n": 0, "cap": 0, "nta": 0})
        b["n"] += 1
        b["cap"] += r.market_cap is not None
        b["nta"] += r.nta_per_share is not None
        b["isin"] = b.get("isin", 0) + (r.isin is not None)
        b["ta"] = b.get("ta", 0) + (r.nta_total is not None)
        b["disc"] = b.get("disc", 0) + (r.discount is not None)
    print(f"\n  {'exchange':10}{'funds':>7}{'mkt cap':>10}{'NTA/sh':>8}"
          f"{'NTA total':>11}{'discount':>10}{'ISIN':>7}")
    for ex, b in sorted(by_ex.items()):
        print(f"  {ex:10}{b['n']:>7}{b['cap']:>10}{b['nta']:>8}"
              f"{b['ta']:>11}{b['disc']:>10}{b['isin']:>7}")
    _discount_sanity(result.records)

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
    print(f"  {'code':7}{'exch':6}{'name':32}{'mkt cap':>11}{'NTA total':>12}"
          f"{'NTA/sh':>9}{'disc':>8}  {'basis':22}")
    ranked = sorted(result.records,
                    key=lambda r: -(r.market_cap or 0))[:25]
    for r in ranked:
        nta = f"{r.nta_per_share:,.4f}" if r.nta_per_share is not None else "—"
        disc = f"{r.discount * 100:.1f}%" if r.discount is not None else "—"
        print(f"  {r.code:7}{r.exchange:6}{(r.name or '')[:30]:32}"
              f"{_fmt_money(r.market_cap, r.currency):>11}"
              f"{_fmt_money(r.nta_total, r.currency):>12}{nta:>9}{disc:>8}  "
              f"{(r.discount_basis or '—'):22}")

    out = _write(args.out, [r.as_row() for r in result.records], COLUMNS)
    dropped = _write(DROPPED_OUT, result.dropped,
                     ["code", "name", "reason", "market_cap", "nta_per_share"])
    print(f"\nwrote {out}")
    print(f"wrote {dropped}")
    print(f"fetch: {fetcher.summary() or 'no network calls'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
