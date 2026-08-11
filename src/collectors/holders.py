#!/usr/bin/env python3
"""Register evidence for the activist pillar.

Sources, in the order they are worth having:

* **ASX** — Forms 603/604/605 (substantial holder notices). Filed whenever a
  holder crosses 5% or moves 1% while above it, so the register's *active*
  edge is well covered even though the long tail is not.
* **UK** — TR-1 major-holdings notifications (3%+, then each 1%).
* **NZX** — SSH notices, same idea.
* **Annual-report top-20 tables**, where only a PDF exists. Annual granularity,
  but it is the only way to see the passive tail and the insider block.

What this buys and what it doesn't: substantial-holder filings show holders
above the threshold, so a "top-20 = 42%" derived purely from them is really
"disclosed holders = 42%" and is a floor, not a measurement. The score
consumes it as evidence with that meaning, and `holder_type` records which
kind of source each row came from.
"""

import re
from typing import Dict, List, Optional

from .. import db
from ..util import to_float, utcnow_iso
from .lake import HOLDER_TITLE_RE, LakeReader, date_range

# "Voting power ... 7.35%", "held 5.02% of the issued capital"
_PCT_PATTERNS = [
    re.compile(r"voting\s+power[^\d%]{0,60}?([\d.]{1,6})\s*%", re.IGNORECASE),
    re.compile(r"(?:person'?s\s+votes|total\s+votes)[^\d%]{0,60}?([\d.]{1,6})\s*%", re.IGNORECASE),
    re.compile(r"([\d.]{1,6})\s*%\s+of\s+(?:the\s+)?(?:issued|total|voting)", re.IGNORECASE),
    re.compile(r"(?:holding|interest)\s*[:\s][^\d%]{0,40}?([\d.]{1,6})\s*%", re.IGNORECASE),
]

# ASX 604s name the holder next to these labels.
_NAME_PATTERNS = [
    re.compile(r"(?:name\s+of\s+substantial\s+holder|substantial\s+holder)\s*\(?[^\n:]{0,30}\)?\s*[:\s]\s*([^\n]{3,90})",
               re.IGNORECASE),
    re.compile(r"ACN\s*/?\s*ARSN\s*[^\n]{0,40}\n\s*([^\n]{3,90})", re.IGNORECASE),
    re.compile(r"(?:name|holder)\s*[:\s]\s*([^\n]{3,90})", re.IGNORECASE),
]

# Roles that mark a holder as an insider/manager rather than an outside investor.
_INSIDER_RE = re.compile(
    r"\bdirector\b|\bfounder\b|pty\s+ltd\s+as\s+trustee|family\s+trust|"
    r"\bmanager\b|management\s+(?:pty|limited|ltd)|\bassociates?\b",
    re.IGNORECASE,
)
_NOMINEE_RE = re.compile(
    r"nominee|custodian|hsbc\s+custody|citicorp|j\s?p\s?morgan\s+nominees|"
    r"national\s+nominees|bnp\s+paribas|computershare|\bclearing\b",
    re.IGNORECASE,
)


def classify_holder(name: str, cfg=None, exchange: str = None) -> str:
    """activist | insider | nominee | institution | unknown.

    Nominee accounts matter: on an ASX top-20 the largest lines are usually
    custodians, and reading "HSBC Custody Nominees 18%" as a blocking stake
    would wrongly rule out a perfectly winnable register.
    """
    if not name:
        return "unknown"
    if cfg is not None:
        from ..models.activist import match_activists
        if match_activists(cfg, exchange or "", [{"holder_name": name}]):
            return "activist"
    if _NOMINEE_RE.search(name):
        return "nominee"
    if _INSIDER_RE.search(name):
        return "insider"
    return "institution"


def parse_notice(text: str) -> Dict[str, Optional[object]]:
    """Pull (holder_name, pct) out of a substantial-holder / TR-1 notice."""
    out: Dict[str, Optional[object]] = {"holder_name": None, "pct": None}
    if not text:
        return out
    flat = re.sub(r"[ \t]+", " ", text)

    for pat in _PCT_PATTERNS:
        m = pat.search(flat)
        if m:
            v = to_float(m.group(1))
            # A voting stake above 100% is a parse error, not a holding.
            if v is not None and 0 < v <= 100:
                out["pct"] = v / 100.0
                break

    for pat in _NAME_PATTERNS:
        m = pat.search(flat)
        if m:
            name = re.sub(r"\s+", " ", m.group(1)).strip(" .:-\t")
            if len(name) >= 3 and not re.fullmatch(r"[\d\W]+", name):
                out["holder_name"] = name[:90]
                break
    return out


def from_lake(conn, cfg, market: str, start: str, end: str,
              reader: LakeReader = None) -> dict:
    """Holder rows parsed from substantial-holder notices already in the lake."""
    stats = {"market": market, "documents": 0, "parsed": 0, "rows": 0,
             "unparsed": 0, "status": "unavailable", "warnings": []}

    reader = reader or LakeReader()
    if not reader.status.available:
        stats["warnings"].append(f"lake unavailable: {reader.status.reason}")
        return stats
    stats["status"] = "ok"

    exchange = {"asx": "ASX", "uk": "LSE", "nz": "NZX"}.get(market, market.upper())
    known = {r["ticker"]: r["fund_id"] for r in
             conn.execute("SELECT ticker, fund_id FROM funds WHERE exchange=?", (exchange,))}
    if not known:
        stats["warnings"].append(f"no {exchange} funds in the universe yet")
        return stats

    docs = reader.scan(market, date_range(start, end), HOLDER_TITLE_RE, set(known))
    stats["documents"] = len(docs)
    stats["warnings"].extend(reader.status.warnings)

    now = utcnow_iso()
    rows = []
    for doc in docs:
        ticker = ((doc.get("company") or {}).get("ticker") or "").upper()
        fid = known.get(ticker)
        if not fid:
            continue
        text = (doc.get("content") or {}).get("text") or ""
        parsed = parse_notice(text)
        if not parsed["holder_name"] or parsed["pct"] is None:
            stats["unparsed"] += 1
            continue
        stats["parsed"] += 1
        rows.append({
            "fund_id": fid, "date": doc.get("published_date"),
            "holder_name": parsed["holder_name"],
            "holder_type": classify_holder(parsed["holder_name"], cfg, exchange),
            "pct": parsed["pct"],
            "source": f"lake:{market}-substantial-holder",
            "source_url": doc.get("url"), "source_status": "ok",
            "retrieved_at": now,
        })
    stats["rows"] = db.insert_holders(conn, rows)
    conn.commit()
    return stats


def latest_register(conn, fund_id: str) -> List[dict]:
    """The most recent disclosed position for each holder.

    Substantial-holder notices supersede one another, so only the latest per
    holder is a current position; summing every historical filing would count
    the same stake once per amendment and manufacture a concentrated register.
    """
    rows = conn.execute(
        "SELECT h.holder_name, h.holder_type, h.pct, h.date, h.source_url "
        "FROM holders h "
        "JOIN (SELECT holder_name, MAX(date) AS d FROM holders "
        "      WHERE fund_id=? AND pct IS NOT NULL GROUP BY holder_name) latest "
        "  ON h.holder_name = latest.holder_name AND h.date = latest.d "
        "WHERE h.fund_id=? AND h.pct IS NOT NULL "
        "ORDER BY h.pct DESC",
        (fund_id, fund_id),
    ).fetchall()
    return [dict(r) for r in rows]


def insider_pct(register: List[dict]) -> Optional[float]:
    """Total disclosed insider/manager holding, or None if nothing is known."""
    if not register:
        return None
    insiders = [r["pct"] for r in register
                if r.get("holder_type") == "insider" and r.get("pct") is not None]
    # No insider *rows* is genuinely ambiguous: it may mean no insider holding,
    # or an insider who never crossed the disclosure threshold. Only claim 0%
    # when we have a register substantial enough for the absence to mean
    # something.
    if not insiders:
        return 0.0 if len(register) >= 5 else None
    return sum(insiders)


def institutional_filing_count(conn, fund_id: str) -> Optional[int]:
    row = conn.execute(
        "SELECT COUNT(DISTINCT holder_name) AS n FROM holders "
        "WHERE fund_id=? AND pct IS NOT NULL AND holder_type IN "
        "('institution','activist')",
        (fund_id,),
    ).fetchone()
    if row is None:
        return None
    any_rows = conn.execute(
        "SELECT COUNT(*) AS n FROM holders WHERE fund_id=?", (fund_id,)
    ).fetchone()
    # Zero filings for a fund we never searched is not evidence of a retail
    # register — return None so the pillar renormalises instead.
    if not any_rows or any_rows["n"] == 0:
        return None
    return row["n"]
