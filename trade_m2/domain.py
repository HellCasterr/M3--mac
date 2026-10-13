from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
THREE_MINUTES = timedelta(minutes=3)


def as_decimal(value: object) -> Decimal:
    return Decimal(str(value))


def ensure_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=IST)
    return value.astimezone(IST)


def display_price(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def calculate_levels(reference_close: Decimal, percentage: Decimal) -> tuple[Decimal, Decimal]:
    if reference_close <= 0:
        raise ValueError("Reference close must be positive.")
    if percentage <= 0:
        raise ValueError("Percentage must be positive.")
    factor = percentage / Decimal("100")
    return reference_close * (Decimal("1") + factor), reference_close * (
        Decimal("1") - factor
    )


def opening_zone(price: Decimal, upper: Decimal, lower: Decimal) -> str:
    if price > upper:
        return "ABOVE"
    if price < lower:
        return "BELOW"
    if price == upper:
        return "ON_UPPER"
    if price == lower:
        return "ON_LOWER"
    return "INSIDE"


def market_session_open(now: datetime | None = None) -> bool:
    now = ensure_ist(now or datetime.now(IST))
    local_time = now.time().replace(tzinfo=None)
    return now.weekday() < 5 and SESSION_OPEN <= local_time < SESSION_CLOSE


def session_bounds(trading_date: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(trading_date, SESSION_OPEN, IST),
        datetime.combine(trading_date, SESSION_CLOSE, IST),
    )


@dataclass(frozen=True)
class Candle:
    instrument_token: str
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass
class _BuildingCandle:
    instrument_token: str
    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    def update(self, price: Decimal) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price

    def complete(self) -> Candle:
        return Candle(
            self.instrument_token,
            self.start,
            self.start + THREE_MINUTES,
            self.open,
            self.high,
            self.low,
            self.close,
        )


class CandleAggregator:
    def __init__(self) -> None:
        self._candles: dict[str, _BuildingCandle] = {}

    @staticmethod
    def bucket(timestamp: datetime) -> datetime | None:
        timestamp = ensure_ist(timestamp)
        opening, closing = session_bounds(timestamp.date())
        if timestamp < opening or timestamp >= closing:
            return None
        seconds = int((timestamp - opening).total_seconds())
        return opening + timedelta(seconds=(seconds // 180) * 180)

    def add_tick(self, token: str, price: Decimal, timestamp: datetime) -> list[Candle]:
        start = self.bucket(timestamp)
        if start is None:
            return []
        current = self._candles.get(token)
        completed: list[Candle] = []
        if current is None or current.start != start:
            if current is not None and current.start < start:
                completed.append(current.complete())
            self._candles[token] = _BuildingCandle(token, start, price, price, price, price)
        else:
            current.update(price)
        return completed

    def finalize_due(self, now: datetime, delay_seconds: int = 3) -> list[Candle]:
        now = ensure_ist(now)
        completed: list[Candle] = []
        for token, candle in list(self._candles.items()):
            if now >= candle.start + THREE_MINUTES + timedelta(seconds=delay_seconds):
                completed.append(candle.complete())
                del self._candles[token]
        return completed
