from __future__ import annotations

import logging
from typing import Any

from kiteconnect import KiteConnect

log = logging.getLogger(__name__)


class KiteClient:
    """Thin wrapper around KiteConnect. Used only for holdings fetch."""

    def __init__(self, kite: KiteConnect) -> None:
        self._kite = kite

    def fetch_holdings(self) -> list[dict[str, Any]]:
        return self._kite.holdings()
