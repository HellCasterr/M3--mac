from __future__ import annotations

import asyncio
import csv
import io
import json
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from fastapi import FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .assistant import (AssistantError, ChatRequest, IndexQuotes, MAX_IMAGE_BYTES,
                        PositionNotebook, PositionSet, answer_question, extract_positions)
from .config import Settings, get_settings
from .domain import IST, market_session_open
from .history import AlertHistory, HistoricalData, HistoryError, HistoryQuery
from .nifty200 import Nifty200Service
from .paper import PaperEngine, PaperSettings, Ticket
from .replay import ReplayJobs, ReplayRequest
from .sectors import SectorMonitor
from .services import MarketMovementService, NiftyLoadJobs, create_daily_rule
from .storage import Store
from .telegram import TelegramNotifier
from .upstox import UpstoxError, UpstoxGateway, UpstoxMonitor


class RuleInput(BaseModel):
    tradingsymbol: str
    percentage: Decimal


class NiftyLoadInput(BaseModel):
    percentage: Decimal | None = None


class ActiveInput(BaseModel):
    active: bool


def _percentage(value: Decimal) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise HTTPException(status_code=400, detail="Enter a valid percentage.") from exc
    if result <= 0 or result > 10:
        raise HTTPException(
            status_code=400, detail="Percentage must be greater than 0 and at most 10."
        )
    return result


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    store = Store(settings.database_path)
    gateway = UpstoxGateway(
        settings.upstox_api_key,
        settings.upstox_api_secret,
        settings.upstox_redirect_url,
    )
    monitor = UpstoxMonitor(gateway, store)
    nifty200 = Nifty200Service(settings.database_path.parent / "nifty200.json")
    sectors = SectorMonitor(store, nifty200)
    monitor.sectors = sectors
    store.sector_context = sectors.context
    movement = MarketMovementService(gateway)
    jobs = NiftyLoadJobs(gateway, monitor, store, nifty200)
    paper = PaperEngine(store, gateway, monitor)
    monitor.paper = paper
    alert_history = AlertHistory(store)
    historical = HistoricalData(store, gateway)
    replay = ReplayJobs(store, gateway, historical)
    telegram = TelegramNotifier(settings, store)
    assistant_notebook = PositionNotebook(store)
    indices = IndexQuotes(gateway)
    oauth_states: dict[str, datetime] = {}
    oauth_lock = threading.RLock()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        paper.start()
        telegram.start()
        try:
            yield
        finally:
            paper.stop()
            monitor.stop()
            telegram.stop()

    app = FastAPI(
        title="Trade M3",
        description="Upstox-only Case 2 crossing alerts for the Nifty 200.",
        version="1.0.0",
        docs_url="/api/docs",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def no_cache_local_terminal(request: Request, call_next):
        response = await call_next(request)
        if request.url.path in {"/", "/terminal", "/alerts-history", "/paper-guide", "/replay", "/assistant"} or (
            request.url.path.startswith("/static/")
        ):
            response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    app.state.store = store
    app.state.gateway = gateway
    app.state.monitor = monitor
    app.state.nifty200 = nifty200
    app.state.jobs = jobs
    app.state.telegram = telegram
    app.state.paper = paper
    app.state.historical = historical
    app.state.replay = replay
    app.state.assistant_notebook = assistant_notebook
    app.state.indices = indices

    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/assistant", include_in_schema=False)
    def assistant_page() -> FileResponse:
        return FileResponse(static_dir / "assistant.html")

    @app.get("/terminal", include_in_schema=False)
    def terminal() -> FileResponse:
        return FileResponse(static_dir / "terminal.html")

    @app.get("/replay", include_in_schema=False)
    def replay_page() -> FileResponse:
        return FileResponse(static_dir / "replay.html")

    @app.post("/api/replay")
    def replay_start(payload: ReplayRequest, request: Request):
        paper_request(request)
        try:
            return replay.start(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/replay/current")
    def replay_current():
        try:
            return replay.current()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="No replay job exists.") from exc

    @app.get("/api/replay/{job_id}")
    def replay_status(job_id: str):
        try:
            return replay.snapshot(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Replay not found.") from exc

    @app.get("/paper-guide", include_in_schema=False)
    def paper_guide() -> FileResponse:
        return FileResponse(
            static_dir.parent.parent / "docs" / "PAPER_TRADING_DESIGN.md", media_type="text/plain"
        )

    @app.get("/api/paper")
    def paper_snapshot():
        return paper.snapshot()

    @app.post("/api/feed/reconnect")
    def reconnect_feed(request: Request):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(status_code=403, detail="Use the local Trade M3 terminal.")
        if not gateway.authenticated:
            raise HTTPException(status_code=401, detail="Connect Upstox from Stock alerts first.")
        try:
            monitor.start()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Feed reconnect failed: {exc}") from exc
        return {"status": "CONNECTING", "monitor": monitor.status()}

    def paper_request(request: Request):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(status_code=403, detail="Use the local Trade M3 terminal.")
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(status_code=415, detail="Use application/json.")

    @app.get("/api/assistant/positions")
    def assistant_positions():
        return {"positions": assistant_notebook.list(), "source": "manually reviewed local data"}

    @app.post("/api/assistant/positions")
    def assistant_save_positions(payload: PositionSet, request: Request):
        paper_request(request)
        return {"positions": assistant_notebook.replace(payload.positions), "saved": True}

    @app.delete("/api/assistant/positions")
    def assistant_clear_positions(request: Request):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(status_code=403, detail="Use the local application.")
        if request.headers.get("x-m3-action") != "local":
            raise HTTPException(status_code=403, detail="Missing local action header.")
        assistant_notebook.clear()
        return {"positions": [], "saved": True}

    @app.post("/api/assistant/extract")
    async def assistant_extract(request: Request, file: UploadFile = File(...)):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(status_code=403, detail="Use the local application.")
        if request.headers.get("x-m3-action") != "local":
            raise HTTPException(status_code=403, detail="Missing local action header.")
        try:
            screenshot = await file.read(MAX_IMAGE_BYTES + 1)
            return await asyncio.to_thread(extract_positions, screenshot)
        except AssistantError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        finally:
            await file.close()

    @app.get("/api/assistant/indices")
    def assistant_indices():
        try:
            return indices.snapshot()
        except AssistantError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    @app.post("/api/assistant/chat")
    def assistant_chat(payload: ChatRequest, request: Request):
        paper_request(request)
        try:
            market = indices.snapshot()
            return answer_question(
                payload.message, payload.history, assistant_notebook.list(), market
            )
        except AssistantError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    @app.put("/api/paper/settings")
    def paper_settings(payload: PaperSettings, request: Request):
        paper_request(request)
        try:
            return paper.set_settings(payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/paper/alerts/{event_id}")
    def paper_submit(event_id: int, payload: Ticket, request: Request):
        paper_request(request)
        try:
            return paper.submit(event_id, payload.model_dump(exclude_none=True))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/paper/alerts/{event_id}/preview")
    def paper_preview(event_id: int, payload: Ticket, request: Request):
        paper_request(request)
        try:
            return paper.preview(event_id, payload.model_dump(exclude_none=True))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/paper/trades/{trade_id}/{action}")
    def paper_action(trade_id: int, action: str, request: Request):
        paper_request(request)
        try:
            return paper.action(trade_id, action)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/paper/export")
    def paper_export():
        data = paper.snapshot()
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(
            [
                "trade_id",
                "alert_id",
                "stock",
                "contract",
                "expiry",
                "strike",
                "kind",
                "state",
                "quantity",
                "remaining",
                "entry",
                "entry_at",
                "entry_basis",
                "reason",
                "realised_pnl",
                "fees",
                "exit_reason",
                "uncertain",
                "created_at",
                "filled_at",
                "closed_at",
                "mae_percent",
                "mfe_percent",
                "settings",
                "sector_snapshot",
            ]
        )
        for t in data["trades"]:
            v = t["candidate"]
            row = [
                t["id"],
                t["event_id"],
                t["symbol"],
                v["symbol"],
                v["expiry"],
                v["strike"],
                v["kind"],
                t["state"],
                t["quantity"],
                t["remaining"],
                t.get("entry"),
                t.get("entry_at"),
                t.get("entry_basis"),
                t.get("reason"),
                t["realised"],
                t["fees"],
                t.get("exit_reason"),
                t["uncertain"],
                t["created_at"],
                t.get("filled_at"),
                t.get("closed_at"),
                t.get("mae_percent"),
                t.get("mfe_percent"),
                json.dumps(t["settings"]),
                json.dumps(t["sector_context"]),
            ]
            writer.writerow(
                [
                    ("'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v)
                    for v in row
                ]
            )
        return Response(
            out.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=trade_m3_paper_trades.csv"},
        )

    @app.get("/auth/upstox/login", include_in_schema=False)
    def login() -> RedirectResponse:
        if not settings.upstox_configured:
            return RedirectResponse(
                "/?error=" + quote("Add UPSTOX_API_KEY and UPSTOX_API_SECRET to .env.")
            )
        state = secrets.token_urlsafe(32)
        with oauth_lock:
            oauth_states.clear()
            oauth_states[state] = datetime.now(IST) + timedelta(minutes=10)
        return RedirectResponse(gateway.login_url(state))

    @app.get("/auth/upstox/callback", include_in_schema=False)
    def callback(code: str | None = None, state: str | None = None) -> RedirectResponse:
        with oauth_lock:
            expires = oauth_states.pop(state, None) if state else None
        if not state or expires is None or expires < datetime.now(IST):
            return RedirectResponse(
                "/?error=" + quote("Upstox login state was missing or expired. Try again.")
            )
        if not code:
            return RedirectResponse("/?error=" + quote("Upstox did not return a login code."))
        try:
            gateway.authenticate(code)
            monitor.subscribe(
                [rule["instrument_token"] for rule in store.active_rules(datetime.now(IST).date())]
            )
            monitor.start()
        except Exception as exc:
            return RedirectResponse("/?error=" + quote(f"Upstox login failed: {exc}"))
        return RedirectResponse("/?login=upstox")

    @app.get("/alerts-history", include_in_schema=False)
    def history_page():
        return FileResponse(static_dir / "history.html")

    @app.get("/api/review/dates")
    def history_dates():
        return {"dates": alert_history.dates(), "timezone": "Asia/Kolkata", "storage": "local"}

    @app.get("/api/review/alerts")
    def history_alerts(trading_date: date, order: Literal["asc", "desc"] = "desc"):
        rows = alert_history.rows(trading_date, order)
        return {
            "trading_date": str(trading_date),
            "count": len(rows),
            "alerts": rows,
            "timezone": "Asia/Kolkata",
        }

    @app.get("/api/review/export")
    def export_review(trading_date: date | None = None, order: Literal["asc", "desc"] = "desc"):
        suffix = str(trading_date) if trading_date else "all_dates"
        return Response(
            alert_history.export(trading_date, order),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=trade_m3_alerts_{suffix}.csv"},
        )

    @app.get("/api/history/contracts")
    def historical_contracts(underlying_key: str, expiry: date):
        try:
            return historical.contracts(underlying_key, expiry)
        except HistoryError as exc:
            raise HTTPException(
                status_code=exc.status, detail={"code": exc.code, "message": exc.message}
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/history/candles")
    def historical_candles(payload: HistoryQuery):
        try:
            return historical.candles(payload.model_dump())
        except HistoryError as exc:
            raise HTTPException(
                status_code=exc.status, detail={"code": exc.code, "message": exc.message}
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/sectors")
    def sector_dashboard() -> dict[str, Any]:
        return sectors.dashboard(datetime.now(IST))

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        now = datetime.now(IST)
        rules = store.daily_rules(now.date())
        return {
            "configured": settings.upstox_configured,
            "authenticated": gateway.authenticated,
            "user_name": gateway.user_name,
            "market_open": market_session_open(now),
            "server_time": now.isoformat(),
            "rule_count": len(rules),
            "active_rule_count": sum(rule["active"] for rule in rules),
            "monitor": monitor.status(),
            "load_job": jobs.active(),
            "telegram": telegram.status(),
        }

    @app.get("/api/telegram/status")
    def telegram_status() -> dict[str, Any]:
        return telegram.status()

    @app.post("/api/telegram/test", status_code=202)
    def telegram_test(request: Request) -> dict[str, Any]:
        # Only the local dashboard should initiate a notification test.
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(
                status_code=403, detail="Open the test from the Trade M3 dashboard."
            )
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(status_code=415, detail="Use an application/json request.")
        try:
            test_id = telegram.queue_test()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"test_id": test_id, "recipient_count": len(telegram.chat_ids)}

    @app.get("/api/telegram/tests/{test_id}")
    def telegram_test_status(test_id: str) -> dict[str, Any]:
        result = telegram.status(test_id)
        if not any(recipient["counts"] for recipient in result["recipients"]):
            raise HTTPException(status_code=404, detail="Telegram test not found.")
        return result

    @app.get("/api/market-movement")
    def market_movement() -> dict[str, Any]:
        if not gateway.authenticated:
            raise HTTPException(status_code=401, detail="Connect Upstox first.")
        try:
            return movement.get(datetime.now(IST).date())
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"India VIX lookup failed: {exc}") from exc

    @app.get("/api/nifty200")
    def constituents(refresh: bool = False) -> dict[str, Any]:
        try:
            result = nifty200.get(force_refresh=refresh)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {
            "symbols": result.symbols,
            "count": len(result.symbols),
            "source": result.source,
        }

    @app.post("/api/nifty200/load", status_code=202)
    def load_nifty200(payload: NiftyLoadInput) -> dict[str, Any]:
        if not gateway.authenticated:
            raise HTTPException(status_code=401, detail="Connect Upstox first.")
        if payload.percentage is None:
            percentage = Decimal(movement.get(datetime.now(IST).date())["moving_percentage"])
        else:
            percentage = _percentage(payload.percentage)
        return jobs.start(percentage)

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Load job not found.")
        return job

    @app.get("/api/instruments/search")
    def search(q: str = Query(min_length=1, max_length=40)) -> list[dict[str, str]]:
        if not gateway.authenticated:
            raise HTTPException(status_code=401, detail="Connect Upstox first.")
        try:
            return gateway.search_instruments(q, "NSE")
        except UpstoxError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/rules", status_code=201)
    def add_rule(payload: RuleInput) -> dict[str, Any]:
        if not gateway.authenticated:
            raise HTTPException(status_code=401, detail="Connect Upstox first.")
        symbol = payload.tradingsymbol.strip().upper()
        if not symbol:
            raise HTTPException(status_code=400, detail="Trading symbol is required.")
        try:
            rule = create_daily_rule(
                gateway,
                store,
                symbol=symbol,
                percentage=_percentage(payload.percentage),
                trading_date=datetime.now(IST).date(),
            )
            monitor.subscribe([rule["instrument_token"]])
            return rule
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Could not add {symbol}: {exc}") from exc

    @app.get("/api/rules")
    def rules() -> list[dict[str, Any]]:
        return store.daily_rules(datetime.now(IST).date())

    @app.patch("/api/rules/{rule_id}")
    def set_rule_status(rule_id: int, payload: ActiveInput) -> dict[str, Any]:
        matching = [
            rule for rule in store.daily_rules(datetime.now(IST).date()) if rule["id"] == rule_id
        ]
        if not matching:
            raise HTTPException(status_code=404, detail="Rule not found.")
        if not store.set_rule_active(rule_id, payload.active):
            raise HTTPException(status_code=404, detail="Rule not found.")
        if payload.active:
            monitor.subscribe([matching[0]["instrument_token"]])
        else:
            monitor.unsubscribe([matching[0]["instrument_token"]])
        return {"id": rule_id, "active": payload.active}

    @app.post("/api/rules/bulk-status")
    def set_bulk_status(payload: ActiveInput) -> dict[str, Any]:
        today = datetime.now(IST).date()
        tokens = [rule["instrument_token"] for rule in store.active_rules(today)]
        affected = store.set_all_active(today, payload.active)
        if payload.active:
            monitor.subscribe([rule["instrument_token"] for rule in store.active_rules(today)])
        else:
            monitor.unsubscribe(tokens)
        return {"affected": affected, "active": payload.active}

    @app.get("/api/events")
    def events(
        after_id: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return store.events_after(after_id, limit, datetime.now(IST).date())

    @app.get("/api/events/snapshot")
    def event_snapshot() -> dict[str, Any]:
        return {
            "events": store.events_after(0, 500, datetime.now(IST).date()),
            "latest_id": store.latest_event_id(),
            "server_time": datetime.now(IST).isoformat(),
        }

    @app.get("/api/events/stream")
    async def event_stream(
        request: Request,
        after_id: int = Query(default=0, ge=0),
        last_event_id: int | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        cursor = max(after_id, last_event_id or 0)

        async def generate():
            nonlocal cursor
            loop = asyncio.get_running_loop()
            last_heartbeat = loop.time()
            yield "retry: 2000\nevent: ready\ndata: {}\n\n"
            while not await request.is_disconnected():
                current = store.events_after(cursor, 100, datetime.now(IST).date())
                if current:
                    for event in current:
                        cursor = max(cursor, int(event["id"]))
                        payload = json.dumps(event, separators=(",", ":"))
                        yield f"id: {event['id']}\nevent: alert\ndata: {payload}\n\n"
                    last_heartbeat = loop.time()
                    continue
                if loop.time() - last_heartbeat >= 15:
                    yield ": keepalive\n\n"
                    last_heartbeat = loop.time()
                await asyncio.sleep(0.75)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


app = build_app()
