import json
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from trade_m2.domain import IST
from trade_m2.options import select_candidate
from trade_m2.paper import PaperEngine, PaperSettings
from trade_m2.storage import Store


@pytest.fixture
def env(tmp_path):
    clock = SimpleNamespace(now=datetime(2026, 9, 28, 10, 0, tzinfo=IST))
    store = Store(tmp_path / "paper.db")
    gateway = SimpleNamespace(authenticated=True)
    monitor = SimpleNamespace(set_option_tokens=lambda _: None, connected=True)
    engine = PaperEngine(store, gateway, monitor, clock=lambda: clock.now)
    settings = engine.settings()
    settings.update(lots=1, budget=10000, fee_fixed=0, fee_percent=0, slippage_percent=0)
    engine.set_settings(settings)
    candidate = {
        "instrument_key": "NSE_FO|1",
        "symbol": "TEST 100 CE",
        "strike": "100",
        "kind": "CE",
        "expiry": "2026-10-29",
        "lot_size": 10,
        "tick_size": ".05",
        "bid": "99.9",
        "ask": "100",
        "ltp": "100",
        "entry_limit": "100",
        "delta": ".60",
        "reason": "test",
    }

    def decision(eid=1, sector="Banking", symbol="TEST", status="READY"):
        d = {
            "event_id": eid,
            "event_time": clock.now.isoformat(),
            "at": clock.now.isoformat(),
            "symbol": symbol,
            "trade_side": "LONG",
            "candidate": dict(candidate),
            "status": status,
            "manual_preview_at": clock.now.isoformat(),
            "sector_context": {"sector": sector, "alignment": "ALIGNED"},
        }
        with store.connection() as c:
            c.execute("INSERT OR REPLACE INTO paper_decisions VALUES (?,?)", (eid, json.dumps(d)))
        return d

    def quote(bid=99.9, ask=100, seconds=1, bq=100, aq=100, stamp=None):
        clock.now += timedelta(seconds=seconds)
        engine.on_quote(
            "NSE_FO|1", {"bidP": bid, "askP": ask, "bidQ": bq, "askQ": aq}, stamp or clock.now
        )

    def submit(eid=1, overrides=None, automatic=False):
        result = engine.submit(eid, overrides or {}, automatic=automatic)
        if result["state"] == "PENDING":
            entry_at = datetime.fromisoformat(result["entry_at"])
            clock.now = entry_at + timedelta(milliseconds=1)
            opening = Decimal("99" if candidate["ltp"] == "99" else "100")
            engine.on_minute_open("NSE_FO|1", opening, entry_at, clock.now)
            engine.on_quote(
                "NSE_FO|1",
                {
                    "bidP": float(opening),
                    "askP": float(opening + Decimal(".1")),
                    "bidQ": 100,
                    "askQ": 100,
                },
                clock.now,
                clock.now,
                opening,
            )
            result = engine.snapshot()["trades"][0]
        return result

    return SimpleNamespace(
        engine=engine,
        clock=clock,
        store=store,
        decision=decision,
        quote=quote,
        submit=submit,
        candidate=candidate,
        monitor=monitor,
        gateway=gateway,
    )


def trade(e):
    return e.engine.snapshot()["trades"][0]


def test_next_minute_option_tick_sets_entry_and_premium_levels(env):
    env.candidate["ltp"] = "99"
    env.decision()
    t = env.submit()
    assert t["state"] == "OPEN" and t["quantity"] == 10
    assert tuple(map(Decimal, (t["entry"], t["target"], t["stop"]))) == (
        99,
        Decimal("108.90"),
        Decimal("94.05"),
    )
    assert t["entry_basis"] == "UPSTOX_I1_OPEN"
    assert t["entry_quote_at"] == env.clock.now.isoformat()
    snap = env.engine.snapshot()
    assert len(snap["fills"]) == 1 and snap["fills"][0]["side"] == "BUY"
    assert snap["fills"][0]["fee"] == "0.00"
    assert Decimal(snap["account"]["cash"]) == Decimal(env.engine.settings()["initial_cash"]) - 990
    assert env.submit()["id"] == t["id"]
    assert len(env.engine.snapshot()["fills"]) == 1


def test_entry_waits_for_exact_next_minute_option_candle_open(env):
    env.decision()
    pending = env.engine.submit(1, {})
    assert pending["state"] == "PENDING"
    assert pending["entry_at"].endswith("10:01:00+05:30")
    assert env.engine.snapshot()["fills"] == []
    entry_at = datetime.fromisoformat(pending["entry_at"])
    env.clock.now = entry_at + timedelta(seconds=5)
    env.engine.on_quote(
        "NSE_FO|1",
        {"bidP": 101, "askP": 102, "bidQ": 100, "askQ": 100},
        env.clock.now,
        env.clock.now,
        Decimal("101.25"),
    )
    assert trade(env)["state"] == "PENDING"
    env.engine.on_minute_open("NSE_FO|1", Decimal("101.25"), entry_at, env.clock.now)
    assert trade(env)["entry"] == "101.25"
    assert trade(env)["entry_basis"] == "UPSTOX_I1_OPEN"


def test_missing_scheduled_minute_is_not_called_option_expiry(env):
    env.decision()
    pending = env.engine.submit(1, {})
    env.clock.now = datetime.fromisoformat(pending["expires_at"]) + timedelta(milliseconds=1)
    env.engine.advance()
    result = trade(env)
    assert result["state"] == "MISSED_ENTRY"
    assert "I1 candle open" in result["reason"]
    assert env.engine.snapshot()["fills"] == []


def test_rest_i1_fallback_opens_pending_trade(env):
    env.decision()
    pending = env.engine.submit(1, {})
    entry_at = datetime.fromisoformat(pending["entry_at"])
    env.clock.now = entry_at + timedelta(seconds=20)
    env.gateway.option_minute_ohlc = lambda tokens: {
        "NSE_FO|1": [{"start": entry_at, "open": Decimal("101.50")}]
    }
    env.engine.poll_pending_entries()
    result = trade(env)
    assert result["state"] == "OPEN"
    assert result["entry"] == "101.50"
    assert result["entry_basis"] == "UPSTOX_I1_OPEN"


def test_target_and_stop_fill_at_observed_bid_even_with_zero_depth(env):
    env.decision()
    env.submit()
    env.quote(110, 110.1, bq=0)
    t = trade(env)
    assert t["state"] == "CLOSED" and t["exit_reason"] == "TARGET"
    assert Decimal(t["realised"]) == 100
    assert len(env.engine.snapshot()["fills"]) == 2
    assert any(
        o["kind"] == "STOP" and o["state"] == "CANCELLED" for o in env.engine.snapshot()["orders"]
    )


def test_stop_gap_can_lose_more_than_five_percent(env):
    env.decision()
    env.submit()
    env.quote(90, 90.1)
    assert trade(env)["state"] == "CLOSED"
    assert trade(env)["exit_reason"] == "STOP"
    assert Decimal(trade(env)["realised"]) == -100


def test_budget_and_cash_are_real_constraints(env):
    env.decision()
    with pytest.raises(ValueError, match="one whole option lot"):
        env.engine.submit(1, {"budget": 100, "lots": 0})
    env.submit(1, {"budget": 3000, "lots": 0})
    assert trade(env)["quantity"] == 30
    env.decision(2, symbol="OTHER")
    with pytest.raises(ValueError, match="virtual cash"):
        env.engine.submit(2, {"budget": 10000, "lots": 100})


def test_sectors_and_legacy_fee_settings_do_not_block_paper_entry(env):
    s = env.engine.settings()
    s.update(
        max_positions=1,
        max_sector_positions=1,
        daily_loss_limit=1,
        aligned_only=True,
        fee_fixed=20,
        fee_percent=1,
        latency_ms=1000,
        slippage_percent=1,
    )
    env.engine.set_settings(s)
    env.decision()
    env.submit()
    opposed = env.decision(2, symbol="OTHER")
    opposed["sector_context"]["alignment"] = "OPPOSED"
    with env.store.connection() as c:
        c.execute("UPDATE paper_decisions SET payload=? WHERE event_id=2", (json.dumps(opposed),))
    env.submit(2)
    snap = env.engine.snapshot()
    assert len(snap["trades"]) == 2
    assert all(f["fee"] == "0.00" for f in snap["fills"])
    assert all(t["settings"]["latency_ms"] == 0 for t in snap["trades"])


def test_option_quote_fallback_and_unaffordable_lot():
    s = PaperSettings().model_dump()
    contracts = [
        {
            "instrument_key": "100CE",
            "expiry": "2026-10-29",
            "instrument_type": "CE",
            "segment": "NSE_FO",
            "trading_symbol": "100CE",
            "lot_size": 10,
            "tick_size": 5,
        }
    ]
    chain = [
        {
            "strike_price": 100,
            "underlying_spot_price": 100,
            "call_options": {
                "instrument_key": "100CE",
                "market_data": {
                    "bid_price": 90,
                    "ask_price": 100,
                    "ltp": 100,
                    "bid_qty": 0,
                    "ask_qty": 0,
                    "volume": 0,
                    "oi": 0,
                },
                "option_greeks": {},
            },
        }
    ]
    c = select_candidate(contracts, chain, "LONG", "2026-10-29", s)
    assert c["policy"] == "ATM_ITM_QUOTED_FALLBACK" and c["strike"] == "100"
    with pytest.raises(ValueError, match=r"one lot .*₹1,000.00"):
        select_candidate(contracts, chain, "LONG", "2026-10-29", {**s, "budget": 100})


def test_nearby_otm_option_fits_budget_when_atm_does_not():
    settings = {**PaperSettings().model_dump(), "budget": 1000}
    contracts = [
        {
            "instrument_key": key,
            "expiry": "2026-10-29",
            "instrument_type": "CE",
            "segment": "NSE_FO",
            "trading_symbol": key,
            "lot_size": 10,
            "tick_size": 5,
        }
        for key in ("ATM", "OTM")
    ]
    chain = [
        {
            "strike_price": strike,
            "underlying_spot_price": 100,
            "call_options": {
                "instrument_key": key,
                "market_data": {
                    "bid_price": ask - 1,
                    "ask_price": ask,
                    "ltp": ask,
                    "bid_qty": 10,
                    "ask_qty": 10,
                    "volume": 100,
                    "oi": 100,
                },
                "option_greeks": {"delta": delta},
            },
        }
        for strike, key, ask, delta in ((100, "ATM", 150, 0.55), (110, "OTM", 70, 0.35))
    ]
    candidate = select_candidate(contracts, chain, "LONG", "2026-10-29", settings)
    assert candidate["symbol"] == "OTM"
    assert candidate["policy"] == "NEAR_OTM_BUDGET_FALLBACK"


def test_late_live_paper_entry_before_1515(env):
    env.clock.now = env.clock.now.replace(hour=15, minute=13, second=25)
    assert env.engine.snapshot()["entries_open"] is True
    settings = env.engine.settings()
    settings["auto"] = True
    env.engine.set_settings(settings)
    env.decision()
    entry = env.submit(1, automatic=True)
    assert entry["state"] == "OPEN" and entry["mode"] == "AUTO"
    env.clock.now = env.clock.now.replace(hour=15, minute=15)
    assert env.engine.snapshot()["entries_open"] is False
    env.decision(2)
    with pytest.raises(ValueError, match="before 15:15"):
        env.engine.submit(2, {}, automatic=True)


def test_stale_or_out_of_order_option_marks_cannot_close_trade(env):
    env.decision()
    env.submit()
    old = env.clock.now - timedelta(minutes=1)
    env.quote(90, 90.1, stamp=old)
    assert trade(env)["state"] == "OPEN"
    env.quote(99, 100)
    env.quote(90, 90.1, stamp=old)
    assert trade(env)["state"] == "OPEN"


def test_expiry_day_without_exit_quote_is_closed_as_uncertain(env):
    env.candidate["expiry"] = env.clock.now.date().isoformat()
    env.decision()
    env.submit()
    env.clock.now = env.clock.now.replace(hour=15, minute=20)
    env.engine.advance()
    assert trade(env)["state"] == "EXIT_PENDING"
    env.clock.now = env.clock.now.replace(hour=15, minute=30, second=16)
    env.engine.advance()
    result = trade(env)
    assert result["state"] == "CLOSED_UNCERTAIN"
    assert result["remaining"] == 0
    assert result["unresolved_quantity"] == result["quantity"]
    assert result["exit_reason"] == "EXPIRY_NO_QUOTE" and result["uncertain"]
    assert env.engine.snapshot()["account"]["total_pnl"] is None
    env.engine.sync_subscriptions()
    assert not env.engine.synced
    assert [o["state"] for o in env.engine.snapshot()["orders"] if o["kind"] == "SESSION"] == [
        "UNCERTAIN"
    ]


def test_missed_session_exit_does_not_fill_at_next_day_quote(env):
    env.decision()
    env.submit()
    env.clock.now = env.clock.now.replace(hour=15, minute=20)
    env.engine.advance()
    fill_count = len(env.engine.snapshot()["fills"])
    env.clock.now += timedelta(days=1)
    env.engine.advance()
    result = trade(env)
    assert result["state"] == "CLOSED_UNCERTAIN"
    assert result["exit_reason"] == "SESSION_NO_QUOTE"
    assert len(env.engine.snapshot()["fills"]) == fill_count
    assert env.engine.snapshot()["account"]["total_pnl"] is None


def test_expired_candidate_is_rejected_with_clear_reason(env):
    env.candidate["expiry"] = (env.clock.now.date() - timedelta(days=1)).isoformat()
    env.decision()
    with pytest.raises(ValueError, match="Selected option has expired"):
        env.engine.submit(1, {})
    assert env.engine.snapshot()["fills"] == []


def test_disconnected_feed_cannot_create_unmonitored_position(env):
    env.decision()
    env.monitor.connected = False
    with pytest.raises(ValueError, match="Live market feed disconnected"):
        env.engine.submit(1, {})
    assert env.engine.snapshot()["fills"] == []


def test_manual_preview_of_recovered_audit_alert_uses_current_option_quote(env):
    old = env.clock.now - timedelta(minutes=30)
    record = {
        "id": 99,
        "trading_date": env.clock.now.date().isoformat(),
        "event_time": old.isoformat(),
        "created_at": env.clock.now.isoformat(),
        "tradingsymbol": "TEST",
        "trade_side": "LONG",
        "source": "RECOVERY",
        "instrument_token": "NSE_EQ|TEST",
        "sector_context": {"sector": "Banking"},
    }
    with env.store.connection() as c:
        c.execute("INSERT INTO alert_history(payload) VALUES (?)", (json.dumps(record),))
    env.engine.research.select = lambda *args: env.candidate
    preview = env.engine.preview(99)
    assert preview["candidate"]["ltp"] == "100"
    assert preview["source"] == "RECOVERY"
    t = env.submit(99)
    assert t["mode"] == "MANUAL" and t["state"] == "OPEN"
    assert Decimal(t["entry"]) == 100


def test_manual_preview_uses_ticket_budget_for_option_selection(env):
    old = env.clock.now - timedelta(minutes=30)
    record = {
        "id": 98,
        "trading_date": env.clock.now.date().isoformat(),
        "event_time": old.isoformat(),
        "created_at": env.clock.now.isoformat(),
        "tradingsymbol": "TEST",
        "trade_side": "LONG",
        "source": "LIVE",
        "instrument_token": "NSE_EQ|TEST",
        "sector_context": {},
    }
    with env.store.connection() as c:
        c.execute("INSERT INTO alert_history(payload) VALUES (?)", (json.dumps(record),))

    def select(_instrument, _side, _date, settings):
        if settings["budget"] < 12000:
            raise ValueError("Option exceeds per-trade budget")
        return env.candidate

    env.engine.research.select = select
    with pytest.raises(ValueError, match="per-trade budget"):
        env.engine.preview(98)
    preview = env.engine.preview(98, {"budget": 12000, "lots": 0})
    assert preview["status"] == "READY"
    assert preview["settings"]["budget"] == 12000


def test_auto_live_signal_fills_once_and_old_recovery_is_skipped(env):
    s = env.engine.settings()
    s["auto"] = True
    env.engine.set_settings(s)
    env.engine.research.select = lambda *args: env.candidate
    now = env.clock.now
    env.store.upsert_rule(
        trading_date=now.date(),
        exchange="NSE",
        tradingsymbol="TEST",
        instrument_token="NSE_EQ|TEST",
        percentage=Decimal(1),
        reference_date=(now - timedelta(days=1)).date(),
        reference_close=Decimal(100),
    )
    env.store.evaluate_tick("NSE_EQ|TEST", Decimal(100), now)
    env.clock.now += timedelta(seconds=1)
    env.store.evaluate_tick("NSE_EQ|TEST", Decimal(102), env.clock.now)
    with env.store.connection() as c:
        c.execute("UPDATE events SET created_at=?", (env.clock.now.isoformat(),))
    assert env.engine.discover_one()
    pending = trade(env)
    assert pending["state"] == "PENDING"
    entry_at = datetime.fromisoformat(pending["entry_at"])
    env.clock.now = entry_at + timedelta(seconds=1)
    env.engine.on_minute_open("NSE_FO|1", Decimal("100"), entry_at, env.clock.now)
    assert len(env.engine.snapshot()["fills"]) == 1
    assert not env.engine.discover_one()
    assert len(env.engine.snapshot()["fills"]) == 1
    env.engine.sync_subscriptions()
    assert env.engine.synced == {"NSE_FO|1"}
