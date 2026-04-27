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
    "rsi_buy_min": 50, "rsi_buy_max": 70,
    "atr_period": 14, "atr_multiplier": 1.5,
    "rel_vol_min": 1.8,
    "stop_loss_pct": 1.5, "take_profit_pct": 3.0,
    "trail_pct": 1.0,
    "bar_timeframe": "5Min", "max_positions": 3,
    "budget_per_trade": 333,
    "trade_start_hour": 10, "trade_start_min": 0,
    "trade_end_hour": 14,   "trade_end_min": 0,
    "close_hour": 15,       "close_min": 45,
    "vwap_required": True, "orb_required": True,
    "multi_tf_required": True, "min_confidence": 60,
    "daily_loss_limit_pct": 3.0,
    "vix_pause_threshold": 28, "vix_stop_threshold": 35,
    "spy_filter": True,
    # ── Portfolio risk + advanced exits ───────────────────────
    "sector_cap": True,           # max 1 position per sector
    "partial_tp_pct": 2.0,        # sell 50% at +2%
    "partial_tp_frac": 0.5,       # fraction to sell on partial
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
    except Exception:
        pass

def log_event(msg):
    ts = now_et().strftime("%H:%M:%S")
    print(f"  [{ts}] {msg}")
    _state["log"] = ([{"t": ts, "m": msg}] + _state["log"])[:100]

# ── Alpaca helpers ────────────────────────────────────────────
def api(method, path, **kw):
    r = requests.request(method, f"{BASE_URL}{path}", headers=HEADERS, **kw)
    try: return r.json()
    except: return {}

def data_get(path, params=None):
    r = requests.get(f"{DATA_URL}{path}", headers=HEADERS, params=params or {})
    try: return r.json()
    except: return {}

def get_account():       return api("GET", "/account")
def get_positions():
    p = api("GET", "/positions")
    return p if isinstance(p, list) else []
def place_order(sym, qty, side):
    return api("POST", "/orders", json={
        "symbol": sym, "qty": str(qty), "side": side,
        "type": "market", "time_in_force": "day"})
def close_position(sym): return api("DELETE", f"/positions/{sym}")
def close_all():         return api("DELETE", "/positions")

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
        json.dump(log[-500:], f, indent=2)

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
    """Fetch VIX via VIXY ETF as proxy"""
    try:
        bars = get_bars("VIXY", "5Min", 5)
        if bars: return bars[-1]["c"]
    except Exception:
        pass
    return None

def spy_trend(p):
    """True = SPY above its fast EMA → market bullish"""
    if not p.get("spy_filter", True): return True
    bars = get_bars("SPY", "5Min", 30)
    if len(bars) < p["ema_fast"]+2: return True
    closes = [b["c"] for b in bars]
    ef = ema_val(closes, p["ema_fast"])
    return closes[-1] > ef if ef else True

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
        else:
            return 0, {"skip": "choppy day, no BB setup"}, bars5, {}
    else:
        # 1. Multi-TF EMA confluence
        tf_sig = multi_tf(sym, p)
        if tf_sig == "buy":
            score += 25; reasons["multi_tf"] = "3-TF bullish"
        else:
            return score, {"multi_tf": "no confluence"}, bars5, {}

    # 2. RSI
    rsi = rsi_val(closes, p["rsi_period"])
    if rsi > 78 or rsi < 35:
        return 0, {"skip": f"RSI {rsi:.0f} extreme"}, bars5, {}
    if p["rsi_buy_min"] <= rsi <= p["rsi_buy_max"]:
        score += 15; reasons["rsi"] = f"RSI {rsi:.0f}"

    # 3. VWAP
    vw = vwap_val(bars5)
    if vw and current > vw:
        score += 15; reasons["vwap"] = f"${current:.2f} > VWAP ${vw:.2f}"
    elif p.get("vwap_required"):
        return score, {"vwap": "below VWAP"}, bars5, {}

    # 4. ORB
    orb_hi, orb_lo = orb_levels(bars1)
    if orb_hi and current > orb_hi:
        score += 15; reasons["orb"] = f"breakout > ${orb_hi:.2f}"
    elif p.get("orb_required") and orb_hi:
        score -= 5

    # 5. Relative Volume
    rv = rel_vol(bars5)
    if rv >= p["rel_vol_min"]:
        score += 10; reasons["rvol"] = f"{rv:.1f}x"

    # 6. MACD
    ml, sl, hist = macd(closes)
    if ml is not None:
        if ml > sl and hist > 0:
            score += 10; reasons["macd"] = f"bullish hist={hist:.3f}"
        elif ml < sl:
            score -= 5

    # 7. Bollinger position
    bb_up, bb_mid, bb_lo = bollinger(closes)
    if bb_up and bb_lo:
        bb_pct = (current - bb_lo) / (bb_up - bb_lo) if bb_up != bb_lo else 0.5
        if bb_pct < 0.4:
            score += 10; reasons["bb"] = f"lower third {bb_pct:.0%}"
        elif bb_pct > 0.85:
            score -= 10; reasons["bb"] = "near upper band"

    # 8. Claude AI boost
    for pk in load_picks():
        if pk.get("symbol") == sym:
            boost = int(pk.get("confidence", 0) * 0.12)
            score += boost
            reasons["claude"] = f"AI conf {pk.get('confidence')}% (+{boost})"
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
    except Exception:
        pass
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
        local  = open_positions.get(sym, {})

        # ── News kill-switch (highest priority) ───────────────
        if sym.upper() in bad_news:
            log_event(f"NEWS KILL {sym} | negative headline detected")
            close_position(sym)
            open_positions.pop(sym, None)
            alert_sell(sym, qty, curr, pct, "news_kill_switch")
            _state["daily_pnl"] += pct / p["max_positions"]
            append_log({"time": now_et().isoformat(), "sym": sym,
                        "action": "sell", "price": curr,
                        "pct": round(pct,2), "reason": "news_kill_switch"})
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
                            "pct": round(pct,2), "reason": "partial_take"})
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
            log_event(f"EXIT {sym} | {reason}")
            close_position(sym)
            open_positions.pop(sym, None)
            alert_sell(sym, qty, curr, pct, reason)
            _state["daily_pnl"] += pct / p["max_positions"]
            append_log({"time": now_et().isoformat(), "sym": sym,
                        "action": "sell", "price": curr,
                        "pct": round(pct,2), "reason": reason})

# ── Main loop ─────────────────────────────────────────────────
def run():
    p           = params()
    acc         = get_account()
    start_eq    = float(acc.get("equity", 100000))
    bp          = float(acc.get("buying_power", 0))

    _state["started"] = now_et().isoformat()
    _state["equity"]  = start_eq

    print("=" * 62)
    print(f"  ADVANCED TRADING BOT v2  |  {now_et().strftime('%Y-%m-%d')}")
    print(f"  Equity: ${start_eq:,.2f}  |  Buying Power: ${bp:,.2f}")
    print("=" * 62)

    wl = get_watchlist()
    alert_startup(start_eq, bp, len(wl))

    while True:
        p   = params()
        now = now_et()

        if not market_open():
            log_event("Market closed — sleeping 60s")
            time.sleep(60); continue

        # ── EOD close ──────────────────────────────────────
        if eod_time(p):
            log_event("EOD — closing all positions")
            close_all()
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

        if cur_count < p["max_positions"] and bp > p["budget_per_trade"] and spy_bull:
            wl = get_watchlist()
            _state["watchlist"] = wl
            held_syms = {pos["symbol"] for pos in get_positions()}
            held_sectors = {get_sector(s) for s in held_syms} - {"etf", "other"}
            bad_news = load_negative_news()
            candidates = []

            for sym in wl:
                if sym in held_syms or sym == "SPY" or sym == "VIXY": continue
                # Skip if breaking negative news on this ticker
                if sym.upper() in bad_news:
                    log_event(f"{sym} skipped — negative news")
                    continue
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

                budget = p["budget_per_trade"] * vix_size_mult
                qty    = int(budget // price)
                if qty < 1: continue

                stop = atr_stop(bars5, price, p["atr_multiplier"])
                tp   = round(price * (1 + p["take_profit_pct"]/100), 2)

                log_event(f"BUY {qty}x {sym} @ ${price:.2f} | stop=${stop:.2f} tp=${tp:.2f} score={sc} sector={sector}")
                res = place_order(sym, qty, "buy")

                if res.get("status") in ("accepted","pending_new","new"):
                    open_positions[sym] = {
                        "qty": qty, "entry": price,
                        "stop": stop, "trail_hi": price, "tp": tp,
                        "partial_taken": False, "original_qty": qty,
                        "sector": sector,
                    }
                    taken_sectors.add(sector)
                    buys_made += 1
                    _state["daily_trades"] += 1
                    alert_buy(sym, qty, price, stop, tp, sc, reasons)
                    append_log({"time": now_et().isoformat(), "sym": sym,
                                "action": "buy", "qty": qty, "price": price,
                                "stop": stop, "tp": tp, "score": sc,
                                "reasons": reasons, "regime": regime})
                else:
                    log_event(f"Order rejected: {res.get('message','')}")

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


if __name__ == "__main__":
    run()
