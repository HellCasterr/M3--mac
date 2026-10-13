from datetime import datetime

from fastapi.testclient import TestClient

from trade_m2 import assistant
from trade_m2 import assistant_quotes as quotes
from trade_m2.app import build_app
from trade_m2.config import Settings
from trade_m2.upstox import UpstoxGateway


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return {"status": "success", "data": self.data}


def gateway():
    client = UpstoxGateway("test-key", "test-secret", "http://127.0.0.1:8000/callback")
    client.api_client = object()
    client.access_token = "fake-token"
    return client


def market_api(url, *, params=None, **kwargs):
    if url.endswith("/v2/instruments/search"):
        if params.get("segments") == "EQ":
            return FakeResponse([{
                "trading_symbol": "GAIL", "segment": "NSE_EQ",
                "instrument_key": "NSE_EQ|INE129A01019",
            }])
        return FakeResponse([{
            "underlying_symbol": "GAIL", "expiry": "2026-10-27",
            "strike_price": 180, "instrument_type": "CE",
            "instrument_key": "NSE_FO|27180", "lot_size": 3150,
            "trading_symbol": "GAIL 180 CE 27 OCT 26",
            "underlying_key": "NSE_EQ|INE129A01019",
        }])
    if url.endswith("/v3/market-quote/quotes"):
        requested = params["instrument_key"].split(",")
        result = {}
        for key in requested:
            result[key.replace("|", ":")] = {
                "instrument_token": key,
                "last_price": 85 if key.startswith("NSE_FO|") else 170,
                "prev_close_price": 90 if key.startswith("NSE_FO|") else 168,
                "last_trade_time": str(int(datetime.now().timestamp()) * 1000),
                "depth": {
                    "buy": [{"price": 83.5}],
                    "sell": [{"price": 86.0}],
                },
            }
        return FakeResponse(result)
    raise AssertionError(url)


def test_equity_lookup_exact_and_index(monkeypatch):
    monkeypatch.setattr(quotes.requests, "get", market_api)
    feed = quotes.PositionQuotes(gateway()).snapshot(
        [{"id": 1, "symbol": "GAIL", "contract": "", "side": "BUY",
          "quantity": 20, "average_price": "160"}],
        question="Check @NIFTY",
    )
    assert feed["connected"] is True
    gail = next(p for p in feed["prices"] if p["symbol"] == "GAIL")
    assert gail["instrument_key"] == "NSE_EQ|INE129A01019"
    assert gail["ltp"] == 170
    assert gail["change_percent"] == 1.19
    assert any(p["symbol"] == "NIFTY" for p in feed["prices"])


def test_options_strict_match_and_estimated_pnl(monkeypatch):
    monkeypatch.setattr(quotes.requests, "get", market_api)
    feed = quotes.PositionQuotes(gateway()).snapshot([
        {"id": 1, "symbol": "GAIL", "contract": "180 CE",
         "expiry": "2026-10-27", "quantity": 1, "average_price": "100",
         "side": "BUY"},
    ])
    item = next(x for x in feed["prices"] if x.get("instrument_kind") == "OPTION")
    assert item["instrument_key"] == "NSE_FO|27180"
    assert item["ltp"] == 85
    assert item["indicative_pnl"] == -15
    assert item["bid"] == 83.5


def test_missing_expiry_never_picks_nearby_option(monkeypatch):
    def do_not_call(*args, **kwargs):
        raise AssertionError("No provider call when expiry missing")

    monkeypatch.setattr(quotes.requests, "get", do_not_call)
    feed = quotes.PositionQuotes(gateway()).snapshot([
        {"id": 1, "symbol": "GAIL", "contract": "180 CE", "expiry": ""}
    ])
    assert not feed["prices"]
    assert feed["unavailable"][0]["status"] == "UNRESOLVED"


def test_no_exact_contract_means_no_price(monkeypatch):
    def wrong_contract(url, *, params=None, **kwargs):
        if url.endswith("/instruments/search"):
            return FakeResponse([{
                "underlying_symbol": "GAIL", "expiry": "2026-10-27",
                "strike_price": 190, "instrument_type": "CE",
                "instrument_key": "NSE_FO|WRONG",
            }])
        raise AssertionError("Quote called for wrong contract")

    monkeypatch.setattr(quotes.requests, "get", wrong_contract)
    result = quotes.PositionQuotes(gateway()).snapshot([
        {"symbol": "GAIL", "contract": "180 CE", "expiry": "2026-10-27"}
    ])
    assert result["prices"] == []
    assert "No substitution" in result["unavailable"][0]["reason"]


def settings(tmp_path):
    return Settings(
        upstox_api_key="", upstox_api_secret="",
        upstox_redirect_url="http://127.0.0.1:8000/auth/upstox/callback",
        host="127.0.0.1", port=8000,
        database_path=tmp_path / "assistant_quote_test.db",
    )


def test_chat_quote_context_and_unconnected_status(tmp_path, monkeypatch):
    monkeypatch.setattr(assistant, "_openai_request", lambda payload: "Quotes unavailable.")
    with TestClient(build_app(settings(tmp_path))) as client:
        result = client.post("/api/assistant/chat", json={
            "message": "Can I exit my holdings?", "history": [],
        })
        assert result.status_code == 200
        assert result.json()["price_data"]["connected"] is False
        assert result.json()["answer"] == "Quotes unavailable."


def test_chat_image_upload_and_reuse_without_saving(tmp_path, monkeypatch):
    captured = []

    def ai(payload):
        captured.append(payload)
        if payload.get("text"):
            return (
                '{"positions":[{"symbol":"GAIL","contract":"180 CE",'
                '"side":"BUY","quantity":5,"average_price":90,'
                '"ltp":80,"expiry":"2026-10-27"}]}'
            )
        return "Image reviewed without trade execution."

    monkeypatch.setattr(assistant, "_openai_request", ai)
    with TestClient(build_app(settings(tmp_path))) as client:
        image = b"\x89PNG\r\n\x1a\n" + b"test payload"
        data = {
            "message": "Should I exit these calls?",
            "history": "[]", "symbols": "[]", "session_positions": "[]",
        }
        response = client.post(
            "/api/assistant/chat-image",
            data=data, files={"file": ("positions.png", image, "image/png")},
            headers={"X-M3-Action": "local"},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["image_review_required"] is True
        assert result["recognized_positions"][0]["symbol"] == "GAIL"
        assert client.get("/api/assistant/positions").json()["positions"] == []
        assert any(
            isinstance(m.get("content"), list)
            and any(p.get("type") == "input_image" for p in m["content"])
            for m in captured[-1]["input"]
        )
        followup = client.post("/api/assistant/chat", json={
            "message": "And now?", "history": [],
            "session_positions": result["recognized_positions"],
        })
        assert followup.status_code == 200


def test_chat_image_requires_local_header(tmp_path):
    with TestClient(build_app(settings(tmp_path))) as client:
        response = client.post("/api/assistant/chat-image", data={"message": "Hi"},
                               headers={"Origin": "https://evil.example"})
        assert response.status_code == 403
