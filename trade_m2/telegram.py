"""Server-side Telegram delivery with a durable, independent queue for each chat."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

import requests

from .config import Settings
from .domain import IST, display_price
from .paper import option_summary
from .storage import Store

MAX_AGE_SECONDS = 300
MAX_ATTEMPTS = 5


def mask_chat(chat_id: str) -> str:
    return "…" + chat_id[-4:]


def format_alert(event: dict[str, Any]) -> str:
    point = "A (upper)" if event["boundary"] == "UPPER" else "B (lower)"
    direction = (
        "upward"
        if (
            (event["opening_zone"] == "INSIDE" and event["boundary"] == "UPPER")
            or event["opening_zone"] == "BELOW"
        )
        else "downward"
    )
    when = datetime.fromisoformat(event["event_time"]).astimezone(IST)
    lines = [
        f"Trade M3 | {event['exchange']}:{event['tradingsymbol']} {event['trade_side']}",
        f"Point {point} crossed {direction}",
        f"Level: ₹{display_price(Decimal(event['threshold']))}",
        f"Reference entry: ₹{display_price(Decimal(event['entry_price']))}",
        f"Indicative stock stop: ₹{display_price(Decimal(event['stop_loss']))} "
        f"({Decimal(event['risk_percent']):.2f}% stock risk)",
        f"Opening zone: {event['opening_zone']}",
        f"{when:%d %b %Y, %H:%M:%S} IST | {event['source']} | Alert #{event['id']}",
    ]
    context = event.get("sector_context")
    if context:
        context = json.loads(context) if isinstance(context, str) else context
        lines.append(context.get("summary", "Sector: unavailable"))
    if event["source"] != "LIVE":
        lines.append("Recovered from earlier candles; this is a delayed alert.")
    lines.append(option_summary(event.get("option_context")))
    lines.append("Stock-price signal; not an option-premium stop. No order placed.")
    return "\n".join(lines)


class TelegramDeliveryError(Exception):
    """Only safe, fixed error messages may be surfaced to the dashboard."""

    def __init__(self, message: str, *, retry: bool = False, delay: float = 0):
        super().__init__(message)
        self.retry = retry
        self.delay = delay


class TelegramNotifier:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self.chat_ids = tuple(dict.fromkeys(settings.telegram_chat_ids))
        self.bot_key = hashlib.sha256(settings.telegram_bot_token.encode()).hexdigest()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._worker_error: str | None = None
        with store.connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS telegram_cursor (
                    bot_key TEXT PRIMARY KEY, event_id INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS telegram_cooldowns (
                    bot_key TEXT NOT NULL, chat_id TEXT NOT NULL, until REAL NOT NULL,
                    PRIMARY KEY(bot_key, chat_id)
                );
                CREATE TABLE IF NOT EXISTS telegram_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_key TEXT NOT NULL, message_key TEXT NOT NULL,
                    event_id INTEGER, chat_id TEXT NOT NULL, text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL, expires_at REAL NOT NULL,
                    updated_at REAL NOT NULL, last_error TEXT,
                    UNIQUE(bot_key, message_key, chat_id)
                );
                CREATE INDEX IF NOT EXISTS idx_telegram_pending
                    ON telegram_deliveries(bot_key, chat_id, status, next_attempt);
            """)

    def start(self) -> None:
        if self.settings.telegram_error or any(t.is_alive() for t in self._threads):
            return
        # First enablement starts with new events. A restart resumes the saved cursor.
        with self.store.connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO telegram_cursor VALUES "
                "(?, (SELECT COALESCE(MAX(id), 0) FROM events))",
                (self.bot_key,),
            )
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._run, args=(chat,), name="telegram", daemon=True)
            for chat in self.chat_ids
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=10)

    def _run(self, chat_id: str) -> None:
        while not self._stop.is_set():
            try:
                self.enqueue_events()
                sent = self.deliver_one(chat_id)
                self._worker_error = None
            except Exception:
                # requests exceptions can include the token-bearing URL. Never expose them.
                self._worker_error = "Telegram delivery worker interrupted; retrying."
                sent = False
            interval = 3.1 if chat_id.startswith("-") else 1.1
            self._stop.wait(interval if sent else 0.5)

    def enqueue_events(self, now: float | None = None) -> None:
        if self.settings.telegram_error:
            return
        now = time.time() if now is None else now
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "SELECT event_id FROM telegram_cursor WHERE bot_key=?",
                (self.bot_key,),
            ).fetchone()
            if cursor is None:
                return
            rows = connection.execute(
                """SELECT e.*, r.exchange, r.tradingsymbol FROM events e
                   JOIN rules r ON r.id=e.rule_id WHERE e.id>? ORDER BY e.id LIMIT 500""",
                (cursor["event_id"],),
            ).fetchall()
            processed_id = cursor["event_id"]
            for row in rows:
                if getattr(self.store, "option_enrichment", False) and not row["option_context"]:
                    age = now - datetime.fromisoformat(row["created_at"]).timestamp()
                    if age < 12:
                        break
                processed_id = row["id"]
                expires = datetime.fromisoformat(row["event_time"]).timestamp() + MAX_AGE_SECONDS
                for chat in self.chat_ids:
                    connection.execute(
                        """INSERT OR IGNORE INTO telegram_deliveries
                           (bot_key, message_key, event_id, chat_id, text, status,
                            next_attempt, expires_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            self.bot_key,
                            f"event:{row['id']}",
                            row["id"],
                            chat,
                            format_alert(dict(row)),
                            "pending" if expires > now else "expired",
                            now,
                            expires,
                            now,
                        ),
                    )
            if rows:
                connection.execute(
                    "UPDATE telegram_cursor SET event_id=? WHERE bot_key=?",
                    (processed_id, self.bot_key),
                )

    def queue_test(self) -> str:
        if self.settings.telegram_error:
            raise ValueError(self.settings.telegram_error)
        test_id = "test:" + uuid.uuid4().hex
        now = time.time()
        message = (
            "Trade M3 | TELEGRAM TEST\n"
            "This chat can receive Case 2 LONG / SHORT alerts.\n"
            "Test notification only; no trade signal or order.\n"
            f"{datetime.now(IST):%d %b %Y, %H:%M:%S} IST"
        )
        with self.store.connection() as connection:
            for chat in self.chat_ids:
                connection.execute(
                    """INSERT INTO telegram_deliveries
                       (bot_key, message_key, chat_id, text, next_attempt, expires_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (self.bot_key, test_id, chat, message, now, now + MAX_AGE_SECONDS, now),
                )
        return test_id

    def _claim(self, chat_id: str, now: float) -> sqlite3.Row | None:
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE telegram_deliveries SET status='expired', updated_at=?
                   WHERE bot_key=? AND chat_id=? AND status='pending' AND expires_at<=?""",
                (now, self.bot_key, chat_id, now),
            )
            connection.execute(
                """UPDATE telegram_deliveries SET status='cancelled', updated_at=?
                   WHERE bot_key=? AND chat_id=? AND status='pending' AND event_id IS NOT NULL
                   AND NOT EXISTS
                       (SELECT 1 FROM events WHERE events.id=telegram_deliveries.event_id)""",
                (now, self.bot_key, chat_id),
            )
            cooldown = connection.execute(
                "SELECT until FROM telegram_cooldowns WHERE bot_key=? AND chat_id=?",
                (self.bot_key, chat_id),
            ).fetchone()
            if cooldown and cooldown["until"] > now:
                return None
            row = connection.execute(
                """SELECT * FROM telegram_deliveries
                   WHERE bot_key=? AND chat_id=? AND status='pending' AND next_attempt<=?
                   ORDER BY (event_id IS NULL) DESC, id LIMIT 1""",
                (self.bot_key, chat_id, now),
            ).fetchone()
            if row:
                # A lease prevents simultaneous claims when two local workers overlap.
                connection.execute(
                    "UPDATE telegram_deliveries SET attempts=attempts+1, next_attempt=? WHERE id=?",
                    (now + 30, row["id"]),
                )
            return row

    def deliver_one(self, chat_id: str, now: float | None = None) -> bool:
        if self.settings.telegram_error or chat_id not in self.chat_ids:
            return False
        now = time.time() if now is None else now
        row = self._claim(chat_id, now)
        if row is None:
            return False
        status, error, next_attempt = "sent", None, now
        try:
            self._send(chat_id, row["text"])
        except TelegramDeliveryError as exc:
            error = str(exc)
            attempts = row["attempts"] + 1
            status = "pending" if exc.retry and attempts < MAX_ATTEMPTS else "failed"
            next_attempt = now + max(exc.delay, min(2**attempts, 30))
            if status == "pending" and next_attempt >= row["expires_at"]:
                status = "expired"
            if exc.delay:
                # Rate limiting applies to subsequent messages to this chat as well.
                with self.store.connection() as connection:
                    connection.execute(
                        """INSERT INTO telegram_cooldowns VALUES (?, ?, ?)
                           ON CONFLICT(bot_key, chat_id) DO UPDATE
                           SET until=MAX(until, excluded.until)""",
                        (self.bot_key, chat_id, next_attempt),
                    )
        with self.store.connection() as connection:
            connection.execute(
                """UPDATE telegram_deliveries SET status=?, last_error=?, next_attempt=?,
                   updated_at=? WHERE id=?""",
                (status, error, next_attempt, now, row["id"]),
            )
        return True

    def _send(self, chat_id: str, message: str) -> None:
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "disable_notification": False},
                timeout=(3.05, 5),
                allow_redirects=False,
            )
        except requests.RequestException:
            raise TelegramDeliveryError(
                "Telegram network error. Check this computer's internet connection.",
                retry=True,
            ) from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if response.status_code == 200 and body.get("ok") is True:
            return
        code = body.get("error_code", response.status_code)
        if code == 429:
            parameters = body.get("parameters")
            delay = parameters.get("retry_after", 5) if isinstance(parameters, dict) else 5
            delay = delay if isinstance(delay, (int, float)) and delay > 0 else 5
            raise TelegramDeliveryError(
                "Telegram rate limit; waiting to retry.", retry=True, delay=delay
            )
        if code in {401, 404}:
            raise TelegramDeliveryError("Check TELEGRAM_BOT_TOKEN in .env, then restart.")
        if code == 403:
            raise TelegramDeliveryError("Bot blocked or not started. Open the bot and tap Start.")
        if code == 400:
            raise TelegramDeliveryError("Check this chat ID and tap Start in the bot's chat.")
        raise TelegramDeliveryError("Telegram service error.", retry=True)

    def status(self, test_id: str | None = None) -> dict[str, Any]:
        recipients = []
        with self.store.connection() as connection:
            for chat in self.chat_ids:
                where = "bot_key=? AND chat_id=?"
                params: list[Any] = [self.bot_key, chat]
                if test_id is not None:
                    where += " AND message_key=?"
                    params.append(test_id)
                counts = {
                    row["status"]: row["n"]
                    for row in connection.execute(
                        f"SELECT status, COUNT(*) n FROM telegram_deliveries WHERE {where} "
                        "GROUP BY status",
                        params,
                    ).fetchall()
                }
                last = connection.execute(
                    f"SELECT status, last_error FROM telegram_deliveries WHERE {where} "
                    "ORDER BY updated_at DESC, id DESC LIMIT 1",
                    params,
                ).fetchone()
                recipients.append(
                    {
                        "chat": mask_chat(chat),
                        "counts": counts,
                        "last_status": last["status"] if last else "waiting",
                        "last_error": last["last_error"] if last else None,
                    }
                )
        return {
            "enabled": self.settings.telegram_enabled,
            "configured": self.settings.telegram_error is None,
            "running": bool(self._threads) and all(t.is_alive() for t in self._threads),
            "error": self.settings.telegram_error or self._worker_error,
            "recipients": recipients,
        }
