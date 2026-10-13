import csv
import io
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from trade_m2.app import build_app
from trade_m2.config import Settings
from trade_m2.domain import IST
from trade_m2.history import HistoricalData, HistoryError
from trade_m2.storage import Store


@pytest.fixture
def history(tmp_path):
    store = Store(tmp_path / "history.db")
    gateway = SimpleNamespace(authenticated=True, access_token="not-a-real-token")
    clock = SimpleNamespace(now=datetime(2026, 9, 28, 10, 5, tzinfo=IST))
    h = HistoricalData(store, gateway, clock=lambda: clock.now)
    return h, clock


def query(**changes):
    return {
        "instrument_key": "NSE_FO|123",
        "from_date": "2026-09-25",
        "to_date": "2026-09-25",
        "interval": "1minute",
        **changes,
    }


def candle(stamp="2026-09-25T10:00:00+05:30"):
    return [stamp, 100, 110, 95, 105, 500, 1000]


def test_normalizes_sorts_and_caches_offline(history):
    h, clock = history
    calls = []

    def fetch(path, params=None):
        calls.append(path)
        return {"candles": [candle("2026-09-25T10:01:00+05:30"), candle(), candle(), ["bad"]]}

    h._get = fetch
    result = h.candles(query())
    assert result["count"] == 2 and result["discarded_rows"] == 2
    assert result["candles"][0]["timestamp"].endswith("10:00:00+05:30")
    assert "NSE_FO%7C123/minutes/1/2026-09-25/2026-09-25" in calls[0]
    h.gateway.authenticated = False
    assert h.candles(query())["cache_hit"] is True
    assert len(calls) == 1
    clock.now += timedelta(days=2)
    h.candles(query())
    assert len(calls) == 1  # Prior-day candles stay available locally.


def test_chunks_long_requests_and_separates_today(history):
    h, clock = history
    paths = []
    h._get = lambda path, params=None: paths.append(path) or {"candles": []}
    result = h.candles(query(from_date="2026-08-01", to_date="2026-09-28"))
    assert result["request_chunks"] == 4
    assert paths[-1] == "/v3/historical-candle/intraday/NSE_FO%7C123/minutes/1"
    assert result["status"] == "EMPTY"


def test_discards_running_future_invalid_and_naive_candles(history):
    h, _ = history
    h._get = lambda *args: {
        "candles": [
            candle("2026-09-28T10:04:00+05:30"),
            candle("2026-09-28T10:05:00+05:30"),
            candle("2026-09-28T10:00:00"),
            ["2026-09-28T10:01:00+05:30", 100, 90, 95, 105, 0, 0],
        ]
    }
    result = h.candles(query(from_date="2026-09-28", to_date="2026-09-28"))
    assert result["count"] == 1 and result["discarded_rows"] == 3


def test_expired_key_and_endpoint(history):
    h, _ = history
    paths = []
    h._get = lambda path, params=None: paths.append(path) or {"candles": [candle()]}
    result = h.candles(query(instrument_key="NSE_FO|123|25-09-2026", expired=True))
    assert result["expired"]
    assert (
        "/v2/expired-instruments/historical-candle/NSE_FO%7C123%7C25-09-2026/1minute/" in paths[0]
    )
    with pytest.raises(ValueError):
        h.candles(query(expired=True))
    with pytest.raises(ValueError):
        h.candles(query(instrument_key="NSE_FO|123|25-09-2026", expired=True, to_date="2026-09-26"))


def test_validation_and_no_partial_cache(history):
    h, _ = history
    for changes in [
        {"to_date": "2026-09-24"},
        {"from_date": "2026-01-01"},
        {"to_date": "2026-09-29"},
        {"instrument_key": "http://evil"},
        {"interval": "2minute"},
    ]:
        with pytest.raises(ValueError):
            h.candles(query(**changes))
    calls = []

    def fail_second(path, params=None):
        calls.append(path)
        if len(calls) == 2:
            raise HistoryError("RATE_LIMITED", "Retry later", 429)
        return {"candles": [candle()]}

    h._get = fail_second
    with pytest.raises(HistoryError):
        h.candles(query(from_date="2026-08-01"))
    with h.store.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM historical_cache").fetchone()[0] == 0


def test_plus_error_and_authentication(history, monkeypatch):
    import requests

    h, _ = history
    response = SimpleNamespace(
        status_code=403, ok=False, json=lambda: {"errors": [{"errorCode": "UDAPI1149"}]}
    )
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: response)
    with pytest.raises(HistoryError) as exc:
        h.candles(query())
    assert exc.value.code == "PLUS_REQUIRED"
    h.gateway.authenticated = False
    with pytest.raises(HistoryError) as exc:
        h.candles(query())
    assert exc.value.status == 401


def test_contract_discovery_uses_expired_route(history):
    h, _ = history
    calls = []
    h._get = lambda path, params=None: calls.append((path, params)) or []
    result = h.contracts("NSE_EQ|TEST", date(2026, 9, 25))
    assert result["expired"]
    assert calls[0][0] == "/v2/expired-instruments/option/contract"
    h.contracts("NSE_EQ|TEST", date(2026, 10, 29))
    assert calls[1][0] == "/v2/option/contract"


def test_local_dates_filter_sort_and_export(tmp_path):
    settings = Settings(
        upstox_api_key="",
        upstox_api_secret="",
        upstox_redirect_url="",
        host="127.0.0.1",
        port=8000,
        database_path=tmp_path / "app.db",
    )
    app = build_app(settings)
    with app.state.store.connection() as c:
        for eid, day, hour in [(1, "2026-09-24", 10), (2, "2026-09-25", 11), (3, "2026-09-25", 9)]:
            v = {
                "id": eid,
                "trading_date": day,
                "event_time": f"{day}T{hour:02d}:00:00+05:30",
                "tradingsymbol": "TEST" if eid != 3 else "=FORMULA()",
                "trade_side": "LONG",
                "entry_price": "100",
                "source": "LIVE",
                "sector_context": json.dumps({"sector": "Banking", "alignment": "ALIGNED"}),
            }
            c.execute("INSERT INTO alert_history(payload) VALUES (?)", (json.dumps(v),))
        c.execute(
            "INSERT INTO paper_decisions VALUES (?,?)",
            (
                2,
                json.dumps(
                    {
                        "status": "READY",
                        "candidate": {
                            "strike": "100",
                            "kind": "CE",
                            "expiry": "2026-10-29",
                            "lot_size": 10,
                        },
                    }
                ),
            ),
        )
    with TestClient(app) as client:
        assert client.get("/alerts-history").status_code == 200
        assert client.get("/api/events/snapshot").json()["events"] == []
        assert b"data-paper-alert" in client.get("/static/app.js").content
        dates = client.get("/api/review/dates").json()["dates"]
        assert dates == [{"date": "2026-09-25", "count": 2}, {"date": "2026-09-24", "count": 1}]
        result = client.get("/api/review/alerts?trading_date=2026-09-25&order=asc").json()
        assert [v["id"] for v in result["alerts"]] == [3, 2]
        assert result["alerts"][1]["option_context"]["candidate"]["kind"] == "CE"
        exported = client.get("/api/review/export?trading_date=2026-09-25&order=asc")
        assert "2026-09-25.csv" in exported.headers["content-disposition"]
        rows = list(csv.DictReader(io.StringIO(exported.text.lstrip("\ufeff"))))
        assert len(rows) == 2 and all(r["trading_date"] == "2026-09-25" for r in rows)
        assert rows[0]["tradingsymbol"] == "'=FORMULA()"
        assert rows[1]["option_strike"] == "100"
        assert client.get("/api/review/alerts?trading_date=2026-09-26").json()["count"] == 0
        assert client.get("/api/review/alerts?trading_date=bad").status_code == 422
        assert client.post("/api/history/candles", json=query()).status_code == 401
        assert (
            client.post("/api/history/candles", json=query(interval="invalid")).status_code == 422
        )
