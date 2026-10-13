from datetime import date
from decimal import Decimal

from trade_m2.services import MarketMovementService, moving_percentage


def test_vix_moving_percentage_matches_existing_formula():
    assert moving_percentage(Decimal("12.990")) == Decimal("1.5203")


def test_market_movement_is_cached():
    class Gateway:
        calls = 0

        def india_vix_previous_close(self, trading_date):
            self.calls += 1
            return date(2026, 9, 17), Decimal("12.990")

    gateway = Gateway()
    service = MarketMovementService(gateway)
    assert service.get(date(2026, 9, 18))["moving_percentage"] == "1.5203"
    service.get(date(2026, 9, 18))
    assert gateway.calls == 1
