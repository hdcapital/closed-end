#!/usr/bin/env python3
"""NZX universe — SEMI-MANUAL BY DESIGN.

The NZ closed-end universe is roughly a dozen vehicles, and NZX does not
publish a clean machine-readable list of them. So the seed list in config.yaml
is the source of record, and the NZX instrument page is used only to confirm
each seed ticker still quotes and to surface listed funds the seed is missing.

Every fund from this leg is tagged `source_status='semi_manual'` and the report
says so. The seed entries carry `verified: false` until the owner has checked
them against the live NZX list — an unverified NZ row should not be ranked
next to an ASX row sourced from an official monthly report as though the two
were equally solid.
"""

import re
from typing import List

from .. import db, fetch
from ..util import utcnow_iso
from .common import compile_exclusions, should_exclude

EXCHANGE = "NZX"
CURRENCY = "NZD"
SOURCE = "config-seed+nzx-instrument-list"


def build(conn, fetcher, cfg) -> dict:
    stats = {"exchange": EXCHANGE, "fetched": 0, "kept": 0, "excluded": 0,
             "status": "semi_manual", "warnings": [], "unverified": 0,
             "listing_confirmed": 0, "candidates_not_in_seed": []}

    seeds = cfg.get("sources.nz.seed_funds", []) or []
    if not seeds:
        stats["warnings"].append("no NZ seed funds configured — NZ leg is empty")
        return stats

    quoted = _quoted_tickers(fetcher, cfg, stats)
    patterns = compile_exclusions(cfg)
    now = utcnow_iso()

    for seed in seeds:
        ticker = str(seed.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        stats["fetched"] += 1
        name = seed.get("name")
        verified = bool(seed.get("verified", False))
        if not verified:
            stats["unverified"] += 1
        reason = should_exclude(name or "", seed.get("sector") or "", patterns)

        status = "excluded" if reason else "live"
        # If we could read the NZX list and the ticker isn't on it, that is a
        # real signal (delisted, renamed) — record it, don't drop it.
        if quoted is not None and ticker not in quoted:
            if not reason:
                status = "delisted"
                reason = "not present on the NZX instrument list at last check"
        elif quoted is not None:
            stats["listing_confirmed"] += 1

        db.upsert_fund(conn, {
            "fund_id": db.fund_id(EXCHANGE, ticker),
            "exchange": EXCHANGE,
            "ticker": ticker,
            "isin": seed.get("isin"),
            "name": name,
            "sector": seed.get("sector") or "unknown",
            "sector_raw": seed.get("sector"),
            "currency": seed.get("currency") or CURRENCY,
            "structure": seed.get("structure") or "listed_investment_company",
            "status": status,
            "status_reason": reason,
            "source": SOURCE,
            "source_url": (cfg.get("sources.nz.instrument_list") or [None])[0],
            # The status a downstream reader must see: this row is not
            # officially sourced the way the ASX and LSE rows are.
            "source_status": "semi_manual" if verified else "semi_manual_unverified",
            "retrieved_at": now,
        })
        if reason and status == "excluded":
            stats["excluded"] += 1
        else:
            stats["kept"] += 1

    if stats["unverified"]:
        stats["warnings"].append(
            f"{stats['unverified']} NZ seed fund(s) still marked verified:false — "
            "owner sign-off needed before the NZ leg is trusted"
        )
    conn.commit()
    return stats


def _quoted_tickers(fetcher, cfg, stats) -> List[str]:
    """Tickers scraped off the NZX instrument page, or None if unreachable.

    None and empty mean different things here: None = we could not check, so
    seeds keep their status; empty = we checked and the page had no tickers,
    which is a parse problem worth reporting rather than a mass delisting.
    """
    urls = cfg.get("sources.nz.instrument_list", []) or []
    for url in urls:
        page = fetcher.get(url, kind="nzx-instrument-list")
        if not page.ok:
            stats["warnings"].append(
                f"NZX instrument list unavailable ({page.status}) — seed list used "
                "without a listing cross-check"
            )
            continue
        found = set(re.findall(r"\b([A-Z]{3,4})\b", page.text))
        if len(found) < 20:
            stats["warnings"].append(
                "NZX page fetched but yielded too few tickers to trust as a "
                "cross-check (likely client-side rendered) — seeds left as-is"
            )
            return None
        return found
    return None
