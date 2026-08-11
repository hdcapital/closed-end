#!/usr/bin/env python3
"""Daily closes and distributions, via yfinance.

Prices only feed discounts and trailing yield, so one convenience source at
daily granularity is an accepted trade-off — but a convenience source needs
guard rails, because the failure mode is silent and expensive:

* **Ticker suffixes.** ASX `.AX`, LSE `.L`, NZX `.NZ`. A missing suffix
  resolves to a *different, real* company on another exchange, so a bad symbol
  returns plausible prices rather than an error. Every symbol is sanity-checked
  against the exchange's currency before its prices are stored.
* **Pence.** LSE quotes most investment trusts in pence while NAV may be
  announced in pounds. A 100x scale error looks exactly like a 99% discount,
  which would otherwise sit at the top of the screen. Detected and recorded.
* **Staleness.** A price older than `stale_price_days` is stored and flagged,
  never quietly treated as current.
"""

import datetime
from typing import List, Optional

from .. import db, fetch
from ..util import to_float, today_utc, utcnow_iso

SOURCE = "yfinance"

# Currency each exchange should report. A mismatch means the symbol resolved
# to the wrong listing.
EXPECTED_CURRENCY = {"ASX": {"AUD"}, "LSE": {"GBP", "GBp", "GBX"}, "NZX": {"NZD"}}

# yfinance reports LSE pence listings as GBp/GBX. Those need dividing by 100
# to compare against a NAV announced in pounds.
PENCE_CURRENCIES = {"GBp", "GBX"}


def symbol_for(exchange: str, ticker: str, cfg) -> str:
    suffixes = cfg.get("sources.prices.suffixes")
    key = {"ASX": "asx", "LSE": "uk", "NZX": "nz"}.get(exchange.upper(), "")
    suffix = suffixes.get(key, "")
    t = ticker.strip().upper().replace(".", "-")     # BRK.A style -> BRK-A
    return f"{t}{suffix}"


def collect(conn, cfg, funds: List[dict], offline: bool = False) -> dict:
    """Fetch price history for each fund and store it.

    `funds` is a list of dicts with fund_id/exchange/ticker/currency.
    """
    stats = {"requested": len(funds), "ok": 0, "empty": 0, "failed": 0,
             "currency_mismatch": 0, "pence_scaled": 0, "stale": 0,
             "rows": 0, "warnings": [], "status": fetch.OK}

    if offline:
        stats["status"] = fetch.SKIPPED
        stats["warnings"].append("offline mode: no prices fetched")
        return stats

    try:
        import yfinance as yf
    except ImportError:
        stats["status"] = fetch.SKIPPED
        stats["warnings"].append("yfinance not installed — prices unavailable")
        return stats

    years = cfg.num("sources.prices.lookback_years")
    start = (datetime.date.today() - datetime.timedelta(days=int(years * 365.25))).isoformat()
    stale_days = cfg.num("run.stale_price_days")
    now = utcnow_iso()

    for f in funds:
        symbol = symbol_for(f["exchange"], f["ticker"], cfg)
        url = f"https://finance.yahoo.com/quote/{symbol}"
        try:
            tk = yf.Ticker(symbol)
            hist = tk.history(start=start, auto_adjust=False, actions=True)
        except Exception as e:
            stats["failed"] += 1
            db.log_source(conn, url=url, kind="price", status=fetch.HTTP_ERROR,
                          detail=f"{type(e).__name__}: {e}")
            _null_price(conn, f["fund_id"], url, "fetch_failed", now)
            continue

        if hist is None or len(hist) == 0:
            stats["empty"] += 1
            db.log_source(conn, url=url, kind="price", status=fetch.HTTP_ERROR,
                          detail="no price history returned — check the ticker suffix")
            _null_price(conn, f["fund_id"], url, "no_history", now)
            continue

        currency = _currency_of(tk)
        expected = EXPECTED_CURRENCY.get(f["exchange"].upper(), set())
        source_status = fetch.OK
        divisor = 1.0

        if currency and expected and currency not in expected:
            # Wrong listing: do not store prices that would produce a fake discount.
            stats["currency_mismatch"] += 1
            stats["warnings"].append(
                f"{f['fund_id']}: {symbol} reports {currency}, expected "
                f"{'/'.join(sorted(expected))} — symbol likely resolves to another "
                "listing; prices not stored"
            )
            db.log_source(conn, url=url, kind="price", status=fetch.PARSE_ERROR,
                          detail=f"currency mismatch {currency}")
            _null_price(conn, f["fund_id"], url, f"currency_mismatch:{currency}", now)
            continue

        if currency in PENCE_CURRENCIES:
            divisor = 100.0
            source_status = "ok_pence_converted"
            stats["pence_scaled"] += 1

        rows = []
        for idx, row in hist.iterrows():
            d = idx.date() if hasattr(idx, "date") else None
            close = to_float(row.get("Close"))
            if d is None or close is None:
                continue
            rows.append({
                "fund_id": f["fund_id"],
                "date": d.isoformat(),
                "close": close / divisor,
                "currency": "GBP" if divisor == 100.0 else (currency or f.get("currency")),
                "volume": to_float(row.get("Volume")),
                "dividend": (to_float(row.get("Dividends")) or 0.0) / divisor,
                "source": SOURCE,
                "source_url": url,
                "source_status": source_status,
                "retrieved_at": now,
            })

        if not rows:
            stats["empty"] += 1
            _null_price(conn, f["fund_id"], url, "no_usable_rows", now)
            continue

        last = rows[-1]["date"]
        age = (datetime.date.fromisoformat(today_utc()) -
               datetime.date.fromisoformat(last)).days
        if age > stale_days:
            stats["stale"] += 1
            stats["warnings"].append(
                f"{f['fund_id']}: last price {last} is {age} days old — flagged stale"
            )
            for r in rows[-1:]:
                r["source_status"] = f"stale:{age}d"

        stats["rows"] += db.insert_prices(conn, rows)
        stats["ok"] += 1
        db.log_source(conn, url=url, kind="price", status=fetch.OK, bytes_=len(rows))
        conn.commit()

    conn.commit()
    return stats


def _currency_of(tk) -> Optional[str]:
    """yfinance exposes currency in several places depending on version."""
    for attr in ("fast_info", "info"):
        try:
            obj = getattr(tk, attr)
            if obj is None:
                continue
            cur = obj.get("currency") if hasattr(obj, "get") else getattr(obj, "currency", None)
            if cur:
                return str(cur)
        except Exception:
            continue
    return None


def _null_price(conn, fund_id: str, url: str, reason: str, now: str) -> None:
    """Record the absence explicitly. A fund with no prices must be visible as
    a fund with no prices, not as a fund that quietly vanished from the screen."""
    db.insert_prices(conn, [{
        "fund_id": fund_id, "date": today_utc(), "close": None, "currency": None,
        "volume": None, "dividend": None, "source": SOURCE, "source_url": url,
        "source_status": reason, "retrieved_at": now,
    }])
    conn.commit()


def trailing_yield(conn, fund_id: str, as_of: str = None) -> Optional[float]:
    """Trailing 12-month distributions over the latest close.

    Reported as its own column and deliberately kept out of the headline
    forward return — see the double-counting note in the README.
    """
    as_of = as_of or today_utc()
    start = (datetime.date.fromisoformat(as_of) - datetime.timedelta(days=365)).isoformat()
    row = conn.execute(
        "SELECT SUM(COALESCE(dividend,0)) AS d FROM price_observations "
        "WHERE fund_id=? AND date>=? AND date<=? AND close IS NOT NULL",
        (fund_id, start, as_of),
    ).fetchone()
    total = row["d"] if row else None
    last = conn.execute(
        "SELECT close FROM price_observations WHERE fund_id=? AND close IS NOT NULL "
        "AND date<=? ORDER BY date DESC LIMIT 1",
        (fund_id, as_of),
    ).fetchone()
    if not total or not last or not last["close"]:
        return None
    return total / last["close"]
