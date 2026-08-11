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

    top = _rows(conn, "SELECT f.exchange, f.ticker, f.name, s.value FROM scores s "
                      "JOIN funds f ON f.fund_id=s.fund_id "
                      "WHERE s.score_name='forward_return' AND s.value IS NOT NULL "
                      "ORDER BY s.value DESC LIMIT 15")
    if top:
        print("\n  top 15 by forward return:")
        for r in top:
            print(f"    {r['exchange']:4} {r['ticker']:8} {(r['name'] or '')[:42]:44} "
                  f"{r['value'] * 100:6.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
