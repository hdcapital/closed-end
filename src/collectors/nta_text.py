#!/usr/bin/env python3
"""Pull NTA/NAV per share out of announcement text.

ASX LICs lodge a monthly NTA statement; UK trusts announce NAV by RNS. Both are
short documents with the number stated in prose or a two-column table, and
neither has a machine-readable format. So: regex, with the ordering and the
sanity checks doing the real work.

Two rules keep this from producing confident nonsense:

1. **Label before number.** A figure is only accepted when it sits next to a
   label that names it ("NTA before tax", "NAV per share"). Grabbing "the first
   number on the page" finds the ASX release header or a date more often than
   it finds the NTA.
2. **Plausibility.** Per-share NTAs live in a narrow range. A match outside it
   is rejected and reported, because the usual cause is a cents/dollars mix-up
   or a total-net-assets figure ($412,345,678) captured as a per-share one.
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional

# Per-share values outside this range are rejected as implausible. UK trusts
# quote NAV in pence (e.g. 342.5p) so the upper bound has to accommodate that.
MIN_PER_SHARE = 0.0001
MAX_PER_SHARE = 100000.0

_NUM = r"(\(?-?[$£]?\s*[\d,]+(?:\.\d+)?\)?\s*(?:c|p|cents?|pence)?)"

# Ordered: the most specific label wins, so "NTA after tax" is never captured
# by the generic "NTA" pattern.
_PATTERNS = [
    ("pre_tax", [
        r"(?:pre[\-\s]?tax|before\s+tax)\s*(?:NTA|net\s+tangible\s+assets?)[^\n\r:]{0,40}?[:\s]\s*" + _NUM,
        r"(?:NTA|net\s+tangible\s+assets?)\s*(?:per\s+(?:share|unit|security)\s*)?"
        r"(?:\((?:pre[\-\s]?tax|before\s+tax)\)|(?:pre[\-\s]?tax|before\s+tax))[^\n\r:]{0,40}?[:\s]\s*" + _NUM,
        r"NTA\s+before\s+tax[^\n\r]{0,40}?[:\s]\s*" + _NUM,
    ]),
    ("post_tax", [
        r"(?:post[\-\s]?tax|after\s+tax)\s*(?:NTA|net\s+tangible\s+assets?)[^\n\r:]{0,40}?[:\s]\s*" + _NUM,
        r"(?:NTA|net\s+tangible\s+assets?)\s*(?:per\s+(?:share|unit|security)\s*)?"
        r"(?:\((?:post[\-\s]?tax|after\s+tax)\)|(?:post[\-\s]?tax|after\s+tax))[^\n\r:]{0,40}?[:\s]\s*" + _NUM,
        r"NTA\s+after\s+tax[^\n\r]{0,40}?[:\s]\s*" + _NUM,
    ]),
    # A UK NAV announcement routinely wraps between the "net asset value per
    # ordinary share" label and the "(cum-income)" qualifier, so these two get
    # a bounded window that may cross a line break. The window is short enough
    # that the label and qualifier still have to belong to the same sentence.
    ("cum_income", [
        r"(?:NAV|net\s+asset\s+value)[\s\S]{0,90}?\(?\s*(?:cum[\-\s]?income|"
        r"including\s+(?:current\s+period\s+)?income)\s*\)?[^\n\r:]{0,40}?[:\s]\s*" + _NUM,
        r"\(?\s*(?:cum[\-\s]?income|including\s+(?:current\s+period\s+)?income)\s*\)?"
        r"[\s\S]{0,40}?(?:NAV|net\s+asset\s+value)[^\n\r:]{0,40}?[:\s]\s*" + _NUM,
    ]),
    ("ex_income", [
        r"(?:NAV|net\s+asset\s+value)[\s\S]{0,90}?\(?\s*(?:ex[\-\s]?income|"
        r"excluding\s+(?:current\s+period\s+)?income)\s*\)?[^\n\r:]{0,40}?[:\s]\s*" + _NUM,
        r"\(?\s*(?:ex[\-\s]?income|excluding\s+(?:current\s+period\s+)?income)\s*\)?"
        r"[\s\S]{0,40}?(?:NAV|net\s+asset\s+value)[^\n\r:]{0,40}?[:\s]\s*" + _NUM,
    ]),
    ("unspecified", [
        r"(?:NTA|NAV|net\s+asset\s+value|net\s+tangible\s+assets?)\s*"
        r"per\s+(?:ordinary\s+)?(?:share|unit|security)[^\n\r:]{0,30}?[:\s]\s*" + _NUM,
        r"(?:NTA|NAV)[^\n\r:]{0,30}?[:\s]\s*" + _NUM,
    ]),
]

_COMPILED = [(k, [re.compile(p, re.IGNORECASE) for p in pats]) for k, pats in _PATTERNS]


@dataclass
class NtaExtract:
    values: dict = field(default_factory=dict)     # nta_type -> value
    units: dict = field(default_factory=dict)      # nta_type -> "dollars"|"cents"|"pence"
    rejected: List[str] = field(default_factory=list)
    matched_text: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.values)


def _parse_number(raw: str):
    """Return (value, unit). Cents/pence are converted to the major unit, and
    the original unit is retained so the report can flag a fund whose series
    changed denomination mid-history."""
    s = raw.strip()
    negative = s.startswith("(") and s.endswith(")")
    unit = "major"
    low = s.lower()
    if re.search(r"(?:\d\s*)(?:c|cents?)\b", low):
        unit = "cents"
    elif re.search(r"(?:\d\s*)(?:p|pence)\b", low):
        unit = "pence"
    cleaned = re.sub(r"[^\d.]", "", s)
    if not cleaned or cleaned.count(".") > 1:
        return None, unit
    try:
        v = float(cleaned)
    except ValueError:
        return None, unit
    if negative:
        v = -v
    if unit in ("cents", "pence"):
        v /= 100.0
    return v, unit


def extract(text: str) -> NtaExtract:
    """Extract every labelled NTA/NAV figure from an announcement."""
    out = NtaExtract()
    if not text:
        return out
    # Collapse the whitespace PDF extraction sprays through table cells, but
    # keep newlines so the "same line as the label" constraint still bites.
    text = re.sub(r"[ \t ]+", " ", text)

    for nta_type, patterns in _COMPILED:
        if nta_type in out.values:
            continue
        for pat in patterns:
            m = pat.search(text)
            if not m:
                continue
            value, unit = _parse_number(m.group(1))
            if value is None:
                continue
            if not (MIN_PER_SHARE <= abs(value) <= MAX_PER_SHARE):
                out.rejected.append(
                    f"{nta_type}={value} rejected as implausible per-share value "
                    f"(from {m.group(0)[:60]!r})"
                )
                continue
            out.values[nta_type] = value
            out.units[nta_type] = unit
            out.matched_text[nta_type] = m.group(0)[:120]
            break

    # A generic match that duplicates a labelled one adds nothing and would
    # otherwise become a second, redundant row in the NTA series.
    if "unspecified" in out.values and (
        out.values["unspecified"] in
        {v for k, v in out.values.items() if k != "unspecified"}
    ):
        out.values.pop("unspecified")
        out.units.pop("unspecified", None)
        out.matched_text.pop("unspecified", None)

    return out


# ---------------------------------------------------------------------------
# Announcement-date extraction
# ---------------------------------------------------------------------------

_AS_AT = re.compile(
    r"as\s+at\s+(?:the\s+)?(\d{1,2}\s+\w+\s+\d{4}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|"
    r"\d{4}-\d{2}-\d{2}|\w+\s+\d{4})",
    re.IGNORECASE,
)


def extract_as_at(text: str) -> Optional[str]:
    """The "as at" date the NTA refers to, which is usually month-end and
    routinely differs from the lodgement date by a week or more. Using the
    lodgement date instead would smear the whole panel."""
    from ..util import parse_date
    if not text:
        return None
    m = _AS_AT.search(text)
    if not m:
        return None
    d = parse_date(m.group(1))
    return d.isoformat() if d else None
