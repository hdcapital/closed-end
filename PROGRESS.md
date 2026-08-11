# PROGRESS

Running log of what works, what is stubbed, and what needs a decision from the
repo owner. Newest entry first.

---

## 2026-08-11 (evening) — ASX leg working; UK leg blocked on data availability

### ASX now produces a real ranking

| | first live run | now |
|---|---|---|
| rankable funds | 0 | most of the universe |
| top forward return | 96% (nonsense) | 7.45% |
| sectors classified | 42 `unknown` | 6 `unknown`, 56 `equity` |
| NAV sanity breaks | 607 | 30 (source defect, filtered from the series) |

The top of the table now reads like a screen — LSX 7.45%, LRT 6.77%, TGF 6.63%
— with `g_conservative`, `base`, `prior` and `r_discount` printed beside each so
every figure can be checked by eye.

### Two findings that changed the design

**1. The ASX archive is ~24 months deep and cannot be extended.** Every
constructed archive URL returns 404, so ASX does not retain older editions at
that path. The landing page's other spreadsheet links are the ETF and
structured-product editions of the same file: they parse perfectly and
contribute nothing because their tickers are not LICs. Two years is the honest
depth of this source.

That made the model's 5-year floor unreachable and left the whole universe
unrankable. The resolution is the one the brief already specified — store the
stated 5y/10y total return where the raw series is unobtainable, tagged
`source=stated` vs `source=computed` — and the report publishes a *5 Year Total
Return (ann.)* for every fund. The rule is that the two never share a column,
not that a stated figure is unusable, so **provenance is now tracked per
window**: a fund may carry a computed since-inception return and a stated
5-year one, each labelled (`r5_source` in the CSV, `g5 src` in the HTML).
Computed always wins where it exists. `run.allow_stated_returns_for_ranking:
false` reverts to recomputed-only, which today leaves ASX unrankable.

**2. Benchmark index rows were being ranked as funds.** The first properly
ranked table had XSOAI — the S&P/ASX Small Ordinaries Accumulation Index — at
number 10. The Spotlight sheet appends benchmark rows next to the funds. Now
excluded by name *and* by ticker shape (ASX index codes are X plus four
letters), because either test alone has a hole.

### The UK leg: probed, and the file does not exist publicly

`src.probe_uk` swept the landing pages and a spread of candidate filenames. The
result is decisive:

* `Issuer list_N.xlsx` **is** real and current — `_90` = Feb 2025, `_95` = May
  2025, `_100` = Sep 2025, numbering rising roughly five per quarter.
* **Every edition has the same header and none carries a ticker or an ISIN:**
  `Admission Date | Company Name | ICB Industry | ICB Super-Sector | Country of
  Incorporation | World Region | Market | International Issuer | Company Market
  Cap (£m)`.
* The LSE `/reports` pages are client-side rendered and link no spreadsheets.

So this is a *company* list, not an instrument list. Without a TIDM there is no
way to price a fund, and ICB Super-Sector is far too coarse to identify a
closed-ended one. The `find_header` guard was refusing it correctly the whole
time; this was never a parsing bug. `instrument_list_fallbacks` is now empty
with the finding recorded inline.

**Owner decision needed — the UK universe (~350 trusts, the largest leg) needs
one of:**

1. **AIC data licence or their JSON endpoint.** Their site renders client-side,
   so the data is behind an API the page already calls. Best fit: their sector
   taxonomy is the one the market actually uses for peer groups.
2. **A commercial reference-data feed** (TIDM + ISIN + ICB subsector).
3. **The LSE's own SPA API** — the instrument list the page renders must come
   from somewhere; identifying that endpoint is a browser-devtools job, and
   whether scraping it is acceptable is a terms-of-service call for the owner,
   not one the code should make quietly.

Until one is chosen the UK leg stays at zero funds and says so in the report.

### Still open

- Register and events remain empty (0 holders, 0 events): no lake credentials
  in CI and no direct filing collector. Activist scores therefore run at low
  evidence coverage, which the `coverage` column states.
- NZ is three seed funds, `verified: false`, listing-confirmed against NZX.
- 70 tests + selftest, green offline and on a clean runner.



## 2026-08-11 (later) — first live runs against the real sources

Two GitHub Actions runs with real network access. Both green; the value was in
what they exposed. Reachability probe: **all five hosts answered 200** from the
runner (asx.com.au, londonstockexchange.com, theaic.co.uk, nzx.com, Yahoo), so
the block is specific to the development session, not to the project.

### What works against live data

- **ASX is the strong leg, as expected.** The collector discovered the current
  report by itself (`asx.com.au/content/dam/asx/issuers/asx-investment-products-reports/2026/excel/asx-investment-products-jun-2026-abs.xlsx`),
  parsed 94 rows to 92 live funds + 2 excluded, and pulled **84 archived monthly
  reports** (7 years, 6,154 NTA observations) with zero fetch failures.
- **Prices**: 40 funds, ~99k rows, zero currency mismatches, zero failures. The
  three "possibly delisted" tickers were reported, not silently dropped.
- **NZX**: all three seed tickers confirmed still quoted against the live list.
- Offline gates (61 tests + selftest) pass identically on a clean runner.

### The big one: the NTA series was the discount column

The live ASX header is:

    ASX Code | Type | Fund Name | MER (% p.a) | Outperf Fee | Mkt Cap ($m)# |
    Mkt Cap ($m) Change | ... | Prem/Disc % NTA (pre-tax) at N | NTA Date |
    NTA Price | Last Close | ... | 5 Year Total Return (ann.)

Three columns were mis-mapped at once, all by substring collisions:

| Field | Matched | Should have matched |
|---|---|---|
| `nta_pre_tax` | `Prem/Disc % NTA (pre-tax) at N` | `NTA Price` |
| `price` | `NTA Price` | `Last Close` |
| `market_cap` | *(unmapped)* | `Mkt Cap ($m)#`, ×1e6 |

So the "NAV series" was a percentage. The NTA sanity check caught it
immediately — **607 implausible month-on-month steps**, ratios to ×1374 — and
the model-input table showed growth "base" values of 13,000% p.a. The 12% cap
and the 5-year history floor contained the damage (only 21 of 95 funds were
ever rankable), but the ranking was still built on a wrong column. **No
synthetic fixture of mine caught this, because I had invented the header.**

Fixed, with the failure mode closed rather than patched:

1. `ColumnMap` now takes `{"match": [...], "not": [...]}`. Level columns
   exclude any header containing `prem`, `disc`, `%`, `return`, `change`,
   `yield` or `date`, making the collision unrepresentable.
2. An ingest-time plausibility guard rejects any per-share NTA outside
   [0.005, 1000] and reports how many it rejected — the backstop for header
   spellings nobody anticipated.
3. `tests/test_asx_report.py` pins the **real** header verbatim, including a
   cross-check that `price / NTA - 1` agrees with the report's own published
   premium/discount. Mis-mapped columns cannot agree.
4. Units now come from the header where the publisher declares them: `MER
   (% p.a)` holding `0.15` is 15bp, and the magnitude heuristic read it as 15%.

### The report is richer than assumed — now collected

The same header carries **MER**, **Outperf Fee**, **Historical Distribution
Yield** and **1/3/5-Year Total Return (ann.)**. Consequences:

- `ocr` and `has_performance_fee` are now populated, so the +0.5% fee haircut
  can fire for the first time (previously it was dead code in production).
- Stated 1/3/5-year total returns are stored as `derived_metrics` with
  `provenance='stated'` — the fallback for funds whose archive history is too
  short to compute from. **Not yet wired into the model**; the pipeline still
  ranks on computed history only. That wiring is the next obvious step and
  needs a decision on whether a stated g5 should make a fund rankable.
- `mandate` now maps to the `Type` column, so the 42 funds that fell to sector
  `unknown` (and therefore the humble 5% prior) should classify properly.

### Still broken / open

1. **LSE remains at zero funds.** The fallback file downloads fine but is the
   wrong file: `Issuer list.xlsx` is a September **2020** snapshot whose
   `Companies` sheet has no ISIN and no TIDM — only Admission Date, Company
   Name, ICB Industry, Market, Market Cap. The `find_header` guard correctly
   refused it rather than mis-parsing. What is needed is the correct current
   LSE instrument-list URL; the landing-page scrape didn't yield one. This is
   the largest remaining gap — roughly 350 trusts, the biggest universe of the
   three.
2. **AIC confirmed client-side rendered** — fetches 200, yields no rows. Needs
   a JSON endpoint or their data licence.
3. **Register and events remain empty** (0 holder rows, 0 events): the lake has
   no credentials in CI, and no direct filing collector exists.
4. `archive_months` is trimmed by the smoke workflow for run time; a full
   refresh should raise it back to the configured 130.

### Third run: the column fix confirmed, and a residual source defect

Re-running with the corrected mapping took the NTA sanity check from **607
suspicious steps to 30** — the mis-mapped column is gone. The 30 that remain
are a genuine inconsistency in the source rather than a parsing bug: for a
handful of small funds the ASX report publishes NTA in cents in some editions
and dollars in others, giving panels like TOP `0.91 -> 92.10 -> 1.01` and BEL
`0.0100 -> 0.9400`.

Handled in `metrics/returns.drop_scale_breaks`: an observation more than 20x
from the median of its immediate neighbours is dropped from the series. The
test is deliberately *local* — a whole-history median would reject the real
growth of a fund that compounded 10x across the panel. And the point is
**dropped, not rescaled**: multiplying by 100 would usually be right, and
"usually right" is exactly the silent correction this project exists to avoid.
A gap in the series is honest; a fabricated level is not.

### Fourth run: values now sane, but history coverage collapsed — OPEN

With the corrected mapping the model output is finally plausible: top forward
returns 5-10% (was 96%), `r_discount` 0-1.5% (was 86%), and sectors classify as
`equity` off the `Type` column instead of falling to `unknown`.

But **NTA coverage fell from ~67 month-ends per fund to ~19**, so every fund now
shows 1.8 years of history and **none is rankable**. Two readings, not yet
distinguished:

* The previous 7-year history was entirely fake — it was the discount column —
  so 1.8 years may simply be the true parseable depth of these 84 files.
* Or older editions of the report name the NTA column something the current
  spec doesn't reach, and the exclusion list now rejects it.

The evidence that it is at least partly the second: `NTA by type` is now
`unspecified 1732` with **zero** rows classified `pre_tax`, meaning the level is
only ever found via the generic `NTA Price` match. The archived-report parse
warnings that would name each edition's header were being **discarded** by
`from_asx_archive` whenever a file parsed its rows successfully — so a file
could parse cleanly, yield no NTA at all, and say nothing. That is now fixed:
the collector counts reports that produced no NTA level and reports the
distinct header signatures it saw. The next run's log should say plainly which
editions are unmapped.

**Do not read any ranking from these runs.** Nothing is rankable, register and
event data are empty, and the archive depth is unresolved.

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
- **Nominee lines dominate ASX top-20 tables.** A custodian holds for many
  unrelated beneficiaries and cannot block anything, so reading "HSBC Custody
  Nominees 18%" as a control block would wrongly rule out a perfectly winnable
  register. Custodians are classified apart from insiders *and* excluded from
  the blocking-stake test, while still counting toward top-20 (which measures
  how much of the register is disclosed at all). Caught by rendering the
  report on realistic data and noticing the score disagreed with its own
  documentation.

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
