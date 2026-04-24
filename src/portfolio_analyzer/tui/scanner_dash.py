"""Textual app: VCP scanner dashboard.

Launched via ``python -m portfolio_analyzer scanner dash``.
Reads the latest (or ``--date``) row set from ``vcp_candidates`` joined with
``fundamentals_meta`` and renders:

- a summary header (trade date, per-decision counts, benchmark ret50),
- a sortable table of candidates,
- a detail pane for the selected row (reasons + full feature breakdown).

Key bindings:
- ``q`` quit
- ``s`` sort table by the currently-cursored column
- ``r`` toggle include-all (IGNORE/SKIP/REJECT) and reload
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Footer, Header, Static

from portfolio_analyzer.tui.scanner_loader import (
    CandidateRow, DashboardData, load_dashboard,
)
from portfolio_analyzer.tui.scanner_views import (
    CANDIDATE_COLUMNS, build_detail_static, build_summary_static,
    candidate_row_tuple, detail_markup, summary_markup,
)


class ScannerDashApp(App):
    """Read-only view of the latest VCP scan with drilldown + sort."""

    CSS = """
    Screen { layout: vertical; }
    #summary-body { padding: 1 2; height: 7; }
    #candidates-table { height: 1fr; }
    #detail-body { padding: 1 2; height: 14; border-top: solid $primary; }
    #main-body { height: 1fr; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "toggle_rejects", "Toggle all"),
        ("s", "sort_cursor", "Sort by column"),
    ]

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        trade_date: dt.date | None = None,
        include_rejects: bool = False,
    ) -> None:
        super().__init__()
        self._db_path = db_path
        self._trade_date = trade_date
        self._include_rejects = include_rejects
        self._data: DashboardData = load_dashboard(
            db_path=db_path, trade_date=trade_date,
            include_rejects=include_rejects,
        )
        self.title = "VCP Scanner Dashboard"
        td = self._data.trade_date
        self.sub_title = (
            f"{td.isoformat()} | {len(self._data.rows)} rows"
            if td else "no scans yet"
        )

    def _rows_by_key(self) -> dict[str, CandidateRow]:
        return {r.isin: r for r in self._data.rows}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="main-body"):
            yield build_summary_static(self._data)
            table = DataTable(
                id="candidates-table", zebra_stripes=True, cursor_type="row",
            )
            yield table
            yield build_detail_static(None)
        yield Footer()

    def on_mount(self) -> None:
        self._populate_table()

    def _populate_table(self) -> None:
        table = self.query_one("#candidates-table", DataTable)
        table.clear(columns=True)
        table.add_columns(*CANDIDATE_COLUMNS)
        for r in self._data.rows:
            table.add_row(*candidate_row_tuple(r), key=r.isin)

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted,
    ) -> None:
        self._refresh_detail(event.row_key)

    def on_data_table_row_selected(
        self, event: DataTable.RowSelected,
    ) -> None:
        self._refresh_detail(event.row_key)

    def _refresh_detail(self, row_key) -> None:
        key_value = row_key.value if row_key is not None else None
        row = self._rows_by_key().get(str(key_value) if key_value else "")
        self.query_one("#detail-body", Static).update(detail_markup(row))

    def action_sort_cursor(self) -> None:
        """Sort the candidates table by the column at the current cursor."""
        table = self.query_one("#candidates-table", DataTable)
        col_idx = table.cursor_column
        if col_idx is None or col_idx < 0:
            return
        try:
            col_key = table.ordered_columns[col_idx].key
        except Exception:
            return
        table.sort(col_key)

    def action_toggle_rejects(self) -> None:
        self._include_rejects = not self._include_rejects
        self._reload()

    def _reload(self) -> None:
        self._data = load_dashboard(
            db_path=self._db_path, trade_date=self._trade_date,
            include_rejects=self._include_rejects,
        )
        self.query_one("#summary-body", Static).update(summary_markup(self._data))
        self._populate_table()
        self.query_one("#detail-body", Static).update(detail_markup(None))
        td = self._data.trade_date
        self.sub_title = (
            f"{td.isoformat()} | {len(self._data.rows)} rows"
            if td else "no scans yet"
        )


def run_scanner_dash(
    *, db_path: Path | None = None, trade_date: dt.date | None = None,
    include_rejects: bool = False,
) -> None:
    """CLI entry point."""
    ScannerDashApp(
        db_path=db_path, trade_date=trade_date, include_rejects=include_rejects,
    ).run()
