"""Widget factories for the scanner (VCP) dashboard TUI."""
from __future__ import annotations

from textual.widgets import DataTable, Static

from portfolio_analyzer.tui.scanner_loader import CandidateRow, DashboardData


def _fmt_pct(x: float | None, *, signed: bool = False) -> str:
    if x is None:
        return "--"
    fmt = f"{x * 100:+.2f}%" if signed else f"{x * 100:.2f}%"
    return fmt


def _fmt_num(x: float | None, nd: int = 2) -> str:
    if x is None:
        return "--"
    return f"{x:.{nd}f}"


def _fmt_price(x: float | None) -> str:
    if x is None:
        return "--"
    return f"{x:,.2f}"


def _fmt_mcap(x: float | None) -> str:
    if x is None:
        return "--"
    if x >= 1e5:
        return f"{x / 1e5:.2f}L"  # ₹ lakh crore
    if x >= 1e3:
        return f"{x / 1e3:.2f}K"
    return f"{x:,.0f}"


CANDIDATE_COLUMNS: tuple[str, ...] = (
    "Symbol", "Decision", "Stage",
    "Close", "Pivot", "Dist%",
    "Final", "VCP", "Tech", "Fund", "Ready",
    "RS50", "Ret50", "Bench50",
    "ROE", "ROCE", "PE", "MCap(Cr)", "Sector", "SecScore",
)


def candidate_row_tuple(row: CandidateRow) -> tuple[str, ...]:
    return (
        row.symbol,
        row.decision,
        row.stage or "--",
        _fmt_price(row.close),
        _fmt_price(row.pivot),
        _fmt_pct(row.distance_to_pivot, signed=True),
        _fmt_num(row.final_score, 3),
        _fmt_num(row.vcp_score, 3),
        _fmt_num(row.technical_score, 3),
        _fmt_num(row.fundamental_score, 3),
        _fmt_num(row.readiness_score, 3),
        _fmt_pct(row.rs_score, signed=True),
        _fmt_pct(row.return_50d, signed=True),
        _fmt_pct(row.benchmark_return_50d, signed=True),
        _fmt_pct(row.roe_latest),
        _fmt_pct(row.roce_latest),
        _fmt_num(row.stock_pe, 1),
        _fmt_mcap(row.market_cap_cr),
        row.sector or "--",
        _fmt_num(row.sector_score, 2),
    )


def build_candidates_table(data: DashboardData) -> DataTable:
    table = DataTable(id="candidates-table", zebra_stripes=True,
                      cursor_type="row")
    table.add_columns(*CANDIDATE_COLUMNS)
    for r in data.rows:
        table.add_row(*candidate_row_tuple(r), key=r.isin)
    return table


def summary_markup(data: DashboardData) -> str:
    if data.trade_date is None:
        return (
            "[b]No VCP scan results in this DB yet.[/]\n"
            "Run [b]scanner vcp-scan[/] first, then re-launch the dashboard."
        )
    counts = data.universe_counts
    buy = counts.get("BUY_ALERT", 0)
    watch = counts.get("WATCHLIST", 0)
    ign = counts.get("IGNORE", 0)
    skp = counts.get("SKIP", 0)
    rej = counts.get("REJECT", 0)
    bench = data.benchmark_return_50d
    bench_str = _fmt_pct(bench, signed=True) if bench is not None else "n/a"
    shown = len(data.rows)
    mode = "+all" if data.include_rejects else "WATCHLIST + BUY_ALERT"
    lines = [
        f"[b]Trade date[/]    {data.trade_date.isoformat()}",
        f"[b]Showing[/]       {shown} rows ({mode})",
        f"[b]Totals[/]        BUY_ALERT={buy}  WATCHLIST={watch}  "
        f"IGNORE={ign}  SKIP={skp}  REJECT={rej}",
        f"[b]Bench ret50[/]   {bench_str}   ([i]NIFTY 500, 50 trading days[/])",
        f"[b]DB[/]            {data.db_path}",
    ]
    return "\n".join(lines)


def build_summary_static(data: DashboardData) -> Static:
    return Static(summary_markup(data), id="summary-body")


def detail_markup(row: CandidateRow | None) -> str:
    if row is None:
        return "[dim]Select a row in the table to see full feature details.[/]"
    reasons = row.reasons or "--"
    lines = [
        f"[b]{row.symbol}[/]  ({row.isin})   "
        f"[{row.decision}/{row.stage or '--'}]",
        "",
        f"Close {_fmt_price(row.close)}   Pivot {_fmt_price(row.pivot)}   "
        f"Dist {_fmt_pct(row.distance_to_pivot, signed=True)}",
        "",
        f"[b]Scores[/]   "
        f"Final {_fmt_num(row.final_score, 3)}   "
        f"VCP {_fmt_num(row.vcp_score, 3)}   "
        f"Tech {_fmt_num(row.technical_score, 3)}   "
        f"Fund {_fmt_num(row.fundamental_score, 3)}   "
        f"Ready {_fmt_num(row.readiness_score, 3)}   "
        f"Combined {_fmt_num(row.combined_score, 3)}",
        "",
        f"[b]RS[/]       "
        f"rs50 {_fmt_pct(row.rs_score, signed=True)}   "
        f"ret50 {_fmt_pct(row.return_50d, signed=True)}   "
        f"bench50 {_fmt_pct(row.benchmark_return_50d, signed=True)}",
        "",
        f"[b]Fundamentals[/]  "
        f"ROE {_fmt_pct(row.roe_latest)}   "
        f"ROCE {_fmt_pct(row.roce_latest)}   "
        f"PE {_fmt_num(row.stock_pe, 1)}   "
        f"MCap {_fmt_mcap(row.market_cap_cr)} Cr   "
        f"Sector {row.sector or '--'}   Industry {row.industry or '--'}   "
        f"SecScore {_fmt_num(row.sector_score, 2)}",
        "",
        f"[b]Reasons[/]   {reasons}",
    ]
    return "\n".join(lines)


def build_detail_static(row: CandidateRow | None) -> Static:
    return Static(detail_markup(row), id="detail-body")
