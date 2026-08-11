#!/usr/bin/env python3
"""Small shared helpers: time, dates, safe numerics."""

import datetime
import math
import re
from typing import Optional

ISO_DATE = "%Y-%m-%d"


def utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(ISO_DATE)


def parse_date(value) -> Optional[datetime.date]:
    """Best-effort date parse across the shapes these sources actually emit.
    Returns None rather than guessing when the string is ambiguous junk."""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    s = str(value).strip()
    if not s:
        return None
    s = s.split("T")[0].strip()
    fmts = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y",
            "%b %Y", "%B %Y", "%Y/%m/%d", "%d-%b-%Y", "%d-%b-%y", "%m/%d/%Y")
    for fmt in fmts:
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    m = re.match(r"^(\d{4})-(\d{2})$", s)
    if m:                                   # a bare month means month-end
        return month_end(int(m.group(1)), int(m.group(2)))
    return None


def month_end(year: int, month: int) -> datetime.date:
    if month == 12:
        return datetime.date(year, 12, 31)
    return datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)


def days_between(a, b) -> Optional[int]:
    da, db = parse_date(a), parse_date(b)
    if da is None or db is None:
        return None
    return abs((db - da).days)


def year_fraction(a, b) -> Optional[float]:
    """Years between two dates on a 365.25-day basis."""
    da, db = parse_date(a), parse_date(b)
    if da is None or db is None:
        return None
    return (db - da).days / 365.25


def to_float(value) -> Optional[float]:
    """Parse a number out of the messy strings these documents carry:
    "$1.23", "1,234.5", "(0.4)" for negatives, "12.3%", "n/a", "-".

    Returns None for anything not confidently numeric. Never returns 0.0 as a
    stand-in for "missing" — a zero NTA and an unknown NTA are different facts.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None if (isinstance(value, float) and math.isnan(value)) else float(value)
    s = str(value).strip()
    if not s or s.lower() in {"n/a", "na", "nan", "-", "--", "none", "nil", ""}:
        return None
    negative = s.startswith("(") and s.endswith(")")
    if negative:
        s = s[1:-1]
    pct = s.endswith("%")
    s = s.rstrip("%")
    s = re.sub(r"[^\d.\-+eE]", "", s)
    if s in {"", "-", "+", ".", "-."}:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    if negative:
        v = -v
    if pct:
        v /= 100.0
    return v


def annualise(total_growth: float, years: float) -> Optional[float]:
    """Convert a cumulative growth multiple to an annualised rate.

    `total_growth` is the ending/starting multiple (1.5 = +50% cumulative).
    A non-positive multiple has no real annualised rate — return None rather
    than raising, because a fund whose NAV went to zero is data, not a crash.
    """
    if total_growth is None or years is None or years <= 0 or total_growth <= 0:
        return None
    return total_growth ** (1.0 / years) - 1.0


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def mean(values) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def median(values) -> Optional[float]:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def stdev(values) -> Optional[float]:
    """Sample standard deviation. None below two points — one observation has
    no dispersion, and returning 0.0 would make every z-score infinite."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    m = sum(vals) / len(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))


def ramp(x: float, lo: float, hi: float) -> float:
    """Linear 0->1 ramp between lo and hi, flat outside. Used by the scorers."""
    if hi == lo:
        return 1.0 if x >= hi else 0.0
    return clamp((x - lo) / (hi - lo), 0.0, 1.0)


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
