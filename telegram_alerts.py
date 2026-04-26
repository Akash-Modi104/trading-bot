"""
Telegram Trade Alert Module
Setup: message @BotFather on Telegram → /newbot → copy token
       then message @userinfobot to get your chat_id
       update alpaca_config.json with both values and set enabled: true
"""

import requests
import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Load .env file
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

def send(message: str):
    """Send a message to Telegram. Silently skips if not configured."""
    enabled = os.environ.get("TELEGRAM_ENABLED", "false").lower() == "true"
    if not enabled:
        return
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=5
        )
    except Exception:
        pass

def alert_buy(sym, qty, price, stop, tp, score, reasons):
    reason_str = " | ".join(k for k in reasons if k not in ("error","claude_reason"))
    send(
        f"<b>🟢 BUY {sym}</b>\n"
        f"Qty: {qty} shares @ ${price:.2f}\n"
        f"Stop: ${stop:.2f}  |  Target: ${tp:.2f}\n"
        f"Score: {score}/100\n"
        f"Signals: {reason_str}"
    )

def alert_sell(sym, qty, price, pct, reason):
    emoji = "✅" if pct > 0 else "🔴"
    send(
        f"<b>{emoji} SELL {sym}</b>\n"
        f"Qty: {qty} shares @ ${price:.2f}\n"
        f"P&L: {pct:+.2f}%\n"
        f"Reason: {reason}"
    )

def alert_daily_loss(current_pnl):
    send(
        f"<b>⛔ DAILY LOSS LIMIT HIT</b>\n"
        f"Realized P&L: {current_pnl:+.2f}%\n"
        f"Bot has stopped trading for today.\n"
        f"All positions will be closed at 3:45 PM ET."
    )

def alert_vix(vix_level, action):
    send(
        f"<b>⚠️ VIX ALERT — {action}</b>\n"
        f"VIX Level: {vix_level:.1f}\n"
        f"{'Reducing position sizes 50%' if action == 'REDUCE' else 'Trading PAUSED for safety'}"
    )

def alert_regime(regime):
    emoji = {"trending": "📈", "choppy": "↔️", "bearish": "📉"}.get(regime, "❓")
    send(f"<b>{emoji} Market Regime: {regime.upper()}</b>")

def alert_eod(equity, pnl, trades, report_path):
    send(
        f"<b>📊 END OF DAY SUMMARY</b>\n"
        f"Account Equity: ${equity:,.2f}\n"
        f"Today's P&L: {pnl:+.2f}%\n"
        f"Trades Executed: {trades}\n"
        f"Report: {os.path.basename(report_path)}"
    )

def alert_startup(equity, bp, watchlist_count):
    send(
        f"<b>🚀 Trading Bot Started</b>\n"
        f"Equity: ${equity:,.2f}\n"
        f"Buying Power: ${bp:,.2f}\n"
        f"Watching {watchlist_count} stocks"
    )
