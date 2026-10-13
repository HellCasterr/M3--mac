from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import requests

from .domain import (
    IST,
    THREE_MINUTES,
    Candle,
    CandleAggregator,
    as_decimal,
    ensure_ist,
    market_session_open,
    session_bounds,
)
from .storage import Store

logger = logging.getLogger(__name__)
INDIA_VIX_KEY = "NSE_INDEX|India VIX"


class UpstoxError(RuntimeError):
    pass


def _sdk() -> Any:
    try:
        import upstox_client
    except ImportError as exc:
        raise UpstoxError("upstox-python-sdk is missing. Run setup_windows.bat again.") from exc
    return upstox_client


class UpstoxGateway:
    def __init__(self, api_key: str, api_secret: str, redirect_url: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.redirect_url = redirect_url
        self.access_token: str | None = None
        self.user_name: str | None = None
        self.api_client: Any | None = None
        self._lock = threading.RLock()
        self._last_history_request = 0.0

    def login_url(self, state: str) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.api_key,
                "redirect_uri": self.redirect_url,
                "state": state,
            }
        )
        return f"https://api.upstox.com/v2/login/authorization/dialog?{query}"

    def authenticate(self, code: str) -> dict[str, Any]:
        try:
            response = requests.post(
                "https://api.upstox.com/v2/login/authorization/token",
                data={
                    "code": code,
                    "client_id": self.api_key,
                    "client_secret": self.api_secret,
                    "redirect_uri": self.redirect_url,
                    "grant_type": "authorization_code",
                },
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=20,
            )
            payload = response.json()
        except (requests.RequestException, requests.JSONDecodeError) as exc:
            raise UpstoxError(f"Upstox login failed: {exc}") from exc
        if not response.ok:
            raise UpstoxError(f"Upstox token exchange failed ({response.status_code}): {payload}")
        token = payload.get("access_token")
        if not token:
            raise UpstoxError("Upstox did not return an access token.")
        sdk = _sdk()
        configuration = sdk.Configuration()
        configuration.access_token = token
        with self._lock:
            self.access_token = str(token)
            self.api_client = sdk.ApiClient(configuration)
            self.user_name = str(payload.get("user_name") or "Upstox user")
        return payload

    @property
    def authenticated(self) -> bool:
        return self.api_client is not None and bool(self.access_token)

    def require_client(self) -> Any:
        if self.api_client is None:
            raise UpstoxError("Connect Upstox before loading market data.")
        return self.api_client

    def search_instruments(self, query: str, exchange: str = "NSE") -> list[dict[str, str]]:
        sdk = _sdk()
        response = sdk.InstrumentsApi(self.require_client()).search_instrument(
            query.strip(),
            exchanges=exchange.upper(),
            segments="EQ",
            instrument_types="EQ",
            records=20,
        )
        results: list[dict[str, str]] = []
        for raw in response.data or []:
            item = raw.to_dict() if hasattr(raw, "to_dict") else raw
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("trading_symbol") or item.get("tradingsymbol") or "")
            token = str(item.get("instrument_key") or item.get("instrument_token") or "")
            if symbol and token:
                results.append(
                    {
                        "exchange": exchange.upper(),
                        "tradingsymbol": symbol,
                        "name": str(item.get("name") or item.get("short_name") or ""),
                        "instrument_token": token,
                    }
                )
        return results

    def resolve_instrument(self, exchange: str, symbol: str) -> dict[str, str]:
        target = symbol.strip().upper()
        for item in self.search_instruments(target, exchange):
            if item["tradingsymbol"].upper() == target:
                return item
        raise UpstoxError(f"No exact {exchange.upper()}:{target} instrument was found.")

    def _throttle_history(self) -> None:
        with self._lock:
            delay = 0.18 - (time.monotonic() - self._last_history_request)
            if delay > 0:
                time.sleep(delay)
            self._last_history_request = time.monotonic()

    @staticmethod
    def _candles(token: str, rows: list[Any], since: datetime, until: datetime) -> list[Candle]:
        result: list[Candle] = []
        for row in rows:
            start = ensure_ist(datetime.fromisoformat(str(row[0]).replace("Z", "+00:00")))
            end = start + THREE_MINUTES
            if end <= until and end > since:
                result.append(
                    Candle(
                        token,
                        start,
                        end,
                        as_decimal(row[1]),
                        as_decimal(row[2]),
                        as_decimal(row[3]),
                        as_decimal(row[4]),
                    )
                )
        return sorted(result, key=lambda candle: candle.start)

    def historical_candles(self, token: str, since: datetime, until: datetime) -> list[Candle]:
        self._throttle_history()
        sdk = _sdk()
        response = sdk.HistoryV3Api(self.require_client()).get_historical_candle_data1(
            token,
            "minutes",
            "3",
            ensure_ist(until).date().isoformat(),
            ensure_ist(since).date().isoformat(),
        )
        return self._candles(
            token, response.data.candles or [], ensure_ist(since), ensure_ist(until)
        )

    def completed_intraday(self, token: str, since: datetime, until: datetime) -> list[Candle]:
        self._throttle_history()
        sdk = _sdk()
        response = sdk.HistoryV3Api(self.require_client()).get_intra_day_candle_data(
            token, "minutes", "3"
        )
        return self._candles(
            token, response.data.candles or [], ensure_ist(since), ensure_ist(until)
        )

    def previous_session_history(
        self, token: str, trading_date: date
    ) -> tuple[date, Decimal, list[Candle]]:
        since = datetime.combine(trading_date - timedelta(days=14), datetime.min.time(), IST)
        until = datetime.combine(trading_date - timedelta(days=1), datetime.max.time(), IST)
        candles = self.historical_candles(token, since, until)
        eligible = [candle for candle in candles if candle.start.date() < trading_date]
        if not eligible:
            raise UpstoxError("No previous completed session was returned by Upstox.")
        final = max(eligible, key=lambda candle: candle.start)
        return final.start.date(), final.close, candles

    def india_vix_previous_close(self, trading_date: date) -> tuple[date, Decimal]:
        reference_date, close, _ = self.previous_session_history(INDIA_VIX_KEY, trading_date)
        return reference_date, close

    def session_open_prices(self, tokens: list[str], trading_date: date) -> dict[str, Decimal]:
        """Fetch today's exchange open for up to 500 instruments in one request."""
        if not tokens:
            return {}
        sdk = _sdk()
        response = sdk.MarketQuoteV3Api(self.require_client()).get_market_quote_ohlc(
            "1d", instrument_key=",".join(tokens)
        )
        result: dict[str, Decimal] = {}
        for key, raw in (response.data or {}).items():
            item = raw.to_dict() if hasattr(raw, "to_dict") else raw
            if not isinstance(item, dict):
                continue
            token = str(item.get("instrument_token") or key)
            live = item.get("live_ohlc")
            if hasattr(live, "to_dict"):
                live = live.to_dict()
            if not isinstance(live, dict) or live.get("open") is None:
                continue
            raw_timestamp = live.get("ts")
            if raw_timestamp is None:
                continue
            candle_date = (
                datetime.fromtimestamp(int(raw_timestamp) / 1000, tz=UTC).astimezone(IST).date()
            )
            if candle_date != trading_date:
                continue
            price = as_decimal(live["open"])
            if price > 0:
                result[token] = price
        return result

    def option_minute_ohlc(self, tokens: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Return current/previous Upstox I1 candles for exact paper-entry pricing."""
        if not tokens:
            return {}
        sdk = _sdk()
        result: dict[str, list[dict[str, Any]]] = {}
        for index in range(0, len(tokens), 500):
            batch = tokens[index : index + 500]
            response = sdk.MarketQuoteV3Api(self.require_client()).get_market_quote_ohlc(
                "I1", instrument_key=",".join(batch)
            )
            for key, raw in (response.data or {}).items():
                item = raw.to_dict() if hasattr(raw, "to_dict") else raw
                if not isinstance(item, dict):
                    continue
                token = str(item.get("instrument_token") or key)
                candles: list[dict[str, Any]] = []
                for field in ("live_ohlc", "prev_ohlc"):
                    candle = item.get(field)
                    if hasattr(candle, "to_dict"):
                        candle = candle.to_dict()
                    if not isinstance(candle, dict):
                        continue
                    if candle.get("open") is None or candle.get("ts") is None:
                        continue
                    try:
                        opening = as_decimal(candle["open"])
                        start = datetime.fromtimestamp(
                            int(candle["ts"]) / 1000, tz=UTC
                        ).astimezone(IST)
                    except (ValueError, TypeError, ArithmeticError, OSError):
                        continue
                    if opening > 0:
                        candles.append({"start": start, "open": opening})
                if candles:
                    result[token] = candles
        return result


class UpstoxMonitor:
    def __init__(self, gateway: UpstoxGateway, store: Store) -> None:
        self.gateway = gateway
        self.store = store
        self.streamer: Any | None = None
        self.running = False
        self.connected = False
        self.last_error: str | None = None
        self._option_tokens: set[str] = set()
        self.paper = None
        self.last_tick_at: datetime | None = None
        self.connected_at: datetime | None = None
        self.error_count = 0
        self.processed_ticks = 0
        self.aggregator = CandleAggregator()
        self._candle_lock = threading.RLock()
        self._lock = threading.RLock()
        self._generation = 0
        self._desired: set[str] = set()
        self._subscribed: set[str] = set()
        self._recovering: set[str] = set()
        self._queued: dict[str, list[tuple[datetime, Decimal]]] = defaultdict(list)

    def start(self) -> None:
        sdk = _sdk()
        with self._lock:
            if self.streamer is not None:
                try:
                    self.streamer.disconnect()
                except Exception:
                    pass
            # An interrupted recovery belongs to the old socket generation.
            # Rebuild it after the new socket opens instead of leaving tokens
            # permanently labelled as recovering with no live feed.
            self._recovering.clear()
            self._queued.clear()
            streamer = sdk.MarketDataStreamerV3(self.gateway.require_client())
            streamer.auto_reconnect(True, 5, 50)
            streamer.on("open", self._on_open)
            streamer.on("message", self._on_message)
            streamer.on("close", self._on_close)
            streamer.on("error", self._on_error)
            streamer.on("reconnecting", self._on_reconnecting)
            streamer.on("autoReconnectStopped", self._on_reconnect_stopped)
            self.streamer = streamer
            self.running = True
            self.connected = False
            self.last_error = None
            self._generation += 1
            generation = self._generation
            threading.Thread(
                target=self._finalizer_loop,
                args=(generation,),
                name="trade-m2-candle-finalizer",
                daemon=True,
            ).start()
        streamer.connect()

    def stop(self) -> None:
        with self._lock:
            self.running = False
            self.connected = False
            self._generation += 1
            self._recovering.clear()
            self._queued.clear()
            streamer = self.streamer
            self.streamer = None
        if streamer is not None:
            try:
                streamer.disconnect()
            except Exception:
                pass

    def subscribe(self, tokens: list[str]) -> None:
        requested = {str(token) for token in tokens if token}
        if not requested:
            return
        with self._lock:
            self._desired.update(requested)
            connected = self.connected
        if connected:
            self._subscribe_batches(requested)
            self._begin_recovery(requested)

    def unsubscribe(self, tokens: list[str]) -> None:
        requested = {str(token) for token in tokens if token}
        if not requested:
            return
        with self._lock:
            self._desired.difference_update(requested)
            streamer = self.streamer
            connected = self.connected
            self._recovering.difference_update(requested)
            for token in requested:
                self._queued.pop(token, None)
        if streamer is None or not connected:
            return
        for index in range(0, len(requested), 500):
            batch = sorted(requested)[index : index + 500]
            try:
                streamer.unsubscribe(batch)
                with self._lock:
                    self._subscribed.difference_update(batch)
            except Exception as exc:
                self._record_error(f"Upstox unsubscribe failed: {exc}")

    def _subscribe_batches(self, tokens: set[str]) -> None:
        with self._lock:
            streamer = self.streamer
            connected = self.connected
        if streamer is None or not connected:
            return
        for index in range(0, len(tokens), 500):
            batch = sorted(tokens)[index : index + 500]
            try:
                streamer.subscribe(batch, "ltpc")
                with self._lock:
                    self._subscribed.update(batch)
            except Exception as exc:
                self._record_error(f"Upstox subscription failed: {exc}")

    def set_option_tokens(self, tokens: set[str]) -> None:
        with self._lock:
            old = self._option_tokens
            self._option_tokens = set(tokens)
            streamer, connected = self.streamer, self.connected
        if streamer is not None and connected:
            if old - tokens:
                streamer.unsubscribe(sorted(old - tokens))
            if tokens:
                streamer.subscribe(sorted(tokens), "full")

    def _on_open(self) -> None:
        with self._lock:
            self.connected = True
            self.connected_at = datetime.now(IST)
            self.last_error = None
            self._subscribed.clear()
            tokens = set(self._desired)
        self._subscribe_batches(tokens)
        with self._lock:
            options = sorted(self._option_tokens)
            streamer = self.streamer
        if options and streamer is not None:
            streamer.subscribe(options, "full")
        self._begin_recovery(tokens)

    def _begin_recovery(self, tokens: set[str]) -> None:
        with self._lock:
            tokens = tokens - self._recovering
            self._recovering.update(tokens)
            generation = self._generation
        if not tokens:
            return
        threading.Thread(
            target=self._recover,
            args=(tokens, generation),
            name="trade-m2-recovery",
            daemon=True,
        ).start()

    def _recover(self, tokens: set[str], generation: int) -> None:
        now = datetime.now(IST)
        opening, _ = session_bounds(now.date())
        try:
            open_prices = self.gateway.session_open_prices(sorted(tokens), now.date())
        except Exception as exc:
            open_prices = {}
            self._record_error(f"Session-open lookup failed; using candle fallback: {exc}")
        def recover_token(token: str) -> None:
            try:
                if generation != self._generation or not self.running:
                    return
                if token in open_prices:
                    self.store.initialize_opening(token, now.date(), open_prices[token])
                candles = self.gateway.completed_intraday(token, opening, now)
                for candle in candles:
                    if generation != self._generation or not self.running:
                        return
                    self.store.evaluate_recovery_candle(candle)
            except Exception as exc:
                self._record_error(f"Recovery failed for {token}: {exc}")
            finally:
                # Drain ticks received during recovery before exposing this
                # instrument to the live path, preserving their order.
                while generation == self._generation and self.running:
                    with self._lock:
                        queued = sorted(self._queued.pop(token, []), key=lambda item: item[0])
                        if not queued:
                            self._recovering.discard(token)
                            break
                    for timestamp, price in queued:
                        source = (
                            "LIVE"
                            if (datetime.now(IST) - timestamp).total_seconds() <= 45
                            else "RECOVERY"
                        )
                        self._process_tick(token, price, timestamp, source=source)
                with self._lock:
                    if generation == self._generation and not self.running:
                        self._recovering.discard(token)
                        self._queued.pop(token, None)

        # Recovery is per underlying; a small pool prevents the 200th stock
        # from waiting for 199 sequential network round trips.
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="trade-m3-recovery") as pool:
            list(pool.map(recover_token, sorted(tokens)))

    def _on_message(self, message: dict[str, Any]) -> None:
        received = datetime.now(IST)
        for token, feed in (message.get("feeds") or {}).items():
            try:
                if str(token) in self._option_tokens:
                    market = feed.get("fullFeed", {}).get("marketFF", {})
                    depth = market.get("marketLevel", {}).get("bidAskQuote", [])
                    stamp = message.get("currentTs")
                    ltpc = feed.get("ltpc") or market.get("ltpc")
                    if self.paper is not None and stamp:
                        minute_rows = market.get("marketOHLC", {}).get("ohlc", [])
                        for minute in minute_rows:
                            if minute.get("interval") != "I1":
                                continue
                            try:
                                minute_start = datetime.fromtimestamp(
                                    int(minute["ts"]) / 1000, tz=UTC
                                ).astimezone(IST)
                                minute_open = as_decimal(minute["open"])
                            except (KeyError, ValueError, TypeError, ArithmeticError, OSError):
                                continue
                            if minute_open > 0:
                                self.paper.on_minute_open(
                                    str(token), minute_open, minute_start, received
                                )
                            break
                        raw_ltt = ltpc.get("ltt") if ltpc else None
                        quote_time = datetime.fromtimestamp(
                            int(raw_ltt or stamp) / 1000, tz=UTC
                        ).astimezone(IST)
                        trade_price = as_decimal(ltpc["ltp"]) if ltpc and ltpc.get("ltp") else None
                        if depth:
                            self.paper.on_quote(
                                str(token), depth[0], quote_time, received, trade_price
                            )
                        elif trade_price is not None:
                            self.paper.on_quote(str(token), None, quote_time, received, trade_price)
                    continue
                ltpc = feed.get("ltpc") or feed.get("fullFeed", {}).get("marketFF", {}).get("ltpc")
                if not ltpc:
                    continue
                price = as_decimal(ltpc["ltp"])
                raw_timestamp = ltpc.get("ltt")
                timestamp = (
                    datetime.fromtimestamp(int(raw_timestamp) / 1000, tz=UTC).astimezone(IST)
                    if raw_timestamp
                    else received
                )
                # LTP may be replayed by the feed with an old last-trade time.
                # It must not create a new crossing at today's current time.
                if not -5 <= (received - timestamp).total_seconds() <= 45:
                    continue
                if getattr(self, "sectors", None) and raw_timestamp:
                    self.sectors.tick(str(token), price, ltpc.get("cp"), timestamp)
                with self._lock:
                    self.last_tick_at = received
                    if token in self._recovering:
                        queue = self._queued[token]
                        queue.append((timestamp, price))
                        if len(queue) > 5000:
                            del queue[: len(queue) - 5000]
                        continue
                self._process_tick(str(token), price, timestamp)
            except Exception as exc:
                self._record_error(f"Ignored invalid Upstox tick: {exc}")

    def _process_tick(
        self, token: str, price: Decimal, timestamp: datetime, *, source: str = "LIVE"
    ) -> None:
        self.store.evaluate_tick(token, price, timestamp, source=source)
        with self._candle_lock:
            completed = self.aggregator.add_tick(token, price, timestamp)
        self.store.save_candles(completed)
        with self._lock:
            self.processed_ticks += 1

    def _finalizer_loop(self, generation: int) -> None:
        while self.running and generation == self._generation:
            try:
                with self._candle_lock:
                    completed = self.aggregator.finalize_due(
                        datetime.now(IST), delay_seconds=3
                    )
                self.store.save_candles(completed)
            except Exception as exc:
                self._record_error(f"Candle finalizer recovered from: {exc}")
            time.sleep(0.5)

    def _on_close(self, code: int, reason: str) -> None:
        with self._lock:
            self.connected = False
            self._subscribed.clear()
            if self.running:
                self.last_error = f"Upstox WebSocket closed ({code}): {reason}"

    def _on_error(self, error: Any) -> None:
        self._record_error(f"Upstox WebSocket error: {error}")

    def _on_reconnecting(self, message: str) -> None:
        with self._lock:
            self.last_error = str(message)

    def _on_reconnect_stopped(self, message: str) -> None:
        with self._lock:
            self.connected = False
        self._record_error(f"Upstox automatic reconnect stopped: {message}")

    def _record_error(self, message: str) -> None:
        with self._lock:
            self.error_count += 1
            self.last_error = message
        logger.warning(message)

    def status(self) -> dict[str, Any]:
        with self._lock:
            now = datetime.now(IST)
            anchor = self.last_tick_at or self.connected_at
            age = (now - anchor).total_seconds() if anchor else None
            stale = bool(
                self.running
                and self.connected
                and self._desired
                and market_session_open(now)
                and (age is None or age > 45)
            )
            return {
                "running": self.running,
                "connected": self.connected,
                "stale": stale,
                "last_error": self.last_error,
                "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
                "last_tick_age_seconds": round(age, 1) if age is not None else None,
                "desired_subscriptions": len(self._desired),
                "active_subscriptions": len(self._subscribed),
                "recovering_subscriptions": len(self._recovering),
                "queued_ticks": sum(len(values) for values in self._queued.values()),
                "processed_ticks": self.processed_ticks,
                "error_count": self.error_count,
            }
