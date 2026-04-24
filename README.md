# portfolio-analyzer

A deterministic, VCP-oriented trading toolkit for Indian equities. It has two
layers that share a codebase:

- **Phase 0 — Portfolio Advisor.** Evaluates your Zerodha holdings against
  market context each day and emits a strict-JSON report with per-stock
  `HOLD / REDUCE / EXIT` decisions, plus a Phase-0 backtester.
- **Phase 1 — Scanner.** Ingests the full NSE equity universe (UDiFF
  bhavcopy, corporate actions, Screener.in fundamentals, benchmark indices)
  into a local SQLite DB and runs a four-stage VCP scanner with a Textual
  dashboard.

Target runtime: Python 3.10+. Data sources: `yfinance` (prices),
`kiteconnect` (holdings), `niftyindices.com` (constituents),
`nsearchives.nseindia.com` (bhavcopy + corporate actions), `screener.in`
(fundamentals).

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

This exposes the CLI as both `portfolio-analyzer` and `python -m portfolio_analyzer`.

## Configure

Credentials are read from `.env` at the repo root (never committed):

```
KITE_API_KEY=...
KITE_API_SECRET=...
```

The access token is obtained via interactive login on every `run` and kept
in memory only — nothing is persisted to disk.

## Phase 0 — Portfolio Advisor

```bash
# Full run: refresh constituents, login, fetch prices, score, emit JSON.
portfolio-analyzer run

# Skip the daily constituent refresh and reuse local CSVs.
portfolio-analyzer run --no-refresh

# Write to a specific file instead of output/<today>.json.
portfolio-analyzer run --out output/2026-04-24.json

# Refresh NIFTY 500 / NIFTY 50 / sector map without running analysis.
portfolio-analyzer refresh [--force]
```

Output is written to `output/<date>.json` and also printed on stdout. The
schema is documented in `context.md` (see *Output Contract*); each holding
gets a `trend`, `relative_strength`, `drawdown_from_high`, `score`,
`decision`, and `reasons`. `pending_exits` carries the defer queue between
runs, so keep the `output/` directory across invocations.

### Backtest

```bash
portfolio-analyzer backtest --years 5 --capital 1000000 --out output/bt5y.json
portfolio-analyzer backtest --start 2020-01-01 --end 2024-12-31
```

Writes a JSON report (CAGR / max-DD / Sharpe / benchmark comparison) plus
sibling CSVs (`*_equity.csv`, `*_decisions.csv`, `*_fills.csv`, etc.).
Daily OHLC is cached under `data/backtest_cache/` (gitignored).

### TUI

```bash
# Explore one or more backtest runs interactively.
portfolio-analyzer tui --input output/bt5y.json --input output/bt5y_v11.json
```

## Phase 1 — Scanner

All scanner state lives in `data/scanner.db` (SQLite, WAL). Typical
bootstrap:

```bash
# 1. Ingest daily bhavcopies for a window (idempotent per date).
portfolio-analyzer scanner ingest-range --start 2024-01-01 --end 2024-12-31

# 2. Ingest corporate actions and rebuild cumulative adjustments.
portfolio-analyzer scanner ca-ingest --start 2024-01-01 --end 2024-12-31

# 3. Ingest the benchmark index needed for RS.
portfolio-analyzer scanner index-ingest --index NIFTY500 --days 500

# 4. Pull fundamentals from Screener.in (7-day freshness cache).
portfolio-analyzer scanner fundamentals-ingest [--limit 50] [--force]

# 5. Score the universe for a given date.
portfolio-analyzer scanner vcp-scan [--date 2024-12-30]

# 6. Browse candidates in the Textual dashboard.
portfolio-analyzer scanner dash [--date 2024-12-30] [--include-rejects]

# Inspect DB status any time.
portfolio-analyzer scanner status
```

Every scanner command prints a JSON summary on stdout; `ingest` and
`ca-ingest` exit 1 on hard errors, while `no_data` (non-trading days) and
`skipped` (already-ingested dates) are non-error outcomes.

## Project layout

```
src/portfolio_analyzer/
  cli.py, config.py, auth.py, kite_client.py    # entry points + IO
  refresh.py, instruments.py                    # NSE constituent refresh
  market.py, stock_analysis.py, scoring.py      # Phase 0 signal engine
  strategy.py, report.py                        # state machine + JSON shape
  backtest/                                     # Phase 0 backtester
  scanner/                                      # Phase 1 ingestion + VCP
    bhavcopy.py, ingest.py, db.py
    corp_actions.py, ca_ingest.py
    fundamentals/{screener.py, ingest.py}
    vcp/{features.py, fundamentals.py, scorer.py, scan.py}
    index_ingest.py
  tui/                                          # Textual dashboards
data/                                           # CSVs + scanner.db (gitignored bits)
output/                                         # JSON reports + backtest CSVs
tests/                                          # pytest suite
```

Deeper design notes live in `context.md` (stable contract) and
`decisions.md` (decision log). Ongoing work is tracked in `todos.md`.

## Testing

```bash
pytest
```

The suite covers scoring, strategy state machine, market trend, refresh,
backtest components (broker / metrics / simulator / strategy), scanner
ingestion (bhavcopy, corporate actions, fundamentals, VCP), and the TUI
loader.

## Non-goals

Phase 0 is **decisions only** — no order placement, no VCP pattern
detection, no intraday logic, no persistent storage beyond the local
backtest cache. Phase 1 covers NSE equities (`EQ`/`BE` series) only; BSE,
SME, derivatives, rights/merger adjustments, and dividend-adjusted total
returns are out of scope.
