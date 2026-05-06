"""
Alpaca broker implementation conforming to BaseBroker.

Reads credentials from environment:
  ALPACA_API_KEY, ALPACA_SECRET_KEY
  ALPACA_BASE_URL  (default: paper endpoint)
  ALPACA_DATA_URL
"""

import os
import time
import logging
import requests
from typing import Optional

from .base import BaseBroker

log = logging.getLogger(__name__)


class AlpacaError(Exception):
    pass


class AlpacaBroker(BaseBroker):
    """Alpaca REST v2 — US paper and live trading."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        base_url: Optional[str] = None,
        data_url: Optional[str] = None,
    ):
        self._api_key = api_key    or os.environ.get("ALPACA_API_KEY", "")
        self._secret  = secret_key or os.environ.get("ALPACA_SECRET_KEY", "")
        self._base    = (base_url  or os.environ.get("ALPACA_BASE_URL",
                         "https://paper-api.alpaca.markets/v2")).rstrip("/")
        self._data    = (data_url  or os.environ.get("ALPACA_DATA_URL",
                         "https://data.alpaca.markets/v2")).rstrip("/")
        if not self._api_key or not self._secret:
            raise AlpacaError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required")

    # ── HTTP helpers ──────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "APCA-API-KEY-ID":     self._api_key,
            "APCA-API-SECRET-KEY": self._secret,
        }

    def _request(self, method: str, url: str, retries: int = 2, **kwargs) -> dict:
        last_err = None
        for attempt in range(retries + 1):
            try:
                r = requests.request(method, url, headers=self._headers(),
                                     timeout=10, **kwargs)
                data = r.json() if r.content else {}
                if r.status_code >= 400:
                    msg = data.get("message") if isinstance(data, dict) else str(data)
                    if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    log.warning("Alpaca %s %s → %d: %s", method, url, r.status_code, msg)
                    return {"_error": True, "status_code": r.status_code, "message": msg}
                return data
            except requests.RequestException as e:
                last_err = e
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        log.error("Alpaca network failure %s %s: %s", method, url, last_err)
        return {"_error": True, "message": str(last_err)}

    def _trade(self, method: str, path: str, **kwargs) -> dict:
        return self._request(method, f"{self._base}{path}", **kwargs)

    def _data_get(self, path: str, params: Optional[dict] = None) -> dict:
        try:
            r = requests.get(f"{self._data}{path}", headers=self._headers(),
                             params=params or {}, timeout=10)
            return r.json() if r.content else {}
        except requests.RequestException as e:
            log.warning("Alpaca data GET %s failed: %s", path, e)
            return {}

    # ── BaseBroker properties ─────────────────────────────────────

    @property
    def name(self) -> str:
        return "Alpaca"

    @property
    def currency(self) -> str:
        return "USD"

    @property
    def timezone(self) -> str:
        return "America/New_York"

    # ── Account ───────────────────────────────────────────────────

    def get_account(self) -> dict:
        data = self._trade("GET", "/account")
        if isinstance(data, dict) and data.get("_error"):
            raise AlpacaError(data.get("message", "get_account failed"))
        return {
            "equity":             float(data.get("equity", 0)),
            "buying_power":       float(data.get("buying_power", 0)),
            "cash":               float(data.get("cash", 0)),
            "portfolio_value":    float(data.get("portfolio_value", 0)),
            "pattern_day_trader": data.get("pattern_day_trader", False),
            "daytrade_count":     int(data.get("daytrade_count", 0)),
            "raw":                data,
        }

    def get_positions(self) -> list:
        data = self._trade("GET", "/positions")
        if not isinstance(data, list):
            return []
        out = []
        for p in data:
            try:
                out.append({
                    "symbol":          p["symbol"],
                    "qty":             int(float(p.get("qty", 0))),
                    "avg_entry_price": float(p.get("avg_entry_price", 0)),
                    "current_price":   float(p.get("current_price", 0)),
                    "unrealized_plpc": float(p.get("unrealized_plpc", 0)),
                    "unrealized_pl":   float(p.get("unrealized_pl", 0)),
                    "market_value":    float(p.get("market_value", 0)),
                    "side":            p.get("side", "long"),
                    "raw":             p,
                })
            except (KeyError, ValueError) as e:
                log.warning("Skipping malformed Alpaca position %s: %s", p.get("symbol"), e)
        return out

    # ── Orders ────────────────────────────────────────────────────

    def place_order(
        self,
        symbol: str,
        qty: int,
        side: str,
        order_type: str = "market",
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
    ) -> dict:
        body: dict = {
            "symbol":         symbol,
            "qty":            str(qty),
            "side":           side,
            "type":           "limit" if limit_price else order_type,
            "time_in_force":  "day",
            "extended_hours": False,
        }
        if limit_price:
            body["limit_price"] = f"{round(limit_price, 2):.2f}"
        if stop_price and take_profit_price and side == "buy":
            body["order_class"] = "bracket"
            body["take_profit"] = {"limit_price": f"{round(take_profit_price, 2):.2f}"}
            body["stop_loss"]   = {"stop_price":  f"{round(stop_price, 2):.2f}"}
        elif stop_price:
            body["stop_price"] = f"{round(stop_price, 2):.2f}"
        return self._trade("POST", "/orders", json=body)

    def get_order(self, order_id: str) -> dict:
        return self._trade("GET", f"/orders/{order_id}")

    def cancel_order(self, order_id: str) -> dict:
        return self._trade("DELETE", f"/orders/{order_id}")

    def wait_for_fill(
        self,
        order_id: str,
        timeout: int = 10,
        requested_qty: Optional[int] = None,
        min_fill_pct: float = 0.5,
    ) -> tuple:
        """Poll until filled or timed out. Returns (filled_qty, avg_price).
        Accepts partial fills >= min_fill_pct of requested_qty."""
        end = time.time() + timeout
        while time.time() < end:
            o = self.get_order(order_id)
            if not isinstance(o, dict) or o.get("_error"):
                return 0, None
            status = o.get("status")
            if status == "filled":
                return int(float(o.get("filled_qty", 0))), float(o.get("filled_avg_price") or 0)
            if status in ("canceled", "rejected", "expired"):
                partial = int(float(o.get("filled_qty", 0)))
                avg     = float(o.get("filled_avg_price") or 0)
                return (partial, avg) if partial > 0 and avg > 0 else (0, None)
            time.sleep(0.5)
        # Timed out: cancel and salvage any partial fill
        self._trade("DELETE", f"/orders/{order_id}")
        o = self.get_order(order_id) or {}
        partial = int(float(o.get("filled_qty", 0)))
        avg     = float(o.get("filled_avg_price") or 0)
        if requested_qty and partial >= max(1, int(requested_qty * min_fill_pct)):
            log.info("Order %s partial fill %d/%d accepted", order_id[:8], partial, requested_qty)
            return partial, avg
        log.info("Order %s timed out, cancelled", order_id[:8])
        return 0, None

    def close_position(self, symbol: str) -> dict:
        return self._trade("DELETE", f"/positions/{symbol}")

    def close_all_positions(self) -> dict:
        """Cancel all orders then liquidate every open position with per-symbol retry."""
        # 1. Cancel open orders — bracket children block bulk liquidation
        cr = self._trade("DELETE", "/orders")
        if isinstance(cr, dict) and cr.get("_error"):
            log.warning("close_all: cancel orders failed: %s", cr.get("message"))
        time.sleep(2)

        # 2. Bulk DELETE /positions (returns 207 multi-status list)
        res = self._trade("DELETE", "/positions")
        if isinstance(res, list):
            failed = [x for x in res if isinstance(x, dict) and x.get("status", 0) >= 400]
            if failed:
                log.warning("close_all: %d position(s) failed bulk close", len(failed))
        elif isinstance(res, dict) and res.get("_error"):
            log.warning("close_all: DELETE /positions error: %s", res.get("message"))

        # 3. Verify and per-symbol retry up to 3 passes
        for attempt in range(3):
            time.sleep(2)
            remaining = self.get_positions()
            if not remaining:
                return {"status": "flat", "passes": attempt + 1}
            syms = [p["symbol"] for p in remaining]
            log.warning("close_all pass %d: still open: %s", attempt, syms)
            for p in remaining:
                r2 = self.close_position(p["symbol"])
                if isinstance(r2, dict) and r2.get("_error"):
                    log.warning("close_all per-symbol fail %s: %s",
                                p["symbol"], r2.get("message"))

        remaining = self.get_positions()
        if remaining:
            syms = [p["symbol"] for p in remaining]
            log.error("close_all GAVE UP — still holding %s", syms)
            return {"_error": True, "message": f"Could not close: {syms}"}
        return {"status": "flat"}

    # ── Market data ───────────────────────────────────────────────

    def get_bars(self, symbol: str, timeframe: str = "5Min", limit: int = 80) -> list:
        data = self._data_get(f"/stocks/{symbol}/bars",
                              {"timeframe": timeframe, "limit": limit, "feed": "iex"})
        return (data.get("bars") or []) if isinstance(data, dict) else []

    def get_latest_price(self, symbol: str) -> Optional[float]:
        data = self._data_get(f"/stocks/{symbol}/trades/latest")
        if isinstance(data, dict):
            return data.get("trade", {}).get("p")
        return None

    def get_quote(self, symbol: str) -> tuple:
        """Return (bid, ask) for spread check."""
        data = self._data_get(f"/stocks/{symbol}/quotes/latest")
        if isinstance(data, dict):
            q = data.get("quote", {})
            return q.get("bp"), q.get("ap")
        return None, None

    def get_clock(self) -> dict:
        """Alpaca market clock: is_open, next_open, next_close."""
        return self._trade("GET", "/clock")

    def is_paper(self) -> bool:
        return "paper" in self._base
