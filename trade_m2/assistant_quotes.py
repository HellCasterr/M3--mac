"""Read-only Upstox instrument resolution and timestamped position/stock quotes.

Never select a nearby option strike/expiry when the exact held contract is missing.
This module makes GET requests only. Nothing here can place broker orders.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

from .domain import IST, market_session_open
from .upstox import UpstoxGateway

INDEX_UNDERLYINGS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "NIFTY50": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "BANK NIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|SENSEX",
}
VALID_SYMBOL = re.compile(r"^[A-Z][A-Z0-9&.\-]{0,28}$")
OPTION_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*(CE|PE)\b", re.I)
BLOCKED_TICKERS = {
    "BUY", "SELL", "LTP", "P&L", "OPEN", "CLOSE", "AVG", "TODAY", "PLEASE",
    "WHAT", "WHY", "HOW", "STOP", "LOSS", "CALL", "PUT", "ALL", "HOLD",
    "BOOK", "THIS", "THOSE", "THESE", "WITH", "PRICE", "PRICES", "API",
    "IST", "OHLC", "HIGH", "LOW", "PAPER", "NSE", "BSE", "CE", "PE",
}


def _expiry(raw: str) -> str | None:
    text = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", raw.strip(), flags=re.I)
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%d %b %y",
                "%d-%b-%Y", "%d-%b-%y", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _number(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
        if result.is_finite() and result > 0:
            return result
    except (InvalidOperation, TypeError, ValueError):
        pass
    return None


def _time(value: Any) -> datetime | None:
    try:
        if isinstance(value, str) and "T" in value:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(IST)
        amount = float(value)
        if amount > 10**11:
            amount /= 1000
        return datetime.fromtimestamp(amount, UTC).astimezone(IST)
    except (ValueError, TypeError, OverflowError):
        return None


def _struck_option(pos: dict[str, Any]) -> tuple[str, str] | None:
    match = OPTION_RE.search(str(pos.get("contract") or ""))
    return (match.group(1), match.group(2).upper()) if match else None


def _candidate_symbols(question: str, extra: list[str]) -> list[str]:
    symbols = [item.strip().upper().lstrip("@") for item in extra[:8]]
    symbols += [match.upper() for match in re.findall(r"@([A-Za-z][\w&.\-]{1,28})", question)]
    symbols += re.findall(r"\b[A-Z][A-Z0-9&.\-]{2,28}\b", question)[:8]
    return list(dict.fromkeys(
        s for s in symbols if VALID_SYMBOL.fullmatch(s) and s not in BLOCKED_TICKERS
    ))[:8]


class PositionQuotes:
    def __init__(self, gateway: UpstoxGateway) -> None:
        self.gateway = gateway
        self._lock = threading.RLock()
        self._instruments: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._quote_cache: tuple[float, tuple[str, ...], dict[str, Any]] | None = None

    def _get(self, path: str, params: dict[str, str]) -> Any:
        response = requests.get(
            "https://api.upstox.com" + path,
            params=params,
            headers={
                "Authorization": f"Bearer {self.gateway.access_token}",
                "Accept": "application/json",
            },
            timeout=12,
        )
        response.raise_for_status()
        return response.json().get("data", {})

    def _search(self, symbol: str, *, strike: str = "", option_type: str = "",
                expiry: str = "") -> dict[str, Any] | None:
        cache_key = "|".join((symbol, strike, option_type, expiry))
        cached = self._instruments.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        if strike:
            if not expiry:
                return None
            query = f"{symbol} {strike}"
            params = {
                "query": query[:50],
                "exchanges": "BSE" if symbol == "SENSEX" else "NSE",
                "segments": "FO", "instrument_types": option_type,
                "expiry": expiry, "records": "30",
            }
        else:
            params = {
                "query": symbol, "exchanges": "NSE", "segments": "EQ",
                "instrument_types": "EQ", "records": "30",
            }
        data = self._get("/v2/instruments/search", params)
        selected = None
        expected_strike = _number(strike) if strike else None
        if isinstance(data, list):
            for row in data:
                if not isinstance(row, dict):
                    continue
                key = row.get("instrument_key")
                if not isinstance(key, str) or "|" not in key:
                    continue
                if strike:
                    if str(row.get("underlying_symbol", "")).upper() != symbol:
                        continue
                    if str(row.get("instrument_type", "")).upper() != option_type:
                        continue
                    if str(row.get("expiry", "")) != expiry:
                        continue
                    if _number(row.get("strike_price")) != expected_strike:
                        continue
                    if not key.startswith("BSE_FO|" if symbol == "SENSEX" else "NSE_FO|"):
                        continue
                elif (
                    row.get("trading_symbol", "").upper() != symbol
                    or row.get("segment") != "NSE_EQ"
                ):
                    continue
                selected = row
                break
        self._instruments[cache_key] = (time.monotonic() + 1200, selected)
        return selected

    def _resolve(self, symbol: str, pos: dict[str, Any] | None) -> dict[str, Any]:
        identifier: dict[str, Any] = {"symbol": symbol}
        if pos is not None:
            identifier["contract"] = str(pos.get("contract") or "")
            identifier["expiry"] = str(pos.get("expiry") or "")
            identifier["position_id"] = pos.get("id")
            identifier["source"] = pos.get("source", "saved")
        opt = _struck_option(pos) if pos else None
        if opt:
            strike, side = opt
            expiry = _expiry(str(pos.get("expiry") or ""))
            if not expiry:
                return {
                    **identifier, "status": "UNRESOLVED",
                    "reason": "Enter exact option expiry (YYYY-MM-DD) before price lookup.",
                }
            if date.fromisoformat(expiry) < datetime.now(IST).date():
                return {
                    **identifier, "status": "EXPIRED",
                    "reason": "Expired option: no current contract LTP. Historical data required.",
                }
            row = self._search(symbol, strike=strike, option_type=side, expiry=expiry)
            if not row:
                return {
                    **identifier, "status": "UNRESOLVED",
                    "reason": "Exact strike/CE-PE/expiry contract not found. No substitution made.",
                }
            return {
                **identifier, "status": "RESOLVED",
                "instrument_key": row["instrument_key"],
                "trading_symbol": row.get("trading_symbol"),
                "instrument_kind": "OPTION", "lot_size": row.get("lot_size"),
                "underlying_key": row.get("underlying_key"),
            }
        if pos and re.search(r"\b(?:CE|PE|FUT)\b", str(pos.get("contract") or ""), re.I):
            return {
                **identifier, "status": "UNRESOLVED",
                "reason": "Contract details incomplete. Confirm strike, CE/PE and expiry.",
            }
        if symbol in INDEX_UNDERLYINGS:
            return {**identifier, "status": "RESOLVED",
                    "instrument_key": INDEX_UNDERLYINGS[symbol],
                    "instrument_kind": "INDEX"}
        row = self._search(symbol)
        if not row:
            return {**identifier, "status": "UNRESOLVED",
                    "reason": "No exact NSE equity ticker found. Check broker symbol."}
        return {
            **identifier, "status": "RESOLVED", "instrument_kind": "EQUITY",
            "instrument_key": row["instrument_key"], "trading_symbol": symbol,
        }

    def _quotes(self, keys: list[str]) -> dict[str, Any]:
        unique = tuple(sorted(set(keys)))
        if not unique:
            return {}
        if (self._quote_cache and self._quote_cache[1] == unique
                and time.monotonic() - self._quote_cache[0] < 10):
            return self._quote_cache[2]
        data: dict[str, Any] = {}
        for offset in range(0, len(unique), 100):
            batch = self._get("/v3/market-quote/quotes", {
                "instrument_key": ",".join(unique[offset:offset + 100]),
            })
            if isinstance(batch, dict):
                for row in batch.values():
                    if isinstance(row, dict) and row.get("instrument_token"):
                        data[str(row["instrument_token"])] = row
        self._quote_cache = (time.monotonic(), unique, data)
        return data

    def snapshot(self, positions: list[dict[str, Any]],
                 question: str = "", extra_symbols: list[str] | None = None
                 ) -> dict[str, Any]:
        now = datetime.now(IST)
        base = {
            "as_of": now.isoformat(), "market_open": market_session_open(now),
            "connected": self.gateway.authenticated,
            "prices": [], "unavailable": [],
        }
        if not self.gateway.authenticated or not self.gateway.access_token:
            base["notice"] = "Upstox not connected. No current stock or option quotes."
            return base
        if len(positions) > 50:
            positions = positions[:50]
        try:
            with self._lock:
                resolved = []
                for pos in positions:
                    symbol = str(pos.get("symbol") or "").strip().upper()
                    if not VALID_SYMBOL.fullmatch(symbol) and symbol not in INDEX_UNDERLYINGS:
                        resolved.append({
                            "symbol": symbol, "status": "UNRESOLVED",
                            "reason": "Invalid symbol; correct it in the positions table.",
                        })
                        continue
                    resolved.append(self._resolve(symbol, pos))
                already = {row["symbol"] for row in resolved}
                for symbol in _candidate_symbols(question, extra_symbols or []):
                    if symbol not in already:
                        resolved.append(self._resolve(symbol, None))
                for row in resolved:
                    if row.get("status") == "RESOLVED" and row.get("underlying_key"):
                        key = row["underlying_key"]
                        if not any(other.get("instrument_key") == key for other in resolved):
                            resolved.append({
                                "symbol": row["symbol"], "status": "RESOLVED",
                                "instrument_key": key, "instrument_kind": "UNDERLYING",
                            })
                quotes = self._quotes([
                    row["instrument_key"] for row in resolved
                    if row.get("status") == "RESOLVED"
                ])
                for row in resolved:
                    if row.get("status") != "RESOLVED":
                        base["unavailable"].append(row)
                        continue
                    q = quotes.get(row["instrument_key"])
                    if not q:
                        base["unavailable"].append({
                            **row, "status": "UNAVAILABLE",
                            "reason": "Upstox returned no full quote for this exact instrument.",
                        })
                        continue
                    ltp = _number(q.get("last_price"))
                    if not ltp:
                        base["unavailable"].append({
                            **row, "status": "UNAVAILABLE",
                            "reason": "Broker quote has no valid last traded price.",
                        })
                        continue
                    traded = _time(q.get("last_trade_time"))
                    age = (now - traded).total_seconds() if traded else None
                    fresh = bool(
                        market_session_open(now) and age is not None and 0 <= age <= 90
                    )
                    cp = _number(q.get("prev_close_price"))
                    depth = q.get("depth") or {}
                    buy = (depth.get("buy") or []) if isinstance(depth, dict) else []
                    sell = (depth.get("sell") or []) if isinstance(depth, dict) else []
                    bid = _number(buy[0].get("price")) if buy and isinstance(buy[0], dict) else None
                    ask = (
                        _number(sell[0].get("price"))
                        if sell and isinstance(sell[0], dict) else None
                    )
                    enriched = {
                        **row, "status": "QUOTED", "ltp": float(ltp),
                        "prev_close": float(cp) if cp else None,
                        "change_percent": (
                            round(float((ltp - cp) / cp * 100), 2) if cp else None
                        ),
                        "bid": float(bid) if bid else None,
                        "ask": float(ask) if ask else None,
                        "last_trade_time": traded.isoformat() if traded else None,
                        "age_seconds": int(age) if age is not None else None,
                        "fresh": fresh,
                        "quote_label": (
                            "FRESH" if fresh else
                            "LAST TRADE / MARKET CLOSED" if not market_session_open(now)
                            else "STALE OR UNVERIFIED"
                        ),
                    }
                    if row["instrument_kind"] == "OPTION":
                        matching = next((
                            p for p in positions if p.get("id") == row.get("position_id")
                            and str(p.get("symbol", "")).upper() == row["symbol"]
                            and str(p.get("contract", "")) == row.get("contract")
                        ), None)
                        if matching:
                            avg = _number(matching.get("average_price"))
                            qty = matching.get("quantity")
                            if avg and isinstance(qty, int) and qty > 0:
                                signed = (
                                    1 if str(matching.get("side", "BUY")).upper() == "BUY"
                                    else -1
                                )
                                enriched["indicative_pnl"] = round(
                                    float((ltp - avg) * qty * signed), 2
                                )
                                enriched["indicative_only"] = True
                    base["prices"].append(enriched)
        except (requests.RequestException, ValueError, TypeError):
            base["notice"] = (
                "Upstox price lookup failed. Check connection and retry; "
                "no unverified price substituted."
            )
            base["unavailable"] = [
                {"symbol": p.get("symbol", ""), "status": "UNAVAILABLE",
                 "reason": "Broker API error."} for p in positions
            ]
            base["prices"] = []
            return base
        base["notice"] = (
            "Broker full quotes. 'FRESH' requires last trade within 90 seconds "
            "during regular market hours. P&L from LTP is indicative, not a sellable fill."
        )
        return base
