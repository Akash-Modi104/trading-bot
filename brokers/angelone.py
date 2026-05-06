"""
Angel One SmartAPI broker integration.

Credentials required:
  api_key      — from Angel One developer console
  client_id    — trading account client code (e.g. A12345)
  password     — 4-digit MPIN or login password
  totp_secret  — base-32 TOTP secret from Angel One (used to generate OTP at login)

Angel One uses Indian exchanges: NSE, BSE, NFO, MCX.
Equity intraday orders use producttype="INTRADAY".
"""

import json
import logging
import os
import time
import socket
import requests
import pyotp
from datetime import datetime, timedelta
from typing import Optional

from .base import BaseBroker

BASE_URL = "https://apiconnect.angelbroking.com"

log = logging.getLogger(__name__)

# Public/private IP used in headers — fallback to localhost if detection fails
def _get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

_LOCAL_IP = _get_local_ip()


class AngelOneError(Exception):
    pass


class AngelOneBroker(BaseBroker):
    """
    Stateful client for Angel One SmartAPI.

    Instantiate with credentials, call login() to get tokens,
    then use order/position/account methods.  Tokens are cached;
    call refresh() when they expire (typically after 24 h).
    """

    def __init__(self, api_key: str, client_id: str, password: str, totp_secret: str):
        self.api_key     = api_key
        self.client_id   = client_id
        self.password    = password
        self.totp_secret = totp_secret

        self.jwt_token     = None
        self.refresh_token = None
        self.feed_token    = None
        self.logged_in_at  = None

    # ── Auth ─────────────────────────────────────────────────────

    def _headers(self, with_auth: bool = True) -> dict:
        h = {
            "Content-Type":    "application/json",
            "Accept":          "application/json",
            "X-UserType":      "USER",
            "X-SourceID":      "WEB",
            "X-ClientLocalIP": _LOCAL_IP,
            "X-ClientPublicIP": _LOCAL_IP,
            "X-MACAddress":    "00:00:00:00:00:00",
            "X-PrivateKey":    self.api_key,
        }
        if with_auth and self.jwt_token:
            h["Authorization"] = f"Bearer {self.jwt_token}"
        return h

    def _post(self, path: str, body: dict, auth: bool = True) -> dict:
        url = BASE_URL + path
        r = requests.post(url, json=body, headers=self._headers(with_auth=auth), timeout=15)
        try:
            data = r.json()
        except Exception:
            raise AngelOneError(f"Non-JSON response ({r.status_code}): {r.text[:200]}")
        if not data.get("status"):
            msg = data.get("message") or data.get("errorcode") or str(data)
            raise AngelOneError(msg)
        return data

    def _get(self, path: str, params: dict = None) -> dict:
        url = BASE_URL + path
        r = requests.get(url, params=params, headers=self._headers(), timeout=15)
        try:
            data = r.json()
        except Exception:
            raise AngelOneError(f"Non-JSON response ({r.status_code}): {r.text[:200]}")
        if not data.get("status"):
            msg = data.get("message") or data.get("errorcode") or str(data)
            raise AngelOneError(msg)
        return data

    def login(self) -> dict:
        """Authenticate and cache JWT + refresh tokens. Returns profile dict."""
        totp = pyotp.TOTP(self.totp_secret).now()
        data = self._post(
            "/rest/auth/angelbroking/user/v1/loginByPassword",
            {
                "clientcode": self.client_id,
                "password":   self.password,
                "totp":       totp,
            },
            auth=False,
        )
        d = data.get("data", {})
        self.jwt_token     = d.get("jwtToken")
        self.refresh_token = d.get("refreshToken")
        self.feed_token    = d.get("feedToken")
        self.logged_in_at  = datetime.utcnow()
        if not self.jwt_token:
            raise AngelOneError("Login succeeded but no jwtToken in response")
        return d

    def refresh_tokens(self) -> dict:
        """Refresh JWT using refresh token (avoids full re-login)."""
        data = self._post(
            "/rest/auth/angelbroking/jwt/v1/generateTokens",
            {"refreshToken": self.refresh_token},
        )
        d = data.get("data", {})
        self.jwt_token     = d.get("jwtToken", self.jwt_token)
        self.refresh_token = d.get("refreshToken", self.refresh_token)
        self.feed_token    = d.get("feedToken", self.feed_token)
        self.logged_in_at  = datetime.utcnow()
        return d

    def logout(self) -> bool:
        try:
            self._post(
                "/rest/secure/angelbroking/user/v1/logout",
                {"clientcode": self.client_id},
            )
            return True
        except Exception:
            return False

    def ensure_logged_in(self):
        """Auto re-login if token is absent or older than 23 hours."""
        if not self.jwt_token:
            self.login()
            return
        if self.logged_in_at:
            age_hours = (datetime.utcnow() - self.logged_in_at).total_seconds() / 3600
            if age_hours > 23:
                self.login()

    # ── Account ──────────────────────────────────────────────────

    def get_profile(self) -> dict:
        self.ensure_logged_in()
        data = self._get("/rest/secure/angelbroking/user/v1/getProfile")
        return data.get("data") or {}

    def get_funds(self) -> dict:
        """Returns available cash, used margin, net equity."""
        self.ensure_logged_in()
        data = self._get("/rest/secure/angelbroking/user/v1/getRMS")
        return data.get("data") or {}

    # ── BaseBroker interface adapters ─────────────────────────────

    @property
    def name(self) -> str:
        return "Angel One"

    @property
    def currency(self) -> str:
        return "INR"

    @property
    def timezone(self) -> str:
        return "Asia/Kolkata"

    def get_account(self) -> dict:
        """Return account dict with equity and buying_power (BaseBroker contract)."""
        summary = self.account_summary()
        try:
            equity = float(summary.get("net") or 0)
        except (TypeError, ValueError):
            equity = 0.0
        try:
            buying_power = float(summary.get("available_cash") or 0)
        except (TypeError, ValueError):
            buying_power = 0.0
        return {
            "equity":       equity,
            "buying_power": buying_power,
            "raw":          summary,
        }

    def get_latest_price(self, symbol: str) -> Optional[float]:
        """Get LTP by searching for the symbol token first.
        symbol should be the NSE trading symbol, e.g. 'RELIANCE-EQ'."""
        try:
            results = self.search_symbol("NSE", symbol)
            if not results:
                return None
            token = str(results[0].get("symboltoken", ""))
            if not token:
                return None
            return self.get_ltp("NSE", symbol, token) or None
        except AngelOneError as e:
            log.warning("get_latest_price(%s) failed: %s", symbol, e)
            return None

    def close_all_positions(self) -> dict:
        """Square off all intraday positions (BaseBroker contract)."""
        results = self.square_off_all_positions()
        failed = [r for r in results if not r.get("ok")]
        if failed:
            return {"_error": True, "message": f"{len(failed)} position(s) failed to close",
                    "results": results}
        return {"status": "flat", "results": results}

    def place_order(  # type: ignore[override]
        self,
        symbol: str,
        qty: int,
        side: str,
        order_type: str = "market",
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        symboltoken: str = "",
        exchange: str = "NSE",
        product_type: str = "INTRADAY",
    ) -> dict:
        """BaseBroker-compatible place_order. Resolves symboltoken via search if not provided."""
        if not symboltoken:
            try:
                results = self.search_symbol(exchange, symbol)
                symboltoken = str(results[0].get("symboltoken", "")) if results else ""
            except AngelOneError as e:
                return {"_error": True, "message": f"symbol lookup failed: {e}"}
        if not symboltoken:
            return {"_error": True, "message": f"Could not resolve token for {symbol}"}
        ao_order_type = "LIMIT" if limit_price else "MARKET"
        price = limit_price or 0.0
        try:
            order_id = self._place_native_order(
                tradingsymbol=symbol,
                symboltoken=symboltoken,
                transaction_type=side.upper(),
                quantity=qty,
                price=price,
                order_type=ao_order_type,
                product_type=product_type,
                exchange=exchange,
            )
            return {"id": order_id, "status": "submitted"}
        except AngelOneError as e:
            return {"_error": True, "message": str(e)}

    def get_bars(self, symbol: str, timeframe: str = "5Min", limit: int = 80) -> list:
        """Fetch OHLCV bars normalised to BaseBroker spec {t,o,h,l,c,v}.
        Searches for symbol token automatically.
        timeframe maps: 5Min→FIVE_MINUTE, 1Min→ONE_MINUTE, 15Min→FIFTEEN_MINUTE, 1D→ONE_DAY."""
        _tf_map = {
            "1Min": "ONE_MINUTE", "3Min": "THREE_MINUTE", "5Min": "FIVE_MINUTE",
            "10Min": "TEN_MINUTE", "15Min": "FIFTEEN_MINUTE", "30Min": "THIRTY_MINUTE",
            "1H": "ONE_HOUR", "1D": "ONE_DAY",
        }
        interval = _tf_map.get(timeframe, "FIVE_MINUTE")
        try:
            results = self.search_symbol("NSE", symbol)
            if not results:
                return []
            token = str(results[0].get("symboltoken", ""))
            if not token:
                return []
            now = datetime.now()
            from_dt = now - timedelta(minutes=limit * 5 + 60)
            candles = self.get_candles(
                exchange="NSE",
                symboltoken=token,
                interval=interval,
                from_date=from_dt.strftime("%Y-%m-%d %H:%M"),
                to_date=now.strftime("%Y-%m-%d %H:%M"),
            )
            bars = []
            for c in candles[-limit:]:
                # Angel One returns [timestamp, open, high, low, close, volume]
                if len(c) >= 6:
                    bars.append({"t": c[0], "o": c[1], "h": c[2], "l": c[3], "c": c[4], "v": c[5]})
            return bars
        except AngelOneError as e:
            log.warning("get_bars(%s) failed: %s", symbol, e)
            return []

    # ── Symbol search ────────────────────────────────────────────

    def search_symbol(self, exchange: str, query: str) -> list:
        """Search for symbols to find their token.  exchange: NSE, BSE, NFO."""
        self.ensure_logged_in()
        data = self._get(
            "/rest/secure/angelbroking/order/v1/searchScrip",
            params={"exchange": exchange, "searchscrip": query},
        )
        return data.get("data") or []

    # ── Orders ───────────────────────────────────────────────────

    def _place_native_order(
        self,
        tradingsymbol: str,
        symboltoken: str,
        transaction_type: str,   # "BUY" or "SELL"
        quantity: int,
        price: float = 0,
        order_type: str = "MARKET",      # MARKET | LIMIT | STOPLOSS_LIMIT | STOPLOSS_MARKET
        product_type: str = "INTRADAY",  # INTRADAY | DELIVERY | MARGIN | CARRYFORWARD
        exchange: str = "NSE",
        variety: str = "NORMAL",         # NORMAL | STOPLOSS | AMO | ROBO
        duration: str = "DAY",
        squareoff: float = 0,
        stoploss: float = 0,
        trailing_stoploss: float = 0,
    ) -> str:
        """Place an order. Returns order_id string."""
        self.ensure_logged_in()
        body = {
            "variety":          variety,
            "tradingsymbol":    tradingsymbol,
            "symboltoken":      str(symboltoken),
            "transactiontype":  transaction_type.upper(),
            "exchange":         exchange.upper(),
            "ordertype":        order_type.upper(),
            "producttype":      product_type.upper(),
            "duration":         duration.upper(),
            "price":            str(price),
            "squareoff":        str(squareoff),
            "stoploss":         str(stoploss),
            "trailingStopLoss": str(trailing_stoploss),
            "quantity":         str(quantity),
        }
        data = self._post("/rest/secure/angelbroking/order/v1/placeOrder", body)
        return data.get("data", {}).get("orderid", "")

    def modify_order(
        self,
        order_id: str,
        tradingsymbol: str,
        symboltoken: str,
        quantity: int,
        price: float,
        order_type: str = "LIMIT",
        product_type: str = "INTRADAY",
        exchange: str = "NSE",
        variety: str = "NORMAL",
        duration: str = "DAY",
    ) -> str:
        self.ensure_logged_in()
        body = {
            "variety":         variety,
            "orderid":         order_id,
            "tradingsymbol":   tradingsymbol,
            "symboltoken":     str(symboltoken),
            "exchange":        exchange.upper(),
            "ordertype":       order_type.upper(),
            "producttype":     product_type.upper(),
            "duration":        duration.upper(),
            "price":           str(price),
            "quantity":        str(quantity),
        }
        data = self._post("/rest/secure/angelbroking/order/v1/modifyOrder", body)
        return data.get("data", {}).get("orderid", order_id)

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> str:
        self.ensure_logged_in()
        data = self._post(
            "/rest/secure/angelbroking/order/v1/cancelOrder",
            {"variety": variety, "orderid": order_id},
        )
        return data.get("data", {}).get("orderid", order_id)

    def place_bracket_order(
        self,
        tradingsymbol: str,
        symboltoken: str,
        transaction_type: str,
        quantity: int,
        price: float,
        stoploss_points: float,
        target_points: float,
        trailing_stoploss: float = 0,
        exchange: str = "NSE",
        duration: str = "DAY",
    ) -> str:
        """
        BO (Bracket Order): entry + automatic stop loss + target.
        stoploss_points and target_points are in absolute price units (e.g. 5.0 means ₹5).
        """
        return self._place_native_order(
            tradingsymbol=tradingsymbol,
            symboltoken=symboltoken,
            transaction_type=transaction_type,
            quantity=quantity,
            price=price,
            order_type="LIMIT",
            product_type="BO",
            exchange=exchange,
            variety="ROBO",
            duration=duration,
            squareoff=target_points,
            stoploss=stoploss_points,
            trailing_stoploss=trailing_stoploss,
        )

    def place_cover_order(
        self,
        tradingsymbol: str,
        symboltoken: str,
        transaction_type: str,
        quantity: int,
        price: float,
        stoploss_price: float,
        exchange: str = "NSE",
        duration: str = "DAY",
    ) -> str:
        """CO (Cover Order): entry + compulsory stop loss at exchange level."""
        return self._place_native_order(
            tradingsymbol=tradingsymbol,
            symboltoken=symboltoken,
            transaction_type=transaction_type,
            quantity=quantity,
            price=price,
            order_type="LIMIT",
            product_type="CO",
            exchange=exchange,
            variety="STOPLOSS",
            duration=duration,
            stoploss=stoploss_price,
        )

    # ── Order/Trade books ────────────────────────────────────────

    def get_order_book(self) -> list:
        self.ensure_logged_in()
        data = self._get("/rest/secure/angelbroking/order/v1/getOrderBook")
        return data.get("data") or []

    def get_trade_book(self) -> list:
        self.ensure_logged_in()
        data = self._get("/rest/secure/angelbroking/order/v1/getTradeBook")
        return data.get("data") or []

    def get_order_status(self, order_id: str) -> dict:
        book = self.get_order_book()
        for o in book:
            if str(o.get("orderid")) == str(order_id):
                return o
        return {}

    # ── Positions & holdings ─────────────────────────────────────

    def get_positions(self) -> list:
        """Intraday open positions."""
        self.ensure_logged_in()
        data = self._get("/rest/secure/angelbroking/order/v1/getPosition")
        return data.get("data") or []

    def get_holdings(self) -> list:
        """Long-term CNC holdings (delivery portfolio)."""
        self.ensure_logged_in()
        data = self._get("/rest/secure/angelbroking/portfolio/v1/getAllHolding")
        inner = data.get("data") or {}
        return inner.get("holdings") or []

    def close_position_native(
        self,
        tradingsymbol: str,
        symboltoken: str,
        quantity: int,
        transaction_type: str,  # "SELL" to close a long, "BUY" to close a short
        exchange: str = "NSE",
        product_type: str = "INTRADAY",
    ) -> str:
        """Market-sell (or buy) to flatten a position by token (native API)."""
        return self._place_native_order(
            tradingsymbol=tradingsymbol,
            symboltoken=symboltoken,
            transaction_type=transaction_type,
            quantity=quantity,
            price=0,
            order_type="MARKET",
            product_type=product_type,
            exchange=exchange,
        )

    def close_position(self, symbol: str) -> dict:  # type: ignore[override]
        """BaseBroker-compatible: close a position by symbol name.
        Looks up open positions to find token and qty, then market-sells."""
        try:
            positions = self.get_positions()
            for p in positions:
                if p.get("tradingsymbol") == symbol:
                    net_qty = int(p.get("netqty", 0))
                    if net_qty == 0:
                        return {"status": "no_position"}
                    side = "SELL" if net_qty > 0 else "BUY"
                    oid = self.close_position_native(
                        tradingsymbol=symbol,
                        symboltoken=p.get("symboltoken", ""),
                        quantity=abs(net_qty),
                        transaction_type=side,
                        exchange=p.get("exchange", "NSE"),
                        product_type=p.get("producttype", "INTRADAY"),
                    )
                    return {"id": oid, "status": "submitted"}
            return {"_error": True, "message": f"No open position found for {symbol}"}
        except AngelOneError as e:
            return {"_error": True, "message": str(e)}

    def square_off_all_positions(self) -> list:
        """Close every open intraday position at market price."""
        positions = self.get_positions()
        results = []
        for p in positions:
            net_qty = int(p.get("netqty", 0))
            if net_qty == 0:
                continue
            side = "SELL" if net_qty > 0 else "BUY"
            try:
                oid = self.close_position_native(
                    tradingsymbol=p.get("tradingsymbol", ""),
                    symboltoken=p.get("symboltoken", ""),
                    quantity=abs(net_qty),
                    transaction_type=side,
                    exchange=p.get("exchange", "NSE"),
                    product_type=p.get("producttype", "INTRADAY"),
                )
                results.append({"symbol": p["tradingsymbol"], "order_id": oid, "ok": True})
            except AngelOneError as e:
                results.append({"symbol": p["tradingsymbol"], "error": str(e), "ok": False})
        return results

    # ── Market data ──────────────────────────────────────────────

    def get_ltp(self, exchange: str, tradingsymbol: str, symboltoken: str) -> float:
        """Last traded price for a single symbol."""
        self.ensure_logged_in()
        body = {
            "mode": "LTP",
            "exchangeTokens": {exchange: [symboltoken]},
        }
        data = self._post("/rest/secure/angelbroking/market/v1/quote/", body)
        fetched = (data.get("data") or {}).get("fetched") or []
        if fetched:
            return float(fetched[0].get("ltp", 0))
        return 0.0

    def get_quote(self, exchange: str, tradingsymbol: str, symboltoken: str) -> dict:
        """Full quote: LTP, bid, ask, OHLC, volume."""
        self.ensure_logged_in()
        body = {
            "mode": "FULL",
            "exchangeTokens": {exchange: [symboltoken]},
        }
        data = self._post("/rest/secure/angelbroking/market/v1/quote/", body)
        fetched = (data.get("data") or {}).get("fetched") or []
        return fetched[0] if fetched else {}

    def get_candles(
        self,
        exchange: str,
        symboltoken: str,
        interval: str,       # ONE_MINUTE, THREE_MINUTE, FIVE_MINUTE, TEN_MINUTE,
                             # FIFTEEN_MINUTE, THIRTY_MINUTE, ONE_HOUR, ONE_DAY
        from_date: str,      # "YYYY-MM-DD HH:MM"
        to_date: str,        # "YYYY-MM-DD HH:MM"
    ) -> list:
        """OHLCV candle data. Returns list of [timestamp, O, H, L, C, V]."""
        self.ensure_logged_in()
        body = {
            "exchange":    exchange,
            "symboltoken": symboltoken,
            "interval":    interval,
            "fromdate":    from_date,
            "todate":      to_date,
        }
        data = self._post("/rest/secure/angelbroking/historical/v1/getCandleData", body)
        return data.get("data") or []

    # ── Convenience helpers ──────────────────────────────────────

    def account_summary(self) -> dict:
        """Merged funds + profile into a dashboard-friendly dict."""
        try:
            profile = self.get_profile()
        except Exception:
            profile = {}
        try:
            funds = self.get_funds()
        except Exception:
            funds = {}
        return {
            "client_id":     self.client_id,
            "name":          profile.get("name", ""),
            "email":         profile.get("email", ""),
            "mobile":        profile.get("mobileno", ""),
            "broker":        profile.get("broker", ""),
            "exchanges":     profile.get("exchanges", []),
            "net":           funds.get("net", ""),
            "available_cash": funds.get("availablecash", ""),
            "used_margin":   funds.get("utilisedmargin", ""),
            "collateral":    funds.get("collateral", ""),
            "m2m_unrealised": funds.get("m2munrealisedprofit", ""),
            "m2m_realised":  funds.get("m2mrealisedprofit", ""),
        }

    @staticmethod
    def token_is_valid(token_dict: dict) -> bool:
        """Check if stored session tokens are still valid (< 23 h old)."""
        logged_in_at = token_dict.get("logged_in_at")
        if not logged_in_at:
            return False
        try:
            age = (datetime.utcnow() - datetime.fromisoformat(logged_in_at)).total_seconds()
            return age < 23 * 3600
        except Exception:
            return False
