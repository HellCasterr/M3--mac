import csv
import io
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient

from trade_m2.app import build_app
from trade_m2.config import Settings
from trade_m2.domain import IST
from trade_m2.replay import ReplayJobs, ReplayRequest, evaluate, parse_alerts
from trade_m2.storage import Store


def bar(minute, opening, high, low, close):
    return {
        "timestamp": f"2026-09-29T10:{minute:02d}:00+05:30",
        "open": str(opening),
        "high": str(high),
        "low": str(low),
        "close": str(close),
    }


def test_next_minute_budget_and_ambiguous_exit():
    bars = [bar(2, 200, 300, 100, 200), bar(3, 100, 111, 94, 102)]
    result = evaluate(bars, "2026-09-29T10:02:15+05:30", Decimal(1000), 5, Decimal(10), Decimal(5))
    assert result["status"] == "STOP" and result["uncertain"]
    assert result["entry_price"] == "100" and Decimal(result["pnl"]) == -50
    assert (
        evaluate(bars, "2026-09-29T10:02:15+05:30", Decimal(499), 5, Decimal(10), Decimal(5))[
            "status"
        ]
        == "OVER_BUDGET"
    )
    assert (
        evaluate(bars, "2026-09-29T10:03:15+05:30", Decimal(1000), 5, Decimal(10), Decimal(5))[
            "status"
        ]
        == "NO_ENTRY_CANDLE"
    )


def test_csv_and_nearest_contract_flow(tmp_path):
    store = Store(tmp_path / "replay.db")
    gateway = SimpleNamespace(
        authenticated=True, resolve_instrument=lambda *a: {"instrument_token": "NSE_EQ|TEST"}
    )
    calls = []

    def contracts(key, expiry):
        calls.append((key, expiry))
        return {
            "contracts": [
                {
                    "instrument_type": "CE",
                    "segment": "NSE_FO",
                    "instrument_key": "NSE_FO|A",
                    "strike_price": 100,
                    "lot_size": 10,
                    "trading_symbol": "TEST 100 CE",
                },
                {
                    "instrument_type": "CE",
                    "segment": "NSE_FO",
                    "instrument_key": "NSE_FO|B",
                    "strike_price": 110,
                    "lot_size": 10,
                    "trading_symbol": "TEST 110 CE",
                },
            ]
        }

    def candles(q):
        calls.append(q)
        return {"candles": [bar(1, 12, 12, 12, 12), bar(3, 10, 10, 10, 10), bar(4, 10, 11, 10, 11)]}

    history = SimpleNamespace(
        clock=lambda: datetime(2026, 9, 29, 18, tzinfo=IST), contracts=contracts, candles=candles
    )
    jobs = ReplayJobs(store, gateway, history)
    raw = io.StringIO()
    writer = csv.DictWriter(
        raw, fieldnames=["id", "tradingsymbol", "trade_side", "event_time", "entry_price"]
    )
    writer.writeheader()
    writer.writerow(
        {
            "id": 75,
            "tradingsymbol": "TEST",
            "trade_side": "LONG",
            "event_time": "2026-09-29T10:02:15+05:30",
            "entry_price": "108",
        }
    )
    alerts = parse_alerts(raw.getvalue())
    request = ReplayRequest(
        csv_text=raw.getvalue(),
        expiry=date(2026, 9, 29),
        budget=Decimal(100),
        target_percent=Decimal(10),
        stop_percent=Decimal(5),
    )
    row = jobs._one(alerts[0], request, {}, {}, {})
    assert row["strike"] == "110" and row["status"] == "TARGET"
    assert row["lots"] == 1 and Decimal(row["pnl"]) == 10
    assert any(q.get("expired") is False for q in calls if isinstance(q, dict))


def test_route_validation(tmp_path):
    app = build_app(
        Settings(
            upstox_api_key="",
            upstox_api_secret="",
            upstox_redirect_url="",
            host="127.0.0.1",
            port=8000,
            database_path=tmp_path / "app.db",
        )
    )
    with TestClient(app) as client:
        assert client.get("/replay").status_code == 200
        assert client.get("/api/replay/current").status_code == 404
        assert client.get("/api/replay/missing").status_code == 404
        assert (
            client.post(
                "/api/replay",
                json={
                    "csv_text": "tradingsymbol,trade_side,event_time\n"
                    "TEST,LONG,2026-09-29T10:02:15+05:30",
                    "expiry": "2026-09-29",
                    "budget": 1000,
                },
            ).status_code
            == 400
        )



def test_current_replay_prefers_running_job_and_survives_page_refresh(tmp_path):
    store = Store(tmp_path / "current.db")
    history = SimpleNamespace(clock=lambda: datetime(2026, 9, 30, 12, tzinfo=IST))
    jobs = ReplayJobs(store, SimpleNamespace(authenticated=True), history)
    jobs.jobs["done"] = {
        "id": "done",
        "state": "DONE",
        "total": 1,
        "processed": 1,
        "rows": [{"status": "TARGET", "pnl": "10"}],
        "expiry": "2026-10-27",
        "error": None,
    }
    jobs.jobs["running"] = {
        "id": "running",
        "state": "RUNNING",
        "total": 84,
        "processed": 23,
        "rows": [{"status": "TARGET", "pnl": "10"}],
        "expiry": "2026-10-27",
        "error": None,
    }
    current = jobs.current()
    assert current["id"] == "running"
    assert current["processed"] == 23
    assert current["counts"] == {"TARGET": 1}
    assert current["total_pnl"] == "10"


def test_current_replay_returns_latest_completed_job_when_idle(tmp_path):
    store = Store(tmp_path / "latest.db")
    history = SimpleNamespace(clock=lambda: datetime(2026, 9, 30, 12, tzinfo=IST))
    jobs = ReplayJobs(store, SimpleNamespace(authenticated=True), history)
    jobs.jobs["first"] = {
        "id": "first",
        "state": "DONE",
        "total": 1,
        "processed": 1,
        "rows": [],
        "expiry": "2026-10-27",
        "error": None,
    }
    jobs.jobs["latest"] = {
        "id": "latest",
        "state": "DONE",
        "total": 2,
        "processed": 2,
        "rows": [],
        "expiry": "2026-10-27",
        "error": None,
    }
    assert jobs.current()["id"] == "latest"


def test_exact_minute_entry_and_sparse_option_candles():
    bars = [
        bar(2, 10, 10.4, 9.8, 10),
        bar(5, 10.2, 11.2, 10, 11),
    ]
    result = evaluate(
        bars,
        "2026-09-29T10:02:00+05:30",
        Decimal(1000),
        10,
        Decimal(10),
        Decimal(5),
    )
    assert result["entry_time"].endswith("10:02:00+05:30")
    assert result["entry_price"] == "10"
    assert result["status"] == "TARGET"
    assert result["history_gaps"] is True


def test_replay_falls_back_to_nearest_strike_with_traded_history(tmp_path):
    store = Store(tmp_path / "fallback.db")
    gateway = SimpleNamespace(
        authenticated=True, resolve_instrument=lambda *a: {"instrument_token": "NSE_EQ|TEST"}
    )

    def contracts(key, expiry):
        return {
            "contracts": [
                {
                    "instrument_type": "CE",
                    "segment": "NSE_FO",
                    "instrument_key": "NSE_FO|EMPTY",
                    "strike_price": 100,
                    "lot_size": 10,
                    "trading_symbol": "TEST 100 CE",
                },
                {
                    "instrument_type": "CE",
                    "segment": "NSE_FO",
                    "instrument_key": "NSE_FO|TRADED",
                    "strike_price": 110,
                    "lot_size": 10,
                    "trading_symbol": "TEST 110 CE",
                },
            ]
        }

    def candles(q):
        if q["instrument_key"] == "NSE_FO|EMPTY":
            return {"candles": []}
        return {
            "candles": [
                bar(3, 10, 10.5, 9.8, 10),
                bar(4, 10, 11.2, 9.9, 11),
            ]
        }

    history = SimpleNamespace(
        clock=lambda: datetime(2026, 9, 29, 18, tzinfo=IST),
        contracts=contracts,
        candles=candles,
    )
    jobs = ReplayJobs(store, gateway, history)
    raw = (
        "id,tradingsymbol,trade_side,event_time,entry_price\n"
        "1,TEST,LONG,2026-09-29T10:02:15+05:30,101\n"
    )
    alert = parse_alerts(raw)[0]
    request = ReplayRequest(
        csv_text=raw,
        expiry=date(2026, 9, 29),
        budget=Decimal(1000),
        target_percent=Decimal(10),
        stop_percent=Decimal(5),
    )
    row = jobs._one(alert, request, {}, {}, {})
    assert row["strike"] == "110"
    assert row["strike_rank"] == 2
    assert row["contracts_tested"] == 2
    assert row["status"] == "TARGET"
