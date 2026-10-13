from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .domain import Candle


@dataclass(frozen=True)
class ProtectiveStop:
    price: Decimal
    risk_percent: Decimal
    atr: Decimal
    method: str


def protective_stop(
    candles: list[Candle], entry: Decimal, side: str
) -> ProtectiveStop:
    recent = candles[-14:]
    if not recent:
        atr = entry * Decimal("0.006")
        swing_low = entry
        swing_high = entry
    else:
        ranges: list[Decimal] = []
        for index, candle in enumerate(recent):
            previous = recent[index - 1].close if index else None
            values = [candle.high - candle.low]
            if previous is not None:
                values.extend((abs(candle.high - previous), abs(candle.low - previous)))
            ranges.append(max(values))
        atr = sum(ranges, Decimal("0")) / Decimal(len(ranges))
        if atr <= 0:
            atr = entry * Decimal("0.006")
        swing_window = recent[-10:]
        swing_low = min(candle.low for candle in swing_window)
        swing_high = max(candle.high for candle in swing_window)

    if side == "LONG":
        raw = min(entry - atr * Decimal("1.50"), swing_low - atr * Decimal("0.10"))
        raw_risk = entry - raw
    else:
        raw = max(entry + atr * Decimal("1.50"), swing_high + atr * Decimal("0.10"))
        raw_risk = raw - entry
    minimum = entry * Decimal("0.0035")
    maximum = entry * Decimal("0.03")
    risk = min(max(raw_risk, minimum), maximum)
    if side == "LONG":
        price = (entry - risk).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
        risk = entry - price
    else:
        price = (entry + risk).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
        risk = price - entry
    return ProtectiveStop(
        price=price,
        risk_percent=(risk / entry * Decimal("100")),
        atr=atr,
        method="1.50x ATR + 10-candle structure fallback",
    )
