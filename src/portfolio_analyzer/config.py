from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"

MA_SHORT = 50
MA_LONG = 200
RETURN_WINDOW = 50
HIGH_52W_WINDOW = 252
HISTORY_DAYS_FETCH = 260

NIFTY500_WEIGHT = 0.70
NIFTY50_WEIGHT = 0.30
BLEND_UP_THRESHOLD = 0.5
BLEND_DOWN_THRESHOLD = -0.5

BREADTH_STRONG = 0.65
BREADTH_WEAK = 0.40

NEAR_HIGH_DRAWDOWN = -0.10
LARGE_DRAWDOWN = -0.25

HOLD_SCORE_MIN = 4
REDUCE_SCORE_MIN = 2

EXIT_GATE_DRAWDOWN = -0.15
EXPOSURE_FLOOR = 0.50
REENTRY_ALLOCATION_FRACTION = 0.50
REARM_MAX_WEIGHT_PER_STOCK = 0.10
REFILL_ALLOCATION_FRACTION = 0.05
REFILL_STOP_EXPOSURE = 0.55
REFILL_EXTERNAL_EXPOSURE_CAP = 0.35
REFILL_TOP_K = 15

TRANSACTION_COST_BPS = 0.001
SLIPPAGE_BPS = 0.00075

NIFTY500_YF_SYMBOL = "^CRSLDX"
NIFTY50_YF_SYMBOL = "^NSEI"
YF_BATCH_SIZE = 50

TOP_N_PERFORMERS = 3

NIFTY500_CSV_URL = "https://niftyindices.com/IndexConstituent/ind_nifty500list.csv"
NIFTY50_CSV_URL = "https://niftyindices.com/IndexConstituent/ind_nifty50list.csv"
REFRESH_HTTP_TIMEOUT = 30
REFRESH_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class Credentials:
    api_key: str
    api_secret: str


def load_credentials() -> Credentials:
    load_dotenv(PROJECT_ROOT / ".env")
    api_key = os.environ.get("KITE_API_KEY", "").strip()
    api_secret = os.environ.get("KITE_API_SECRET", "").strip()
    if not api_key or not api_secret:
        raise RuntimeError(
            "Missing KITE_API_KEY / KITE_API_SECRET. Copy .env.example to .env and fill in."
        )
    return Credentials(api_key=api_key, api_secret=api_secret)
