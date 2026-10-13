from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from trade_m2.domain import IST
from trade_m2.sectors import SectorMonitor


def monitor():
    rules = [
        {"tradingsymbol": f"B{i}", "instrument_token": str(i), "active": True} for i in range(5)
    ]
    universe = SimpleNamespace(
        industries={r["tradingsymbol"]: "Banking" for r in rules}, _value=None
    )
    return SectorMonitor(SimpleNamespace(daily_rules=lambda _: rules), universe)


def test_breadth_staleness_coverage_and_future_ticks():
    m = monitor()
    t = datetime(2026, 9, 28, 10, tzinfo=IST)
    for i in range(5):
        m.tick(str(i), Decimal(101), 100, t)
    row = m.dashboard(t)["sectors"][0]
    assert row["bias"] == "BULLISH" and row["mean_percent"] == 1
    assert m.dashboard(t + timedelta(seconds=46))["sectors"][0]["bias"] == "UNAVAILABLE"
    assert m.dashboard(t - timedelta(seconds=1))["sectors"][0]["fresh"] == 0
    m.tick("0", Decimal(90), 100, t - timedelta(seconds=1))
    assert m.dashboard(t)["sectors"][0]["mean_percent"] == 1
    for i in range(2):
        m.tick(str(i), Decimal(101), 100, t + timedelta(seconds=60))
    assert m.dashboard(t + timedelta(seconds=60))["sectors"][0]["bias"] == "UNAVAILABLE"


def test_alignment_and_recovery_never_uses_present_context():
    m = monitor()
    t = datetime(2026, 9, 28, 10, tzinfo=IST)
    for i in range(5):
        m.tick(str(i), Decimal(101), 100, t)
    with patch("trade_m2.sectors.datetime") as clock:
        clock.now.return_value = t
        assert m.context("B0", "LONG", t, "LIVE")["alignment"] == "ALIGNED"
        assert m.context("B0", "SHORT", t, "LIVE")["alignment"] == "OPPOSED"
        assert m.context("B0", "LONG", t, "RECOVERY")["alignment"] == "UNAVAILABLE"
        assert (
            m.context("B0", "LONG", t - timedelta(minutes=5), "LIVE")["alignment"] == "UNAVAILABLE"
        )


def test_bad_close_and_session_reset():
    m = monitor()
    t = datetime(2026, 9, 28, 10, tzinfo=IST)
    for bad in [None, 0, -1, "NaN"]:
        m.tick("0", Decimal(101), bad, t)
    assert not m.quotes
    m.tick("0", Decimal(101), 100, t)
    assert m.dashboard(t + timedelta(days=1))["sectors"][0]["fresh"] == 0


def test_snapshot_is_durable_and_audit_survives_rule_reset(store, trading_date):
    import json

    from trade_m2.telegram import format_alert

    store.sector_context = lambda *args: {"summary": "Banking +1.00% ALIGNED"}
    t = datetime.combine(trading_date, datetime.min.time(), tzinfo=IST).replace(hour=10)
    store.evaluate_tick("NSE_EQ|TEST", Decimal(100), t)
    event = store.evaluate_tick("NSE_EQ|TEST", Decimal(111), t + timedelta(seconds=1))[0]
    assert "Banking +1.00% ALIGNED" in format_alert(event)
    store.sector_context = lambda *args: {"summary": "CHANGED"}
    saved = store.events_after(0, 10, trading_date)[0]
    assert json.loads(saved["sector_context"])["summary"] == "Banking +1.00% ALIGNED"
    store.upsert_rule(
        trading_date=trading_date,
        exchange="NSE",
        tradingsymbol="TEST",
        instrument_token="NSE_EQ|TEST",
        percentage=Decimal(9),
        reference_date=trading_date - timedelta(days=1),
        reference_close=Decimal(100),
    )
    assert not store.events_after(0, 10, trading_date)
    with store.connection() as connection:
        history = connection.execute("SELECT payload FROM alert_history").fetchall()
    assert len(history) == 1
    assert json.loads(history[0]["payload"])["percentage"] == "10"
