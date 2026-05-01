"""
Zerodha Kite API broker integration.

Auth flow (OAuth, daily):
  1. Direct user to the login URL:
       https://kite.trade/connect/login?api_key={api_key}&v=3
  2. After login, Zerodha redirects to your registered redirect URL with
       ?request_token=<token>&action=login&status=success
  3. Exchange request_token for access_token via generate_session().
  4. access_token is valid for ~1 day; store it encrypted in DB.

Credentials stored in DB:
  api_key       — from Kite Developer console
  api_secret    — from Kite Developer console
  access_token  — obtained after OAuth (refreshed daily)
  request_token — last used request token (ephemeral, single-use)
"""

import hashlib
import json
import requests
from datetime import datetime, timedelta

BASE_URL = "https://api.kite.trade"
LOGIN_URL = "https://kite.trade/connect/login?api_key={api_key}&v=3"


class ZerodhaError(Exception):
    pass


class ZerodhaBroker:
    """
    Stateful client for Zerodha Kite Connect v3 API.

    Typical usage:
      1. On first connect, redirect user to login_url() to get request_token.
      2. Call generate_session(request_token) to get access_token.
      3. Use order/position/account methods.
      4. Next day: repeat from step 1 (access_token expires daily at ~6 AM IST).
    """

    def __init__(self, api_key: str, api_secret: str, access_token: str = ""):
        self.api_key      = api_key
        self.api_secret   = api_secret
        self.access_token = access_token

    # ── Auth ─────────────────────────────────────────────────────

    def login_url(self) -> str:
        """URL to redirect the user to for Kite login."""
        return LOGIN_URL.format(api_key=self.api_key)

    def generate_session(self, request_token: str) -> dict:
        """
        Exchange request_token for access_token.
        Sets self.access_token and returns the session dict.
        """
        checksum = hashlib.sha256(
            (self.api_key + request_token + self.api_secret).encode()
        ).hexdigest()
        r = requests.post(
            f"{BASE_URL}/session/token",
            data={
                "api_key":       self.api_key,
                "request_token": request_token,
                "checksum":      checksum,
            },
            headers={"X-Kite-Version": "3"},
            timeout=15,
        )
        data = self._parse(r)
        self.access_token = data.get("access_token", "")
        return data

    def invalidate_session(self) -> bool:
        """Logout / invalidate the current access_token."""
        try:
            r = requests.delete(
                f"{BASE_URL}/session/token",
                params={"api_key": self.api_key, "access_token": self.access_token},
                headers=self._headers(),
                timeout=10,
            )
            self._parse(r)
            return True
        except Exception:
            return False

    # ── HTTP helpers ─────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "X-Kite-Version": "3",
            "Authorization":  f"token {self.api_key}:{self.access_token}",
        }

    def _parse(self, r: requests.Response) -> dict:
        try:
            data = r.json()
        except Exception:
            raise ZerodhaError(f"Non-JSON response ({r.status_code}): {r.text[:200]}")
        if data.get("status") == "error":
            raise ZerodhaError(data.get("message") or str(data))
        return data.get("data", data)

    def _get(self, path: str, params: dict = None):
        r = requests.get(f"{BASE_URL}{path}", params=params,
                         headers=self._headers(), timeout=15)
        return self._parse(r)

    def _post(self, path: str, data: dict = None):
        r = requests.post(f"{BASE_URL}{path}", data=data,
                          headers=self._headers(), timeout=15)
        return self._parse(r)

    def _put(self, path: str, data: dict = None):
        r = requests.put(f"{BASE_URL}{path}", data=data,
                         headers=self._headers(), timeout=15)
        return self._parse(r)

    def _delete(self, path: str, params: dict = None):
        r = requests.delete(f"{BASE_URL}{path}", params=params,
                            headers=self._headers(), timeout=15)
        return self._parse(r)

    # ── Account ──────────────────────────────────────────────────

    def get_profile(self) -> dict:
        return self._get("/user/profile")

    def get_funds(self) -> dict:
        """Returns equity + commodity margins."""
        return self._get("/user/margins")

    # ── Orders ───────────────────────────────────────────────────

    def place_order(
        self,
        tradingsymbol: str,
        transaction_type: str,    # "BUY" or "SELL"
        quantity: int,
        price: float = 0,
        trigger_price: float = 0,
        order_type: str = "MARKET",  # MARKET | LIMIT | SL | SL-M
        product: str = "MIS",        # MIS (intraday) | CNC (delivery) | NRML (F&O)
        exchange: str = "NSE",
        variety: str = "regular",    # regular | amo | co | iceberg | auction
        validity: str = "DAY",       # DAY | IOC | TTL
        disclosed_quantity: int = 0,
        squareoff: float = 0,
        stoploss: float = 0,
        trailing_stoploss: float = 0,
        tag: str = "",
    ) -> str:
        """Place an order. Returns order_id string."""
        data = {
            "tradingsymbol":    tradingsymbol.upper(),
            "exchange":         exchange.upper(),
            "transaction_type": transaction_type.upper(),
            "order_type":       order_type.upper(),
            "product":          product.upper(),
            "quantity":         str(quantity),
            "validity":         validity.upper(),
            "price":            str(price),
            "trigger_price":    str(trigger_price),
            "disclosed_quantity": str(disclosed_quantity),
        }
        if variety in ("co",):
            data["trigger_price"] = str(trigger_price or stoploss)
        if variety == "regular" and squareoff and stoploss:
            data["squareoff"]        = str(squareoff)
            data["stoploss"]         = str(stoploss)
            data["trailing_stoploss"] = str(trailing_stoploss)
            variety = "bo"  # promote to Bracket Order
        if tag:
            data["tag"] = tag[:20]

        result = self._post(f"/orders/{variety}", data)
        return str(result.get("order_id", result))

    def modify_order(
        self,
        order_id: str,
        quantity: int = None,
        price: float = None,
        order_type: str = None,
        trigger_price: float = None,
        validity: str = None,
        disclosed_quantity: int = None,
        variety: str = "regular",
    ) -> str:
        data = {}
        if quantity         is not None: data["quantity"]          = str(quantity)
        if price            is not None: data["price"]             = str(price)
        if order_type       is not None: data["order_type"]        = order_type.upper()
        if trigger_price    is not None: data["trigger_price"]     = str(trigger_price)
        if validity         is not None: data["validity"]          = validity.upper()
        if disclosed_quantity is not None: data["disclosed_quantity"] = str(disclosed_quantity)
        result = self._put(f"/orders/{variety}/{order_id}", data)
        return str(result.get("order_id", result))

    def cancel_order(self, order_id: str, variety: str = "regular") -> str:
        result = self._delete(f"/orders/{variety}/{order_id}")
        return str(result.get("order_id", result))

    # ── Order/Trade books ────────────────────────────────────────

    def get_orders(self) -> list:
        return self._get("/orders") or []

    def get_order_history(self, order_id: str) -> list:
        return self._get(f"/orders/{order_id}") or []

    def get_trades(self) -> list:
        return self._get("/trades") or []

    def get_order_trades(self, order_id: str) -> list:
        return self._get(f"/orders/{order_id}/trades") or []

    # ── Positions & holdings ─────────────────────────────────────

    def get_positions(self) -> dict:
        """Returns {'day': [...], 'net': [...]}."""
        return self._get("/portfolio/positions") or {}

    def get_holdings(self) -> list:
        return self._get("/portfolio/holdings") or []

    def convert_position(
        self,
        tradingsymbol: str,
        exchange: str,
        transaction_type: str,
        position_type: str,
        quantity: int,
        old_product: str,
        new_product: str,
    ) -> bool:
        """Convert between MIS <-> CNC / NRML."""
        data = {
            "tradingsymbol":   tradingsymbol.upper(),
            "exchange":        exchange.upper(),
            "transaction_type": transaction_type.upper(),
            "position_type":   position_type,
            "quantity":        str(quantity),
            "old_product":     old_product.upper(),
            "new_product":     new_product.upper(),
        }
        self._put("/portfolio/positions", data)
        return True

    def close_position(
        self,
        tradingsymbol: str,
        quantity: int,
        transaction_type: str,
        exchange: str = "NSE",
        product: str = "MIS",
    ) -> str:
        return self.place_order(
            tradingsymbol=tradingsymbol,
            transaction_type=transaction_type,
            quantity=quantity,
            order_type="MARKET",
            product=product,
            exchange=exchange,
        )

    def square_off_all_positions(self) -> list:
        """Close all open MIS/day positions at market price."""
        pos_data = self.get_positions()
        day_pos  = pos_data.get("day", [])
        results  = []
        for p in day_pos:
            net_qty = int(p.get("quantity", 0))
            if net_qty == 0:
                continue
            side = "SELL" if net_qty > 0 else "BUY"
            try:
                oid = self.close_position(
                    tradingsymbol=p.get("tradingsymbol", ""),
                    quantity=abs(net_qty),
                    transaction_type=side,
                    exchange=p.get("exchange", "NSE"),
                    product=p.get("product", "MIS"),
                )
                results.append({"symbol": p["tradingsymbol"], "order_id": oid, "ok": True})
            except ZerodhaError as e:
                results.append({"symbol": p["tradingsymbol"], "error": str(e), "ok": False})
        return results

    # ── Market data ──────────────────────────────────────────────

    def get_quote(self, instruments: list) -> dict:
        """
        instruments: list of "NSE:RELIANCE", "BSE:SENSEX" etc.
        Returns full quote dict keyed by instrument string.
        """
        params = {"i": instruments}
        return self._get("/quote", params) or {}

    def get_ltp(self, instruments: list) -> dict:
        """
        instruments: list of "NSE:RELIANCE" etc.
        Returns {instrument: {instrument_token, last_price}}.
        """
        params = {"i": instruments}
        return self._get("/quote/ltp", params) or {}

    def get_ohlc(self, instruments: list) -> dict:
        params = {"i": instruments}
        return self._get("/quote/ohlc", params) or {}

    def get_candles(
        self,
        instrument_token: str,
        interval: str,       # minute, 3minute, 5minute, 10minute, 15minute,
                             # 30minute, 60minute, day
        from_date: str,      # "YYYY-MM-DD" or "YYYY-MM-DD HH:MM:SS"
        to_date: str,
        continuous: bool = False,
        oi: bool = False,
    ) -> list:
        """Returns list of [date, open, high, low, close, volume] candles."""
        params = {
            "from":       from_date,
            "to":         to_date,
            "continuous": 1 if continuous else 0,
            "oi":         1 if oi else 0,
        }
        data = self._get(f"/instruments/historical/{instrument_token}/{interval}", params)
        return (data or {}).get("candles", [])

    def get_instruments(self, exchange: str = None) -> list:
        """Download full instrument master list (large — ~10MB CSV-parsed)."""
        path = f"/instruments/{exchange.upper()}" if exchange else "/instruments"
        r = requests.get(f"{BASE_URL}{path}", headers=self._headers(), timeout=30)
        if r.status_code != 200:
            raise ZerodhaError(f"Instruments download failed: HTTP {r.status_code}")
        # CSV → list of dicts
        lines = r.text.strip().split("\n")
        if not lines:
            return []
        headers = lines[0].split(",")
        result  = []
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) == len(headers):
                result.append(dict(zip(headers, parts)))
        return result

    def search_instruments(self, query: str, exchange: str = "NSE") -> list:
        """Simple in-memory filter on cached instrument data (subset returned)."""
        all_inst = self.get_instruments(exchange)
        q = query.upper()
        return [i for i in all_inst if q in i.get("tradingsymbol", "").upper()
                or q in i.get("name", "").upper()][:50]

    # ── Helpers ──────────────────────────────────────────────────

    def account_summary(self) -> dict:
        try:
            profile = self.get_profile()
        except Exception:
            profile = {}
        try:
            funds = self.get_funds()
            equity = funds.get("equity", {})
        except Exception:
            equity = {}
        return {
            "client_id":      profile.get("user_id", ""),
            "name":           profile.get("user_name", ""),
            "email":          profile.get("email", ""),
            "broker":         profile.get("broker", "ZERODHA"),
            "exchanges":      profile.get("exchanges", []),
            "available_cash": equity.get("available", {}).get("live_balance", ""),
            "used_margin":    equity.get("utilised", {}).get("debits", ""),
            "net":            equity.get("net", ""),
            "opening_balance": equity.get("available", {}).get("opening_balance", ""),
        }
