"""
Alpaca broker implementation wrapping the paper/live trading API.

Reads credentials from environment:
  ALPACA_API_KEY
  ALPACA_SECRET_KEY
  ALPACA_BASE_URL   (default: paper)
  ALPACA_DATA_URL
"""

import os
import time
import requests
from typing import Optional

from .base import BaseBroker

_BASE_URL = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")
_DATA_URL = os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets/v2")
_API_KEY  = os.environ.get("ALPACA_API_KEY", "")
_SECRET   = os.environ.get("ALPACA_SECRET_KEY", "")

_HEADERS = {
    "APCA-API-KEY-ID":     _API_KEY,
    "APCA-API-SECRET-KEY": _SECRET,
}


def _api(method: str, path: str, retries: int = 2, base: str = None, **kw) -> dict:
    base = base or _BASE_URL
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.request(
                method, f"{base}{path}",
                headers=_HEADERS, timeout=10, **kw
            )
            data = r.json() if r.content else {}
            if r.status_code >= 400:
                msg = data.get("message") if isinstance(data, dict) else str(data)
                if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return {"_error": True, "status_code": r.status_code, "message": msg}
            return data
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    return {"_error": True, "message": str(last_err)}


def _data_get(path: str, params: dict = None) -> dict:
    try:
        r = requests.get(
            f"{_DATA_URL}{path}",
            headers=_HEADERS,
            params=params or {},
            timeout=10,
        )
        return r.json() if r.content else {}
    except Exception:
        return {}


class AlpacaBroker(BaseBroker):
    """Alpaca paper/live trading via REST API."""

    @property
    def name(self) -> str:
        return "Alpaca"

    @property
    def currency(self) -> str:
        return "USD"

    @property
    def timezone(self) -> str:
        return "America/New_York"

    # ── Account ──────────────────────────────────────────────────

    def get_account(self) -> dict:
        return _api("GET", "/account")

    def get_positions(self) -> list:
        p = _api("GET", "/positions")
        return p if isinstance(p, list) else []

    # ── Orders ───────────────────────────────────────────────────

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
        body = {
            "symbol":        symbol,
            "qty":           str(qty),
            "side":          side,
            "type":          "limit" if limit_price else order_type,
            "time_in_force": "day",
            "extended_hours": False,
        }
        if limit_price:
            body["limit_price"] = f"{round(limit_price, 2):.2f}"
        if stop_price and take_profit_price and side == "buy":
            body["order_class"] = "bracket"
            body["take_profit"] = {"limit_price": f"{round(take_profit_price, 2):.2f}"}
            body["stop_loss"]   = {"stop_price":  f"{round(stop_price, 2):.2f}"}
        return _api("POST", "/orders", json=body)

    def get_order(self, order_id: str) -> dict:
        return _api("GET", f"/orders/{order_id}")

    def cancel_order(self, order_id: str) -> dict:
        return _api("DELETE", f"/orders/{order_id}")

    def close_position(self, symbol: str) -> dict:
        return _api("DELETE", f"/positions/{symbol}")

    def close_all_positions(self) -> dict:
        # Cancel all working orders first (bracket children block liquidation)
        _api("DELETE", "/orders")
        time.sleep(2)
        res = _api("DELETE", "/positions")
        return res if isinstance(res, dict) else {"ok": True, "results": res}

    # ── Market data ──────────────────────────────────────────────

    def get_bars(self, symbol: str, timeframe: str = "5Min", limit: int = 80) -> list:
        d = _data_get(
            f"/stocks/{symbol}/bars",
            {"timeframe": timeframe, "limit": limit, "feed": "iex"},
        )
        return (d.get("bars") or []) if isinstance(d, dict) else []

    def get_latest_price(self, symbol: str) -> Optional[float]:
        d = _data_get(f"/stocks/{symbol}/trades/latest")
        if isinstance(d, dict):
            return d.get("trade", {}).get("p")
        return None

    def get_quote(self, symbol: str):
        """Returns (bid, ask) tuple for spread check."""
        d = _data_get(f"/stocks/{symbol}/quotes/latest")
        if isinstance(d, dict):
            q = d.get("quote", {})
            return q.get("bp"), q.get("ap")
        return None, None

    def get_clock(self) -> dict:
        return _api("GET", "/clock")
