"""
End-of-Day Backtesting & HTML Report Generator
Runs after market close each trading day.
"""

import json
import os
import base64
import requests
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from datetime import datetime
import pytz

# ── Config ────────────────────────────────────────────────────
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
CONFIG     = os.path.join(BASE_DIR, "alpaca_config.json")
STRAT_FILE = os.path.join(BASE_DIR, "strategy_params.json")
HISTORY_F  = os.path.join(BASE_DIR, "strategy_history.json")
REPORTS    = os.path.join(BASE_DIR, "reports")
os.makedirs(REPORTS, exist_ok=True)

ET = pytz.timezone("America/New_York")

# ── Load .env credentials ──────────────────────────────────────
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

API_KEY    = os.environ.get("ALPACA_API_KEY", "")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")
DATA_URL   = os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets/v2")

HEADERS = {
    "APCA-API-KEY-ID":     API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY,
}

WATCHLIST = ["AAPL", "TSLA", "NVDA", "MSFT", "AMZN", "META", "GOOGL", "AMD"]

# Real-world trading cost model ─────────────────────────────────
# Slippage: market orders fill 5 bps off mid (entry & exit each)
# Commission: Alpaca paper has $0 but model SEC/FINRA fees on sells
SLIPPAGE_BPS    = 5.0       # 0.05% per side
COMMISSION_PCT  = 0.003     # ~0.003% sell-side regulatory fees

DEFAULT_PARAMS = {
    "ema_fast": 9, "ema_slow": 21, "rsi_period": 14,
    "rsi_buy_min": 50, "rsi_buy_max": 70,
    "stop_loss_pct": 1.5, "take_profit_pct": 3.0,
    "bar_timeframe": "5Min", "max_positions": 2, "budget_per_trade": 500,
    "partial_tp_pct": 2.0, "partial_tp_frac": 0.5,
    "sector_cap": True,
}

COLORS = {
    "bg": "#0d1117", "panel": "#161b22", "border": "#30363d",
    "blue": "#58a6ff", "green": "#39d353", "red": "#f85149",
    "yellow": "#e3b341", "purple": "#bc8cff",
    "text": "#c9d1d9", "muted": "#8b949e",
}

# ── Helpers ───────────────────────────────────────────────────
def load_params():
    if os.path.exists(STRAT_FILE):
        with open(STRAT_FILE) as f:
            return json.load(f)
    return DEFAULT_PARAMS.copy()

def save_params(p):
    with open(STRAT_FILE, "w") as f:
        json.dump(p, f, indent=2)

def get_account():
    r = requests.get(f"{BASE_URL}/account", headers=HEADERS)
    return r.json()

def get_bars(symbol, timeframe="5Min", limit=80):
    params = {"timeframe": timeframe, "limit": limit, "feed": "iex"}
    r = requests.get(f"{DATA_URL}/stocks/{symbol}/bars", headers=HEADERS, params=params)
    data = r.json()
    if not isinstance(data, dict):
        return []
    return data.get("bars") or []

def img_to_b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()

# ── Indicators ─────────────────────────────────────────────────
def ema_series(values, period):
    if len(values) < period:
        return [None] * len(values)
    k = 2 / (period + 1)
    out  = [None] * (period - 1)
    seed = sum(values[:period]) / period
    out.append(seed)
    for v in values[period:]:
        seed = v * k + seed * (1 - k)
        out.append(seed)
    return out

def rsi_series(closes, period=14):
    if len(closes) < period + 1:
        return [50.0] * len(closes)
    out = [None] * period
    gains  = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    def _r(ag, al): return 100 if al == 0 else 100 - 100 / (1 + ag / al)
    out.append(_r(ag, al))
    for i in range(period, len(gains)):
        ag = (ag * (period-1) + gains[i]) / period
        al = (al * (period-1) + losses[i]) / period
        out.append(_r(ag, al))
    return out

# ── Backtest ───────────────────────────────────────────────────
def _apply_costs(entry_price, exit_price):
    """Apply slippage + commission to a round-trip trade.
    Returns (effective_entry, effective_exit, net_pct)"""
    slip = SLIPPAGE_BPS / 10000.0
    eff_entry = entry_price * (1 + slip)        # buy fills slightly higher
    eff_exit  = exit_price  * (1 - slip)        # sell fills slightly lower
    gross_pct = (eff_exit - eff_entry) / eff_entry * 100
    net_pct   = gross_pct - COMMISSION_PCT      # regulatory sell fees
    return round(eff_entry, 4), round(eff_exit, 4), round(net_pct, 3)

def backtest(symbol, params, bars=None, include_costs=True):
    """Backtest strategy. If bars provided, use those (for walk-forward).
    Otherwise fetch limit=80 (latest day)."""
    if bars is None:
        bars = get_bars(symbol, timeframe=params["bar_timeframe"], limit=80)
    if len(bars) < 30:
        return None
    closes = [b["c"] for b in bars]
    times  = [b["t"] for b in bars]
    vols   = [b["v"] for b in bars]
    ef = ema_series(closes, params["ema_fast"])
    es = ema_series(closes, params["ema_slow"])
    rv = rsi_series(closes, params["rsi_period"])

    trades, pos, ep, ei = [], None, 0, 0
    partial_tp = params.get("partial_tp_pct", None)
    for i in range(1, len(closes)):
        if any(x is None for x in [ef[i], es[i], ef[i-1], es[i-1]]):
            continue
        cup  = ef[i-1] <= es[i-1] and ef[i] > es[i]
        cdown= ef[i-1] >= es[i-1] and ef[i] < es[i]
        rok  = params["rsi_buy_min"] <= (rv[i] or 50) <= params["rsi_buy_max"]
        price = closes[i]
        if pos is None and cup and rok:
            pos, ep, ei = "long", price, i
        elif pos == "long":
            pct = (price - ep) / ep * 100
            reason = None
            if pct >= params["take_profit_pct"]:  reason = "take_profit"
            elif pct <= -params["stop_loss_pct"]: reason = "stop_loss"
            elif cdown:                            reason = "signal_exit"
            if reason:
                gross_pct = round(pct, 2)
                if include_costs:
                    eff_e, eff_x, net = _apply_costs(ep, price)
                    trades.append({"entry_time": times[ei], "exit_time": times[i],
                                   "entry": ep, "exit": price,
                                   "pct": net, "gross_pct": gross_pct,
                                   "reason": reason, "bars_held": i - ei})
                else:
                    trades.append({"entry_time": times[ei], "exit_time": times[i],
                                   "entry": ep, "exit": price,
                                   "pct": gross_pct, "gross_pct": gross_pct,
                                   "reason": reason, "bars_held": i - ei})
                pos = None

    win  = [t for t in trades if t["pct"] > 0]
    loss = [t for t in trades if t["pct"] <= 0]
    gross_total = round(sum(t.get("gross_pct", t["pct"]) for t in trades), 2)
    net_total   = round(sum(t["pct"] for t in trades), 2)
    return {
        "symbol": symbol,
        "trades": trades, "total_trades": len(trades),
        "wins": len(win), "losses": len(loss),
        "win_rate": round(len(win)/len(trades)*100, 1) if trades else 0,
        "total_pct": net_total,            # net of costs
        "gross_pct": gross_total,           # before costs
        "cost_drag": round(gross_total - net_total, 2),
        "avg_win":  round(np.mean([t["pct"] for t in win]),  2) if win  else 0,
        "avg_loss": round(np.mean([t["pct"] for t in loss]), 2) if loss else 0,
        "closes": closes, "times": times, "volumes": vols,
        "ema_fast": ef, "ema_slow": es, "rsi": rv,
        "ema_fast_n": params["ema_fast"], "ema_slow_n": params["ema_slow"],
    }

# ── Walk-Forward Validation ────────────────────────────────────
def walk_forward_validate(candidate_params, baseline_params):
    """
    Fetch ~37 days of 5-min bars, split into:
      - train: first 30 days  (used to optimize candidate_params)
      - test:  last 7 days     (out-of-sample validation)
    Only accept candidate_params if test P&L >= baseline P&L on test set.
    Returns (chosen_params, wf_notes).
    """
    notes = []
    train_pnl_cand = test_pnl_cand = 0.0
    train_pnl_base = test_pnl_base = 0.0
    n_symbols = 0

    # 5-min bars: ~78 per trading day. 37 days ≈ 2900 bars (cap at 10000).
    BARS_PER_DAY = 78
    TOTAL_DAYS   = 37
    TEST_DAYS    = 7
    fetch_limit  = TOTAL_DAYS * BARS_PER_DAY

    for sym in WATCHLIST:
        bars = get_bars(sym, timeframe="5Min", limit=fetch_limit)
        if len(bars) < (TOTAL_DAYS - TEST_DAYS) * BARS_PER_DAY * 0.5:
            continue
        n_symbols += 1
        split_idx = max(len(bars) - TEST_DAYS * BARS_PER_DAY, len(bars) // 2)
        train_bars = bars[:split_idx]
        test_bars  = bars[split_idx:]

        rc = backtest(sym, candidate_params, bars=train_bars)
        rb = backtest(sym, baseline_params,  bars=train_bars)
        rc_t = backtest(sym, candidate_params, bars=test_bars)
        rb_t = backtest(sym, baseline_params,  bars=test_bars)

        if rc:    train_pnl_cand += rc["total_pct"]
        if rb:    train_pnl_base += rb["total_pct"]
        if rc_t:  test_pnl_cand  += rc_t["total_pct"]
        if rb_t:  test_pnl_base  += rb_t["total_pct"]

    if n_symbols == 0:
        notes.append("Walk-forward: insufficient data — keeping baseline params.")
        return baseline_params, notes

    notes.append(f"Walk-forward ({n_symbols} symbols, {TOTAL_DAYS-TEST_DAYS}d train / {TEST_DAYS}d test):")
    notes.append(f"  Train: candidate {train_pnl_cand:+.2f}% vs baseline {train_pnl_base:+.2f}%")
    notes.append(f"  Test:  candidate {test_pnl_cand:+.2f}% vs baseline {test_pnl_base:+.2f}%")

    # Reject candidate if it underperforms baseline on out-of-sample test
    # (this is the overfitting check)
    if test_pnl_cand >= test_pnl_base - 0.5:  # 0.5% tolerance
        notes.append(f"  ✓ Candidate accepted (test set passed)")
        return candidate_params, notes
    else:
        drop = test_pnl_base - test_pnl_cand
        notes.append(f"  ✗ Candidate REJECTED — overfitting suspected ({drop:.2f}% worse on test)")
        return baseline_params, notes

# ── Optimiser ──────────────────────────────────────────────────
def optimise(results, params):
    new   = params.copy()
    trades= [t for r in results if r for t in r["trades"]]
    if not trades:
        return new, ["No trades today — parameters unchanged."]

    wr   = sum(1 for t in trades if t["pct"] > 0) / len(trades) * 100
    al   = np.mean([t["pct"] for t in trades if t["pct"] <= 0] or [0])
    aw   = np.mean([t["pct"] for t in trades if t["pct"] > 0]  or [0])
    ah   = np.mean([t["bars_held"] for t in trades])
    notes= []

    if wr < 45:
        new["rsi_buy_min"] = min(new["rsi_buy_min"] + 2, 60)
        notes.append(f"Win rate {wr:.0f}% < 45% → raised RSI min to {new['rsi_buy_min']}")
    if wr > 70:
        new["rsi_buy_min"] = max(new["rsi_buy_min"] - 2, 45)
        notes.append(f"Win rate {wr:.0f}% > 70% → relaxed RSI min to {new['rsi_buy_min']}")
    if al < -2.0:
        new["stop_loss_pct"] = round(max(new["stop_loss_pct"] - 0.2, 0.8), 1)
        notes.append(f"Avg loss {al:.2f}% → tightened stop-loss to {new['stop_loss_pct']}%")
    if 0 < aw < 1.5:
        new["take_profit_pct"] = round(max(new["take_profit_pct"] - 0.3, 1.5), 1)
        notes.append(f"Avg win {aw:.2f}% → lowered take-profit to {new['take_profit_pct']}%")
    if ah > 20:
        new["ema_fast"] = max(new["ema_fast"] - 1, 5)
        notes.append(f"Avg hold {ah:.0f} bars → faster EMA to {new['ema_fast']}")
    if not notes:
        notes.append("Strategy performing well — no parameter changes needed.")
    return new, notes

# ── Charts (matplotlib → base64 PNG) ──────────────────────────
def styled(ax, title=""):
    ax.set_facecolor(COLORS["panel"])
    ax.tick_params(colors=COLORS["text"], labelsize=8)
    for s in ax.spines.values():
        s.set_edgecolor(COLORS["border"])
    if title:
        ax.set_title(title, color=COLORS["text"], fontsize=10, fontweight="bold", pad=5)

def fig_b64(fig):
    tmp = os.path.join(REPORTS, "_tmp.png")
    fig.savefig(tmp, dpi=130, bbox_inches="tight", facecolor=COLORS["bg"])
    plt.close(fig)
    return img_to_b64(tmp)

def chart_pnl(trades):
    fig, ax = plt.subplots(figsize=(10, 3.2), facecolor=COLORS["bg"])
    styled(ax, "Cumulative P&L (%) — Backtest")
    cum, xs, ys, clrs = 0, [], [], []
    for i, t in enumerate(trades):
        cum += t["pct"]; xs.append(i+1); ys.append(round(cum,2))
        clrs.append(COLORS["green"] if cum >= 0 else COLORS["red"])
    if xs:
        ax.bar(xs, ys, color=clrs, alpha=0.85)
        ax.axhline(0, color=COLORS["muted"], linewidth=0.8, linestyle="--")
        ax.set_xlabel("Trade #", color=COLORS["muted"], fontsize=8)
        ax.set_ylabel("Cum %", color=COLORS["muted"], fontsize=8)
    plt.tight_layout()
    return fig_b64(fig)

def chart_win_loss(results):
    syms = [r["symbol"] for r in results]
    wins = [r["wins"]   for r in results]
    loss = [r["losses"] for r in results]
    x    = np.arange(len(syms))
    fig, ax = plt.subplots(figsize=(9, 3.2), facecolor=COLORS["bg"])
    styled(ax, "Win / Loss by Symbol")
    w = 0.35
    ax.bar(x-w/2, wins, w, label="Wins",   color=COLORS["green"], alpha=0.85)
    ax.bar(x+w/2, loss, w, label="Losses", color=COLORS["red"],   alpha=0.85)
    ax.set_xticks(x); ax.set_xticklabels(syms, color=COLORS["text"], fontsize=9)
    ax.legend(fontsize=8, facecolor=COLORS["panel"], labelcolor=COLORS["text"])
    plt.tight_layout()
    return fig_b64(fig)

def chart_price(r):
    closes = r["closes"][-60:]
    ef     = r["ema_fast"][-60:]
    es     = r["ema_slow"][-60:]
    vols   = r["volumes"][-60:]
    xs     = list(range(len(closes)))
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 5), facecolor=COLORS["bg"],
                                    gridspec_kw={"height_ratios": [3, 1]})
    styled(ax1, f"{r['symbol']} — Price + EMA({r['ema_fast_n']}/{r['ema_slow_n']})")
    ax1.plot(xs, closes, color=COLORS["text"],   linewidth=1.2, label="Close")
    ef_clean = [v for v in ef if v is not None]
    ef_xs    = [i for i, v in enumerate(ef) if v is not None]
    es_clean = [v for v in es if v is not None]
    es_xs    = [i for i, v in enumerate(es) if v is not None]
    if ef_clean: ax1.plot(ef_xs, ef_clean, color=COLORS["yellow"],  linewidth=1.1,
                          label=f"EMA{r['ema_fast_n']}", linestyle="--")
    if es_clean: ax1.plot(es_xs, es_clean, color=COLORS["purple"],  linewidth=1.1,
                          label=f"EMA{r['ema_slow_n']}", linestyle="--")
    ax1.legend(fontsize=7, facecolor=COLORS["panel"], labelcolor=COLORS["text"])
    styled(ax2, "Volume")
    ax2.bar(xs, vols, color=COLORS["blue"], alpha=0.6)
    plt.tight_layout()
    return fig_b64(fig)

def chart_rsi(r):
    rv   = [v for v in r["rsi"][-60:] if v is not None]
    xs   = list(range(len(rv)))
    fig, ax = plt.subplots(figsize=(10, 2.5), facecolor=COLORS["bg"])
    styled(ax, f"{r['symbol']} — RSI(14)")
    ax.plot(xs, rv, color=COLORS["blue"], linewidth=1.2)
    ax.axhline(70, color=COLORS["red"],   linewidth=0.8, linestyle="--", alpha=0.7)
    ax.axhline(50, color=COLORS["muted"], linewidth=0.8, linestyle="--", alpha=0.5)
    ax.axhline(30, color=COLORS["green"], linewidth=0.8, linestyle="--", alpha=0.7)
    ax.fill_between(xs, rv, 50, where=[v>=50 for v in rv], alpha=0.12, color=COLORS["green"])
    ax.fill_between(xs, rv, 50, where=[v< 50 for v in rv], alpha=0.12, color=COLORS["red"])
    ax.set_ylim(0, 100); ax.set_ylabel("RSI", color=COLORS["muted"], fontsize=8)
    plt.tight_layout()
    return fig_b64(fig)

def chart_params_history():
    if not os.path.exists(HISTORY_F):
        return None
    with open(HISTORY_F) as f:
        h = json.load(f)
    if len(h) < 2:
        return None
    dates = [x["date"] for x in h]
    sl    = [x["params"]["stop_loss_pct"]   for x in h]
    tp    = [x["params"]["take_profit_pct"] for x in h]
    rm    = [x["params"]["rsi_buy_min"]     for x in h]
    fig, ax = plt.subplots(figsize=(10, 3), facecolor=COLORS["bg"])
    styled(ax, "Strategy Parameter Evolution (30 days)")
    ax.plot(dates, sl, color=COLORS["red"],    marker="o", label="Stop Loss %",   linewidth=1.5)
    ax.plot(dates, tp, color=COLORS["green"],  marker="o", label="Take Profit %", linewidth=1.5)
    ax.plot(dates, rm, color=COLORS["yellow"], marker="o", label="RSI Buy Min",   linewidth=1.5)
    ax.legend(fontsize=8, facecolor=COLORS["panel"], labelcolor=COLORS["text"])
    plt.xticks(rotation=30, ha="right", fontsize=7, color=COLORS["text"])
    plt.tight_layout()
    return fig_b64(fig)

# ── HTML builder ───────────────────────────────────────────────
def b64_img_tag(b64, w="100%"):
    return f'<img src="data:image/png;base64,{b64}" style="width:{w};border-radius:8px;margin:8px 0">'

def kpi_card(label, value, color):
    return f'''
    <div class="kpi">
      <div class="kpi-label">{label}</div>
      <div class="kpi-value" style="color:{color}">{value}</div>
    </div>'''

def load_live_trades_today(date_str):
    """Read trade_log.json and return today's actual executed trades (buy+sell pairs)."""
    log_f = os.path.join(BASE_DIR, "trade_log.json")
    if not os.path.exists(log_f):
        return []
    try:
        with open(log_f) as f:
            log = json.load(f)
    except Exception:
        return []
    return [t for t in log if str(t.get("time", "")).startswith(date_str)]

def build_live_trades_html(live_trades):
    """Render today's actual buys/sells with realized P&L."""
    if not live_trades:
        return ('<div class="card"><h2>Live Trades Today</h2>'
                f'<p style="color:{COLORS["muted"]}">No trades executed today.</p></div>')
    sells = [t for t in live_trades if t.get("action") == "sell"]
    buys  = [t for t in live_trades if t.get("action") == "buy"]
    realized = sum(float(t.get("pnl_abs") or 0) for t in sells)
    wins  = sum(1 for t in sells if float(t.get("pct", 0)) > 0)
    wr    = (wins / len(sells) * 100) if sells else 0

    rows = ""
    for t in live_trades[-30:]:
        side = t.get("action", "")
        time_s = str(t.get("time", ""))[11:19]
        sym  = t.get("sym", "")
        qty  = t.get("qty", "")
        px   = t.get("price", 0)
        pct  = t.get("pct")
        pnl  = t.get("pnl_abs")
        reason = t.get("reason", "") or ", ".join(list((t.get("reasons") or {}).keys())[:3])
        if side == "sell":
            cls = "pos" if (pct or 0) > 0 else "neg"
            pct_html = f'<span class="{cls}">{pct:+.2f}%</span>' if pct is not None else "-"
            pnl_html = f'<span class="{cls}">${pnl:+.2f}</span>' if pnl is not None else "-"
        else:
            pct_html = "-"; pnl_html = "-"
        rows += (f"<tr><td>{time_s}</td><td><b>{side.upper()}</b></td>"
                 f"<td>{sym}</td><td>{qty}</td><td>${px:.2f}</td>"
                 f"<td>{pct_html}</td><td>{pnl_html}</td><td>{reason}</td></tr>")

    pnl_clr = COLORS["green"] if realized >= 0 else COLORS["red"]
    return f"""
    <div class="card">
      <h2>Live Trades Executed Today</h2>
      <div class="kpi-row">
        {kpi_card("Buys",          str(len(buys)),       COLORS['blue'])}
        {kpi_card("Sells",         str(len(sells)),      COLORS['blue'])}
        {kpi_card("Win Rate",      f"{wr:.0f}%",         COLORS['green'] if wr>=50 else COLORS['red'])}
        {kpi_card("Realized P&L",  f"${realized:+,.2f}", pnl_clr)}
      </div>
      <table>
        <tr><th>Time</th><th>Side</th><th>Sym</th><th>Qty</th><th>Price</th>
            <th>%</th><th>$ P&L</th><th>Reason</th></tr>
        {rows}
      </table>
    </div>"""

def build_html(date_str, account, results, all_trades, new_params, opt_notes):
    bp     = float(account.get("buying_power", 0))
    equity = float(account.get("equity", 0))
    valid  = [r for r in results if r and r["total_trades"] > 0]
    total_pnl   = sum(t["pct"] for t in all_trades)
    gross_pnl   = sum(t.get("gross_pct", t["pct"]) for t in all_trades)
    cost_drag   = round(gross_pnl - total_pnl, 2)
    win_ct      = sum(1 for t in all_trades if t["pct"] > 0)
    win_rate    = win_ct / len(all_trades) * 100 if all_trades else 0

    pnl_b64  = chart_pnl(all_trades) if all_trades else None
    wl_b64   = chart_win_loss(valid)  if valid      else None
    ev_b64   = chart_params_history()

    recs = []
    if win_rate < 40:
        recs.append("Win rate below 40% — consider reducing position size or widening stock screening criteria.")
    if win_rate > 65:
        recs.append("Strong win rate — strategy is working well. Consider slightly increasing per-trade budget.")
    if total_pnl < -3:
        recs.append("Negative day — review entry timing. Consider waiting for 2 confirmed bullish bars before entering.")
    if total_pnl > 5:
        recs.append("Profitable day — parameters effective. Monitor for overfitting to today's market conditions.")
    if not recs:
        recs.append("Performance within expected range. Continue current strategy and review after 3 trading days.")

    css = f"""
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background:{COLORS['bg']}; color:{COLORS['text']}; font-family:'Segoe UI',sans-serif; padding:24px; }}
    h1 {{ color:{COLORS['blue']}; font-size:2rem; text-align:center; margin-bottom:4px; }}
    h2 {{ color:{COLORS['blue']}; font-size:1.2rem; margin:24px 0 10px; border-left:3px solid {COLORS['blue']}; padding-left:10px; }}
    h3 {{ color:{COLORS['text']}; font-size:1rem; margin:16px 0 8px; }}
    .subtitle {{ color:{COLORS['muted']}; text-align:center; font-size:.9rem; margin-bottom:24px; }}
    .kpi-row {{ display:flex; gap:12px; flex-wrap:wrap; margin:12px 0; }}
    .kpi {{ background:{COLORS['panel']}; border:1px solid {COLORS['border']}; border-radius:8px;
             padding:14px 20px; flex:1; min-width:130px; text-align:center; }}
    .kpi-label {{ color:{COLORS['muted']}; font-size:.78rem; margin-bottom:6px; text-transform:uppercase; letter-spacing:.05em; }}
    .kpi-value {{ font-size:1.4rem; font-weight:700; }}
    .card {{ background:{COLORS['panel']}; border:1px solid {COLORS['border']}; border-radius:10px;
              padding:18px; margin:16px 0; }}
    table {{ width:100%; border-collapse:collapse; font-size:.82rem; margin-top:8px; }}
    th {{ background:{COLORS['blue']}; color:{COLORS['bg']}; padding:8px 10px; text-align:center; }}
    td {{ padding:6px 10px; text-align:center; border-bottom:1px solid {COLORS['border']}; }}
    tr:nth-child(even) td {{ background:{COLORS['bg']}; }}
    .pos {{ color:{COLORS['green']}; font-weight:700; }}
    .neg {{ color:{COLORS['red']};   font-weight:700; }}
    .tag {{ display:inline-block; padding:2px 8px; border-radius:4px; font-size:.75rem; font-weight:700; }}
    .tag-tp  {{ background:#1a3a1a; color:{COLORS['green']}; }}
    .tag-sl  {{ background:#3a1a1a; color:{COLORS['red']}; }}
    .tag-sig {{ background:#1a1a3a; color:{COLORS['purple']}; }}
    .note {{ background:#1a1a0d; border-left:3px solid {COLORS['yellow']};
              padding:10px 14px; border-radius:4px; margin:6px 0; font-size:.88rem;
              color:{COLORS['yellow']}; }}
    .rec  {{ background:#0d1a1a; border-left:3px solid {COLORS['blue']};
              padding:10px 14px; border-radius:4px; margin:6px 0; font-size:.88rem; }}
    .param-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(160px,1fr)); gap:10px; margin:10px 0; }}
    .param-item {{ background:{COLORS['bg']}; border:1px solid {COLORS['border']}; border-radius:6px;
                   padding:10px; text-align:center; }}
    .param-key {{ color:{COLORS['muted']}; font-size:.75rem; }}
    .param-val {{ color:{COLORS['yellow']}; font-size:1.1rem; font-weight:700; margin-top:4px; }}
    hr {{ border:none; border-top:1px solid {COLORS['border']}; margin:20px 0; }}
    """

    def tag_html(reason):
        cls = {"take_profit": "tag-tp", "stop_loss": "tag-sl"}.get(reason, "tag-sig")
        return f'<span class="tag {cls}">{reason.replace("_"," ")}</span>'

    symbol_sections = ""
    for r in valid:
        pc_b64  = chart_price(r)
        rc_b64  = chart_rsi(r)
        wr_clr  = COLORS["green"] if r["win_rate"] >= 50 else COLORS["red"]
        pnl_clr = COLORS["green"] if r["total_pct"] >= 0 else COLORS["red"]
        trade_rows = ""
        for t in r["trades"][-12:]:
            pc = f'<span class="{"pos" if t["pct"]>0 else "neg"}">{t["pct"]:+.2f}%</span>'
            trade_rows += f"""
            <tr>
              <td>{t['entry_time'][:16].replace('T',' ')}</td>
              <td>{t['exit_time'][:16].replace('T',' ')}</td>
              <td>${t['entry']:.2f}</td>
              <td>${t['exit']:.2f}</td>
              <td>{pc}</td>
              <td>{tag_html(t['reason'])}</td>
            </tr>"""

        symbol_sections += f"""
        <div class="card">
          <h2>{r['symbol']} — Detailed Analysis</h2>
          <div class="kpi-row">
            {kpi_card("Trades",    r['total_trades'],             COLORS['text'])}
            {kpi_card("Win Rate",  f"{r['win_rate']}%",           wr_clr)}
            {kpi_card("Total P&L", f"{r['total_pct']:+.2f}%",    pnl_clr)}
            {kpi_card("Avg Win",   f"{r['avg_win']:+.2f}%",      COLORS['green'])}
            {kpi_card("Avg Loss",  f"{r['avg_loss']:+.2f}%",     COLORS['red'])}
          </div>
          {b64_img_tag(pc_b64)}
          {b64_img_tag(rc_b64)}
          <h3>Trade Log (last 12)</h3>
          <table>
            <tr><th>Entry Time</th><th>Exit Time</th><th>Entry $</th><th>Exit $</th><th>P&L %</th><th>Reason</th></tr>
            {trade_rows}
          </table>
        </div>"""

    p = new_params
    param_grid = "".join([
        f'<div class="param-item"><div class="param-key">{k.replace("_"," ").title()}</div><div class="param-val">{v}</div></div>'
        for k, v in p.items()
    ])

    notes_html = "".join(f'<div class="note">• {n}</div>' for n in opt_notes)
    recs_html  = "".join(f'<div class="rec">→ {r}</div>' for r in recs)

    ev_section = ""
    if ev_b64:
        ev_section = f"<h2>Strategy Parameter Evolution (30 days)</h2>{b64_img_tag(ev_b64)}"

    pnl_section = ""
    if pnl_b64:
        pnl_section = f"<h2>Cumulative P&L — Backtest</h2><div class='card'>{b64_img_tag(pnl_b64)}</div>"

    wl_section = ""
    if wl_b64:
        wl_section = f"<h2>Win / Loss by Symbol</h2><div class='card'>{b64_img_tag(wl_b64)}</div>"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trading Report — {date_str}</title>
<style>{css}</style>
</head>
<body>
  <h1>Intraday Trading Report</h1>
  <div class="subtitle">Date: {date_str} &nbsp;|&nbsp; Generated: {datetime.now(ET).strftime('%I:%M %p ET')}</div>
  <hr>

  <h2>Account Overview</h2>
  <div class="kpi-row">
    {kpi_card("Equity",           f"${equity:,.0f}",     COLORS['green'] if equity >= 100000 else COLORS['yellow'])}
    {kpi_card("Buying Power",     f"${bp:,.0f}",         COLORS['blue'])}
    {kpi_card("Stocks Scanned",   str(len(results)),     COLORS['text'])}
    {kpi_card("Backtest Trades",  str(len(all_trades)),  COLORS['purple'])}
    {kpi_card("Overall Win Rate", f"{win_rate:.0f}%",    COLORS['green'] if win_rate>=50 else COLORS['red'])}
    {kpi_card("Net P&L",          f"{total_pnl:+.2f}%", COLORS['green'] if total_pnl>=0 else COLORS['red'])}
    {kpi_card("Gross P&L",        f"{gross_pnl:+.2f}%", COLORS['muted'])}
    {kpi_card("Cost Drag",        f"-{cost_drag:.2f}%", COLORS['yellow'])}
  </div>

  {build_live_trades_html(load_live_trades_today(date_str))}

  {pnl_section}
  {wl_section}
  {symbol_sections}

  <hr>
  <div class="card">
    <h2>Strategy Optimisation</h2>
    {ev_section}
    <h3>Today's Changes</h3>
    {notes_html}
    <h3>Updated Parameters for Tomorrow</h3>
    <div class="param-grid">{param_grid}</div>
  </div>

  <div class="card">
    <h2>Recommendations for Tomorrow</h2>
    {recs_html}
  </div>
</body>
</html>"""
    return html


# ── Main ───────────────────────────────────────────────────────
def main():
    today   = datetime.now(ET).strftime("%Y-%m-%d")
    params  = load_params()
    account = get_account()
    print(f"[EOD] {today} | Equity: ${float(account.get('equity',0)):,.2f}")

    results = []
    for sym in WATCHLIST:
        print(f"  Backtesting {sym}...")
        r = backtest(sym, params)
        results.append(r)

    all_trades = [t for r in results if r for t in r["trades"]]
    print(f"  Total backtest trades: {len(all_trades)}")

    new_params, opt_notes = optimise(results, params)

    # ── Walk-forward validation (overfitting guard) ─────────────
    # Only swap in new_params if they survive 7-day out-of-sample test
    print("  Running walk-forward validation (30d train / 7d test)...")
    validated_params, wf_notes = walk_forward_validate(new_params, params)
    opt_notes.extend(wf_notes)
    save_params(validated_params)
    new_params = validated_params

    # Append strategy history
    history = []
    if os.path.exists(HISTORY_F):
        with open(HISTORY_F) as f:
            try: history = json.load(f)
            except: history = []
    win_ct = sum(1 for t in all_trades if t["pct"] > 0)
    history.append({
        "date": today, "params": new_params,
        "win_rate": win_ct / max(len(all_trades), 1) * 100,
        "total_pnl": sum(t["pct"] for t in all_trades),
    })
    with open(HISTORY_F, "w") as f:
        json.dump(history[-30:], f, indent=2)

    html = build_html(today, account, results, all_trades, new_params, opt_notes)
    out  = os.path.join(REPORTS, f"trading_report_{today}.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n  Report saved: {out}")
    return out


if __name__ == "__main__":
    out = main()
    print(f"Done: {out}")
