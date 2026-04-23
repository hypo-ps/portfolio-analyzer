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

