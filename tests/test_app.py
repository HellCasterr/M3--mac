from pathlib import Path

from fastapi.testclient import TestClient

from trade_m2.app import build_app
from trade_m2.config import Settings


def settings(tmp_path: Path, configured: bool = False) -> Settings:
    return Settings(
        upstox_api_key="key" if configured else "",
        upstox_api_secret="secret" if configured else "",
        upstox_redirect_url="http://127.0.0.1:8000/auth/upstox/callback",
        host="127.0.0.1",
        port=8000,
        database_path=tmp_path / "api.db",
    )


def test_dashboard_and_unauthenticated_status(tmp_path):
    with TestClient(build_app(settings(tmp_path))) as client:
        home = client.get("/")
        assert home.status_code == 200 and 'id="homeTicket"' in home.text
        status = client.get("/api/status").json()
        assert status["configured"] is False
        assert status["authenticated"] is False
        assert status["rule_count"] == 0


def test_load_requires_upstox_login(tmp_path):
    with TestClient(build_app(settings(tmp_path, configured=True))) as client:
        response = client.post("/api/nifty200/load", json={"percentage": "1.5"})
        assert response.status_code == 401


def test_oauth_login_redirect_contains_state(tmp_path):
    with TestClient(build_app(settings(tmp_path, configured=True))) as client:
        response = client.get("/auth/upstox/login", follow_redirects=False)
        assert response.status_code in {302, 307}
        location = response.headers["location"]
        assert "api.upstox.com/v2/login/authorization/dialog" in location
        assert "state=" in location


def test_sector_dashboard_and_history_export(tmp_path):
    with TestClient(build_app(settings(tmp_path))) as client:
        data = client.get("/api/sectors").json()
        assert data["sectors"] == []
        assert "not an NSE sector index" in data["basis"]
        response = client.get("/api/review/export")
        assert response.status_code == 200
        assert "sector_context" in response.text
        assert "attachment" in response.headers["content-disposition"]


def test_paper_routes_and_settings_validation(tmp_path):
    app = build_app(settings(tmp_path))
    with TestClient(app) as client:
        terminal = client.get("/terminal")
        assert terminal.status_code == 200
        assert terminal.headers["cache-control"] == "no-store, max-age=0"
        assert "SIMPLE PAPER V3" in terminal.text
        assert isinstance(client.get("/api/paper").json()["entries_open"], bool)
        assert client.get("/static/terminal.js").headers["cache-control"] == "no-store, max-age=0"
        assert client.get("/paper-guide").status_code == 200
        snapshot = client.get("/api/paper").json()
        assert snapshot["paper_only"] is True and snapshot["settings"]["auto"] is False
        assert "feed" in snapshot
        assert client.post("/api/feed/reconnect", json={}).status_code == 401
        payload = dict(snapshot["settings"])
        payload["stop_percent"] = 7.5
        assert client.put("/api/paper/settings", json=payload).status_code == 200
        assert client.get("/api/paper").json()["settings"]["stop_percent"] == 7.5
        payload["stop_percent"] = 101
        assert client.put("/api/paper/settings", json=payload).status_code == 422
        assert client.post("/api/paper/alerts/999", json={}).status_code == 400
        assert client.get("/api/paper/export").status_code == 200
        assert (
            client.put(
                "/api/paper/settings",
                json=snapshot["settings"],
                headers={"Origin": "https://other.example"},
            ).status_code
            == 403
        )
