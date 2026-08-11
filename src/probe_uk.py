#!/usr/bin/env python3
"""Find the real LSE instrument-list file, by looking rather than guessing.

    python -m src.probe_uk

The UK leg is the largest gap in the screen (~350 trusts) and the reason is
narrow: the fallback `Issuer list.xlsx` downloads fine but is a September 2020
snapshot whose only sheet has no ISIN and no TIDM, so the universe builder
correctly refuses it. What is missing is the *current* file's URL.

Guessing filenames one release at a time is slow and each guess costs a run, so
this probes a spread of candidates in one pass and prints, for every one that
answers: the sheet names, the first rows, and — decisively — whether a header
carrying both a ticker and an ISIN exists anywhere in it. That turns "the LSE
leg doesn't work" into a fact about which file to use.

Read-only and rate-limited like every other fetch here; it writes nothing.
"""

import sys

from . import config, db, fetch, tabular
from .universe.common import find_links
from .universe.uk import COLUMN_SPEC, REQUIRED_HEADER_HINTS

DOCS = "https://docs.londonstockexchange.com/sites/default/files/reports/"

# Names this file has plausibly used. The numbered variants are how the LSE
# publishes successive editions (a copy of "Issuer list_81.xlsx" circulates
# publicly), so a short descending sweep should meet the current one.
CANDIDATE_NAMES = [
    "Issuer%20list.xlsx",
    "List%20of%20all%20companies.xlsx",
    "Instrument%20list.xlsx",
    "issuer-list.xlsx",
]
NUMBERED = "Issuer%20list_{n}.xlsx"

LANDINGS = [
    "https://www.londonstockexchange.com/reports?tab=issuers",
    "https://www.londonstockexchange.com/reports?tab=instruments",
    "https://www.londonstockexchange.com/reports",
]


def _inspect(fetched) -> None:
    """Say what a downloaded workbook actually contains."""
    try:
        sheets = tabular.read_sheets(fetched.content, fetched.url)
    except Exception as e:
        print(f"      unreadable: {e}")
        return
    print(f"      sheets: {', '.join(list(sheets)[:8])}")
    hit = False
    for name, rows in sheets.items():
        idx = tabular.find_header(rows, REQUIRED_HEADER_HINTS)
        if idx is None:
            continue
        cmap = tabular.ColumnMap(rows[idx], COLUMN_SPEC)
        if cmap.has("ticker") and cmap.has("isin"):
            hit = True
            print(f"      *** USABLE: sheet '{name}' row {idx} has ticker+ISIN")
            print(f"          header: {tabular.header_row_text(cmap.raw_header)[:300]}")
            print(f"          unmapped: {', '.join(cmap.missing) or 'none'}")
            print(f"          data rows: ~{max(0, len(rows) - idx - 1)}")
    if not hit:
        print("      no sheet carries both a ticker and an ISIN column:")
        print(tabular.describe(sheets, max_rows=4))


def main(argv=None) -> int:
    cfg = config.load()
    conn = db.connect()
    fetcher = fetch.Fetcher(cfg, conn=conn)

    print("=" * 72)
    print("LANDING PAGES — what do they actually link?")
    print("=" * 72)
    for url in LANDINGS:
        page = fetcher.get(url, kind="uk-probe-landing")
        print(f"  [{page.status}] {url}")
        if not page.ok:
            continue
        sheets = find_links(page.text, url, extensions=(".xlsx", ".xlsm", ".csv"))
        print(f"      spreadsheet links found: {len(sheets)}")
        for u in sheets[:15]:
            print(f"        {u}")
        if not sheets:
            # The LSE site is a single-page app, so an empty result here is the
            # expected outcome and not a fetch failure — worth stating plainly.
            allx = find_links(page.text, url)
            print(f"      (no spreadsheets; {len(allx)} links total — "
                  "consistent with a client-side rendered page)")

    print("\n" + "=" * 72)
    print("DIRECT CANDIDATES")
    print("=" * 72)
    names = list(CANDIDATE_NAMES) + [NUMBERED.format(n=n) for n in
                                     (0, 60, 70, 75, 80, 81, 82, 85, 90, 95, 100)]
    for name in names:
        url = DOCS + name
        doc = fetcher.get(url, kind="uk-probe-file")
        size = len(doc.content) if doc.content else 0
        print(f"  [{doc.status}] {name}  ({size} bytes)")
        if doc.ok and size > 5000:
            _inspect(doc)

    print("\nDone. Any line marked *** USABLE names the file to put in "
          "config.yaml under sources.uk.instrument_list_fallbacks.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
