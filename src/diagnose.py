#!/usr/bin/env python3
"""Print a health summary of the store: what was collected, and what wasn't.

    python -m src.diagnose

Built for the first live run, but permanently useful. The interesting output
is the failure side — which sources refused us, which documents parsed to
nothing — because that is what a screen quietly built on thin data looks like
from the inside.
"""

import sys

from . import db


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params)]


def main(argv=None) -> int:
    path = argv[0] if argv else None
    conn = db.connect(path)

    print("=" * 72)
    print("UNIVERSE")
    print("=" * 72)
    for r in _rows(conn, "SELECT exchange, status, COUNT(*) n FROM funds "
                         "GROUP BY exchange, status ORDER BY exchange, status"):
        print(f"  {r['exchange']:5} {r['status']:12} {r['n']:5}")
    total = _rows(conn, "SELECT COUNT(*) n FROM funds")[0]["n"]
    print(f"  {'TOTAL':5} {'':12} {total:5}")

    print("\n  sectors (live funds):")
    for r in _rows(conn, "SELECT COALESCE(sector,'(null)') s, COUNT(*) n FROM funds "
                         "WHERE status='live' GROUP BY s ORDER BY n DESC"):
        print(f"    {r['s']:20} {r['n']:5}")

    print("\n" + "=" * 72)
    print("OBSERVATIONS")
    print("=" * 72)
    for label, sql in [
        ("nta rows (with a value)",
         "SELECT COUNT(*) n FROM nta_observations WHERE nta_per_share IS NOT NULL"),
        ("nta rows (NULL, i.e. recorded gaps)",
         "SELECT COUNT(*) n FROM nta_observations WHERE nta_per_share IS NULL"),
        ("distinct funds with NTA",
         "SELECT COUNT(DISTINCT fund_id) n FROM nta_observations "
         "WHERE nta_per_share IS NOT NULL"),
        ("price rows",
         "SELECT COUNT(*) n FROM price_observations WHERE close IS NOT NULL"),
        ("distinct funds with prices",
         "SELECT COUNT(DISTINCT fund_id) n FROM price_observations WHERE close IS NOT NULL"),
        ("holder rows",
         "SELECT COUNT(*) n FROM holders WHERE pct IS NOT NULL"),
        ("fund events", "SELECT COUNT(*) n FROM fund_events"),
    ]:
        print(f"  {label:42} {_rows(conn, sql)[0]['n']:7}")

    span = _rows(conn, "SELECT MIN(date) a, MAX(date) b FROM nta_observations "
                       "WHERE nta_per_share IS NOT NULL")
    if span and span[0]["a"]:
        print(f"  {'NTA date span':42} {span[0]['a']} .. {span[0]['b']}")
    print("\n  NTA by type:")
    for r in _rows(conn, "SELECT nta_type, COUNT(*) n FROM nta_observations "
                         "WHERE nta_per_share IS NOT NULL GROUP BY nta_type ORDER BY n DESC"):
        print(f"    {r['nta_type']:20} {r['n']:7}")
    print("\n  NTA by source:")
    for r in _rows(conn, "SELECT source, COUNT(*) n FROM nta_observations "
                         "WHERE nta_per_share IS NOT NULL GROUP BY source ORDER BY n DESC"):
        print(f"    {r['source'][:44]:46} {r['n']:7}")

    print("\n" + "=" * 72)
    print("FETCH OUTCOMES  (the honest part)")
    print("=" * 72)
    for r in _rows(conn, "SELECT status, COUNT(*) n FROM source_log "
                         "GROUP BY status ORDER BY n DESC"):
        print(f"  {r['status']:16} {r['n']:6}")

    bad = _rows(conn, "SELECT status, kind, url, detail FROM source_log "
                      "WHERE status NOT IN ('ok','cached') "
                      "ORDER BY id DESC LIMIT 25")
    if bad:
        print("\n  most recent failures:")
        for r in bad:
            print(f"    [{r['status']}] {r['kind']} {(r['url'] or '')[:78]}")
            if r["detail"]:
                print(f"        {r['detail'][:140]}")

    print("\n" + "=" * 72)
    print("SOURCE STATUS ON STORED FIGURES")
    print("=" * 72)
    for table in ("funds", "nta_observations", "price_observations"):
        print(f"  {table}:")
        for r in _rows(conn, f"SELECT source_status s, COUNT(*) n FROM {table} "
                             f"GROUP BY s ORDER BY n DESC LIMIT 12"):
            print(f"    {r['s'][:50]:52} {r['n']:7}")

    print("\n" + "=" * 72)
    print("SCORES")
    print("=" * 72)
    for r in _rows(conn, "SELECT score_name, COUNT(*) n, "
                         "SUM(CASE WHEN value IS NULL THEN 1 ELSE 0 END) nulls "
                         "FROM scores GROUP BY score_name"):
        print(f"  {r['score_name']:20} rows={r['n']:5} null={r['nulls']:5}")

    _model_inputs(conn)
    _nta_sanity(conn)
    return 0


def _model_inputs(conn) -> None:
    """The growth term next to the inputs that produced it.

    A headline return is only auditable if you can see whether it came from
    real history or from the prior plus a cap. `rankable` is printed because a
    scored-but-unrankable fund must never be mistaken for a recommendation.
    """
    import json

    rows = _rows(conn, "SELECT f.exchange, f.ticker, f.name, f.sector, s.value, s.components "
                       "FROM scores s JOIN funds f ON f.fund_id=s.fund_id "
                       "WHERE s.score_name='forward_return' AND s.value IS NOT NULL "
                       "ORDER BY s.value DESC LIMIT 20")
    if not rows:
        return
    print("\n" + "=" * 72)
    print("TOP 20 BY FORWARD RETURN — with the inputs behind each number")
    print("=" * 72)
    print(f"  {'exch':5}{'code':8}{'sector':18}{'total':>8}{'g_cons':>8}{'base':>9}"
          f"{'prior':>7}{'yrs':>6}{'w':>6}{'r_disc':>8}  rankable")
    for r in rows:
        try:
            c = json.loads(r["components"] or "{}")
        except Exception:
            c = {}
        f = lambda k, d=2: (f"{c[k] * 100:.{d}f}" if c.get(k) is not None else "—")  # noqa: E731
        yrs = c.get("g_weight_on_data")
        # invert w = y/(y+10) to show the history the weight implies
        years = (10 * yrs / (1 - yrs)) if (yrs is not None and yrs < 1) else None
        print(f"  {r['exchange']:5}{r['ticker']:8}{(r['sector'] or '')[:17]:18}"
              f"{f('total'):>8}{f('g_conservative'):>8}{f('g_base'):>9}"
              f"{f('g_prior'):>7}{(f'{years:.1f}' if years else '—'):>6}"
              f"{(f'{yrs:.2f}' if yrs is not None else '—'):>6}{f('r_discount'):>8}"
              f"  {'yes' if c.get('rankable') else 'NO — ' + str(c.get('exclusion_reason') or '')[:40]}")
    print("\n  NB values are percent. 'base' is the raw return window before shrinkage;")
    print("  where base is blank the estimate is the sector prior, not measured history.")


def _nta_sanity(conn) -> None:
    """Month-on-month NTA jumps that no fund actually made.

    The classic cause is a source switching a column between dollars and cents
    between editions: a 100x step looks like a spectacular return and would
    otherwise march straight to the top of the screen.
    """
    print("\n" + "=" * 72)
    print("NTA SANITY — implausible month-on-month steps")
    print("=" * 72)
    rows = _rows(conn,
                 "SELECT fund_id, date, nta_per_share FROM nta_observations "
                 "WHERE nta_per_share IS NOT NULL AND nta_per_share > 0 "
                 "ORDER BY fund_id, date")
    prev_id, prev_v, prev_d = None, None, None
    flagged = []
    for r in rows:
        if r["fund_id"] == prev_id and prev_v:
            ratio = r["nta_per_share"] / prev_v
            if ratio >= 1.5 or ratio <= 0.67:
                flagged.append((r["fund_id"], prev_d, prev_v, r["date"],
                                r["nta_per_share"], ratio))
        prev_id, prev_v, prev_d = r["fund_id"], r["nta_per_share"], r["date"]

    if not flagged:
        print("  none — no step beyond +50%/-33% in one observation")
    else:
        print(f"  {len(flagged)} suspicious step(s); worst 20:")
        for fid, d0, v0, d1, v1, ratio in sorted(
                flagged, key=lambda x: -max(x[5], 1 / x[5]))[:20]:
            print(f"    {fid:12} {d0} {v0:>12.4f}  ->  {d1} {v1:>12.4f}   x{ratio:.2f}")
        print("  A ratio near 100 or 0.01 is a dollars/cents switch, not performance.")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
