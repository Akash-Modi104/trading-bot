"""
Indian Intraday Trading Bot — NSE/BSE via Angel One or Zerodha.

Market hours: 09:15–15:30 IST (Monday–Friday)
EOD square-off: 15:15 IST
Entry window: 09:30–14:45 IST

Strategy mirrors intraday_bot_v2.py (EMA/RSI/VWAP/MACD/BB) adapted for:
  - IST timezone
  - ₹ denominated sizing
  - NSE symbol format (no suffix for Angel One, NSE: prefix for Zerodha)
  - Angel One candle data via get_candles()
  - NIFTY trend filter (replaces SPY)

Configuration:
  BROKER=angelone  or  BROKER=zerodha   (required)
  All broker credential env vars (see brokers/__init__.py)
  INDIAN_BOT_BUDGET_PER_TRADE  — ₹ per position when no DB allocation is set
  DB allocation budget         — total ₹ capital ceiling, split across positions
  INDIAN_BOT_MAX_POSITIONS     — max simultaneous positions (default 3)
  INDIAN_BOT_STOP_PCT          — stop-loss % (default 1.5)
  INDIAN_BOT_TP_PCT            — take-profit % (default 3.0)
  INDIAN_BOT_DAILY_LOSS_LIMIT  — max daily drawdown % (default 3.0)
"""

import _force_ipv4_kite  # Force IPv4 for kite.trade per NSE IP whitelist
import json
import os
import time
from datetime import datetime, date, time as dtime

import numpy as np
import pytz
import requests

from brokers import get_broker
from brokers.angelone import AngelOneBroker, AngelOneError
from brokers.zerodha import ZerodhaBroker
from telegram_alerts import alert_buy, alert_sell, alert_daily_loss, alert_eod, alert_startup
import safe_io
import profit_engine as pe  # Quality filters + risk management

# ── Paths ──────────────────────────────────────────────────────
BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
LOG_FILE       = os.path.join(BASE_DIR, "indian_trade_log.json")
STATE_FILE     = os.path.join(BASE_DIR, "indian_bot_state.json")
POSITIONS_F    = os.path.join(BASE_DIR, "indian_positions_state.json")
NEG_NEWS_F     = os.path.join(BASE_DIR, "negative_news_in.json")  # written by news_scanner_indian.py

# ── Timezone ───────────────────────────────────────────────────
IST = pytz.timezone("Asia/Kolkata")

# ── Configuration from env ─────────────────────────────────────
ENV_FILE = os.path.join(BASE_DIR, ".env")
try:
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)
except ImportError:
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip())

BUDGET_PER_TRADE  = float(os.environ.get("INDIAN_BOT_BUDGET_PER_TRADE", 10000))
TOTAL_BUDGET       = float(os.environ.get("INDIAN_BOT_TOTAL_BUDGET", BUDGET_PER_TRADE))
MAX_POSITIONS     = int(os.environ.get("INDIAN_BOT_MAX_POSITIONS", 3))
STOP_PCT          = float(os.environ.get("INDIAN_BOT_STOP_PCT", 1.5))
TP_PCT            = float(os.environ.get("INDIAN_BOT_TP_PCT", 3.0))
DAILY_LOSS_LIMIT  = float(os.environ.get("INDIAN_BOT_DAILY_LOSS_LIMIT", 3.0))
MIN_CONFIDENCE    = int(os.environ.get("INDIAN_BOT_MIN_CONFIDENCE", 65))
POSITION_PCT      = float(os.environ.get("INDIAN_BOT_POSITION_PCT", 100))

MARGIN_BUFFER_PCT = float(os.environ.get("INDIAN_BOT_MARGIN_BUFFER_PCT", 15))
ORDER_BUFFER_INR  = float(os.environ.get("INDIAN_BOT_ORDER_BUFFER_INR", 20))
BUY_REJECT_COOLDOWN_SEC = int(os.environ.get("INDIAN_BOT_BUY_REJECT_COOLDOWN_SEC", 600))

_NO_NEW_BUYS_UNTIL = 0.0
_SYMBOL_BUY_BLOCK_UNTIL: dict = {}


def _load_allocation(broker_name: str) -> dict:
    """Load fund allocation overrides from DB (silently falls back to env defaults)."""
    try:
        import db as _db
        user_id = int(os.environ.get("BOT_USER_ID", 1))
        return _db.get_fund_allocation(user_id, broker_name)
    except Exception:
        return {}


_LAST_MODE_LOGGED = None  # avoid spamming the log when mode is unchanged

def _apply_allocation(broker_name: str):
    """
    Override module-level config from DB allocation settings.
    get_fund_allocation() already merges in trading-mode preset values
    (ruthless/balanced/slow_gainer), so by the time we read max_positions,
    stop_pct, tp_pct here they reflect the active mode.
    """
    global BUDGET_PER_TRADE, TOTAL_BUDGET, MAX_POSITIONS, STOP_PCT, TP_PCT
    global MIN_CONFIDENCE, POSITION_PCT, _LAST_MODE_LOGGED
    alloc = _load_allocation(broker_name)
    if alloc.get("max_positions", 0) > 0:
        MAX_POSITIONS = int(alloc["max_positions"])
    if alloc.get("stop_pct", 0) > 0:
        STOP_PCT = alloc["stop_pct"]
    if alloc.get("tp_pct", 0) > 0:
        TP_PCT = alloc["tp_pct"]
    if alloc.get("min_confidence", 0) > 0:
        MIN_CONFIDENCE = int(alloc["min_confidence"])
    if alloc.get("position_pct", 0) > 0:
        POSITION_PCT = float(alloc["position_pct"])
    if alloc.get("budget", 0) > 0:
        # DB "budget" is the user's total broker capital ceiling. Split it
        # across the active mode's max slots; do not spend it once per symbol.
        TOTAL_BUDGET = float(alloc["budget"])
        BUDGET_PER_TRADE = (TOTAL_BUDGET / max(1, MAX_POSITIONS)) * (POSITION_PCT / 100.0)
    # Surface the active mode in the state file (so the dashboard can show it)
    # and log the change once whenever it flips.
    mode = (alloc.get("trading_mode") or "balanced").lower()
    _state["trading_mode"] = mode
    _state["mode_label"]   = alloc.get("mode_label",   mode.title())
    _state["mode_tagline"] = alloc.get("mode_tagline", "")
    _state["allocation"] = {
        "total_budget":     TOTAL_BUDGET,
        "budget_per_trade": BUDGET_PER_TRADE,
        "max_positions":    MAX_POSITIONS,
        "stop_pct":         STOP_PCT,
        "tp_pct":           TP_PCT,
        "min_confidence":   MIN_CONFIDENCE,
        "position_pct":     POSITION_PCT,
        "trading_mode":     mode,
    }
    if mode != _LAST_MODE_LOGGED:
        print(f"[mode] {broker_name} switched to '{mode}' "
              f"(max_pos={MAX_POSITIONS}, stop={STOP_PCT}%, tp={TP_PCT}%, "
              f"total_budget=₹{TOTAL_BUDGET:,.0f}, per_trade=₹{BUDGET_PER_TRADE:,.0f}, "
              f"min_conf={MIN_CONFIDENCE})", flush=True)
        _LAST_MODE_LOGGED = mode


def _is_auto_trade_enabled(broker_name: str) -> bool:
    """Returns True if the user has enabled autonomous trading for this broker."""
    try:
        import db as _db
        user_id = int(os.environ.get("BOT_USER_ID", 1))
        alloc = _db.get_fund_allocation(user_id, broker_name)
        return bool(alloc.get("auto_trade", 0))
    except Exception:
        # If DB is unavailable, respect env var
        return os.environ.get("INDIAN_BOT_AUTO_TRADE", "0") == "1"

# ── NIFTY 50 watchlist (NSE trading symbols) ──────────────────
# Full NIFTY 50 constituents — high-volume large-caps suitable for intraday.
# HDFC merged into HDFCBANK (2023) so removed from list.
WATCHLIST = [
    # IT
    "TCS", "INFY", "WIPRO", "HCLTECH", "TECHM", "LTIM",
    # Banking & Financial
    "HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK",
    "BAJFINANCE", "BAJAJFINSV", "INDUSINDBK", "SBILIFE", "HDFCLIFE", "SHRIRAMFIN",
    # Energy / Oil & Gas
    "RELIANCE", "ONGC", "COALINDIA", "BPCL",
    # Auto
    "MARUTI", "TATAMOTORS", "M&M", "BAJAJ-AUTO", "HEROMOTOCO", "EICHERMOT",
    # Consumer / FMCG
    "HINDUNILVR", "ITC", "NESTLEIND", "BRITANNIA", "TATACONSUM", "TITAN", "ASIANPAINT",
    # Pharma
    "SUNPHARMA", "DRREDDY", "DIVISLAB", "CIPLA", "APOLLOHOSP",
    # Metals & Materials
    "TATASTEEL", "JSWSTEEL", "HINDALCO", "ULTRACEMCO", "GRASIM",
    # Telecom / Power / Infra
    "BHARTIARTL", "NTPC", "POWERGRID", "LT",
    # Adani
    "ADANIENT", "ADANIPORTS",
]

# Approximate NSE symbol tokens for Angel One (used for candle data)
# Real tokens must be looked up from Angel One symbol master; these are common ones.
# If a token is missing, candle data fetch is skipped for that symbol.
SYMBOL_TOKENS: dict = {
    "RELIANCE": "2885",    "TCS": "11536",      "HDFCBANK": "1333",
    "ICICIBANK": "4963",   "INFY": "1594",      "HINDUNILVR": "1394",
    "SBIN": "3045",        "BAJFINANCE": "317",  "BHARTIARTL": "10604",
    "WIPRO": "3787",       "KOTAKBANK": "1922",  "LT": "11483",
    "AXISBANK": "5900",    "MARUTI": "10999",   "TITAN": "3506",
    "TATAMOTORS": "3432",  "TATASTEEL": "3499", "ADANIENT": "25",
    "TECHM": "13538",      "HCLTECH": "7229",   "NTPC": "11630",
    "ONGC": "2475",        "POWERGRID": "14977","HDFC": "1330",
    "SUNPHARMA": "3351",   "DRREDDY": "881",    "DIVISLAB": "10940",
    "ASIANPAINT": "236",   "ULTRACEMCO": "11532","ADANIPORTS": "15083",
}

# ── Bot state ─────────────────────────────────────────────────
_state = {
    "started":         None,
    "broker":          None,
    "daily_pnl":       0.0,
    "daily_trades":    0,
    "trading_paused":  False,
    "pause_reason":    "",
    "positions":       [],
    "watchlist":       list(WATCHLIST),
    "last_scan":       None,
    "equity":          0.0,
    "log":             [],
}

open_positions: dict = {}   # sym → {qty, entry, stop, trail_hi, tp, partial_taken}


def save_state():
    try:
        safe_io.write_json_atomic(STATE_FILE, _state, indent=2)
    except Exception as e:
        print(f"  [WARN] save_state failed: {e}")


def log_event(msg: str):
    ts = now_ist().strftime("%H:%M:%S")
    print(f"  [{ts}] {msg}")
    _state["log"] = ([{"t": ts, "m": msg}] + _state["log"])[:100]


def now_ist():
    return datetime.now(IST)


def _num(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _position_qty(row: dict) -> int:
    """Normalize broker position quantity fields."""
    return int(_num(
        row.get("quantity", row.get("netqty", row.get("qty", 0))),
        0,
    ))


def _position_ltp(row: dict, fallback: float = 0.0) -> float:
    return _num(
        row.get("last_price", row.get("ltp", row.get("current_price", fallback))),
        fallback,
    )


def _position_avg(row: dict, fallback: float = 0.0) -> float:
    return _num(
        row.get("average_price", row.get("avgnetprice", row.get("entry", fallback))),
        fallback,
    )


def _available_margin(broker) -> float:
    """Return the broker's current available margin for new entries."""
    try:
        funds = broker.get_funds() or {}
        if isinstance(broker, ZerodhaBroker):
            equity = funds.get("equity", {}) if isinstance(funds, dict) else {}
            avail = equity.get("available", {}) if isinstance(equity, dict) else {}
            # Zerodha's equity.net is the best match for "available margin"
            # in rejection messages; fall back to live/cash values.
            return _num(
                equity.get("net")
                or avail.get("live_balance")
                or avail.get("cash")
                or avail.get("opening_balance"),
                0,
            )
        if isinstance(broker, AngelOneBroker):
            return _num(funds.get("availablecash") or funds.get("net"), 0)
    except Exception as e:
        log_event(f"[scan] margin fetch failed: {e} — assuming 0")
    return 0.0


def _required_margin_estimate(price: float, qty: int) -> float:
    """Conservative entry estimate: cash value + buffer + small order cushion."""
    return (price * qty * (1 + MARGIN_BUFFER_PCT / 100.0)) + ORDER_BUFFER_INR


def _max_affordable_qty(price: float, cash_limit: float, margin_limit: float) -> int:
    if price <= 0:
        return 0
    limit = min(cash_limit, margin_limit)
    unit = price * (1 + MARGIN_BUFFER_PCT / 100.0)
    return max(0, int((limit - ORDER_BUFFER_INR) // unit))


def _entry_block_reason(symbol: str) -> str:
    now_ts = time.time()
    if now_ts < _NO_NEW_BUYS_UNTIL:
        return f"recent margin rejection — cooling off {int(_NO_NEW_BUYS_UNTIL - now_ts)}s"
    until = _SYMBOL_BUY_BLOCK_UNTIL.get(symbol.upper(), 0)
    if now_ts < until:
        return f"{symbol} rejected recently — cooling off {int(until - now_ts)}s"
    return ""


def _record_buy_rejection(symbol: str, message: str):
    global _NO_NEW_BUYS_UNTIL
    now_ts = time.time()
    _SYMBOL_BUY_BLOCK_UNTIL[symbol.upper()] = now_ts + BUY_REJECT_COOLDOWN_SEC
    if "insufficient" in message.lower() or "margin" in message.lower() or "fund" in message.lower():
        _NO_NEW_BUYS_UNTIL = max(_NO_NEW_BUYS_UNTIL, now_ts + BUY_REJECT_COOLDOWN_SEC)


# ── Trade log ─────────────────────────────────────────────────

def load_log() -> list:
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE) as f:
        try:
            return json.load(f)
        except Exception:
            return []


def append_log(entry: dict):
    safe_io.append_json_list_atomic(LOG_FILE, entry, max_entries=5000)


def _broker_open_positions(broker) -> dict:
    """Return {symbol: {qty, avg_price, exchange}} for whatever the broker
    actually has open right now. Normalises across Zerodha + Angel One."""
    out = {}
    try:
        if isinstance(broker, ZerodhaBroker):
            raw = broker.get_positions() or {}
            day = raw.get("day") if isinstance(raw, dict) else None
            net = raw.get("net") if isinstance(raw, dict) else None
            for p in (net or day or []):
                qty = _position_qty(p)
                if qty == 0:
                    continue
                out[p.get("tradingsymbol", "")] = {
                    "qty":       qty,
                    "avg_price": _position_avg(p, 0),
                    "exchange":  p.get("exchange") or "NSE",
                }
        elif isinstance(broker, AngelOneBroker):
            for p in (broker.get_positions() or []):
                qty = _position_qty(p)
                if qty == 0:
                    continue
                out[p.get("tradingsymbol", "")] = {
                    "qty":       qty,
                    "avg_price": _position_avg(p, 0),
                    "exchange":  p.get("exchange") or "NSE",
                }
    except Exception as e:
        log_event(f"  could not fetch broker positions: {e}")
    return out


def _reconcile_positions_with_broker(broker):
    """
    Reconcile in-memory `open_positions` (tracked by bot) with the broker's
    actual open positions. Three resolutions:

      1. tracked but not at broker  → drop from tracking (broker closed it
                                       while bot was down — likely manual
                                       or by another process). Logged.
      2. at broker but not tracked  → adopt with sentinel stop/tp values
                                       so check_exits() will manage it.
                                       Logged loudly so the operator knows.
      3. tracked AND at broker      → keep tracked metadata (entry, stop,
                                       trail_hi) but resync qty/avg_price
                                       to broker truth.
    """
    broker_pos = _broker_open_positions(broker)
    tracked    = set(open_positions.keys())
    actual     = set(broker_pos.keys())
    only_tracked = tracked - actual
    only_actual  = actual - tracked
    both         = tracked & actual

    for sym in only_tracked:
        log_event(f"  reconcile: {sym} tracked but not at broker — dropping")
        open_positions.pop(sym, None)

    for sym in only_actual:
        bp = broker_pos[sym]
        log_event(f"  reconcile: {sym} held at broker but not tracked "
                  f"(qty={bp['qty']} avg=₹{bp['avg_price']:.2f}) — adopting")
        # Conservative fallback stop/tp (configured pcts vs broker entry)
        entry = bp["avg_price"] or 0
        open_positions[sym] = {
            "qty":       bp["qty"],
            "entry":     entry,
            "stop":      entry * (1 - STOP_PCT / 100) if entry > 0 else 0,
            "tp":        entry * (1 + TP_PCT  / 100) if entry > 0 else 0,
            "trail_hi":  entry,
            "exchange":  bp.get("exchange", "NSE"),
            "gtt_id":    None,        # no associated bracket — bot will exit on signal
            "adopted":   True,
            "adopted_at": now_ist().isoformat(),
        }

    for sym in both:
        bp = broker_pos[sym]
        # Resync just the parts that should always reflect broker truth.
        open_positions[sym]["qty"]       = bp["qty"]
        open_positions[sym]["entry"]     = open_positions[sym].get("entry") or bp["avg_price"]
        if not open_positions[sym].get("exchange"):
            open_positions[sym]["exchange"] = bp.get("exchange", "NSE")

    if only_tracked or only_actual:
        save_positions()
    log_event(f"  reconciliation OK — broker={len(actual)} tracked={len(tracked)} "
              f"adopted={len(only_actual)} dropped={len(only_tracked)}")


def _safe_cancel_gtt(broker, gtt_id, symbol: str = "?", retries: int = 3):
    """Cancel a Zerodha GTT with bounded retries. Failed cancellations are
    a real-money risk: a stale GTT can sell phantom quantity tomorrow.
    Logs loudly on every attempt and writes a dedicated 'gtt_orphan' log
    if all retries fail so the operator can clean up at Kite manually.
    """
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            broker.delete_gtt(gtt_id)
            log_event(f"GTT {gtt_id} ({symbol}) cancelled OK")
            return True
        except Exception as e:
            last_err = e
            log_event(f"GTT cancel attempt {attempt}/{retries} for "
                      f"{gtt_id} ({symbol}) failed: {e}")
            time.sleep(0.5 * attempt)  # tiny backoff
    # All retries exhausted — append to an orphan log the operator can audit
    try:
        orphan_path = os.path.join(BASE_DIR, "gtt_orphans.json")
        safe_io.append_json_list_atomic(orphan_path, {
            "ts":      now_ist().isoformat(),
            "gtt_id":  gtt_id,
            "symbol":  symbol,
            "error":   str(last_err)[:300],
        }, max_entries=1000)
    except Exception as e:
        log_event(f"  could not record orphan GTT: {e}")
    log_event(f"!! GTT {gtt_id} ({symbol}) NOT CANCELLED — "
              f"check Kite GTT page and clear it manually !!")
    return False


def save_positions():
    try:
        safe_io.write_json_atomic(POSITIONS_F, open_positions, indent=2)
    except Exception as e:
        print(f"  [WARN] save_positions failed: {e}")


def load_positions_from_disk() -> dict:
    if not os.path.exists(POSITIONS_F):
        return {}
    try:
        with open(POSITIONS_F) as f:
            return json.load(f)
    except Exception:
        return {}


# ── Market hours ──────────────────────────────────────────────

def market_open_ist() -> bool:
    n = now_ist()
    return dtime(9, 15) <= n.time() <= dtime(15, 30) and n.weekday() < 5


def in_entry_window() -> bool:
    n = now_ist().time()
    return dtime(9, 30) <= n <= dtime(14, 45)


def eod_time() -> bool:
    return now_ist().time() >= dtime(15, 15)


# ── Candle data from Angel One ─────────────────────────────────

def _angel_candles(broker: AngelOneBroker, symbol: str, interval: str = "FIVE_MINUTE", bars: int = 80) -> list:
    """5-day lookback for warmup so indicators are valid at market open."""
    from datetime import timedelta
    token = SYMBOL_TOKENS.get(symbol)
    if not token:
        return []
    now = now_ist()
    from_dt = (now - timedelta(days=5)).replace(hour=9, minute=15, second=0, microsecond=0)
    from_str = from_dt.strftime("%Y-%m-%d %H:%M")
    to_str   = now.strftime("%Y-%m-%d %H:%M")
    try:
        raw = broker.get_candles("NSE", token, interval, from_str, to_str)
    except Exception:
        return []
    # raw: list of [timestamp, open, high, low, close, volume]
    result = []
    for c in raw:
        if len(c) >= 6:
            result.append({"t": c[0], "o": c[1], "h": c[2], "l": c[3], "c": c[4], "v": c[5]})
    return result[-bars:]


# Dynamic instrument-token cache for Zerodha (filled lazily)
_ZRD_TOKEN_CACHE: dict = {}


# Global flag set when access_token is rejected — main loop checks this
# and re-instantiates the broker to pick up fresh creds from DB.
_ZRD_TOKEN_STALE = False


def _is_auth_error(exc: Exception) -> bool:
    s = str(exc).lower()
    return ("access_token" in s or "api_key" in s
            or "tokenexception" in s or "permissionexception" in s
            or "session" in s and "invalid" in s)


def _zerodha_get_token(broker: ZerodhaBroker, symbol: str) -> str:
    """Resolve Zerodha NSE instrument token for a trading symbol, cached."""
    global _ZRD_TOKEN_STALE
    if symbol in _ZRD_TOKEN_CACHE:
        return _ZRD_TOKEN_CACHE[symbol]
    try:
        ltp = broker.get_ltp([f"NSE:{symbol}"])
        info = (ltp or {}).get(f"NSE:{symbol}") or {}
        tok = info.get("instrument_token")
        if tok:
            _ZRD_TOKEN_CACHE[symbol] = str(tok)
            return str(tok)
    except Exception as e:
        if _is_auth_error(e):
            _ZRD_TOKEN_STALE = True   # signal main loop to refresh broker
            # Only log once per stale cycle to avoid log spam
            if not getattr(_zerodha_get_token, "_logged_stale", False):
                log_event(f"Zerodha access_token stale — will refresh from DB")
                _zerodha_get_token._logged_stale = True
        else:
            log_event(f"token lookup failed {symbol}: {str(e)[:80]}")
    return ""


def _zerodha_candles(broker: ZerodhaBroker, symbol: str, interval: str = "5minute", bars: int = 80) -> list:
    """
    Fetch candles with 5-day lookback so indicators (EMA/RSI/VWAP) are
    immediately valid at market open instead of waiting 2 hours for
    today's candles to accumulate.
    """
    from datetime import timedelta
    token = _zerodha_get_token(broker, symbol)
    if not token:
        return []
    now = now_ist()
    # Lookback 5 days so we always have >=80 bars (handles weekends/holidays)
    from_dt = (now - timedelta(days=5)).replace(hour=9, minute=15, second=0, microsecond=0)
    try:
        raw = broker.get_candles(
            token, interval,
            from_dt.strftime("%Y-%m-%d %H:%M:%S"),
            now.strftime("%Y-%m-%d %H:%M:%S"),
        )
    except Exception as e:
        log_event(f"zerodha candles {symbol}: {str(e)[:80]}")
        return []
    result = []
    for c in (raw or []):
        if len(c) >= 6:
            result.append({"t": c[0], "o": c[1], "h": c[2], "l": c[3], "c": c[4], "v": c[5]})
    return result[-bars:]


def _norm_positions(raw):
    """Normalize broker.get_positions() to a flat list of dicts.
    Zerodha returns {"net":[...], "day":[...]}, AngelOne returns a list."""
    if raw is None:
        return []
    if isinstance(raw, dict):
        return raw.get("net") or raw.get("positions") or []
    if isinstance(raw, list):
        return raw
    return []


def get_bars(broker, symbol: str, bars: int = 80) -> list:
    if isinstance(broker, AngelOneBroker):
        return _angel_candles(broker, symbol, "FIVE_MINUTE", bars)
    if isinstance(broker, ZerodhaBroker):
        return _zerodha_candles(broker, symbol, "5minute", bars)
    return []


# ── Indicators (identical to intraday_bot_v2.py) ──────────────

def ema_series(vals, n):
    if len(vals) < n:
        return [None] * len(vals)
    k = 2 / (n + 1)
    out = [None] * (n - 1)
    s = sum(vals[:n]) / n
    out.append(s)
    for v in vals[n:]:
        s = v * k + s * (1 - k)
        out.append(s)
    return out


def ema_val(vals, n):
    s = ema_series(vals, n)
    return next((x for x in reversed(s) if x is not None), None)


def rsi_val(closes, n=14):
    if len(closes) < n + 1:
        return 50
    gains  = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag = sum(gains[-n:]) / n
    al = sum(losses[-n:]) / n
    return 100 if al == 0 else 100 - 100 / (1 + ag / al)


def atr_val(bars, n=14):
    if len(bars) < n + 1:
        return None
    trs = [
        max(
            bars[i]["h"] - bars[i]["l"],
            abs(bars[i]["h"] - bars[i - 1]["c"]),
            abs(bars[i]["l"] - bars[i - 1]["c"]),
        )
        for i in range(1, len(bars))
    ]
    return sum(trs[-n:]) / n


def vwap_val(bars):
    num = den = 0
    for b in bars:
        tp = (b["h"] + b["l"] + b["c"]) / 3
        num += tp * b["v"]
        den += b["v"]
    return num / den if den else None


def macd(closes):
    if len(closes) < 35:
        return None, None, None
    e12 = ema_series(closes, 12)
    e26 = ema_series(closes, 26)
    macd_line = [
        (a - b) if (a is not None and b is not None) else None
        for a, b in zip(e12, e26)
    ]
    valid = [x for x in macd_line if x is not None]
    if len(valid) < 9:
        return None, None, None
    signal = ema_series(valid, 9)
    ml = valid[-1]
    sl = signal[-1]
    if sl is None:
        return None, None, None
    return ml, sl, ml - sl


def bollinger(closes, n=20, k=2):
    if len(closes) < n:
        return None, None, None
    window = closes[-n:]
    mid = sum(window) / n
    std = (sum((x - mid) ** 2 for x in window) / n) ** 0.5
    return mid + k * std, mid, mid - k * std


def ema_cross(closes, fast=9, slow=21):
    ef = ema_series(closes, fast)
    es = ema_series(closes, slow)
    if any(x is None for x in [ef[-1], es[-1], ef[-2], es[-2]]):
        return "hold"
    if ef[-2] <= es[-2] and ef[-1] > es[-1]:
        return "buy"
    if ef[-2] >= es[-2] and ef[-1] < es[-1]:
        return "sell"
    return "hold"


# ── India VIX filter ──────────────────────────────────────────
# VIX thresholds: pause new entries above PAUSE, halt all entries above HALT
INDIA_VIX_PAUSE = float(os.environ.get("INDIA_VIX_PAUSE", 20.0))
INDIA_VIX_HALT  = float(os.environ.get("INDIA_VIX_HALT",  25.0))

# Cache + consecutive-failure counter. Once we've failed N times in a row
# AND have no fresh cached value, switch to fail-CLOSED (block new entries)
# rather than fail-open. Letting the bot trade through unknown volatility
# is the riskier choice.
_vix_cache = {"value": None, "ts": 0, "consecutive_failures": 0}
INDIA_VIX_FAIL_CLOSED_AFTER = 3   # consecutive fetch failures
INDIA_VIX_CACHE_MAX_AGE     = 1800  # 30 min — older cache = treat as stale


def get_india_vix() -> tuple:
    """
    Fetch India VIX from NSE. Cached for 5 minutes to avoid hammering.

    Returns (value: float|None, status: str). Status is one of:
      'fresh'  — fresh fetch
      'cached' — using cached value (≤30 min old)
      'stale'  — cached value too old to trust
      'down'   — no value available; fetcher has failed N+ times
    """
    now_ts = time.time()
    if _vix_cache["value"] is not None and now_ts - _vix_cache["ts"] < 300:
        return _vix_cache["value"], "cached"
    try:
        resp = requests.get(
            "https://www.nseindia.com/api/allIndices",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Accept": "application/json",
                "Referer": "https://www.nseindia.com/",
            },
            timeout=8,
        )
        data = resp.json().get("data", [])
        for item in data:
            if item.get("index") == "INDIA VIX":
                vix = float(item.get("last", 0))
                _vix_cache["value"] = vix
                _vix_cache["ts"] = now_ts
                _vix_cache["consecutive_failures"] = 0
                return vix, "fresh"
        # Endpoint returned but no INDIA VIX row found
        _vix_cache["consecutive_failures"] += 1
    except Exception as e:
        _vix_cache["consecutive_failures"] += 1
        log_event(f"India VIX fetch failed ({_vix_cache['consecutive_failures']}x): {e}")

    # Fall through to cache (if young enough) or signal "down"
    cached = _vix_cache["value"]
    if cached is not None and (now_ts - _vix_cache["ts"]) < INDIA_VIX_CACHE_MAX_AGE:
        return cached, "stale"
    if _vix_cache["consecutive_failures"] >= INDIA_VIX_FAIL_CLOSED_AFTER:
        return None, "down"
    return None, "down"  # no cache, no live — also treat as down


def india_vix_ok() -> tuple:
    """
    Returns (can_enter: bool, vix_value: float|None, reason: str).
    Fail-CLOSED: if VIX is unavailable for several consecutive attempts,
    block new entries — trading blind through volatility is too risky.
    """
    vix, status = get_india_vix()
    if status == "down":
        return False, vix, ("India VIX unavailable — pausing entries "
                            f"(failed {_vix_cache['consecutive_failures']}x)")
    if vix is None:
        return False, vix, "India VIX unavailable"
    if vix >= INDIA_VIX_HALT:
        return False, vix, f"India VIX={vix:.1f} ≥ HALT {INDIA_VIX_HALT} — no new entries"
    if vix >= INDIA_VIX_PAUSE:
        return False, vix, f"India VIX={vix:.1f} ≥ PAUSE {INDIA_VIX_PAUSE} — reducing risk"
    suffix = "" if status == "fresh" else f" ({status})"
    return True, vix, f"India VIX={vix:.1f} OK{suffix}"


# ── NIFTY trend filter (replaces SPY) ────────────────────────

def nifty_bull(broker) -> bool:
    """Fetch NIFTY 50 5-min bars and check if above 21-EMA (0.2% tolerance)."""
    try:
        if isinstance(broker, AngelOneBroker):
            token = "99926000"  # Angel One NIFTY 50 index token
            now = now_ist()
            from_dt = now.replace(hour=9, minute=0, second=0, microsecond=0)
            raw = broker.get_candles(
                "NSE", token, "FIVE_MINUTE",
                from_dt.strftime("%Y-%m-%d %H:%M"),
                now.strftime("%Y-%m-%d %H:%M"),
            )
            bars = []
            for c in raw:
                if len(c) >= 6:
                    bars.append({"c": c[4]})
            if len(bars) < 25:
                return True
            closes = [b["c"] for b in bars]
            ef = ema_val(closes, 21)
            if not ef:
                return True
            return closes[-1] >= ef * 0.998
        elif isinstance(broker, ZerodhaBroker):
            # Zerodha: fetch NIFTY 50 index candles using instrument token 256265
            now = now_ist()
            from_dt = now.replace(hour=9, minute=0, second=0, microsecond=0)
            raw = broker.get_candles(
                "256265", "5minute",
                from_dt.strftime("%Y-%m-%d %H:%M:%S"),
                now.strftime("%Y-%m-%d %H:%M:%S"),
            )
            if len(raw) < 25:
                return True
            closes = [c[4] for c in raw if len(c) >= 5]
            ef = ema_val(closes, 21)
            if not ef:
                return True
            return closes[-1] >= ef * 0.998
    except Exception as e:
        log_event(f"nifty_bull fetch failed: {e}")
    return True  # fail open


# ── Scoring (adapted for Indian markets) ─────────────────────

def score_stock(broker, symbol: str) -> tuple:
    bars = get_bars(broker, symbol, 80)
    if len(bars) < 25:
        return 0, {"error": "no data"}, []

    closes = [b["c"] for b in bars]
    current = closes[-1]
    score = 0
    reasons = {}

    # EMA cross
    sig = ema_cross(closes)
    if sig == "sell":
        return 0, {"ema": "bearish"}, bars

    if sig == "buy":
        score += 25
        reasons["ema"] = "EMA bullish cross"
    else:
        score += 8
        reasons["ema"] = "EMA holding"

    # RSI
    rsi = rsi_val(closes)
    if rsi > 82 or rsi < 30:
        return 0, {"skip": f"RSI {rsi:.0f} extreme"}, bars
    if 45 <= rsi <= 75:
        score += 15
        reasons["rsi"] = f"RSI {rsi:.0f}"
    elif 35 <= rsi < 45:
        score += 5

    # VWAP
    vw = vwap_val(bars)
    if vw and current > vw:
        score += 15
        reasons["vwap"] = "above VWAP"
    elif vw and current <= vw:
        score -= 8

    # MACD
    ml, sl, hist = macd(closes)
    if ml is not None:
        if ml > sl and hist > 0:
            score += 10
            reasons["macd"] = "MACD bullish"
        elif ml < sl:
            score -= 5

    # Bollinger
    bb_up, bb_mid, bb_lo = bollinger(closes)
    if bb_up and bb_lo and bb_up != bb_lo:
        bb_pct = (current - bb_lo) / (bb_up - bb_lo)
        if bb_pct < 0.4:
            score += 10
            reasons["bb"] = "BB lower-third"
        elif bb_pct > 0.88:
            score -= 10

    return min(score, 100), reasons, bars


# ── Daily P&L ─────────────────────────────────────────────────

def calc_daily_pnl(broker, start_equity: float) -> float:
    if isinstance(broker, ZerodhaBroker):
        pnl = calc_daily_pnl_inr(broker)
        return (pnl / start_equity * 100) if start_equity > 0 else 0.0
    try:
        acc = broker.get_account()
        current = float(acc.get("equity", acc.get("net", start_equity)) or start_equity)
        if start_equity <= 0:
            return 0.0
        return (current - start_equity) / start_equity * 100
    except Exception:
        return 0.0


def calc_daily_pnl_inr(broker) -> float:
    """Gross intraday P&L from broker positions, before charges."""
    try:
        if isinstance(broker, ZerodhaBroker):
            raw = broker.get_positions() or {}
            rows = raw.get("day") or raw.get("net") or []
            return sum(_num(p.get("pnl"), 0) for p in rows)
    except Exception:
        pass
    return 0.0


def get_account_equity(broker, fallback: float = 0.0) -> float:
    if isinstance(broker, ZerodhaBroker):
        margin = _available_margin(broker)
        return margin if margin > 0 else fallback
    try:
        acc = broker.get_account()
        # Angel One returns net as string
        val = acc.get("equity") or acc.get("net") or acc.get("available_cash") or fallback
        return float(val)
    except Exception:
        return fallback


# ── Position management ───────────────────────────────────────

def update_trailing(sym: str, current_price: float):
    pos = open_positions.get(sym)
    if not pos:
        return
    trail_stop = current_price * (1 - 1.0 / 100)  # 1% trailing
    if trail_stop > pos["stop"]:
        open_positions[sym]["stop"] = round(trail_stop, 2)
        open_positions[sym]["trail_hi"] = current_price


def _adopt_position_row(row: dict) -> bool:
    sym = (row.get("tradingsymbol") or row.get("symbol", "")).upper()
    qty = _position_qty(row)
    if not sym or qty == 0 or sym in open_positions:
        return False
    entry = _position_avg(row, _position_ltp(row, 0))
    if entry <= 0:
        return False
    log_event(f"  adopt {sym}: broker has open qty={qty} avg=₹{entry:.2f}; restoring SL/TP tracking")
    open_positions[sym] = {
        "qty":       qty,
        "entry":     entry,
        "stop":      round(entry * (1 - STOP_PCT / 100), 2),
        "trail_hi":  entry,
        "tp":        round(entry * (1 + TP_PCT / 100), 2),
        "exchange":  row.get("exchange", "NSE"),
        "gtt_id":    None,
        "adopted":   True,
        "adopted_at": now_ist().isoformat(),
    }
    return True


def check_exits_indian(broker, p_list: list):
    """p_list: list of position dicts from broker.get_positions()"""
    # DEBUG: log every call so we can verify exit loop is running
    log_event(f"[check_exits] called: {len(p_list)} broker positions, {len(open_positions)} tracked")
    pos_map = {}
    adopted = False
    for p in p_list:
        sym = (p.get("tradingsymbol") or p.get("symbol", "")).upper()
        if sym:
            pos_map[sym] = p
            adopted = _adopt_position_row(p) or adopted
    if adopted:
        save_positions()

    # News kill-switch: emergency exit if breaking negative news.
    # CRITICAL: iterate over BROKER positions (source of truth) not just
    # open_positions dict (can be empty due to reconciliation drift).
    _bad_news = load_negative_news()
    if _bad_news:
        for _bp in p_list:
            _bsym = (_bp.get("tradingsymbol") or _bp.get("symbol", "")).upper()
            _bqty = _position_qty(_bp)
            if _bsym in _bad_news and _bqty != 0:
                log_event(f"NEWS KILL {_bsym}: forcing exit — negative news (broker qty={_bqty})")
                try:
                    _side = "SELL" if _bqty > 0 else "BUY"
                    if isinstance(broker, ZerodhaBroker):
                        _oid = broker.close_position(
                            tradingsymbol=_bsym,
                            quantity=abs(_bqty),
                            transaction_type=_side,
                            exchange=_bp.get("exchange", "NSE"),
                            product=_bp.get("product", "MIS"),
                        )
                        log_event(f"  NEWS KILL {_bsym} order placed → {_oid}")
                        # Cancel its GTT bracket if we have one tracked
                        _gtt = (open_positions.get(_bsym) or {}).get("gtt_id")
                        if _gtt:
                            try:
                                broker.delete_gtt(int(_gtt))
                                log_event(f"  NEWS KILL {_bsym} GTT {_gtt} cancelled")
                            except Exception:
                                pass
                    elif isinstance(broker, AngelOneBroker):
                        broker.square_off_all_positions()
                    alert_sell(_bsym, abs(_bqty), _position_avg(_bp, 0), 0, "news_kill_switch")
                    append_log({
                        "time":   now_ist().isoformat(),
                        "sym":    _bsym,
                        "action": "sell",
                        "qty":    abs(_bqty),
                        "reason": "news_kill_switch",
                        "broker": _state.get("broker", "indian"),
                        "currency": "INR",
                    })
                    open_positions.pop(_bsym, None)
                    save_positions()
                except Exception as _e:
                    log_event(f"  NEWS KILL {_bsym} FAILED: {_e}")

    for sym, pos in list(open_positions.items()):
        # News kill already handled above — skip duplicate trigger
        if sym.upper() in _bad_news:
            log_event(f"NEWS KILL {sym}: forcing exit — negative news")
            try:
                _pos_qty = abs(int(pos.get("qty", 0)))
                if _pos_qty > 0 and isinstance(broker, ZerodhaBroker):
                    _oid = broker.close_position(
                        tradingsymbol=sym,
                        quantity=_pos_qty,
                        transaction_type="SELL",
                        exchange="NSE",
                        product="MIS",
                    )
                    log_event(f"  NEWS KILL {sym} order placed → {_oid}")
                    # Cancel its GTT bracket so it doesn't double-fire
                    _gtt = pos.get("gtt_id")
                    if _gtt:
                        try:
                            broker.delete_gtt(int(_gtt))
                            log_event(f"  NEWS KILL {sym} GTT {_gtt} cancelled")
                        except Exception:
                            pass
                elif _pos_qty > 0 and isinstance(broker, AngelOneBroker):
                    broker.square_off_all_positions()  # AngelOne side
                alert_sell(sym, _pos_qty, pos.get("entry", 0), 0, "news_kill_switch")
                append_log({
                    "time":   now_ist().isoformat(),
                    "sym":    sym,
                    "action": "sell",
                    "qty":    _pos_qty,
                    "reason": "news_kill_switch",
                    "broker": _state.get("broker", "indian"),
                    "currency": "INR",
                })
                open_positions.pop(sym, None)
                save_positions()
                continue
            except Exception as _e:
                log_event(f"  NEWS KILL {sym} FAILED: {_e}")
        bp = pos_map.get(sym)
        if not bp:
            continue

        netqty = _position_qty(bp)
        if netqty == 0:
            open_positions.pop(sym, None)
            continue

        entry   = _num(pos.get("entry"), _position_avg(bp, 0))
        curr    = _position_ltp(bp, entry)
        qty     = abs(netqty)
        pct     = (curr - entry) / entry * 100 if entry else 0

        update_trailing(sym, curr)
        stop = open_positions[sym]["stop"]
        tp   = open_positions[sym]["tp"]

        reason = None
        if curr <= stop:
            reason = f"stop_loss ({pct:.2f}%)"
        elif curr >= tp:
            reason = f"take_profit ({pct:.2f}%)"

        if reason:
            log_event(f"EXIT {sym} | {reason} | curr=₹{curr:.2f}")
            try:
                if isinstance(broker, AngelOneBroker):
                    token = SYMBOL_TOKENS.get(sym, "")
                    broker.close_position_native(
                        tradingsymbol=sym,
                        symboltoken=token,
                        quantity=qty,
                        transaction_type="SELL",
                    )
                elif isinstance(broker, ZerodhaBroker):
                    broker.close_position(
                        tradingsymbol=sym,
                        quantity=qty,
                        transaction_type="SELL",
                        exchange="NSE",
                        product="MIS",
                    )
                    # Cancel the outstanding GTT so it doesn't fire again.
                    # Critical — a stale GTT will re-sell tomorrow on a phantom
                    # quantity (or worse, open a short). Retry up to 3x and
                    # surface failures so they're investigated, not silenced.
                    gtt_id = pos.get("gtt_id")
                    if gtt_id:
                        _safe_cancel_gtt(broker, gtt_id, sym)
                else:
                    broker.close_position(sym)
                open_positions.pop(sym, None)
                save_positions()
                alert_sell(sym, qty, curr, pct, reason)
                if pct < 0:
                    pe.record_loss()
                    log_event(f"  loss recorded for cool-off tracking ({pct:.2f}%)")
                append_log({
                    "time": now_ist().isoformat(),
                    "sym":  sym,
                    "action": "sell",
                    "qty":  qty,
                    "price": curr,
                    "entry_price": entry,
                    "pct":  round(pct, 2),
                    "pnl_abs": round((curr - entry) * qty, 2),
                    "reason": reason,
                    "broker": _state.get("broker", "indian"),
                    "currency": "INR",
                })
            except Exception as e:
                log_event(f"  EXIT {sym} FAILED: {e}")


# ── News kill-switch ──────────────────────────────────────────

def _log_pos_size(where: str):
    log_event(f"[POS] {where}: open_positions={len(open_positions)} keys={list(open_positions.keys())[:5]}")

def load_negative_news() -> set:
    """Returns set of tickers with breaking negative news today.
    Populated by news_scanner_indian.py running on cron/supervisor.
    File format: {"date": "YYYY-MM-DD", "tickers": ["XYZ", ...], "sources": {...}}"""
    if not os.path.exists(NEG_NEWS_F):
        return set()
    try:
        with open(NEG_NEWS_F) as f:
            d = json.load(f)
        today = now_ist().strftime("%Y-%m-%d")
        if d.get("date") == today:
            return set(t.upper() for t in d.get("tickers", []))
    except Exception as e:
        log_event(f"negative_news load failed: {e}")
    return set()


def _zerodha_order_snapshot(broker, order_id: str) -> dict:
    try:
        hist = broker.get_order_history(order_id) or []
        if isinstance(hist, list) and hist:
            return hist[-1]
    except Exception:
        pass
    try:
        for order in broker.get_orders() or []:
            if str(order.get("order_id")) == str(order_id):
                return order
    except Exception:
        pass
    return {}


def _wait_for_zerodha_fill(broker, order_id: str, symbol: str, timeout_sec: float = 8.0) -> dict:
    """Poll a fresh Zerodha order and return fill details or an error."""
    deadline = time.time() + timeout_sec
    last = {}
    while time.time() < deadline:
        last = _zerodha_order_snapshot(broker, order_id)
        status = (last.get("status") or "").upper()
        filled = int(_num(last.get("filled_quantity"), 0))
        if status == "COMPLETE" and filled > 0:
            return {
                "ok": True,
                "qty": filled,
                "price": _num(last.get("average_price"), 0),
                "status": status,
            }
        if status in ("REJECTED", "CANCELLED"):
            msg = last.get("status_message") or last.get("status_message_raw") or status
            _record_buy_rejection(symbol, msg)
            return {"ok": False, "status": status, "message": msg}
        time.sleep(0.5)

    status = (last.get("status") or "UNKNOWN").upper()
    filled = int(_num(last.get("filled_quantity"), 0))
    if filled > 0:
        try:
            broker.cancel_order(order_id, variety=last.get("variety", "regular"))
        except Exception:
            pass
        return {
            "ok": True,
            "qty": filled,
            "price": _num(last.get("average_price"), 0),
            "status": status,
            "partial": True,
        }

    try:
        broker.cancel_order(order_id, variety=last.get("variety", "regular"))
        log_event(f"  BUY {symbol} not filled quickly — cancelled pending order {order_id}")
    except Exception as e:
        log_event(f"  BUY {symbol} pending cancel failed: {e}")
    return {"ok": False, "status": status, "message": "order_not_filled"}


# ── Entry execution ───────────────────────────────────────────

def execute_buy(broker, symbol: str, qty: int, price: float, stop: float, tp: float, score: int, reasons: dict):
    block_reason = _entry_block_reason(symbol)
    if block_reason:
        log_event(f"SKIP {symbol}: {block_reason}")
        return False

    # News kill-switch: never enter if there's breaking negative news
    if symbol.upper() in load_negative_news():
        log_event(f"NEWS BLOCK {symbol}: skipping entry — negative news headline detected")
        return False

    # Last-line defense: re-check broker state right before placing order.
    # This catches races where another scan already filled a position.
    try:
        _hold = _broker_open_positions(broker)
        if symbol in _hold:
            log_event(f"SKIP {symbol}: already held by broker (qty={_hold[symbol]['qty']})")
            return False
    except Exception:
        pass

    log_event(f"BUY {qty}x {symbol} @ ₹{price:.2f} | stop=₹{stop:.2f} tp=₹{tp:.2f} score={score}")
    try:
        if isinstance(broker, AngelOneBroker):
            token = SYMBOL_TOKENS.get(symbol, "")
            order_id = broker._place_native_order(
                tradingsymbol=symbol,
                symboltoken=token,
                transaction_type="BUY",
                quantity=qty,
                order_type="MARKET",
                product_type="INTRADAY",
                exchange="NSE",
            )
        elif isinstance(broker, ZerodhaBroker):
            # Zerodha requires LIMIT orders (MARKET orders need market_protection
            # which gets converted to AMO outside trading hours). LIMIT at 0.5%
            # above signal price gives near-instant fill while avoiding slippage.
            limit_price = round(price * 1.005, 1)
            order_id = broker.place_order(
                tradingsymbol=symbol,
                transaction_type="BUY",
                quantity=qty,
                order_type="LIMIT",
                price=limit_price,
                product="MIS",
                exchange="NSE",
                tag="indianbot",
            )
            fill = _wait_for_zerodha_fill(broker, order_id, symbol)
            if not fill.get("ok"):
                log_event(f"  BUY {symbol} {fill.get('status', 'FAILED')}: {fill.get('message', '')}")
                return False
            qty = int(fill["qty"])
            if fill.get("price", 0) > 0:
                price = float(fill["price"])
                stop = round(price * (1 - STOP_PCT / 100), 2)
                tp = round(price * (1 + TP_PCT / 100), 2)
        else:
            res = broker.place_order(symbol, qty, "buy")
            order_id = res.get("id", "") if isinstance(res, dict) else str(res)

        gtt_id = None
        if isinstance(broker, ZerodhaBroker):
            # Place an OCO GTT so SL/TP fires even if the bot restarts
            gtt_id = pe.place_gtt_with_retry(
                broker, "place_oco_gtt",
                {"tradingsymbol": symbol, "exchange": "NSE", "quantity": qty,
                 "sl_price": stop, "tp_price": tp, "last_price": price, "product": "MIS"},
                log_event, max_retries=3,
            )
            if gtt_id:
                log_event(f"  GTT OCO placed → gtt_id={gtt_id} SL=₹{stop} TP=₹{tp}")

        open_positions[symbol] = {
            "qty":      qty,
            "entry":    price,
            "stop":     stop,
            "trail_hi": price,
            "tp":       tp,
            "partial_taken": False,
            "gtt_id":   gtt_id,
        }
        save_positions()
        alert_buy(symbol, qty, price, stop, tp, score, reasons)
        append_log({
            "time":  now_ist().isoformat(),
            "sym":   symbol,
            "action": "buy",
            "qty":   qty,
            "price": price,
            "stop":  stop,
            "tp":    tp,
            "score": score,
            "reasons": reasons,
            "broker": _state.get("broker", "indian"),
            "currency": "INR",
        })
        _state["daily_trades"] += 1
        return True
    except Exception as e:
        log_event(f"  BUY {symbol} FAILED: {e}")
        return False


# ── EOD square-off ────────────────────────────────────────────

def square_off_all(broker):
    log_event("EOD square-off — closing all Indian intraday positions")
    try:
        if isinstance(broker, AngelOneBroker):
            results = broker.square_off_all_positions()
            ok  = sum(1 for r in results if r.get("ok"))
            bad = [r for r in results if not r.get("ok")]
            log_event(f"Square-off: {ok} closed, {len(bad)} failed")
            for b in bad:
                log_event(f"  FAIL {b.get('symbol','?')}: {b.get('error','?')}")
        elif isinstance(broker, ZerodhaBroker):
            # Cancel any GTTs first so they don't trigger after we manually close.
            # If a cancellation fails the GTT will fire later — log loudly so
            # the operator notices and can clean up manually.
            for sym, pos in list(open_positions.items()):
                gtt_id = pos.get("gtt_id")
                if gtt_id:
                    _safe_cancel_gtt(broker, gtt_id, sym)
            results = broker.square_off_all_positions()
            log_event(f"Zerodha square-off: {len(results)} orders submitted")
        else:
            # Generic fallback if available
            if hasattr(broker, "close_all_positions"):
                broker.close_all_positions()
            elif hasattr(broker, "square_off_all_positions"):
                broker.square_off_all_positions()
    except Exception as e:
        log_event(f"Square-off error: {e}")
    open_positions.clear()
    save_positions()


# ── Main loop ─────────────────────────────────────────────────

def run():
    broker_name = os.environ.get("BROKER", "zerodha")
    user_id     = int(os.environ.get("BOT_USER_ID", 1))

    # Apply DB fund allocation overrides (budget, positions, stop/tp %)
    _apply_allocation(broker_name)

    # Wait until: (a) auto_trade is enabled AND (b) credentials are available.
    # This makes the bot resilient: it sits idle until the user enables it
    # via the dashboard, then starts trading. No human restart needed.
    _print_waiting = True
    while True:
        if not _is_auto_trade_enabled(broker_name):
            if _print_waiting:
                print(f"[INDIAN BOT] Waiting — auto_trade is OFF for {broker_name}. "
                      "Toggle it ON in dashboard → Overview.", flush=True)
                _print_waiting = False
            time.sleep(20)
            continue
        try:
            broker = get_broker(broker_name, user_id=user_id)
            break  # creds OK, auto_trade ON — proceed
        except Exception as _ce:
            if _print_waiting:
                print(f"[INDIAN BOT] Waiting on credentials: {_ce}", flush=True)
                _print_waiting = False
            time.sleep(20)
            continue

    # Token refresh helper - called each loop iteration if creds went stale
    def _refresh_broker_if_stale(current_broker):
        global _ZRD_TOKEN_STALE
        if not _ZRD_TOKEN_STALE:
            return current_broker
        log_event(f"[REFRESH] starting broker refresh — open_positions size BEFORE: {len(open_positions)}")
        try:
            new_broker = get_broker(broker_name, user_id=user_id)
            log_event(f"[REFRESH] new broker created — open_positions size AFTER get_broker: {len(open_positions)}")
            # Clear token cache (instrument tokens may differ across sessions)
            _ZRD_TOKEN_CACHE.clear()
            # Reset logged flag
            if hasattr(_zerodha_get_token, "_logged_stale"):
                _zerodha_get_token._logged_stale = False
            _ZRD_TOKEN_STALE = False
            log_event("Zerodha broker refreshed with fresh DB credentials")
            return new_broker
        except Exception as e:
            log_event(f"Broker refresh failed: {str(e)[:120]} — will retry next cycle")
            return current_broker

    _state["broker"] = broker_name
    _state["started"] = now_ist().isoformat()
    _state["allocation"] = {
        "total_budget":     TOTAL_BUDGET,
        "budget_per_trade": BUDGET_PER_TRADE,
        "max_positions":    MAX_POSITIONS,
        "stop_pct":         STOP_PCT,
        "tp_pct":           TP_PCT,
        "min_confidence":   MIN_CONFIDENCE,
        "position_pct":     POSITION_PCT,
        "trading_mode":     _state.get("trading_mode", "balanced"),
    }

    # Ensure login for Angel One
    if isinstance(broker, AngelOneBroker):
        try:
            broker.ensure_logged_in()
            log_event(f"Angel One login OK — {broker.client_id}")
        except Exception as e:
            log_event(f"Angel One login FAILED: {e}")
            raise

    start_equity = get_account_equity(broker, 100000)
    _state["equity"] = start_equity

    persisted = load_positions_from_disk()
    if persisted:
        open_positions.update(persisted)
        log_event(f"Restored {len(persisted)} tracked positions from disk")

    # ── Startup reconciliation: broker is the source of truth ───────────
    # If the bot crashed mid-trade, the disk view of positions can drift
    # from what the broker actually holds. Re-anchor against the broker
    # before resuming so we never re-enter a symbol or miss an exit.
    try:
        _reconcile_positions_with_broker(broker)
    except Exception as e:
        log_event(f"  startup reconciliation FAILED ({e}) — "
                  f"continuing with disk view; manual check recommended")

    print("=" * 60)
    print(f"  INDIAN INTRADAY BOT  |  {now_ist().strftime('%Y-%m-%d')}")
    print(f"  Broker: {broker_name}  |  Equity: ₹{start_equity:,.2f}")
    print("=" * 60)

    alert_startup(start_equity, start_equity, len(WATCHLIST))

    while True:
        now = now_ist()
        # Surface market state to the dashboard so the auto-trade pill can
        # explain why the bot isn't trading even when it's "ON".
        _state["market_open"] = market_open_ist()
        _state["now_ist"]     = now.strftime("%Y-%m-%d %H:%M:%S")

        # ── NSE holiday auto-skip ─────────────────────────
        holiday, hname = pe.is_nse_holiday(now)
        if holiday:
            log_event(f"NSE holiday ({hname}) — bot idle. Sleeping 1 hour.")
            save_state()
            time.sleep(3600)
            continue

        if not _state["market_open"]:
            log_event("Market closed — sleeping 60s")
            save_state()
            time.sleep(60)
            continue

        # ── Pre-EOD / EOD flatten (avoid Zerodha auto-square penalty) ──
        eod_phase = pe.pre_eod_phase()
        if eod_phase == "closed":
            log_event("Market closed — bot finished for today.")
            save_state()
            break
        if eod_phase == "hard_flat":
            log_event("HARD FLAT (15:14-15:20): MARKET exits to beat auto-square penalty")
            square_off_all(broker)
            daily_pnl = calc_daily_pnl(broker, start_equity)
            alert_eod(get_account_equity(broker, start_equity), daily_pnl, _state["daily_trades"], "")
            log_event("EOD done.")
            save_state()
            break
        if eod_phase == "soft_flat":
            # Cancel any pending LIMITs first, then place LIMIT exits
            try:
                if isinstance(broker, ZerodhaBroker):
                    for o in (broker.get_orders() or []):
                        if (o.get("status") or "").upper() in ("OPEN", "TRIGGER PENDING"):
                            try: broker.cancel_order(o["order_id"], variety=o.get("variety","regular"))
                            except Exception: pass
            except Exception:
                pass
            square_off_all(broker)
            log_event("SOFT FLAT (15:10-15:14): LIMIT exits placed — waiting for fills")
            save_state()
            time.sleep(60)
            continue
        # Legacy eod_time() guard kept for safety
        if eod_time():
            square_off_all(broker)
            daily_pnl = calc_daily_pnl(broker, start_equity)
            alert_eod(get_account_equity(broker, start_equity), daily_pnl, _state["daily_trades"], "")
            log_event("EOD done. Bot finished for today.")
            save_state()
            break

        # ── Re-login Angel One every cycle (ensure token fresh) ──
        if isinstance(broker, AngelOneBroker):
            try:
                broker.ensure_logged_in()
            except Exception as e:
                log_event(f"Token refresh failed: {e} — retrying in 60s")
                time.sleep(60)
                continue

        # ── Daily loss limit ───────────────────────────────
        if isinstance(broker, ZerodhaBroker):
            daily_pnl_inr = calc_daily_pnl_inr(broker)
            daily_pnl = (daily_pnl_inr / start_equity * 100) if start_equity > 0 else 0.0
        else:
            daily_pnl = calc_daily_pnl(broker, start_equity)
            daily_pnl_inr = (daily_pnl / 100.0) * start_equity
        _state["daily_pnl"] = daily_pnl
        _state["daily_pnl_inr"] = round(daily_pnl_inr, 2)
        loss_limit_env = os.environ.get("INDIAN_BOT_DAILY_LOSS_INR")
        DAILY_LOSS_LIMIT_INR = (
            float(loss_limit_env)
            if loss_limit_env
            else max(25.0, start_equity * DAILY_LOSS_LIMIT / 100.0)
        )
        if daily_pnl_inr <= -DAILY_LOSS_LIMIT_INR:
            if not _state["trading_paused"]:
                log_event(f"Daily loss limit hit (₹{daily_pnl_inr:.2f}, {daily_pnl:.2f}%) — no new entries")
                alert_daily_loss(daily_pnl)
                _state["trading_paused"] = True
                _state["pause_reason"] = f"Daily loss ₹{daily_pnl_inr:.2f}"
            # Still monitor exits
            try:
                positions = _norm_positions(broker.get_positions())
                check_exits_indian(broker, positions)
            except Exception as e:
                log_event(f"Exit check error: {e}")
            save_state()
            time.sleep(60)
            continue

        if _state["trading_paused"]:
            _state["trading_paused"] = False

        print(f"\n[{now.strftime('%H:%M:%S')} IST] ── Scan cycle")

        # ── India VIX filter ───────────────────────────────
        vix_ok, vix_val, vix_reason = india_vix_ok()
        if not vix_ok:
            log_event(f"VIX HALT: {vix_reason}")
            _state["vix"] = vix_val
            _state["pause_reason"] = vix_reason
            save_state()
            time.sleep(60)
            continue
        _state["vix"] = vix_val

        # ── NIFTY trend filter ─────────────────────────────
        nifty_up = nifty_bull(broker)
        if not nifty_up:
            log_event("NIFTY below 21-EMA — skipping new longs")

        # ── Check exits ────────────────────────────────────
        try:
            positions = _norm_positions(broker.get_positions())
        except Exception as e:
            log_event(f"get_positions error: {e}")
            positions = []
        check_exits_indian(broker, positions)

        # ── New entries ────────────────────────────────────
        if not in_entry_window():
            log_event("Outside entry window 09:30–14:45 IST")
            save_state()
            time.sleep(60)
            continue


        # ── Stale order cleanup: cancel LIMITs older than 5 min ──
        try:
            if isinstance(broker, ZerodhaBroker):
                for o in (broker.get_orders() or []):
                    if pe.is_stale_order(o):
                        try:
                            broker.cancel_order(o["order_id"], variety=o.get("variety","regular"))
                            log_event(f"  STALE cancelled: {o.get('tradingsymbol')} {o.get('order_id')}")
                        except Exception as _e:
                            log_event(f"  STALE cancel failed: {_e}")
        except Exception:
            pass

        # ── Quality window filter (skip open volatility + lunch chop) ──
        qok, qreason = pe.is_quality_window(now)
        if not qok:
            log_event(f"Quality window skip: {qreason}")
            save_state()
            time.sleep(60)
            continue

        # ── Cool-off after consecutive losses ──
        cok, creason = pe.in_cooloff()
        if cok:
            log_event(f"Cool-off active: {creason}")
            save_state()
            time.sleep(60)
            continue

        # ── Source-of-truth slot accounting: USE BROKER, not in-memory ──
        broker_held = _broker_open_positions(broker)   # dict sym → {qty, avg_price, ...}
        held_syms   = set(broker_held.keys())
        cur_count   = len(held_syms)
        log_event(f"[scan] broker positions: {cur_count}/{MAX_POSITIONS}  "
                  f"held: {sorted(held_syms) if held_syms else '(none)'}")

        if cur_count >= MAX_POSITIONS or not nifty_up:
            log_event(f"Slots full ({cur_count}/{MAX_POSITIONS}) or NIFTY bearish — no new buys")
            save_state()
            time.sleep(60)
            continue

        # ── Pre-trade margin check ───────────────────────────────────────
        # Use actual broker-available margin and the per-trade slice of the
        # user's total budget. Never assume MIS leverage is available.
        avail_margin = _available_margin(broker)
        if _entry_block_reason("*"):
            log_event(f"No new buys: {_entry_block_reason('*')}")
            save_state()
            time.sleep(60)
            continue
        min_cash_for_scan = min(BUDGET_PER_TRADE, avail_margin)
        log_event(f"[scan] available margin: Rs{avail_margin:,.2f}  "
                  f"per-trade cap: Rs{BUDGET_PER_TRADE:,.2f}")
        if min_cash_for_scan <= ORDER_BUFFER_INR:
            log_event("Insufficient usable margin — skipping new entries")
            save_state()
            time.sleep(60)
            continue

        candidates = []
        for sym in WATCHLIST:
            # SKIP if we already hold this symbol (broker is source of truth)
            if sym in held_syms:
                continue
            # SKIP if sector concentration cap reached (max 2/sector)
            sec_ok, sec_reason = pe.can_add_to_sector(sym, held_syms)
            if not sec_ok:
                continue
            sc, reasons, bars = score_stock(broker, sym)
            log_event(f"{sym:12s} score={sc:3d}  {list(reasons.keys())[:3]}")
            # Require BOTH score ≥ active mode threshold AND volume surge.
            if sc >= MIN_CONFIDENCE and pe.has_volume_surge(bars):
                candidates.append((sc, sym, reasons, bars))

        candidates.sort(reverse=True)
        slots = MAX_POSITIONS - cur_count

        # Cap how many BUYs we issue per scan cycle; actual affordability is
        # checked per candidate below and remaining margin is decremented.
        _max_per_cycle = max(1, min(slots, len(candidates)))
        log_event(f"[scan] candidates={len(candidates)}  slots={slots}  "
                  f"max_this_cycle={_max_per_cycle}")

        remaining_margin = avail_margin
        buys_done = 0
        for sc, sym, reasons, bars in candidates:
            if buys_done >= slots:
                break
            block_reason = _entry_block_reason(sym)
            if block_reason:
                log_event(f"SKIP {sym}: {block_reason}")
                continue
            if not bars:
                continue
            price = bars[-1]["c"]
            if not price or price <= 0:
                continue

            qty = _max_affordable_qty(price, BUDGET_PER_TRADE, remaining_margin)
            if qty <= 0:
                log_event(f"SKIP {sym}: price ₹{price:.2f} exceeds per-trade/margin cap")
                continue
            required_margin = _required_margin_estimate(price, qty)
            if required_margin > remaining_margin:
                log_event(f"SKIP {sym}: needs about ₹{required_margin:,.0f}, "
                          f"available ₹{remaining_margin:,.0f}")
                continue
            stop  = round(price * (1 - STOP_PCT / 100), 2)
            tp    = round(price * (1 + TP_PCT / 100), 2)
            if execute_buy(broker, sym, qty, price, stop, tp, sc, reasons):
                buys_done += 1
                remaining_margin = max(0.0, remaining_margin - required_margin)

        if candidates and buys_done == 0:
            log_event("No candidate passed cash/margin safety checks this cycle")

        # ── Update dashboard state ─────────────────────────
        _state["last_scan"] = now.isoformat()
        _state["positions"] = [
            {
                "sym":   sym,
                "qty":   p["qty"],
                "entry": p["entry"],
                "stop":  p["stop"],
                "tp":    p["tp"],
            }
            for sym, p in open_positions.items()
        ]
        _state["equity"] = get_account_equity(broker, start_equity)
        save_state()

        # Refresh allocation each cycle so live dashboard changes take effect
        _apply_allocation(broker_name)
        print(f"  NIFTY={'bull' if nifty_up else 'bear'} | "
              f"VIX={vix_val:.1f} | "
              f"DayPnL={daily_pnl:+.2f}% | "
              f"Open={len(open_positions)}/{MAX_POSITIONS} | "
              f"Budget=₹{TOTAL_BUDGET:,.0f} total / ₹{BUDGET_PER_TRADE:,.0f} per trade")
        time.sleep(60)


def _sleep_until_next_open():
    """Sleep until 09:10 IST on next trading day."""
    from datetime import timedelta
    while True:
        now = now_ist()
        target_today = now.replace(hour=9, minute=10, second=0, microsecond=0)
        if now.weekday() < 5 and now < target_today:
            target = target_today
        else:
            days_ahead = 1
            if now.weekday() == 4:
                days_ahead = 3
            elif now.weekday() == 5:
                days_ahead = 2
            target = (now + timedelta(days=days_ahead)).replace(
                hour=9, minute=10, second=0, microsecond=0
            )
        secs = max((target - now).total_seconds(), 0)
        print(
            f"\n[INDIAN BOT] Market closed. Next session: "
            f"{target.strftime('%Y-%m-%d %H:%M IST')} "
            f"({secs / 3600:.1f}h away). Sleeping…",
            flush=True,
        )
        time.sleep(secs + 2)
        return


if __name__ == "__main__":
    import traceback

    while True:
        try:
            run()
        except KeyboardInterrupt:
            print("[INDIAN BOT] Stopped.", flush=True)
            break
        except Exception as exc:
            print(f"\n[INDIAN BOT CRASH] {exc}", flush=True)
            traceback.print_exc()
            print("[INDIAN BOT] Restarting in 45s…", flush=True)
            time.sleep(45)
            continue
        _sleep_until_next_open()
