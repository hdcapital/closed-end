#!/usr/bin/env python3
"""Universe hygiene shared by all three exchanges."""

import re
from typing import List, Optional, Tuple
from urllib.parse import urljoin


def compile_exclusions(cfg) -> List[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in cfg.get("universe.exclusion_patterns")]


def should_exclude(name: str, extra: str, patterns: List[re.Pattern]) -> Optional[str]:
    """Return the reason this vehicle is out of universe, or None to keep it.

    Excluded funds are still stored with status='excluded' and this reason, so
    the next run doesn't rediscover them and a human can check the call.
    """
    text = f"{name or ''} {extra or ''}"
    for p in patterns:
        m = p.search(text)
        if m:
            return f"matched exclusion pattern /{p.pattern}/ on '{m.group(0)}'"
    return None


def find_links(html: str, base_url: str, *, extensions: Tuple[str, ...] = (),
               keywords: Tuple[str, ...] = ()) -> List[str]:
    """Absolute links from a landing page filtered by extension and keyword.

    Used instead of hardcoding download filenames: both ASX and LSE have moved
    these files, and a link scraped from the live page survives that.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue
        url = urljoin(base_url, href)
        haystack = f"{url} {a.get_text(' ', strip=True)}".lower()
        if extensions and not any(url.lower().split("?")[0].endswith(e) for e in extensions):
            continue
        if keywords and not any(k.lower() in haystack for k in keywords):
            continue
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def dedupe_key(row: dict) -> str:
    """Dedupe on ISIN where available, else exchange+ticker, per spec."""
    isin = (row.get("isin") or "").strip().upper()
    if isin:
        return f"ISIN:{isin}"
    return f"{(row.get('exchange') or '').upper()}:{(row.get('ticker') or '').upper()}"
