# Decisions — Phase 0 Portfolio Analyzer

> Evolving. Each entry is dated. Append new decisions at the bottom; never silently
> rewrite history — if a decision is reversed, add a new entry referencing the old one.

---

## 2026-04-21 — Project bootstrap

### D1. Scope
- **Decision:** Build Phase 0 only (portfolio analyzer). Phase 1 deferred.
- **Rationale:** User directive. VCP screening depends on a stable decision + data backbone first.

### D2. Stack & location
- **Decision:** Python, project at `portfolio-analyzer/` under workspace root.
- **Alternatives considered:** Go (gokiteconnect), Kotlin/Java. Rejected for faster
  iteration and pandas/numpy ergonomics for OHLC math.

### D3. Data source
- **Decision:** Zerodha Kite Connect for **everything** — holdings, per-stock OHLC,
  index candles, and breadth constituent OHLC. No secondary provider (e.g., yfinance).
- **Rationale:** User provides Kite credentials; single source avoids symbol-mapping drift.

### D4. Credentials
- **Decision:** `.env` for `KITE_API_KEY` / `KITE_API_SECRET`. `access_token` lives in
  process memory only — **no disk persistence**. Interactive login every run.
- **Rationale:** User directive. Trade-off: ~30s of manual login per run, accepted.

### D5. Market trend composition
- **Decision:**
  - Primary = NIFTY 500 (70% weight)
  - Secondary = NIFTY 50 (30% weight)
  - Per-index classification uses MA-stack (price vs 50DMA vs 200DMA).
  - Blended trend = weighted map of `{UP:+1, SIDEWAYS:0, DOWN:-1}` with thresholds ±0.5.
- **Rationale:** User-specified weights. MA-stack chosen over fixed return thresholds
  for robustness across volatility regimes.

### D6. Breadth signal
- **Decision:** `breadth_pct` = % of NIFTY 500 constituents with `price > 50DMA`.
  Regimes: ≥65% strong, 40–65% mixed, <40% weak.
- **Override rule (confirmed):** Blended UPTREND + breadth <40% → downgrade to SIDEWAYS.

### D7. `market.return_50d` source
- **Decision:** Reported value = NIFTY 500 50-day return (also used as market reference for RS).
- **Rationale:** Consistent with primary benchmark in D5.

### D8. Per-stock metrics
- **52w high:** close-based (max of last 252 daily closes). More stable than daily-high-based.
- **return_50d:** `(price / close_50_trading_days_ago) - 1`.
- **RS:** `stock.return_50d - nifty500.return_50d`.
- **drawdown_from_high:** `(price - 52w_high) / 52w_high` (negative).
- **trend:** STRONG if `price > 50DMA > 200DMA`, else WEAK.

### D9. Scoring & decisions
- **Decision:** Implement exactly as spec:
  - `+2` STRONG trend · `+2` positive RS · `+1` drawdown > -10% · `-2` drawdown < -25%
  - `≥3` HOLD · `1–2` REDUCE · `≤0` EXIT.

### D10. Rate limiting
- **Decision:** Token bucket at 2.5 req/sec for historical calls (Kite limit = 3). Dedup
  between NIFTY 500 breadth set and portfolio holdings.

### D11. Tests never hit Kite
- **Decision:** All unit tests use synthetic OHLC fixtures. No live API calls in `pytest`.

### D12. Working doc location
- **Decision:** `context.md`, `decisions.md`, `todos.md` live at project root (this dir),
  not at workspace root.

---

## 2026-04-21 — Plan approved, pending items resolved

### D13. Symmetric breadth override (was P1)
- **Decision:** Yes — blended DOWNTREND + breadth >65% → upgrade to **SIDEWAYS**.

### D14. Extended `market` JSON (was P2)
- **Decision:** Yes — emit additional fields additively:
  `breadth_pct`, `breadth_regime`, `nifty500_trend`, `nifty50_trend`.
  Original contract fields unchanged.

### D15. Negative diagnostic reasons (was P3)
- **Decision:** Yes — `reasons[]` includes brief negative diagnostics
  (e.g., `"Below key MAs"`, `"Underperforming market"`) even when they
  don't shift the score.

### D16. Sector mapping (was P4)
- **Decision:** Ship `data/sector_map.csv` with header-only (plus any rows discovered
  at first run stubbed to `UNKNOWN`). User fills manually; no auto-lookup.

### D17. Static NIFTY CSVs (was P5)
- **Decision:** Ship `data/nifty500.csv`, `data/nifty50.csv` as header-only templates
  with a dated source note. User pastes symbols from official NSE lists. No refresher
  script in Phase 0 — analyzer logs a warning if files are empty.

### D18. NIFTY 500 breadth fetch budget
- **Decision:** Accept ~3–4 min full-universe fetch per run. Mitigations:
  (a) rate limiter at 2.5 req/sec, (b) in-memory dedup with portfolio holdings,
  (c) progress log every 25 calls. No sampling / no caching in Phase 0.

### D19. Holdings eligibility
- **Decision:** Analyze NSE equity only. Holdings where `exchange != "NSE"` or where
  the instrument type is not equity (ETFs, bonds, etc.) are **skipped with a warning**
  and excluded from `portfolio_summary` counts.

### D20. `top_performers` / `weakest_stocks` ranking
- **Decision:** Rank by `score` descending (for top) / ascending (for weakest). Ties
  broken by `relative_strength`. Array size = **3** each. Fewer if portfolio has <3
  eligible stocks.

---

## Reversals / Revisions

*(none yet)*

---

## 2026-04-21 — Implementation notes (post-build)

### N1. Python version
- Project targets Python 3.10+. Local venv uses `python3.12` (system default `python3` is 3.9 on this machine).

### N2. Index tradingsymbols (unverified)
- `config.py` uses `"NIFTY 500"` and `"NIFTY 50"` as the `tradingsymbol` values expected from
  `kite.instruments("NSE")`. This will be verified at first live run — the exact symbols
  Kite returns for indices may differ (e.g. no space, or a different segment). If needed,
  update `NIFTY500_INDEX_SYMBOL` / `NIFTY50_INDEX_SYMBOL` constants.

### N3. InstrumentCache filter
- Currently accepts NSE rows where `segment == "NSE"` **or** `instrument_type == "EQ"`.
  This keeps indices (typically `segment == "INDICES"`) accessible via `cache.token()`.
  Revisit once we see actual instrument rows at runtime.

---

## 2026-04-21 — Auto-refresh of constituent lists

### D21. Auto-refresh reference data daily
- **Decision:** On every `run`, if local CSVs are not dated today, re-download from
  niftyindices.com. Files: `data/nifty500.csv`, `data/nifty50.csv`,
  `data/sector_map.auto.csv` (derived from `Industry` column).
- **CLI additions:** `refresh [--force]` subcommand; `run --no-refresh` for offline use.
- **Failure mode:** On network error, log warning and reuse existing files (graceful degradation).
- **Sanity check:** Reject suspiciously small payloads (<100 for NIFTY 500, <10 for NIFTY 50).

### D22. Sector map precedence
- **Decision:** `data/sector_map.csv` (manual user overrides) wins over
  `data/sector_map.auto.csv` (auto-generated) at merge time. Either may be absent.

### D23. HTTP User-Agent
- **Decision:** Use a standard Chrome-like UA for niftyindices.com requests.
  niftyindices.com blocks minimal / empty UAs (observed: 20s read-timeout on bot UAs).

### D24. Refresh file formats
- **Decision:** Symbol CSVs written as `# refreshed <date> from <url>\nsymbol\n<SYM>\n...`
  (backward-compatible with existing `load_symbol_list`). Sector map as two-column CSV.

### D25. Kite instruments dump — no disk caching
- **Decision:** `kite.instruments("NSE")` continues to be fetched fresh in-memory on every run.
  Not cached to disk.
- **Rationale:** Marginal optimization; Kite call is cheap and server-cached. Unlike
  niftyindices.com (flaky, separate host), Kite reliability is already acceptable.
  Revisit only if startup latency becomes a real pain point.
- **Superseded by D26** — instruments dump no longer fetched.

### D26. Price data source — yfinance, not Kite historical
- **Decision:** Daily close series (indices, holdings, breadth universe) are fetched from
  Yahoo Finance via `yfinance`. Kite is used only for auth + `holdings()`.
- **Context:** Kite returned `PermissionException: Insufficient permission for that call.`
  on `historical_data()`. The Historical Data API is a separate ₹2000/month add-on on top
  of the base Kite Connect subscription.
- **Rationale:**
  - Phase 0 is a personal decision-support tool; paying another ₹24k/year for daily candles
    is not justified.
  - yfinance is free, reasonably reliable for NSE equities and NSE indices, and supports
    batch downloads (hundreds of tickers per call).
  - Data correctness for our signals (price, 50/200 DMA, 52w high, 50d return) does not
    require tick-level precision — Yahoo's daily closes are adequate.
- **Ticker mapping:**
  - NSE equity `SYM` → `SYM.NS`
  - NIFTY 500 → `^CRSLDX`
  - NIFTY 50  → `^NSEI`
- **Implementation:** `yf_fetch.fetch_daily_closes()` batches tickers (default 50) through
  `yf.download(..., threads=False)`. `threads=True` triggered a `database is locked` race
  on yfinance's internal SQLite cache during initial testing.
- **Fallback:** Any ticker missing from a batch result is retried individually via
  `yf.Ticker(t).history()`. Observed on first live run that batch download occasionally
  drops valid tickers (SAKSOFT.NS, ECLERX.NS) even though single-ticker fetches succeed
  — this is a known yfinance batch quirk, not a symbol problem.
- **Removed:** `util/rate_limit.py`, `KiteClient.fetch_daily_close`,
  `KiteClient.fetch_instruments_nse`, `InstrumentCache`. The Kite historical rate-limit
  budget is no longer relevant.
- **Trade-offs accepted:**
  - Yahoo occasionally lags or has missing rows for illiquid NSE names; affected symbols
    are logged and skipped.
  - No official SLA from Yahoo — if the endpoint breaks, fallback is to subscribe to the
    Kite Historical API add-on (revert D26) or another provider.

### D27. BSE-listed holdings — include, resolve against NSE ticker
- **Decision:** Kite `holdings()` records with `exchange="BSE"` are no longer skipped.
  Their `tradingsymbol` is used directly against yfinance with the `.NS` suffix.
- **Rationale:** Major dual-listed names (BAJFINANCE, SBICARD, SWIGGY, etc.) have identical
  tradingsymbols on NSE and BSE. NSE daily closes are the correct reference for our
  NIFTY 500-relative signals, regardless of which venue the user bought on.
- **Dedup:** If a user holds the same symbol on both exchanges, it is scored once.
- **Fallback:** Genuine BSE-only symbols will miss yfinance and be logged as
  "No price history; excluded" — acceptable until such a case appears.

## 2026-04-21 — Backtest scaffolding (Phase 0)

### D-BT1. Initial portfolio = NIFTY 50 equal-weight on start date
- **Decision:** Backtest starts by buying each of the 50 NIFTY 50 stocks with
  equal capital (`initial_capital / 50` each) at the start date's **Open**.
- **Rationale:** Deterministic, reproducible, avoids anachronism of projecting
  today's holdings backwards. 50 positions give enough signal to study exits.
- **Known bias:** Uses today's NIFTY 50 list, not point-in-time. Acceptable for
  Phase 0; would require paid historical constituent data to fix.

### D-BT2. Backtest window = 5 years
- **Decision:** Default window is the last 5 years ending on the backtest start
  date (passed as CLI flag, defaults to yesterday's close).
- **Rationale:** ~1250 trading days — statistically meaningful, short enough
  that NIFTY 50 composition drift is tolerable.

### D-BT3. Decision state machine (one-way)
- **Decision:** Each position has a state: `FULL → REDUCED → EXITED`. Transitions
  are one-way; a position cannot re-enter or be "un-reduced".
  - `HOLD` signal → no action
  - `REDUCE` signal while `FULL` → sell 50%, move to `REDUCED`
  - `REDUCE` signal while already `REDUCED` → no action
  - `EXIT` signal → sell remaining, move to `EXITED`
- **Rationale:** Prevents oscillating signals from repeatedly halving a position.
  Phase 0 has no re-entry logic so `EXITED` is terminal.

### D-BT4. Cash reinvestment = none
- **Decision:** Cash from REDUCE/EXIT sits idle, earns 0% interest.
- **Rationale:** Phase 0 is sell-only; no entry rules exist. Interest accrual
  is a refinement for a later phase.

### D-BT5. Breadth override — use today's NIFTY 500 list retroactively
- **Decision:** When computing `breadth_pct` at historical dates, use the
  current NIFTY 500 constituent list (same one refreshed daily from niftyindices).
- **Known bias:** Survivorship bias — today's list excludes stocks delisted
  during the backtest window. Documented and accepted for Phase 0.

### D-BT6. Price adjustments for backtest = auto-adjusted
- **Decision:** Backtest fetcher uses `yf.download(..., auto_adjust=True)` so
  that splits and bonuses don't appear as spurious price drops / EXIT signals.
- **Difference from live analyzer:** Live analyzer uses `auto_adjust=False`
  (matches Kite/NSE raw close). Backtest uses adjusted prices throughout.

### D-BT7. Transaction costs = zero (Phase 0)
- **Decision:** No brokerage, STT, stamp duty, or slippage modelled.
- **Revisit:** Add as parameters once strategy shows meaningful edge over
  buy-and-hold; realistic Zerodha costs are ~0.1% round-trip which could
  shift borderline decisions.

### D-BT8. Signal evaluation = daily, execution = T+1 Open
- **Decision:** Signal computed on day T close. Trade executed at day T+1 Open.
- **Edge case:** If T+1 is the last available bar, trade is skipped (no close
  to mark against).

### D-BT9. Data caching = parquet per ticker
- **Decision:** Backtest fetches full-window OHLC per ticker and caches to
  `data/backtest_cache/{ticker}.parquet`. Re-runs within the same window read
  from cache. Cache files are gitignored.
- **Refresh rule:** Cache is considered fresh if it covers the requested
  `[start, end]` window. Outside that, the ticker is re-fetched.

### D-BT10. Loss-avoided metric formula
- **Decision:** For each EXIT signal at day T, compute the stock's return
  from day T+1 Open to day T+21 Close. Average across all EXIT signals.
  - Positive average → exits preceded drawdowns on average (good)
  - Negative average → exits were premature on average (bad)
- **Companion metric "hit rate on EXIT":** fraction of EXIT signals where
  the 21-day forward return is negative.

### D-BT11. Reuse existing scoring pipeline
- **Decision:** Backtest does NOT duplicate scoring logic. It vectorizes over
  dates by calling `stock_analysis.compute_metrics` and `scoring.score_stock`
  point-in-time on date-sliced series (or the vector equivalent).
- **Rationale:** Guarantees live analyzer and backtest agree on the same
  decision for the same inputs. Any future scoring change takes effect in
  both automatically.

## 2026-04-22 — Phase 0 strategy fixes (post-backtest review)

Initial 5y backtest was structurally broken: ~1.3% CAGR vs ~14% NIFTY 500,
~0 avg exposure by end, 60% of EXITs preceded UP moves (premature). Fixes
below restore exposure discipline and reduce churn, at the cost of making the
strategy stateful. D-BT12..18 apply to **both** live analyzer and backtest
(reaffirming D-BT11 parity goal, with the strategy pipeline now state-aware).

### D-BT12. Raise score thresholds (Fix 4)
- **Decision:** `HOLD_SCORE_MIN = 4` (was 3), `REDUCE_SCORE_MIN = 2` (was 1).
  Score ≤ 1 is still required for a raw EXIT signal.
- **Rationale:** Old thresholds fired HOLD too easily and EXIT too eagerly.
  HOLD now requires strong trend + positive RS at minimum.

### D-BT13. Hard EXIT gate (Fix 1)
- **Decision:** A score-based EXIT signal is suppressed unless at least one of
  `price < 200DMA` or `drawdown_from_high < -0.15` holds. Suppressed EXITs
  are downgraded to `REDUCE`.
- **Constant:** `EXIT_GATE_DRAWDOWN = -0.15`.
- **Rationale:** Stops the strategy from dumping stocks on borderline scores
  alone. True exit requires observable weakness.

### D-BT14. Decision state machine with hysteresis (Fix 2)
- **Decision:** Actionable decisions come from a state machine over
  `{HOLD, REDUCE, EXIT}`, keyed on the stock's **previous-day decision**:

  ```
  prev=HOLD   →  EXIT    iff hard gate (D-BT13)
              →  REDUCE  iff per-stock trend=WEAK AND RS<0
              →  HOLD    otherwise
  prev=REDUCE →  EXIT    iff hard gate
              →  HOLD    iff per-stock trend=STRONG AND RS>0 (upgrade)
              →  REDUCE  otherwise
  prev=EXIT   →  EXIT    (terminal; no re-entry in Phase 0)
  ```
- **Supersedes D-BT3.** The one-way rule is relaxed: REDUCE→HOLD upgrade is
  now allowed. EXIT remains terminal.
- **On REDUCE→HOLD upgrade:** broker position is not re-bought; the upgrade
  re-arms the state so a later REDUCE transition can halve the position
  again (50% → 25%). Documented as "re-armed reducer".
- **First-day seed:** stocks held at the backtest start are seeded with
  previous-state = `HOLD`. In the live analyzer, the previous-state comes
  from yesterday's JSON (see D-BT17); if missing, `HOLD` is assumed.
- **Raw score decision** (from `scoring.score_stock`, D-BT12) is retained in
  output as a **diagnostic**, labelled `raw_signal`. `decision` is the
  state-machine output.

### D-BT15. Soft exposure floor in UPTREND (Fix 3)
- **Decision:** While the day's market trend (D11 blended + D15 override) is
  `UPTREND`, the simulator refuses to execute any REDUCE or EXIT that would
  drop invested-fraction below 50% of total equity.
  - Blocked trades do **not** advance the state machine for that stock;
    the signal will be re-attempted on subsequent days.
  - No re-buying is performed when exposure is above 50% (Phase 0 has no
    entry rules; floor is enforced by *withholding sells* only).
- **Rationale:** Prior backtest ended fully in cash. A soft floor preserves
  market exposure when the regime is favourable.
- **Applies to backtest only for now.** The live analyzer does not model a
  portfolio size, so the floor cannot be enforced there; it is purely a
  simulator-side constraint.

### D-BT16. New metrics: avg_exposure, exit_quality (Fixes 5, 6)
- **`avg_exposure`:** arithmetic mean of daily `invested_mv / equity` across
  the backtest window. Target ≥ 0.5 for the fix to be effective.
- **`exit_quality_rate`:** fraction of EXIT signals whose 21d forward return
  is negative (promoted/renamed from `hit_rate_negative_21d`). Target > 0.5.
- Both metrics included in the JSON backtest report.

### D-BT17. Live analyzer reads yesterday's JSON for prev-state (Fix 2 live)
- **Decision:** `cli.run` looks for the most recent JSON in `output/` with
  date strictly earlier than today. If found, each stock's `decision` is
  loaded as that stock's `prev_state` input to the state machine. If absent,
  or if the file is >7 calendar days old, `prev_state` defaults to `HOLD` and
  a warning is logged.
- **Rationale:** Avoids DB/state-file plumbing; re-uses the JSON artefact
  the user already persists. Simple, inspectable, transparent.
- **Bootstrap note:** first run after this change treats every stock as
  `HOLD` — acceptable because the next day's file will seed state correctly.

### D-BT18. Raw-signal diagnostic in live JSON
- **Decision:** Live JSON output gains a `raw_signal` field alongside
  `decision`. `raw_signal` is the score-based call (HOLD/REDUCE/EXIT from
  D-BT12 thresholds), `decision` is the state-machine output.
- **Backward compat:** `decision` field name unchanged. Any external consumer
  of the report continues to read `decision`; they just get fewer EXITs and
  a stickier HOLD.

## 2026-04-23 — Second-pass fixes after low avg_exposure backtest

First-pass fix run (D-BT12..18) cut churn (1337 → 49 EXITs) but still drained
the book to `avg_exposure ≈ 5%` by mid-2022 and stayed in cash for 3.5y.
Diagnosis: EXIT was terminal (D-BT14) and the floor was off outside UPTREND
(D-BT15), so the 2021-22 correction cascade was unprotected and the 2023-25
rally couldn't be re-joined. Two targeted changes:

### D-BT19. Controlled re-entry from EXIT (supersedes D-BT14 terminality)
- **Decision:** EXIT is no longer terminal. From `prev_state == EXIT`, the
  state machine transitions to `REDUCE` iff **all** of:
  - `price > 200DMA`
  - `price > 50DMA`
  - `relative_strength > 0`
- **Sizing:** Re-entry buys 50% of the stock's *initial* rupee allocation
  (`initial_capital / N_holdings`), not 50% of its peak equity. Rationale:
  the initial allocation is deterministic and independent of path.
- **Broker state after re-entry:** `REDUCED` (not `FULL`). A subsequent
  REDUCE → HOLD upgrade re-arms to `FULL` normally (D-BT14 upgrade rule is
  unchanged).
- **No re-entry for genuinely new positions** — this applies only to symbols
  previously held and EXITed in the same run.
- **Cash-clipped:** if available cash < target re-entry rupees, buy what cash
  allows; no credit. If cash == 0 the re-entry is effectively skipped and the
  state machine does NOT advance (same semantics as floor-blocked trades),
  so it will be re-attempted on the next qualifying day.
- **Live analyzer:** emits `decision=REDUCE, prev_state=EXIT` when conditions
  are met. The live pipeline does not execute buys; it surfaces the signal
  for the user to size manually.

### D-BT20. Extended exposure floor (supersedes D-BT15 regime gate)
- **Decision:** Floor now active whenever `market_trend != DOWNTREND`
  (i.e. UPTREND *and* SIDEWAYS). Disabled only in DOWNTREND.
- **Rationale:** Most of the 2021-22 drain happened on SIDEWAYS days; the
  uptrend-only floor let them through. Keeping it off in DOWNTREND preserves
  the spec's "allow capital preservation in the one regime that warrants it".
- **Constant renamed:** `EXPOSURE_FLOOR_UPTREND` → `EXPOSURE_FLOOR` in
  `config.py`. Value unchanged (0.50).

### D-BT21. Ranked re-arm: active capital deployment under constraint
- **Problem observed after D-BT19/20:** `avg_exposure` climbed from 5% to 39%
  but still sat below the 50% floor in 2025 (0.26). The floor can only *block*
  over-reduction; it cannot *raise* exposure when many names are already in
  REDUCED/EXITED state and cash is piling up.
- **Decision:** On each trading day, after the state-machine decision loop,
  if **market_trend != DOWNTREND AND exposure < EXPOSURE_FLOOR**, actively
  deploy cash by upgrading the highest-ranked REDUCED positions back to FULL.
- **Candidate filter:** `state == REDUCED AND shares > 0`. EXITED positions
  are excluded — re-entry (D-BT19) handles them with its own stricter gate.
- **Ranking metric:** Relative strength (`return_50d - market_return_50d`)
  at T-close. Higher = better. NaN ranks are skipped.
- **Sizing per upgrade:** Buy additional shares equal to current share count
  (restoring the share count sold by the prior REDUCE), capped by:
  - available cash
  - `REARM_MAX_WEIGHT_PER_STOCK * equity` — current position value
    (prevents over-concentration; default 0.10)
- **Loop termination:** Stop when exposure >= EXPOSURE_FLOOR, cash is
  exhausted, or no more ranked candidates remain.
- **Execution timing:** T+1 open, same as decision fills. Fill reason
  `REARM`. State flips REDUCED → FULL.
- **Diagnostics added:** `rearm_count`, `rearm_avg_forward_return_21d` in the
  backtest report.
- **Not applied to the live analyzer:** the live pipeline only surfaces
  decisions; it does not sequence intraday capital-deployment actions. The
  ranked re-arm is a simulator-only execution rule.

### D-BT22. Opportunistic refill: fresh entries to close the exposure gap
- **Problem observed after D-BT21:** `avg_exposure` rose from 0.39 to 0.45 but
  still trailed the 0.50 floor, because ranked re-arm can only restore names
  already in REDUCED state. In late-cycle windows (2025/2026) many NIFTY50
  names are EXITED and the REDUCED candidate pool is exhausted, leaving cash
  idle even when breadth is acceptable.
- **Decision:** On each trading day, after ranked re-arm, if
  **market_trend != DOWNTREND AND exposure < REFILL_STOP_EXPOSURE**, open
  fresh entries into NIFTY50 names that are currently not held.
- **Candidate filter:** `price > 50DMA AND price > 200DMA AND RS > 0` at
  T-close, and the symbol is either unknown to the portfolio or its position
  is EXITED / zero-shares. REDUCED/FULL names are excluded (D-BT21 handles
  them).
- **Ranking metric:** Relative strength (`return_50d - market_return_50d`).
- **Sizing per entry:** `REFILL_ALLOCATION_FRACTION * equity` (default 0.05),
  clipped by available cash.
- **Loop termination:** Stop when exposure >= `REFILL_STOP_EXPOSURE`
  (default 0.55), cash is exhausted, or no more qualifying candidates remain.
- **Execution timing:** T+1 open, same as other simulator fills. Fill reason
  `REFILL`. State initialises to FULL (fresh entries, no hysteresis history).
- **Guardrails:**
  - `stop_exposure > EXPOSURE_FLOOR` to avoid oscillation against D-BT21.
  - RS > 0 prevents buying into laggards.
  - 50DMA + 200DMA gate mirrors D-BT19 re-entry, ensuring only
    structurally intact names are picked.
- **Diagnostics added:** `num_refills`, `total_rupees_deployed`,
  `refill_avg_forward_return_21d` in the backtest report; `refills.csv`
  artifact with (date, symbol, rupees) rows.
- **Not applied to the live analyzer:** same rationale as D-BT21 — the live
  pipeline surfaces decisions only; refill is a simulator-only deployment
  rule.

### D-BT23. Refill universe expansion: NIFTY 500 with external exposure cap
- **Problem observed after D-BT22:** `avg_exposure` climbed to 0.454 but per-year
  still sagged in 2025/2026 (0.38) because the NIFTY 50 refill pool is nearly
  fully INIT'd on day 0; only index additions (ETERNAL, JIOFIN) ever qualified
  as "not currently held". Only 4 refills fired across 5y.
- **Decision:** Expand the refill candidate pool from NIFTY 50 to the full
  NIFTY 500 breadth universe. Core portfolio (INIT) remains NIFTY 50.
- **Candidate filter (unchanged from D-BT22):** `price > 50DMA AND price > 200DMA
  AND drawdown >= -15% AND RS > 0 AND not-currently-held`. Applied to all 500
  names — `compute_refill_eligibility` is now invoked on `universe_close`.
- **Ranking:** Relative strength (`return_50d - market_return_50d`), computed
  for the full universe.
- **Top-K cap:** After ranking, truncate the candidate list to the top
  `REFILL_TOP_K` names (default 15) to avoid spraying 5% positions across
  hundreds of marginal symbols in a single day.
- **External exposure cap:** For symbols not in the NIFTY 50 core set, the
  aggregate market value of those positions may not exceed
  `REFILL_EXTERNAL_EXPOSURE_CAP * equity` (default 0.35). Once the cap is
  reached, additional non-core candidates are skipped for the day; core
  NIFTY 50 names previously EXITed are unaffected by the cap.
- **Sizing per entry (unchanged):** `REFILL_ALLOCATION_FRACTION * equity`
  (0.05), cash-clipped. Cap-clipped for non-core entries.
- **Decision state machine over the broader pool:** `compute_decisions` is
  now run on `universe_close` so that once a non-core name is refilled, the
  ordinary `REDUCE/EXIT/REENTRY` transitions fire for it identically to a
  core name — no special exit logic for refill-originated positions.
- **Performance (5y backtest, v7 -> v8):**
  - CAGR 8.48% -> 16.99% (benchmark 14.06%)
  - Sharpe 1.16 -> 1.56
  - Max drawdown -13.08% -> -10.80%
  - `avg_exposure` 0.454 -> 0.489
  - `num_refills` 4 -> 252, deployed ~Rs.6.29M
  - `refill_avg_forward_return_21d` -0.35% -> +2.99%
- **Not applied to the live analyzer:** same rationale as D-BT21/22; refill is
  a simulator-side capital-deployment rule.

### D-BT24. REENTRY gate hygiene: add drawdown >= -15% to `_reentry_qualifies`
- **Problem observed:** D-BT19's re-entry gate (price > 200DMA AND > 50DMA AND
  RS > 0) did not require `drawdown >= EXIT_GATE_DRAWDOWN`, so a name meeting
  price/RS conditions but still in deep drawdown could trigger REENTRY and
  immediately re-satisfy the hard EXIT gate on the next bar. This produced
  costless but noisy REENTRY-then-EXIT fill cycles in the 2023/2025 backtest.
- **Decision:** `strategy._reentry_qualifies` additionally requires
  `drawdown_from_high >= cfg.EXIT_GATE_DRAWDOWN` (i.e., >= -15%).
- **Parity:** Tightens both the live analyzer and backtest symmetrically.
  Pure hygiene — not a capital-deployment lever.
- **Alignment:** Matches `compute_refill_eligibility`'s own drawdown check,
  so REENTRY and REFILL now share the same structural-integrity gate.


### D-BT25. EXIT deferral: 2-day timer with acute-breakdown bypass
- **Problem observed:** v9 per-year decomposition showed whipsaws in V-shape
  recovery windows (notably 2025: -8.45 pp alpha). Names that briefly dipped
  below 200DMA or -15% drawdown were EXITed at the next open, then re-entered
  via the D-BT19 re-entry gate a few sessions later, paying slippage + costs
  both ways and missing the bounce. Sub-period analysis showed most of the
  exits fired within 2-3 sessions of a short-lived breakdown.
- **Decision:** when the decision matrix emits EXIT, the simulator enqueues a
  pending exit with `cfg.EXIT_DEFER_DAYS = 2` business days rather than
  executing immediately. Each subsequent day, at the t+1 open:
  - If `price < ma200 * cfg.EXIT_DEFER_DMA_THRESHOLD` (default 0.95 -> >5%
    below 200DMA), the exit is treated as an acute breakdown and fires
    immediately with reason `EXIT_ACUTE`.
  - Otherwise the timer decrements; on expiry the exit fires with reason
    `EXIT_DEFERRED`.
  - If the matrix emits any non-EXIT signal during the window (typically
    EXIT -> REDUCE via the re-entry gate), the deferral is cancelled and the
    position stays in its pre-exit state (no reduce_half is fired on the
    cancellation day).
- **Floor interaction:** on timer expiry or acute breakdown, `_would_breach_
  floor` is checked the same way as the immediate-EXIT path; floor-blocked
  deferrals are logged with `cancel_floor` and the pending entry is dropped.
- **Diagnostics:** `SimResult.defer_history` records every enqueue, decrement,
  fire_acute, fire_expired, cancel_upgrade, cancel_floor event. The report
  surfaces aggregate counters under `exit_deferrals`. A `<stem>_defers.csv`
  companion artifact is written.
- **Backtest results (2021-04 to 2026-04 vs v9 baseline, NIFTY 500 refill):**
  CAGR 15.52% -> 17.55% (+2.03 pp); alpha 1.46 pp -> 3.55 pp (2.4x); Sharpe
  1.47 -> 1.55; fills 2385 -> 2037 (-15%); executed exits 4102 -> 3453 (-16%).
  Per-year: 2023 alpha +4.89 -> +11.61 (+6.72 pp), 2024 +8.78 -> +11.47
  (+2.69), 2025 -8.45 -> -5.81 (+2.64). MaxDD worsened from -11.4% to -17.7%,
  an accepted tradeoff for sitting through short-lived breakdowns. Sideways
  2022 regressed (+2.69 -> -0.73) since deferred names rode small pullbacks
  down rather than whipsawing back.
- **Not yet applied to the live analyzer.** The live pipeline is stateless
  across days apart from the previous JSON (D-BT17); implementing parity
  requires persisting `pending_exits` in the daily report and loading it on
  the next run. Deferred as a follow-up because (a) the live analyzer calls
  `strategy.decide` once per day and does not execute trades, and (b) the
  primary value of this rule is for backtested capital-deployment analysis.
  When parity is added, the live path should read yesterday's pending map,
  evaluate acute / timer today, and emit the same EXIT_ACUTE / EXIT_DEFERRED
  decisions to the caller.
- **Tests:** `tests/test_backtest_simulator.py` adds 4 tests under the
  `with_defer` pytest mark: timer-expiry fill date, acute bypass, upgrade
  cancellation, and DEFER_DAYS=1 boundary. A matching `_no_defer` autouse
  fixture mirrors `_zero_costs`: legacy tests keep immediate-EXIT semantics.


### D-BT26. Refined conditional EXIT-defer: triple-gate + gap-down bypass
- **Problem observed:** the D-BT25 acute rule (`price < 200DMA * 0.95`) was
  crude on two fronts. (a) It treated every sub-5%-of-200DMA print as "defer",
  leaving strong names with deep drawdowns waiting while the slide continued.
  (b) It had no mechanism for overnight gaps, so a -4% gap-down next to a
  still-above-ma200 close still got deferred. The sideways 2022 regression
  (-3.4 pp alpha vs v9) was the clearest symptom: benign pullbacks that
  stayed above ma200 rode down for 2 days before firing.
- **Decision:** replace the single DMA-threshold with a conditional rule
  gated on structural strength, drawdown severity, and intraday gap behavior.
  An EXIT fires immediately (no defer) iff either:
  1. **Gap-down bypass** — `(prev_close - open) / prev_close >
     cfg.EXIT_DEFER_GAP_DOWN_PCT` (default 3%). Liquidity events and
     earnings-gap breakdowns skip the timer regardless of structure.
  2. **Strong breakdown (triple-gate)** — `price < 200DMA` AND
     `drawdown < cfg.EXIT_DEFER_DD_THRESHOLD` (default -10%) AND `rs < 0`.
     All three legs must be true: sustained structural failure + meaningful
     drawdown + underperformance vs NIFTY 500. Any single-leg failure
     falls through to the 2-day defer timer.
- **Intent by case:**
  - Case 1 (all three legs true) — cut losses quickly, don't hope.
  - Case 2 (below ma200 but mild dd) — give the name 2 days to reclaim.
  - Case 3 (rs > 0) — strong relative winner; ride out the noise.
  - Plus an unconditional gap-down guardrail for acute events.
- **Implementation:** `simulator._is_acute_breakdown(px, prev_close, ma200,
  dd, rs, dd_threshold, gap_pct)` is the single predicate. NaN on any leg
  fails that leg (protective default: missing history defers rather than
  fires). Applied at both the enqueue moment (EXIT first emitted) and each
  subsequent pending-exit resolution day so a gap-down mid-defer can pull
  the trigger early.
- **Config changes:** removed `EXIT_DEFER_DMA_THRESHOLD`. Added
  `EXIT_DEFER_DD_THRESHOLD = -0.10` and `EXIT_DEFER_GAP_DOWN_PCT = 0.03`.
- **Data wiring:** drawdown frame now computed inside the simulator from
  `close_df.rolling(HIGH_52W_WINDOW).max()` alongside the existing ma200
  frame. RS is read from the same `rank_df` already used by D-BT21 ranked
  re-arm.
- **Backtest results (2021-04 to 2026-04 vs v10 baseline):**

  | Metric         | v9      | v10     | v11     | Δ (v11-v10) |
  |----------------|---------|---------|---------|-------------|
  | CAGR           | 15.52%  | 17.55%  | 18.16%  | +0.61 pp    |
  | Sharpe         | 1.47    | 1.55    | 1.56    | +0.01       |
  | MaxDD          | -11.44% | -17.73% | -13.57% | **+4.16 pp**|
  | Alpha (CAGR)   | +1.46   | +3.55   | +4.16   | +0.61 pp    |
  | Fills          | 2,385   | 2,037   | 2,120   | +4%         |
  | fire_acute     | —       | 287     | 967     | +237%       |

  Per-year alpha (percentage points): 2021 +4.16 -> -6.59 (regression);
  2022 -0.73 -> **+5.50** (sideways year fix, +6.23 pp); 2023 +11.61 ->
  +15.58; 2024 +11.47 -> +16.97; 2025 -5.81 -> -6.75 (vs -8.45 in v9);
  2026 YTD -0.33 -> -1.05.
- **Tradeoffs:** the gap-down bypass and tighter structural gate materially
  reduce MaxDD (-17.7% -> -13.6%) and reclaim the 2022 sideways alpha at
  the cost of 2021 drawdown-era alpha (more aggressive acute firing caught
  the May 2021 correction earlier but missed the snap-back). Overall
  5-year alpha is still higher and the drawdown profile is closer to v9,
  which is the better risk-adjusted outcome.
- **Tests:** `tests/test_backtest_simulator.py` adds 3 tests under
  `with_defer`: gap-down bypass, triple-gate immediate fire, RS>0 forces
  defer despite breakdown, and mild-dd defer. The old single-threshold
  acute test is replaced by these richer scenarios.
- **Live parity:** implemented in D-BT28.


### D-BT28. Live parity for the deferred-exit queue
- **Problem:** the live analyzer was stateless across days apart from
  yesterday's decision (D-BT17). It never deferred EXIT signals and never
  emitted acute-bypass fires, so live output diverged from the v11 backtest
  exactly when risk management matters most (mild breakdown chop vs sharp
  drawdowns). Users running `python -m portfolio_analyzer run` got signals
  closer to pre-D-BT25 behaviour.
- **Decision:** port the D-BT25/D-BT26 defer mechanics into the live path
  with full parity on the acute-breakdown predicate.
- **Shared helper:** `strategy.is_acute_breakdown(px, prev_close, ma200, dd,
  rs, ...)` now owns the triple-gate + gap-down logic. The simulator imports
  it as `_is_acute_breakdown` so the predicate cannot drift between live and
  backtest.
- **Resolver:** `strategy.resolve_with_defer(prev_state, metrics, raw_signal,
  prev_close, pending_days=None)` wraps `decide()` and returns a
  `DeferResolution(decision, prev_state, pending_days_remaining, event,
  reason)`. Event names match the simulator log: `enqueue`, `decrement`,
  `fire_acute`, `fire_expired`, `cancel_upgrade`. When enqueued or
  decrementing, `decision` is frozen to `prev_state` (the sell hasn't fired
  yet) and `pending_days_remaining` is the carry-forward counter.
- **Persistence:** `ReportOut.pending_exits: list[PendingExitOut]` now
  round-trips in the daily JSON. `cli._load_previous_states` returns
  `(prev_decisions, pending_exits)`; on each run, pending entries are
  resolved first (fire/decrement/cancel) and any new mild EXIT is enqueued
  with `days_remaining = EXIT_DEFER_DAYS` and `enqueued_date = today`.
- **Gap-down in live:** the simulator uses `px = T+1 open` and
  `prev_close = T close`. The live analyzer has close-only data, so it uses
  `px = metrics.price` (today's close) and `prev_close = series.iloc[-2]`
  (yesterday's close). This is a close-to-close proxy for the backtest's
  open-gap check; it fires on single-day drops > 3% regardless of
  intraday behaviour. Accepted tradeoff: fetching OHLC per holding for a
  true open-gap measure was out of scope for this change.
- **User-facing surfaces:**
  - Per-stock `decision` stays `HOLD | REDUCE | EXIT` (no new action).
  - Stocks in the defer queue show their current (pre-EXIT) decision plus
    a `[defer] enqueue|decrement: …` line appended to `reasons`.
  - The top-level `pending_exits` list surfaces every symbol still being
    watched, with the countdown timer.
- **Tests:** `tests/test_strategy.py` adds 10 new cases covering the acute
  predicate (gap-down, triple-gate, NaN legs), the resolver (enqueue,
  fire-on-enqueue, decrement, expiry, mid-defer acute, upgrade cancel,
  `defer_days=0` bypass, and EXIT-from-EXIT). `tests/test_prev_state.py`
  adds a pending-exits round-trip case and is updated for the new
  `(states, pending)` return shape. 158 tests pass (was 147).


## 2026-04-23 — Phase 1 scanner bootstrap (NSE universe + OHLCV ingestion)

### D-S1. Phase 1 lives inside `portfolio-analyzer`
- **Decision:** Phase 1 (VCP scanner data pipeline) ships as a new subpackage
  `src/portfolio_analyzer/scanner/` rather than a separate project.
- **Rationale:** Reuses Phase 0 config, logging, CLI group, test scaffolding.
  Phase 0 contract is unaffected; no cross-imports from Phase 0 into scanner
  code (isolation works the other direction only).

### D-S2. Storage engine — SQLite
- **Decision:** Use SQLite (single file at `data/scanner.db`) for the stock
  universe, OHLCV history, and ingestion log. WAL mode, FK enforcement on.
- **Alternatives considered:** parquet-per-ticker (like `data/backtest_cache`);
  duckdb; Postgres. Rejected because (a) ~2.6k stocks × 10y daily ≈ ~6.5M rows
  fits comfortably in SQLite, (b) we need upserts on `(isin, trade_date)` which
  parquet does not do natively, (c) zero external service dependency.
- **Gitignored:** `data/scanner.db` and its `-wal` / `-shm` / `-journal` siblings.

### D-S3. NSE bhavcopy source — UDiFF Common Bhavcopy Final
- **Decision:** Fetch from
  `https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip`.
- **Context:** The legacy `cm{DD}{MMM}{YYYY}bhav.csv.zip` format was
  **discontinued on 2024-07-08** per NSE circular 62424. UDiFF is now the
  authoritative daily bhavcopy across NSE, BSE, and all SEBI-regulated MIIs.
- **Auth:** No cookie handshake required on the `nsearchives.nseindia.com` host
  — a standard desktop UA (reused `REFRESH_USER_AGENT`) is sufficient.
- **Non-trading days:** return HTTP 404. Treated as `no_data` outcome, not
  `error`, so date-range ingestion does not halt on weekends/holidays.

### D-S4. Primary key — ISIN
- **Decision:** `stock_master.isin` is the PK; `market_data` keys on
  `(isin, trade_date)`. Symbol is a secondary lookup via an index.
- **Rationale:** Symbols rotate (rename, dual-listing, corporate actions);
  ISIN is stable per security and shared across NSE/BSE, which lets us join
  BSE bhavcopies in later phases without building a remap table.
- **Rows without ISIN are dropped** at parse time (observed <1% of UDiFF rows).

### D-S5. Equity filter — `FinInstrmTp=STK AND SctySrs IN {EQ, BE}`
- **Decision:** Parser keeps only instrument type `STK` with series `EQ` or
  `BE`. Everything else (derivatives, SME, government bonds, treasury bills,
  closed-end funds, rights) is discarded.
- **Sample counts on 2026-04-22 UDiFF:** 2,501 EQ + 153 BE = **2,654 rows**
  — matches the Phase 1 "full NSE equity universe" scope.
- **Revisit:** SME (`SctySrs=SM`, ~370 rows/day) and derivatives will be
  added as separate series sets in later phases.

### D-S6. Idempotent ingestion + ingestion log
- **Decision:** `ingest_date(trade_date)` is idempotent — it checks
  `ingestion_log` for `trade_date` first and skips unless `--force`. Writes
  are wrapped in a single transaction per date. Re-ingestion updates the
  existing market_data row rather than inserting a duplicate.
- **Range semantics:** `ingest_range(start, end)` iterates weekday-by-weekday
  (NSE is closed Sat/Sun); holidays are surfaced as per-date `no_data` in the
  summary rather than raised as errors.

### D-S7. Scanner CLI lives under the existing `portfolio-analyzer` command
- **Decision:** New click subgroup `scanner` with three commands:
  - `scanner ingest --date YYYY-MM-DD [--force] [--db PATH]`
  - `scanner ingest-range --start --end [--force] [--db PATH]`
  - `scanner status [--db PATH]`
- **Output:** JSON on stdout (matches Phase 0 CLI convention); exit code 1
  only on per-date `error` status (network / parse failure).


## 2026-04-23 — Phase 1 corporate-actions normalization

### D-S8. Corporate-actions source — NSE JSON API
- **Decision:** Fetch from
  `https://www.nseindia.com/api/corporates-corporateActions?index=equities&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY`.
- **Rationale:** Single call covers multi-year windows (10y ≈ 22.5k records,
  ~7 MB JSON). Returns **ISIN** directly, so rows bind to `stock_master`
  without a symbol remap. Needs a desktop UA + `Referer` header; any archives
  CSV equivalent (`/content/equities/corporate_actions.csv`) 404s and was
  abandoned.
- **Idempotency:** `corporate_actions` PK is
  `(isin, ex_date, action_type, raw_subject)`; re-fetching the same window
  upserts without duplication.

### D-S9. Action taxonomy + subject parser
- **Decision:** Classify the freeform `subject` field into
  `BONUS | SPLIT | DIVIDEND | RIGHTS | BUYBACK | MERGER | CONSOLIDATION | INTEREST | OTHER`.
  Only **BONUS** and **SPLIT** carry a non-1.0 `price_factor`.
- **Ratio extraction:**
  - `BONUS X:Y` → `price_factor = Y/(X+Y)` (e.g. 1:1 → 0.5; 4:1 → 0.2)
  - `SPLIT From Rs A To Rs B` → `price_factor = B/A` (accepts `Re 1` variant)
- **Compound subjects:** strings like `Bonus 4:1/Face Value Split ...` emit
  two rows — one BONUS, one SPLIT — with distinct `action_type`s under the
  same `raw_subject`. The compound PK prevents collisions.
- **Explicit skips:** `Bonus Ncrps 1:116` (preference-share bonus) falls
  through to `OTHER`; rights theoretical-ex-rights (TERP) math is deferred;
  dividends are stored as metadata only (this slice is not a total-return
  series).

### D-S10. Raw-first storage with a materialized adjustment layer
- **Decision:** `market_data` stores raw exchange OHLCV and never changes.
  Adjustments live in three places:
  1. `corporate_actions` — event log keyed on ISIN + ex_date + action_type.
  2. `cumulative_adjustments(isin, trade_date, factor)` — materialized helper
     holding only rows where `factor != 1.0`. Rebuilt in full from
     `corporate_actions` + `market_data` on every CA ingest.
  3. `adjusted_market_data` — read-only SQL view joining the above via
     `COALESCE(factor, 1.0)`, exposing `adj_open/high/low/close/volume`.
- **Adjustment semantics:** `factor(t) = Π price_factor(CA)` over all
  price-adjusting CAs with `ex_date > t` (strictly greater). A bar **on**
  `ex_date` is already ex-action and gets factor 1.0. Volume is divided by
  the same factor.
- **Rationale:** Keeps the audit trail (raw prices are exactly what NSE
  published), makes adjustments reversible, and avoids having to mutate every
  `market_data` row whenever a new CA appears.

### D-S11. Scanner CLI grows CA commands
- **Decision:** Two new subcommands plus a status extension:
  - `scanner ca-ingest --start --end [--no-rebuild] [--db PATH]`
  - `scanner ca-rebuild-adjustments [--db PATH]`
  - `scanner status` now includes a `corporate_actions` block
    (`total`, `by_type`, `earliest_ex_date`, `latest_ex_date`, `adjusted_bars`).
- **Out of scope for this slice:** dividend-adjusted total-return series,
  TERP for rights, merger/demerger handling, and BSE corporate actions
  (will layer in alongside BSE bhavcopy ingestion).


## 2026-04-23 — Phase 1 fundamentals ingestion (Screener.in)

### D-S12. Fundamentals source — Screener.in HTML
- **Decision:** Scrape the public `screener.in/company/{SYMBOL}/{variant}/`
  pages, preferring `consolidated` and falling back to `standalone` on 404.
- **Rationale:** Screener publishes 10y+ annual P&L, balance-sheet highlights
  and ratios in a single page, plus a `top-ratios` block with live
  market-cap / PE / ROE / ROCE / 52w band. `robots.txt` only disallows
  `?q=`/`?page=` query patterns, so company pages are fair game. Tickertape
  and Yahoo are slated as fallbacks for symbols Screener doesn't cover.
- **Scraping etiquette:** desktop `REFRESH_USER_AGENT`, 2s throttle
  (`SCREENER_REQUEST_DELAY_SEC`), exponential backoff on 429 / 5xx, max
  `SCREENER_MAX_RETRIES` per variant.

### D-S13. Fundamentals schema — ISIN-keyed, source-tagged
- **Decision:** Four new tables, all keyed on `isin` + `source` (+ `fiscal_year`
  + `report_type` where annual):
  - `fundamentals_meta(isin, source, sector, industry, market_cap_cr, ...)`
  - `financials_annual(isin, fiscal_year, source, report_type, sales_cr, ...)`
  - `ratios_annual(isin, fiscal_year, source, report_type, roce_pct, ...)`
  - `fundamentals_ingestion_log(isin, source, status, detail, report_type, fetched_at)`
- **Units:** money columns in **rupees crore**; percentages stored as
  decimals (28% → 0.28) — the parser normalizes at read time via
  `_clean_number` (handles Indian commas, ₹, `Cr.`/`L`/`K` suffixes,
  parentheses for negatives, `+` expand markers, em-dash blanks).
- **Report variant:** the `report_type` column (`consolidated` | `standalone`)
  is part of the PK so a company can carry both variants without collisions.

### D-S14. Orchestrator — freshness-cached, forgiving ingest
- **Decision:** `scanner/fundamentals/ingest.py` iterates `stock_master`,
  calls the Screener fetcher + parser per symbol, upserts into the three
  tables, and writes one row per run into `fundamentals_ingestion_log` with
  status `ok` / `not_found` / `error`.
- **Freshness:** re-runs skip any ISIN whose most recent `ok` fetch is newer
  than `SCREENER_REFRESH_AFTER_DAYS` (default 7). `--force` overrides.
- **Failure isolation:** network, 404 and parser exceptions are captured per
  symbol; the loop commits every 25 symbols so a long run can be interrupted
  without losing progress.

### D-S15. Scanner CLI grows a fundamentals command
- **Decision:** `scanner fundamentals-ingest [--symbol S]... [--limit N]
  [--refresh-days D] [--force] [--db PATH]`. JSON summary mirrors the
  other scanner commands (`processed`, `ok`, `not_found`, `error`,
  `skipped`). `scanner status` now includes a `fundamentals` block
  (`companies_covered`, `annual_rows`, `ratios_rows`, `by_status`,
  `last_fetch`).
- **Out of scope for this slice:** Tickertape / Yahoo fallbacks,
  promoter-holding / shareholding-pattern block, quarterly P&L,
  cash-flow statements, peer comparisons.


## 2026-04-24 — Phase 1 VCP scanner engine

### D-S16. VCP feature engine — adjusted OHLCV, numpy-only
- **Decision:** `scanner/vcp/features.py` reads `adjusted_market_data` for
  the last 320 bars per ISIN (enough for EMA200 + 1y return + buffer) and
  returns a typed `TechnicalFeatures` row. Features: EMA50/200 with seeded
  SMA, Wilder ATR14/50, a separate ATR5-recent vs ATR30-trailing ratio for
  compression, 1y/3m returns, 52w band + distance, avg-volume/turnover
  over 20d/50d, 20-bar normalized range, 10-bar close pivot with distance,
  20-bar log-volume linear-regression slope, and the last three 5-bar
  fractal swing highs / lows.
- **Rationale:** All indicators run off adjusted prices so splits/bonuses
  never trigger false breakdowns. Minimum-history gate of 252 bars keeps
  EMA200 and 1y metrics honest.

### D-S17. Four-stage scoring pipeline
- **Decision:** `scanner/vcp/scorer.py` composes the decision via:
  1. **Stage-1 hard filters** (liquidity + trend + strength + near-highs):
     turnover ≥ 0.5 Cr, market cap ≥ 100 Cr, `close > EMA50 > EMA200`,
     EMA50 20d-slope > 0, 1y return ≥ 20%, within 25% of 52w high.
  2. **Stage-2 fundamentals**: hard-reject ROE < 10% or D/E > 2.0;
     soft `fundamental_score = 0.35·growth + 0.25·ROE + 0.20·ROCE + 0.20·D/E`.
  3. **Stage-3 VCP sub-scores** (`vcp_score ∈ [0,1]`, weighted):
     contraction 0.25, volatility 0.20, volume 0.15, structure 0.15,
     pivot 0.15, range 0.10.
  4. **Readiness + final blend**:
     `readiness = clamp(1 − |dist_to_pivot|/0.05)`;
     `combined = 0.7·(0.5·tech + 0.5·vcp) + 0.3·fund`;
     `final = combined · (0.5 + 0.5·readiness)`.
- **Decision ladder:** `BUY_ALERT/READY` if `final ≥ 0.75` **and** within 2%
  of pivot; `WATCHLIST/BUILDING` if `final ≥ 0.55`; `WATCHLIST/CONTRACTING`
  if `vcp ≥ 0.40`; else `REJECT/STAGE3_FAIL`. Stage-1/2 hard-fails short-
  circuit to `REJECT/STAGE1_FAIL | STAGE2_FAIL` and carry the failed-check
  list in `reasons`.

### D-S18. `vcp_candidates` — lean persistence, full audit on demand
- **Decision:** Results land in `vcp_candidates(isin, trade_date)` with
  per-row `symbol`, `close`, `pivot`, `distance_to_pivot`, all sub-scores,
  `decision`, `stage`, and a semicolon-joined `reasons` trace. Upsert keyed
  on `(isin, trade_date)` so a re-run on the same date is idempotent.
- **Default storage is lean:** only `WATCHLIST` and `BUY_ALERT` rows persist.
  `--store-rejects` keeps the full audit (useful for calibration / backtest
  research). Two supporting indexes: `(trade_date, final_score DESC)` for
  ranked reads, `(decision)` for filter reads.

### D-S19. Scanner CLI grows a `vcp-scan` command
- **Decision:** `scanner vcp-scan [--date YYYY-MM-DD] [--symbol S]...
  [--limit N] [--store-rejects] [--db PATH]`. If `--date` is omitted the
  scanner reads `MAX(trade_date)` from `market_data`. JSON summary mirrors
  the other scanner commands (`universe`, `scored`, `skipped_history`,
  `by_decision`, `stored`). `scanner status` now carries a `vcp` block
  (`total`, `by_decision`, `latest_scan_date`, `latest_scan_rows`).
- **Performance:** full-universe scan (~3k ISINs × 320 adjusted bars each)
  completes in ~10–12s single-threaded; suitable for end-of-day cron.
- **Out of scope for this slice:** BUY_ALERT confirmation window
  (multi-bar breakout check), forward-test harness, win-rate calibration,
  per-sector score normalization, and per-symbol explain CLI.



## 2026-04-24 — Phase 1 scanner dashboard slice

### D-S20. NIFTY 500 index ingestion + RS score in vcp_candidates
- **Decision:** Reuse the Phase 0 RS formula (`rs = stock_ret50 -
  bench_ret50`, `RETURN_WINDOW = 50`) inside the scanner by ingesting
  benchmark closes into a new `index_data(index_symbol, trade_date, close)`
  table and reading them during `vcp-scan`. Benchmark defaults to
  `NIFTY500` (yfinance `^CRSLDX`); the same table accepts `NIFTY50`
  (`^NSEI`) for later RS-matrix work.
- **Rationale:** Keeps the scanner offline-consistent (no yfinance calls
  inside the hot scan loop), uses the same numerator/denominator as the
  Phase 0 live analyzer so RS values compare one-to-one across systems,
  and lives entirely on adjusted series so splits/bonuses never distort
  the 50-bar return.
- `vcp_candidates` gains `return_50d`, `benchmark_return_50d`, `rs_score`.
  `scan_date` now returns `benchmark_index` + `benchmark_return_50d` in
  its `ScanResult`. If the index hasn't been ingested, all three fields
  are `NULL` and the scan otherwise behaves identically.
- **Schema migration strategy:** `init_schema` reads `PRAGMA table_info`
  and adds only the missing columns (SQLite has no `IF NOT EXISTS` for
  `ADD COLUMN`). The same mechanism is reusable for future column
  additions via `_COLUMN_MIGRATIONS`.
- **CLI:** `scanner index-ingest [--index NIFTY500|NIFTY50] [--days N]
  [--db PATH]`. Defaults: `NIFTY500`, 500 calendar days (well over 1y).
  `scanner status` gains an `indices` block with per-index bars + date
  range.
- **Out of scope for this slice:** per-symbol ingest of stock closes
  from yfinance (all stock series still come from the NSE bhavcopy
  pipeline), per-sector RS, rolling RS percentile ranks.

### D-S21. Scanner dashboard — Textual, DB-backed, single-screen
- **Decision:** New app at `tui/scanner_dash.py` launched via
  `scanner dash`. Reads `vcp_candidates` joined with `fundamentals_meta`
  for the latest (or `--date`) scan and renders three regions:
  summary header, sortable candidates table, detail pane for the
  cursored row.
- **Framework:** Reuses the existing Textual + textual-plotext stack
  (already a dependency for the backtest TUI). No new runtime deps.
- **Columns (20):** Symbol, Decision, Stage, Close, Pivot, Dist%, Final,
  VCP, Tech, Fund, Ready, RS50, Ret50, Bench50, ROE, ROCE, PE, MCap,
  Sector. Dist%/RS50/Ret50/Bench50 are rendered signed.
- **Default filter:** `WATCHLIST` + `BUY_ALERT` only (matches the lean
  persistence of `vcp-scan`). Press `r` to toggle including `REJECT`
  rows if they're in the DB (requires `vcp-scan --store-rejects`).
- **Key bindings:** `q` quit, `s` sort by the currently-cursored column
  (uses `DataTable.sort`), `r` toggle include-rejects and reload.
- **Selection model:** Row cursor movement (`up`/`down`) live-updates the
  detail Static via `.update(detail_markup(...))` — no widget removal /
  remount, which avoided a `DuplicateIds` class of bugs seen in the
  first iteration.
- **Out of scope for this slice:** per-row PlotextPlot of recent OHLC,
  sector-grouped views, in-dashboard rescan button, export-to-CSV
  action, filter-by-sector/decision widgets.



## 2026-04-24 — VCP scoring rewrite

### D-S22. VCP scorer rewrite: strict rules, VCP-led blend, gated RS reward
- **Decision:** Replace the original scorer (D-S17) with a VCP-first
  formulation whose final decision is always gated on `vcp_score ≥ 0.40`.
  Scope covers Stage-3 sub-scores, weights, the combined blend, readiness,
  RS integration, and the decision ladder. Stage-1 and Stage-2 hard filters
  are unchanged.
- **Rationale:** Initial live output showed stocks with near-zero VCP
  patterns (`vcp ≤ 0.11`) passing `WATCHLIST` on the strength of
  `technical_score` + `fundamental_score` + positive `rs_score` alone.
  A scanner named "VCP" must not short-list non-VCP setups. The rewrite
  restores VCP as the primary alpha source and demotes tech / fund / RS
  to confirmation, filter, and reward respectively.
- **Feature additions (`TechnicalFeatures`):**
  - `return_20d` — 20-trading-day return, used to gate "dead stock"
    volatility false-positives.
  - `pivot_touches` — count of last-10 closes within ±2% of the 10-bar
    pivot; strong pivots are retested, weak ones aren't.
  - `close_std_5_norm` — std of last-5 closes divided by current close;
    unlocks the breakout-pressure bonus.
- **Sub-score tightening:**
  - Contraction requires **both** consecutive swing transitions to tighten
    (`r2 < r1 < r0`); partial tightening returns `0`.
  - Volatility returns `0` if `|return_20d| < 2%` (no movement → no
    compression to speak of).
  - Volume switches from raw slope to a hybrid of 20-day log-slope + a
    50-day percentile rank (rewards recent quieting, punishes blow-offs).
  - Pivot score halves when `pivot_touches < 2` (unretested level).
- **Enhancements:**
  - Shakeout bonus of `+0.10` added to `structure` when the final swing-low
    undercuts the prior low **and** price has already recovered above it.
  - Breakout-pressure bonus of `+0.05` added to the aggregate `vcp_score`
    when `close_std_5_norm < 0.005` (coiled-spring signature).
- **Weights rebalanced** to reflect relative importance of the pattern
  definition: contraction `0.28`, volatility `0.20`, volume `0.15`,
  structure `0.17`, pivot `0.15`, range `0.05` (sum = 1.0). Net effect:
  shift 5 pts off `range` (weakest signal) to contraction and structure.
- **Blend:** `combined = 0.5·vcp + 0.3·tech + 0.2·fund` — VCP-led, down
  from the old `0.7·(0.5·tech + 0.5·vcp) + 0.3·fund` which weighted VCP
  and tech equally.
- **Asymmetric readiness:** band is `5%` below pivot, `2%` above (late
  entries punished faster than early ones). Replaces the symmetric 5%
  `READINESS_BAND` with two constants (`READINESS_BAND_BELOW`,
  `READINESS_BAND_ABOVE`).
- **RS as reward-only multiplier:** `combined *= 1 + 0.2·min(rs_score, 1)`
  when `rs_score > 0` **and** `vcp ≥ 0.40`. Non-leaders and weak-VCP
  names are unchanged; cap is +20%. No RS penalty is ever applied.
- **Decision ladder (hard gate on `vcp ≥ 0.40`):**
  - `REJECT/STAGE3_FAIL` if `vcp < 0.40`, regardless of `final`.
  - `BUY_ALERT/READY` if gated, `final ≥ 0.75`, within ±2% of pivot.
  - `WATCHLIST/BUILDING` if gated, `final ≥ 0.55`.
  - `WATCHLIST/CONTRACTING` otherwise (gated).
- **Plumbing:** `scan.py` now computes `rs_score` **before**
  `score_candidate` and passes it through as a keyword arg. Row layout of
  `vcp_candidates` is unchanged.
- **Data wipe:** 71 stale `vcp_candidates` rows (scored under the old
  formula) were deleted from `data/scanner.db` after the rewrite landed;
  next scan re-populated the table under the new formula.
- **Observed effect on `2026-04-23` universe (2,288 scored):** candidates
  dropped from 32 → 16 after the VCP gate was added. All 16 remaining
  WATCHLIST rows have real contraction / structure / pivot signals; the
  previous leakers (KRN, QPOWER, NATIONALUM — all `vcp ≤ 0.29`) are now
  REJECT as expected.
- **Out of scope:** per-sector VCP weight tuning, forward-test harness,
  `vcp_score` confidence interval. The scorer remains deterministic and
  single-threaded; no parameter is learned from data.



## 2026-04-24 — VCP lifecycle state machine

### D-S23. State-then-score: lifecycle classification drives decision
- **Decision:** Classify every candidate into exactly one of eight lifecycle
  states — `TREND`, `BASE_BUILDING`, `CONTRACTING`, `READY`, `BREAKOUT`,
  `EXTENDED`, `NONE` (passed hard filters but no pattern fit), `FAIL`
  (stage-1/2 hard fail). The state is projected to a decision via a fixed
  map (`STATE_TO_DECISION`); the final-score ladder of D-S22 is removed.
- **Rationale:** D-S22 fixed *which* setups score well but still scored
  every stock on the same ladder, producing `WATCHLIST/CONTRACTING` rows
  for stocks that had already broken out (and were therefore no longer
  pre-breakout watch material). Decoupling **timing** (state) from
  **quality** (vcp_score, final_score) means the dashboard's top bucket
  only ever contains stocks in the intended lifecycle phase.
- **New features (`TechnicalFeatures`):**
  - `range_5d_norm` — `(max(h5) − min(l5)) / close`; needed for BREAKOUT.
  - `atr_expanding` — `atr5_recent / atr30_trailing ≥ 1.2`; EXTENDED signal.
  - `volume_spike` — last-bar volume `≥ 1.5 × avg_volume_20d`.
  - `distance_to_ema50` — signed `(close − ema50) / ema50`; EXTENDED reach.
- **State rules (priority: EXTENDED > BREAKOUT > READY > CONTRACTING >
  BASE_BUILDING > TREND > NONE):**
  - `EXTENDED`: `distance_to_pivot > 0.05` and `atr_expanding` and
    `distance_to_ema50 > 0.15`.
  - `BREAKOUT`: `distance_to_pivot > 0` and `range_5d_norm > range_20d`
    and `volume_spike`.
  - `READY`: `vcp ≥ 0.50`, `−0.03 ≤ dist_to_pivot ≤ 0`, `pivot_score > 0.50`,
    `range_20d ≤ 0.08`, `close_std_5_norm < 0.010`. (relaxed in D-S23a)
  - `CONTRACTING`: `vcp ≥ 0.45` and ≥2 of 4 sub-conditions
    (`contraction > 0`, `volatility > 0.40`, `volume > 0.40`,
    `structure > 0.50`). (refactored in D-S23a)
  - `BASE_BUILDING`: `0.08 < range_20d ≤ 0.15`, `vcp ≥ 0.30`,
    `structure ≥ 0.30`.
  - `TREND`: `return_3m > 0.10`, `range_20d > 0.15`, `vcp < 0.30`.
  - `NONE`: anything else that passed stage-1/2.
- **Decision projection:**
  `READY → BUY_ALERT`, `CONTRACTING → WATCHLIST`,
  `BASE_BUILDING | TREND | NONE → IGNORE`,
  `BREAKOUT | EXTENDED → SKIP`, `FAIL → REJECT`.
- **Schema:** the `stage` column carries the new 8-value vocabulary in
  place of the old `{READY, BUILDING, CONTRACTING, STAGE1_FAIL,
  STAGE2_FAIL, STAGE3_FAIL}`. No DDL change; values only. The `decision`
  column now also holds `IGNORE` and `SKIP` alongside the original three.
- **Storage:** `IGNORE` and `SKIP` rows are persisted by default (they
  feed the `r`-toggle view in the dashboard). `REJECT` rows are still
  opt-in via `vcp-scan --store-rejects`. `scan.py` is otherwise unchanged.
- **RS boost:** kept as-is from D-S22 (gated on `vcp ≥ 0.40`). Since any
  `CONTRACTING` (`vcp ≥ 0.45`) and `READY` (`vcp ≥ 0.55`) state already
  clears that gate, the existing gate is a correct subset of the state
  machine and no new condition is needed.
- **Dashboard / loader:** `DASH_DECISIONS_ALL` expands to
  `(BUY_ALERT, WATCHLIST, IGNORE, SKIP, REJECT)`; the `r` binding now
  toggles the full set. The summary row breaks out per-decision counts
  for all five buckets.
- **Data wipe:** 16 stale `vcp_candidates` rows (scored under the D-S22
  ladder) were deleted from `data/scanner.db`; next scan re-populated the
  table under the state machine.
- **Observed effect on `2026-04-23` universe (2,288 scored):** 0 READY,
  0 CONTRACTING, 0 BREAKOUT, 0 EXTENDED, 166 IGNORE (BASE_BUILDING / NONE),
  2,122 REJECT. The top `vcp_score` names (JBCHEPHARM 0.60, ONGC 0.54,
  FEDERALBNK 0.54, LINDEINDIA 0.50) all fall into `BASE_BUILDING` or
  `NONE` — correctly reflecting that on this date no stock is in the
  pre-breakout sweet spot. Thresholds are intentionally tight (per spec);
  they will relax naturally as the set of true VCPs grows.
- **Out of scope:** per-state ranking by confidence score, state-transition
  persistence (today's state vs yesterday's), `scanner vcp-explain` CLI,
  forward-test harness. Thresholds inside `_detect_state` are hard-coded
  constants (`STATE_*`) — future tuning is a separate ADR.

#### D-S23a. CONTRACTING → probabilistic; READY → slightly relaxed
- **Decision (amendment, same day):** Replace the CONTRACTING hard AND-gate
  with a count-of-four sub-score rule, and relax the READY gates by one
  tier. Priority ordering, state vocabulary, and decision projection are
  unchanged.
- **Rationale:** First live run of D-S23 yielded 0 CONTRACTING across 2,288
  scored names on `2026-04-23` — the conjunctive rule
  (`contraction>0 AND volatility>0.5 AND volume>0.5 AND range_20d≤0.10`)
  eliminated legitimate mid-base setups (JBCHEPHARM, ONGC, FEDERALBNK)
  because one sub-dimension lagged. The spec's intent is "valid VCP
  forming," which tolerates one weak leg. Similarly, READY at
  `vcp≥0.55 / pivot>0.6 / dist∈[-0.02,0]` was tight enough to miss
  everything: softening to `0.50 / 0.50 / [-0.03,0]` still demands a
  genuinely coiled setup without over-specifying it.
- **CONTRACTING rule (new):** gate on `vcp ≥ 0.45`; then require at least
  `STATE_CONTRACTING_MIN_SUBS = 2` of the four sub-score conditions
  `contraction > 0`, `volatility > 0.40`, `volume > 0.40`,
  `structure > 0.50`. The `range_20d ≤ 0.10` constraint is dropped
  (priority ordering still routes narrow-range setups to CONTRACTING
  before BASE_BUILDING).
- **READY rule (new):** `vcp ≥ 0.50` (was 0.55), `-0.03 ≤ d2p ≤ 0`
  (was `-0.02`), `pivot_score > 0.50` (was `0.60`). `range_20d ≤ 0.08`
  and `close_std_5_norm < 0.010` unchanged — these encode the
  "coiled-tight" geometry and loosening them would blur into CONTRACTING.
- **New constants:** `STATE_CONTRACTING_CONTRACTION`,
  `STATE_CONTRACTING_STRUCTURE`, `STATE_CONTRACTING_MIN_SUBS`.
  `STATE_CONTRACTING_RANGE_20D` removed.
- **Observed effect on `2026-04-23` universe (2,288 scored):**
  decisions shifted from `{0 WATCHLIST, 166 IGNORE, 2,122 REJECT}` to
  `{8 WATCHLIST, 158 IGNORE, 2,122 REJECT}`. All 8 WATCHLIST rows are
  `CONTRACTING`: JBCHEPHARM (vcp=0.60), ONGC (0.54), FEDERALBNK (0.54),
  LINDEINDIA (0.50), CPSEETF (0.47), ICICIB22 (0.47), ARIES (0.46),
  LLOYDSME (0.46). Still 0 READY — none of the top 8 pass the tight
  `range_20d ≤ 0.08 + std5 < 0.010` geometry on this date, which is
  correct: they are mid-base, not pre-breakout.
- **Tests:** all 268 existing tests still pass. `test_state_contracting_on_valid_vcp`
  remains representative (all four sub-scores hit → 4 of 4 subs); no
  new tests added.

#### D-S24. Ingest Screener quarterly P&L into `financials_quarterly`
- **Decision:** Parse the `#quarters` table from Screener company pages and
  persist it into a new `financials_quarterly(isin, period_end, source,
  report_type, ...)` table keyed by the quarter-end month's last day
  (ISO `YYYY-MM-DD`). `period_end` replaces `fiscal_year` as the temporal
  key because quarterly reporting is month-level.
- **Rationale:** Annual P&L gives trend but not cadence — a trailing-
  four-quarters QoQ or YoY acceleration signal (sales or EPS growth
  picking up in the latest quarter) is a classic Minervini / Mark Ritchie
  leading indicator for pre-breakout names. Having the quarters in the
  same DB as `financials_annual` lets the VCP fundamentals layer add
  "recent-quarter acceleration" sub-scores without a separate fetch.
- **Columns captured (11):** `sales_cr`, `expenses_cr`,
  `operating_profit_cr`, `opm_pct`, `other_income_cr`, `interest_cr`,
  `depreciation_cr`, `profit_before_tax_cr`, `tax_pct`, `net_profit_cr`,
  `eps`. Screener's quarterly table omits `Dividend Payout %` (it is an
  annual-only metric), so `_QL_LABEL_MAP` is derived from `_PL_LABEL_MAP`
  minus that key. Balance-sheet fields are not reported quarterly by
  Screener and are deliberately excluded.
- **Date handling:** `_period_end_from_header("Mar 2023") → "2023-03-31"`
  via a month→last-day lookup (`Mar→03-31`, `Jun→06-30`, `Sep→09-30`,
  `Dec→12-31`, etc.). `TTM` and short-period headers (`Mar 20183m`,
  `9m`) are rejected, matching the annual parser's behaviour. Storing
  ISO dates keeps lexicographic sort order identical to chronological
  order, so `ORDER BY period_end` works without parsing.
- **Idempotency:** same `INSERT … ON CONFLICT(isin, period_end, source,
  report_type) DO UPDATE SET` pattern used for annual tables. Re-running
  the ingest is a no-op on row count; a re-fetch updates in place.
- **Pipeline wiring:** `ingest_one` now calls
  `sdb.upsert_financials_quarterly` after the annual + ratios upserts and
  before `record_fundamentals_ingestion`. `fundamentals_summary` grows a
  `quarterly_rows` field surfaced by `scanner status`.
- **Tests:** `test_period_end_from_header` (boundary cases),
  fixture assertion on INFY's 13 quarterly rows (sorted, unique, no
  dividend-payout key), db roundtrip (`n_q == len(quarterly_financials)`
  and `summary["quarterly_rows"]` matches), idempotency (re-run → same
  row count), CLI status smoke (`quarterly_rows > 0`). Total: 269 tests
  passing (up from 268).
- **Out of scope:** quarterly ratios (`#ratios` is annual-only on
  Screener), derived "acceleration" features on top of quarterly rows —
  those are a VCP fundamentals-layer ADR (future D-S25+).


### D-S25. Structural pivot — last swing-high in a 40-bar base window
- **Decision:** Replace the 10-bar close-max pivot with a structural pivot
  computed over a 40-bar window (`PIVOT_WINDOW = 40`). The pivot is the
  **last 5-bar fractal swing-high** whose bar-index falls inside the
  window; if no swing sits there, fall back to the highest close over the
  last 40 bars. `pivot_range` keeps its "tightness of the last 10 bars"
  semantics (so `PIVOT_RANGE_MAX = 0.08` stays meaningful). `pivot_touches`
  now counts closes within ±2% of the pivot over the full 40-bar base
  (was: last 10 bars).
- **Rationale:** The original 10-bar `closes[-10:].max()` is a micro-pivot,
  not a base pivot. Real VCP pivots form over 3–8 weeks (15–40 bars); a
  10-bar window produces a noisy, near-price pivot that causes
  `distance_to_pivot` to hug zero regardless of structure. The symptoms:
  stocks that had visibly broken out weeks earlier were still being
  classified as `CONTRACTING`/`READY` because their "pivot" was the
  already-broken-out level, and stocks with a clean base but a drifting
  last-10-bar top were rejected for being "far from pivot". Widening to
  40 bars aligns the metric with how traders draw base pivots and makes
  `distance_to_pivot` a true measure of base position.
- **Fallback rationale:** In monotone uptrends with no fractal structure,
  no swing-highs exist inside the window; using `closes[-40:].max()`
  preserves a sensible pivot (the highest recent close) so those names
  don't silently lose the feature. TREND-state names exercise this branch.
- **Code changes (`scanner/vcp/features.py`):**
  - New module constant `PIVOT_WINDOW = 40`.
  - `_find_swings` is now called **before** the pivot block so the pivot
    lookup can scan `sh_all` for swings inside the base window. Return
    shape of `TechnicalFeatures` is unchanged.
  - Pivot block rewritten: `recent_swings = [(i,p) for i,p in sh_all if
    i >= n-PIVOT_WINDOW]`; prefer last entry, else fallback to
    `closes[-PIVOT_WINDOW:].max()`. `pivot_touches` now scans the 40-bar
    slice.
- **Calibration:** `_pivot_score` (in `scorer.py`) and
  `MIN_PIVOT_TOUCHES = 2` are unchanged. Because retests are counted over
  40 bars instead of 10, a well-formed base will generally produce more
  touches, so the "halve if touches < 2" penalty fires less often — this
  is intended: a well-retested pivot is a quality signal, and the old
  10-bar window under-counted it.
- **Compatibility:** Stored `vcp_candidates` rows scored under the
  10-bar pivot become stale (the `pivot`, `distance_to_pivot`, and
  `pivot_touches` columns carry the old semantics). A re-scan is required
  to refresh them; no schema change.
- **Tests (41 total in `test_scanner_vcp.py`, up from 39):**
  - `test_pivot_picks_last_swing_high_in_window_over_10bar_max` — injects
    a clear fractal peak 25 bars back on an otherwise flat series; asserts
    `pivot == 110.0` even though the 10-bar max is far lower.
  - `test_pivot_falls_back_to_window_max_without_swing` — strictly
    monotone rise (no fractal pivots possible); asserts
    `pivot == closes[-40:].max()` and `swing_highs == ()`.
  - `test_compute_technical_features_on_vcp_series` — `distance_to_pivot`
    assertion loosened from `<0.01` to `<0.05` (the old bound encoded the
    micro-pivot's "close pinned 0.5% below 10-bar max" behaviour).
  - All 271 existing tests pass.
- **Out of scope:** base-depth feature (structural low since pivot),
  days-since-pivot, volume-at-pivot, Darvas-style box boundary detection,
  base-count (first-base vs. third-base) — these belong to a later ADR.


### D-S26. Swing spacing filter — collapse clustered fractals
- **Decision:** After fractal detection in `_find_swings`, run a greedy
  non-maximum-suppression pass that collapses adjacent same-type swings
  closer than `SWING_MIN_SPACING = 6` bars, keeping the more extreme value
  (higher for highs, lower for lows). Implemented via a new helper
  `_apply_spacing(swings, min_spacing, *, prefer)`.
- **Rationale:** The 5-bar fractal rule only enforces that a swing is the
  extreme of its 11-bar neighbourhood — two swing-highs can still sit 6–10
  bars apart with different values, and with low fractal-`n` (used in the
  contraction / structure scorers) even 3–4 bar separation is possible.
  Without a spacing filter, `swing_highs[-3:]` can collapse into a
  single-week cluster, which makes `_contraction_score` and
  `_structure_score` measure intra-cluster noise instead of structural
  tightening. It also distorts the D-S25 pivot lookup: a minor clustered
  swing can win over a genuine earlier peak because it is more recent.
- **Algorithm:** walks the fractal list left-to-right; if the next swing
  is within `min_spacing` bars of the last accepted swing, keep whichever
  is more extreme and drop the other. Greedy rather than optimal
  (a windowed argmax would pick the globally best peak per cluster) —
  simpler, cheap, and sufficient for the typical 2–3 candidate cluster
  sizes observed on real bars.
- **Threshold choice (`6`):** user review specified 5–7 bars; 6 is the
  middle. Below 5 is dominated by the fractal rule itself (tied values);
  above 8 starts discarding legitimate secondary swings that contribute to
  contraction analysis.
- **Code changes (`scanner/vcp/features.py`):**
  - New constant `SWING_MIN_SPACING = 6`.
  - New helper `_apply_spacing(swings, min_spacing, *, prefer)`.
  - `_find_swings` calls `_apply_spacing` on both `sh` and `sl` before
    returning; call sites (pivot lookup, `swing_highs`, `swing_lows`) are
    unchanged — they see filtered lists.
- **Tests (2 new, total 273):**
  - `test_apply_spacing_collapses_close_cluster_to_extreme` — direct unit
    test covering both `prefer='max'` and `prefer='min'` branches.
  - `test_find_swings_applies_spacing_filter_end_to_end` — synthetic
    highs with two fractal-detected peaks 4 bars apart; asserts only the
    taller survives the module-level filter.
- **Compatibility:** No schema change; stored `vcp_candidates` rows remain
  valid numerically (they were already scored under fractal output). New
  scans may produce marginally different `contraction` / `structure`
  sub-scores on names with previously clustered swings.
- **Out of scope:** spacing for cross-type adjacency (swing-high next to
  swing-low), adaptive spacing based on ATR / volatility, optimal
  windowed-argmax rather than greedy NMS.


### D-S27. VCP `_volume_score` — explicit 3-part weighted formula
- **Decision:** Replace the two-part (slope + `pct <= avg20`) volume score
  with a transparent weighted sum of three components, each clamped to
  [0, 1]:
  - `slope_part` — 20-bar log-volume slope (negative → 1.0). Unchanged.
  - `dryup_part` — direct ratio `avg_volume_20d / avg_volume_50d`, mapped
    so 1.00 → 0.0 and ≤0.70 → 1.0 (linear in between).
  - `exp_part` — volume expansion near the pivot: only active when
    `|distance_to_pivot| ≤ 0.02`. Compares the mean of the last 3 bars'
    volume against `avg_volume_20d`; 1.0× → 0.0, ≥1.5× → 1.0. Captures
    the "volume kicks in right at the pivot" VCP tell. Zero otherwise.
  - Final: `0.40·slope + 0.40·dryup + 0.20·expansion`.
- **Rationale:** the previous `pct_part` (fraction of the 50-day pool at
  or below `avg_volume_20d`) was hard to reason about and conflated two
  ideas — dry-up vs. distribution tail. A uniform 100k pool and a
  100k-20d-mean produced 1.0 even though there was no structural dry-up
  at all. The new form uses the 20d/50d ratio, which is the classical
  volume-contraction measure. The explicit `exp_part` rewards the
  late-stage absorption → expansion behaviour that the old score could
  only hint at via the slope term.
- **Threshold choices:**
  - Dry-up band 0.70–1.00: 70% of the 50d average on a 20d window is
    what Minervini / O'Neil-style pre-breakout bases typically show; a
    20d==50d volume profile has no dry-up worth scoring.
  - Expansion trigger `≥ 1.5× avg20` over 3 bars near pivot: same
    multiplier used by `volume_spike` elsewhere in features; 3-bar
    smoothing avoids one-day flukes. Proximity window ±2% matches the
    `pivot_touches` band (D-S25) so pivot-adjacent logic is consistent.
  - Weights (0.40 / 0.40 / 0.20): slope and dry-up describe the base
    interior (equal weight); expansion is a late-stage confirmation and
    only fires near the pivot, so it gets a smaller share but still
    enough to lift a clean near-pivot setup by ~0.2·0.15 = 0.03 in the
    aggregate score.
- **Code changes (`scanner/vcp/scorer.py`):** `_volume_score` rewritten;
  signature unchanged. Depends on existing `TechnicalFeatures` fields
  (`avg_volume_50d`, `distance_to_pivot`, `volume_last_50d`). No feature
  schema change.
- **Tests (3 new, 46 in `test_scanner_vcp.py` / 276 total):**
  - `test_volume_score_dryup_ratio_scales_linearly` — pins the dry-up
    mapping at ratios 1.00 / 0.85 / 0.70.
  - `test_volume_score_expansion_only_triggers_near_pivot` — same setup
    with `distance_to_pivot=0.01` vs `-0.10` produces 0.20 vs 0.00,
    proving the proximity gate.
  - `test_volume_score_handles_missing_inputs` — all-None inputs score
    0.0 without raising.
  - Existing `test_volume_score_rewards_recent_below_50d_median` still
    passes under the new formula.
- **Compatibility:** `vcp_candidates.volume_score` values may shift on
  re-scan; no schema change, so stored rows remain valid. Dashboard /
  TUI read the aggregate `score` — effect bounded to ≤0.15 swing per
  name.
- **Out of scope:** volume-at-pivot absolute levels, relative-volume vs.
  sector, turnover-ratio-based dry-up — these require additional feature
  fields and belong to a later ADR.


### D-S28. Relax READY + introduce EARLY_READY lifecycle state
- **Decision:** Loosen the `READY` coil-quality gates and introduce a new
  `EARLY_READY` state between `READY` and `CONTRACTING` in the
  priority-ordered state machine. New priority:
  `EXTENDED > BREAKOUT > READY > EARLY_READY > CONTRACTING >
   BASE_BUILDING > TREND > NONE`.
- **READY changes:**
  - `STATE_READY_RANGE_20D`: `0.08 → 0.12`.
  - `STATE_READY_STD5`: `0.010 → 0.015`.
  - `STATE_READY_DIST_BELOW` unchanged at `-0.03` (READY remains
    "price is at / just under the pivot").
  - `STATE_READY_VCP` and `STATE_READY_PIVOT` unchanged (0.50 / 0.50).
- **EARLY_READY gates (new):**
  - `vcp ≥ 0.50` (same as READY — coil quality must be real).
  - `pivot_score > 0.50` (same as READY).
  - `range_20d ≤ 0.12`, `close_std_5_norm < 0.015` (same as relaxed READY).
  - `-0.06 ≤ distance_to_pivot ≤ -0.02` — the *only* dimension that
    differs from READY. Price is 2–6% below pivot: coil is in, breakout
    has not arrived.
- **Decision projection:** `EARLY_READY → WATCHLIST`. Keeps the alerting
  surface conservative: only `READY` (price at pivot) produces
  `BUY_ALERT`. `EARLY_READY` sits alongside `CONTRACTING` in the
  WATCHLIST bucket so these names appear on the dashboard but do not
  trigger a buy alert.
- **Rationale:** the pre-D-S28 `READY` gates (range ≤ 8%, std < 1%) were
  calibrated tight enough that most real NSE bases — which trade 8–12%
  peak-to-trough and show 1.0–1.5% 5-day stdev at the tail of a
  contraction — never cleared them, so the scanner leaned toward
  mid-base `CONTRACTING` and skipped over legitimate setups within a
  day or two of their pivot. Relaxing the range/std caps to 12% / 1.5%
  admits those setups. Separately, analysts commonly want to see names
  that are *almost* at their pivot — `EARLY_READY` carves out the
  2–6%-below band as its own addressable stage without polluting the
  `BUY_ALERT` feed.
- **Overlap handling:** READY covers `[-0.03, 0.0]`, EARLY_READY covers
  `[-0.06, -0.02]`. In the shared `[-0.03, -0.02]` band priority ordering
  ensures READY wins. Both states use strict-less comparisons on
  `close_std_5_norm` so a value of exactly `0.015` fails both.
- **Code changes (`scanner/vcp/scorer.py`):**
  - New constants `STATE_EARLY_READY_*` (6 of them).
  - `_detect_state` gains an EARLY_READY branch between the READY branch
    and the CONTRACTING branch.
  - `STATE_TO_DECISION` gains `"EARLY_READY": "WATCHLIST"`.
  - Docstrings in the module header and `score_candidate` updated to the
    9-valued vocabulary and new priority.
- **Tests (4 new, 50 in `test_scanner_vcp.py` / 280 total):**
  - `test_state_ready_accepts_relaxed_range_and_std_d_s28` — pins the
    new READY caps (`range_20d = 0.11`, `std5 = 0.012`).
  - `test_state_early_ready_on_coiled_but_below_pivot_d_s28` — same
    coil as READY but `d = -0.04` → `EARLY_READY`.
  - `test_state_ready_beats_early_ready_in_overlap_band_d_s28` —
    `d = -0.025` must resolve to READY by priority.
  - `test_state_early_ready_rejects_when_price_too_far_below_pivot_d_s28`
    — `d = -0.08` must not classify as EARLY_READY.
  - `test_state_to_decision_mapping_is_exhaustive` extended to cover the
    new state.
- **Compatibility:**
  - `vcp_candidates.stage` now admits `"EARLY_READY"` in addition to
    the existing 8 values. No DDL change; column is `TEXT`.
  - Dashboard (`scanner_loader.DASH_DECISIONS_*`, `scanner_views`) filters
    by `decision` (BUY_ALERT/WATCHLIST/…), not by `stage`, so no TUI
    change is required — EARLY_READY rows simply appear in the WATCHLIST
    bucket alongside CONTRACTING.
  - Reason-strings include `state=EARLY_READY` naturally via the existing
    `reasons.append(f"state={state}")` line.
- **Out of scope:** a dedicated `EARLY_BUY_ALERT` decision tier, plot
  styling for EARLY_READY on the TUI, or re-tuning the CONTRACTING gates
  — the EARLY_READY band sits entirely above CONTRACTING's floor so no
  compensating change is needed there.


### D-S29. BREAKOUT confirmation — 3-bar volume expansion, not single bar
- **Decision:** Switch the `BREAKOUT` branch of `_detect_state` from the
  single-bar `volume_spike` gate to a new 3-bar average gate,
  `volume_expansion_3bar`. The feature is defined as
  `mean(volumes[-3:]) ≥ 1.3 × avg_volume_20d`. `volume_spike` (last bar
  only, 1.5× avg20) is retained as a separate feature for any future
  single-day-spike use case and for backwards compatibility with callers
  that introspect the dataclass.
- **Rationale:** a single loud volume bar — a fund fat-fingering an
  order, an F&O expiry flush, a block trade — is routinely enough to
  satisfy `vol[-1] ≥ 1.5 × avg20` on a name that otherwise shows no
  institutional pressure. Combined with the BREAKOUT branch's other
  conditions (crossed pivot + 5-bar range > 20-bar range), this produced
  false `BREAKOUT` classifications on the day of the spike that
  disappeared the next scan. Averaging over three bars forces the
  confirmation to persist through the next-day fade, which is the
  standard institutional accumulation pattern.
- **Threshold choices:**
  - `1.3×` on a 3-bar mean is stricter than it looks: an even
    distribution at 1.3× across three bars is equivalent to requiring
    each bar to beat the 20d average meaningfully. A single 1.9× bar
    surrounded by two 1.0× bars yields a 3-bar mean of 1.3× — the
    threshold is deliberately set so a lone spike cannot carry the
    signal alone, a reinforcing bar on either side is required.
  - `3 bars` matches the O'Neil / IBD "follow-through" convention used
    for index breakouts; on individual names the accumulation typically
    shows as 2–3 consecutive above-average bars.
- **Code changes:**
  - `scanner/vcp/features.py`:
    - New field `volume_expansion_3bar: bool | None` on
      `TechnicalFeatures`.
    - Computed in `compute_technical_features` when `n ≥ 3` and
      `avg_volume_20d > 0`.
    - `volume_spike` field and computation untouched.
  - `scanner/vcp/scorer.py`: BREAKOUT branch gates on
    `t.volume_expansion_3bar is True` instead of `t.volume_spike is True`.
- **Tests (3 new, 53 in `test_scanner_vcp.py` / 283 total):**
  - `test_volume_expansion_3bar_true_when_last_3_avg_over_threshold` —
    three 140k bars against a 100k baseline → True.
  - `test_volume_expansion_3bar_false_on_single_bar_spike_only` — lone
    160k bar trips `volume_spike` but fails the 3-bar mean gate.
  - `test_state_breakout_rejects_single_bar_spike_without_3bar_confirmation`
    — end-to-end: `volume_spike=True, volume_expansion_3bar=False` no
    longer returns `"BREAKOUT"`.
  - Baseline fixture (`_baseline_tech`) and the existing BREAKOUT /
    EXTENDED tests updated to carry `volume_expansion_3bar` where the
    scenario requires confirmation.
- **Compatibility:**
  - No SQLite DDL change; `volume_spike` remains in the dataclass and
    was never persisted as a column.
  - In-memory callers that constructed `TechnicalFeatures` directly
    (currently: only tests) must supply `volume_expansion_3bar`. No
    production callers construct the dataclass manually.
- **Out of scope:** sector-relative volume gates, pre-market / closing
  auction volume attribution, and weighting the 3-bar mean by body size
  (green vs. red bars) — those belong to a later ADR focused on
  absorption vs. distribution distinction.
