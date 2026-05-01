"""
Telegram Trade Alert Module — per-user fan-out.
Falls back to legacy single-bot creds in .env if the DB layer is unavailable
(e.g. the bot process can't import db/auth).
"""
import os, json, requests
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Load .env (legacy single-bot fallback)
_ENV_FILE = os.path.join(BASE_DIR, ".env")
try:
    from dotenv import load_dotenv
    load_dotenv(_ENV_FILE)
except ImportError:
    if os.path.exists(_ENV_FILE):
        with open(_ENV_FILE) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip())

# Try to import the per-user backend; degrade gracefully if not available
try:
    import sys
    if BASE_DIR not in sys.path:
        sys.path.insert(0, BASE_DIR)
    import auth as _auth
    _MULTI_USER_OK = True
except Exception:
    _MULTI_USER_OK = False

def _post(token, chat_id, message):
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=5
        )
    except Exception:
        pass

def send(message: str, event: str = "info"):
    """Send to all enabled users (DB) plus legacy single-bot if configured.
    `event` is one of buy/sell/eod/vix/startup/info — used to filter per-user."""
    sent = 0
    # Per-user fan-out
    if _MULTI_USER_OK:
        try:
            for u in _auth.list_active_telegram():
                # Allow if event missing in user config OR explicitly enabled
                ev_map = u.get("events") or {}
                if event != "info" and event in ev_map and not ev_map.get(event):
                    continue
                _post(u["token"], u["chat_id"], message)
                sent += 1
        except Exception:
            pass
    # Legacy single-bot fallback
    if os.environ.get("TELEGRAM_ENABLED", "false").lower() == "true":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if token and chat_id:
            _post(token, chat_id, message)
            sent += 1
    return sent

def alert_buy(sym, qty, price, stop, tp, score, reasons):
    reason_str = " | ".join(k for k in reasons if k not in ("error","claude_reason"))
    send(
        f"<b>BUY {sym}</b>\n"
        f"Qty: {qty} shares @ ${price:.2f}\n"
        f"Stop: ${stop:.2f}  |  Target: ${tp:.2f}\n"
        f"Score: {score}/100\n"
        f"Signals: {reason_str}",
        event="buy"
    )

def alert_sell(sym, qty, price, pct, reason):
    arrow = "UP" if pct > 0 else "DOWN"
    send(
        f"<b>SELL {sym} ({arrow})</b>\n"
        f"Qty: {qty} shares @ ${price:.2f}\n"
        f"P&L: {pct:+.2f}%\n"
        f"Reason: {reason}",
        event="sell"
    )

def alert_daily_loss(current_pnl):
    send(
        f"<b>DAILY LOSS LIMIT HIT</b>\n"
        f"Realized P&L: {current_pnl:+.2f}%\n"
        f"Bot has stopped trading for today.\n"
        f"All positions will be closed at 3:45 PM ET.",
        event="vix"
    )

def alert_vix(vix_level, action):
    send(
        f"<b>VIX ALERT — {action}</b>\n"
        f"VIX Level: {vix_level:.1f}",
        event="vix"
    )

def alert_regime(regime):
    send(f"<b>Market Regime: {regime.upper()}</b>", event="info")

def alert_eod(equity, pnl, trades, report_path):
    send(
        f"<b>END OF DAY</b>\n"
        f"Equity: ${equity:,.2f}\n"
        f"P&L: {pnl:+.2f}%\n"
        f"Trades: {trades}\n"
        f"Report: {os.path.basename(report_path)}",
        event="eod"
    )

def alert_startup(equity, bp, watchlist_count):
    send(
        f"<b>Trading Bot Started</b>\n"
        f"Equity: ${equity:,.2f}\n"
        f"Buying Power: ${bp:,.2f}\n"
        f"Watching {watchlist_count} stocks",
        event="startup"
    )
