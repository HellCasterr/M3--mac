"""Read-only option discovery and an explicit, unvalidated strike-selection policy."""

from __future__ import annotations

from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

import requests


def dec(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Non-finite value")
    return result


def price_tick(value, tick, up=False) -> Decimal:
    return (dec(value) / dec(tick)).to_integral_value(
        rounding=ROUND_CEILING if up else ROUND_FLOOR
    ) * dec(tick)


def fee(value, settings, fixed=True) -> Decimal:
    return (
        dec(value) * dec(settings["fee_percent"]) / 100
        + (dec(settings["fee_fixed"]) if fixed else 0)
    ).quantize(Decimal(".01"))


class OptionResearch:
    def __init__(self, gateway):
        self.gateway = gateway
        self.cache = {}

    def _get(self, path, params):
        if not self.gateway.authenticated:
            raise ValueError("Connect Upstox for option market data.")
        try:
            response = requests.get(
                "https://api.upstox.com/v2" + path,
                params=params,
                headers={
                    "Authorization": f"Bearer {self.gateway.access_token}",
                    "Accept": "application/json",
                },
                timeout=4,
            )
            if response.status_code == 429:
                raise ValueError("Upstox rate limit; no candidate for this alert.")
            if not response.ok:
                raise ValueError(f"Option data unavailable (HTTP {response.status_code}).")
            payload = response.json()
            if payload.get("status") != "success" or not isinstance(payload.get("data"), list):
                raise ValueError("Invalid option data response.")
            return payload["data"]
        except requests.RequestException as exc:
            raise ValueError("Option-data request failed; no candidate.") from exc

    def select(self, instrument, side, today, settings):
        key = (instrument, today.isoformat())
        if key not in self.cache:
            self.cache[key] = self._get("/option/contract", {"instrument_key": instrument})
        contracts = self.cache[key]
        expiries = []
        for c in contracts:
            try:
                expiry = date.fromisoformat(c["expiry"])
                if (expiry - today).days >= settings["min_expiry_days"]:
                    expiries.append(expiry)
            except (KeyError, ValueError, TypeError):
                continue
        if not expiries:
            raise ValueError("No available option expiry passes the minimum-days rule.")
        expiry = min(expiries).isoformat()
        chain = self._get("/option/chain", {"instrument_key": instrument, "expiry_date": expiry})
        return select_candidate(contracts, chain, side, expiry, settings)


def select_candidate(contracts, chain, side, expiry, settings):
    if side not in {"LONG", "SHORT"}:
        raise ValueError("Unsupported direction; buy CE or buy PE only.")
    kind = "CE" if side == "LONG" else "PE"
    leg = "call_options" if kind == "CE" else "put_options"
    metadata = {
        c.get("instrument_key"): c
        for c in contracts
        if c.get("expiry") == expiry
        and c.get("instrument_type") == kind
        and c.get("segment") == "NSE_FO"
    }
    if not metadata or not chain:
        raise ValueError("No option contracts for this underlying and expiry.")
    try:
        spot = dec(chain[0]["underlying_spot_price"])
        strikes = sorted({dec(row["strike_price"]) for row in chain})
        if spot <= 0:
            raise ValueError("Invalid spot")
        atm = min(strikes, key=lambda strike: (abs(strike - spot), strike))
        itm = sorted(
            (s for s in strikes if (s < spot if kind == "CE" else s > spot)),
            key=lambda strike: abs(strike - spot),
        )[:2]
        otm = sorted(
            (s for s in strikes if (s > spot if kind == "CE" else s < spot)),
            key=lambda strike: abs(strike - spot),
        )[:3]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Missing valid spot/strike data.") from exc
    eligible = []
    quoted = []
    otm_quoted = []
    unaffordable = []
    for row in chain:
        try:
            strike = dec(row["strike_price"])
            data = row[leg]
            meta = metadata[data["instrument_key"]]
            if strike not in {atm, *itm, *otm}:
                continue
            md, greeks = data["market_data"], data.get("option_greeks") or {}
            delta = dec(greeks["delta"]) if greeks.get("delta") is not None else None
            bid, ask = dec(md["bid_price"]), dec(md["ask_price"])
            ltp = dec(md["ltp"])
            lot = int(meta["lot_size"])
            tick = dec(meta["tick_size"]) / 100  # Upstox contract units: paise.
            if not 0 < bid <= ask or ltp <= 0 or lot <= 0 or tick <= 0:
                continue
            spread = (ask - bid) / ((ask + bid) / 2) * 100
            estimate = ltp
            lots = settings["lots"] or 1
            cost = estimate * lot * lots
            if cost > dec(settings["budget"]):
                unaffordable.append((estimate * lot, lot, estimate))
                continue
            candidate = {
                "instrument_key": data["instrument_key"],
                "symbol": meta["trading_symbol"],
                "strike": str(strike),
                "kind": kind,
                "expiry": expiry,
                "lot_size": lot,
                "tick_size": str(tick),
                "bid": str(bid),
                "ask": str(ask),
                "ltp": str(ltp),
                "bid_qty": int(md.get("bid_qty") or 0),
                "ask_qty": int(md.get("ask_qty") or 0),
                "delta": str(delta) if delta is not None else "unavailable",
                "iv": greeks.get("iv"),
                "volume": int(md.get("volume") or 0),
                "oi": int(md.get("oi") or 0),
                "spread_percent": str(spread),
                "entry_limit": str(estimate),
                "spot": str(spot),
                "policy": "ATM_ITM_DELTA_V1",
                "reason": f"ATM/near ITM; quoted spread {spread:.2f}%; simulated option LTP entry",
            }
            if strike in otm and strike != atm:
                candidate["policy"] = "NEAR_OTM_BUDGET_FALLBACK"
                candidate["reason"] = (
                    f"Nearby OTM budget fallback; quoted spread {spread:.2f}%; "
                    "simulated option LTP entry"
                )
                otm_quoted.append((abs(strike - spot), spread, candidate))
                continue
            quoted.append((abs(strike - spot), spread, candidate))
            if (delta is not None and (delta > 0 if kind == "CE" else delta < 0)
                and Decimal(".45") <= abs(delta) <= Decimal(".75")
                and spread <= dec(settings["max_spread_percent"])
                and min(candidate["bid_qty"], candidate["ask_qty"]) >= lot
                and candidate["volume"] > 0 and candidate["oi"] > 0):
                eligible.append(
                    (abs(abs(delta) - Decimal(".60")), spread, -candidate["volume"], candidate)
                )
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
    if eligible:
        return min(eligible, key=lambda x: x[:3])[3]
    if quoted:
        candidate = min(quoted, key=lambda x: x[:2])[2]
        candidate["policy"] = "ATM_ITM_QUOTED_FALLBACK"
        candidate["reason"] += "; liquidity screen did not pass; paper quote only"
        return candidate
    if otm_quoted:
        return min(otm_quoted, key=lambda x: x[:2])[2]
    if not eligible:
        if unaffordable:
            one_lot, lot_size, ltp = min(unaffordable)
            requested = settings["lots"] or 1
            raise ValueError(
                f"ATM/near-ITM option exceeds ₹{dec(settings['budget']):,.2f} "
                f"per-trade budget: cheapest quoted candidate costs ₹{one_lot:,.2f} "
                f"for one lot ({lot_size} × ₹{ltp:,.2f} LTP); "
                f"{requested} requested lot(s) would cost ₹{one_lot * requested:,.2f}."
            )
        raise ValueError("No quoted ATM/near-ITM option within the per-trade budget.")
