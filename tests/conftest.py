from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from trade_m2.storage import Store


@pytest.fixture
def trading_date() -> date:
    return date(2026, 9, 18)


@pytest.fixture
def store(tmp_path, trading_date) -> Store:
    result = Store(tmp_path / "test.db")
    result.upsert_rule(
        trading_date=trading_date,
        exchange="NSE",
        tradingsymbol="TEST",
        instrument_token="NSE_EQ|TEST",
        percentage=Decimal("10"),
        reference_date=date(2026, 9, 17),
        reference_close=Decimal("100"),
    )
    return result
