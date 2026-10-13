from pathlib import Path

from fastapi.testclient import TestClient

from trade_m2 import assistant
from trade_m2.app import build_app
from trade_m2.config import Settings


def local_settings(tmp_path: Path) -> Settings:
    return Settings(
        upstox_api_key="",
        upstox_api_secret="",
        upstox_redirect_url="http://127.0.0.1:8000/auth/upstox/callback",
        host="127.0.0.1",
        port=8000,
        database_path=tmp_path / "assistant.db",
    )


def test_assistant_page_and_unconnected_market(tmp_path):
    with TestClient(build_app(local_settings(tmp_path))) as client:
        page = client.get("/assistant")
        assert page.status_code == 200
        assert "My positions" in page.text
        assert page.headers["cache-control"] == "no-store, max-age=0"
        snapshot = client.get("/api/assistant/indices").json()
        assert snapshot["connected"] is False
        assert snapshot["indices"] == []


def test_reviewed_positions_persist_and_clear(tmp_path):
    with TestClient(build_app(local_settings(tmp_path))) as client:
        submitted = {
            "positions": [{
                "symbol": "NIFTY",
                "contract": "24000 CE",
                "side": "BUY",
                "quantity": 65,
                "average_price": "121.50",
                "ltp": None,
                "expiry": "2026-10-29",
            }]
        }
        response = client.post("/api/assistant/positions", json=submitted)
        assert response.status_code == 200
        rows = client.get("/api/assistant/positions").json()["positions"]
        assert len(rows) == 1 and rows[0]["quantity"] == 65
        assert rows[0]["average_price"] == "121.50"
        assert client.post(
            "/api/assistant/positions", json=submitted,
            headers={"Origin": "https://attacker.example"},
        ).status_code == 403
        assert client.delete("/api/assistant/positions").status_code == 403
        assert client.delete(
            "/api/assistant/positions", headers={"X-M3-Action": "local"}
        ).status_code == 200
        assert client.get("/api/assistant/positions").json()["positions"] == []


def test_screenshot_requires_key_and_explicit_save(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    png = b"\x89PNG\r\n\x1a\n" + b"fake-image"
    with TestClient(build_app(local_settings(tmp_path))) as client:
        response = client.post(
            "/api/assistant/extract", files={"file": ("position.png", png, "image/png")},
            headers={"X-M3-Action": "local"},
        )
        assert response.status_code == 503
        assert not client.get("/api/assistant/positions").json()["positions"]
        monkeypatch.setattr(
            assistant, "_openai_request",
            lambda payload: (
                '{"positions":[{"symbol":"BANKNIFTY","contract":"55000 PE",'
                '"side":"BUY","quantity":30,"average_price":200,"ltp":220,'
                '"expiry":"2026-10-29"}]}'
            ),
        )
        response = client.post(
            "/api/assistant/extract", files={"file": ("position.png", png, "image/png")},
            headers={"X-M3-Action": "local"},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["needs_review"] is True and result["saved"] is False
        assert len(result["positions"]) == 1
        assert client.get("/api/assistant/positions").json()["positions"] == []


def test_chat_no_key_returns_configuration_error(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with TestClient(build_app(local_settings(tmp_path))) as client:
        response = client.post(
            "/api/assistant/chat", json={"message": "How is Nifty?", "history": []}
        )
        assert response.status_code == 503


def test_index_quote_math_uses_broker_close(tmp_path, monkeypatch):
    app = build_app(local_settings(tmp_path))
    app.state.gateway.access_token = "fake-token"
    app.state.gateway.api_client = object()

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "data": {
                    "NSE_INDEX:Nifty 50": {
                        "instrument_token": "NSE_INDEX|Nifty 50",
                        "last_price": 25000,
                        "cp": 24800,
                        "ltt": "1791891000000",
                    }
                }
            }

    monkeypatch.setattr(assistant.requests, "get", lambda *args, **kwargs: Response())
    with TestClient(app) as client:
        response = client.get("/api/assistant/indices")
        assert response.status_code == 200
        indices = response.json()["indices"]
        assert len(indices) == 1
        assert indices[0]["change"] == 200
        assert indices[0]["change_percent"] == 0.81
