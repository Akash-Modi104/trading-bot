"""
Simulate the bot's behavior on the most recent Friday session.
Walks through each 5-min bar, applies all entry/exit logic from intraday_bot_v2,
and prints a detailed trade-by-trade breakdown.
"""
import os, json, sys
from datetime import datetime, timedelta
import pytz, requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Load .env
ENV = os.path.join(BASE_DIR, ".env")
if os.path.exists(ENV):
    for line in open(ENV):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

API_KEY    = os.environ["ALPACA_API_KEY"]
SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
DATA_URL   = os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets/v2")
HEADERS    = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY}
ET = pytz.timezone("America/New_York")

# -- Find last Friday -----------------------------------------
today = datetime.now(ET).date()
days_back = (today.weekday() - 4) % 7 or 7   # Friday = 4
last_friday = today - timedelta(days=days_back)
print(f"\n{'='*68}")
print(f"  SIMULATING BOT ON LAST FRIDAY: {last_friday}")
print(f"{'='*68}\n")

WATCHLIST = ["AAPL","TSLA","NVDA","MSFT","AMZN","META","GOOGL","AMD",
             "NFLX","UBER","COIN","ARM","PLTR","AVGO","MU"]

# -- Strategy params -----------------------------------------
P = {
    "ema_fast": 9, "ema_slow": 21, "rsi_period": 14,
    "rsi_buy_min": 50, "rsi_buy_max": 70,
    "stop_loss_pct": 1.5, "take_profit_pct": 3.0,
    "partial_tp_pct": 2.0, "partial_tp_frac": 0.5,
    "rel_vol_min": 1.8, "max_positions": 3, "budget_per_trade": 333,
}

SECTOR = {"AAPL":"tech","MSFT":"tech","GOOGL":"tech","META":"tech","NVDA":"tech",
          "AMD":"tech","ARM":"tech","AVGO":"tech","PLTR":"tech","MU":"tech",
          "AMZN":"consumer","TSLA":"consumer","NFLX":"comm",
          "UBER":"industrial","COIN":"crypto"}

# -- Indicators ----------------------------------------------
def ema_series(vals, n):
    if len(vals) < n: return [None]*len(vals)
    k = 2/(n+1); out = [None]*(n-1)
    s = sum(vals[:n])/n; out.append(s)
    for v in vals[n:]: s = v*k+s*(1-k); out.append(s)
    return out

def rsi(closes, n=14):
    if len(closes) < n+1: return 50
    g = [max(closes[i]-closes[i-1],0) for i in range(1,len(closes))]
    l = [max(closes[i-1]-closes[i],0) for i in range(1,len(closes))]
    ag, al = sum(g[-n:])/n, sum(l[-n:])/n
    return 100 if al==0 else 100-100/(1+ag/al)

def vwap(bars):
    num=den=0
    for b in bars:
        tp=(b["h"]+b["l"]+b["c"])/3; num+=tp*b["v"]; den+=b["v"]
    return num/den if den else None

def rel_vol(bars, lb=20):
    if len(bars) < 5: return 1.0
    avg = sum(b["v"] for b in bars[-lb-1:-1])/min(lb,len(bars)-1)
    return bars[-1]["v"]/avg if avg else 1.0

# -- Fetch Friday's bars (full session 9:30–16:00) ----------
def fetch_friday(sym):
    start = f"{last_friday}T13:30:00Z"  # 9:30 ET = 13:30 UTC (summer)
    end   = f"{last_friday}T20:30:00Z"  # 16:30 ET
    r = requests.get(f"{DATA_URL}/stocks/{sym}/bars",
                     headers=HEADERS,
                     params={"timeframe":"5Min","start":start,"end":end,
                             "limit":200,"feed":"iex"})
    d = r.json()
    return d.get("bars") or [] if isinstance(d, dict) else []

# -- Score & simulate ----------------------------------------
def simulate():
    print(f"Fetching Friday bars for {len(WATCHLIST)} symbols...\n")
    data = {}
    for s in WATCHLIST:
        bars = fetch_friday(s)
        if len(bars) >= 30:
            data[s] = bars
            print(f"  {s:6s} {len(bars)} bars  open=${bars[0]['o']:.2f} close=${bars[-1]['c']:.2f}")

    if not data:
        print("\nNo data returned — Alpaca may not have free-tier IEX data for that date.")
        return

    print(f"\n{'-'*68}\nWalking through session bar-by-bar...\n")

    open_pos   = {}   # sym → {entry, qty, stop, partial_taken}
    trades     = []
    n_bars     = max(len(b) for b in data.values())
    starting_eq = 1000.0   # simulate $1000 budget
    cash       = starting_eq
    held_sec   = set()

    for i in range(25, n_bars):  # need 25 bars for indicators
        # - Check exits on open positions -
        for sym in list(open_pos.keys()):
            if i >= len(data[sym]): continue
            curr = data[sym][i]["c"]
            pos = open_pos[sym]
            entry = pos["entry"]
            pct = (curr - entry) / entry * 100
            ts = data[sym][i]["t"][11:16]

            reason = None
            if curr <= pos["stop"]:
                reason = f"stop_loss"
            elif pct >= P["take_profit_pct"]:
                reason = f"take_profit"
            elif (not pos["partial_taken"]) and pct >= P["partial_tp_pct"] and pos["qty"] >= 2:
                # Partial take
                sell_qty = max(1, pos["qty"]//2)
                proceeds = sell_qty * curr
                cash += proceeds
                trades.append({"t":ts,"sym":sym,"action":"PARTIAL",
                               "qty":sell_qty,"px":curr,"pct":pct,"reason":"partial@+2%"})
                pos["qty"] -= sell_qty
                pos["partial_taken"] = True
                pos["stop"] = entry * 1.001  # breakeven
                continue

            if reason:
                proceeds = pos["qty"] * curr
                cash += proceeds
                trades.append({"t":ts,"sym":sym,"action":"SELL",
                               "qty":pos["qty"],"px":curr,"pct":pct,"reason":reason})
                held_sec.discard(SECTOR.get(sym,"other"))
                del open_pos[sym]

        # - Try new entries (only between bar 6 and 60 = ~10am-2pm) -
        if 6 <= i <= 60 and len(open_pos) < P["max_positions"]:
            cands = []
            for sym, bars in data.items():
                if sym in open_pos: continue
                if i+1 > len(bars): continue
                sec = SECTOR.get(sym,"other")
                if sec in held_sec: continue   # sector cap

                window = bars[:i+1]
                closes = [b["c"] for b in window]
                if len(closes) < 25: continue

                ef = ema_series(closes, 9)
                es = ema_series(closes, 21)
                if any(x is None for x in [ef[-1],es[-1],ef[-2],es[-2]]): continue

                # EMA crossover this bar?
                cross_up = ef[-2] <= es[-2] and ef[-1] > es[-1]
                if not cross_up: continue

                r = rsi(closes)
                if not (P["rsi_buy_min"] <= r <= P["rsi_buy_max"]): continue

                vw = vwap(window)
                if vw is None or closes[-1] <= vw: continue

                rv = rel_vol(window)
                if rv < 1.3: continue   # relaxed for sim

                score = 60 + min(int(rv*5), 25) + (10 if r < 65 else 0)
                cands.append((score, sym, closes[-1], r, rv, vw))

            cands.sort(reverse=True)
            for sc, sym, px, r, rv, vw in cands:
                if len(open_pos) >= P["max_positions"]: break
                if cash < P["budget_per_trade"]: break
                qty = int(P["budget_per_trade"] // px)
                if qty < 1: continue
                cost = qty * px
                cash -= cost
                stop = round(px * (1 - P["stop_loss_pct"]/100), 2)
                open_pos[sym] = {"entry":px,"qty":qty,"stop":stop,
                                 "partial_taken":False,
                                 "original_qty":qty}
                held_sec.add(SECTOR.get(sym,"other"))
                ts = data[sym][i]["t"][11:16]
                trades.append({"t":ts,"sym":sym,"action":"BUY",
                               "qty":qty,"px":px,"pct":0.0,
                               "reason":f"sc={sc} rsi={r:.0f} rv={rv:.1f}x"})

    # - Force-close at EOD -
    for sym, pos in list(open_pos.items()):
        last_px = data[sym][-1]["c"]
        pct = (last_px - pos["entry"]) / pos["entry"] * 100
        cash += pos["qty"] * last_px
        trades.append({"t":"15:55","sym":sym,"action":"SELL",
                       "qty":pos["qty"],"px":last_px,"pct":pct,
                       "reason":"eod_close"})

    # - Print results -
    print(f"{'TIME':>6} {'SYM':>6} {'ACTION':>8} {'QTY':>4} {'PRICE':>8} {'P&L%':>7}  REASON")
    print("-"*78)
    for t in trades:
        pnl = f"{t['pct']:+.2f}%" if t['pct'] != 0 else "  --  "
        print(f"{t['t']:>6} {t['sym']:>6} {t['action']:>8} {t['qty']:>4} ${t['px']:>7.2f} {pnl:>7}  {t['reason']}")

    sells = [t for t in trades if t['action'] in ("SELL","PARTIAL")]
    wins  = [t for t in sells if t['pct'] > 0]
    final_eq = cash
    print(f"\n{'='*68}")
    print(f"  FRIDAY SIMULATION RESULTS  ({last_friday})")
    print(f"{'='*68}")
    print(f"  Starting capital : ${starting_eq:,.2f}")
    print(f"  Ending capital   : ${final_eq:,.2f}")
    print(f"  P&L              : ${final_eq-starting_eq:+.2f} ({(final_eq-starting_eq)/starting_eq*100:+.2f}%)")
    print(f"  Total trades     : {len([t for t in trades if t['action']=='BUY'])} entries, {len(sells)} exits")
    print(f"  Win rate         : {len(wins)}/{len(sells)} ({len(wins)/max(len(sells),1)*100:.0f}%)")
    print(f"  Avg P&L per exit : {sum(t['pct'] for t in sells)/max(len(sells),1):+.2f}%")
    print(f"{'='*68}\n")

if __name__ == "__main__":
    simulate()
