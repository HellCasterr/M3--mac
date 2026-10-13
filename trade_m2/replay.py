"""Retrospective, candle-based paper study. Never sends broker orders."""

from __future__ import annotations

import csv
import io
import threading
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from .domain import IST
from .history import HistoryError
from .options import dec


class ReplayRequest(BaseModel):
    csv_text: str = Field(min_length=1, max_length=2_000_000)
    expiry: date
    target_percent: Decimal = Field(default=Decimal("10"), gt=0, le=100)
    stop_percent: Decimal = Field(default=Decimal("5"), gt=0, lt=100)
    budget: Decimal = Field(default=Decimal("10000"), gt=0, le=100_000_000)

    @model_validator(mode="after")
    def valid_date(self):
        if self.expiry > datetime.now(IST).date() + timedelta(days=180):
            raise ValueError("Expiry is too far in the future.")
        return self


def parse_alerts(content: str):
    try:
        reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff"), newline=""))
        headers = set(reader.fieldnames or ())
        if not {"tradingsymbol", "trade_side", "event_time"} <= headers:
            raise ValueError("CSV needs tradingsymbol, trade_side and event_time columns.")
        alerts = []
        for index, row in enumerate(reader, 1):
            if index > 500:
                raise ValueError("Upload at most 500 alerts per run.")
            symbol = (row.get("tradingsymbol") or "").strip().upper()
            side = (row.get("trade_side") or "").strip().upper()
            if not symbol or not symbol.replace("-", "").replace("&", "").isalnum():
                raise ValueError(f"Row {index}: invalid stock symbol.")
            if side not in {"LONG", "SHORT"}:
                raise ValueError(f"Row {index}: direction must be LONG or SHORT.")
            try:
                stamp = datetime.fromisoformat((row.get("event_time") or "").strip())
                if stamp.tzinfo is None:
                    raise ValueError("missing timezone")
                stamp = stamp.astimezone(IST)
            except ValueError as exc:
                raise ValueError(
                    f"Row {index}: event_time needs an ISO date and timezone."
                ) from exc
            price = row.get("entry_price", "").strip()
            try:
                spot = dec(price) if price else None
                if spot is not None and spot <= 0:
                    raise ValueError("nonpositive price")
            except ValueError as exc:
                raise ValueError(f"Row {index}: invalid stock entry_price.") from exc
            alerts.append(
                {
                    "row": index,
                    "id": row.get("id") or str(index),
                    "symbol": symbol,
                    "side": side,
                    "alert_time": stamp.isoformat(),
                    "spot": str(spot) if spot else None,
                }
            )
    except csv.Error as exc:
        raise ValueError("Invalid CSV file.") from exc
    if not alerts:
        raise ValueError("CSV has no alert rows.")
    return alerts


def _first_entry_minute(signal):
    minute = signal.replace(second=0, microsecond=0)
    return minute if signal == minute else minute + timedelta(minutes=1)


def evaluate(candles, alert_time, budget, lot_size, target_percent, stop_percent):
    """Replay on traded one-minute OHLC, entering at the alert minute when exact."""
    signal = datetime.fromisoformat(alert_time)
    cutoff = datetime.combine(signal.date(), time(15, 30), IST)
    first_entry = _first_entry_minute(signal)
    rows = [
        c
        for c in candles
        if first_entry <= datetime.fromisoformat(c["timestamp"]) < cutoff
    ]
    if not rows:
        return {
            "status": "NO_ENTRY_CANDLE",
            "reason": "No traded option minute at/after the alert before 15:30 IST.",
        }
    entry = rows[0]
    price = dec(entry["open"])
    if price <= 0:
        return {"status": "NO_ENTRY_CANDLE", "reason": "Option entry candle has no valid price."}
    cost = price * lot_size
    if cost > budget:
        return {
            "status": "OVER_BUDGET",
            "reason": f"One lot costs ₹{cost:.2f}; budget is ₹{budget:.2f}.",
            "one_lot_cost": str(cost),
        }
    lots = int(budget // cost)
    quantity = lots * lot_size
    target = price * (1 + target_percent / 100)
    stop = price * (1 - stop_percent / 100)
    stamps = [datetime.fromisoformat(c["timestamp"]) for c in rows]
    has_gaps = any(
        current - previous > timedelta(minutes=1)
        for previous, current in zip(stamps, stamps[1:], strict=False)
    )
    base = {
        "entry_time": entry["timestamp"],
        "entry_price": str(price),
        "one_lot_cost": str(cost),
        "lots": lots,
        "quantity": quantity,
        "cost": str(price * quantity),
        "target_price": str(target),
        "stop_price": str(stop),
        "price_basis": (
            "traded one-minute OHLC; exact-minute open when alert is on :00, "
            "otherwise next traded minute"
        ),
        "history_gaps": has_gaps,
    }
    # Missing option candles are common when a contract does not trade every
    # minute. Do not abort the replay merely because there is a timestamp gap:
    # evaluate the prices that actually traded and flag the coverage instead.
    for candle in rows:
        high, low = dec(candle["high"]), dec(candle["low"])
        opening = dec(candle["open"])
        if candle is not entry and opening <= stop:
            return {
                **base,
                "status": "STOP",
                "exit_time": candle["timestamp"],
                "exit_price": str(opening),
                "pnl": str((opening - price) * quantity),
                "uncertain": False,
                "reason": "Option opened below the stop.",
            }
        if candle is not entry and opening >= target:
            return {
                **base,
                "status": "TARGET",
                "exit_time": candle["timestamp"],
                "exit_price": str(opening),
                "pnl": str((opening - price) * quantity),
                "uncertain": False,
                "reason": "Option opened above the target.",
            }
        if low <= stop:
            ambiguous = high >= target
            exit_price = min(dec(candle["open"]), stop) if candle is not entry else stop
            return {
                **base,
                "status": "STOP",
                "exit_time": candle["timestamp"],
                "exit_price": str(exit_price),
                "pnl": str((exit_price - price) * quantity),
                "uncertain": ambiguous,
                "reason": "Both levels touched in one minute; stop assumed first."
                if ambiguous
                else "Stop touched (one-minute candle).",
            }
        if high >= target:
            exit_price = max(dec(candle["open"]), target) if candle is not entry else target
            return {
                **base,
                "status": "TARGET",
                "exit_time": candle["timestamp"],
                "exit_price": str(exit_price),
                "pnl": str((exit_price - price) * quantity),
                "uncertain": False,
                "reason": "Target touched (one-minute candle).",
            }
    last = rows[-1]
    last_stamp = datetime.fromisoformat(last["timestamp"])
    stale_exit = last_stamp < cutoff - timedelta(minutes=2)
    return {
        **base,
        "status": "TIME_EXIT",
        "exit_time": last["timestamp"],
        "exit_price": last["close"],
        "pnl": str((dec(last["close"]) - price) * quantity),
        "uncertain": stale_exit,
        "reason": (
            "No target/stop in traded candles; exited on the last traded option "
            "candle before 15:30 IST."
            if not stale_exit
            else (
                "No target/stop in traded candles; the option stopped trading before "
                "15:30, so the last traded close was used."
            )
        ),
    }


class ReplayJobs:
    def __init__(self, store, gateway, history):
        self.store, self.gateway, self.history = store, gateway, history
        self.lock = threading.Lock()
        self.jobs = {}

    def start(self, payload: ReplayRequest):
        alerts = parse_alerts(payload.csv_text)
        if not self.gateway.authenticated:
            raise ValueError("Connect Upstox to fetch option contract and candle history.")
        with self.lock:
            if any(j["state"] == "RUNNING" for j in self.jobs.values()):
                raise ValueError("A replay is already running.")
            job_id = uuid.uuid4().hex
            self.jobs[job_id] = {
                "id": job_id,
                "state": "RUNNING",
                "total": len(alerts),
                "processed": 0,
                "rows": [],
                "expiry": payload.expiry.isoformat(),
                "error": None,
            }
        threading.Thread(target=self._run, args=(job_id, alerts, payload), daemon=True).start()
        return self.snapshot(job_id)

    def current(self):
        """Return the active replay, or the most recently created replay after refresh."""
        with self.lock:
            if not self.jobs:
                raise KeyError("current")
            running = [job["id"] for job in self.jobs.values() if job["state"] == "RUNNING"]
            job_id = running[-1] if running else next(reversed(self.jobs))
        return self.snapshot(job_id)

    def snapshot(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            rows = [dict(r) for r in job["rows"]]
            return {
                **job,
                "rows": rows,
                "counts": {
                    status: sum(r["status"] == status for r in rows)
                    for status in sorted({r["status"] for r in rows})
                },
                "total_pnl": str(sum((dec(r["pnl"]) for r in rows if "pnl" in r), Decimal(0))),
            }

    def _underlying(self, symbol, day):
        with self.store.connection() as c:
            found = c.execute(
                "SELECT instrument_token FROM rules "
                "WHERE trading_date=? AND tradingsymbol=? LIMIT 1",
                (day.isoformat(), symbol),
            ).fetchone()
        if found:
            return found["instrument_token"]
        return self.gateway.resolve_instrument("NSE", symbol)["instrument_token"]

    def _one(self, alert, payload, underlying_cache, contract_cache, candle_cache):
        day = datetime.fromisoformat(alert["alert_time"]).date()
        if day > self.history.clock().date() or day > payload.expiry:
            return {
                "status": "INVALID_DATE",
                "reason": "Alert is after selected expiry or in the future.",
            }
        stamp = datetime.fromisoformat(alert["alert_time"])
        first_entry = _first_entry_minute(stamp)
        market_close = datetime.combine(day, time(15, 30), IST)
        if stamp.time() < time(9, 15) or first_entry >= market_close:
            return {
                "status": "OUTSIDE_SESSION",
                "reason": "No executable one-minute replay entry between 09:15 and 15:30 IST.",
            }

        symbol = alert["symbol"]
        if symbol not in underlying_cache:
            underlying_cache[symbol] = self._underlying(symbol, day)
        key = underlying_cache[symbol]
        spot = dec(alert["spot"]) if alert["spot"] else None
        if spot is None:
            stock_key = (key, day)
            if stock_key not in candle_cache:
                candle_cache[stock_key] = self.history.candles(
                    {
                        "instrument_key": key,
                        "from_date": day,
                        "to_date": day,
                    }
                )["candles"]
            previous = [
                c
                for c in candle_cache[stock_key]
                if datetime.fromisoformat(c["timestamp"]) + timedelta(minutes=1) <= stamp
            ]
            if not previous:
                return {
                    "status": "NO_STOCK_PRICE",
                    "reason": "No stock price recorded by alert time.",
                }
            spot = dec(previous[-1]["close"])

        contract_key = (key, payload.expiry)
        if contract_key not in contract_cache:
            contract_cache[contract_key] = self.history.contracts(key, payload.expiry)["contracts"]
        kind = "CE" if alert["side"] == "LONG" else "PE"
        contracts = [
            c
            for c in contract_cache[contract_key]
            if c.get("instrument_type") == kind
            and c.get("segment") == "NSE_FO"
            and c.get("instrument_key")
            and int(c.get("lot_size") or 0) > 0
        ]
        if not contracts:
            return {
                "status": "NO_CONTRACT",
                "reason": f"No {kind} F&O contract exists for the selected expiry.",
            }

        ranked = sorted(
            contracts,
            key=lambda c: (abs(dec(c["strike_price"]) - spot), dec(c["strike_price"])),
        )
        # The mathematically closest strike can be completely untraded in a
        # later-month series. Try nearby strikes in distance order and use the
        # closest contract that actually has an executable candle.
        max_candidates = min(7, len(ranked))
        attempted = []
        expired = payload.expiry < self.history.clock().date()

        for rank, contract in enumerate(ranked[:max_candidates], 1):
            token = contract["instrument_key"]
            original_token = token
            cached_today = None
            if expired and len(token.split("|")) == 2:
                cached_today = self.history.cached_candles(
                    {
                        "instrument_key": token,
                        "from_date": day,
                        "to_date": day,
                        "interval": "1minute",
                        "expired": False,
                    }
                )
                if cached_today is None:
                    token = token + "|" + payload.expiry.strftime("%d-%m-%Y")

            option_key = (token, day)
            if option_key not in candle_cache:
                candle_cache[option_key] = (
                    cached_today
                    or self.history.candles(
                        {
                            "instrument_key": token,
                            "from_date": day,
                            "to_date": day,
                            "interval": "1minute",
                            "expired": expired,
                        }
                    )
                )["candles"]

            detail = evaluate(
                candle_cache[option_key],
                alert["alert_time"],
                payload.budget,
                int(contract["lot_size"]),
                payload.target_percent,
                payload.stop_percent,
            )
            attempted.append(str(contract["strike_price"]))
            if detail["status"] == "NO_ENTRY_CANDLE":
                continue

            return {
                "contract": contract.get("trading_symbol"),
                "instrument_key": token,
                "source_instrument_key": original_token,
                "strike": str(contract["strike_price"]),
                "strike_rank": rank,
                "contracts_tested": rank,
                "selection_reason": (
                    "Nearest strike had executable option history."
                    if rank == 1
                    else (
                        f"Used the {rank}th-nearest strike because {rank - 1} closer "
                        "strike(s) had no traded entry candle."
                    )
                ),
                "kind": kind,
                "spot": str(spot),
                "lot_size": int(contract["lot_size"]),
                **detail,
            }

        nearest = ranked[0]
        return {
            "contract": nearest.get("trading_symbol"),
            "instrument_key": nearest.get("instrument_key"),
            "strike": str(nearest["strike_price"]),
            "kind": kind,
            "spot": str(spot),
            "lot_size": int(nearest["lot_size"]),
            "contracts_tested": max_candidates,
            "tested_strikes": ",".join(attempted),
            "status": "NO_ENTRY_CANDLE",
            "reason": (
                f"No traded {kind} option candle was available at/after the alert "
                f"for the {max_candidates} nearest strikes before 15:30 IST."
            ),
        }

    def _run(self, job_id, alerts, payload):
        underlying_cache, contract_cache, candle_cache = {}, {}, {}
        for alert in alerts:
            try:
                result = self._one(alert, payload, underlying_cache, contract_cache, candle_cache)
            except HistoryError as exc:
                result = {"status": exc.code, "reason": exc.message}
            except Exception as exc:
                result = {"status": "DATA_ERROR", "reason": str(exc)[:240]}
            with self.lock:
                job = self.jobs[job_id]
                job["rows"].append({**alert, **result})
                job["processed"] += 1
        with self.lock:
            self.jobs[job_id]["state"] = "DONE"
