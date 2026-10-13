from datetime import date, datetime
from decimal import Decimal

from trade_m2.domain import IST, CandleAggregator, calculate_levels, opening_zone


def test_levels_are_exact_decimals():
    upper, lower = calculate_levels(Decimal("365.20"), Decimal("1.43"))
    assert upper == Decimal("370.42236")
    assert lower == Decimal("359.97764")


def test_opening_zones_include_exact_boundaries():
    upper, lower = Decimal("110"), Decimal("90")
    assert opening_zone(Decimal("111"), upper, lower) == "ABOVE"
    assert opening_zone(Decimal("110"), upper, lower) == "ON_UPPER"
    assert opening_zone(Decimal("100"), upper, lower) == "INSIDE"
    assert opening_zone(Decimal("90"), upper, lower) == "ON_LOWER"
    assert opening_zone(Decimal("89"), upper, lower) == "BELOW"


def test_candle_aggregator_aligns_to_nse_open():
    aggregator = CandleAggregator()
    day = date(2026, 9, 18)
    first = datetime(day.year, day.month, day.day, 9, 16, 12, tzinfo=IST)
    second = datetime(day.year, day.month, day.day, 9, 18, 0, tzinfo=IST)
    assert aggregator.add_tick("token", Decimal("100"), first) == []
    completed = aggregator.add_tick("token", Decimal("102"), second)
    assert len(completed) == 1
    assert completed[0].start.minute == 15
    assert completed[0].open == Decimal("100")
    assert completed[0].close == Decimal("100")
