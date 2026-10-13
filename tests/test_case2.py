from datetime import datetime, timedelta
from decimal import Decimal

from trade_m2.domain import IST, Candle


def at(trading_date, hour=9, minute=15, second=0):
    return datetime(
        trading_date.year, trading_date.month, trading_date.day,
        hour, minute, second, tzinfo=IST,
    )


def tick(store, trading_date, price, seconds=0):
    return store.evaluate_tick(
        "NSE_EQ|TEST", Decimal(str(price)), at(trading_date, second=seconds)
    )


def test_inside_open_crosses_a_upward_for_immediate_long(store, trading_date):
    assert tick(store, trading_date, 100) == []
    assert tick(store, trading_date, 109, 1) == []
    events = tick(store, trading_date, 110, 2)
    assert [(event["boundary"], event["trade_side"]) for event in events] == [
        ("UPPER", "LONG")
    ]
    assert events[0]["source"] == "LIVE"
    assert Decimal(events[0]["entry_price"]) == Decimal("110")
    assert Decimal(events[0]["stop_loss"]) < Decimal(events[0]["entry_price"])


def test_inside_open_crosses_b_downward_for_immediate_short(store, trading_date):
    tick(store, trading_date, 100)
    tick(store, trading_date, 91, 1)
    events = tick(store, trading_date, 90, 2)
    assert [(event["boundary"], event["trade_side"]) for event in events] == [
        ("LOWER", "SHORT")
    ]
    assert Decimal(events[0]["stop_loss"]) > Decimal(events[0]["entry_price"])


def test_above_a_open_crosses_downward_for_long(store, trading_date):
    tick(store, trading_date, 120)
    tick(store, trading_date, 111, 1)
    events = tick(store, trading_date, 109, 2)
    assert len(events) == 1
    assert events[0]["opening_zone"] == "ABOVE"
    assert events[0]["boundary"] == "UPPER"
    assert events[0]["trade_side"] == "LONG"


def test_below_b_open_crosses_upward_for_short(store, trading_date):
    tick(store, trading_date, 80)
    tick(store, trading_date, 89, 1)
    events = tick(store, trading_date, 91, 2)
    assert len(events) == 1
    assert events[0]["opening_zone"] == "BELOW"
    assert events[0]["boundary"] == "LOWER"
    assert events[0]["trade_side"] == "SHORT"


def test_quote_open_is_initialized_once_before_recovery(store, trading_date):
    assert store.initialize_opening(
        "NSE_EQ|TEST", trading_date, Decimal("120")
    ) is True
    assert store.initialize_opening(
        "NSE_EQ|TEST", trading_date, Decimal("100")
    ) is False
    events = tick(store, trading_date, 109, 2)
    assert len(events) == 1
    assert events[0]["opening_zone"] == "ABOVE"
    assert store.daily_rules(trading_date)[0]["opening_price"] == "120"


def test_gap_open_ignores_the_other_boundary(store, trading_date):
    tick(store, trading_date, 120)
    tick(store, trading_date, 89, 1)
    tick(store, trading_date, 91, 2)
    events = store.events_after(0, 20, trading_date)
    assert [(event["boundary"], event["trade_side"]) for event in events] == [
        ("UPPER", "LONG")
    ]


def test_one_alert_per_boundary_but_both_boundaries_are_allowed(store, trading_date):
    tick(store, trading_date, 100)
    assert len(tick(store, trading_date, 111, 1)) == 1
    assert tick(store, trading_date, 109, 2) == []
    assert tick(store, trading_date, 111, 3) == []
    assert len(tick(store, trading_date, 89, 4)) == 1
    assert tick(store, trading_date, 91, 5) == []
    assert tick(store, trading_date, 89, 6) == []
    events = store.events_after(0, 20, trading_date)
    assert [event["boundary"] for event in events] == ["UPPER", "LOWER"]


def test_exact_boundary_open_waits_for_a_move_away(store, trading_date):
    assert tick(store, trading_date, 110) == []
    assert tick(store, trading_date, 109, 1) == []
    events = tick(store, trading_date, 110, 2)
    assert len(events) == 1
    assert events[0]["opening_zone"] == "INSIDE"


def test_recovery_skips_inside_candle_that_touches_both_levels(store, trading_date):
    start = at(trading_date)
    candle = Candle(
        "NSE_EQ|TEST", start, start + timedelta(minutes=3),
        Decimal("100"), Decimal("111"), Decimal("89"), Decimal("101"),
    )
    assert store.evaluate_recovery_candle(candle) == []
    assert store.events_after(0, 20, trading_date) == []


def test_recovery_can_reconstruct_unambiguous_inside_cross(store, trading_date):
    start = at(trading_date)
    candle = Candle(
        "NSE_EQ|TEST", start, start + timedelta(minutes=3),
        Decimal("100"), Decimal("112"), Decimal("99"), Decimal("111"),
    )
    events = store.evaluate_recovery_candle(candle)
    assert len(events) == 1
    assert events[0]["source"] == "RECOVERY"
    assert events[0]["trade_side"] == "LONG"


def test_unchanged_rule_preserves_state_and_changed_percentage_resets(store, trading_date):
    tick(store, trading_date, 100)
    tick(store, trading_date, 111, 1)
    before = store.daily_rules(trading_date)[0]
    assert before["upper_sent"] is True
    store.upsert_rule(
        trading_date=trading_date, exchange="NSE", tradingsymbol="TEST",
        instrument_token="NSE_EQ|TEST", percentage=Decimal("10"),
        reference_date=trading_date - timedelta(days=1), reference_close=Decimal("100"),
    )
    assert store.daily_rules(trading_date)[0]["upper_sent"] is True
    store.upsert_rule(
        trading_date=trading_date, exchange="NSE", tradingsymbol="TEST",
        instrument_token="NSE_EQ|TEST", percentage=Decimal("9"),
        reference_date=trading_date - timedelta(days=1), reference_close=Decimal("100"),
    )
    reset = store.daily_rules(trading_date)[0]
    assert reset["upper_sent"] is False
    assert reset["opening_price"] is None
    assert store.events_after(0, 20, trading_date) == []
    tick(store, trading_date, 100)
    replacement = tick(store, trading_date, 109, 2)
    assert len(replacement) == 1
    assert replacement[0]["boundary"] == "UPPER"
