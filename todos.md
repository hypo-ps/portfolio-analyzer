# TODOs — Phase 0 Portfolio Analyzer

> Active work and pending questions. Remove or strike items once complete.
> Mirrors the agent task list but is the human-readable source of truth.

## Awaiting user

- [ ] Provide Kite API credentials in `.env` (copy from `.env.example`).
- [x] ~~Populate `data/nifty500.csv` and `data/nifty50.csv`~~ — now auto-refreshed daily from niftyindices.com.

## Build

- [x] Scaffold project, pyproject, .env handling, .gitignore.
- [x] Install deps in `.venv` (Python 3.12).
- [x] `auth.py`, `util/ohlc.py`, `kite_client.py`, `instruments.py`, `stock_analysis.py`, `market.py`, `scoring.py`, `report.py`, `cli.py`, `refresh.py`, `yf_fetch.py`.
- [x] Auto-refresh NSE constituents + sector map on every run (with `refresh` subcommand and `--no-refresh` flag).
- [x] Pivot price data source from Kite Historical to yfinance (D26). Removed `InstrumentCache`, `util/rate_limit`, and Kite historical wrappers.
- [x] Tests (58 passing) covering scoring, market trend + override, stock metrics, refresh.
- [x] Backtest package (`backtest/`): OHLC parquet cache, state-machine broker,
      vectorized Phase 0 strategy, event-driven simulator, perf + exit metrics.
- [x] Backtest tests (24 new, 82 total): broker, strategy parity vs scalar
      scorer, simulator semantics (init, reduce-then-exit, equity curve), metrics formulas.
- [x] CLI: `python -m portfolio_analyzer backtest --start --end --capital --out`.
- [ ] README (intentionally deferred — not required for Phase 0 completion).

## Verification still needed (live run)

- [ ] First live run end-to-end: confirm holdings fetch + yfinance batch fetch + scoring JSON.
- [ ] Spot-check: any NIFTY 500 symbols Yahoo doesn't have data for (delisted / renamed). Log-only; expected to be rare.
- [ ] Run full 5-year backtest and review equity curve vs NIFTY 500 buy-and-hold.
      First run downloads ~502 tickers × 5y (~2 min); subsequent runs hit parquet cache.

## Open questions / risks

*(resolved at approval — see decisions D18–D20; reopen here if revisited)*

## Out of scope (Phase 0)

- VCP pattern detection
- Order placement / execution
- Persistent DB or OHLC cache
- Watchlist screening beyond current holdings
- Intraday / tick-level logic

## Phase 1 — Scanner data pipeline

Design: NSE/BSE VCP scanner covering full equity universe with clean OHLCV +
fundamentals. Ingestion, universe, and historical OHLCV storage come first;
VCP detection and fundamentals remain out of scope for this slice.

- [x] NSE UDiFF bhavcopy fetcher + parser (`scanner/bhavcopy.py`).
- [x] SQLite storage with `stock_master` (ISIN PK), `market_data`, `ingestion_log`.
- [x] Idempotent `ingest_date` / `ingest_range` orchestrator.
- [x] CLI: `scanner ingest`, `scanner ingest-range`, `scanner status`.
- [x] Tests: parser fixtures, db upsert idempotency, orchestrator with mocked
      network, CLI smoke (26 new tests, 184 total).
- [x] Live end-to-end verified: 2,654 NSE equity rows ingested for 2026-04-22.
- [x] Corporate-actions normalization for splits + bonuses
      (`scanner/corp_actions.py`, `scanner/ca_ingest.py`, `corporate_actions`
      table, `cumulative_adjustments`, `adjusted_market_data` view,
      `scanner ca-ingest` / `ca-rebuild-adjustments` CLI). Dividends stored
      as metadata only. Live verified: 311 CAs in a 90-day window, incl. 13
      BONUS and 8 SPLIT events correctly parsed.
- [x] Historical backfill: populated 2024-07-08 → 2026-04-23, ~1.03M bars
      across 2,939 ISINs, 4,284 corporate actions, 28,012 price-adjusted bars.
- [x] Fundamentals ingestion layer — Screener.in scraper covering sector /
      industry / market-cap / P/E / ROE / ROCE / 52w high-low plus 10y+ of
      annual P&L, balance-sheet highlights and ratios.
      Tables: `fundamentals_meta`, `financials_annual`, `ratios_annual`,
      `fundamentals_ingestion_log`. CLI: `scanner fundamentals-ingest`.
- [ ] BSE bhavcopy ingestion (same UDiFF format on BSE's server).
- [ ] Cross-exchange ISIN mapping + dedupe for dual-listed names.
- [ ] SME series (`SctySrs=SM`) as a separate universe.
- [ ] Rights-issue TERP adjustment (currently stored but not adjusted).
- [ ] Merger / demerger / consolidation handling.
- [ ] Dividend-adjusted total-return series (separate view).
- [ ] BSE corporate actions (same schema, different source).
- [ ] Fundamentals fallback sources (Tickertape, Yahoo) for names not on
      Screener; promoter-holding / shareholding-pattern block.
- [x] VCP feature engineering + scanner engine — four-stage pipeline
      (Stage-1 hard filters, Stage-2 fundamentals, Stage-3 VCP sub-scores,
      readiness + final blend) producing `WATCHLIST`/`BUY_ALERT`/`REJECT`
      decisions. Modules: `scanner/vcp/{features,fundamentals,scorer,scan}.py`,
      `vcp_candidates` table, `scanner vcp-scan` CLI. Full-universe scan
      runs end-to-end in ~10s on ~3k ISINs.
- [x] NIFTY 500 index ingestion + RS score — `scanner/index_ingest.py`,
      new `index_data` table, `scanner index-ingest` CLI. `vcp_candidates`
      carries `return_50d`, `benchmark_return_50d`, `rs_score` (same
      formula as Phase 0 live analyzer).
- [x] Scanner dashboard — `tui/scanner_dash.py` Textual app launched via
      `scanner dash`. Sortable candidates table joined with
      `fundamentals_meta` + per-row drilldown pane. Toggle include-rejects
      at runtime (`r`); sort by cursored column (`s`).
- [ ] VCP BUY_ALERT confirmation window — require a ≥1-bar breakout above
      pivot on above-average volume before promoting READY → CONFIRMED.
- [ ] VCP backtest harness: replay `scanner vcp-scan` per trade date across
      the historical window, stub-buy BUY_ALERTs, measure forward
      N-day/90-day win rate, drawdown and hit rate by stage / sector.
- [ ] Per-sector normalization of fundamental_score (cross-sector ROE/D/E
      baselines differ meaningfully).
- [ ] `scanner vcp-explain --symbol S` — pretty-print the full reason trail
      (stage outcomes, sub-score breakdown, pivot distance) for a single name.
