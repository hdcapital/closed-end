#!/usr/bin/env python3
"""AIC Monthly Information Release -> uniform records.

    https://www.theaic.co.uk/aic/statistics/mir

The file this project needed all along. Where the industry overview carries
assets and market cap, the MIR carries **net** assets, shares in issue,
month-end price and gearing — so the discount comes out exact instead of
inflated by whatever a fund borrows.

That difference is not academic. Against the July 2026 file, the gross-assets
method overstated F&C by 8.1pp, Scottish Mortgage by 8.9pp, and reported City
of London at a 2.0% discount when it was on a 3.2% *premium*. In each case the
error equalled the gearing.

Three things about the format, all of which will break a naive reader:

* **Two header rows.** Row 0 is a group banner, row 1 the field names.
* **The header block repeats**, roughly once per AIC sector, so ~45 of the 327
  rows in the July file are headers wearing a data row's clothing.
* **cp1252, not UTF-8** — the pound signs make `utf-8` throw outright.

And two traps in the numbers:

* The columns labelled "SHF per share inc/exc CYR" hold *aggregate*
  shareholders' funds, not a per-share figure. Trusting the label gives a NAV
  per share in the hundreds of millions. The per-share value is SHF / shares.
* Aggregate SHF is in the **major** unit even when the reported currency is
  GBX. Checked against the AIC's own per-share NAV column: Pacific Assets shows
  SHF/shares = 4.4731 against a stated 447.31p. So the price needs dividing by
  100 and the aggregate does not.

Coverage, from the July 2026 file: 282 vehicles listed, but only 137 carry
price, shares and net assets. The other 145 rows are name-and-ISIN only — every
figure blank. Those are emitted with no numbers rather than guessed at, and the
industry overview's gross-assets discount remains their only estimate.
"""

from typing import List, Tuple

from ..universe.common import find_links
from ..util import parse_date, to_float
from .record import Record

EXCHANGE = "LSE"
SOURCE = "aic-mir"
LANDING = "https://www.theaic.co.uk/aic/statistics/mir"

# Field names from header row 1.
F_NAME, F_ISIN, F_SECTOR = "Name", "ISIN", "Category"
F_TYPE, F_DATE, F_CUR = "Type", "Date", "Reported Currency"
F_PRICE, F_SHARES = "MonthEnd Price", "Number"
F_NET_CUM, F_NET_EX = "SHF per share inc CYR", "SHF per share exc CYR"
F_GROSS_ASSETS, F_NET_GEAR = "Total Assets incl CYR", "NetGearing"

# Currencies quoted in minor units.
MINOR_UNIT = {"GBX": 100.0, "GBP": 1.0, "USD": 1.0, "EUR": 1.0}

# Only ordinary equity gets a meaningful NAV-vs-price discount. Zero dividend
# preference shares and C shares have their own economics and would pollute a
# discount screen if mixed in with the ordinaries.
ORDINARY = ("ordinary",)


def find_file(fetcher, info: dict = None) -> Tuple[str, str]:
    page = fetcher.get(LANDING, kind="aic-mir-landing")
    if not page.ok:
        return None, page.status
    links = find_links(page.text, LANDING, extensions=(".csv", ".xlsx", ".xls"),
                       keywords=("mir", "monthly", "information", "release"))
    links = links or find_links(page.text, LANDING,
                                extensions=(".csv", ".xlsx", ".xls", ".zip"))
    if not links and info is not None:
        # Say what the page *did* offer. A silent "not found" leaves the next
        # run no better informed than this one.
        near = find_links(page.text, LANDING,
                          keywords=("mir", "monthly", "download", "statistic"))
        info["warnings"].append(
            f"no data file linked; nearest candidates: {near[:8] or 'none'}")
    return (links[0] if links else None), page.status


def _rows_from(content: bytes, filename: str):
    """(field_names, data_rows). Handles the CSV and spreadsheet forms."""
    if filename.lower().split("?")[0].endswith((".xlsx", ".xlsm", ".xls")):
        from .. import tabular
        sheets = tabular.read_sheets(content, filename)
        rows = max(sheets.values(), key=len)
        return [str(c or "").strip() for c in rows[1]], rows[2:]
    import csv
    import io
    # cp1252: the file carries pound signs and is not valid UTF-8.
    text = content.decode("cp1252", "replace")
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 3:
        return [], []
    return [c.strip() for c in rows[1]], rows[2:]


def harvest(fetcher, url: str = None) -> Tuple[List[Record], dict]:
    info = {"source": SOURCE, "status": "skipped", "url": url, "rows": 0,
            "warnings": [], "header_repeats": 0, "non_ordinary": 0,
            "incomplete": 0, "priced": 0}

    if not url:
        url, status = find_file(fetcher, info)
        info["status"] = status
        if not url:
            info["warnings"].append(
                f"no MIR file linked from {LANDING} (landing status {status})")
            return [], info
    info["url"] = url

    doc = fetcher.get(url, kind="aic-mir")
    info["status"] = doc.status
    if not doc.ok:
        info["warnings"].append(f"download failed: {doc.detail or doc.status}")
        return [], info

    names, data = _rows_from(doc.content, url)
    if not names:
        info["status"] = "parse_error"
        info["warnings"].append("could not read a header row from the MIR file")
        return [], info

    # index() takes the FIRST match deliberately: "Name" occurs three times in
    # the header — the fund at column 1, then twice more for unitised share
    # classes near the end, where it is blank for every ordinary trust.
    ix = {}
    for f in (F_NAME, F_ISIN, F_SECTOR, F_TYPE, F_DATE, F_CUR, F_PRICE,
              F_SHARES, F_NET_CUM, F_NET_EX, F_GROSS_ASSETS, F_NET_GEAR):
        if f in names:
            ix[f] = names.index(f)
    missing = [f for f in (F_NAME, F_ISIN, F_PRICE, F_SHARES, F_NET_CUM) if f not in ix]
    if missing:
        info["status"] = "parse_error"
        info["warnings"].append(
            f"MIR is missing expected fields {missing} | header: {names[:20]}")
        return [], info

    def cell(row, field):
        i = ix.get(field)
        return row[i] if (i is not None and i < len(row)) else None

    out = []
    for row in data:
        name = (cell(row, F_NAME) or "").strip()
        if not name:
            continue
        # The header block repeats once per sector; those rows echo the field
        # names back at us and must not become funds.
        if name == F_NAME or (cell(row, F_CUR) or "").strip() == F_CUR:
            info["header_repeats"] += 1
            continue
        share_type = (cell(row, F_TYPE) or "").strip().lower()
        if share_type and not any(k in share_type for k in ORDINARY):
            info["non_ordinary"] += 1
            continue

        isin = (cell(row, F_ISIN) or "").strip().upper()
        cur = (cell(row, F_CUR) or "").strip().upper() or "GBX"
        divisor = MINOR_UNIT.get(cur, 1.0)
        shares = to_float(cell(row, F_SHARES))
        price = to_float(cell(row, F_PRICE))
        # Cum-income is the UK headline convention; the ex-income figure is
        # kept only as a fallback when the cum one is absent.
        net_assets = to_float(cell(row, F_NET_CUM)) or to_float(cell(row, F_NET_EX))

        nav_ps = discount = None
        if net_assets and shares and shares > 0:
            # SHF is aggregate despite the column's "per share" label; divide.
            nav_ps = net_assets / shares
            if price and nav_ps > 0:
                # Price is in the minor unit (GBX), aggregate SHF in the major.
                discount = (price / divisor) / nav_ps - 1.0
        if discount is None:
            info["incomplete"] += 1
        else:
            info["priced"] += 1

        d = parse_date(cell(row, F_DATE))
        out.append(Record(
            code=None,                      # MIR is keyed by ISIN, not TIDM
            isin=isin if len(isin) == 12 else None,
            exchange=EXCHANGE,
            name=name,
            vehicle_type="investment_trust",
            sector=(cell(row, F_SECTOR) or "").strip() or None,
            currency="GBP" if cur.startswith("GB") else cur,
            nta_total=net_assets,
            nta_per_share=nav_ps,
            nta_unit="declared_major" if nav_ps is not None else None,
            price=price / divisor if price else None,
            discount=discount,
            # Named for what it is: a price-to-NAV discount computed from NET
            # assets, so gearing is already deducted and it needs no caveat.
            discount_basis="price_over_nav_net" if discount is not None else None,
            nta_date=d.isoformat() if d else None,
            as_of=d.isoformat() if d else None,
            source=SOURCE,
            source_url=url,
        ))

    info["rows"] = len(out)
    info["warnings"].append(
        f"{info['priced']} of {len(out)} rows carry price + shares + net assets "
        f"and get an exact net discount; {info['incomplete']} are name-and-ISIN "
        f"only and keep the industry overview's gross-assets estimate")
    info["warnings"].append(
        f"skipped {info['header_repeats']} repeated header row(s) and "
        f"{info['non_ordinary']} non-ordinary share class(es)")
    if not out:
        info["warnings"].append("no usable MIR rows parsed")
    return out, info
