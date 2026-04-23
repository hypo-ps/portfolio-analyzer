"""Textual App: analyze an existing backtest JSON + its CSV artifacts.
Launched from the CLI via `python -m portfolio_analyzer tui --input <path>`."""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Container
from textual.widgets import Footer, Header, TabbedContent, TabPane

from portfolio_analyzer.tui.loader import BacktestArtifacts, load
from portfolio_analyzer.tui.views import (
    build_equity, build_fills, build_holdings, build_per_quarter,
    build_per_year, build_refills, build_summary,
)


class BacktestTUI(App):
    """Read-only viewer for a single backtest run."""

    CSS = """
    Screen { layout: vertical; }
    TabbedContent { height: 1fr; }
    #summary-body { padding: 1 2; }
    #year-body { height: 1fr; }
    #year-body DataTable { height: 40%; }
    #year-body PlotextPlot { height: 60%; }
    DataTable { height: 1fr; }
    PlotextPlot { height: 1fr; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("1", "show_tab('summary')", "Summary"),
        ("2", "show_tab('equity')", "Equity"),
        ("3", "show_tab('year')", "Per-Year"),
        ("4", "show_tab('quarter')", "Per-Quarter"),
        ("5", "show_tab('fills')", "Fills"),
        ("6", "show_tab('refills')", "Refills"),
        ("7", "show_tab('holdings')", "Holdings"),
    ]

    def __init__(self, artifacts: BacktestArtifacts) -> None:
        super().__init__()
        self.artifacts = artifacts
        self.title = "Backtest viewer"
        self.sub_title = str(artifacts.path)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with TabbedContent(initial="summary"):
            with TabPane("Summary", id="summary"):
                yield Container(build_summary(self.artifacts))
            with TabPane("Equity", id="equity"):
                yield build_equity(self.artifacts)
            with TabPane("Per-Year", id="year"):
                yield build_per_year(self.artifacts)
            with TabPane("Per-Quarter", id="quarter"):
                yield build_per_quarter(self.artifacts)
            with TabPane("Fills", id="fills"):
                yield build_fills(self.artifacts)
            with TabPane("Refills", id="refills"):
                yield build_refills(self.artifacts)
            with TabPane("Holdings", id="holdings"):
                yield build_holdings(self.artifacts)
        yield Footer()

    def action_show_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id


def run_tui(input_path: Path) -> None:
    """CLI entry point: load artifacts and launch the app."""
    art = load(Path(input_path))
    BacktestTUI(art).run()
