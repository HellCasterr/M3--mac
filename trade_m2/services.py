from __future__ import annotations

import threading
import uuid
from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any

from .domain import IST
from .nifty200 import Nifty200Service
from .storage import Store
from .upstox import UpstoxGateway, UpstoxMonitor

VIX_DIVISOR = Decimal("8.54400")


def moving_percentage(vix_close: Decimal) -> Decimal:
    if vix_close <= 0:
        raise ValueError("India VIX close must be positive.")
    return (vix_close / VIX_DIVISOR).quantize(
        Decimal("0.0001"), rounding=ROUND_DOWN
    )


class MarketMovementService:
    def __init__(self, gateway: UpstoxGateway) -> None:
        self.gateway = gateway
        self._lock = threading.RLock()
        self._cache: dict[date, dict[str, Any]] = {}

    def get(self, trading_date: date) -> dict[str, Any]:
        with self._lock:
            if trading_date in self._cache:
                return self._cache[trading_date]
        reference_date, close = self.gateway.india_vix_previous_close(trading_date)
        result = {
            "trading_date": trading_date.isoformat(),
            "reference_date": reference_date.isoformat(),
            "vix_close": str(close),
            "moving_percentage": str(moving_percentage(close)),
            "moving_percentage_display": f"{moving_percentage(close)}%",
            "divisor": str(VIX_DIVISOR),
        }
        with self._lock:
            self._cache[trading_date] = result
        return result


def create_daily_rule(
    gateway: UpstoxGateway,
    store: Store,
    *,
    symbol: str,
    percentage: Decimal,
    trading_date: date,
) -> dict[str, Any]:
    instrument = gateway.resolve_instrument("NSE", symbol)
    reference_date, reference_close, history = gateway.previous_session_history(
        instrument["instrument_token"], trading_date
    )
    store.save_candles(history[-300:])
    return store.upsert_rule(
        trading_date=trading_date,
        exchange="NSE",
        tradingsymbol=instrument["tradingsymbol"],
        instrument_token=instrument["instrument_token"],
        percentage=percentage,
        reference_date=reference_date,
        reference_close=reference_close,
    )


class NiftyLoadJobs:
    def __init__(
        self,
        gateway: UpstoxGateway,
        monitor: UpstoxMonitor,
        store: Store,
        nifty200: Nifty200Service,
    ) -> None:
        self.gateway = gateway
        self.monitor = monitor
        self.store = store
        self.nifty200 = nifty200
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._active_job: str | None = None

    def start(self, percentage: Decimal) -> dict[str, Any]:
        with self._lock:
            if self._active_job:
                existing = self._jobs.get(self._active_job)
                if existing and existing["status"] == "RUNNING":
                    return dict(existing)
            job_id = uuid.uuid4().hex[:12]
            job = {
                "id": job_id,
                "status": "RUNNING",
                "total": 200,
                "processed": 0,
                "created": 0,
                "failed": 0,
                "current_symbol": None,
                "percentage": str(percentage),
                "source": None,
                "failures": [],
                "started_at": datetime.now(IST).isoformat(),
                "finished_at": None,
            }
            self._jobs[job_id] = job
            self._active_job = job_id
        threading.Thread(
            target=self._run,
            args=(job_id, percentage),
            name="trade-m2-nifty200-loader",
            daemon=True,
        ).start()
        return dict(job)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def active(self) -> dict[str, Any] | None:
        with self._lock:
            return self.get(self._active_job) if self._active_job else None

    def _run(self, job_id: str, percentage: Decimal) -> None:
        try:
            constituent_result = self.nifty200.get()
            self._update(job_id, source=constituent_result.source)
            batch: list[str] = []
            today = datetime.now(IST).date()
            for symbol in constituent_result.symbols:
                self._update(job_id, current_symbol=symbol)
                try:
                    rule = create_daily_rule(
                        self.gateway,
                        self.store,
                        symbol=symbol,
                        percentage=percentage,
                        trading_date=today,
                    )
                    batch.append(rule["instrument_token"])
                    self._increment(job_id, created=True)
                    if len(batch) >= 20:
                        self.monitor.subscribe(batch)
                        batch.clear()
                except Exception as exc:
                    self._increment(job_id, created=False, symbol=symbol, error=str(exc))
            if batch:
                self.monitor.subscribe(batch)
            self._update(
                job_id,
                status="COMPLETED",
                current_symbol=None,
                finished_at=datetime.now(IST).isoformat(),
            )
        except Exception as exc:
            self._update(
                job_id,
                status="FAILED",
                current_symbol=None,
                finished_at=datetime.now(IST).isoformat(),
                fatal_error=str(exc),
            )

    def _increment(
        self,
        job_id: str,
        *,
        created: bool,
        symbol: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job["processed"] += 1
            if created:
                job["created"] += 1
            else:
                job["failed"] += 1
                job["failures"].append({"symbol": symbol, "error": error})

    def _update(self, job_id: str, **values: Any) -> None:
        with self._lock:
            self._jobs[job_id].update(values)
