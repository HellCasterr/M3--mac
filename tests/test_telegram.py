from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest
import requests
from fastapi.testclient import TestClient

from trade_m2.app import build_app
from trade_m2.config import Settings, get_settings
from trade_m2.domain import IST
from trade_m2.storage import Store
from trade_m2.telegram import MAX_ATTEMPTS, TelegramNotifier

TOKEN = "123456:fake_token_for_tests"
CHATS = ("111111111", "222222222")


def response(code=200, **body):
    return Mock(status_code=code, json=Mock(return_value=body or {"ok": True}))


@pytest.fixture
def telegram_settings(tmp_path):
    return Settings(
        upstox_api_key="", upstox_api_secret="", upstox_redirect_url="http://localhost/callback",
        host="127.0.0.1", port=8000, database_path=tmp_path / "telegram.db",
        telegram_bot_token=TOKEN, telegram_chat_ids=CHATS,
    )


@pytest.fixture
def post(monkeypatch):
    mock = Mock(return_value=response())
    monkeypatch.setattr("trade_m2.telegram.requests.post", mock)
    return mock


@pytest.fixture
def notifier(telegram_settings, post):
    sender = TelegramNotifier(telegram_settings, Store(telegram_settings.database_path))
    # Exercise lifecycle, then drive delivery deterministically without sleeping in tests.
    sender.start()
    sender.stop()
    return sender


def crossing(sender, stamp=None, side="LONG", source="LIVE"):
    stamp = stamp or datetime.now(IST)
    sender.store.upsert_rule(
        trading_date=stamp.date(), exchange="NSE", tradingsymbol="TEST",
        instrument_token="NSE_EQ|TEST", percentage=Decimal("10"),
        reference_date=stamp.date() - timedelta(days=1), reference_close=Decimal("100"),
    )
    sender.store.evaluate_tick("NSE_EQ|TEST", Decimal("100"), stamp - timedelta(seconds=1))
    events = sender.store.evaluate_tick(
        "NSE_EQ|TEST", Decimal("111" if side == "LONG" else "89"), stamp, source=source,
    )
    return events[0]


def test_env_two_chats_deduplicates_and_supports_legacy_id(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", " 111111111,222222222,111111111, ")
    assert get_settings().telegram_chat_ids == CHATS
    assert get_settings().telegram_error is None
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHATS[0])
    assert get_settings().telegram_chat_ids == (CHATS[0],)
    assert TOKEN not in repr(get_settings())


@pytest.mark.parametrize("overrides", [
    {"telegram_enabled": False}, {"telegram_bot_token": ""}, {"telegram_chat_ids": ()},
    {"telegram_chat_ids": ("@person",)}, {"telegram_chat_ids": ("+919999999999",)},
    {"telegram_chat_ids": ("0",)},
])
def test_invalid_or_disabled_config_never_sends(telegram_settings, post, overrides):
    config = replace(telegram_settings, **overrides)
    sender = TelegramNotifier(config, Store(config.database_path))
    sender.start()
    assert not sender.status()["running"]
    assert sender.status()["error"]
    with pytest.raises(ValueError):
        sender.queue_test()
    assert sender.deliver_one(CHATS[0]) is False
    post.assert_not_called()


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_real_crossing_delivered_to_both_once(notifier, post, side):
    event = crossing(notifier, side=side)
    notifier.enqueue_events()
    notifier.enqueue_events()
    for chat in CHATS:
        assert notifier.deliver_one(chat)
        assert not notifier.deliver_one(chat)
    assert post.call_count == 2
    assert [call.kwargs["json"]["chat_id"] for call in post.call_args_list] == list(CHATS)
    text = post.call_args.kwargs["json"]["text"]
    assert f"Trade M3 | NSE:TEST {side}" in text
    assert f"Alert #{event['id']}" in text
    assert "IST | LIVE" in text and "Indicative stock stop" in text
    assert "upward" in text if side == "LONG" else "downward" in text
    assert "parse_mode" not in post.call_args.kwargs["json"]
    assert "allow_paid_broadcast" not in post.call_args.kwargs["json"]


def test_one_blocked_recipient_does_not_block_other(notifier, post):
    test_id = notifier.queue_test()
    post.side_effect = [response(403, ok=False, description=f"sensitive {TOKEN}"), response()]
    assert notifier.deliver_one(CHATS[0])
    assert notifier.deliver_one(CHATS[1])
    result = notifier.status(test_id)
    assert [r["last_status"] for r in result["recipients"]] == ["failed", "sent"]
    assert "Start" in result["recipients"][0]["last_error"]
    assert TOKEN not in str(result)
    assert CHATS[0] not in str(result)


def test_rate_limit_retries_only_affected_chat_and_honors_new_messages(notifier, post):
    notifier.queue_test()
    now = datetime.now(IST).timestamp() + 1
    post.side_effect = [response(429, ok=False, parameters={"retry_after": 30}), response(),
                        response(), response()]
    assert notifier.deliver_one(CHATS[0], now)
    assert notifier.deliver_one(CHATS[1], now)
    notifier.queue_test()  # Newly queued messages must also respect the cooldown.
    assert not notifier.deliver_one(CHATS[0], now + 5)
    assert notifier.deliver_one(CHATS[0], now + 31)
    assert notifier.deliver_one(CHATS[0], now + 33)
    assert post.call_count == 4


def test_network_error_is_safe_and_pending_delivery_survives_restart(notifier, post):
    crossing(notifier)
    notifier.enqueue_events()
    post.side_effect = [response(), requests.Timeout(f"https://api.telegram.org/bot{TOKEN}")]
    now = datetime.now(IST).timestamp() + 1
    notifier.deliver_one(CHATS[0], now)
    notifier.deliver_one(CHATS[1], now)
    assert TOKEN not in str(notifier.status())
    restarted = TelegramNotifier(notifier.settings, Store(notifier.store.path))
    post.side_effect = None
    post.return_value = response()
    restarted.enqueue_events()
    assert not restarted.deliver_one(CHATS[0], now + 5)
    assert restarted.deliver_one(CHATS[1], now + 5)
    assert post.call_count == 3


def test_retry_attempts_are_bounded(notifier, post):
    test_id = notifier.queue_test()
    post.return_value = response(503, ok=False)
    now = datetime.now(IST).timestamp() + 1
    for offset in (0, 3, 8, 17, 34):
        assert notifier.deliver_one(CHATS[0], now + offset)
    assert not notifier.deliver_one(CHATS[0], now + 100)
    assert post.call_count == MAX_ATTEMPTS
    assert notifier.status(test_id)["recipients"][0]["last_status"] == "failed"


def test_old_recovery_event_expires_and_recent_recovery_is_labeled(notifier, post):
    crossing(notifier, datetime.now(IST) - timedelta(minutes=6), source="RECOVERY")
    notifier.enqueue_events()
    assert not notifier.deliver_one(CHATS[0])
    assert notifier.status()["recipients"][0]["counts"]["expired"] == 1
    post.assert_not_called()
    crossing(notifier, side="SHORT", source="RECOVERY")
    notifier.enqueue_events()
    assert notifier.deliver_one(CHATS[0])
    assert "this is a delayed alert" in post.call_args.kwargs["json"]["text"]


def test_pending_expiry_and_rule_recalculation_cancel_stale_alerts(notifier, post):
    crossing(notifier)
    notifier.enqueue_events()
    later = datetime.now(IST).timestamp() + 301
    assert not notifier.deliver_one(CHATS[0], later)
    now = datetime.now(IST)
    notifier.store.upsert_rule(
        trading_date=now.date(), exchange="NSE", tradingsymbol="TEST",
        instrument_token="NSE_EQ|TEST", percentage=Decimal("9"),
        reference_date=now.date() - timedelta(days=1), reference_close=Decimal("100"),
    )
    assert not notifier.deliver_one(CHATS[1])
    assert notifier.status()["recipients"][1]["counts"]["cancelled"] == 1
    post.assert_not_called()


def test_first_enablement_does_not_replay_old_events(telegram_settings, post):
    sender = TelegramNotifier(telegram_settings, Store(telegram_settings.database_path))
    crossing(sender)
    sender.start()
    sender.stop()
    sender.enqueue_events()
    assert not sender.deliver_one(CHATS[0])
    post.assert_not_called()


def test_delivery_is_claimed_by_only_one_worker(notifier, post):
    notifier.queue_test()
    second = TelegramNotifier(notifier.settings, Store(notifier.store.path))
    now = datetime.now(IST).timestamp() + 1
    assert notifier._claim(CHATS[0], now) is not None
    assert second._claim(CHATS[0], now) is None
    assert second._claim(CHATS[0], now + 31) is not None


def test_test_endpoint_without_upstox_and_no_credential_exposure(telegram_settings, post):
    app = build_app(telegram_settings)
    with TestClient(app) as client:
        assert app.state.telegram.status()["running"]
        app.state.telegram.stop()  # Manually drive workers for deterministic assertions.
        status = client.get("/api/status")
        assert TOKEN not in status.text
        assert CHATS[0] not in status.text
        assert status.json()["authenticated"] is False
        result = client.post("/api/telegram/test", json={})
        assert result.status_code == 202
        test_id = result.json()["test_id"]
        assert result.json()["recipient_count"] == 2
        for chat in CHATS:
            app.state.telegram.deliver_one(chat)
        states = client.get(f"/api/telegram/tests/{test_id}").json()["recipients"]
        assert all(state["last_status"] == "sent" for state in states)
        assert "TELEGRAM TEST" in post.call_args.kwargs["json"]["text"]
        assert app.state.store.latest_event_id() == 0
        assert client.get("/api/telegram/tests/unknown").status_code == 404
        assert client.post("/api/telegram/test", json={}, headers={
            "Origin": "https://unrelated.example",
        }).status_code == 403
        assert client.post("/api/telegram/test").status_code == 415
        assert client.get("/api/telegram/status").status_code == 200
    assert not app.state.telegram.status()["running"]


def test_unconfigured_test_endpoint_gives_setup_error(telegram_settings, post):
    app = build_app(replace(telegram_settings, telegram_bot_token=""))
    with TestClient(app) as client:
        result = client.post("/api/telegram/test", json={})
        assert result.status_code == 400
        assert "TELEGRAM_BOT_TOKEN" in result.json()["detail"]
    post.assert_not_called()
