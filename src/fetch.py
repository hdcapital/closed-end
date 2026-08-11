#!/usr/bin/env python3
"""Polite, cached HTTP with an auditable failure mode.

Design rules, in priority order:

1. **Never fabricate.** A fetch returns a `Fetched` whose `.ok` is False and
   whose `.status` says why. Callers write NULL + that status into the DB.
   Nothing downstream is allowed to invent a number to fill the hole.
2. **Cache raw documents.** Everything lands in data/raw/<host>/<hash>-<name>
   so re-runs don't re-hit the source and a human can open the exact bytes a
   figure came from.
3. **Be a good citizen.** One request per host per `min_interval_seconds`,
   robots.txt honoured, real User-Agent naming the project.
4. **An egress block is not an error to route around.** When the network
   refuses us (403 on CONNECT through a corporate proxy, DNS blackhole), the
   status is `blocked` and the run continues with that source missing and
   loudly reported. See PROGRESS.md.
"""

import hashlib
import os
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Optional

import requests

from .util import utcnow_iso

# status values written to source_log
OK = "ok"
CACHED = "cached"
HTTP_ERROR = "http_error"
BLOCKED = "blocked"
ROBOTS_DENIED = "robots_denied"
PARSE_ERROR = "parse_error"
SKIPPED = "skipped"


@dataclass
class Fetched:
    url: str
    status: str
    content: Optional[bytes] = None
    http_status: Optional[int] = None
    cache_path: Optional[str] = None
    detail: Optional[str] = None
    retrieved_at: str = field(default_factory=utcnow_iso)

    @property
    def ok(self) -> bool:
        return self.status in (OK, CACHED) and self.content is not None

    @property
    def text(self) -> str:
        if not self.content:
            return ""
        return self.content.decode("utf-8", "replace")


def _looks_like_egress_block(exc: Exception) -> bool:
    """Distinguish "the network won't let us out" from "the site is down".

    A policy proxy answers CONNECT with 403/407 and requests surfaces that as a
    ProxyError/TunnelError. Calling that an http_error would hide an
    infrastructure problem inside a data-quality report.
    """
    s = f"{type(exc).__name__}: {exc}".lower()
    return any(
        m in s
        for m in (
            "proxyerror", "tunnel connection failed", "cannot connect to proxy",
            "403", "407", "egress", "blocked by", "name or service not known",
            "temporary failure in name resolution",
        )
    )


class Fetcher:
    def __init__(self, cfg, conn=None, offline: bool = False):
        self.cfg = cfg
        self.conn = conn
        self.offline = offline or os.environ.get("CLOSED_END_OFFLINE") == "1"
        self.ua = cfg.get("http.user_agent")
        self.min_interval = cfg.num("http.min_interval_seconds")
        self.timeout = cfg.num("http.timeout_seconds")
        self.max_retries = int(cfg.num("http.max_retries"))
        self.respect_robots = bool(cfg.get("http.respect_robots"))
        self.cache_ttl_days = cfg.num("http.cache_ttl_days")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.cache_dir = os.path.join(root, cfg.get("http.cache_dir"))
        os.makedirs(self.cache_dir, exist_ok=True)
        self._last_hit: dict = {}
        self._robots: dict = {}
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": self.ua})
        # Counters the run summary reports, so a mostly-blocked run is obvious.
        self.counts = {OK: 0, CACHED: 0, HTTP_ERROR: 0, BLOCKED: 0,
                       ROBOTS_DENIED: 0, SKIPPED: 0}

    # -- cache ------------------------------------------------------------
    def cache_path_for(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.replace(":", "_") or "unknown"
        name = os.path.basename(parsed.path) or "index"
        name = "".join(c for c in name if c.isalnum() or c in "._-")[:80]
        digest = hashlib.sha256(url.encode()).hexdigest()[:12]
        return os.path.join(self.cache_dir, host, f"{digest}-{name}")

    def _cached(self, url: str) -> Optional[str]:
        path = self.cache_path_for(url)
        if not os.path.exists(path):
            return None
        age_days = (time.time() - os.path.getmtime(path)) / 86400.0
        if age_days > self.cache_ttl_days:
            return None
        return path

    # -- robots -----------------------------------------------------------
    def _robots_allows(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parsed = urllib.parse.urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        rp = self._robots.get(base)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(base + "/robots.txt")
            try:
                # Fetch robots through the same session so it obeys the proxy.
                r = self._session.get(base + "/robots.txt", timeout=self.timeout)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except Exception:
                # Unreachable robots.txt is not consent. But it is also not a
                # denial: treat as allow-with-caution, the conventional reading,
                # and let the rate limiter carry the politeness.
                rp.parse([])
            self._robots[base] = rp
        try:
            return rp.can_fetch(self.ua, url)
        except Exception:
            return True

    # -- rate limit -------------------------------------------------------
    def _wait(self, url: str) -> None:
        host = urllib.parse.urlparse(url).netloc
        last = self._last_hit.get(host)
        if last is not None:
            delta = time.time() - last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
        self._last_hit[host] = time.time()

    # -- main -------------------------------------------------------------
    def get(self, url: str, kind: str = "document", force: bool = False) -> Fetched:
        cached = None if force else self._cached(url)
        if cached:
            with open(cached, "rb") as f:
                content = f.read()
            self.counts[CACHED] += 1
            self._log(url, kind, CACHED, cache_path=cached, bytes_=len(content))
            return Fetched(url=url, status=CACHED, content=content, cache_path=cached)

        if self.offline:
            self.counts[SKIPPED] += 1
            self._log(url, kind, SKIPPED, detail="offline mode: no cached copy")
            return Fetched(url=url, status=SKIPPED,
                           detail="offline mode and nothing cached")

        if not self._robots_allows(url):
            self.counts[ROBOTS_DENIED] += 1
            self._log(url, kind, ROBOTS_DENIED, detail="robots.txt disallows this path")
            return Fetched(url=url, status=ROBOTS_DENIED,
                           detail="robots.txt disallows this path for our User-Agent")

        last_detail = None
        for attempt in range(self.max_retries):
            self._wait(url)
            try:
                r = self._session.get(url, timeout=self.timeout)
            except Exception as e:                       # network/proxy layer
                last_detail = f"{type(e).__name__}: {e}"
                if _looks_like_egress_block(e):
                    self.counts[BLOCKED] += 1
                    self._log(url, kind, BLOCKED, detail=last_detail)
                    return Fetched(url=url, status=BLOCKED, detail=last_detail)
                time.sleep(2 ** attempt)
                continue

            if r.status_code == 200:
                path = self.cache_path_for(url)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(r.content)
                self.counts[OK] += 1
                self._log(url, kind, OK, http_status=200, bytes_=len(r.content),
                          cache_path=path)
                return Fetched(url=url, status=OK, content=r.content,
                               http_status=200, cache_path=path)

            # 403/407 from a policy proxy looks like an HTTP error but isn't
            # the site's answer — record it as a block so the report says so.
            if r.status_code in (403, 407) and "proxy" in (r.reason or "").lower():
                self.counts[BLOCKED] += 1
                self._log(url, kind, BLOCKED, http_status=r.status_code,
                          detail=f"proxy refused: {r.reason}")
                return Fetched(url=url, status=BLOCKED, http_status=r.status_code,
                               detail=f"proxy refused: {r.reason}")

            last_detail = f"HTTP {r.status_code} {r.reason}"
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt * 2)
                continue
            break                                        # 4xx: retrying won't help

        self.counts[HTTP_ERROR] += 1
        self._log(url, kind, HTTP_ERROR, detail=last_detail)
        return Fetched(url=url, status=HTTP_ERROR, detail=last_detail)

    def get_first(self, urls, kind: str = "document") -> Fetched:
        """Try candidate URLs in order; return the first success, else the last
        failure (so the caller still gets a status to record)."""
        result = None
        for u in urls:
            result = self.get(u, kind=kind)
            if result.ok:
                return result
        return result or Fetched(url="", status=SKIPPED, detail="no candidate URLs")

    def _log(self, url, kind, status, **kw) -> None:
        if self.conn is None:
            return
        from . import db
        db.log_source(self.conn, url=url, kind=kind, status=status, **kw)
        self.conn.commit()

    def summary(self) -> str:
        return ", ".join(f"{k}={v}" for k, v in self.counts.items() if v)
