#!/usr/bin/env python3
"""Canonical SQLite store.

House rule, enforced by the schema rather than by good intentions: every
stored number carries a source and a retrieval timestamp. The observation
tables declare `source` and `retrieved_at` NOT NULL, so there is no way to
insert a figure that nobody can trace back to a document.

`source_status` is the other half of the honesty contract. When a fetch fails
we still write a row — with the value NULL and a status explaining why — so a
gap in the data is visible as a gap, never as a silently-missing fund.
"""

import os
import sqlite3
from typing import Iterable, Optional

SCHEMA_VERSION = 1

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "data", "db", "funds.sqlite")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row per closed-end vehicle we know about, live or dead.
CREATE TABLE IF NOT EXISTS funds (
    fund_id        TEXT PRIMARY KEY,   -- "<exchange>:<ticker>", stable
    exchange       TEXT NOT NULL,      -- ASX | LSE | NZX
    ticker         TEXT NOT NULL,
    isin           TEXT,               -- dedupe key where available
    name           TEXT,
    sector         TEXT,               -- normalised: equity, debt, property, ...
    sector_raw     TEXT,               -- as published (AIC / ASX mandate string)
    currency       TEXT,
    structure      TEXT,               -- LIC | LIT | investment_trust | ...
    listing_date   TEXT,
    status         TEXT NOT NULL DEFAULT 'live',   -- live | delisted | excluded
    status_reason  TEXT,
    market_cap     REAL,
    shares_on_issue REAL,
    ocr            REAL,               -- ongoing charges ratio, as a fraction
    has_performance_fee INTEGER,
    externally_managed INTEGER,
    fee_on_gross_assets INTEGER,
    source         TEXT NOT NULL,
    source_url     TEXT,
    source_status  TEXT NOT NULL DEFAULT 'ok',
    retrieved_at   TEXT NOT NULL,
    UNIQUE (exchange, ticker)
);

CREATE INDEX IF NOT EXISTS idx_funds_isin   ON funds(isin);
CREATE INDEX IF NOT EXISTS idx_funds_status ON funds(status);

-- NTA/NAV per share. nta_type keeps the UK/Australia inconsistency visible
-- instead of averaging two different things together.
CREATE TABLE IF NOT EXISTS nta_observations (
    fund_id       TEXT NOT NULL REFERENCES funds(fund_id) ON DELETE CASCADE,
    date          TEXT NOT NULL,
    nta_per_share REAL,
    nta_type      TEXT NOT NULL,       -- pre_tax | post_tax | cum_income | ex_income | unspecified
    currency      TEXT,
    source        TEXT NOT NULL,
    source_url    TEXT,
    source_status TEXT NOT NULL DEFAULT 'ok',
    retrieved_at  TEXT NOT NULL,
    PRIMARY KEY (fund_id, date, nta_type, source)
);

CREATE INDEX IF NOT EXISTS idx_nta_fund_date ON nta_observations(fund_id, date);

CREATE TABLE IF NOT EXISTS price_observations (
    fund_id       TEXT NOT NULL REFERENCES funds(fund_id) ON DELETE CASCADE,
    date          TEXT NOT NULL,
    close         REAL,
    currency      TEXT,
    volume        REAL,
    dividend      REAL,                -- cash distribution paid with this ex-date
    source        TEXT NOT NULL,
    source_url    TEXT,
    source_status TEXT NOT NULL DEFAULT 'ok',
    retrieved_at  TEXT NOT NULL,
    PRIMARY KEY (fund_id, date, source)
);

CREATE INDEX IF NOT EXISTS idx_price_fund_date ON price_observations(fund_id, date);

-- Register evidence: TR-1s, Forms 603/604/605, SSH notices, annual-report top-20.
CREATE TABLE IF NOT EXISTS holders (
    fund_id       TEXT NOT NULL REFERENCES funds(fund_id) ON DELETE CASCADE,
    date          TEXT NOT NULL,
    holder_name   TEXT NOT NULL,
    holder_type   TEXT,                -- activist | institution | insider | manager | nominee | unknown
    pct           REAL,                -- fraction of shares on issue, 0-1
    source        TEXT NOT NULL,
    source_url    TEXT,
    source_status TEXT NOT NULL DEFAULT 'ok',
    retrieved_at  TEXT NOT NULL,
    PRIMARY KEY (fund_id, date, holder_name, source)
);

CREATE INDEX IF NOT EXISTS idx_holders_fund ON holders(fund_id);

-- Per-fund facts that feed the endgame pillar and can't be derived from prices.
CREATE TABLE IF NOT EXISTS fund_events (
    fund_id       TEXT NOT NULL REFERENCES funds(fund_id) ON DELETE CASCADE,
    event_type    TEXT NOT NULL,       -- continuation_vote | manager_agreement_expiry | ...
    event_date    TEXT,
    detail        TEXT,
    source        TEXT NOT NULL,
    source_url    TEXT,
    source_status TEXT NOT NULL DEFAULT 'ok',
    retrieved_at  TEXT NOT NULL,
    PRIMARY KEY (fund_id, event_type, event_date)
);

-- Computed metrics. `provenance` never mixes stated and computed figures:
-- it is 'computed' or 'stated' per metric, and the report carries it through.
CREATE TABLE IF NOT EXISTS derived_metrics (
    fund_id      TEXT NOT NULL REFERENCES funds(fund_id) ON DELETE CASCADE,
    as_of        TEXT NOT NULL,
    metric       TEXT NOT NULL,
    value        REAL,
    provenance   TEXT NOT NULL DEFAULT 'computed',   -- computed | stated | unavailable
    detail       TEXT,
    computed_at  TEXT NOT NULL,
    PRIMARY KEY (fund_id, as_of, metric)
);

CREATE TABLE IF NOT EXISTS scores (
    fund_id      TEXT NOT NULL REFERENCES funds(fund_id) ON DELETE CASCADE,
    as_of        TEXT NOT NULL,
    score_name   TEXT NOT NULL,
    value        REAL,
    components   TEXT,                 -- JSON decomposition, so a number can be audited by eye
    computed_at  TEXT NOT NULL,
    PRIMARY KEY (fund_id, as_of, score_name)
);

-- Every fetch attempt, successful or not. This is the audit trail that makes
-- "we have no data for X" a checkable claim rather than an excuse.
CREATE TABLE IF NOT EXISTS source_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url           TEXT NOT NULL,
    kind          TEXT,
    status        TEXT NOT NULL,       -- ok | cached | http_error | blocked | robots_denied | parse_error | skipped
    http_status   INTEGER,
    bytes         INTEGER,
    cache_path    TEXT,
    detail        TEXT,
    attempted_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_source_log_status ON source_log(status);
"""


def connect(path: str = None) -> sqlite3.Connection:
    path = path or os.environ.get("CLOSED_END_DB") or DEFAULT_DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def fund_id(exchange: str, ticker: str) -> str:
    return f"{exchange.upper()}:{ticker.upper().strip()}"


# ---------------------------------------------------------------------------
# Upserts. Each takes a dict shaped like the table.
# ---------------------------------------------------------------------------

_FUND_COLS = [
    "fund_id", "exchange", "ticker", "isin", "name", "sector", "sector_raw",
    "currency", "structure", "listing_date", "status", "status_reason",
    "market_cap", "shares_on_issue", "ocr", "has_performance_fee",
    "externally_managed", "fee_on_gross_assets",
    "source", "source_url", "source_status", "retrieved_at",
]


def _with_provenance(row: dict) -> dict:
    """Fill the provenance columns a caller may have left off.

    A column DEFAULT does not help here: passing an explicit NULL overrides it,
    and these helpers always bind every column. `retrieved_at` defaults to now
    because that is genuinely when the row entered the store; `source` is left
    alone, so an untraceable figure still fails the NOT NULL constraint rather
    than acquiring a fake source.
    """
    out = dict(row)
    if not out.get("source_status"):
        out["source_status"] = "ok"
    if not out.get("retrieved_at"):
        from .util import utcnow_iso
        out["retrieved_at"] = utcnow_iso()
    return out


def upsert_fund(conn: sqlite3.Connection, row: dict) -> None:
    """Insert or update a fund. Existing non-null columns are only overwritten
    when the incoming row actually carries a value, so a thin source (a price
    feed) can't blank out a rich one (the ASX monthly report)."""
    row = _with_provenance(row)
    if not row.get("status"):
        row["status"] = "live"
    vals = [row.get(c) for c in _FUND_COLS]
    updates = ", ".join(
        f"{c}=COALESCE(excluded.{c}, {c})"
        for c in _FUND_COLS
        if c not in ("fund_id", "exchange", "ticker")
    )
    conn.execute(
        f"INSERT INTO funds ({','.join(_FUND_COLS)}) "
        f"VALUES ({','.join('?' * len(_FUND_COLS))}) "
        f"ON CONFLICT(fund_id) DO UPDATE SET {updates}",
        vals,
    )


def insert_nta(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    cols = ["fund_id", "date", "nta_per_share", "nta_type", "currency",
            "source", "source_url", "source_status", "retrieved_at"]
    data = [tuple(_with_provenance(r).get(c) for c in cols) for r in rows]
    conn.executemany(
        f"INSERT INTO nta_observations ({','.join(cols)}) "
        f"VALUES ({','.join('?' * len(cols))}) "
        f"ON CONFLICT(fund_id, date, nta_type, source) DO UPDATE SET "
        f"nta_per_share=excluded.nta_per_share, source_url=excluded.source_url, "
        f"source_status=excluded.source_status, retrieved_at=excluded.retrieved_at",
        data,
    )
    return len(data)


def insert_prices(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    cols = ["fund_id", "date", "close", "currency", "volume", "dividend",
            "source", "source_url", "source_status", "retrieved_at"]
    data = [tuple(_with_provenance(r).get(c) for c in cols) for r in rows]
    conn.executemany(
        f"INSERT INTO price_observations ({','.join(cols)}) "
        f"VALUES ({','.join('?' * len(cols))}) "
        f"ON CONFLICT(fund_id, date, source) DO UPDATE SET "
        f"close=excluded.close, volume=excluded.volume, dividend=excluded.dividend, "
        f"source_status=excluded.source_status, retrieved_at=excluded.retrieved_at",
        data,
    )
    return len(data)


def insert_holders(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    cols = ["fund_id", "date", "holder_name", "holder_type", "pct",
            "source", "source_url", "source_status", "retrieved_at"]
    data = [tuple(_with_provenance(r).get(c) for c in cols) for r in rows]
    conn.executemany(
        f"INSERT INTO holders ({','.join(cols)}) "
        f"VALUES ({','.join('?' * len(cols))}) "
        f"ON CONFLICT(fund_id, date, holder_name, source) DO UPDATE SET "
        f"pct=excluded.pct, holder_type=excluded.holder_type, "
        f"source_status=excluded.source_status, retrieved_at=excluded.retrieved_at",
        data,
    )
    return len(data)


def insert_events(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    cols = ["fund_id", "event_type", "event_date", "detail",
            "source", "source_url", "source_status", "retrieved_at"]
    data = [tuple(_with_provenance(r).get(c) for c in cols) for r in rows]
    conn.executemany(
        f"INSERT INTO fund_events ({','.join(cols)}) "
        f"VALUES ({','.join('?' * len(cols))}) "
        f"ON CONFLICT(fund_id, event_type, event_date) DO UPDATE SET "
        f"detail=excluded.detail, source_status=excluded.source_status, "
        f"retrieved_at=excluded.retrieved_at",
        data,
    )
    return len(data)


def put_metric(conn: sqlite3.Connection, fund_id_: str, as_of: str, metric: str,
               value: Optional[float], provenance: str = "computed",
               detail: str = None, computed_at: str = None) -> None:
    from .util import utcnow_iso
    conn.execute(
        "INSERT INTO derived_metrics (fund_id, as_of, metric, value, provenance, detail, computed_at) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(fund_id, as_of, metric) DO UPDATE SET "
        "value=excluded.value, provenance=excluded.provenance, detail=excluded.detail, "
        "computed_at=excluded.computed_at",
        (fund_id_, as_of, metric, value, provenance, detail, computed_at or utcnow_iso()),
    )


def put_score(conn: sqlite3.Connection, fund_id_: str, as_of: str, score_name: str,
              value: Optional[float], components: str = None,
              computed_at: str = None) -> None:
    from .util import utcnow_iso
    conn.execute(
        "INSERT INTO scores (fund_id, as_of, score_name, value, components, computed_at) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(fund_id, as_of, score_name) DO UPDATE SET "
        "value=excluded.value, components=excluded.components, computed_at=excluded.computed_at",
        (fund_id_, as_of, score_name, value, components, computed_at or utcnow_iso()),
    )


def log_source(conn: sqlite3.Connection, *, url: str, kind: str, status: str,
               http_status: int = None, bytes_: int = None, cache_path: str = None,
               detail: str = None) -> None:
    from .util import utcnow_iso
    conn.execute(
        "INSERT INTO source_log (url, kind, status, http_status, bytes, cache_path, detail, attempted_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (url, kind, status, http_status, bytes_, cache_path, detail, utcnow_iso()),
    )
