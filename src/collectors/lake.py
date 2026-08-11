#!/usr/bin/env python3
"""Optional reader for the sibling `market-ingestion` S3 lake.

That repo already scrapes ASX announcements and UK RNS daily and stores them
text-extracted and deduped. NTA statements and substantial-holder notices are
exactly the documents this screen needs, so reading them there beats scraping
the same sites a second time — cheaper, kinder to the sources, and it inherits
their proven extraction.

The lake's consumer contract is honoured as documented in its README: check
`manifests/<market>/<date>.done.json` before reading a day, and never treat an
absent marker as an empty day. Note also that its ASX triage flags NTA
announcements as `is_admin_noise` — correct for a stock screener, wrong for
this one, so we deliberately read the flagged documents.

Entirely optional. Without AWS credentials the whole module reports
`unavailable` and the collectors fall back to their direct-fetch paths.
"""

import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

LAKE_PREFIX = os.environ.get("S3_LAKE_PREFIX", "market-data/")

# Titles that carry a per-share NTA/NAV figure.
NTA_TITLE_RE = re.compile(
    r"net\s+tangible\s+asset|NTA(?:\s|$)|net\s+asset\s+value|NAV(?:\s|$)|"
    r"monthly\s+(?:NTA|update)|daily\s+fund\s+update",
    re.IGNORECASE,
)

# Register-movement filings: ASX Forms 603/604/605, UK TR-1, NZX SSH notices.
HOLDER_TITLE_RE = re.compile(
    r"substantial\s+holder|substantial\s+(?:share)?holding|becoming\s+a\s+substantial|"
    r"ceasing\s+to\s+be\s+a\s+substantial|change\s+in\s+substantial|"
    r"form\s+60[345]|TR-?1|holding\(s\)\s+in\s+company|"
    r"notification\s+of\s+major\s+(?:holdings|interest)|SSH\s+notice",
    re.IGNORECASE,
)


@dataclass
class LakeStatus:
    available: bool = False
    reason: Optional[str] = None
    days_read: int = 0
    days_missing_marker: int = 0
    days_failed: int = 0
    documents: int = 0
    warnings: List[str] = field(default_factory=list)


class LakeReader:
    def __init__(self, bucket: str = None):
        self.bucket = bucket or os.environ.get("AWS_BUCKET_NAME")
        self.client = None
        self.status = LakeStatus()
        if not self.bucket:
            self.status.reason = "AWS_BUCKET_NAME not set"
            return
        if not os.environ.get("AWS_ACCESS_KEY_ID"):
            self.status.reason = "AWS_ACCESS_KEY_ID not set"
            return
        try:
            import boto3
            self.client = boto3.client("s3")
            # Probe once, up front: a credentials problem should be the first
            # line of the log, not a hundred silent misses later.
            self.client.list_objects_v2(Bucket=self.bucket, Prefix=LAKE_PREFIX, MaxKeys=1)
            self.status.available = True
        except Exception as e:
            self.status.reason = f"{type(e).__name__}: {str(e)[:200]}"
            self.client = None

    def _get_json(self, key: str):
        try:
            r = self.client.get_object(Bucket=self.bucket, Key=key)
            return json.loads(r["Body"].read().decode("utf-8", "replace"))
        except Exception:
            return None

    def _get_lines(self, key: str) -> List[dict]:
        try:
            r = self.client.get_object(Bucket=self.bucket, Key=key)
            body = r["Body"].read().decode("utf-8", "replace")
        except Exception:
            return []
        out = []
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out

    def day_status(self, market: str, date: str) -> Optional[str]:
        """The done-marker status, or None when no marker exists.

        None is not 'empty': the contract says an absent marker means the day
        was never successfully written, and reading it would silently
        under-count.
        """
        marker = self._get_json(f"{LAKE_PREFIX}manifests/{market}/{date}.done.json")
        return marker.get("status") if marker else None

    def manifest(self, market: str, date: str) -> List[dict]:
        return self._get_lines(f"{LAKE_PREFIX}manifests/{market}/{date}.jsonl")

    def document(self, key: str) -> Optional[dict]:
        return self._get_json(key)

    def scan(self, market: str, dates: List[str], title_re: re.Pattern,
             tickers: set = None, max_documents: int = None) -> List[dict]:
        """Documents across `dates` whose title matches, optionally restricted
        to a ticker set. Returns the full document dicts (text included)."""
        if not self.status.available:
            return []
        out = []
        for date in dates:
            status = self.day_status(market, date)
            if status is None:
                self.status.days_missing_marker += 1
                continue
            if status == "failed":
                self.status.days_failed += 1
                self.status.warnings.append(f"{market} {date}: lake run failed — day skipped")
                continue
            self.status.days_read += 1
            if status == "ok_empty":
                continue
            for entry in self.manifest(market, date):
                title = entry.get("title") or ""
                ticker = (entry.get("ticker") or "").upper()
                if tickers is not None and ticker not in tickers:
                    continue
                if not title_re.search(title):
                    continue
                doc = self.document(entry.get("key", ""))
                if doc:
                    out.append(doc)
                    self.status.documents += 1
                if max_documents and len(out) >= max_documents:
                    return out
        return out


def date_range(start: str, end: str) -> List[str]:
    import datetime
    d0 = datetime.date.fromisoformat(start)
    d1 = datetime.date.fromisoformat(end)
    days = []
    while d0 <= d1:
        days.append(d0.isoformat())
        d0 += datetime.timedelta(days=1)
    return days
