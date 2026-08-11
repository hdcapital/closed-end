# closed-end

A systematic screen for **closed-end funds and listed investment companies** on
the ASX, the LSE (including AIM investment companies) and the NZX.

The method is rock-turning: cover the whole universe from official sources,
estimate conservatively, apply the same mechanical rules to every fund, and
make each number auditable. The engine ranks funds two ways — by a conservative
expected forward return, and by how attackable they look to an activist — and
publishes the overlap as the priority pile.

The bias throughout is toward **not fooling ourselves**. Where a figure cannot
be sourced it is stored as `NULL` with a status explaining why; nothing is
imputed, and a gap in the data reads as a gap.

---

## Running it

```bash
pip install -r requirements.txt

python -m src.selftest                      # offline end-to-end, no network
python -m pytest tests/ -q                  # hand-computed model fixtures

python -m src.run --phase all               # full refresh
python -m src.run --phase universe --exchange asx
python -m src.run --phase prices --max-funds 5
python -m src.run --phase report
python -m src.run --phase all --offline     # cached documents only
```

Outputs land in `report/screen.csv` (full universe, every metric and
provenance flag) and `report/screen.html` (ranked tables plus the data-quality
appendix).

Everything tunable lives in `config.yaml`. No prior, weight or threshold is
hardcoded in `src/`; if you find one, that is a bug.

### Automation

| Workflow | Cadence | What it does |
|---|---|---|
| `screen-monthly.yml` | 6th, 04:23 UTC | Universe + ASX monthly report + NTA panel + prices + models + report |
| `screen-weekly.yml` | Mondays, 05:11 UTC | Prices and discounts only, then rescore and rebuild |
| `tests.yml` | every push | `pytest` + offline selftest |

The monthly job runs on the 6th because the ASX Investment Products report for
the prior month lands in the first few business days. Raw documents and the
SQLite store are cached between runs, so re-runs don't re-fetch archived
reports that can never change.

**Optional integration.** The sibling `hdcapital/market-ingestion` repo already
scrapes ASX announcements and UK RNS daily into an S3 lake. Where its four AWS
secrets are present, this engine reads NTA statements and substantial-holder
notices from that lake instead of scraping the same sites again, honouring its
documented done-marker contract. Without the credentials those layers report
`unavailable` and the rest of the run proceeds.

---

## Data sources and what each is worth

| Leg | Primary source | Cross-check | Honest assessment |
|---|---|---|---|
| **ASX** | Monthly Investment Products report (XLSX): code, name, mandate, market cap, pre/post-tax NTA, price, premium/discount for every LIC and LIT | Per-fund monthly NTA announcements | **The best source in the project.** Official, monthly, and one archived file per month is a whole-universe panel. Build here first. |
| **LSE** | *Currently none that works* | — | **Blocked, not broken.** The public `Issuer list_N.xlsx` is a company list: no TIDM, no ISIN, ICB Super-Sector only. Probed and rejected — see PROGRESS. |
| **NZX** | Human-verified seed list in `config.yaml`, cross-checked against the NZX instrument list | — | **Semi-manual by design and currently unverified.** ~12 vehicles; NZX publishes no clean machine-readable list of them. |
| **Prices** | Yahoo Finance via `yfinance`, daily closes and distributions | Month-end price in the ASX report | A convenience source with guard rails, not a reference source. |

### Known limitations, source by source

- **ASX archived reports.** Roughly **24 months deep, and that is the ceiling**:
  archive URLs constructed from the current report's pattern all 404, so older
  editions are not retained. The other spreadsheets the landing page links are
  the ETF and structured-product editions of the same file — they parse and
  contribute nothing, because their tickers are not LICs. Because 24 months is
  below the model's 5-year floor, ranking uses the report's own published
  5-year total return, tagged `stated` (see "Returns" below).
- **ASX benchmark rows.** The Spotlight sheet appends index rows (S&P/ASX 200
  Accumulation and friends) next to the funds. They are excluded by name and by
  ticker shape; one reached the top-20 table before that was added.
- **UK universe.** The LSE's public issuer list carries **no ticker and no
  ISIN**, so it cannot identify or price a fund; `find_header` refuses it rather
  than mis-parsing it, and the UK leg is currently empty. Three options are
  listed in PROGRESS for the owner to choose between (AIC licence/endpoint, a
  commercial reference feed, or the LSE SPA's own API). The RNS NAV parser
  exists and is tested; it has nothing to point at until the universe exists.
- **AIC.** A membership body, not a data provider. `http.respect_robots` is
  honoured, so if their robots.txt disallows the path the run records
  `robots_denied` and falls back to LSE data alone. Their list is also rendered
  client-side, so a successful fetch may still yield no rows — reported, not
  papered over.
- **Yahoo.** A missing ticker suffix (`.AX`, `.L`, `.NZ`) resolves to a
  *different real company* on another exchange and returns plausible prices
  rather than an error. Every symbol is therefore checked against the
  exchange's expected currency before its prices are stored, and LSE pence
  quotes (`GBp`/`GBX`) are converted — an unconverted 100× scale error looks
  exactly like a 99% discount and would sit at the top of the screen.

---

## The formulas

### Returns

Annualised NTA **total** return — NAV growth with distributions reinvested:

```
index_{i+1} = index_i × (nta_{i+1} + distributions paid in (t_i, t_{i+1}]) / nta_i
r_window    = (index_end / index_start)^(1/years) − 1
```

Reported for 5y, 10y and since-inception.

- Distributions are reinvested at the **end-of-period** NTA, not at the NTA on
  their ex-date. With a monthly series that is the best available
  approximation; it slightly understates a fund paying large distributions into
  a rising NAV, and is applied identically to every fund.
- **One NTA type per fund, never a blend.** Pre-tax is primary for the ASX,
  cum-income for the UK. Mixing pre- and post-tax would manufacture a return at
  every point the source changed which figure it published.
- A trailing window is only reported when the history actually reaches back
  (20 days of tolerance for observation-date drift). A 5-year label computed
  off 3 years of data is exactly the self-deception this screen exists to avoid.
- **Stated figures fill windows that cannot be computed**, never ones that can.
  Provenance is per window, not per fund: `r5_source` / `r10_source` /
  `r_all_source` each say `computed` or `stated`, so the two never share a
  column while a fund may legitimately carry one of each. On the ASX this is
  the difference between a working screen and an empty one, because the
  publisher's archive is shallower than its own performance table. Turn it off
  with `run.allow_stated_returns_for_ranking: false`.
- **Scale breaks are dropped, not repaired.** Some ASX editions publish a small
  fund's NTA in cents and the next in dollars. An observation more than 20x
  from the median of its immediate neighbours is excluded from the series; the
  comparison is local so a fund that genuinely compounded 10x is not truncated.
  Rescaling by 100 would usually be right, and usually-right silent corrections
  are what this screen exists to avoid.

### Discounts

```
discount_t = price_t / nta_t − 1          (negative = discount)
```

Reported as current, 5y mean, 10y mean, all-time mean, 5y standard deviation,
and the current z-score against the fund's own 5y history.

- A price is matched to the nearest **preceding** NTA, no more than
  `max_price_nta_gap_days` (45) stale. A discount struck against an NAV that had
  not yet been published is lookahead bias, and in a backtest it is the
  flattering kind.
- Means are trading-day-weighted, so a month with a suspended quote contributes
  less than a fully traded one.
- The z-score is **withheld** below 24 observations in the 5y window, or when
  the discount history is flat — a z-score off six points is noise wearing a
  statistic's clothing.

### Conservative expected forward return

```
E[annual return] = g_conservative + r_discount + y_income − drag        (H = 5y)
```

**1. Growth (`g_conservative`).** Asymmetric and deterioration-sensitive:

```
g5 <  g10 (deteriorating):  base = 0.6·g5 + 0.4·g10      weight the recent decay
g5 ≥ g10 (improving):       base = 0.3·g5 + 0.7·g10      anchor to the long run
                            capped at g10 + 1.00pp        never extrapolate a hot run
shrinkage:  w = n_years / (n_years + 10)
            g_shrunk = w·base + (1−w)·prior
g_conservative = min(g_shrunk, 12%) − haircut
haircut = 1.0% flat, +0.5% if OCR > 1.5% or a performance fee exists
```

Where a fund has no full 10-year window, the since-inception figure stands in
as the long-run anchor. Sector priors, deliberately modest: listed equity 6.5%,
small-cap equity 7%, debt/credit 5%, property 5%, private equity 7%,
infrastructure 6%, hedge/multi-asset 4.5%, unknown 5%.

**2. Discount reversion (`r_discount`).** Wide discounts are often structural,
so the target respects both the fund's own history and its peers':

```
d_star = 0.5·d_own + 0.5·d_peer
d_own  = 10y mean discount (else all available)
d_peer = median current discount of the same sector on the same exchange
         (n ≥ 5), else the same sector globally, always excluding the fund itself

d0 < d_star  (cheap):    d_H = d0 + 0.5·(d_star − d0)     only half the gap closes
d0 ≥ d_star  (tight):    d_H = d_star                      premiums fully deflate

r_discount = ((1 + d_H) / (1 + d0))^(1/H) − 1
positive r_discount ×= min(1, |z| / 1.5)                   never damp a negative
```

The 50/50 blend is the point: it stops the model assuming an illiquid private
equity trust closes to NAV just because it once traded there. The peer group
excludes the subject fund, since a group containing it drags its own target
toward itself and damps the very reversion being measured.

**3. Income (`y_income`) — zero by default.** `g` is already a *total* NTA
return, so adding a distribution yield on top would count distributions twice.
Trailing yield is reported as its own column. `include_yield_in_forward_return`
turns it on for anyone who disagrees.

**4. Drag — 0.5% p.a. flat.** Trading friction, discount-volatility drag, and
general model optimism. This is a fudge factor and is labelled as one.

**5. Wind-up scenario** (reported separately, never in the headline, and only
for the top decile of activist score):

```
((1 − 0.02) / (1 + d0))^(1/3) − 1 + g_conservative
```

Full discount capture to 2% below NTA over three years. It assumes a campaign
both happens *and* succeeds — a scenario, not an expectation.

### Activist-target score

Three pillars, each 0–100, combined **40 / 35 / 25**.

**A. Prize (40%)** — depth of the discount below `d_star` (35%), persistence
(share of the last 3 years spent wider than −10%, 25%), size sweet spot (20%),
and 5y NAV underperformance vs the sector median (20%). The size band peaks at
£50m–£600m / A$100m–A$1bn: big enough to absorb a stake and repay campaign
costs, small enough to actually accumulate 5–20%.

**B. Winnable register (35%)** — a known activist already on the register
(45%), concentration structure (25%), insider alignment (20%), retail-heaviness
(10%). Ideal is fragmented: top-20 between 30% and 60% with no single holder
above 20%. Insiders below 2% score full marks; above 25% the campaign is
near-unwinnable and scores zero.

**C. Executable endgame (25%)** — liquidity of the underlying assets (45%),
trigger events (35%), governance friction (20%). A listed-equity portfolio can
be liquidated near NAV and scores 95; private equity scores 20, because there
the discount is partly deserved and the endgame is not really available.

**Missing data is renormalised, not defaulted.** A component that cannot be
computed is dropped and the remaining weights in its pillar are rescaled.
Scoring an unknown as 0 would punish thin coverage as though it were bad news;
scoring it 50 would invent a fact. Every score therefore carries a `coverage`
fraction, and a high score at low coverage is a hypothesis, not a finding.

---

## Known issues with the model

Implemented exactly as specified; these are flagged rather than silently
"improved", because changing a specification without saying so is worse than
an imperfect specification.

1. **It is a total return, not a price return.** `g` is an NTA *total* return
   (distributions reinvested), so the sum estimates what a holder earns
   *including* distributions received in cash. Calling it an expected *price*
   return and then setting `y_income = 0` to avoid double counting arrives at
   the right number under the wrong name — a price-return model would need
   `g` to be NAV-per-share growth *ex* distributions. The report labels the
   column a total return.

2. **The terms are added, not compounded.** `g + r ≠ (1+g)(1+r) − 1`. The error
   is second-order (~30bp at g=8%, r=4%) and always makes the estimate
   *smaller*, so it errs conservative.

3. **Damping is undefined when the z-score is missing.** The spec damps
   positive reversion by `min(1, |z|/1.5)` but is silent on what to do when `z`
   cannot be computed. Granting full credit there is precisely the
   self-deception the damping exists to prevent, so `z_missing_damping`
   defaults to `0.0` — the upside is withheld — and every affected fund says so
   in its decomposition. Set it to `1.0` for the opposite reading.

4. **The register pillar tops out at 96, not 100.** A register that is merely
   not retail-heavy scores the *neutral* 60 on that component, never 100,
   because being institutional is not itself a positive. Deliberate, but it
   means the pillar's practical ceiling is below its nominal one.

5. **`d_star` is symmetric about a fund's own history.** A trust that has
   *always* traded at −40% has `d_own = −40%`, so half its target is its own
   pathology. That is intentional (structural discounts are real), but it means
   the model will never flag a permanently-broken fund as cheap on reversion
   alone — the activist score is what is supposed to catch those.

## Known biases in the data

- **Survivorship.** Historical discount and return averages are computed from
  funds that still exist. Vehicles wound up or acquired during the window are
  absent. Most closed-end funds die either at a wide discount (bad) or via a
  premium-closing corporate action (good), so the bias runs both ways and is
  **not** estimated here. Delisted funds encountered in historical sources are
  kept with `status='delisted'` so the gap is at least visible.
- **Stated vs computed returns.** Never mixed within a column. Every return
  carries `returns_provenance` (`computed` | `stated` | `unavailable`) and the
  report counts them in the appendix.
- **NTA staleness.** A monthly NTA against a daily price makes the discount
  series stale by construction — up to 45 days by the matching rule. Uniform
  across funds, but it means a fast-moving portfolio's discount is noisier than
  it looks.
- **Pre/post-tax NTA inconsistency.** ASX LICs are carried on pre-tax NTA; UK
  trusts on cum-income NAV. These are not the same quantity. An Australian LIC
  with a large deferred tax liability looks cheaper on pre-tax NTA than a UK
  trust with identical economics. Cross-market comparisons inherit this, and
  the report says so above the tables.
- **Register coverage is a floor, not a measurement.** Substantial-holder
  filings only reveal holders above the disclosure threshold (5% ASX, 3% UK),
  so a "top-20 = 42%" derived from them means "disclosed holders = 42%".

---

## Layout

```
src/
  config.py      config access that refuses YAML's string-shaped numbers
  db.py          SQLite schema; provenance enforced by NOT NULL, not by habit
  fetch.py       cached, rate-limited, robots-respecting HTTP
  tabular.py     header-finding, fuzzy column mapping for human spreadsheets
  universe/      asx.py, uk.py, nz.py, common.py
  collectors/    asx_monthly.py, nta.py, nta_text.py, prices.py, holders.py, lake.py
  metrics/       returns.py, discounts.py
  models/        forward_return.py, activist.py
  report/        build.py
  pipeline.py    two-pass metric -> model -> score
  run.py         CLI
  selftest.py    offline end-to-end
data/raw/        cached source documents (gitignored)
data/db/         funds.sqlite (gitignored)
config.yaml      every prior, weight and threshold
```

**Store schema:** `funds`, `nta_observations`, `price_observations`, `holders`,
`fund_events`, `derived_metrics`, `scores`, `source_log`. The observation
tables declare `source` and `retrieved_at` `NOT NULL`, so a figure nobody can
trace back to a document cannot be inserted. `source_log` records every fetch
attempt, successful or not, which is what makes "we have no data for X" a
checkable claim rather than an excuse.

See `PROGRESS.md` for what currently works, what is stubbed, and the
data-quality problems found so far.
