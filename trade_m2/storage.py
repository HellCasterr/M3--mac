from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from .domain import Candle, calculate_levels, display_price, ensure_ist, opening_zone
from .risk import protective_stop


class Store:
    def __init__(self, path: Path) -> None:
        self.sector_context = None
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=20000")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trading_date TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    tradingsymbol TEXT NOT NULL,
                    instrument_token TEXT NOT NULL,
                    percentage TEXT NOT NULL,
                    reference_date TEXT NOT NULL,
                    reference_close TEXT NOT NULL,
                    upper_level TEXT NOT NULL,
                    lower_level TEXT NOT NULL,
                    opening_price TEXT,
                    opening_zone TEXT,
                    last_price TEXT,
                    upper_sent INTEGER NOT NULL DEFAULT 0,
                    lower_sent INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    UNIQUE(trading_date, instrument_token)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_id INTEGER NOT NULL,
                    boundary TEXT NOT NULL,
                    trade_side TEXT NOT NULL,
                    opening_zone TEXT NOT NULL,
                    previous_price TEXT NOT NULL,
                    current_price TEXT NOT NULL,
                    threshold TEXT NOT NULL,
                    entry_price TEXT NOT NULL,
                    stop_loss TEXT NOT NULL,
                    risk_percent TEXT NOT NULL,
                    stop_method TEXT NOT NULL,
                    source TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(rule_id, boundary),
                    FOREIGN KEY(rule_id) REFERENCES rules(id)
                );
                CREATE TABLE IF NOT EXISTS alert_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS market_candles (
                    instrument_token TEXT NOT NULL,
                    candle_start TEXT NOT NULL,
                    candle_end TEXT NOT NULL,
                    candle_open TEXT NOT NULL,
                    candle_high TEXT NOT NULL,
                    candle_low TEXT NOT NULL,
                    candle_close TEXT NOT NULL,
                    PRIMARY KEY(instrument_token, candle_start)
                );
                CREATE INDEX IF NOT EXISTS idx_rules_date_active
                    ON rules(trading_date, active);
                CREATE INDEX IF NOT EXISTS idx_events_time ON events(event_time);
                """
            )

            columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
            if "sector_context" not in columns:
                connection.execute("ALTER TABLE events ADD COLUMN sector_context TEXT")

    def upsert_rule(
        self,
        *,
        trading_date: date,
        exchange: str,
        tradingsymbol: str,
        instrument_token: str,
        percentage: Decimal,
        reference_date: date,
        reference_close: Decimal,
    ) -> dict[str, Any]:
        upper, lower = calculate_levels(reference_close, percentage)
        now = datetime.now(UTC).isoformat()
        with self._lock, self.connection() as connection:
            existing = connection.execute(
                """SELECT id, percentage, reference_date, reference_close
                   FROM rules WHERE trading_date=? AND instrument_token=?""",
                (trading_date.isoformat(), instrument_token),
            ).fetchone()
            configuration_changed = bool(
                existing
                and (
                    existing["percentage"] != str(percentage)
                    or existing["reference_date"] != reference_date.isoformat()
                    or existing["reference_close"] != str(reference_close)
                )
            )
            connection.execute(
                """
                INSERT INTO rules (
                    trading_date, exchange, tradingsymbol, instrument_token,
                    percentage, reference_date, reference_close, upper_level,
                    lower_level, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trading_date, instrument_token) DO UPDATE SET
                    exchange=excluded.exchange,
                    tradingsymbol=excluded.tradingsymbol,
                    active=1,
                    percentage=excluded.percentage,
                    reference_date=excluded.reference_date,
                    reference_close=excluded.reference_close,
                    upper_level=excluded.upper_level,
                    lower_level=excluded.lower_level,
                    opening_price=CASE WHEN rules.percentage=excluded.percentage
                        AND rules.reference_date=excluded.reference_date
                        AND rules.reference_close=excluded.reference_close
                        THEN rules.opening_price ELSE NULL END,
                    opening_zone=CASE WHEN rules.percentage=excluded.percentage
                        AND rules.reference_date=excluded.reference_date
                        AND rules.reference_close=excluded.reference_close
                        THEN rules.opening_zone ELSE NULL END,
                    last_price=CASE WHEN rules.percentage=excluded.percentage
                        AND rules.reference_date=excluded.reference_date
                        AND rules.reference_close=excluded.reference_close
                        THEN rules.last_price ELSE NULL END,
                    upper_sent=CASE WHEN rules.percentage=excluded.percentage
                        AND rules.reference_date=excluded.reference_date
                        AND rules.reference_close=excluded.reference_close
                        THEN rules.upper_sent ELSE 0 END,
                    lower_sent=CASE WHEN rules.percentage=excluded.percentage
                        AND rules.reference_date=excluded.reference_date
                        AND rules.reference_close=excluded.reference_close
                        THEN rules.lower_sent ELSE 0 END
                """,
                (
                    trading_date.isoformat(),
                    exchange,
                    tradingsymbol,
                    instrument_token,
                    str(percentage),
                    reference_date.isoformat(),
                    str(reference_close),
                    str(upper),
                    str(lower),
                    now,
                ),
            )
            if configuration_changed and existing is not None:
                connection.execute("DELETE FROM events WHERE rule_id=?", (existing["id"],))
            row = connection.execute(
                "SELECT * FROM rules WHERE trading_date=? AND instrument_token=?",
                (trading_date.isoformat(), instrument_token),
            ).fetchone()
        return self._rule_dict(row)

    def save_candles(self, candles: list[Candle]) -> int:
        if not candles:
            return 0
        with self._lock, self.connection() as connection:
            for candle in candles:
                connection.execute(
                    """
                    INSERT INTO market_candles VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(instrument_token, candle_start) DO UPDATE SET
                        candle_end=excluded.candle_end,
                        candle_open=excluded.candle_open,
                        candle_high=excluded.candle_high,
                        candle_low=excluded.candle_low,
                        candle_close=excluded.candle_close
                    """,
                    (
                        candle.instrument_token,
                        ensure_ist(candle.start).isoformat(),
                        ensure_ist(candle.end).isoformat(),
                        str(candle.open),
                        str(candle.high),
                        str(candle.low),
                        str(candle.close),
                    ),
                )
        return len(candles)

    def initialize_opening(self, token: str, trading_date: date, price: Decimal) -> bool:
        """Persist the true session open once without replaying it as a crossing."""
        with self._lock, self.connection() as connection:
            rows = connection.execute(
                """SELECT id, upper_level, lower_level FROM rules
                   WHERE trading_date=? AND instrument_token=? AND active=1
                   AND opening_price IS NULL""",
                (trading_date.isoformat(), token),
            ).fetchall()
            for row in rows:
                zone = opening_zone(price, Decimal(row["upper_level"]), Decimal(row["lower_level"]))
                connection.execute(
                    """UPDATE rules SET opening_price=?, opening_zone=?, last_price=?
                       WHERE id=?""",
                    (str(price), zone, str(price), row["id"]),
                )
        return bool(rows)

    @staticmethod
    def _resolved_boundary_zone(zone: str, price: Decimal, upper: Decimal, lower: Decimal) -> str:
        if zone == "ON_UPPER" and price != upper:
            return "ABOVE" if price > upper else "INSIDE"
        if zone == "ON_LOWER" and price != lower:
            return "BELOW" if price < lower else "INSIDE"
        return zone

    @staticmethod
    def _crossings(
        zone: str,
        previous: Decimal,
        current: Decimal,
        upper: Decimal,
        lower: Decimal,
        upper_sent: bool,
        lower_sent: bool,
    ) -> list[tuple[str, str, Decimal]]:
        result: list[tuple[str, str, Decimal]] = []
        if zone == "INSIDE":
            if not upper_sent and previous < upper <= current:
                result.append(("UPPER", "LONG", upper))
            if not lower_sent and previous > lower >= current:
                result.append(("LOWER", "SHORT", lower))
        elif zone == "ABOVE" and not upper_sent and previous > upper >= current:
            result.append(("UPPER", "LONG", upper))
        elif zone == "BELOW" and not lower_sent and previous < lower <= current:
            result.append(("LOWER", "SHORT", lower))
        return result

    def _recent_candles(
        self, connection: sqlite3.Connection, token: str, before: datetime
    ) -> list[Candle]:
        rows = connection.execute(
            """
            SELECT * FROM market_candles
            WHERE instrument_token=? AND candle_end<=? AND candle_end>=?
            ORDER BY candle_start DESC LIMIT 30
            """,
            (
                token,
                ensure_ist(before).isoformat(),
                (ensure_ist(before) - timedelta(days=5)).isoformat(),
            ),
        ).fetchall()
        return [
            Candle(
                token,
                datetime.fromisoformat(row["candle_start"]),
                datetime.fromisoformat(row["candle_end"]),
                Decimal(row["candle_open"]),
                Decimal(row["candle_high"]),
                Decimal(row["candle_low"]),
                Decimal(row["candle_close"]),
            )
            for row in reversed(rows)
        ]

    def evaluate_tick(
        self,
        token: str,
        price: Decimal,
        timestamp: datetime,
        *,
        source: str = "LIVE",
    ) -> list[dict[str, Any]]:
        timestamp = ensure_ist(timestamp)
        created: list[dict[str, Any]] = []
        with self._lock, self.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM rules WHERE trading_date=? AND instrument_token=?
                   AND active=1""",
                (timestamp.date().isoformat(), token),
            ).fetchall()
            for row in rows:
                upper = Decimal(row["upper_level"])
                lower = Decimal(row["lower_level"])
                if row["opening_price"] is None:
                    zone = opening_zone(price, upper, lower)
                    connection.execute(
                        """UPDATE rules SET opening_price=?, opening_zone=?, last_price=?
                           WHERE id=?""",
                        (str(price), zone, str(price), row["id"]),
                    )
                    continue
                zone = self._resolved_boundary_zone(str(row["opening_zone"]), price, upper, lower)
                if zone != row["opening_zone"]:
                    connection.execute(
                        "UPDATE rules SET opening_zone=?, last_price=? WHERE id=?",
                        (zone, str(price), row["id"]),
                    )
                    continue
                previous = Decimal(row["last_price"] or row["opening_price"])
                crossings = self._crossings(
                    zone,
                    previous,
                    price,
                    upper,
                    lower,
                    bool(row["upper_sent"]),
                    bool(row["lower_sent"]),
                )
                for boundary, side, threshold in crossings:
                    event = self._insert_event(
                        connection,
                        row,
                        boundary,
                        side,
                        zone,
                        previous,
                        price,
                        threshold,
                        timestamp,
                        source,
                    )
                    if event is not None:
                        created.append(event)
                connection.execute(
                    "UPDATE rules SET last_price=? WHERE id=?", (str(price), row["id"])
                )
        return created

    def evaluate_recovery_candle(self, candle: Candle) -> list[dict[str, Any]]:
        timestamp = ensure_ist(candle.end)
        created: list[dict[str, Any]] = []
        with self._lock, self.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM rules WHERE trading_date=? AND instrument_token=?
                   AND active=1""",
                (timestamp.date().isoformat(), candle.instrument_token),
            ).fetchall()
            for row in rows:
                upper = Decimal(row["upper_level"])
                lower = Decimal(row["lower_level"])
                if row["opening_price"] is None:
                    zone = opening_zone(candle.open, upper, lower)
                    connection.execute(
                        """UPDATE rules SET opening_price=?, opening_zone=?, last_price=?
                           WHERE id=?""",
                        (str(candle.open), zone, str(candle.open), row["id"]),
                    )
                    row = connection.execute(
                        "SELECT * FROM rules WHERE id=?", (row["id"],)
                    ).fetchone()
                zone = self._resolved_boundary_zone(
                    str(row["opening_zone"]), candle.close, upper, lower
                )
                previous = Decimal(row["last_price"] or row["opening_price"])
                if zone == "INSIDE":
                    upper_hit = not bool(row["upper_sent"]) and previous < upper <= candle.high
                    lower_hit = not bool(row["lower_sent"]) and previous > lower >= candle.low
                    if upper_hit and lower_hit:
                        candidates: list[tuple[str, str, Decimal]] = []
                    elif upper_hit:
                        candidates = [("UPPER", "LONG", upper)]
                    elif lower_hit:
                        candidates = [("LOWER", "SHORT", lower)]
                    else:
                        candidates = []
                elif zone == "ABOVE":
                    candidates = (
                        [("UPPER", "LONG", upper)]
                        if not bool(row["upper_sent"]) and previous > upper >= candle.low
                        else []
                    )
                elif zone == "BELOW":
                    candidates = (
                        [("LOWER", "SHORT", lower)]
                        if not bool(row["lower_sent"]) and previous < lower <= candle.high
                        else []
                    )
                else:
                    candidates = []
                for boundary, side, threshold in candidates:
                    event = self._insert_event(
                        connection,
                        row,
                        boundary,
                        side,
                        zone,
                        previous,
                        threshold,
                        threshold,
                        timestamp,
                        "RECOVERY",
                    )
                    if event is not None:
                        created.append(event)
                connection.execute(
                    "UPDATE rules SET opening_zone=?, last_price=? WHERE id=?",
                    (zone, str(candle.close), row["id"]),
                )
            connection.execute(
                """
                INSERT INTO market_candles VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instrument_token, candle_start) DO UPDATE SET
                    candle_end=excluded.candle_end,
                    candle_open=excluded.candle_open,
                    candle_high=excluded.candle_high,
                    candle_low=excluded.candle_low,
                    candle_close=excluded.candle_close
                """,
                (
                    candle.instrument_token,
                    ensure_ist(candle.start).isoformat(),
                    ensure_ist(candle.end).isoformat(),
                    str(candle.open),
                    str(candle.high),
                    str(candle.low),
                    str(candle.close),
                ),
            )
        return created

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        boundary: str,
        side: str,
        zone: str,
        previous: Decimal,
        current: Decimal,
        threshold: Decimal,
        timestamp: datetime,
        source: str,
    ) -> dict[str, Any] | None:
        history = self._recent_candles(connection, str(row["instrument_token"]), timestamp)
        stop = protective_stop(history, current, side)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        context = (
            self.sector_context(row["tradingsymbol"], side, timestamp, source)
            if self.sector_context
            else {"summary": "Sector: unavailable"}
        )
        try:
            cursor = connection.execute(
                """
                INSERT INTO events (
                    rule_id, boundary, trade_side, opening_zone, previous_price,
                    current_price, threshold, entry_price, stop_loss, risk_percent,
                    stop_method, source, event_time, created_at, sector_context
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    boundary,
                    side,
                    zone,
                    str(previous),
                    str(current),
                    str(threshold),
                    str(current),
                    str(stop.price),
                    str(stop.risk_percent),
                    stop.method,
                    source,
                    ensure_ist(timestamp).isoformat(),
                    now,
                    json.dumps(context),
                ),
            )
        except sqlite3.IntegrityError:
            return None
        audit = {
            "id": int(cursor.lastrowid),
            "trading_date": row["trading_date"],
            "event_time": ensure_ist(timestamp).isoformat(),
            "created_at": now,
            "tradingsymbol": row["tradingsymbol"],
            "trade_side": side,
            "entry_price": str(current),
            "source": source,
            "sector_context": json.dumps(context),
            "percentage": row["percentage"],
            "reference_close": row["reference_close"],
            "threshold": str(threshold),
            "stop_loss": str(stop.price),
            "risk_percent": str(stop.risk_percent),
            "boundary": boundary,
            "opening_zone": zone,
            "instrument_token": row["instrument_token"],
            "exchange": row["exchange"],
        }
        connection.execute("INSERT INTO alert_history(payload) VALUES (?)", (json.dumps(audit),))
        column = "upper_sent" if boundary == "UPPER" else "lower_sent"
        connection.execute(f"UPDATE rules SET {column}=1 WHERE id=?", (row["id"],))
        return {
            "id": int(cursor.lastrowid),
            "rule_id": int(row["id"]),
            "exchange": row["exchange"],
            "tradingsymbol": row["tradingsymbol"],
            "boundary": boundary,
            "trade_side": side,
            "opening_zone": zone,
            "previous_price": str(previous),
            "current_price": str(current),
            "threshold": str(threshold),
            "threshold_display": display_price(threshold),
            "entry_price": str(current),
            "entry_price_display": display_price(current),
            "stop_loss": str(stop.price),
            "stop_loss_display": display_price(stop.price),
            "risk_percent": str(stop.risk_percent),
            "risk_percent_display": self._display_percent(stop.risk_percent),
            "stop_method": stop.method,
            "source": source,
            "event_time": ensure_ist(timestamp).isoformat(),
            "created_at": now,
            "sector_context": json.dumps(context),
        }

    def daily_rules(self, trading_date: date) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM rules WHERE trading_date=? ORDER BY tradingsymbol",
                (trading_date.isoformat(),),
            ).fetchall()
        return [self._rule_dict(row) for row in rows]

    def active_rules(self, trading_date: date) -> list[dict[str, Any]]:
        return [rule for rule in self.daily_rules(trading_date) if rule["active"]]

    def set_all_active(self, trading_date: date, active: bool) -> int:
        with self._lock, self.connection() as connection:
            cursor = connection.execute(
                "UPDATE rules SET active=? WHERE trading_date=?",
                (int(active), trading_date.isoformat()),
            )
            return cursor.rowcount

    def set_rule_active(self, rule_id: int, active: bool) -> bool:
        with self._lock, self.connection() as connection:
            cursor = connection.execute(
                "UPDATE rules SET active=? WHERE id=?", (int(active), rule_id)
            )
            return cursor.rowcount > 0

    def latest_event_id(self) -> int:
        with self.connection() as connection:
            row = connection.execute("SELECT COALESCE(MAX(id),0) id FROM events").fetchone()
        return int(row["id"])

    def events_after(self, event_id: int, limit: int, trading_date: date) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT e.*, r.exchange, r.tradingsymbol, r.percentage
                FROM events e JOIN rules r ON r.id=e.rule_id
                WHERE e.id>? AND r.trading_date=? ORDER BY e.id ASC LIMIT ?
                """,
                (event_id, trading_date.isoformat(), limit),
            ).fetchall()
        return [self._event_dict(row) for row in rows]

    @staticmethod
    def _display_percent(value: Decimal) -> str:
        rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"{rounded}%"

    @staticmethod
    def _rule_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["id"] = int(result["id"])
        for key in ("upper_sent", "lower_sent", "active"):
            result[key] = bool(result[key])
        result["upper_level_display"] = display_price(Decimal(result["upper_level"]))
        result["lower_level_display"] = display_price(Decimal(result["lower_level"]))
        result["reference_close_display"] = display_price(Decimal(result["reference_close"]))
        return result

    @classmethod
    def _event_dict(cls, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key in ("threshold", "entry_price", "stop_loss"):
            result[f"{key}_display"] = display_price(Decimal(result[key]))
        result["risk_percent_display"] = cls._display_percent(Decimal(result["risk_percent"]))
        return result
