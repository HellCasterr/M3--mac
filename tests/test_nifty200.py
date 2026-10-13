import json

import pytest
import requests

from trade_m2.nifty200 import Nifty200Service


class Response:
    def __init__(self, text):
        self.content = text.encode()

    def raise_for_status(self):
        return None


def csv_text(count=200):
    return "Company Name,Industry,Symbol\n" + "\n".join(
        f"Company {index},Industry,SYM{index}" for index in range(count)
    )


def test_downloads_and_caches_exactly_200(monkeypatch, tmp_path):
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: Response(csv_text()))
    cache = tmp_path / "nifty200.json"
    result = Nifty200Service(cache).get()
    assert len(result.symbols) == 200
    assert result.source == "NSE Indices"
    assert len(json.loads(cache.read_text())["symbols"]) == 200


def test_download_rejects_wrong_constituent_count(monkeypatch, tmp_path):
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: Response(csv_text(199)))
    service = Nifty200Service(tmp_path / "missing.json")
    service.packaged_path = tmp_path / "also-missing.json"
    with pytest.raises(RuntimeError, match="no valid fallback"):
        service.get()


def test_uses_last_valid_cache_on_download_failure(monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise requests.ConnectionError("offline")

    cache = tmp_path / "nifty200.json"
    cache.write_text(json.dumps({"symbols": [f"SYM{i}" for i in range(200)]}))
    monkeypatch.setattr(requests, "get", fail)
    result = Nifty200Service(cache).get()
    assert result.source == "local cache"
    assert len(result.symbols) == 200
