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
  INDIAN_BOT_BUDGET_PER_TRADE  — ₹ per position (default 10000)
  INDIAN_BOT_MAX_POSITIONS     — max simultaneous positions (default 3)
  INDIAN_BOT_STOP_PCT          — stop-loss % (default 1.5)
  INDIAN_BOT_TP_PCT            — take-profit % (default 3.0)
  INDIAN_BOT_DAILY_LOSS_LIMIT  — max daily drawdown % (default 3.0)
"""

import json
import os
import time
from datetime import datetime, date, time as dtime

import numpy as np
import pytz
import requests

from brokers import get_broker
from brokers.angelone import AngelOneBroker, AngelOneError
from telegram_alerts import alert_buy, alert_sell, alert_daily_loss, alert_eod, alert_startup

# ── Paths ──────────────────────────────────────────────────────
BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
LOG_FILE       = os.path.join(BASE_DIR, "indian_trade_log.json")
STATE_FILE     = os.path.join(BASE_DIR, "indian_bot_state.json")
POSITIONS_F    = os.path.join(BASE_DIR, "indian_positions_state.json")

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
MAX_POSITIONS     = int(os.environ.get("INDIAN_BOT_MAX_POSITIONS", 3))
STOP_PCT          = float(os.environ.get("INDIAN_BOT_STOP_PCT", 1.5))
TP_PCT            = float(os.environ.get("INDIAN_BOT_TP_PCT", 3.0))
DAILY_LOSS_LIMIT  = float(os.environ.get("INDIAN_BOT_DAILY_LOSS_LIMIT", 3.0))

# ── Nifty 50 watchlist (NSE trading symbols) ──────────────────
WATCHLIST = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY",
    "HINDUNILVR", "HDFC", "SBIN", "BAJFINANCE", "BHARTIARTL",
    "WIPRO", "KOTAKBANK", "LT", "AXISBANK", "ASIANPAINT",
    "MARUTI", "TITAN", "ULTRACEMCO", "SUNPHARMA", "NTPC",
    "ONGC", "TATAMOTORS", "POWERGRID", "TATASTEEL", "ADANIENT",
    "ADANIPORTS", "TECHM", "HCLTECH", "DRREDDY", "DIVISLAB",
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
    "watchlist":       WATCHLIST[:15],
    "last_scan":       None,
    "equity":          0.0,
    "log":             [],
}

open_positions: dict = {}   # sym → {qty, entry, stop, trail_hi, tp, partial_taken}


def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(_state, f, indent=2, default=str)
    except Exception:
        pass


def log_event(msg: str):
    ts = now_ist().strftime("%H:%M:%S")
    print(f"  [{ts}] {msg}")
    _state["log"] = ([{"t": ts, "m": msg}] + _state["log"])[:100]


def now_ist():
    return datetime.now(IST)


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
    log = load_log()
    log.append(entry)
    with open(LOG_FILE, "w") as f:
        json.dump(log[-5000:], f, indent=2, default=str)


def save_positions():
    try:
        with open(POSITIONS_F, "w") as f:
            json.dump(open_positions, f, indent=2, default=str)
    except Exception:
        pass


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
    token = SYMBOL_TOKENS.get(symbol)
    if not token:
        return []
    now = now_ist()
    from_dt = now.replace(hour=9, minute=0, second=0, microsecond=0)
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


def get_bars(broker, symbol: str, bars: int = 80) -> list:
    if isinstance(broker, AngelOneBroker):
        return _angel_candles(broker, symbol, "FIVE_MINUTE", bars)
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
    except Exception:
        pass
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
    try:
        acc = broker.get_account()
        current = float(acc.get("equity", acc.get("net", start_equity)) or start_equity)
        if start_equity <= 0:
            return 0.0
        return (current - start_equity) / start_equity * 100
    except Exception:
        return 0.0


def get_account_equity(broker, fallback: float = 0.0) -> float:
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


def check_exits_indian(broker, p_list: list):
    """p_list: list of position dicts from broker.get_positions()"""
    pos_map = {}
    for p in p_list:
        sym = p.get("tradingsymbol") or p.get("symbol", "")
        if sym:
            pos_map[sym] = p

    for sym, pos in list(open_positions.items()):
        bp = pos_map.get(sym)
        if not bp:
            continue

        netqty = int(bp.get("netqty", bp.get("qty", 0)) or 0)
        if netqty == 0:
            open_positions.pop(sym, None)
            continue

        entry   = pos["entry"]
        curr    = float(bp.get("ltp") or bp.get("current_price") or entry)
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
                    broker.close_position(
                        tradingsymbol=sym,
                        symboltoken=token,
                        quantity=qty,
                        transaction_type="SELL",
                    )
                else:
                    broker.close_position(sym)
                open_positions.pop(sym, None)
                save_positions()
                alert_sell(sym, qty, curr, pct, reason)
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


# ── Entry execution ───────────────────────────────────────────

def execute_buy(broker, symbol: str, qty: int, price: float, stop: float, tp: float, score: int, reasons: dict):
    log_event(f"BUY {qty}x {symbol} @ ₹{price:.2f} | stop=₹{stop:.2f} tp=₹{tp:.2f} score={score}")
    try:
        if isinstance(broker, AngelOneBroker):
            token = SYMBOL_TOKENS.get(symbol, "")
            order_id = broker.place_order(
                tradingsymbol=symbol,
                symboltoken=token,
                transaction_type="BUY",
                quantity=qty,
                order_type="MARKET",
                product_type="INTRADAY",
                exchange="NSE",
            )
        else:
            res = broker.place_order(symbol, qty, "buy")
            order_id = res.get("id", "") if isinstance(res, dict) else str(res)

        open_positions[symbol] = {
            "qty":      qty,
            "entry":    price,
            "stop":     stop,
            "trail_hi": price,
            "tp":       tp,
            "partial_taken": False,
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
        else:
            broker.close_all_positions()
    except Exception as e:
        log_event(f"Square-off error: {e}")
    open_positions.clear()
    save_positions()


# ── Main loop ─────────────────────────────────────────────────

def run():
    broker_name = os.environ.get("BROKER", "angelone")
    broker = get_broker(broker_name)
    _state["broker"] = broker_name
    _state["started"] = now_ist().isoformat()

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

    print("=" * 60)
    print(f"  INDIAN INTRADAY BOT  |  {now_ist().strftime('%Y-%m-%d')}")
    print(f"  Broker: {broker.name}  |  Equity: ₹{start_equity:,.2f}")
    print("=" * 60)

    alert_startup(start_equity, start_equity, len(WATCHLIST))

    while True:
        now = now_ist()

        if not market_open_ist():
            log_event("Market closed — sleeping 60s")
            save_state()
            time.sleep(60)
            continue

        # ── EOD square-off ─────────────────────────────────
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
        daily_pnl = calc_daily_pnl(broker, start_equity)
        _state["daily_pnl"] = daily_pnl
        if daily_pnl <= -DAILY_LOSS_LIMIT:
            if not _state["trading_paused"]:
                log_event(f"Daily loss limit hit ({daily_pnl:.2f}%) — no new entries")
                alert_daily_loss(daily_pnl)
                _state["trading_paused"] = True
                _state["pause_reason"] = f"Daily loss {daily_pnl:.2f}%"
            # Still monitor exits
            try:
                positions = broker.get_positions()
                check_exits_indian(broker, positions)
            except Exception as e:
                log_event(f"Exit check error: {e}")
            save_state()
            time.sleep(60)
            continue

        if _state["trading_paused"]:
            _state["trading_paused"] = False

        print(f"\n[{now.strftime('%H:%M:%S')} IST] ── Scan cycle")

        # ── NIFTY trend filter ─────────────────────────────
        nifty_up = nifty_bull(broker)
        if not nifty_up:
            log_event("NIFTY below 21-EMA — skipping new longs")

        # ── Check exits ────────────────────────────────────
        try:
            positions = broker.get_positions()
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

        cur_count = len(open_positions)
        if cur_count >= MAX_POSITIONS or not nifty_up:
            log_event(f"Slots full ({cur_count}/{MAX_POSITIONS}) or NIFTY bearish — no new buys")
            save_state()
            time.sleep(60)
            continue

        candidates = []
        for sym in WATCHLIST:
            if sym in open_positions:
                continue
            sc, reasons, bars = score_stock(broker, sym)
            log_event(f"{sym:12s} score={sc:3d}  {list(reasons.keys())[:3]}")
            if sc >= 45:
                candidates.append((sc, sym, reasons, bars))

        candidates.sort(reverse=True)
        slots = MAX_POSITIONS - cur_count

        for sc, sym, reasons, bars in candidates[:slots]:
            if not bars:
                continue
            price = bars[-1]["c"]
            if not price or price <= 0:
                continue

            qty   = max(1, int(BUDGET_PER_TRADE // price))
            stop  = round(price * (1 - STOP_PCT / 100), 2)
            tp    = round(price * (1 + TP_PCT / 100), 2)
            execute_buy(broker, sym, qty, price, stop, tp, sc, reasons)

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

        print(f"  NIFTY={'bull' if nifty_up else 'bear'} | "
              f"DayPnL={daily_pnl:+.2f}% | "
              f"Open={len(open_positions)}/{MAX_POSITIONS}")
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
