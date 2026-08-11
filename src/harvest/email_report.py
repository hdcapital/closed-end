#!/usr/bin/env python3
"""Mail the harvest to its owner: a formatted summary plus the spreadsheet.

    python -m src.harvest.email_report --dry-run   # write data/email.html +
                                                   # data/universe.xlsx, send nothing
    python -m src.harvest.email_report             # send via Gmail SMTP

Credentials come from the environment and are never written anywhere:

    GMAIL_USERNAME      the sending Gmail address
    GMAIL_APP_PASSWORD  an app password (Google account > Security > 2-Step
                        Verification > App passwords) — NOT the account password
    MAIL_TO             recipient(s), comma-separated; defaults to GMAIL_USERNAME

The HTML uses inline styles only, because Gmail's renderer strips most of a
<style> block; what survives everywhere is styling carried on the tags
themselves. A plain-text alternative rides along for anything that can't show
HTML at all.
"""

import argparse
import csv
import html
import os
import smtplib
import sys
from datetime import date
from email.message import EmailMessage

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UNIVERSE = os.path.join(ROOT, "data", "universe.csv")
DROPPED = os.path.join(ROOT, "data", "universe_dropped.csv")
XLSX_OUT = os.path.join(ROOT, "data", "universe.xlsx")
HTML_OUT = os.path.join(ROOT, "data", "email.html")

# The exact bases; the gross-assets figure is an estimate and labelled so.
EXACT = ("price_over_nav_net", "published")

# A discount this wide is the report's headline material.
DEEP = -0.20

INK, MUTED, LINE = "#1f2430", "#6b7280", "#e5e7eb"
RED, GREEN = "#b91c1c", "#15803d"


def load_rows(path):
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        for k in ("market_cap", "nta_total", "nta_per_share", "price", "discount"):
            if k in r:
                r[k] = float(r[k]) if r[k] not in (None, "") else None
    return rows


def _pctl(vals, q):
    return vals[max(0, min(len(vals) - 1, int(len(vals) * q) - 1))]


def summarise(rows):
    by_ex = {}
    for r in rows:
        by_ex.setdefault(r["exchange"], []).append(r)
    bases = sorted({r["discount_basis"] for r in rows if r["discount"] is not None})
    by_basis = []
    for b in bases:
        g = sorted(r["discount"] for r in rows
                   if r["discount"] is not None and r["discount_basis"] == b)
        by_basis.append((b, len(g), _pctl(g, .10), _pctl(g, .50), _pctl(g, .90),
                         sum(1 for d in g if d > 0)))
    exact = [r for r in rows
             if r["discount"] is not None and r["discount_basis"] in EXACT]

    # Average and median by market, on the trustworthy bases only — folding in
    # the gross-assets estimates would drag every mean wide by the gearing
    # bias and present it as a market fact.
    def _stats(g):
        d = sorted(x["discount"] for x in g)
        return (len(d), sum(d) / len(d), _pctl(d, .50)) if d else (0, None, None)

    by_market = [(ex, *_stats([r for r in exact if r["exchange"] == ex]))
                 for ex in sorted({r["exchange"] for r in exact})]
    by_market.append(("All", *_stats(exact)))

    return {
        "as_of": date.today().isoformat(),
        "total": len(rows),
        "by_ex": {ex: len(g) for ex, g in sorted(by_ex.items())},
        "n_exact": len(exact),
        "by_basis": by_basis,
        "by_market": by_market,
        # Every discount wider than 20%, on a basis that is actually right; a
        # gross-assets figure would fill this table on its bias alone.
        "deep": sorted((r for r in exact if r["discount"] <= DEEP),
                       key=lambda r: r["discount"]),
    }


def _money(v, cur):
    if v is None:
        return "—"
    sym = {"AUD": "A$", "GBP": "£", "NZD": "NZ$"}.get(cur, "")
    for unit, div in (("bn", 1e9), ("m", 1e6), ("k", 1e3)):
        if abs(v) >= div:
            return f"{sym}{v / div:,.1f}{unit}"
    return f"{sym}{v:,.0f}"


def _pct(v):
    if v is None:
        return "—"
    color = GREEN if v > 0 else (RED if v < -0.001 else INK)
    return f'<span style="color:{color};font-weight:600">{v * 100:+.1f}%</span>'


def _table(headers, rows_html):
    head = "".join(
        f'<th align="left" style="padding:6px 10px;font-size:11px;color:{MUTED};'
        f'text-transform:uppercase;letter-spacing:.04em;border-bottom:2px solid {LINE}">'
        f"{h}</th>" for h in headers)
    return (f'<table cellpadding="0" cellspacing="0" width="100%" '
            f'style="border-collapse:collapse;margin:6px 0 22px">'
            f"<tr>{head}</tr>{rows_html}</table>")


def _td(v, align="left"):
    return (f'<td align="{align}" style="padding:6px 10px;font-size:13px;'
            f'color:{INK};border-bottom:1px solid {LINE}">{v}</td>')


def render_html(s):
    e = html.escape
    stat_cells = "".join(
        f'<td align="center" style="padding:12px 8px">'
        f'<div style="font-size:24px;font-weight:700;color:{INK}">{v}</div>'
        f'<div style="font-size:11px;color:{MUTED};text-transform:uppercase;'
        f'letter-spacing:.05em">{k}</div></td>'
        for k, v in [("funds", s["total"]),
                     *[(f"{ex} funds", n) for ex, n in s["by_ex"].items()],
                     ("exact discounts", s["n_exact"])])

    basis_rows = "".join(
        "<tr>" + _td(f"<code style='font-size:12px'>{e(b)}</code>")
        + _td(n, "right") + _td(_pct(p10), "right") + _td(_pct(med), "right")
        + _td(_pct(p90), "right") + _td(prem, "right") + "</tr>"
        for b, n, p10, med, p90, prem in s["by_basis"])

    market_rows = "".join(
        "<tr>" + _td("<b>All</b>" if ex == "All" else f"<b>{e(ex)}</b>")
        + _td(n, "right") + _td(_pct(avg), "right") + _td(_pct(med), "right")
        + "</tr>"
        for ex, n, avg, med in s["by_market"])

    deep_rows = "".join(
        "<tr>" + _td(f"<b>{e(r['code'])}</b>") + _td(e(r["exchange"]))
        + _td(e((r["name"] or "")[:34]))
        + _td(f'<span style="color:{MUTED}">{e((r["sector"] or "—")[:32])}</span>')
        + _td(_money(r["market_cap"], r["currency"]), "right")
        + _td(_pct(r["discount"]), "right") + "</tr>"
        for r in s["deep"])

    return f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#f3f4f6">
<div style="max-width:680px;margin:0 auto;padding:24px 12px;
  font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif">
  <div style="background:{INK};border-radius:12px 12px 0 0;padding:26px 30px">
    <div style="color:#fff;font-size:20px;font-weight:700">Closed-end universe</div>
    <div style="color:#9ca3af;font-size:13px;margin-top:4px">
      ASX monthly report · AIC industry overview · AIC MIR &nbsp;—&nbsp; {s["as_of"]}</div>
  </div>
  <div style="background:#fff;border:1px solid {LINE};border-top:0;
    border-radius:0 0 12px 12px;padding:22px 30px 28px">

    <table width="100%" cellpadding="0" cellspacing="0"
      style="border-collapse:collapse;margin-bottom:8px"><tr>{stat_cells}</tr></table>

    <div style="font-size:15px;font-weight:700;color:{INK};margin:14px 0 0">
      Discounts, by how they were arrived at</div>
    {_table(["basis", "n", "p10", "median", "p90", "prem"], basis_rows)}

    <div style="font-size:15px;font-weight:700;color:{INK}">
      Average discount by market <span style="font-weight:400;color:{MUTED};
      font-size:12px">(exact &amp; published bases only)</span></div>
    {_table(["market", "n", "average", "median"], market_rows)}

    <div style="font-size:15px;font-weight:700;color:{INK}">
      All discounts wider than 20%
      <span style="font-weight:400;color:{MUTED};font-size:12px">
      ({len(s["deep"])} funds; exact &amp; published bases only)</span></div>
    {_table(["code", "exch", "name", "invests in", "mkt cap", "disc"], deep_rows)}

    <div style="font-size:12px;color:{MUTED};line-height:1.6;border-top:1px solid {LINE};
      padding-top:14px">
      <b>Reading the bases.</b> <code>price_over_nav_net</code> is exact (AIC MIR:
      month-end price against net shareholders' funds). <code>published</code> is the
      ASX's own stated premium/discount. <code>mcap_over_gross_assets</code> is an
      estimate biased wide by gearing — geared funds read cheaper than they are —
      and is excluded from the market averages and the wider-than-20% table for
      that reason.<br>
      The full table, every dropped row and its reason are in the attached
      spreadsheet.
    </div>
  </div>
  <div style="text-align:center;color:#9ca3af;font-size:11px;padding:14px">
    Generated by the harvest pipeline · hdcapital/closed-end</div>
</div>
</body></html>"""


def render_text(s):
    lines = [f"Closed-end universe — {s['as_of']}",
             f"{s['total']} funds ({', '.join(f'{ex} {n}' for ex, n in s['by_ex'].items())}), "
             f"{s['n_exact']} exact discounts", "",
             "Discounts by basis (n, p10, median, p90, premiums):"]
    for b, n, p10, med, p90, prem in s["by_basis"]:
        lines.append(f"  {b:24} {n:>4}  {p10*100:+6.1f}%  {med*100:+6.1f}%  "
                     f"{p90*100:+6.1f}%  {prem}")
    lines += ["", "Average discount by market (exact & published bases):"]
    for ex, n, avg, med in s["by_market"]:
        lines.append(f"  {ex:5} n={n:<4} avg {avg*100:+6.1f}%  median {med*100:+6.1f}%"
                     if n else f"  {ex:5} n=0")
    lines += ["", f"Discounts wider than 20% ({len(s['deep'])} funds):"]
    for r in s["deep"]:
        lines.append(f"  {r['code']:6} {r['exchange']:4} {r['discount']*100:+6.1f}%  "
                     f"{(r['sector'] or '—')[:30]:32} {(r['name'] or '')[:30]}")
    lines += ["", "Full table attached as universe.xlsx"]
    return "\n".join(lines)


def build_email(s, xlsx_path, sender, to):
    # One address or several: "a@x.com, b@y.com" becomes a proper To list.
    recipients = [a.strip() for a in str(to).split(",") if a.strip()]
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = (f"Closed-end universe: {s['total']} funds, "
                      f"{s['n_exact']} exact discounts — {s['as_of']}")
    msg.set_content(render_text(s))
    msg.add_alternative(render_html(s), subtype="html")
    with open(xlsx_path, "rb") as fh:
        msg.add_attachment(
            fh.read(),
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=f"universe-{s['as_of']}.xlsx")
    return msg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Email the harvested universe")
    ap.add_argument("--dry-run", action="store_true",
                    help="write data/email.html + data/universe.xlsx, send nothing")
    ap.add_argument("--to", default=None)
    args = ap.parse_args(argv)

    rows = load_rows(UNIVERSE)
    dropped = list(csv.DictReader(open(DROPPED, newline="", encoding="utf-8")))
    s = summarise(rows)

    from . import xlsx_report
    xlsx_report.build(rows, dropped, {
        "subtitle": f"{s['total']} funds · "
                    f"{' · '.join(f'{ex} {n}' for ex, n in s['by_ex'].items())} · "
                    f"as of {s['as_of']}",
        "facts": [("Funds", s["total"]),
                  *[(f"{ex} funds", n) for ex, n in s["by_ex"].items()],
                  ("Exact discounts", s["n_exact"])],
        "by_basis": s["by_basis"],
        "by_market": s["by_market"],
        "notes": [
            "price_over_nav_net — exact: AIC MIR month-end price vs net shareholders' funds",
            "published — the ASX states its own premium/discount",
            "mcap_over_gross_assets — estimate, biased WIDE by gearing (geared funds read cheaper than they are)",
        ],
    }, XLSX_OUT)
    print(f"wrote {XLSX_OUT}")

    html_doc = render_html(s)
    with open(HTML_OUT, "w", encoding="utf-8") as fh:
        fh.write(html_doc)
    print(f"wrote {HTML_OUT}")

    if args.dry_run:
        print("dry run: nothing sent")
        return 0

    sender = os.environ.get("GMAIL_USERNAME")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not sender or not password:
        print("GMAIL_USERNAME / GMAIL_APP_PASSWORD not set: cannot send. "
              "Set the repository secrets, or use --dry-run.", file=sys.stderr)
        return 1
    to = args.to or os.environ.get("MAIL_TO") or sender

    msg = build_email(s, XLSX_OUT, sender, to)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)
    print(f"sent to {to}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
