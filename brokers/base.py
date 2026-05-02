"""
Abstract base broker interface.

All broker implementations (AlpacaBroker, AngelOneBroker, ZerodhaBroker)
must subclass BaseBroker and implement every abstract method.

The bot selects the broker at startup via the BROKER env var:
  BROKER=alpaca    → AlpacaBroker  (US markets, paper/live)
  BROKER=angelone  → AngelOneBroker (Indian NSE/BSE)
  BROKER=zerodha   → ZerodhaBroker  (Indian NSE/BSE/MCX)
"""

from abc import ABC, abstractmethod
from typing import Optional


class BaseBroker(ABC):
    """Common interface every broker must implement."""

    # ── Account ──────────────────────────────────────────────────

    @abstractmethod
    def get_account(self) -> dict:
        """Return account info dict with at least: equity, buying_power."""

    @abstractmethod
    def get_positions(self) -> list:
        """Return list of open positions. Each dict must include:
        symbol, qty, avg_entry_price, current_price, unrealized_plpc."""

    # ── Orders ───────────────────────────────────────────────────

    @abstractmethod
    def place_order(
        self,
        symbol: str,
        qty: int,
        side: str,               # "buy" or "sell"
        order_type: str = "market",
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
    ) -> dict:
        """Place an order. Returns dict with at least: id, status.
        On error returns dict with _error=True and message key."""

    @abstractmethod
    def close_position(self, symbol: str) -> dict:
        """Market-close a single position by symbol."""

    @abstractmethod
    def close_all_positions(self) -> dict:
        """Cancel all open orders then liquidate all positions."""

    # ── Market data ──────────────────────────────────────────────

    @abstractmethod
    def get_bars(self, symbol: str, timeframe: str = "5Min", limit: int = 80) -> list:
        """Return list of OHLCV bar dicts with keys: t, o, h, l, c, v."""

    @abstractmethod
    def get_latest_price(self, symbol: str) -> Optional[float]:
        """Return the latest trade price for a symbol."""

    # ── Order status ─────────────────────────────────────────────

    def get_order(self, order_id: str) -> dict:
        """Return order status dict. Default: not supported."""
        return {"_error": True, "message": "get_order not supported by this broker"}

    def cancel_order(self, order_id: str) -> dict:
        """Cancel a pending order. Default: not supported."""
        return {"_error": True, "message": "cancel_order not supported by this broker"}

    # ── Convenience ──────────────────────────────────────────────

    @property
    def name(self) -> str:
        """Human-readable broker name, e.g. 'Alpaca', 'Angel One'."""
        return self.__class__.__name__

    @property
    def currency(self) -> str:
        """ISO currency code, e.g. 'USD', 'INR'."""
        return "USD"

    @property
    def timezone(self) -> str:
        """Primary market timezone, e.g. 'America/New_York', 'Asia/Kolkata'."""
        return "America/New_York"
