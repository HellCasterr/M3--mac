"""Observed Nifty 200 sector baskets, not official weighted sector indices."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from threading import RLock

from .domain import IST, market_session_open


class SectorMonitor:
    def __init__(self, store, universe):
        self.store = store
        self.universe = universe
        self.quotes = {}
        self.lock = RLock()

    def tick(self, token, price, previous_close, timestamp):
        timestamp = timestamp.astimezone(IST)
        try:
            close = Decimal(str(previous_close))
        except Exception:
            return
        if not close.is_finite() or close <= 0 or not price.is_finite() or price <= 0:
            return
        if not market_session_open(timestamp):
            return
        with self.lock:
            old = self.quotes.get(token)
            if old and timestamp < old[2]:
                return
            self.quotes[token] = (price, close, timestamp)

    def dashboard(self, now):
        now = now.astimezone(IST)
        groups = {}
        # No network request on alert or quote paths. The normal universe loader
        # updates this classification from the official constituent file.
        industries = self.universe.industries.copy()
        for sector in industries.values():
            group = groups.setdefault(
                sector, {"sector": sector, "total": 0, "returns": [], "times": []}
            )
            group["total"] += 1
        rules = self.store.daily_rules(now.date())
        with self.lock:
            quotes = self.quotes.copy()
        for rule in rules:
            sector = industries.get(rule["tradingsymbol"])
            if not sector:
                continue
            group = groups.setdefault(
                sector, {"sector": sector, "total": 0, "returns": [], "times": []}
            )
            quote = quotes.get(rule["instrument_token"])
            if not rule["active"] or not quote:
                continue
            price, close, ts = quote
            if ts.date() != now.date() or not 0 <= (now - ts).total_seconds() <= 45:
                continue
            group["returns"].append((price / close - 1) * 100)
            group["times"].append(ts)
        rows = []
        for group in groups.values():
            values = group.pop("returns")
            times = group.pop("times")
            n = len(values)
            mean = sum(values) / n if n else None
            rising = sum(v > 0 for v in values)
            falling = sum(v < 0 for v in values)
            coverage = n / group["total"]
            eligible = n >= 3 and coverage >= 0.8 and market_session_open(now)
            # Explicit, unvalidated descriptive thresholds; not a prediction score.
            bias = "UNAVAILABLE"
            if eligible:
                bias = (
                    "BULLISH"
                    if mean >= Decimal(".20") and rising / n >= 0.6
                    else "BEARISH"
                    if mean <= Decimal("-.20") and falling / n >= 0.6
                    else "MIXED"
                )
            rows.append(
                {
                    **group,
                    "fresh": n,
                    "coverage_percent": round(coverage * 100, 1),
                    "mean_percent": round(float(mean), 3) if mean is not None else None,
                    "rising": rising,
                    "falling": falling,
                    "unchanged": n - rising - falling,
                    "bias": bias,
                    "oldest_quote": min(times).isoformat() if times else None,
                }
            )
        return {
            "as_of": now.isoformat(),
            "basis": (
                "Equal-weight Nifty 200 sector basket vs previous official close; "
                "not an NSE sector index"
            ),
            "mapping_source": self.universe._value.source
            if self.universe._value
            else "Load Nifty 200 to retrieve classifications",
            "unmapped": sum(r["tradingsymbol"] not in industries for r in rules),
            "sectors": sorted(rows, key=lambda x: x["sector"]),
        }

    def context(self, symbol, side, timestamp, source):
        sector = self.universe.industries.get(symbol)
        if source != "LIVE":
            return {
                "sector": sector,
                "alignment": "UNAVAILABLE",
                "summary": "Sector: unavailable at historical alert time (recovered alert)",
            }
        # Prevent delayed ticks being annotated with the present market state.
        now = datetime.now(IST)
        if not 0 <= (now - timestamp).total_seconds() <= 45:
            return {
                "sector": sector,
                "alignment": "UNAVAILABLE",
                "summary": "Sector: unavailable for delayed tick",
            }
        row = next((r for r in self.dashboard(timestamp)["sectors"] if r["sector"] == sector), None)
        if not row or row["bias"] == "UNAVAILABLE":
            return {
                "sector": sector,
                "alignment": "UNAVAILABLE",
                "summary": f"Sector: {sector or 'unmapped'} — insufficient fresh coverage",
            }
        expected = "BULLISH" if side == "LONG" else "BEARISH"
        alignment = (
            "ALIGNED"
            if row["bias"] == expected
            else ("MIXED" if row["bias"] == "MIXED" else "OPPOSED")
        )
        return {
            **row,
            "as_of": timestamp.isoformat(),
            "alignment": alignment,
            "summary": (
                f"Sector basket: {sector} {row['mean_percent']:+.2f}% | "
                f"{row['rising']}/{row['fresh']} rising | {row['bias']} | "
                f"{alignment} with {side} | coverage {row['fresh']}/{row['total']}"
            ),
        }
