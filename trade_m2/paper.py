"""Persistent paper-only execution. No broker order APIs are used."""

from __future__ import annotations

import json
import threading
from datetime import datetime, time, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .domain import IST, market_session_open
from .options import OptionResearch, dec, fee, price_tick


class PaperSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    auto: bool = False
    initial_cash: float = Field(100000, ge=1000, le=100000000)
    budget: float = Field(10000, gt=0, le=10000000)
    lots: int = Field(0, ge=0, le=100)
    target_percent: float = Field(10, gt=0, le=100)
    stop_percent: float = Field(5, gt=0, lt=100)
    hold_minutes: int = Field(30, ge=1, le=360)
    entry_timeout: int = Field(240, ge=5, le=240)
    latency_ms: int = Field(0, ge=0, le=10000)
    quote_ttl: int = Field(5, ge=1, le=15)
    slippage_percent: float = Field(0, ge=0, le=5)
    fee_fixed: float = Field(0, ge=0, le=1000)
    fee_percent: float = Field(0, ge=0, le=5)
    max_positions: int = Field(5, ge=1, le=50)
    max_sector_positions: int = Field(2, ge=1, le=50)
    daily_loss_limit: float = Field(2000, gt=0, le=10000000)
    max_spread_percent: float = Field(1, gt=0, le=10)
    min_expiry_days: int = Field(0, ge=0, le=30)
    aligned_only: bool = False

    @model_validator(mode="after")
    def validate_budget(self):
        if self.budget > self.initial_cash:
            raise ValueError("Per-trade budget cannot exceed initial virtual cash.")
        return self


class Ticket(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    budget: float | None = Field(None, gt=0, le=10000000)
    lots: int | None = Field(None, ge=0, le=100)
    target_percent: float | None = Field(None, gt=0, le=100)
    stop_percent: float | None = Field(None, gt=0, lt=100)
    hold_minutes: int | None = Field(None, ge=1, le=360)


def iso(now):
    return now.astimezone(IST).isoformat()


def dt(value):
    return datetime.fromisoformat(value).astimezone(IST)


def option_summary(context):
    if not context:
        return "Option selection pending — no executable recommendation yet."
    c = json.loads(context) if isinstance(context, str) else context
    if c.get("status") != "READY":
        return "Option unavailable: " + c.get("reason", "no eligible contract")
    v = c["candidate"]
    return (
        f"Candidate BUY {v['strike']} {v['kind']} | expiry {v['expiry']} | "
        f"lot {v['lot_size']} | LTP sizing reference ₹{v.get('ltp', 'unavailable')} | "
        f"bid/ask ₹{v['bid']}/₹{v['ask']} | "
        f"delta {v['delta']} | {v['reason']} (paper candidate, not a fill)"
    )


class PaperEngine:
    def __init__(self, store, gateway, monitor, clock=None):
        self.store, self.gateway, self.monitor = store, gateway, monitor
        self.clock = clock or (lambda: datetime.now(IST))
        self.research = OptionResearch(gateway)
        self.lock = threading.RLock()
        self.quotes = {}
        self.stop_event = threading.Event()
        self.threads = []
        self.last_error = None
        self.synced = set()
        self._last_entry_poll_at = None
        with store.connection() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS paper_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS paper_decisions (
                    event_id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER UNIQUE NOT NULL,
                    payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS paper_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id INTEGER, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS paper_fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id INTEGER, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS paper_quotes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id INTEGER, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS paper_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, payload TEXT NOT NULL);
            """)
            columns = {r[1] for r in c.execute("PRAGMA table_info(events)")}
            if "option_context" not in columns:
                c.execute("ALTER TABLE events ADD COLUMN option_context TEXT")
            c.execute(
                "INSERT OR IGNORE INTO paper_meta VALUES (?, ?)",
                ("settings", json.dumps(PaperSettings().model_dump())),
            )
            if c.execute("SELECT 1 FROM paper_meta WHERE key='simple_paper_v1'").fetchone() is None:
                saved = json.loads(
                    c.execute("SELECT value FROM paper_meta WHERE key='settings'").fetchone()[0]
                )
                saved.update(
                    target_percent=10,
                    stop_percent=5,
                    latency_ms=0,
                    slippage_percent=0,
                    fee_fixed=0,
                    fee_percent=0,
                    aligned_only=False,
                )
                c.execute(
                    "UPDATE paper_meta SET value=? WHERE key='settings'",
                    (json.dumps(PaperSettings.model_validate(saved).model_dump()),),
                )
                c.execute("INSERT INTO paper_meta VALUES ('simple_paper_v1','1')")
            c.execute(
                "INSERT OR IGNORE INTO paper_meta VALUES (?, ?)", ("auto_since", iso(self.clock()))
            )
            first_start = (
                c.execute("SELECT 1 FROM paper_meta WHERE key='cursor'").fetchone() is None
            )
            c.execute(
                "INSERT OR IGNORE INTO paper_meta VALUES (?, ?)",
                ("cursor", str(store.latest_event_id())),
            )
            if first_start:
                c.execute(
                    "UPDATE events SET option_context=? WHERE option_context IS NULL",
                    (
                        json.dumps(
                            {
                                "status": "UNAVAILABLE",
                                "reason": "Alert predates paper-engine activation.",
                            }
                        ),
                    ),
                )
        self.store.option_enrichment = True
        self._recover()

    @staticmethod
    def _rows(c, table):
        return [
            dict(json.loads(r["payload"]), id=r["id"])
            for r in c.execute(f"SELECT id, payload FROM {table} ORDER BY id")
        ]

    @staticmethod
    def _save(c, table, obj):
        c.execute(f"UPDATE {table} SET payload=? WHERE id=?", (json.dumps(obj), obj["id"]))

    def _audit(self, c, message, now):
        c.execute(
            "INSERT INTO paper_audit(at,payload) VALUES (?,?)", (iso(now), json.dumps(message))
        )

    def settings(self):
        with self.store.connection() as c:
            return json.loads(
                c.execute("SELECT value FROM paper_meta WHERE key='settings'").fetchone()[0]
            )

    def set_settings(self, value):
        settings = PaperSettings.model_validate(value).model_dump()
        with self.lock, self.store.connection() as c:
            old = json.loads(
                c.execute("SELECT value FROM paper_meta WHERE key='settings'").fetchone()[0]
            )
            if (
                settings["initial_cash"] != old["initial_cash"]
                and c.execute("SELECT 1 FROM paper_trades LIMIT 1").fetchone()
            ):
                raise ValueError("Starting cash is locked after the first paper trade.")
            if settings["auto"] and not old["auto"]:
                c.execute(
                    "UPDATE paper_meta SET value=? WHERE key='auto_since'", (iso(self.clock()),)
                )
            c.execute("UPDATE paper_meta SET value=? WHERE key='settings'", (json.dumps(settings),))
            self._audit(c, {"action": "SETTINGS", "settings": settings}, self.clock())
        return settings

    def _recover(self):
        with self.lock, self.store.connection() as c:
            for t in self._rows(c, "paper_trades"):
                if t["state"] == "PENDING":
                    self._cancel(c, t, "CANCELLED", "Application restart", self.clock())
                elif t["remaining"]:
                    t["uncertain"] = True
                    self._save(c, "paper_trades", t)

    def start(self):
        if any(t.is_alive() for t in self.threads):
            return
        self.stop_event.clear()
        self.threads = [
            threading.Thread(target=self._selection_loop, daemon=True, name="paper-selection"),
            threading.Thread(target=self._timer_loop, daemon=True, name="paper-watchdog"),
        ]
        for t in self.threads:
            t.start()

    def stop(self):
        self.stop_event.set()
        for t in self.threads:
            t.join(timeout=10)

    def _selection_loop(self):
        while not self.stop_event.is_set():
            try:
                # Recovered alerts need an audit decision, but must not hold up a
                # fresh LIVE alert behind one sleep per historical row.
                if self.discover_one():
                    continue
            except Exception:
                self.last_error = (
                    "Option discovery interrupted; retrying. No real orders are possible."
                )
            self.stop_event.wait(0.5)

    def _timer_loop(self):
        while not self.stop_event.is_set():
            try:
                self.sync_subscriptions()
                self.poll_pending_entries()
                self.advance()
            except Exception:
                self.last_error = (
                    "Paper watchdog interrupted; inspect feed and unresolved positions."
                )
            self.stop_event.wait(0.5)

    def discover_one(self):
        now = self.clock()
        with self.store.connection() as c:
            cursor = int(c.execute("SELECT value FROM paper_meta WHERE key='cursor'").fetchone()[0])
            row = c.execute(
                """SELECT e.*, r.instrument_token, r.tradingsymbol FROM events e
                JOIN rules r ON r.id=e.rule_id WHERE e.id>? ORDER BY e.id LIMIT 1""",
                (cursor,),
            ).fetchone()
        if row is None:
            return False
        event = dict(row)
        settings = self.settings()
        decision = {
            "event_id": event["id"],
            "event_time": event["event_time"],
            "detected_at": event["created_at"],
            "symbol": event["tradingsymbol"],
            "trade_side": event["trade_side"],
            "source": event["source"],
            "sector_context": json.loads(event["sector_context"] or "{}"),
            "settings": settings,
            "at": iso(now),
            "status": "UNAVAILABLE",
        }
        try:
            detected = dt(event["created_at"])
            signal_age_at_detection = (detected - dt(event["event_time"])).total_seconds()
            if event["source"] == "RECOVERY" and not 0 <= signal_age_at_detection <= 240:
                raise ValueError("Historical recovery alert; no new paper order.")
            if not market_session_open(now):
                raise ValueError("Market session closed.")
            if event["source"] == "RECOVERY" and getattr(self.monitor, "sectors", None):
                # The event keeps its original historical sector snapshot. An
                # optional alignment filter uses the current, separately timed
                # market snapshot for a decision made now.
                decision["sector_context"] = self.monitor.sectors.context(
                    event["tradingsymbol"], event["trade_side"], now, "LIVE"
                )
            candidate = self.research.select(
                event["instrument_token"], event["trade_side"], now.date(), settings
            )
            decision.update(status="READY", candidate=candidate, quote_at=iso(self.clock()))
        except ValueError as exc:
            decision["reason"] = str(exc)
        decision["at"] = iso(self.clock())
        with self.lock, self.store.connection() as c:
            c.execute(
                "INSERT OR REPLACE INTO paper_decisions VALUES (?, ?)",
                (event["id"], json.dumps(decision)),
            )
            c.execute(
                "UPDATE events SET option_context=? WHERE id=?", (json.dumps(decision), event["id"])
            )
            c.execute("UPDATE paper_meta SET value=? WHERE key='cursor'", (str(event["id"]),))
            since = dt(
                c.execute("SELECT value FROM paper_meta WHERE key='auto_since'").fetchone()[0]
            )
        if settings["auto"] and self.settings()["auto"] and dt(event["created_at"]) >= since:
            try:
                self.submit(event["id"], {}, automatic=True)
            except ValueError as exc:
                decision["auto_result"] = str(exc)
                with self.store.connection() as c:
                    c.execute(
                        "UPDATE paper_decisions SET payload=? WHERE event_id=?",
                        (json.dumps(decision), event["id"]),
                    )
        return True

    def decisions(self):
        with self.store.connection() as c:
            return [
                json.loads(r[0])
                for r in c.execute(
                    "SELECT payload FROM paper_decisions ORDER BY event_id DESC LIMIT 500"
                )
            ]

    def preview(self, event_id, overrides=None):
        """Select a current option for an explicit manual ticket, including recovered alerts."""
        now = self.clock()
        if not self.gateway.authenticated or not getattr(self.monitor, "connected", False):
            raise ValueError("Connect the live Upstox market feed before a manual paper trade.")
        if not market_session_open(now) or now.time() >= time(15, 15):
            raise ValueError("Manual paper entries require the open market before 15:15 IST.")
        with self.lock, self.store.connection() as c:
            if c.execute("SELECT 1 FROM paper_trades WHERE event_id=?", (event_id,)).fetchone():
                raise ValueError("This alert already has a paper trade.")
            row = c.execute(
                """SELECT e.*, r.instrument_token, r.tradingsymbol, r.trading_date
                FROM events e JOIN rules r ON r.id=e.rule_id WHERE e.id=?""",
                (event_id,),
            ).fetchone()
            if row:
                event = dict(row)
            else:
                audit = c.execute(
                    """SELECT payload FROM alert_history
                    WHERE CAST(json_extract(payload, '$.id') AS INTEGER)=?
                    ORDER BY id DESC LIMIT 1""",
                    (event_id,),
                ).fetchone()
                if not audit:
                    raise ValueError("Alert not found in local history.")
                event = json.loads(audit[0])
        if event["trading_date"] != now.date().isoformat():
            raise ValueError("Only today's alerts can open a current manual paper trade.")
        overrides = Ticket.model_validate(overrides or {}).model_dump(exclude_none=True)
        settings = self.settings()
        settings.update(overrides)
        settings = PaperSettings.model_validate(settings).model_dump()
        candidate = self.research.select(
            event["instrument_token"], event["trade_side"], now.date(), settings
        )
        sector = event.get("sector_context") or {}
        if isinstance(sector, str):
            sector = json.loads(sector)
        decision = {
            "event_id": event_id,
            "event_time": event["event_time"],
            "detected_at": event.get("created_at") or event["event_time"],
            "symbol": event["tradingsymbol"],
            "trade_side": event["trade_side"],
            "source": event["source"],
            "sector_context": sector,
            "settings": settings,
            "at": iso(now),
            "manual_preview_at": iso(now),
            "quote_at": iso(self.clock()),
            "status": "READY",
            "candidate": candidate,
        }
        with self.lock, self.store.connection() as c:
            c.execute(
                "INSERT OR REPLACE INTO paper_decisions VALUES (?,?)",
                (event_id, json.dumps(decision)),
            )
            c.execute(
                "UPDATE events SET option_context=? WHERE id=?",
                (json.dumps(decision), event_id),
            )
        return decision

    def submit(self, event_id, overrides, automatic=False):
        overrides = Ticket.model_validate(overrides).model_dump(exclude_none=True)
        now = self.clock()
        with self.lock, self.store.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            duplicate = c.execute(
                "SELECT id,payload FROM paper_trades WHERE event_id=?", (event_id,)
            ).fetchone()
            if duplicate:
                return dict(json.loads(duplicate["payload"]), id=duplicate["id"])
            record = c.execute(
                "SELECT payload FROM paper_decisions WHERE event_id=?", (event_id,)
            ).fetchone()
            if record is None:
                raise ValueError(
                    "Option selection is pending or this historical alert is not eligible."
                )
            d = json.loads(record[0])
            if d["status"] != "READY":
                raise ValueError(d.get("reason", "No candidate."))
            if not automatic and (
                not d.get("manual_preview_at")
                or not 0 <= (now - dt(d["manual_preview_at"])).total_seconds() <= 30
            ):
                raise ValueError(
                    "Manual option preview is stale. Reopen the ticket for a new quote."
                )
            if not getattr(self.monitor, "connected", False):
                raise ValueError(
                    "Live market feed disconnected; cannot monitor a paper option position."
                )
            if not market_session_open(now) or now.time() >= time(15, 15):
                raise ValueError("Entries allowed only during market hours before 15:15 IST.")
            s = json.loads(
                c.execute("SELECT value FROM paper_meta WHERE key='settings'").fetchone()[0]
            )
            s.update(overrides)
            s = PaperSettings.model_validate(s).model_dump()
            s.update(latency_ms=0, slippage_percent=0, fee_fixed=0, fee_percent=0)
            if automatic and not s["auto"]:
                raise ValueError("Automatic paper mode is paused.")
            # Sector context remains visible on the alert but is not an entry gate.
            candidate = d["candidate"]
            days = (datetime.fromisoformat(candidate["expiry"]).date() - now.date()).days
            if days < 0:
                raise ValueError("Selected option has expired. Refresh the option selection.")
            if days < s["min_expiry_days"]:
                raise ValueError("Candidate does not pass the current minimum-expiry rule.")
            reference = dec(candidate["ltp"])
            if reference <= 0:
                raise ValueError("No valid option LTP to size a pending paper entry.")
            lot_size = candidate["lot_size"]
            estimated_lots = s["lots"] or int(
                min(dec(s["budget"]), dec(self._account(c, now)["available"]))
                / (reference * lot_size)
            )
            if estimated_lots < 1:
                raise ValueError("Budget cannot fund one whole option lot at the latest LTP.")
            estimated_qty = estimated_lots * lot_size
            estimated_cost = reference * estimated_qty
            available = dec(self._account(c, now)["available"])
            if estimated_cost > available:
                raise ValueError("Insufficient virtual cash for the requested paper lots.")
            if estimated_cost > dec(s["budget"]):
                raise ValueError("Requested lots exceed the per-trade budget at the latest LTP.")
            # Automatic entries are anchored to the live alert detection minute.
            # A manual preview may be opened later, so its scheduled entry is the
            # next minute from the current ticket submission rather than a past alert minute.
            entry_anchor = (
                now
                if not automatic and d.get("manual_preview_at")
                else dt(d.get("detected_at") or d.get("event_time") or now.isoformat())
            )
            entry_at = entry_anchor.replace(second=0, microsecond=0) + timedelta(minutes=1)
            t = {
                "event_id": event_id,
                "symbol": d["symbol"],
                "candidate": candidate,
                "sector_context": d["sector_context"],
                "sector": d["sector_context"].get("sector") or "UNKNOWN",
                "settings": s,
                "state": "PENDING",
                "mode": "AUTO" if automatic else "MANUAL",
                "created_at": iso(now),
                "remaining": 0,
                "quantity": estimated_qty,
                "realised": "0",
                "fees": "0",
                "uncertain": False,
                "exit_reason": None,
                "mark": None,
                "last_quote_at": None,
                "entry_at": iso(entry_at),
                "expires_at": iso(entry_at + timedelta(seconds=s["entry_timeout"])),
                "reserved": str(reference * estimated_qty),
                "entry_basis": "NEXT_1M_CANDLE_OPEN",
            }
            t.update(
                entry=None,
                target=None,
                stop=None,
            )
            cursor = c.execute(
                "INSERT INTO paper_trades(event_id,payload) VALUES (?,?)", (event_id, "{}")
            )
            t["id"] = cursor.lastrowid
            self._order(c, t, "ENTRY", "PENDING", now, estimated_qty, reference)
            self._save(c, "paper_trades", t)
            self._audit(c, {"action": "SUBMIT", "trade_id": t["id"], "settings": s}, now)
            return t

    def _account(self, c, now):
        s = json.loads(c.execute("SELECT value FROM paper_meta WHERE key='settings'").fetchone()[0])
        trades = self._rows(c, "paper_trades")
        fills = self._rows(c, "paper_fills")
        cash = dec(s["initial_cash"])
        for f in fills:
            value = dec(f["price"]) * f["quantity"]
            cash += (value if f["side"] == "SELL" else -value) - dec(f["fee"])
        reserved = sum(
            (dec(t.get("reserved", 0)) for t in trades if t["state"] == "PENDING"), Decimal(0)
        )
        unrealised = Decimal(0)
        complete = not any(int(t.get("unresolved_quantity", 0)) for t in trades)
        daily = sum(
            (
                dec(t["realised"])
                for t in trades
                if dt(t.get("filled_at", t["created_at"])).date() == now.date()
            ),
            Decimal(0),
        )
        for t in trades:
            if not t["remaining"]:
                continue
            if (
                not t.get("mark")
                or not t.get("last_quote_at")
                or (now - dt(t["last_quote_at"])).total_seconds() > t["settings"]["quote_ttl"]
            ):
                complete = False
                continue
            value = dec(t["mark"]) * t["remaining"]
            pnl = (dec(t["mark"]) - dec(t["entry"])) * t["remaining"] - fee(
                value, t["settings"], not t.get("exit_fee_paid")
            )
            unrealised += pnl
            daily += pnl
        realised = sum((dec(t["realised"]) for t in trades), Decimal(0))
        clean = [t for t in trades if t["state"] == "CLOSED" and not t["uncertain"]]
        return {
            "cash": str(cash),
            "reserved": str(reserved),
            "available": str(cash - reserved),
            "realised": str(realised),
            "unrealised": str(unrealised) if complete else None,
            "total_pnl": str(realised + unrealised) if complete else None,
            "daily_pnl": str(daily) if complete else None,
            "clean_closed": len(clean),
            "clean_wins": sum(dec(t["realised"]) > 0 for t in clean),
            "uncertain_count": sum(t["uncertain"] for t in trades),
            "estimated_costs": str(sum((dec(t["fees"]) for t in trades), Decimal(0))),
        }

    def _risk(self, c, proposed, now, own_id=None):
        s = proposed["settings"]
        account = self._account(c, now)
        available = dec(account["available"]) + (dec(proposed.get("reserved", 0)) if own_id else 0)
        candidate = proposed["candidate"]
        unit = dec(candidate["ltp"]) * candidate["lot_size"]
        lots = s["lots"] or int(min(dec(s["budget"]), available) / unit)
        if lots < 1 or unit * lots > available:
            raise ValueError("Insufficient virtual cash for one whole option lot.")

    def _order(self, c, t, kind, state, now, qty, price=None):
        o = {
            "trade_id": t["id"],
            "symbol": t["candidate"]["symbol"],
            "kind": kind,
            "state": state,
            "quantity": qty,
            "filled_quantity": 0,
            "price": str(price) if price is not None else None,
            "created_at": iso(now),
        }
        cursor = c.execute(
            "INSERT INTO paper_orders(trade_id,payload) VALUES (?,?)", (t["id"], json.dumps(o))
        )
        o["id"] = cursor.lastrowid
        self._save(c, "paper_orders", o)
        return o

    def _orders(self, c, t):
        return [o for o in self._rows(c, "paper_orders") if o["trade_id"] == t["id"]]

    def _fill(self, c, t, order, side, qty, price, now):
        cost = fee(price * qty, t["settings"], not order["filled_quantity"])
        fill = {
            "trade_id": t["id"],
            "order_id": order["id"],
            "side": side,
            "quantity": qty,
            "price": str(price),
            "fee": str(cost),
            "at": iso(now),
        }
        c.execute(
            "INSERT INTO paper_fills(trade_id,payload) VALUES (?,?)", (t["id"], json.dumps(fill))
        )
        order["filled_quantity"] += qty
        order["state"] = "FILLED" if order["filled_quantity"] == order["quantity"] else "PARTIAL"
        order["updated_at"] = iso(now)
        self._save(c, "paper_orders", order)
        t["fees"] = str(dec(t["fees"]) + cost)
        return cost

    def _cancel(self, c, t, state, reason, now):
        t.update(state=state, reserved="0", reason=reason)
        for o in self._orders(c, t):
            if o["state"] == "PENDING":
                o.update(state=state, updated_at=iso(now))
                self._save(c, "paper_orders", o)
        self._save(c, "paper_trades", t)

    def _close_uncertain(self, c, t, reason, now):
        """Stop tracking a position when its valid exit quote was missed."""
        if not t.get("remaining"):
            return
        quantity = t["remaining"]
        t.update(
            state="CLOSED_UNCERTAIN",
            exit_reason=reason,
            exit_trigger_at=t.get("exit_trigger_at") or iso(now),
            closed_at=iso(now),
            uncertain=True,
            mark=None,
            unresolved_quantity=quantity,
            remaining=0,
            reason=(
                "No valid option bid was observed before the session or expiry "
                "deadline. No exit fill or P&L was invented."
            ),
        )
        for order in self._orders(c, t):
            if order["state"] in {"WAITING", "TRIGGERED"}:
                order.update(state="UNCERTAIN", updated_at=iso(now))
                self._save(c, "paper_orders", order)
        self._save(c, "paper_trades", t)
        self._audit(
            c,
            {
                "action": "UNCERTAIN_EXIT",
                "trade_id": t["id"],
                "quantity": quantity,
                "reason": reason,
            },
            now,
        )

    def _trigger(self, c, t, reason, now):
        previous_reason = t.get("exit_reason")
        if previous_reason and (previous_reason != "TARGET" or reason == "TARGET"):
            return
        if previous_reason == "TARGET":
            for old in self._orders(c, t):
                if old["id"] == t["exit_order_id"]:
                    old.update(state="CANCELLED", updated_at=iso(now))
                    self._save(c, "paper_orders", old)
        t["exit_fee_paid"] = False
        t.update(state="EXIT_PENDING", exit_reason=reason, exit_trigger_at=iso(now))
        selected = None
        for o in self._orders(c, t):
            if o["kind"] in {"TARGET", "STOP"} and o["state"] == "WAITING":
                if o["kind"] == reason:
                    o["state"] = "TRIGGERED"
                    o["quantity"] = t["remaining"]
                    selected = o
                elif reason != "TARGET":
                    o["state"] = "CANCELLED"
                o["updated_at"] = iso(now)
                self._save(c, "paper_orders", o)
        if selected is None:
            selected = self._order(c, t, reason, "TRIGGERED", now, t["remaining"])
        t["exit_order_id"] = selected["id"]

    def action(self, trade_id, action):
        now = self.clock()
        with self.lock, self.store.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT payload FROM paper_trades WHERE id=?", (trade_id,)).fetchone()
            if not row:
                raise ValueError("Paper trade not found.")
            t = json.loads(row[0])
            if action == "cancel" and t["state"] == "PENDING":
                self._cancel(c, t, "CANCELLED", "Manual cancellation", now)
            elif action == "close" and t["remaining"]:
                self._trigger(c, t, "MANUAL", now)
                self._save(c, "paper_trades", t)
            else:
                raise ValueError("Action does not apply to this trade state.")
            self._audit(c, {"action": action, "trade_id": trade_id}, now)
            return t

    def advance(self):
        now = self.clock()
        with self.lock, self.store.connection() as c:
            for t in self._rows(c, "paper_trades"):
                expiry = datetime.fromisoformat(t["candidate"]["expiry"]).date()
                if t["state"] == "PENDING" and now.date() > expiry:
                    self._cancel(c, t, "EXPIRED", "Option expiry passed before entry.", now)
                elif t["state"] == "PENDING" and (
                    now >= dt(t["expires_at"]) or now.time() >= time(15, 15)
                ):
                    self._cancel(
                        c,
                        t,
                        "MISSED_ENTRY",
                        (
                            "No exact Upstox I1 candle open was captured for the "
                            "scheduled entry minute."
                        ),
                        now,
                    )
                elif t["remaining"]:
                    if now.date() > expiry:
                        self._close_uncertain(c, t, "EXPIRED_NO_QUOTE", now)
                        continue
                    if now.date() > dt(t["filled_at"]).date():
                        self._close_uncertain(c, t, "SESSION_NO_QUOTE", now)
                        continue
                    session_cutoff = datetime.combine(now.date(), time(15, 30), IST)
                    if now >= session_cutoff + timedelta(seconds=15) and (
                        t.get("exit_reason") or now.date() == expiry
                    ):
                        reason = (
                            "EXPIRY_NO_QUOTE" if now.date() >= expiry else "SESSION_NO_QUOTE"
                        )
                        self._close_uncertain(c, t, reason, now)
                        continue
                    anchor = dt(t.get("last_quote_at") or t["filled_at"])
                    if (now - anchor).total_seconds() > t["settings"]["quote_ttl"]:
                        t["uncertain"] = True
                    if t.get("exit_reason") in {None, "TARGET"}:
                        if now.date() > dt(t["filled_at"]).date() or now.time() >= time(15, 20):
                            self._trigger(c, t, "SESSION", now)
                        elif now >= dt(t["filled_at"]) + timedelta(
                            minutes=t["settings"]["hold_minutes"]
                        ):
                            self._trigger(c, t, "TIME", now)
                    self._save(c, "paper_trades", t)

    def _open_pending_at_minute(self, c, t, buy, candle_start, observed_at):
        if t["state"] != "PENDING" or candle_start != dt(t["entry_at"]):
            return False
        s = t["settings"]
        tick = dec(t["candidate"]["tick_size"])
        available = dec(self._account(c, observed_at)["available"]) + dec(t.get("reserved", 0))
        unit = buy * t["candidate"]["lot_size"]
        cap = min(dec(s["budget"]), available)
        lots = s["lots"] or int(cap / unit)
        if lots < 1 or lots * unit > cap:
            self._cancel(
                c,
                t,
                "REJECTED",
                (
                    "Scheduled one-minute option open exceeds the per-trade budget "
                    "or available paper cash."
                ),
                observed_at,
            )
            return False
        qty = lots * t["candidate"]["lot_size"]
        order = next(o for o in self._orders(c, t) if o["kind"] == "ENTRY")
        order["quantity"] = qty
        order["price"] = str(buy)
        cost = self._fill(c, t, order, "BUY", qty, buy, candle_start)
        t.update(
            quantity=qty,
            remaining=qty,
            state="OPEN",
            entry=str(buy),
            reserved="0",
            filled_at=iso(candle_start),
            entry_quote_at=iso(observed_at),
            entry_basis="UPSTOX_I1_OPEN",
            realised=str(-cost),
            mae_percent=0.0,
            mfe_percent=0.0,
        )
        t["target"] = str(
            price_tick(buy * (1 + dec(s["target_percent"]) / 100), tick, True)
        )
        t["stop"] = str(
            price_tick(buy * (1 - dec(s["stop_percent"]) / 100), tick, True)
        )
        self._order(c, t, "TARGET", "WAITING", observed_at, qty, t["target"])
        self._order(c, t, "STOP", "WAITING", observed_at, qty, t["stop"])
        self._save(c, "paper_trades", t)
        self._audit(
            c,
            {
                "action": "MINUTE_OPEN_ENTRY",
                "trade_id": t["id"],
                "entry_at": t["entry_at"],
                "observed_at": iso(observed_at),
                "price": str(buy),
            },
            observed_at,
        )
        return True

    def on_minute_open(self, token, opening, candle_start, received=None):
        observed_at = received or self.clock()
        try:
            candle_start = candle_start.astimezone(IST)
            opening = dec(opening)
            if opening <= 0 or not market_session_open(candle_start):
                return False
            if candle_start.second or candle_start.microsecond:
                return False
        except (ValueError, TypeError, ArithmeticError):
            return False
        with self.lock, self.store.connection() as c:
            for t in self._rows(c, "paper_trades"):
                if t["state"] != "PENDING" or t["candidate"]["instrument_key"] != token:
                    continue
                if candle_start != dt(t["entry_at"]):
                    continue
                if observed_at > dt(t["expires_at"]):
                    continue
                return self._open_pending_at_minute(c, t, opening, candle_start, observed_at)
        return False

    def poll_pending_entries(self):
        now = self.clock()
        if not self.gateway.authenticated or not market_session_open(now):
            return
        if self._last_entry_poll_at is not None and (
            now - self._last_entry_poll_at
        ).total_seconds() < 1:
            return
        self._last_entry_poll_at = now
        with self.lock, self.store.connection() as c:
            pending = [
                t
                for t in self._rows(c, "paper_trades")
                if t["state"] == "PENDING"
                and dt(t["entry_at"]) <= now <= dt(t["expires_at"])
            ]
        if not pending:
            return
        tokens = sorted({t["candidate"]["instrument_key"] for t in pending})
        try:
            candles = self.gateway.option_minute_ohlc(tokens)
        except Exception as exc:
            self.last_error = f"Pending-entry I1 lookup failed; retrying: {exc}"
            return
        wanted = {t["candidate"]["instrument_key"]: dt(t["entry_at"]) for t in pending}
        for token, rows in candles.items():
            target = wanted.get(token)
            if target is None:
                continue
            for row in rows:
                if row["start"] == target:
                    self.on_minute_open(token, row["open"], row["start"], now)
                    break

    def on_quote(self, token, book, timestamp, received=None, ltp=None):
        now = received or self.clock()
        try:
            if book:
                bid, ask = dec(book["bidP"]), dec(book["askP"])
                bq, aq = int(book["bidQ"]), int(book["askQ"])
                if not 0 < bid <= ask or min(bq, aq) < 0:
                    return
            else:
                bid = ask = None
                bq = aq = 0
            if not market_session_open(timestamp):
                return
            if timestamp.date() != now.date() or not 0 <= (now - timestamp).total_seconds() <= 15:
                return
        except (ValueError, KeyError, TypeError, ArithmeticError):
            return
        with self.lock, self.store.connection() as c:
            previous = self.quotes.get(token)
            if book and previous and timestamp <= previous["timestamp"]:
                return
            if book:
                self.quotes[token] = {"bid": str(bid), "ask": str(ask), "timestamp": timestamp}
            for t in self._rows(c, "paper_trades"):
                if t["candidate"]["instrument_key"] != token or not (
                    t["state"] == "PENDING" or t["remaining"]
                ):
                    continue
                s = t["settings"]
                if (now - timestamp).total_seconds() > s["quote_ttl"]:
                    continue
                if (
                    t.get("last_quote_at")
                    and (timestamp - dt(t["last_quote_at"])).total_seconds() > s["quote_ttl"]
                    and t["remaining"]
                ):
                    t["uncertain"] = True
                expiry = datetime.fromisoformat(t["candidate"]["expiry"]).date()
                if now.date() > expiry:
                    if t["state"] == "PENDING":
                        self._cancel(c, t, "EXPIRED", "Option expiry passed before entry.", now)
                    else:
                        self._close_uncertain(c, t, "EXPIRED_NO_QUOTE", now)
                    continue
                if t["remaining"] and now.date() > dt(t["filled_at"]).date():
                    self._close_uncertain(c, t, "SESSION_NO_QUOTE", now)
                    continue
                tick = dec(t["candidate"]["tick_size"])
                sell = (
                    max(tick, price_tick(bid * (1 - dec(s["slippage_percent"]) / 100), tick))
                    if bid is not None
                    else None
                )
                if sell is not None:
                    t.update(mark=str(sell), last_quote_at=iso(timestamp))
                if t["state"] == "PENDING":
                    if now >= dt(t["expires_at"]) or now.time() >= time(15, 15):
                        self._cancel(
                            c,
                            t,
                            "MISSED_ENTRY",
                            (
                            "No exact Upstox I1 candle open was captured for the "
                            "scheduled entry minute."
                        ),
                            now,
                        )
                    self._save(c, "paper_trades", t)
                    continue
                elif t["remaining"] and sell is not None:
                    move = float((sell / dec(t["entry"]) - 1) * 100)
                    t["mae_percent"] = min(t.get("mae_percent", 0), move)
                    t["mfe_percent"] = max(t.get("mfe_percent", 0), move)
                    if t.get("exit_reason") in {None, "TARGET"}:
                        if now.date() > dt(t["filled_at"]).date() or now.time() >= time(15, 20):
                            self._trigger(c, t, "SESSION", now)
                        elif now >= dt(t["filled_at"]) + timedelta(minutes=s["hold_minutes"]):
                            self._trigger(c, t, "TIME", now)
                        elif bid <= dec(t["stop"]):
                            self._trigger(c, t, "STOP", now)
                        elif sell >= dec(t["target"]):
                            self._trigger(c, t, "TARGET", now)
                    if t.get("exit_reason"):
                        qty = t["remaining"]
                        if qty:
                            order = next(
                                o for o in self._orders(c, t) if o["id"] == t["exit_order_id"]
                            )
                            cost = self._fill(c, t, order, "SELL", qty, sell, now)
                            t["remaining"] -= qty
                            t["realised"] = str(
                                dec(t["realised"]) + (sell - dec(t["entry"])) * qty - cost
                            )
                            t["exit_fee_paid"] = True
                            if not t["remaining"]:
                                t.update(state="CLOSED", closed_at=iso(now))
                                for sibling in self._orders(c, t):
                                    if sibling["state"] == "WAITING":
                                        sibling.update(state="CANCELLED", updated_at=iso(now))
                                        self._save(c, "paper_orders", sibling)
                self._save(c, "paper_trades", t)
                # Persist observed active-position path for first-hit and gap auditing.
                if book:
                    c.execute(
                    "INSERT INTO paper_quotes(trade_id,payload) VALUES (?,?)",
                    (
                        t["id"],
                        json.dumps(
                            {
                                "at": iso(timestamp),
                                "received_at": iso(now),
                                "bid": str(bid),
                                "ask": str(ask),
                                "bid_qty": bq,
                                "ask_qty": aq,
                            }
                        ),
                    ),
                    )

    def sync_subscriptions(self):
        with self.lock, self.store.connection() as c:
            tokens = {
                t["candidate"]["instrument_key"]
                for t in self._rows(c, "paper_trades")
                if t["state"] == "PENDING" or t["remaining"]
            }
        if tokens != self.synced:
            self.monitor.set_option_tokens(tokens)
            self.synced = tokens

    def snapshot(self):
        with self.lock, self.store.connection() as c:
            feed = self.monitor.status() if hasattr(self.monitor, "status") else {}
            return {
                "settings": self.settings(),
                "account": self._account(c, self.clock()),
                "trades": self._rows(c, "paper_trades")[::-1],
                "orders": self._rows(c, "paper_orders")[::-1],
                "fills": self._rows(c, "paper_fills")[::-1],
                "decisions": self.decisions(),
                "error": self.last_error,
                "as_of": iso(self.clock()),
                "paper_only": True,
                "entries_open": (
                    market_session_open(self.clock()) and self.clock().time() < time(15, 15)
                ),
                "market_data_connected": bool(getattr(self.monitor, "connected", False)),
                "feed": feed,
                "authenticated": bool(self.gateway.authenticated),
                "running": any(t.is_alive() for t in self.threads),
            }
