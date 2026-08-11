# PROGRESS

Running log of what works, what is stubbed, and what needs a decision from the
repo owner. Newest entry first.

---

## 2026-08-11 — Phases 0–5 built; no live data validated

### The headline problem: this session had no egress to any market data source

Every financial host is refused at the proxy by the organisation's egress
policy. Confirmed by direct probe, not inferred:

| Host | Result |
|---|---|
| `www.asx.com.au` | 403 at CONNECT |
| `www.londonstockexchange.com`, `docs.londonstockexchange.com` | 403 at CONNECT |
| `www.theaic.co.uk` | 403 at CONNECT |
| `www.nzx.com` | 403 at CONNECT |
| `query1.finance.yahoo.com` | 403 at CONNECT |

Per the proxy's own documentation these are policy denials, not transient
errors, and are not to be retried or routed around. PyPI and GitHub are
allowed; web *search* works, so source URL patterns could be researched even
though the documents themselves could not be fetched.

The AWS credentials present in the environment are placeholders —
`AWS_BUCKET_NAME` is unset and `ListBuckets` returns `InvalidAccessKeyId` — so
the sibling `market-ingestion` lake could not be read either.

**What this means for the code.** GitHub Actions runners have normal egress, so
the collectors are written as real, working fetch-and-parse code rather than
stubs, and the whole engine is exercised offline against synthetic fixtures
whose answers are known. What has **not** happened is a single parse of a real
ASX, LSE, AIC, NZX or Yahoo document. Treat the first live run as the real
integration test.

**Decision for the owner:** either allow those five hosts through the egress
policy for this repo's sessions, or accept that the first validation happens in
Actions. Nothing in the code needs to change either way.

### What works, verified offline

- **49 unit tests + a 20-check end-to-end selftest, all passing**, both fully
  offline. Every expected value in the model tests is derived longhand in its
  docstring, so a changed weight fails against a number a human can re-check on
  paper rather than one regenerated from the code under test.
- **Store.** Full schema; provenance is structural — the selftest asserts that
  an NTA row with no `source` is rejected by the database.
- **Fetch layer.** Caching, per-host rate limiting, robots.txt, and — verified
  against the live proxy — correct classification of an egress block as
  `blocked` rather than a site error. The two need different fixes and must not
  look alike in a data-quality report.
- **Spreadsheet parsing.** Header-finding under a title banner, fuzzy column
  mapping, footnote stop, percent/fraction normalisation, LIC vs LIT detection,
  mandate → sector taxonomy. Exercised on a synthetic file shaped like the ASX
  monthly report.
- **NTA text extraction.** Pre/post-tax and cum/ex-income variants, dollars,
  cents and pence, "as at" date extraction, and rejection of totals-only
  announcements. A real RNS wrapping bug was found and fixed this session: the
  `(cum-income)` qualifier routinely sits on a different line from the "net
  asset value" label, which a same-line regex missed.
- **Metrics, both models, pipeline, CSV + HTML report, CLI, three workflows.**
  A full `--phase all --offline` run completes and degrades honestly: every
  unreachable source is named above the rankings, and no fund is silently
  dropped.

### Stubbed or thin — ranked by how much it matters

1. **UK NAV history is the weakest leg.** The `stated` provenance path exists
   and the store distinguishes stated from computed, but no stated figures are
   populated, because the AIC page is both robots-gated and client-side
   rendered. The RNS NAV parser (`nta_text`) is written and tested but is only
   wired to the lake reader, not to a direct RNS source. **Options for the
   owner:** (a) an AIC data licence, (b) point the lake's existing UK RNS
   ingestion at NAV announcements — cheapest, since that repo already scrapes
   Investegate daily, (c) a commercial NAV feed.
2. **NZ is semi-manual and currently unverified.** Three seed funds (KFL, BRM,
   MLN) are configured with `verified: false` and are stored with
   `source_status='semi_manual_unverified'`. The list is knowingly incomplete.
   **Needs owner sign-off against the live NZX list before the NZ leg is
   trusted** — an unverified NZ row should not sit beside an ASX row sourced
   from an official monthly report as though the two were equally solid.
3. **Fund-level fundamentals are not collected.** `ocr`,
   `has_performance_fee`, `externally_managed`, `fee_on_gross_assets` and chair
   tenure are schema columns with no collector. Consequences: the +0.5% fee
   haircut never fires, and the governance component of the endgame pillar
   always renormalises away. Both are visible in the coverage figure rather
   than hidden.
4. **`fund_events` has no collector.** Continuation votes, manager-agreement
   expiries, buyback authorities and tender history are the *most decisive*
   input to the endgame pillar and currently come from nowhere. A
   config-maintained list is the pragmatic first step; parsing UK articles and
   annual reports is the thorough one.
5. **Annual-report top-20 PDF extraction is not built.** Register data
   therefore depends entirely on substantial-holder filings, which reveal only
   holders above the disclosure threshold. The retail-heaviness inference is
   correspondingly crude.
6. **Delisted funds are only recorded when a source mentions them.** Nothing
   actively sweeps historical reports for vehicles that have disappeared, so
   the survivorship caveat in the report is a warning rather than a
   quantification.

### Data-quality issues found while building

- **PyYAML resolves `50e6` to a string**, not a number (YAML 1.1 wants
  `5.0e+7`). Two size thresholds were written that way and would have compared
  false, silently changing every size score. `config.num()` now raises on a
  non-numeric value instead of coercing, and the config uses plain integers.
- **Binding an explicit `NULL` overrides a SQLite column `DEFAULT`.** Every
  caller omitting `source_status` hit a `NOT NULL` failure. Caught by a test,
  fixed in the store helpers.
- **Dataclass fields shadow same-named modules.** `returns: returns.ReturnSet`
  fails because an annotated assignment binds the field name *before*
  evaluating the annotation.
- **The sibling lake flags NTA announcements as `is_admin_noise`.** Correct for
  a stock screener, wrong for this one. The lake reader deliberately reads the
  flagged documents; anyone reusing that reader should know why.
- **Substantial-holder notices supersede one another.** Summing every
  historical filing would count one stake once per amendment and manufacture a
  concentrated register, so only the latest filing per holder counts as a
  position.
- **Nominee lines dominate ASX top-20 tables.** Reading "HSBC Custody Nominees
  18%" as a blocking stake would wrongly rule out perfectly winnable
  registers, so custodians are classified apart from insiders.

### Open questions for the owner

1. **Egress policy** — allow the five market hosts, or validate in Actions?
2. **UK NAV** — which of the three options above?
3. **NZ** — sign off the seed list, or drop the NZ leg until it can be sourced
   properly?
4. **`z_missing_damping` defaults to 0.0**, withholding reversion upside where
   the discount z-score cannot be computed. This is the most consequential
   judgement call not specified in the brief: it will zero the reversion term
   for any fund with a short or flat discount history. Set it to `1.0` for the
   opposite reading, or `0.5` for a middle course.
5. **Email digest** — the sibling repos use AWS + Actions but no email
   mechanism was found in `market-ingestion` to mirror, so none was built. The
   report is committed to the repo and uploaded as an artifact. Say the word
   and it can post to wherever the other projects notify.
