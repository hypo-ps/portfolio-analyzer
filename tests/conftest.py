from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def _series(values: list[float]) -> pd.Series:
    idx = pd.date_range(start="2020-01-01", periods=len(values), freq="B")
    return pd.Series(values, index=idx, dtype="float64")


@pytest.fixture
def series_factory():
    return _series


@pytest.fixture
def linear_uptrend():
    return _series([100 + i * 0.5 for i in range(260)])


@pytest.fixture
def linear_downtrend():
    return _series([200 - i * 0.5 for i in range(260)])


@pytest.fixture
def sideways_series():
    rng = np.random.default_rng(42)
    base = np.full(260, 100.0) + rng.normal(0, 0.5, 260)
    return _series(base.tolist())
