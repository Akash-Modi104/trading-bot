"""
Advanced Intraday Trading Bot v2 — Full Feature Build
Features:
  1. Daily loss limit        5. Telegram alerts
  2. Trailing stop-loss      6. MACD confirmation
  3. SPY/QQQ trend filter    7. Bollinger Bands
  4. VIX fear filter         8. Market regime detection
  + VWAP, ORB, ATR, RelVol, Multi-TF, EMA/RSI, Claude AI picks
"""

import json, os, time, requests
import numpy as np
from datetime import datetime, date, time as dtime
import pytz
from telegram_alerts import (alert_buy, alert_sell, alert_daily_loss,
                              alert_vix, alert_regime, alert_eod, alert_startup)

# ── Load environment variables from .env ────────────────────────
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
try:
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)
except ImportError:
    # Fallback: manually load .env if python-dotenv not installed
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip())

# ── Broker selection ─────────────────────────────────────────
# This file is the US/Alpaca bot. For Indian markets use indian_bot.py.
_BROKER = os.environ.get("BROKER", "alpaca").lower()
if _BROKER not in ("alpaca", ""):
    raise SystemExit(
        f"[intraday_bot_v2] BROKER={_BROKER!r} is not supported here. "
        "Use indian_bot.py for Indian market brokers (angelone, zerodha)."
    )

# ── Paths ─────────────────────────────────────────────────────
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
CONFIG     = os.path.join(BASE_DIR, "alpaca_config.json")
PICKS_FILE = os.path.join(BASE_DIR, "claude_picks.json")
STRAT_FILE = os.path.join(BASE_DIR, "strategy_params.json")
LOG_FILE   = os.path.join(BASE_DIR, "trade_log.json")
STATE_FILE = os.path.join(BASE_DIR, "bot_state.json")   # live state for dashboard
NEG_NEWS_F = os.path.join(BASE_DIR, "negative_news.json")  # written by Claude scanner

# ── Sector map for portfolio diversification ─────────────────────
SECTOR_MAP = {
    # Technology
    "AAPL":"tech","MSFT":"tech","GOOGL":"tech","GOOG":"tech","META":"tech",
    "NVDA":"tech","AMD":"tech","INTC":"tech","ARM":"tech","AVGO":"tech",
    "ORCL":"tech","CRM":"tech","ADBE":"tech","PLTR":"tech","SMCI":"tech",
    "MU":"tech","QCOM":"tech","TXN":"tech","NOW":"tech",
    # Consumer Discretionary
    "AMZN":"consumer","TSLA":"consumer","HD":"consumer","NKE":"consumer",
    "MCD":"consumer","SBUX":"consumer","LOW":"consumer","TGT":"consumer",
    # Communication / Media
    "NFLX":"comm","DIS":"comm","T":"comm","VZ":"comm","CMCSA":"comm",
    # Financial
    "JPM":"financial","BAC":"financial","WFC":"financial","GS":"financial",
    "MS":"financial","C":"financial","V":"financial","MA":"financial",
    # Crypto / Fintech
    "COIN":"crypto","MSTR":"crypto","SQ":"crypto","PYPL":"crypto",
    # Energy
    "XOM":"energy","CVX":"energy","COP":"energy","OXY":"energy",
    # Healthcare
    "JNJ":"health","PFE":"health","UNH":"health","LLY":"health","ABBV":"health",
    # Industrial / Transport
    "UBER":"industrial","LYFT":"industrial","BA":"industrial","CAT":"industrial",
    # ETFs (skip for sector cap)
    "SPY":"etf","QQQ":"etf","IWM":"etf","DIA":"etf","VIXY":"etf",
}

def get_sector(sym):
    return SECTOR_MAP.get(sym.upper(), "other")

ET = pytz.timezone("America/New_York")

# ── Load credentials from environment variables ──────────────────
API_KEY    = os.environ.get("ALPACA_API_KEY", "")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")
DATA_URL   = os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets/v2")

# Load config for risk params and telegram settings
with open(CONFIG) as f:
    cfg = json.load(f)

HEADERS  = {"APCA-API-KEY-ID": API_KEY,
            "APCA-API-SECRET-KEY": SECRET_KEY}
RISK     = cfg.get("risk", {})

# ── Strategy defaults ─────────────────────────────────────────
DEFAULTS = {
    "ema_fast": 9, "ema_slow": 21, "rsi_period": 14,
    # Wider RSI band — don't miss stocks at 45 or 73
    "rsi_buy_min": 45, "rsi_buy_max": 75,
    "atr_period": 14, "atr_multiplier": 1.5,
    # Relative volume is a score bonus, not a hard filter
    "rel_vol_min": 1.2,
    "stop_loss_pct": 1.5, "take_profit_pct": 3.0,
    "trail_pct": 1.0,
    "bar_timeframe": "5Min", "max_positions": 3,
    "budget_per_trade": 333,
    "trade_start_hour": 9,  "trade_start_min": 45,  # enter from 9:45 ET
    "trade_end_hour": 15,   "trade_end_min": 20,     # last entry 15:20 ET
    "close_hour": 15,       "close_min": 45,
    # Soft filters — score bonuses, not hard gates
    "vwap_required": False, "orb_required": False,
    "multi_tf_required": False,
    "min_confidence": 45,   # was 60 — lower bar so bot can actually trade
    "daily_loss_limit_pct": 3.0,
    "vix_pause_threshold": 28, "vix_stop_threshold": 35,
    "spy_filter": True,
    # ── Portfolio risk + advanced exits ───────────────────────
    "sector_cap": True,           # max 1 position per sector
    "partial_tp_pct": 2.0,        # sell 50% at +2%
    "partial_tp_frac": 0.5,       # fraction to sell on partial
    # ── New safety controls ───────────────────────────────────
    "max_drawdown_pct": 8.0,           # pause entries if 7d drawdown exceeds
    "consecutive_loss_pause": 3,       # # losses to trigger cooldown
    "consecutive_loss_cooldown_min": 30,
    "risk_per_trade_pct": 1.0,         # ATR-scaled sizing target
    "max_spread_pct": 0.5,             # reject illiquid quotes (relaxed to 0.5%)
}

def params():
    p = DEFAULTS.copy()
    p.update(RISK)
    if os.path.exists(STRAT_FILE):
        with open(STRAT_FILE) as f:
            p.update(json.load(f))
    return p

# ── State (shared with dashboard) ────────────────────────────
_state = {
    "started": None, "regime": "unknown",
    "daily_pnl": 0.0, "daily_trades": 0,
    "trading_paused": False, "pause_reason": "",
    "positions": [], "watchlist": [],
    "last_scan": None, "vix": None,
    "equity": 0.0, "buying_power": 0.0,
    "log": [],
}

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(_state, f, indent=2, default=str)
    except Exception as e:
        print(f"  [WARN] save_state failed: {e}")

def log_event(msg):
    ts = now_et().strftime("%H:%M:%S")
    print(f"  [{ts}] {msg}")
    _state["log"] = ([{"t": ts, "m": msg}] + _state["log"])[:100]

# ── Alpaca helpers (with retry, error logging, fill verification) ──
def api(method, path, retries=2, **kw):
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.request(method, f"{BASE_URL}{path}",
                                 headers=HEADERS, timeout=10, **kw)
            data = r.json() if r.content else {}
            # Surface Alpaca error responses (422, 403, 500…)
            if r.status_code >= 400:
                msg = data.get("message") if isinstance(data, dict) else str(data)
                log_event(f"ALPACA ERR {method} {path} → {r.status_code}: {msg}")
                if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(1.5 * (attempt + 1)); continue
                return {"_error": True, "status_code": r.status_code, "message": msg}
            return data
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    log_event(f"ALPACA NETWORK FAIL {method} {path}: {last_err}")
    return {"_error": True, "message": str(last_err)}

def data_get(path, params=None):
    try:
        r = requests.get(f"{DATA_URL}{path}", headers=HEADERS,
                         params=params or {}, timeout=10)
        return r.json() if r.content else {}
    except Exception:
        return {}

def get_account():       return api("GET", "/account")
def get_positions():
    p = api("GET", "/positions")
    return p if isinstance(p, list) else []

def place_order(sym, qty, side, limit_price=None,
                bracket_stop=None, bracket_tp=None):
    """Place order. Uses LIMIT (not market) for slippage protection.
    If bracket_stop AND bracket_tp are provided, submits a BRACKET order:
    SL+TP are attached atomically — if the bot crashes after the buy fills,
    Alpaca still enforces the exits."""
    body = {
        "symbol": sym, "qty": str(qty), "side": side,
        "type": "limit" if limit_price else "market",
        "time_in_force": "day",
        "extended_hours": False,
    }
    if limit_price:
        body["limit_price"] = f"{round(limit_price, 2):.2f}"
    if bracket_stop and bracket_tp and side == "buy":
        body["order_class"] = "bracket"
        body["take_profit"] = {"limit_price": f"{round(bracket_tp, 2):.2f}"}
        body["stop_loss"]   = {"stop_price":  f"{round(bracket_stop, 2):.2f}"}
    return api("POST", "/orders", json=body)

def get_quote(sym):
    """Latest NBBO quote — used for spread check."""
    d = data_get(f"/stocks/{sym}/quotes/latest")
    if isinstance(d, dict):
        q = d.get("quote", {})
        return q.get("bp"), q.get("ap")  # bid, ask
    return None, None

def spread_ok(sym, max_pct=0.3):
    """Reject illiquid stocks with wide spreads (default >0.3% of mid)."""
    bid, ask = get_quote(sym)
    if not bid or not ask or bid <= 0 or ask <= 0:
        return True  # quote unavailable, don't block
    mid = (bid + ask) / 2
    spread_pct = (ask - bid) / mid * 100
    if spread_pct > max_pct:
        log_event(f"{sym} spread {spread_pct:.2f}% > {max_pct}% — skip")
        return False
    return True

def get_clock():
    """Alpaca /clock — knows market open/close including holidays."""
    return api("GET", "/clock")

def get_order(order_id):
    return api("GET", f"/orders/{order_id}")

def wait_for_fill(order_id, timeout=10, requested_qty=None, min_fill_pct=0.5):
    """Poll order until filled or timed out.
    Accepts partial fills ≥ min_fill_pct of requested_qty (default 50%)."""
    end = time.time() + timeout
    while time.time() < end:
        o = get_order(order_id)
        if not isinstance(o, dict) or o.get("_error"):
            return 0, None
        status = o.get("status")
        if status == "filled":
            return int(float(o.get("filled_qty", 0))), float(o.get("filled_avg_price") or 0)
        if status in ("canceled", "rejected", "expired"):
            log_event(f"Order {order_id[:8]} {status}: {o.get('reject_reason') or ''}")
            # Salvage partial fill if any
            partial = int(float(o.get("filled_qty", 0)))
            avg     = float(o.get("filled_avg_price") or 0)
            if partial > 0 and avg > 0:
                return partial, avg
            return 0, None
        time.sleep(0.5)
    # Timeout: cancel and check if any partial fill happened
    api("DELETE", f"/orders/{order_id}")
    o = get_order(order_id) or {}
    partial = int(float(o.get("filled_qty", 0)))
    avg     = float(o.get("filled_avg_price") or 0)
    if requested_qty and partial >= max(1, int(requested_qty * min_fill_pct)):
        log_event(f"Order {order_id[:8]} partial fill {partial}/{requested_qty} accepted")
        return partial, avg
    if partial > 0:
        log_event(f"Order {order_id[:8]} partial fill {partial} below threshold — closing")
    log_event(f"Order {order_id[:8]} timeout, cancelled")
    return 0, None

def close_position(sym): return api("DELETE", f"/positions/{sym}")
def close_all():
    # Robust EOD flatten:
    # 1) cancel all working orders (bracket children block liquidation)
    # 2) DELETE /positions returns 207 multi-status — inspect body for per-symbol errors
    # 3) verify positions actually went to zero; per-symbol retry if not
    cr = api("DELETE", "/orders")
    if isinstance(cr, dict) and cr.get("_error"):
        log_event(f"close_all: cancel-orders failed: {cr.get('message')}")
    elif isinstance(cr, list):
        rejected = [x for x in cr if isinstance(x, dict) and x.get('status', 200) >= 400]
        if rejected:
            log_event(f"close_all: {len(rejected)}/{len(cr)} order cancels rejected")
    time.sleep(2)
    res = api("DELETE", "/positions")
    # 207 returns a list of {symbol, status, body}; inspect for per-symbol failures
    if isinstance(res, list):
        ok = sum(1 for x in res if isinstance(x, dict) and x.get('status', 0) < 400)
        bad = [x for x in res if isinstance(x, dict) and x.get('status', 0) >= 400]
        log_event(f"close_all: DELETE /positions ok={ok} failed={len(bad)} (total {len(res)})")
        for b in bad:
            log_event(f"  close_all FAIL {b.get('symbol','?')}: {b.get('body',{}).get('message','?')}")
    elif isinstance(res, dict) and res.get("_error"):
        log_event(f"close_all: DELETE /positions failed: {res.get('message')}")
    # Verify + per-symbol retry up to 3 passes
    for attempt in range(3):
        time.sleep(2)
        remaining = get_positions()
        if not remaining:
            log_event(f"close_all: verified flat after pass {attempt}")
            return res
        log_event(f"close_all: pass {attempt} — {len(remaining)} still open: " +
                  ", ".join("{}({})".format(p['symbol'], p['qty']) for p in remaining))
        for p in remaining:
            r2 = close_position(p['symbol'])
            if isinstance(r2, dict) and r2.get("_error"):
                log_event(f"  per-symbol close fail {p['symbol']}: {r2.get('message')}")
    rem = get_positions()
    if rem:
        log_event(f"close_all: GAVE UP — still holding {[p['symbol'] for p in rem]}")
    return res

def get_bars(sym, tf="5Min", limit=80):
    d = data_get(f"/stocks/{sym}/bars", {"timeframe": tf, "limit": limit, "feed": "iex"})
    return (d.get("bars") or []) if isinstance(d, dict) else []

def get_latest_price(sym):
    d = data_get(f"/stocks/{sym}/trades/latest")
    return d.get("trade", {}).get("p") if isinstance(d, dict) else None

# ── Trade log ─────────────────────────────────────────────────
def load_log():
    if not os.path.exists(LOG_FILE): return []
    with open(LOG_FILE) as f:
        try: return json.load(f)
        except: return []

def append_log(entry):
    log = load_log()
    log.append(entry)
    with open(LOG_FILE, "w") as f:
        json.dump(log[-5000:], f, indent=2)

def load_picks():
    if not os.path.exists(PICKS_FILE): return []
    with open(PICKS_FILE) as f:
        try:
            data = json.load(f)
            today = now_et().strftime("%Y-%m-%d")
            return [p for p in data if p.get("date") == today]
        except: return []

# ── Indicators ────────────────────────────────────────────────
def ema_series(vals, n):
    if len(vals) < n: return [None]*len(vals)
    k = 2/(n+1); out = [None]*(n-1)
    s = sum(vals[:n])/n; out.append(s)
    for v in vals[n:]: s = v*k + s*(1-k); out.append(s)
    return out

def ema_val(vals, n):
    s = ema_series(vals, n)
    return next((x for x in reversed(s) if x is not None), None)

def rsi_val(closes, n=14):
    if len(closes) < n+1: return 50
    gains  = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
    ag = sum(gains[-n:])/n; al = sum(losses[-n:])/n
    return 100 if al == 0 else 100-100/(1+ag/al)

def atr_val(bars, n=14):
    if len(bars) < n+1: return None
    trs = [max(bars[i]["h"]-bars[i]["l"],
               abs(bars[i]["h"]-bars[i-1]["c"]),
               abs(bars[i]["l"]-bars[i-1]["c"]))
           for i in range(1, len(bars))]
    return sum(trs[-n:])/n

def vwap_val(bars):
    num = den = 0
    for b in bars:
        tp = (b["h"]+b["l"]+b["c"])/3
        num += tp*b["v"]; den += b["v"]
    return num/den if den else None

def rel_vol(bars, lb=20):
    if len(bars) < 5: return 1.0
    avg = sum(b["v"] for b in bars[-lb-1:-1]) / min(lb, len(bars)-1)
    return bars[-1]["v"] / avg if avg else 1.0

def orb_levels(bars_1min):
    orb = [b for b in bars_1min if "09:3" in b["t"] or "09:4" in b["t"]][:15]
    if not orb: return None, None
    return max(b["h"] for b in orb), min(b["l"] for b in orb)

def macd(closes):
    """Returns (macd_line, signal_line, histogram) for latest bar"""
    if len(closes) < 35: return None, None, None
    e12 = ema_series(closes, 12)
    e26 = ema_series(closes, 26)
    macd_line = [
        (a-b) if (a is not None and b is not None) else None
        for a, b in zip(e12, e26)
    ]
    valid = [x for x in macd_line if x is not None]
    if len(valid) < 9: return None, None, None
    signal = ema_series(valid, 9)
    ml = valid[-1]; sl = signal[-1]
    if sl is None: return None, None, None
    return ml, sl, ml - sl

def bollinger(closes, n=20, k=2):
    """Returns (upper, mid, lower) for latest bar"""
    if len(closes) < n: return None, None, None
    window = closes[-n:]
    mid  = sum(window)/n
    std  = (sum((x-mid)**2 for x in window)/n)**0.5
    return mid + k*std, mid, mid - k*std

def ema_cross(closes, fast, slow):
    ef = ema_series(closes, fast); es = ema_series(closes, slow)
    if any(x is None for x in [ef[-1],es[-1],ef[-2],es[-2]]): return "hold"
    if ef[-2] <= es[-2] and ef[-1] > es[-1]: return "buy"
    if ef[-2] >= es[-2] and ef[-1] < es[-1]: return "sell"
    return "hold"

def multi_tf(sym, p):
    sigs = []
    for tf, lim in [("1Min",40),("5Min",60),("15Min",30)]:
        bars = get_bars(sym, tf, lim)
        if len(bars) < p["ema_slow"]+2: return "hold"
        closes = [b["c"] for b in bars]
        sig = ema_cross(closes, p["ema_fast"], p["ema_slow"])
        rsi = rsi_val(closes)
        sigs.append("buy" if sig=="buy" and p["rsi_buy_min"]<=rsi<=p["rsi_buy_max"]
                    else "sell" if sig=="sell" else "hold")
    return "buy" if sigs.count("buy")>=2 else "sell" if sigs.count("sell")>=2 else "hold"

# ── Market Filters ────────────────────────────────────────────
def get_vix():
    """Fetch real ^VIX from Yahoo Finance public quote endpoint.
    Falls back to VIXY ETF proxy if Yahoo fails."""
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX",
            params={"interval": "5m", "range": "1d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=5,
        )
        d = r.json()
        meta = d.get("chart", {}).get("result", [{}])[0].get("meta", {})
        v = meta.get("regularMarketPrice")
        if v: return float(v)
    except Exception as e:
        log_event(f"VIX Yahoo fetch failed: {e}")
    # Fallback to VIXY ETF proxy
    try:
        bars = get_bars("VIXY", "5Min", 5)
        if bars: return bars[-1]["c"]
    except Exception as e:
        log_event(f"VIX VIXY fallback failed: {e}")
    return None

# ── PDT (Pattern Day Trader) protection ─────────────────────────
def count_day_trades_5d():
    """A day-trade is buy+sell of the same symbol same day. Count over last 5 trading days.
    Returns the count from local trade_log.json."""
    log = load_log()
    from collections import defaultdict
    by_day_sym = defaultdict(lambda: {"buys": 0, "sells": 0})
    for t in log:
        d = str(t.get("time", ""))[:10]
        if not d: continue
        sym = t.get("sym", "")
        if t.get("action") == "buy":  by_day_sym[(d, sym)]["buys"]  += 1
        if t.get("action") == "sell": by_day_sym[(d, sym)]["sells"] += 1

    # Count day-trades in last 5 weekdays
    from datetime import timedelta
    today = now_et().date()
    cutoff = today - timedelta(days=10)  # generous window for weekends
    count = 0
    for (d, sym), c in by_day_sym.items():
        try:
            dt = datetime.strptime(d, "%Y-%m-%d").date()
        except Exception:
            continue
        if dt >= cutoff and c["buys"] >= 1 and c["sells"] >= 1:
            count += 1
    return count

def pdt_blocks_new_trade(equity):
    """If account < $25K and 4+ day-trades in 5d, NO new round-trips allowed."""
    if equity >= 25000:
        return False, ""
    cnt = count_day_trades_5d()
    if cnt >= 3:  # 3 already done = next would be the 4th = PDT lock
        return True, f"PDT guard: {cnt} day-trades in 5d, account ${equity:.0f} < $25K"
    return False, ""

# ── Drawdown stop & consecutive-loss circuit breaker ────────────
EQUITY_HIST_F = os.path.join(BASE_DIR, "equity_history.json")

def load_equity_history():
    if not os.path.exists(EQUITY_HIST_F): return []
    try:
        with open(EQUITY_HIST_F) as f: return json.load(f)
    except Exception: return []

def append_equity_snapshot(equity):
    """Append current equity once per day (overwrites today's entry)."""
    h = load_equity_history()
    today = now_et().strftime("%Y-%m-%d")
    h = [e for e in h if e.get("date") != today]
    h.append({"date": today, "equity": round(equity, 2),
              "ts": now_et().isoformat()})
    h = h[-180:]  # keep ~6 months
    try:
        with open(EQUITY_HIST_F, "w") as f:
            json.dump(h, f, indent=2)
    except Exception as e:
        print(f"  [WARN] append_equity_snapshot failed: {e}")

def rolling_drawdown_pct(equity, lookback_days=7):
    """Returns drawdown % from peak equity over last N days."""
    h = load_equity_history()
    if not h: return 0.0
    recent = h[-lookback_days:]
    peak = max((e["equity"] for e in recent), default=equity)
    peak = max(peak, equity)
    if peak <= 0: return 0.0
    return (equity - peak) / peak * 100   # negative when in drawdown

def consecutive_losses_today():
    """Count losing sells since last winning sell (today only)."""
    log = load_log()
    today = now_et().strftime("%Y-%m-%d")
    sells_today = [t for t in log
                   if str(t.get("time", "")).startswith(today)
                   and t.get("action") == "sell"]
    if not sells_today: return 0
    n = 0
    for t in reversed(sells_today):
        if float(t.get("pct", 0)) <= 0: n += 1
        else: break
    return n

# ── ATR-scaled position sizing ──────────────────────────────────
def atr_scaled_qty(price, atr, budget, target_risk_pct=1.0):
    """Size position so each trade risks ~target_risk_pct of budget given ATR-based stop.
    Smaller qty for high-ATR (volatile) names, larger qty for low-ATR (stable) names."""
    if not atr or atr <= 0:
        return int(budget // price)
    risk_per_share = atr * 1.5  # matches atr_multiplier
    if risk_per_share <= 0:
        return int(budget // price)
    risk_dollars = budget * (target_risk_pct / 100.0) * 10  # 10x leverage on the risk slice
    qty_by_risk  = int(risk_dollars // risk_per_share)
    qty_by_cash  = int(budget // price)
    return max(1, min(qty_by_risk, qty_by_cash))

# ── Persisted positions (for stop, trail_hi, partial_taken) ─────
POSITIONS_F = os.path.join(BASE_DIR, "positions_state.json")

def save_positions():
    try:
        with open(POSITIONS_F, "w") as f:
            json.dump(open_positions, f, indent=2, default=str)
    except Exception as e:
        print(f"  [WARN] save_positions failed: {e}")

def load_positions_from_disk():
    if not os.path.exists(POSITIONS_F): return {}
    try:
        with open(POSITIONS_F) as f: return json.load(f)
    except Exception: return {}

# ── Earnings filter (best-effort; gracefully skips if not available) ──
_earnings_cache = {"date": None, "syms": set()}
def has_earnings_today(sym):
    """Avoid trading into earnings — outsized gap risk.
    Uses Alpaca's corporate-actions/announcements; fails open if endpoint denies."""
    today = now_et().strftime("%Y-%m-%d")
    if _earnings_cache["date"] != today:
        _earnings_cache["date"] = today
        _earnings_cache["syms"] = set()
        try:
            r = requests.get(
                f"{BASE_URL}/corporate_actions/announcements",
                headers=HEADERS,
                params={"ca_types": "earnings", "since": today, "until": today},
                timeout=8,
            )
            if r.status_code == 200:
                for a in (r.json() or []):
                    s = a.get("target_symbol") or a.get("symbol")
                    if s: _earnings_cache["syms"].add(s.upper())
        except Exception as e:
            log_event(f"Earnings fetch failed: {e}")
    return sym.upper() in _earnings_cache["syms"]

def spy_trend(p):
    """Smart SPY filter — bull unless SPY is clearly weak.
    Uses a 21-EMA on 5min bars (≈ 105 min lookback) with a 0.2% tolerance
    band so minor intraday dips don't whipsaw the bot out of every entry.
    Old version used a 9-EMA which was too noisy and blocked trades on
    routine pullbacks. Returns True (bull) if data missing — fail open."""
    if not p.get("spy_filter", True): return True
    bars = get_bars("SPY", "5Min", 60)
    if len(bars) < 25: return True
    closes = [b["c"] for b in bars]
    ef = ema_val(closes, 21)
    if not ef: return True
    # Tolerance: SPY can be up to 0.2% below EMA-21 and still count as bull.
    # Only blocks when the market is meaningfully weak (≥ 0.2% below trend).
    return closes[-1] >= ef * 0.998

def market_regime(bars_spy):
    """
    trending  — strong directional move (momentum strategy)
    choppy    — sideways noise (mean-reversion)
    bearish   — broad selloff (reduce/pause)
    """
    if len(bars_spy) < 20: return "trending"
    closes = [b["c"] for b in bars_spy]
    # ADX proxy: compare recent range to avg range
    ranges  = [b["h"]-b["l"] for b in bars_spy]
    avg_rng = sum(ranges[-20:])/20
    day_rng = max(closes[-20:]) - min(closes[-20:])
    rsi     = rsi_val(closes)
    if rsi < 40:
        return "bearish"
    if day_rng > avg_rng * 1.5:
        return "trending"
    return "choppy"

# ── Scoring engine ─────────────────────────────────────────────
def score_stock(sym, p, regime):
    bars5  = get_bars(sym, "5Min", 80)
    bars1  = get_bars(sym, "1Min", 40)
    score  = 0; reasons = {}

    if len(bars5) < 25:
        return 0, {"error": "no data"}, [], {}

    closes  = [b["c"] for b in bars5]
    current = closes[-1]

    # ── Regime-aware: choppy day uses mean-reversion ──────────
    if regime == "choppy":
        bb_up, bb_mid, bb_lo = bollinger(closes)
        if bb_lo and current <= bb_lo * 1.002:
            score += 40; reasons["bb_bounce"] = f"Price near lower BB ${bb_lo:.2f}"
        # No hard rejection in choppy — other signals can still earn score
    else:
        # 1. EMA signal on primary 5-min timeframe (never a hard gate)
        ema5_sig = ema_cross(closes, p["ema_fast"], p["ema_slow"])
        if ema5_sig == "sell":
            # Active bearish crossover — skip this candidate entirely
            return 0, {"ema": "5min bearish"}, bars5, {}

        # 2. Multi-TF confluence BONUS — soft, not required
        tf_sig = multi_tf(sym, p)
        if tf_sig == "buy":
            score += 30; reasons["multi_tf"] = "multi-TF bullish"
        elif ema5_sig == "buy":
            score += 18; reasons["ema"] = "5min bullish cross"
        else:
            score += 5; reasons["ema"] = "5min holding"

    # 3. RSI — only reject extremes; wide-band bonus
    rsi = rsi_val(closes, p["rsi_period"])
    if rsi > 82 or rsi < 30:
        return 0, {"skip": f"RSI {rsi:.0f} extreme"}, bars5, {}
    if p["rsi_buy_min"] <= rsi <= p["rsi_buy_max"]:
        score += 15; reasons["rsi"] = f"RSI {rsi:.0f}"
    elif 40 <= rsi < p["rsi_buy_min"]:
        score += 5; reasons["rsi"] = f"RSI {rsi:.0f} acceptable"

    # 4. VWAP — soft bonus/penalty, never a hard gate
    vw = vwap_val(bars5)
    if vw and current > vw:
        score += 15; reasons["vwap"] = f"above VWAP"
    elif vw and current <= vw:
        score -= 8; reasons["vwap"] = "below VWAP"

    # 5. ORB — bonus only
    orb_hi, orb_lo = orb_levels(bars1)
    if orb_hi and current > orb_hi:
        score += 15; reasons["orb"] = f"ORB breakout > ${orb_hi:.2f}"

    # 6. Relative Volume
    rv = rel_vol(bars5)
    if rv >= p["rel_vol_min"]:
        score += 10; reasons["rvol"] = f"{rv:.1f}x"
    elif rv >= 1.0:
        score += 3   # at least average volume

    # 7. MACD
    ml, sl, hist = macd(closes)
    if ml is not None:
        if ml > sl and hist > 0:
            score += 10; reasons["macd"] = f"MACD bullish"
        elif ml < sl:
            score -= 5

    # 8. Bollinger position
    bb_up, bb_mid, bb_lo = bollinger(closes)
    if bb_up and bb_lo:
        bb_pct = (current - bb_lo) / (bb_up - bb_lo) if bb_up != bb_lo else 0.5
        if bb_pct < 0.4:
            score += 10; reasons["bb"] = f"BB lower-third"
        elif bb_pct > 0.88:
            score -= 10; reasons["bb"] = "BB upper-band"

    # 9. AI scanner boost (scanner picks add confidence directly)
    for pk in load_picks():
        if pk.get("symbol") == sym:
            boost = int(pk.get("confidence", 0) * 0.15)  # up to +15 from AI
            score += boost
            reasons["ai"] = f"scanner={pk.get('confidence')}% (+{boost})"
            break

    return min(score, 100), reasons, bars5, {}

# ── News kill-switch ──────────────────────────────────────────
def load_negative_news():
    """Returns set of tickers with breaking negative news today.
    File format: {"date": "YYYY-MM-DD", "tickers": ["XYZ", ...]}
    Populated by Claude scanner skill."""
    if not os.path.exists(NEG_NEWS_F): return set()
    try:
        with open(NEG_NEWS_F) as f:
            d = json.load(f)
        today = now_et().strftime("%Y-%m-%d")
        if d.get("date") == today:
            return set(t.upper() for t in d.get("tickers", []))
    except Exception as e:
        log_event(f"negative_news load failed: {e}")
    return set()

# ── Trailing stop ─────────────────────────────────────────────
open_positions = {}  # sym → {qty, entry, stop, trail_hi, tp, partial_taken, original_qty, sector}

def update_trailing(sym, current_price, p):
    """Raise stop as price increases"""
    pos = open_positions.get(sym)
    if not pos: return
    trail_stop = current_price * (1 - p["trail_pct"]/100)
    if trail_stop > pos["stop"]:
        open_positions[sym]["stop"] = round(trail_stop, 2)
        open_positions[sym]["trail_hi"] = current_price

def atr_stop(bars, entry, mult):
    a = atr_val(bars, 14)
    return round(entry - a*mult, 2) if a else round(entry*0.985, 2)

# ── Daily P&L tracking ─────────────────────────────────────────
def calc_daily_pnl(start_equity):
    acc = get_account()
    current = float(acc.get("equity", start_equity))
    return (current - start_equity) / start_equity * 100

# ── Time helpers ──────────────────────────────────────────────
def now_et(): return datetime.now(ET)

def market_open():
    n = now_et()
    return dtime(9,30) <= n.time() <= dtime(16,0) and n.weekday() < 5

def in_trade_window(p):
    n = now_et().time()
    return (dtime(p["trade_start_hour"], p["trade_start_min"]) <= n <=
            dtime(p["trade_end_hour"], p["trade_end_min"]))

def eod_time(p):
    return now_et().time() >= dtime(p["close_hour"], p["close_min"])

def get_watchlist():
    picks = load_picks()
    ai = [pk["symbol"] for pk in picks if pk.get("confidence",0) >= 60]
    base = ["AAPL","TSLA","NVDA","MSFT","AMZN","META","GOOGL","AMD",
            "SPY","QQQ","NFLX","UBER","COIN","ARM","PLTR"]
    return list(dict.fromkeys(ai + base))[:20]

# ── Exit check ─────────────────────────────────────────────────
def check_exits(alpaca_pos, p):
    bad_news = load_negative_news()
    partial_tp_pct = float(p.get("partial_tp_pct", 2.0))
    partial_frac   = float(p.get("partial_tp_frac", 0.5))

    for sym, pos in list(alpaca_pos.items()):
        entry  = float(pos.get("avg_entry_price", 0))
        curr   = float(pos.get("current_price", entry))
        qty    = int(float(pos.get("qty", 1)))
        pct    = (curr-entry)/entry*100 if entry else 0

        # Rebuild tracking entry after a bot restart (open_positions is in-memory)
        if sym not in open_positions:
            open_positions[sym] = {
                "qty": qty, "entry": entry,
                "stop": round(entry * (1 - p["stop_loss_pct"] / 100), 2),
                "trail_hi": curr,
                "tp":   round(entry * (1 + p["take_profit_pct"] / 100), 2),
                "partial_taken": False, "original_qty": qty,
                "sector": get_sector(sym),
            }

        local  = open_positions[sym]

        # ── News kill-switch (highest priority) ───────────────
        if sym.upper() in bad_news:
            log_event(f"NEWS KILL {sym} | negative headline detected")
            cd = local.get("close_attempt_cooldown_until", 0)
            if time.time() < cd:
                continue
            res = close_position(sym)
            if isinstance(res, dict) and res.get("_error"):
                log_event(f"  NEWS KILL {sym} REJECTED: {res.get('message')} — cooling down 60s")
                open_positions[sym]["close_attempt_cooldown_until"] = time.time() + 60
                continue
            open_positions.pop(sym, None); save_positions()
            alert_sell(sym, qty, curr, pct, "news_kill_switch")
            # Realized P&L in $: (exit-entry)*qty. daily_pnl is dollar-summed and recomputed
            # from the live equity each cycle by calc_daily_pnl(), so this is just bookkeeping.
            realized_usd = (curr - entry) * qty
            append_log({"time": now_et().isoformat(), "sym": sym,
                        "action": "sell", "qty": qty, "price": curr,
                        "entry_price": entry, "pct": round(pct,2),
                        "pnl_abs": round((curr-entry)*qty, 2),
                        "reason": "news_kill_switch"})
            continue

        # Update trailing stop
        update_trailing(sym, curr, p)
        stop = open_positions.get(sym, {}).get("stop", entry*(1-p["stop_loss_pct"]/100))
        tp   = local.get("tp", entry*(1+p["take_profit_pct"]/100))

        # ── Partial profit-taking (sell half at +partial_tp_pct) ──
        if (not local.get("partial_taken")
            and pct >= partial_tp_pct
            and qty >= 2):
            sell_qty = max(1, int(qty * partial_frac))
            log_event(f"PARTIAL {sym} | sell {sell_qty}/{qty} @ {pct:+.2f}%")
            res = api("POST", "/orders", json={
                "symbol": sym, "qty": str(sell_qty), "side": "sell",
                "type": "market", "time_in_force": "day"})
            if res.get("status") in ("accepted","pending_new","new"):
                # Move stop to breakeven on remaining shares (locks in profit)
                open_positions[sym]["partial_taken"] = True
                open_positions[sym]["stop"] = max(stop, entry * 1.001)
                alert_sell(sym, sell_qty, curr, pct, "partial_take")
                append_log({"time": now_et().isoformat(), "sym": sym,
                            "action": "sell", "qty": sell_qty, "price": curr,
                            "entry_price": entry, "pct": round(pct,2),
                            "pnl_abs": round((curr-entry)*sell_qty, 2),
                            "reason": "partial_take"})
                continue  # Don't also full-exit this cycle

        reason = None
        if curr <= stop:    reason = f"stop_loss ({pct:.2f}%)"
        elif curr >= tp:    reason = f"take_profit ({pct:.2f}%)"
        else:
            bars5 = get_bars(sym, "5Min", 30)
            if len(bars5) > p["ema_slow"]+2:
                closes = [b["c"] for b in bars5]
                if ema_cross(closes, p["ema_fast"], p["ema_slow"]) == "sell":
                    reason = f"signal_exit ({pct:.2f}%)"

        if reason:
            cd = local.get("close_attempt_cooldown_until", 0)
            if time.time() < cd:
                continue
            log_event(f"EXIT {sym} | {reason}")
            res = close_position(sym)
            if isinstance(res, dict) and res.get("_error"):
                log_event(f"  EXIT {sym} REJECTED: {res.get('message')} — cooling down 60s")
                open_positions[sym]["close_attempt_cooldown_until"] = time.time() + 60
                continue
            open_positions.pop(sym, None); save_positions()
            alert_sell(sym, qty, curr, pct, reason)
            # Realized P&L in $: (exit-entry)*qty. daily_pnl is dollar-summed and recomputed
            # from the live equity each cycle by calc_daily_pnl(), so this is just bookkeeping.
            realized_usd = (curr - entry) * qty
            append_log({"time": now_et().isoformat(), "sym": sym,
                        "action": "sell", "qty": qty, "price": curr,
                        "entry_price": entry, "pct": round(pct,2),
                        "pnl_abs": round((curr-entry)*qty, 2),
                        "reason": reason})

# ── Main loop ─────────────────────────────────────────────────
def run():
    p           = params()
    acc         = get_account()
    start_eq    = float(acc.get("equity", 100000))
    bp          = float(acc.get("buying_power", 0))

    _state["started"] = now_et().isoformat()
    _state["equity"]  = start_eq

    # Restore in-memory tracking (trail_hi, partial_taken, etc.) from disk
    persisted = load_positions_from_disk()
    if persisted:
        open_positions.update(persisted)
        log_event(f"Restored {len(persisted)} tracked positions from disk")

    # Snapshot equity once per day for drawdown calc
    append_equity_snapshot(start_eq)

    print("=" * 62)
    print(f"  ADVANCED TRADING BOT v2  |  {now_et().strftime('%Y-%m-%d')}")
    print(f"  Equity: ${start_eq:,.2f}  |  Buying Power: ${bp:,.2f}")
    print("=" * 62)

    wl = get_watchlist()
    alert_startup(start_eq, bp, len(wl))

    # Track previous regime so we only alert on change
    last_dd_alert = 0
    cooldown_until = 0  # UNIX ts — set by consecutive-loss breaker

    while True:
        p   = params()
        now = now_et()

        if not market_open():
            log_event("Market closed — sleeping 60s")
            time.sleep(60); continue

        # ── EOD close ──────────────────────────────────────
        if eod_time(p):
            log_event("EOD — closing all positions")
            _eod_res = close_all()
            if isinstance(_eod_res, dict) and _eod_res.get("_error"):
                log_event(f"EOD close FAILED: {_eod_res.get('message')}")
            else:
                log_event("EOD close: liquidation submitted OK")
            daily_pnl = calc_daily_pnl(start_eq)
            trades    = _state["daily_trades"]
            alert_eod(float(get_account().get("equity", start_eq)),
                      daily_pnl, trades,
                      os.path.join(BASE_DIR, "reports", f"trading_report_{now.strftime('%Y-%m-%d')}.html"))
            import subprocess, sys
            subprocess.Popen(
                [sys.executable, os.path.join(BASE_DIR, "eod_report.py")],
                cwd=BASE_DIR
            )
            log_event("EOD report launched. Bot finished.")
            save_state(); break

        # ── VIX filter ─────────────────────────────────────
        vix = get_vix()
        _state["vix"] = vix
        vix_stop  = float(p.get("vix_stop_threshold", 35))
        vix_pause = float(p.get("vix_pause_threshold", 28))

        if vix and vix >= vix_stop:
            if not _state["trading_paused"]:
                log_event(f"VIX {vix:.1f} >= {vix_stop} — trading STOPPED")
                alert_vix(vix, "STOP")
                _state["trading_paused"] = True
                _state["pause_reason"]   = f"VIX {vix:.1f}"
            time.sleep(60); save_state(); continue

        vix_size_mult = 0.5 if (vix and vix >= vix_pause) else 1.0
        if vix and vix >= vix_pause and not _state["trading_paused"]:
            log_event(f"VIX {vix:.1f} elevated — reducing position size 50%")
            alert_vix(vix, "REDUCE")

        if _state["trading_paused"] and (not vix or vix < vix_stop):
            _state["trading_paused"] = False

        # ── Rolling 7-day drawdown stop ────────────────────
        cur_equity = float(get_account().get("equity", start_eq))
        dd_pct = rolling_drawdown_pct(cur_equity, lookback_days=7)
        max_dd = float(p.get("max_drawdown_pct", 8.0))  # default: pause at -8%
        if dd_pct <= -max_dd:
            if time.time() - last_dd_alert > 1800:  # alert at most every 30 min
                log_event(f"DRAWDOWN STOP: 7d drawdown {dd_pct:.2f}% <= -{max_dd}% — entries paused")
                last_dd_alert = time.time()
            _state["trading_paused"] = True
            _state["pause_reason"]   = f"7d drawdown {dd_pct:.2f}%"
            alpaca_pos = {p2["symbol"]: p2 for p2 in get_positions()}
            check_exits(alpaca_pos, p)
            save_state(); time.sleep(60); continue

        # ── Consecutive-loss circuit breaker ───────────────
        cl_threshold = int(p.get("consecutive_loss_pause", 3))
        cl_cooldown_min = int(p.get("consecutive_loss_cooldown_min", 30))
        if cooldown_until > time.time():
            mins_left = int((cooldown_until - time.time()) / 60)
            log_event(f"Cooldown active ({mins_left}m left) — entries paused")
            alpaca_pos = {p2["symbol"]: p2 for p2 in get_positions()}
            check_exits(alpaca_pos, p); save_state(); time.sleep(60); continue
        if consecutive_losses_today() >= cl_threshold:
            cooldown_until = time.time() + cl_cooldown_min * 60
            log_event(f"⛔ {cl_threshold} consecutive losses — pausing entries {cl_cooldown_min}m")

        # ── Daily loss limit ───────────────────────────────
        daily_pnl = calc_daily_pnl(start_eq)
        _state["daily_pnl"] = daily_pnl
        limit = float(p.get("daily_loss_limit_pct", 3.0))
        if daily_pnl <= -limit:
            if not _state["trading_paused"]:
                log_event(f"Daily loss limit hit ({daily_pnl:.2f}%) — no new trades")
                alert_daily_loss(daily_pnl)
                _state["trading_paused"] = True
                _state["pause_reason"]   = f"Daily loss {daily_pnl:.2f}%"
            # Still monitor exits
            alpaca_pos = {p2["symbol"]: p2 for p2 in get_positions()}
            check_exits(alpaca_pos, p)
            save_state(); time.sleep(60); continue

        print(f"\n[{now.strftime('%H:%M:%S')}] ── Cycle {'(PAUSED-entries)' if _state['trading_paused'] else ''}")

        # ── Market regime ──────────────────────────────────
        spy_bars = get_bars("SPY", "5Min", 30)
        regime   = market_regime(spy_bars)
        if regime != _state["regime"]:
            log_event(f"Regime change: {_state['regime']} → {regime}")
            alert_regime(regime)
            _state["regime"] = regime

        # ── SPY trend filter ───────────────────────────────
        spy_bull = spy_trend(p)
        if not spy_bull:
            log_event("SPY below EMA — market bearish, skipping new longs")

        # ── Check exits on all open positions ──────────────
        alpaca_pos = {pos["symbol"]: pos for pos in get_positions()}
        check_exits(alpaca_pos, p)

        # ── New entries ────────────────────────────────────
        if not in_trade_window(p):
            log_event(f"Outside window {p['trade_start_hour']:02d}:{p['trade_start_min']:02d}"
                      f"–{p['trade_end_hour']:02d}:{p['trade_end_min']:02d} ET")
            save_state(); time.sleep(60); continue

        acc       = get_account()
        bp        = float(acc.get("buying_power", 0))
        cur_count = len(get_positions())
        _state["equity"] = float(acc.get("equity", start_eq))
        _state["buying_power"] = bp

        # ── PDT guard (block entries if rule would lock account) ──
        pdt_blocked, pdt_reason = pdt_blocks_new_trade(cur_equity)
        if pdt_blocked:
            log_event(pdt_reason); save_state(); time.sleep(60); continue

        if cur_count < p["max_positions"] and bp > p["budget_per_trade"] and spy_bull:
            wl = get_watchlist()
            _state["watchlist"] = wl
            held_syms = {pos["symbol"] for pos in get_positions()}
            held_sectors = {get_sector(s) for s in held_syms} - {"etf", "other"}
            bad_news = load_negative_news()
            candidates = []

            for sym in wl:
                if sym in held_syms or sym == "SPY" or sym == "VIXY": continue
                if sym.upper() in bad_news:
                    log_event(f"{sym} skipped — negative news"); continue
                if has_earnings_today(sym):
                    log_event(f"{sym} skipped — earnings today"); continue
                sc, reasons, bars5, _ = score_stock(sym, p, regime)
                log_event(f"{sym:6s} score={sc:3d}  {list(reasons.keys())}")
                if sc >= p.get("min_confidence", 60):
                    candidates.append((sc, sym, reasons, bars5))

            candidates.sort(reverse=True)
            slots = p["max_positions"] - cur_count
            taken_sectors = set(held_sectors)  # track sectors we'll be in after buys
            sector_cap_enabled = p.get("sector_cap", True)

            buys_made = 0
            for sc, sym, reasons, bars5 in candidates:
                if buys_made >= slots: break
                sector = get_sector(sym)

                # ── Portfolio-level risk: sector cap (max 1 per sector) ──
                if sector_cap_enabled and sector not in ("etf", "other") \
                   and sector in taken_sectors:
                    log_event(f"{sym} skipped — sector '{sector}' already held")
                    continue

                acc2 = get_account()
                bp   = float(acc2.get("buying_power", 0))
                if bp < p["budget_per_trade"]: break

                price = bars5[-1]["c"] if bars5 else get_latest_price(sym)
                if not price: continue

                # Spread check — reject illiquid quotes
                if not spread_ok(sym, max_pct=float(p.get("max_spread_pct", 0.3))):
                    continue

                budget = p["budget_per_trade"] * vix_size_mult
                # Volatility-scaled qty: high-ATR names get fewer shares
                atr = atr_val(bars5, 14)
                qty = atr_scaled_qty(price, atr, budget,
                                     target_risk_pct=float(p.get("risk_per_trade_pct", 1.0)))
                if qty < 1: continue

                stop = atr_stop(bars5, price, p["atr_multiplier"])
                tp   = round(price * (1 + p["take_profit_pct"]/100), 2)

                # Slippage cap: 0.15% above current — fills aggressively, blocks runaway prints
                limit_px = round(price * 1.0015, 2)
                atr_str = f"{atr:.2f}" if atr else "0"
                log_event(f"BUY {qty}x {sym} @ ~${price:.2f} (limit ${limit_px}) | "
                          f"stop=${stop:.2f} tp=${tp:.2f} ATR={atr_str} "
                          f"score={sc} sector={sector}")
                # BRACKET ORDER: SL+TP attached atomically. Even if bot crashes after fill,
                # Alpaca enforces the exits — true crash-safe execution.
                res = place_order(sym, qty, "buy", limit_price=limit_px,
                                  bracket_stop=stop, bracket_tp=tp)

                if res.get("_error") or not res.get("id"):
                    log_event(f"Order REJECTED {sym}: {res.get('message','no id')}")
                    continue

                # Verify the fill (accept ≥50% partial fills)
                filled_qty, fill_px = wait_for_fill(res["id"], timeout=10,
                                                   requested_qty=qty, min_fill_pct=0.5)
                if filled_qty == 0 or not fill_px:
                    log_event(f"Order {sym} did not fill — skipping")
                    continue

                # Re-derive stops from ACTUAL fill price (not pre-fill estimate)
                actual_stop = atr_stop(bars5, fill_px, p["atr_multiplier"])
                actual_tp   = round(fill_px * (1 + p["take_profit_pct"]/100), 2)

                open_positions[sym] = {
                    "qty": filled_qty, "entry": fill_px,
                    "stop": actual_stop, "trail_hi": fill_px, "tp": actual_tp,
                    "partial_taken": False, "original_qty": filled_qty,
                    "sector": sector, "order_id": res["id"],
                    "entry_time": now_et().isoformat(),
                }
                taken_sectors.add(sector)
                buys_made += 1
                _state["daily_trades"] += 1
                save_positions()
                alert_buy(sym, filled_qty, fill_px, actual_stop, actual_tp, sc, reasons)
                append_log({"time": now_et().isoformat(), "sym": sym,
                            "action": "buy", "qty": filled_qty, "price": fill_px,
                            "intended_price": price, "slippage_pct": round((fill_px-price)/price*100, 3),
                            "stop": actual_stop, "tp": actual_tp, "score": sc,
                            "order_id": res["id"], "reasons": reasons, "regime": regime})

        # ── Dashboard state update ─────────────────────────
        _state["last_scan"] = now.isoformat()
        _state["positions"] = [
            {"sym": pos["symbol"],
             "qty": pos["qty"],
             "entry": float(pos.get("avg_entry_price", 0)),
             "curr":  float(pos.get("current_price", 0)),
             "pct":   round(float(pos.get("unrealized_plpc", 0))*100, 2)}
            for pos in get_positions()
        ]
        save_state()

        # ── Print summary ──────────────────────────────────
        positions_now = get_positions()
        print(f"  Regime={regime} | VIX={vix or '?'} | SPY={'bull' if spy_bull else 'bear'}"
              f" | DayPnL={daily_pnl:+.2f}% | Open={len(positions_now)}")
        for pos in positions_now:
            pct = float(pos.get("unrealized_plpc", 0))*100
            print(f"    {pos['symbol']:6s}  {pct:+.2f}%  "
                  f"curr=${float(pos.get('current_price',0)):.2f}  "
                  f"stop=${open_positions.get(pos['symbol'],{}).get('stop',0):.2f}")

        time.sleep(60)


def _sleep_until_next_open():
    """Sleep until 09:28 ET on the next trading day so the process stays alive."""
    from datetime import timedelta
    while True:
        now  = datetime.now(ET)
        today_target = now.replace(hour=9, minute=28, second=0, microsecond=0)
        # If it's a weekday and we haven't reached 09:28 yet → target is today
        if now.weekday() < 5 and now < today_target:
            target = today_target
        else:
            # Jump ahead to the next weekday
            days_ahead = 1
            if   now.weekday() == 4: days_ahead = 3   # Friday  → Monday
            elif now.weekday() == 5: days_ahead = 2   # Saturday → Monday
            target = (now + timedelta(days=days_ahead)).replace(
                hour=9, minute=28, second=0, microsecond=0)
        secs = max((target - now).total_seconds(), 0)
        print(f"\n[BOT] Market closed. Next session: {target.strftime('%Y-%m-%d %H:%M ET')}"
              f" ({secs/3600:.1f}h away). Sleeping…", flush=True)
        time.sleep(secs + 2)
        return  # wake up, outer loop calls run() again


if __name__ == "__main__":
    import traceback
    while True:
        try:
            run()
        except KeyboardInterrupt:
            print("[BOT] Keyboard interrupt — stopping.", flush=True)
            break
        except Exception as exc:
            print(f"\n[BOT CRASH] {exc}", flush=True)
            traceback.print_exc()
            print("[BOT] Restarting in 45s…", flush=True)
            time.sleep(45)
            continue
        # run() returned cleanly (EOD break) — sleep until next open
        _sleep_until_next_open()
