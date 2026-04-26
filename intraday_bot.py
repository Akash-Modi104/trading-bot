"""
Intraday Trading Bot — Alpaca Paper Trading
Budget: $1,000 | Runs 9:30 AM – 3:45 PM ET
Strategy: EMA9/EMA21 crossover + RSI(14) on 5-min bars
"""

import requests
import json
import time
from datetime import datetime, time as dtime
import pytz

# ── Credentials ──────────────────────────────────────────────
with open("alpaca_config.json") as f:
    cfg = json.load(f)

BASE_URL   = cfg["endpoint"]
DATA_URL   = "https://data.alpaca.markets/v2"
HEADERS    = {
    "APCA-API-KEY-ID":     cfg["api_key"],
    "APCA-API-SECRET-KEY": cfg["secret_key"],
    "Content-Type":        "application/json"
}

# ── Config ────────────────────────────────────────────────────
BUDGET          = 1000.0          # total capital to deploy
MAX_POSITIONS   = 2               # max concurrent positions
STOP_LOSS_PCT   = 0.015           # 1.5 % stop-loss per trade
TAKE_PROFIT_PCT = 0.03            # 3.0 % take-profit per trade
POLL_SECONDS    = 60              # check every 60 s
CLOSE_HOUR      = 15              # close all by 3:45 PM ET
CLOSE_MINUTE    = 45
ET              = pytz.timezone("America/New_York")

WATCHLIST = ["AAPL", "TSLA", "NVDA", "MSFT", "AMZN"]

# ── State ─────────────────────────────────────────────────────
positions    = {}   # symbol → {qty, entry_price}
daily_pnl    = 0.0
trade_log    = []

# ── Alpaca helpers ────────────────────────────────────────────
def get_account():
    r = requests.get(f"{BASE_URL}/account", headers=HEADERS)
    return r.json()

def get_positions():
    r = requests.get(f"{BASE_URL}/positions", headers=HEADERS)
    return r.json()

def place_order(symbol, qty, side):
    body = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": "market",
        "time_in_force": "day"
    }
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json=body)
    return r.json()

def close_position(symbol):
    r = requests.delete(f"{BASE_URL}/positions/{symbol}", headers=HEADERS)
    return r.json()

def close_all_positions():
    r = requests.delete(f"{BASE_URL}/positions", headers=HEADERS)
    return r.json()

def get_bars(symbol, timeframe="5Min", limit=30):
    params = {"timeframe": timeframe, "limit": limit, "feed": "iex"}
    r = requests.get(f"{DATA_URL}/stocks/{symbol}/bars", headers=HEADERS, params=params)
    data = r.json()
    return data.get("bars", [])

def get_latest_price(symbol):
    r = requests.get(f"{DATA_URL}/stocks/{symbol}/trades/latest", headers=HEADERS)
    data = r.json()
    return data.get("trade", {}).get("p")

# ── Indicators ────────────────────────────────────────────────
def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    result = sum(values[:period]) / period
    for v in values[period:]:
        result = v * k + result * (1 - k)
    return result

def rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100
    rs = ag / al
    return 100 - (100 / (1 + rs))

# ── Signal logic ──────────────────────────────────────────────
def get_signal(symbol):
    bars = get_bars(symbol, limit=35)
    if len(bars) < 25:
        return "hold", None
    closes = [b["c"] for b in bars]
    price  = closes[-1]
    e9     = ema(closes, 9)
    e21    = ema(closes, 21)
    e9_prev  = ema(closes[:-1], 9)
    e21_prev = ema(closes[:-1], 21)
    r      = rsi(closes)

    if e9 is None or e21 is None:
        return "hold", price

    # Crossover up + RSI bullish
    if e9_prev <= e21_prev and e9 > e21 and r > 50:
        return "buy", price
    # Crossover down + RSI bearish
    if e9_prev >= e21_prev and e9 < e21 and r < 50:
        return "sell", price

    return "hold", price

# ── Time helpers ──────────────────────────────────────────────
def now_et():
    return datetime.now(ET)

def market_open():
    n = now_et()
    return dtime(9, 30) <= n.time() <= dtime(16, 0) and n.weekday() < 5

def should_close_all():
    n = now_et()
    return n.time() >= dtime(CLOSE_HOUR, CLOSE_MINUTE)

# ── Main loop ─────────────────────────────────────────────────
def run():
    global daily_pnl
    print("=" * 55)
    print(f"  Intraday Bot started — {now_et().strftime('%Y-%m-%d')}")
    acc = get_account()
    print(f"  Buying power : ${float(acc.get('buying_power', 0)):,.2f}")
    print(f"  Budget cap   : ${BUDGET:,.2f}")
    print("=" * 55)

    while True:
        now = now_et()

        if not market_open():
            print(f"[{now.strftime('%H:%M')}] Market closed — waiting...")
            time.sleep(60)
            continue

        # Force-close all positions before end of day
        if should_close_all():
            print(f"[{now.strftime('%H:%M')}] End-of-day — closing all positions")
            close_all_positions()
            print(f"  Daily PnL: ${daily_pnl:+.2f}")
            print("Bot finished for today.")
            break

        print(f"\n[{now.strftime('%H:%M:%S')}] Scanning {WATCHLIST} ...")

        # Check stop-loss / take-profit on open positions
        open_pos = {p["symbol"]: p for p in get_positions() if isinstance(get_positions(), list)}
        try:
            open_pos = {p["symbol"]: p for p in get_positions()}
        except Exception:
            open_pos = {}

        for sym, pos in list(open_pos.items()):
            entry  = float(pos.get("avg_entry_price", 0))
            curr   = float(pos.get("current_price", entry))
            pct    = (curr - entry) / entry if entry else 0

            if pct <= -STOP_LOSS_PCT:
                print(f"  STOP-LOSS  {sym}  {pct*100:.2f}%  → selling")
                res = close_position(sym)
                pnl = float(pos.get("unrealized_pl", 0))
                daily_pnl += pnl
                trade_log.append({"sym": sym, "action": "stop-loss", "pnl": pnl, "time": now.isoformat()})

            elif pct >= TAKE_PROFIT_PCT:
                print(f"  TAKE-PROFIT {sym}  {pct*100:.2f}%  → selling")
                res = close_position(sym)
                pnl = float(pos.get("unrealized_pl", 0))
                daily_pnl += pnl
                trade_log.append({"sym": sym, "action": "take-profit", "pnl": pnl, "time": now.isoformat()})

        # Scan for new entries
        current_count = len(open_pos)
        for sym in WATCHLIST:
            if current_count >= MAX_POSITIONS:
                break
            if sym in open_pos:
                continue

            signal, price = get_signal(sym)
            print(f"  {sym:6s}  signal={signal}  price=${price}")

            if signal == "buy" and price:
                per_trade = BUDGET / MAX_POSITIONS
                qty = int(per_trade // price)
                if qty >= 1:
                    print(f"  → BUY {qty} x {sym} @ ~${price:.2f}")
                    res = place_order(sym, qty, "buy")
                    if res.get("status") in ("accepted", "pending_new", "new"):
                        positions[sym] = {"qty": qty, "entry_price": price}
                        current_count += 1
                        trade_log.append({"sym": sym, "action": "buy", "qty": qty, "price": price, "time": now.isoformat()})
                    else:
                        print(f"    Order rejected: {res.get('message')}")

        print(f"  Open positions: {current_count} | Daily PnL: ${daily_pnl:+.2f}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run()
