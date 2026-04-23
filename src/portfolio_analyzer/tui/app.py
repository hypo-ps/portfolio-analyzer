"""Textual App: analyze one or more backtest JSON reports + their CSV artifacts.
Launched from the CLI via `python -m portfolio_analyzer tui --input <path>`.
When more than one `--input` is supplied, a leading "Compare" tab is added and
the single-run tabs operate on the first artifact set."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from textual.app import App, ComposeResult
from textual.containers import Container
from textual.widgets import Footer, Header, TabbedContent, TabPane

from portfolio_analyzer.tui.loader import BacktestArtifacts, load
from portfolio_analyzer.tui.views import (
    build_compare, build_equity, build_fills, build_holdings, build_per_quarter,
    build_per_year, build_refills, build_summary,
)


class BacktestTUI(App):
    """Read-only viewer for one or more backtest runs."""

    CSS = """
    Screen { layout: vertical; }
    TabbedContent { height: 1fr; }
    #summary-body { padding: 1 2; }
    #year-body { height: 1fr; }
    #year-body DataTable { height: 40%; }
    #year-body PlotextPlot { height: 60%; }
    #compare-body { height: 1fr; }
    #compare-body DataTable { height: 34%; }
    #compare-body PlotextPlot { height: 33%; }
    DataTable { height: 1fr; }
    PlotextPlot { height: 1fr; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("c", "show_tab('compare')", "Compare"),
        ("1", "show_tab('summary')", "Summary"),
        ("2", "show_tab('equity')", "Equity"),
        ("3", "show_tab('year')", "Per-Year"),
        ("4", "show_tab('quarter')", "Per-Quarter"),
        ("5", "show_tab('fills')", "Fills"),
        ("6", "show_tab('refills')", "Refills"),
        ("7", "show_tab('holdings')", "Holdings"),
    ]

    def __init__(self, artifacts: Sequence[BacktestArtifacts]) -> None:
        super().__init__()
        if not artifacts:
            raise ValueError("BacktestTUI requires at least one artifact set")
        self.artifacts_list = list(artifacts)
        self.primary = self.artifacts_list[0]
        self.title = "Backtest viewer" + (f" ({len(self.artifacts_list)} runs)"
                                          if len(self.artifacts_list) > 1 else "")
        self.sub_title = ", ".join(str(a.path) for a in self.artifacts_list)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        initial = "compare" if len(self.artifacts_list) > 1 else "summary"
        with TabbedContent(initial=initial):
            if len(self.artifacts_list) > 1:
                with TabPane("Compare", id="compare"):
                    yield build_compare(self.artifacts_list)
            with TabPane("Summary", id="summary"):
                yield Container(build_summary(self.primary))
            with TabPane("Equity", id="equity"):
                yield build_equity(self.primary)
            with TabPane("Per-Year", id="year"):
                yield build_per_year(self.primary)
            with TabPane("Per-Quarter", id="quarter"):
                yield build_per_quarter(self.primary)
            with TabPane("Fills", id="fills"):
                yield build_fills(self.primary)
            with TabPane("Refills", id="refills"):
                yield build_refills(self.primary)
            with TabPane("Holdings", id="holdings"):
                yield build_holdings(self.primary)
        yield Footer()

    def action_show_tab(self, tab_id: str) -> None:
        if tab_id == "compare" and len(self.artifacts_list) <= 1:
            return
        self.query_one(TabbedContent).active = tab_id


def run_tui(input_paths: Sequence[Path]) -> None:
    """CLI entry point: load each artifact set and launch the app."""
    arts = [load(Path(p)) for p in input_paths]
    BacktestTUI(arts).run()
