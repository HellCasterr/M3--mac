from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from trade_m2 import upstox
from trade_m2.upstox import UpstoxGateway


class QuoteApi:
    payload = {}

    def __init__(self, _client):
        pass

    def get_market_quote_ohlc(self, interval, **kwargs):
        assert interval == "1d"
        assert kwargs["instrument_key"] == "NSE_EQ|ONE,NSE_EQ|STALE"
        return SimpleNamespace(data=self.payload)


def test_session_open_ignores_stale_ohlc(monkeypatch):
    today = date(2026, 9, 21)
    current = int(datetime(2026, 9, 21, 3, 45, tzinfo=UTC).timestamp() * 1000)
    stale = int(datetime(2026, 9, 18, 3, 45, tzinfo=UTC).timestamp() * 1000)
    QuoteApi.payload = {
        "one": {
            "instrument_token": "NSE_EQ|ONE",
            "live_ohlc": {"open": 101.25, "ts": current},
        },
        "stale": {
            "instrument_token": "NSE_EQ|STALE",
            "live_ohlc": {"open": 88.5, "ts": stale},
        },
    }
    monkeypatch.setattr(
        upstox,
        "_sdk",
        lambda: SimpleNamespace(MarketQuoteV3Api=QuoteApi),
    )
    gateway = UpstoxGateway("key", "secret", "callback")
    gateway.api_client = object()
    result = gateway.session_open_prices(
        ["NSE_EQ|ONE", "NSE_EQ|STALE"], today
    )
    assert result == {"NSE_EQ|ONE": Decimal("101.25")}


def test_delayed_last_trade_does_not_generate_a_fresh_alert():
    from trade_m2.upstox import UpstoxMonitor

    calls = []
    monitor = UpstoxMonitor(None, SimpleNamespace())
    monitor._process_tick = lambda *args, **kwargs: calls.append(args)
    now = datetime.now(UTC)
    monitor._on_message({"feeds": {"NSE_EQ|ONE": {"ltpc": {
        "ltp": 102, "ltt": int((now.timestamp() - 300) * 1000)
    }}}})
    assert calls == []
    monitor._on_message({"feeds": {"NSE_EQ|ONE": {"ltpc": {
        "ltp": 102, "ltt": int(now.timestamp() * 1000)
    }}}})
    assert len(calls) == 1


class MinuteQuoteApi:
    payload = {}

    def __init__(self, _client):
        pass

    def get_market_quote_ohlc(self, interval, **kwargs):
        assert interval == "I1"
        assert kwargs["instrument_key"] == "NSE_FO|1,NSE_FO|2"
        return SimpleNamespace(data=self.payload)


def test_option_minute_ohlc_returns_live_and_previous_i1(monkeypatch):
    start = datetime(2026, 9, 30, 4, 5, tzinfo=UTC)
    previous = start - timedelta(minutes=1)
    MinuteQuoteApi.payload = {
        "TEST": {
            "instrument_token": "NSE_FO|1",
            "live_ohlc": {
                "open": 101.5,
                "ts": int(start.timestamp() * 1000),
            },
            "prev_ohlc": {
                "open": 100.25,
                "ts": int(previous.timestamp() * 1000),
            },
        }
    }
    monkeypatch.setattr(
        upstox,
        "_sdk",
        lambda: SimpleNamespace(MarketQuoteV3Api=MinuteQuoteApi),
    )
    gateway = UpstoxGateway("key", "secret", "callback")
    gateway.api_client = object()
    result = gateway.option_minute_ohlc(["NSE_FO|1", "NSE_FO|2"])
    rows = result["NSE_FO|1"]
    assert [row["open"] for row in rows] == [Decimal("101.5"), Decimal("100.25")]
    assert rows[0]["start"].tzinfo is not None


def test_option_full_feed_forwards_i1_open_to_paper():
    from trade_m2.upstox import UpstoxMonitor

    minute_calls = []
    quote_calls = []
    monitor = UpstoxMonitor(None, SimpleNamespace())
    monitor._option_tokens = {"NSE_FO|1"}
    monitor.paper = SimpleNamespace(
        on_minute_open=lambda *args: minute_calls.append(args),
        on_quote=lambda *args: quote_calls.append(args),
    )
    now = datetime.now(UTC)
    minute = now.replace(second=0, microsecond=0)
    monitor._on_message(
        {
            "currentTs": int(now.timestamp() * 1000),
            "feeds": {
                "NSE_FO|1": {
                    "fullFeed": {
                        "marketFF": {
                            "ltpc": {
                                "ltp": 101.75,
                                "ltt": int(now.timestamp() * 1000),
                            },
                            "marketOHLC": {
                                "ohlc": [
                                    {
                                        "interval": "I1",
                                        "open": 101.5,
                                        "ts": int(minute.timestamp() * 1000),
                                    }
                                ]
                            },
                            "marketLevel": {
                                "bidAskQuote": [
                                    {"bidP": 101.4, "askP": 101.8, "bidQ": 10, "askQ": 10}
                                ]
                            },
                        }
                    }
                }
            },
        }
    )
    assert len(minute_calls) == 1
    assert minute_calls[0][0] == "NSE_FO|1"
    assert minute_calls[0][1] == Decimal("101.5")
    assert len(quote_calls) == 1
