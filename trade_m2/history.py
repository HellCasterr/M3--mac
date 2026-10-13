"""Read-only, cached historical market data and local alert review."""

from __future__ import annotations

import csv
import io
import json
import threading
from datetime import date, datetime, timedelta
from typing import Literal
from urllib.parse import quote

import requests
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .domain import IST
from .options import dec


class HistoryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instrument_key: str = Field(
        min_length=3, max_length=160, pattern=r"^[A-Z_]+\|[^/\\?#\x00-\x1f]+$"
    )
    from_date: date
    to_date: date
    interval: Literal["1minute", "3minute", "5minute", "15minute", "30minute", "day"] = "1minute"
    expired: bool = False
    refresh: bool = False

    @model_validator(mode="after")
    def bounds(self):
        if self.to_date < self.from_date:
            raise ValueError("to_date must be on or after from_date.")
        if (self.to_date - self.from_date).days > 89:
            raise ValueError("Request at most 90 calendar days at a time.")
        if self.expired:
            parts = self.instrument_key.split("|")
            if len(parts) != 3:
                raise ValueError("Use the full expired instrument key from contract discovery.")
            try:
                expiry = datetime.strptime(parts[-1], "%d-%m-%Y").date()
            except ValueError as exc:
                raise ValueError("Expired key must end in DD-MM-YYYY.") from exc
            if self.to_date > expiry:
                raise ValueError("Expired-contract request cannot extend beyond expiry.")
        elif len(self.instrument_key.split("|")) != 2:
            raise ValueError("Use expired=true for a dated expired-contract key.")
        return self


class HistoryError(Exception):
    def __init__(self, code, message, status=502):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class HistoricalData:
    def __init__(self, store, gateway, clock=None):
        self.store, self.gateway = store, gateway
        self.clock = clock or (lambda: datetime.now(IST))
        self.lock = threading.Lock()
        with store.connection() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS historical_cache (
                cache_key TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, payload TEXT NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS historical_contract_cache (
                underlying_key TEXT NOT NULL, expiry TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (underlying_key, expiry))""")

    def _get(self, path, params=None):
        if not self.gateway.authenticated:
            raise HistoryError(
                "LOGIN_REQUIRED", "Connect Upstox to fetch uncached historical data.", 401
            )
        try:
            r = requests.get(
                "https://api.upstox.com" + path,
                params=params,
                headers={
                    "Authorization": f"Bearer {self.gateway.access_token}",
                    "Accept": "application/json",
                },
                timeout=8,
            )
            try:
                body = r.json()
            except ValueError as exc:
                raise HistoryError(
                    "INVALID_RESPONSE", "Upstox returned a non-JSON response."
                ) from exc
            codes = (
                {
                    str(e.get("errorCode") or e.get("error_code"))
                    for e in (body.get("errors") or [])
                    if isinstance(e, dict)
                }
                if isinstance(body, dict)
                else set()
            )
            if "UDAPI1149" in codes:
                raise HistoryError(
                    "PLUS_REQUIRED",
                    "Upstox Plus access is required for expired-instrument history.",
                    403,
                )
            if r.status_code == 429:
                raise HistoryError(
                    "RATE_LIMITED",
                    "Upstox rate limit reached. Retry later; no partial result was cached.",
                    429,
                )
            if r.status_code in {401, 403}:
                raise HistoryError(
                    "ACCESS_DENIED",
                    "Upstox access denied. Reconnect and check historical-data entitlement.",
                    r.status_code,
                )
            if not r.ok or not isinstance(body, dict) or body.get("status") != "success":
                raise HistoryError(
                    "PROVIDER_ERROR", "Upstox rejected the instrument, interval or date range."
                )
            return body.get("data")
        except requests.RequestException as exc:
            raise HistoryError(
                "NETWORK_ERROR", "Historical-data request failed or timed out."
            ) from exc

    def contracts(self, underlying_key, expiry):
        # Validates/encodes supplied values; host and endpoint are fixed.
        HistoryQuery(instrument_key=underlying_key, from_date=expiry, to_date=expiry)
        with self.store.connection() as c:
            cached = c.execute(
                "SELECT payload FROM historical_contract_cache WHERE underlying_key=? AND expiry=?",
                (underlying_key, expiry.isoformat()),
            ).fetchone()
        if cached:
            return json.loads(cached["payload"])
        today = self.clock().date()
        if expiry >= today:
            path = "/v2/option/contract"
        else:
            path = "/v2/expired-instruments/option/contract"
        with self.lock:
            data = self._get(
                path, {"instrument_key": underlying_key, "expiry_date": expiry.isoformat()}
            )
        if not isinstance(data, list):
            raise HistoryError("INVALID_RESPONSE", "Missing option contract list.")
        result = {
            "underlying_key": underlying_key,
            "expiry": expiry.isoformat(),
            "expired": expiry < today,
            "contracts": data,
            "note": "Use the exact returned instrument_key, including its expiry suffix.",
        }
        with self.store.connection() as c:
            c.execute(
                "INSERT OR REPLACE INTO historical_contract_cache VALUES (?,?,?)",
                (underlying_key, expiry.isoformat(), json.dumps(result)),
            )
        return result

    def candles(self, query):
        q = HistoryQuery.model_validate(query)
        now = self.clock()
        if q.to_date > now.date():
            raise ValueError("Future historical dates are not allowed.")
        if (
            q.expired
            and datetime.strptime(q.instrument_key.split("|")[-1], "%d-%m-%Y").date() >= now.date()
        ):
            raise ValueError("Expired-contract history requires an expiry before today.")
        key = json.dumps(q.model_dump(mode="json", exclude={"refresh"}), sort_keys=True)
        with self.lock:
            with self.store.connection() as c:
                cached = c.execute(
                    "SELECT * FROM historical_cache WHERE cache_key=?", (key,)
                ).fetchone()
            ttl = 30 if q.to_date == now.date() else 86400
            if (
                cached
                and not q.refresh
                and (
                    q.to_date < now.date()
                    or 0
                    <= (now - datetime.fromisoformat(cached["fetched_at"])).total_seconds()
                    < ttl
                )
            ):
                result = json.loads(cached["payload"])
                result["cache_hit"] = True
                return result
            token = quote(q.instrument_key, safe="")
            end = min(q.to_date, now.date() - timedelta(days=1))
            start = q.from_date
            raw = []
            chunks = 0
            while start <= end:
                stop = min(end, start + timedelta(days=27))
                if q.expired:
                    path = (
                        f"/v2/expired-instruments/historical-candle/{token}/"
                        f"{q.interval}/{stop}/{start}"
                    )
                else:
                    unit, interval = (
                        ("days", 1) if q.interval == "day" else ("minutes", int(q.interval[:-6]))
                    )
                    path = f"/v3/historical-candle/{token}/{unit}/{interval}/{stop}/{start}"
                body = self._get(path)
                if not isinstance(body, dict) or not isinstance(body.get("candles"), list):
                    raise HistoryError(
                        "INVALID_RESPONSE", "Historical response has no candle list."
                    )
                raw.extend(body["candles"])
                chunks += 1
                start = stop + timedelta(days=1)
            if q.to_date == now.date() and not q.expired and q.interval != "day":
                interval = int(q.interval[:-6])
                body = self._get(f"/v3/historical-candle/intraday/{token}/minutes/{interval}")
                if not isinstance(body, dict) or not isinstance(body.get("candles"), list):
                    raise HistoryError("INVALID_RESPONSE", "Intraday response has no candle list.")
                raw.extend(body["candles"])
                chunks += 1
            candles, discarded = self._normalize(raw, q, now)
            result = {
                "instrument_key": q.instrument_key,
                "expired": q.expired,
                "interval": q.interval,
                "from_date": str(q.from_date),
                "to_date": str(q.to_date),
                "timezone": "Asia/Kolkata",
                "source": "UPSTOX",
                "fetched_at": now.isoformat(),
                "cache_hit": False,
                "request_chunks": chunks,
                "count": len(candles),
                "candles": candles,
                "discarded_rows": discarded,
                "status": "AVAILABLE" if candles else "EMPTY",
                "warnings": [
                    "OHLC candles do not reveal intrabar target/stop order or historical bid/ask.",
                    "Provider coverage varies; missing candles are never interpolated.",
                    "Only completed candles are returned; today’s daily candle is excluded.",
                ],
            }
            with self.store.connection() as c:
                c.execute(
                    "INSERT OR REPLACE INTO historical_cache VALUES (?,?,?)",
                    (key, now.isoformat(), json.dumps(result)),
                )
            return result

    def cached_candles(self, query):
        """Read a saved exact request without contacting Upstox."""
        q = HistoryQuery.model_validate(query)
        key = json.dumps(q.model_dump(mode="json", exclude={"refresh"}), sort_keys=True)
        with self.store.connection() as c:
            row = c.execute(
                "SELECT payload FROM historical_cache WHERE cache_key=?", (key,)
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    @staticmethod
    def _normalize(raw, q, now):
        by_time = {}
        discarded = 0
        duration = (
            timedelta(days=1) if q.interval == "day" else timedelta(minutes=int(q.interval[:-6]))
        )
        for row in raw:
            try:
                stamp = datetime.fromisoformat(row[0])
                if stamp.tzinfo is None:
                    raise ValueError("Missing timezone")
                stamp = stamp.astimezone(IST)
                if not q.from_date <= stamp.date() <= q.to_date or stamp + duration > now:
                    discarded += 1
                    continue
                o, h, lo, close = [dec(v) for v in row[1:5]]
                volume = dec(row[5])
                oi = dec(row[6]) if len(row) > 6 and row[6] is not None else None
                if (
                    min(o, h, lo, close, volume) < 0
                    or not lo <= min(o, close) <= max(o, close) <= h
                    or (oi is not None and oi < 0)
                ):
                    raise ValueError("Invalid OHLC")
                item = {
                    "timestamp": stamp.isoformat(),
                    "open": str(o),
                    "high": str(h),
                    "low": str(lo),
                    "close": str(close),
                    "volume": str(volume),
                    "open_interest": str(oi) if oi is not None else None,
                }
                if item["timestamp"] in by_time:
                    discarded += 1
                by_time[item["timestamp"]] = item
            except (ValueError, TypeError, IndexError, ArithmeticError):
                discarded += 1
        return sorted(by_time.values(), key=lambda c: c["timestamp"]), discarded


class AlertHistory:
    def __init__(self, store):
        self.store = store
        with store.connection() as c:
            c.execute("""CREATE INDEX IF NOT EXISTS idx_alert_history_day
                ON alert_history(json_extract(payload, '$.trading_date'))""")

    def dates(self):
        with self.store.connection() as c:
            return [
                dict(r)
                for r in c.execute("""SELECT json_extract(payload, '$.trading_date') AS date,
                COUNT(*) AS count FROM alert_history GROUP BY date ORDER BY date DESC""")
            ]

    def rows(self, day=None, order="desc"):
        with self.store.connection() as c:
            where = " WHERE json_extract(payload, '$.trading_date')=?" if day else ""
            rows = c.execute(
                "SELECT id,payload FROM alert_history" + where + " ORDER BY id",
                (str(day),) if day else (),
            ).fetchall()
            existing = {r["id"]: dict(r) for r in c.execute("SELECT * FROM events")}
            decisions = {
                r["event_id"]: json.loads(r["payload"])
                for r in c.execute("SELECT * FROM paper_decisions")
            }
            trades = {
                r["event_id"]: json.loads(r["payload"])
                for r in c.execute("SELECT * FROM paper_trades")
            }
        result = []
        for row in rows:
            v = json.loads(row["payload"])
            eid = v["id"]
            v["history_id"] = row["id"]
            v["created_at"] = v.get("created_at") or existing.get(eid, {}).get("created_at")
            for key in ["stop_loss", "risk_percent", "boundary", "opening_zone"]:
                if key not in v and eid in existing:
                    v[key] = existing[eid].get(key)
            sector = v.get("sector_context") or {}
            if isinstance(sector, str):
                sector = json.loads(sector)
            v["sector_context"] = sector
            v["option_context"] = decisions.get(eid)
            t = trades.get(eid)
            v["paper_trade"] = (
                {
                    k: t.get(k)
                    for k in [
                        "id",
                        "state",
                        "mode",
                        "quantity",
                        "remaining",
                        "entry",
                        "realised",
                        "fees",
                        "exit_reason",
                        "uncertain",
                        "filled_at",
                        "closed_at",
                        "entry_at",
                        "entry_basis",
                        "reason",
                        "settings",
                    ]
                }
                if t
                else None
            )
            result.append(v)
        return sorted(
            result,
            key=lambda v: (v.get("event_time", ""), v["history_id"]),
            reverse=order == "desc",
        )

    def export(self, day=None, order="desc"):
        rows = self.rows(day, order)
        fields = [
            "history_id",
            "id",
            "trading_date",
            "event_time",
            "tradingsymbol",
            "trade_side",
            "entry_price",
            "stop_loss",
            "risk_percent",
            "boundary",
            "opening_zone",
            "source",
            "percentage",
            "reference_close",
            "threshold",
            "sector",
            "sector_alignment",
            "sector_mean_percent",
            "sector_context",
            "option_status",
            "option_strike",
            "option_type",
            "option_expiry",
            "option_instrument_key",
            "option_lot_size",
            "option_bid",
            "option_ask",
            "option_delta",
            "option_reason",
            "paper_id",
            "paper_state",
            "paper_quantity",
            "paper_entry",
            "paper_entry_at",
            "paper_entry_basis",
            "paper_reason",
            "paper_realised_pnl",
            "paper_fees",
            "paper_exit_reason",
            "paper_uncertain",
            "paper_settings",
        ]
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for v in rows:
            d = v.get("option_context") or {}
            option = d.get("candidate") or {}
            sector = v["sector_context"]
            t = v.get("paper_trade") or {}
            v.update(
                sector=sector.get("sector"),
                sector_alignment=sector.get("alignment"),
                sector_mean_percent=sector.get("mean_percent"),
                sector_context=json.dumps(sector),
                option_status=d.get("status", "NOT_RECORDED"),
                option_strike=option.get("strike"),
                option_type=option.get("kind"),
                option_expiry=option.get("expiry"),
                option_instrument_key=option.get("instrument_key"),
                option_lot_size=option.get("lot_size"),
                option_bid=option.get("bid"),
                option_ask=option.get("ask"),
                option_delta=option.get("delta"),
                option_reason=option.get("reason") or d.get("reason"),
                paper_id=t.get("id"),
                paper_state=t.get("state"),
                paper_quantity=t.get("quantity"),
                paper_entry=t.get("entry"),
                paper_entry_at=t.get("entry_at"),
                paper_entry_basis=t.get("entry_basis"),
                paper_reason=t.get("reason"),
                paper_realised_pnl=t.get("realised"),
                paper_fees=t.get("fees"),
                paper_exit_reason=t.get("exit_reason"),
                paper_uncertain=t.get("uncertain"),
                paper_settings=json.dumps(t.get("settings")) if t else "",
            )
            for key in fields:
                value = v.get(key)
                if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                    try:
                        dec(value)
                    except (ValueError, ArithmeticError):
                        v[key] = "'" + value
            writer.writerow(v)
        return "\ufeff" + out.getvalue()
