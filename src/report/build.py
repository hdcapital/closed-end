#!/usr/bin/env python3
"""CSV and HTML output.

The HTML is deliberately unglamorous. Its job is to make a number checkable:
every headline forward return is shown next to its decomposition (g, r, drag),
every score next to its pillars and its evidence coverage, and every fund next
to the provenance of the figures behind it. A ranking you cannot audit by eye
is a ranking you should not trade.
"""

import csv
import html
import os
from typing import Dict, List

from ..util import today_utc, utcnow_iso

CSV_COLUMNS = [
    "fund_id", "exchange", "ticker", "name", "sector", "sector_raw", "currency",
    "structure", "market_cap", "status",
    "nta_total_return_5y", "nta_total_return_10y", "nta_total_return_since_inception",
    "nta_history_years", "nta_type", "returns_provenance", "nta_observations",
    "discount_current", "discount_date", "discount_mean_5y", "discount_mean_10y",
    "discount_mean_all", "discount_stdev_5y", "discount_zscore",
    "discount_pct_time_wide", "d_star", "peer_group", "n_peers",
    "trailing_dividend_yield",
    "g_conservative", "g_base", "g_shrunk", "g_prior", "g_weight_on_data",
    "g_regime", "g_haircut", "r_discount", "r_discount_undamped", "damping",
    "drag", "forward_return_total", "rankable", "exclusion_reason",
    "windup_scenario",
    "activist_score", "activist_prize", "activist_register", "activist_endgame",
    "activist_coverage", "matched_activists", "insider_pct", "top20_pct",
    "largest_holder_pct",
    "source_status", "source_url", "warnings",
]


def _row_for(r) -> dict:
    f = r.fund
    fwd = r.forward
    act = r.activist
    dec = fwd.decomposition() if fwd else {}
    reg_ev = act.register.evidence if act else {}
    matched = reg_ev.get("matched_activists") or []

    return {
        "fund_id": f["fund_id"], "exchange": f["exchange"], "ticker": f["ticker"],
        "name": f.get("name"), "sector": f.get("sector"), "sector_raw": f.get("sector_raw"),
        "currency": f.get("currency"), "structure": f.get("structure"),
        "market_cap": f.get("market_cap"), "status": f.get("status"),
        "nta_total_return_5y": r.returns.r5,
        "nta_total_return_10y": r.returns.r10,
        "nta_total_return_since_inception": r.returns.r_all,
        "nta_history_years": r.returns.n_years,
        "nta_type": r.returns.nta_type,
        "returns_provenance": r.returns.provenance,
        "nta_observations": r.returns.n_observations,
        "discount_current": r.discounts.current,
        "discount_date": r.discounts.current_date,
        "discount_mean_5y": r.discounts.mean_5y,
        "discount_mean_10y": r.discounts.mean_10y,
        "discount_mean_all": r.discounts.mean_all,
        "discount_stdev_5y": r.discounts.stdev_5y,
        "discount_zscore": r.discounts.z_score,
        "discount_pct_time_wide": r.discounts.pct_time_wider_than_threshold,
        "d_star": r.d_star,
        "peer_group": dec.get("peer_group"),
        "n_peers": fwd.reversion.n_peers if fwd else None,
        "trailing_dividend_yield": r.trailing_yield,
        "g_conservative": dec.get("g_conservative"), "g_base": dec.get("g_base"),
        "g_shrunk": dec.get("g_shrunk"), "g_prior": dec.get("g_prior"),
        "g_weight_on_data": dec.get("g_weight_on_data"), "g_regime": dec.get("g_regime"),
        "g_haircut": dec.get("g_haircut"), "r_discount": dec.get("r_discount"),
        "r_discount_undamped": dec.get("r_discount_undamped"),
        "damping": dec.get("damping"), "drag": dec.get("drag"),
        "forward_return_total": dec.get("total"),
        "rankable": fwd.rankable if fwd else False,
        "exclusion_reason": fwd.exclusion_reason if fwd else None,
        "windup_scenario": dec.get("windup_scenario"),
        "activist_score": act.total if act else None,
        "activist_prize": act.prize.value if act else None,
        "activist_register": act.register.value if act else None,
        "activist_endgame": act.endgame.value if act else None,
        "activist_coverage": act.coverage if act else None,
        "matched_activists": "; ".join(m["matched"] for m in matched),
        "insider_pct": reg_ev.get("insider_pct"),
        "top20_pct": reg_ev.get("top20_pct"),
        "largest_holder_pct": reg_ev.get("largest_holder_pct"),
        "source_status": f.get("source_status"), "source_url": f.get("source_url"),
        "warnings": " | ".join(r.warnings),
    }


def write_csv(results: Dict[str, object], path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    rows = [_row_for(r) for r in results.values()]
    rows.sort(key=lambda r: (r["forward_return_total"] is None,
                             -(r["forward_return_total"] or 0)))
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def _pct(v, dp=1):
    return "—" if v is None else f"{v * 100:.{dp}f}%"


def _num(v, dp=1):
    return "—" if v is None else f"{v:.{dp}f}"


def _money(v, currency=""):
    if v is None:
        return "—"
    for unit, div in (("bn", 1e9), ("m", 1e6), ("k", 1e3)):
        if abs(v) >= div:
            return f"{currency}{v / div:.2f}{unit}"
    return f"{currency}{v:.0f}"


def _esc(s):
    return html.escape(str(s)) if s is not None else "—"


STYLE = """
body{font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
     margin:0;padding:2rem;background:#fbfbf9;color:#1a1a1a;max-width:1600px}
h1{font-size:1.6rem;margin:0 0 .25rem} h2{font-size:1.15rem;margin:2.5rem 0 .5rem}
.sub{color:#666;margin-bottom:1.5rem}
table{border-collapse:collapse;width:100%;font-size:12.5px;margin-bottom:.5rem}
th,td{padding:5px 8px;border-bottom:1px solid #e6e4de;text-align:right;white-space:nowrap}
th{background:#f2f0ea;text-align:right;font-weight:600;position:sticky;top:0}
th:first-child,td:first-child,th.l,td.l{text-align:left}
tr:hover{background:#f7f5ef}
.neg{color:#a11}.pos{color:#161}.muted{color:#888}
.note{background:#fffbe8;border-left:3px solid #d9a441;padding:.75rem 1rem;margin:1rem 0;font-size:13px}
.warn{background:#fdeeee;border-left:3px solid #b33;padding:.75rem 1rem;margin:1rem 0;font-size:13px}
.wrap{overflow-x:auto}
code{background:#f0eee8;padding:1px 4px;border-radius:3px;font-size:12px}
"""


def write_html(results: Dict[str, object], path: str, cfg, run_meta: dict = None) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    rows = [_row_for(r) for r in results.values()]
    run_meta = run_meta or {}

    rankable = [r for r in rows if r["rankable"] and r["forward_return_total"] is not None]
    excluded = [r for r in rows if not r["rankable"]]

    by_fwd = sorted(rankable, key=lambda r: -r["forward_return_total"])
    by_act = sorted([r for r in rows if r["activist_score"] is not None],
                    key=lambda r: -r["activist_score"])

    n_fwd = int(cfg.num("report.top_forward_return"))
    n_act = int(cfg.num("report.top_activist"))
    min_fwd = cfg.num("report.overlap_min_forward_return")
    min_act = cfg.num("report.overlap_min_activist_score")
    overlap = sorted(
        [r for r in rankable
         if r["forward_return_total"] >= min_fwd
         and (r["activist_score"] or 0) >= min_act],
        key=lambda r: -(r["forward_return_total"] + (r["activist_score"] or 0) / 1000))

    p: List[str] = []
    p.append(f"<!doctype html><meta charset='utf-8'><title>Closed-end screen "
             f"{today_utc()}</title><style>{STYLE}</style>")
    p.append(f"<h1>Closed-end fund &amp; LIC screen</h1>")
    p.append(f"<div class='sub'>Generated {utcnow_iso()} &middot; "
             f"{len(rows)} live funds &middot; {len(rankable)} rankable &middot; "
             f"{len(excluded)} excluded for insufficient data</div>")

    p.append(_headline_caveats(cfg, rows, run_meta))

    # --- 1. Forward return ---------------------------------------------------
    p.append(f"<h2>1. Top {n_fwd} by conservative expected forward return</h2>")
    p.append("<div class='sub'>Decomposition shown so every headline number can be "
             "audited by eye: <code>total = g_conservative + r_discount &minus; drag</code>. "
             "This is an expected <b>total</b> return (NTA growth is a total return, "
             "distributions reinvested); trailing yield is shown separately and is "
             "<b>not</b> added — see the README on double counting.</div>")
    p.append(_table(
        by_fwd[:n_fwd],
        [("Fund", lambda r: f"{_esc(r['ticker'])} <span class='muted'>{_esc(r['exchange'])}</span>", "l"),
         ("Name", lambda r: _esc((r["name"] or "")[:38]), "l"),
         ("Sector", lambda r: _esc(r["sector"]), "l"),
         ("Mkt cap", lambda r: _money(r["market_cap"])),
         ("Disc", lambda r: _cls(r["discount_current"], _pct(r["discount_current"]))),
         ("d*", lambda r: _pct(r["d_star"])),
         ("z", lambda r: _num(r["discount_zscore"], 2)),
         ("g5", lambda r: _pct(r["nta_total_return_5y"])),
         ("g10", lambda r: _pct(r["nta_total_return_10y"])),
         ("g_cons", lambda r: _pct(r["g_conservative"])),
         ("r_disc", lambda r: _pct(r["r_discount"])),
         ("drag", lambda r: _pct(r["drag"])),
         ("<b>Total</b>", lambda r: f"<b>{_cls(r['forward_return_total'], _pct(r['forward_return_total']))}</b>"),
         ("Yield", lambda r: _pct(r["trailing_dividend_yield"])),
         ("Prov", lambda r: _esc(r["returns_provenance"]), "l"),
         ]))

    # --- 2. Activist targets -------------------------------------------------
    p.append(f"<h2>2. Top {n_act} activist targets</h2>")
    p.append("<div class='sub'>Pillars: prize 40% / register 35% / endgame 25%. "
             "<b>Coverage</b> is the share of the evidence that was actually "
             "available — a high score at low coverage is a hypothesis, not a "
             "finding. The wind-up column is a <b>scenario</b> (full discount "
             "capture to &minus;2% of NTA over 3 years), never an expectation.</div>")
    p.append(_table(
        by_act[:n_act],
        [("Fund", lambda r: f"{_esc(r['ticker'])} <span class='muted'>{_esc(r['exchange'])}</span>", "l"),
         ("Name", lambda r: _esc((r["name"] or "")[:34]), "l"),
         ("Sector", lambda r: _esc(r["sector"]), "l"),
         ("Mkt cap", lambda r: _money(r["market_cap"])),
         ("Disc", lambda r: _cls(r["discount_current"], _pct(r["discount_current"]))),
         ("% time &lt;&minus;10%", lambda r: _pct(r["discount_pct_time_wide"], 0)),
         ("<b>Score</b>", lambda r: f"<b>{_num(r['activist_score'])}</b>"),
         ("Prize", lambda r: _num(r["activist_prize"])),
         ("Register", lambda r: _num(r["activist_register"])),
         ("Endgame", lambda r: _num(r["activist_endgame"])),
         ("Cover", lambda r: _pct(r["activist_coverage"], 0)),
         ("Activists on register", lambda r: _esc(r["matched_activists"] or "—"), "l"),
         ("Insider", lambda r: _pct(r["insider_pct"])),
         ("Top20", lambda r: _pct(r["top20_pct"])),
         ("Wind-up", lambda r: _cls(r["windup_scenario"], _pct(r["windup_scenario"]))),
         ]))

    # --- 3. Overlap ----------------------------------------------------------
    p.append("<h2>3. Priority pile — cheap <i>and</i> attackable</h2>")
    p.append(f"<div class='sub'>Forward return &ge; {_pct(min_fwd)} <b>and</b> activist "
             f"score &ge; {min_act:.0f}. {len(overlap)} fund(s) clear both bars.</div>")
    if overlap:
        p.append(_table(
            overlap,
            [("Fund", lambda r: f"{_esc(r['ticker'])} <span class='muted'>{_esc(r['exchange'])}</span>", "l"),
             ("Name", lambda r: _esc((r["name"] or "")[:40]), "l"),
             ("Sector", lambda r: _esc(r["sector"]), "l"),
             ("Disc", lambda r: _cls(r["discount_current"], _pct(r["discount_current"]))),
             ("Fwd return", lambda r: _cls(r["forward_return_total"], _pct(r["forward_return_total"]))),
             ("Activist", lambda r: _num(r["activist_score"])),
             ("Cover", lambda r: _pct(r["activist_coverage"], 0)),
             ("Wind-up", lambda r: _cls(r["windup_scenario"], _pct(r["windup_scenario"]))),
             ("Activists on register", lambda r: _esc(r["matched_activists"] or "—"), "l"),
             ]))
    else:
        p.append("<p class='muted'>Nothing clears both bars this run.</p>")

    # --- 4. Data quality -----------------------------------------------------
    p.append(_data_quality(rows, excluded, run_meta, cfg))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(p))
    return path


def _cls(v, text):
    if v is None:
        return "—"
    return f"<span class='{'neg' if v < 0 else 'pos'}'>{text}</span>"


def _table(rows, columns) -> str:
    if not rows:
        return "<p class='muted'>No rows.</p>"
    head = "".join(f"<th class='{c[2] if len(c) > 2 else ''}'>{c[0]}</th>" for c in columns)
    body = []
    for r in rows:
        cells = "".join(
            f"<td class='{c[2] if len(c) > 2 else ''}'>{c[1](r)}</td>" for c in columns)
        body.append(f"<tr>{cells}</tr>")
    return f"<div class='wrap'><table><thead><tr>{head}</tr></thead><tbody>" \
           + "".join(body) + "</tbody></table></div>"


def _headline_caveats(cfg, rows, run_meta) -> str:
    """Caveats a reader must see before the rankings, not after them."""
    out = []
    blocked = run_meta.get("blocked_sources") or []
    if blocked:
        out.append(
            "<div class='warn'><b>Sources unavailable this run.</b> "
            + _esc("; ".join(blocked)) +
            ". Figures below are computed only from what was reachable; funds "
            "with no data are listed in the appendix, never silently dropped.</div>")

    stated = sum(1 for r in rows if r["returns_provenance"] == "stated")
    if stated:
        out.append(
            f"<div class='note'><b>{stated} fund(s)</b> carry manager/AIC-<i>stated</i> "
            "performance rather than a series recomputed here. The "
            "<code>Prov</code> column marks them; stated and computed figures "
            "are never mixed within a column.</div>")

    out.append(
        "<div class='note'><b>Survivorship.</b> Historical discount and return "
        "averages are computed from funds that still exist. Vehicles wound up "
        "or taken over during the window are absent from those averages, which "
        "flatters both — most closed-end funds die at a discount or via a "
        "premium-closing corporate action, so the bias runs in both directions "
        "and is not estimated here.</div>")

    out.append(
        "<div class='note'><b>NTA basis differs by market.</b> ASX LICs are "
        "carried on <i>pre-tax</i> NTA (the figure the ASX report leads with); "
        "UK trusts on cum-income NAV. Post-tax NTA is stored but never blended "
        "into the same series. Cross-market discount comparisons inherit this "
        "inconsistency — an Australian LIC with large deferred tax looks "
        "cheaper on pre-tax NTA than a UK trust with the same economics.</div>")
    return "".join(out)


def _data_quality(rows, excluded, run_meta, cfg) -> str:
    p = ["<h2>4. Data quality appendix</h2>"]

    prov = {}
    for r in rows:
        prov[r["returns_provenance"]] = prov.get(r["returns_provenance"], 0) + 1
    p.append("<div class='sub'>Provenance counts: " +
             ", ".join(f"<code>{_esc(k)}</code> {v}" for k, v in sorted(prov.items())) +
             "</div>")

    src = run_meta.get("source_counts")
    if src:
        p.append("<div class='sub'>Fetch outcomes: " +
                 ", ".join(f"<code>{_esc(k)}</code> {v}" for k, v in sorted(src.items())) +
                 "</div>")

    p.append(f"<h3 style='font-size:1rem'>Funds excluded from the ranking "
             f"({len(excluded)})</h3>")
    p.append(_table(
        sorted(excluded, key=lambda r: (r["exchange"], r["ticker"]))[:400],
        [("Fund", lambda r: f"{_esc(r['ticker'])} <span class='muted'>{_esc(r['exchange'])}</span>", "l"),
         ("Name", lambda r: _esc((r["name"] or "")[:44]), "l"),
         ("Sector", lambda r: _esc(r["sector"]), "l"),
         ("NTA obs", lambda r: _esc(r["nta_observations"])),
         ("History", lambda r: _num(r["nta_history_years"], 1)),
         ("Reason", lambda r: _esc(r["exclusion_reason"]), "l"),
         ("Source status", lambda r: _esc(r["source_status"]), "l"),
         ]))

    stale = [r for r in rows if r["warnings"]]
    p.append(f"<h3 style='font-size:1rem'>Funds carrying warnings ({len(stale)})</h3>")
    p.append(_table(
        stale[:250],
        [("Fund", lambda r: f"{_esc(r['ticker'])} <span class='muted'>{_esc(r['exchange'])}</span>", "l"),
         ("Warnings", lambda r: _esc(r["warnings"][:300]), "l")]))

    p.append("<div class='sub' style='margin-top:2rem'>Config: "
             f"<code>{_esc(os.path.basename(cfg.path))}</code> &middot; horizon "
             f"{cfg.num('run.horizon_years'):g}y &middot; drag "
             f"{_pct(cfg.num('forward_return.drag'))} &middot; growth cap "
             f"{_pct(cfg.num('forward_return.growth_cap'))} &middot; min history "
             f"{cfg.num('run.min_years_history'):g}y</div>")
    return "".join(p)
