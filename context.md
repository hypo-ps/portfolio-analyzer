# Context — Phase 0 Portfolio Analyzer

> Stable knowledge. Update only when core intent, contract, or definitions change.

## Purpose

Phase 0 of a VCP-oriented trading system. Builds the **decision + data backbone**:
a deterministic analyzer that evaluates the user's Zerodha portfolio against the
market each day and produces actionable HOLD/REDUCE/EXIT decisions.

**VCP pattern detection is explicitly out of scope for Phase 0.**

## Core Philosophy

- Price > everything
- Relative strength matters more than fundamentals
- Cut weak stocks early
- Hold leaders near highs

## Target System (end-state)

The full product is a four-layer decision system. Phase 0 builds the
foundations for layer A (decisions) and the context inputs layer C depends on.
Layers B and D are later phases; their shape is fixed here so Phase 0
artefacts (metrics, state machine, data windows) are designed to feed them
without rework.

### A. Portfolio Advisor — per-holding decision

```json
{
  "symbol": "XYZ",
  "decision": "HOLD | REDUCE | EXIT | ADD",
  "confidence": 0.0,
  "reasons": {
    "technical": "...",
    "market_regime": "...",
    "sector_strength": "...",
    "fundamentals": "..."
  }
}
```

- Extends the current `HOLD | REDUCE | EXIT` contract with an `ADD` action
  (re-arm / size-up for winners) and a scalar `confidence`.
- `reasons` is structured (per-signal) rather than the flat list Phase 0
  currently emits; each sub-key is sourced from one of the layers below.

### B. Opportunity Engine — VCP-based watchlist

```json
{
  "symbol": "ABC",
  "setup": "VCP",
  "stage": "early | mid | breakout-ready",
  "score": 0.0,
  "why": {
    "structure": "...",
    "volume": "...",
    "trend": "...",
    "sector": "..."
  }
}
```

- Scans beyond current holdings (NIFTY 500 pool already wired via D-BT22/23).
- Detects VCP structure (Volatility Contraction Pattern): tightening
  pullbacks, declining volume into the base, prior uptrend, breakout pivot.
- Explicitly **out of scope for Phase 0** (see Non-goals).

### C. Context Layer — market state feeding A and B

- Market trend (blended NIFTY 500 + NIFTY 50 — implemented)
- Breadth (% of NIFTY 500 above 50DMA — implemented)
- Sector rotation (sector-level relative strength — partial; `sector_map`
  exists, RS aggregation pending)
- News sentiment (external feed; not started)

### D. Fundamental Layer — quality filter

- Growth quality (revenue/earnings CAGR, consistency)
- Earnings trend (YoY / QoQ direction, beats/misses)
- Profitability (ROE, ROCE, margin trend)
- Debt (D/E, interest coverage)

Consumed by A (feeds `reasons.fundamentals` + confidence) and B (VCP
candidates filtered by minimum fundamental quality). Data source TBD;
Phase 0 deliberately ignores fundamentals.

## Stack & Location

- Language: Python
- Project root: `portfolio-analyzer/` (this directory)
- Portfolio source: Zerodha Kite Connect (`holdings()` only)
- Price data source: Yahoo Finance via `yfinance` (daily closes for indices, holdings, breadth)
  - Index tickers: NIFTY 500 → `^CRSLDX`, NIFTY 50 → `^NSEI`
  - Equity tickers: NSE tradingsymbol + `.NS` (e.g. `INFY.NS`)
- Reference data: NSE (niftyindices.com) constituent CSVs, auto-refreshed daily on `run`
- Credentials: `.env` for `KITE_API_KEY` / `KITE_API_SECRET`; access_token in-memory only, re-login every run

## Reference Data Refresh

On each `run`, if local NSE constituent files are not dated today, they are re-downloaded from:
- `https://niftyindices.com/IndexConstituent/ind_nifty500list.csv`
- `https://niftyindices.com/IndexConstituent/ind_nifty50list.csv`

Written to:
- `data/nifty500.csv`, `data/nifty50.csv` — symbol-only format
- `data/sector_map.auto.csv` — symbol → industry (from `Industry` column)

`data/sector_map.csv` is a manual override file; it is merged at load time and wins over auto.

Use `python -m portfolio_analyzer refresh [--force]` to refresh explicitly, or
`python -m portfolio_analyzer run --no-refresh` to skip refresh and use whatever is on disk.
If network fails, the refresh logs a warning and falls back to existing files.

## Output Contract (Strict JSON)

```json
{
  "date": "YYYY-MM-DD",
  "market": {
    "trend": "UPTREND | DOWNTREND | SIDEWAYS",
    "return_50d": 0.0
  },
  "portfolio_summary": {
    "total_stocks": 0,
    "hold_count": 0,
    "reduce_count": 0,
    "exit_count": 0
  },
  "stocks": [
    {
      "symbol": "INFY",
      "sector": "IT",
      "price": 0.0,
      "trend": "STRONG | WEAK",
      "relative_strength": 0.0,
      "drawdown_from_high": 0.0,
      "score": 0.0,
      "decision": "HOLD | REDUCE | EXIT",
      "reasons": ["..."]
    }
  ],
  "pending_exits": [
    {"symbol": "XYZ", "days_remaining": 2, "enqueued_date": "YYYY-MM-DD"}
  ],
  "top_performers": ["..."],
  "weakest_stocks": ["..."]
}
```

Additive fields under `market` (pending approval): `breadth_pct`, `breadth_regime`,
`nifty500_trend`, `nifty50_trend`. See `decisions.md`.

`pending_exits` carries the D-BT25/D-BT26 defer queue between runs (D-BT28):
each entry is a symbol whose EXIT signal has not yet been acted on, with a
`days_remaining` counter that decrements each run until the timer expires or
an acute breakdown fires the EXIT immediately.

## Definitions

### Per-stock trend
- `STRONG` → `price > 50DMA > 200DMA`
- `WEAK` → otherwise

### Relative Strength
`RS = stock.return_50d - market.return_50d`

Market reference = **NIFTY 500** `return_50d`.

### Drawdown
`drawdown = (price - 52w_high) / 52w_high` (negative-valued)

52-week high = max of daily **close** over last 252 trading days.

### Scoring
| Condition | Points |
|---|---|
| Strong trend | +2 |
| Positive RS | +2 |
| Near highs (drawdown > -10%) | +1 |
| Large drawdown (< -25%) | -2 |

### Decision
- `score >= 3` → HOLD
- `score in [1, 2]` → REDUCE
- `score <= 0` → EXIT

## Market Trend Logic

### Per-index classification (same rule as per-stock trend)
- `price > 50DMA > 200DMA` → UPTREND
- `price < 50DMA < 200DMA` → DOWNTREND
- else → SIDEWAYS

### Blended trend
- NIFTY 500 weight: **70%**
- NIFTY 50 weight:  **30%**
- Map `{UP: +1, SIDEWAYS: 0, DOWN: -1}`, compute weighted score:
  - `>= 0.5` → UPTREND
  - `<= -0.5` → DOWNTREND
  - else → SIDEWAYS

### Breadth
`breadth_pct = (# NIFTY 500 stocks with price > 50DMA) / 500`

Regimes: `>= 0.65` strong · `0.40 – 0.65` mixed · `< 0.40` weak.

### Breadth override (on blended trend)
- Blended UPTREND but breadth < 40% → downgrade to **SIDEWAYS**
- Blended DOWNTREND but breadth > 65% → upgrade to **SIDEWAYS** *(symmetric — pending confirmation)*

## Data Windows

- Per holding: last ~260 trading days of daily OHLC (covers 200DMA warm-up + 52w high + 50d return).
- Indices (NIFTY 500, NIFTY 50): same window.
- Breadth: latest close + 50DMA per NIFTY 500 constituent (single historical call each).

## Fetch Strategy

- Single batched call stream to `yfinance` (default batch size 50, `threads=False`).
- Ticker set per run = `{^CRSLDX, ^NSEI} ∪ nifty500_syms ∪ holding_syms`, deduped.
- Expected wall time: tens of seconds for ~500 tickers (Yahoo's batch endpoint is fast).
- Failed or missing tickers are logged and excluded from downstream scoring.

## Backtesting (Phase 0 decisions)

- Package: `src/portfolio_analyzer/backtest/` — `data`, `portfolio`, `broker`,
  `phase0_strategy`, `simulator`, `metrics`, `runner`.
- CLI: `python -m portfolio_analyzer backtest --start ... --end ... --capital ...`.
- Default window: last **5 years** ending at yesterday's close.
- Initial portfolio: **NIFTY 50 equal-weight** at the start date's Open (D-BT1).
- Signal path reuses the live scoring thresholds; the strategy module vectorizes
  the computation and has a parity test against scalar `score_stock`.
- Decision state machine per position: `FULL → REDUCED → EXITED` (one-way; D-BT3).
- T-close signal → T+1 open execution (D-BT8). Adjusted prices (D-BT6). No costs (D-BT7).
- Data cache: `data/backtest_cache/{ticker}.parquet`, gitignored (D-BT9).
- Output: JSON report with CAGR / max-DD / Sharpe / benchmark vs NIFTY 500 +
  exit diagnostics (avg forward return, hit rate); equity, decisions, and
  fills are written as CSV siblings to the JSON.

## Non-goals (Phase 0)

- No VCP pattern detection
- No order placement / execution
- No persistent storage (DB, disk cache for access_token or OHLC — beyond the
  backtest parquet cache which is local-only and gitignored)
- No watchlist screening beyond current holdings
- No intraday logic — daily close only

## Phase 1 — Scanner data pipeline

Phase 1 covers the **full NSE/BSE equity universe** with clean OHLCV (and
later, fundamentals). It feeds layers B (VCP watchlist) and D (fundamentals)
in the target end-state described above, and lives alongside Phase 0 in the
same project rather than as a separate codebase (D-S1).

### Subpackage

- `src/portfolio_analyzer/scanner/`
  - `bhavcopy.py` — NSE UDiFF fetch + CSV parse (ISIN-keyed rows)
  - `db.py` — SQLite schema, connection, upsert helpers
  - `ingest.py` — orchestrator: idempotent per-date ingestion, range iteration
  - `corp_actions.py`, `ca_ingest.py` — corporate-action fetcher + orchestrator
  - `fundamentals/screener.py` — Screener.in fetcher + HTML parser
  - `fundamentals/ingest.py` — fundamentals orchestrator across `stock_master`

### Data source

- **NSE UDiFF Common Bhavcopy Final** —
  `https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip`
- Equity filter: `FinInstrmTp == 'STK' AND SctySrs IN {EQ, BE}`
  (~2.6k rows/day; SME and derivatives deferred).
- Non-trading days return HTTP 404 and are surfaced as `no_data`, not errors.

### Storage (`data/scanner.db`, SQLite, WAL, FK on)

- `stock_master(isin PK, symbol, name, series, exchange, first_seen_date, last_seen_date)`
- `market_data(isin, trade_date, open, high, low, close, prev_close, volume, turnover, trades)`
  with PK `(isin, trade_date)` and FK `isin → stock_master`.
- `ingestion_log(trade_date PK, source, rows_ingested, ingested_at)` —
  drives the idempotency check.

ISIN is the primary key (D-S4) so renames, corporate actions, and future BSE
ingestion can share the same rows without a symbol remap layer.

### CLI

```
portfolio-analyzer scanner ingest                  --date YYYY-MM-DD [--force] [--db PATH]
portfolio-analyzer scanner ingest-range            --start YYYY-MM-DD --end YYYY-MM-DD
                                                   [--force] [--db PATH]
portfolio-analyzer scanner ca-ingest               --start YYYY-MM-DD --end YYYY-MM-DD
                                                   [--no-rebuild] [--db PATH]
portfolio-analyzer scanner ca-rebuild-adjustments  [--db PATH]
portfolio-analyzer scanner fundamentals-ingest     [--symbol S]... [--limit N]
                                                   [--refresh-days D] [--force] [--db PATH]
portfolio-analyzer scanner status                  [--db PATH]
```

All commands emit JSON on stdout. `ingest` / `ca-ingest` exit 1 on `error`
(network / parse failure); `no_data` and `skipped` are non-error outcomes.

### Corporate actions (D-S8/D-S9/D-S10/D-S11)

- `scanner/corp_actions.py` — NSE JSON API fetcher + subject classifier.
  Emits one `CorpAction` per event, with compound subjects
  (e.g. `Bonus 4:1/Face Value Split ...`) expanded into two rows.
- Extra tables in `data/scanner.db`:
  - `corporate_actions(isin, ex_date, action_type, ratio_num, ratio_den,
    price_factor, raw_subject, symbol, name, source, ingested_at)` with PK
    `(isin, ex_date, action_type, raw_subject)`.
  - `cumulative_adjustments(isin, trade_date, factor)` — materialized, only
    rows where `factor != 1.0`.
  - `adjusted_market_data` view — raw OHLCV × `COALESCE(factor, 1.0)`.
- Only `BONUS` and `SPLIT` carry a non-1.0 factor. Dividends, rights,
  buybacks, mergers, etc. are stored as metadata only for this slice.

### Fundamentals (D-S12/D-S13/D-S14/D-S15)

- `scanner/fundamentals/screener.py` — fetches
  `screener.in/company/{SYMBOL}/{variant}/` with a 2s throttle and
  exponential backoff, falling back from `consolidated` to `standalone`;
  parses the `top-ratios` block, `#profit-loss`, `#balance-sheet` and
  `#ratios` tables into a typed `ScreenerCompany`.
- `scanner/fundamentals/ingest.py` — iterates `stock_master`, skips ISINs
  whose last successful fetch is newer than `SCREENER_REFRESH_AFTER_DAYS`
  (7 days by default), upserts per-symbol, and logs status to
  `fundamentals_ingestion_log`.
- Extra tables in `data/scanner.db` (all ISIN-keyed, `source='screener'`):
  - `fundamentals_meta` — sector, industry, market-cap, PE, ROE, ROCE,
    52w band, promoter holding.
  - `financials_annual(isin, fiscal_year, source, report_type, ...)` —
    sales, expenses, OPM%, net profit, EPS, borrowings, total assets.
  - `ratios_annual(isin, fiscal_year, source, report_type, ...)` — debtor
    days, inventory days, CCC, working-capital days, ROCE%.
  - `fundamentals_ingestion_log(isin, source, status, detail, report_type,
    fetched_at)` — per-symbol outcome, drives the freshness skip.
- Money columns store rupees crore; percentages store decimals (28% → 0.28).

### Non-goals (Phase 1)

- BSE ingestion, dual-listed dedupe, SME series
- Rights / merger / demerger price adjustments
- Dividend-adjusted total-return series
- Tickertape / Yahoo fundamentals fallbacks, quarterly P&L, cash flow
- VCP feature engineering / scanner engine
- Any change to the Phase 0 live analyzer or backtest contracts
