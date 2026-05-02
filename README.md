# trading-bot

Automated intraday trading dashboard with multi-broker support (Alpaca, Angel One, Zerodha).

## What it does

- **US markets** — `intraday_bot_v2.py` trades Alpaca paper/live accounts on NYSE/NASDAQ. Strategy: EMA crossover + RSI + VWAP + MACD + Bollinger Bands + ATR-scaled sizing.
- **Indian markets** — `indian_bot.py` trades NSE via Angel One or Zerodha. Same strategy logic adapted for IST timing (09:15–15:30) and ₹ sizing.
- **Dashboard** — React single-page app served by Flask at port 5001. Real-time P&L, positions, trade history across all brokers, analytics, and strategy controls.

## Architecture

```
api_server.py        Flask backend — REST API + SSE push
intraday_bot_v2.py   US intraday bot (Alpaca)
indian_bot.py        Indian intraday bot (Angel One / Zerodha)
local_scanner.py     AI stock scanner (Claude picks)
eod_report.py        End-of-day HTML report generator
brokers/
  base.py            Abstract BaseBroker interface
  alpaca_broker.py   Alpaca REST implementation
  angelone.py        Angel One SmartAPI implementation
  zerodha.py         Zerodha Kite Connect v3 implementation
templates/
  react_index.html   React dashboard (in-browser Babel, no build step)
  login.html         Auth pages
```

## Setup

```bash
pip install -r requirements.txt

# Copy and fill credentials
cp .env.example .env
nano .env
```

Required `.env` keys:

```
# Alpaca (US bot)
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets/v2

# Indian bot (choose one)
BROKER=angelone
AO_API_KEY=...
AO_CLIENT_ID=...
AO_PASSWORD=...
AO_TOTP_SECRET=...

# OR Zerodha
# BROKER=zerodha
# ZRD_API_KEY=...
# ZRD_API_SECRET=...
# ZRD_ACCESS_TOKEN=...   # refresh daily

# Dashboard
FLASK_SECRET_KEY=<random 32 chars>
ADMIN_EMAIL=you@example.com
ADMIN_PASSWORD=<strong password>

# Telegram alerts (optional)
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

## Running

```bash
# Dashboard
python api_server.py

# US bot
python intraday_bot_v2.py

# Indian bot
BROKER=angelone python indian_bot.py

# AI scanner
python local_scanner.py
```

On the VPS, all four processes are managed by **Supervisor**. See `supervisor/*.conf`.

## Bot strategy

The trading signal is a composite score (0–100):

| Signal | Points |
|---|---|
| EMA 9/21 bullish crossover (5-min) | +25 |
| Multi-timeframe confluence (1m+15m) | +30 |
| RSI 45–75 | +15 |
| Price above VWAP | +15 |
| ORB breakout | +15 |
| Relative volume > 1.2× | +10 |
| MACD histogram positive | +10 |
| Bollinger lower-third | +10 |

Trades execute when score >= 45 (configurable). Position sizing is ATR-scaled. Exits: trailing stop, take-profit, EMA bearish crossover, or 15:45 ET EOD (US) / 15:15 IST (India).

Risk controls: daily loss limit, VIX pause/stop (US), consecutive-loss circuit breaker, 7-day drawdown stop, PDT guard, sector cap.

## API endpoints

| Endpoint | Description |
|---|---|
| `GET /api/data` | Live bot state snapshot |
| `GET /api/history?range=week` | Trade history with daily/monthly P&L |
| `GET /api/trades/combined?broker=all` | Merged trades from all brokers |
| `GET /api/analytics` | Sharpe, Sortino, win rate, signal attribution |
| `GET /api/aggregate/overview` | Parallel account summary across all brokers |
| `POST /api/admin/panic_flat` | Emergency close all positions on all brokers |
| `GET /api/export/combined.csv` | Download all trades as CSV |
| `GET /api/angelone/positions` | Live Angel One positions |
| `GET /api/zerodha/positions` | Live Zerodha positions |

## Dashboard tabs

- **Overview** — live metrics, open positions (% of equity), equity curve
- **Trades** — today's Alpaca trades with CSV export
- **Combined** — trades from all brokers (filterable by range and broker)
- **History** — daily/monthly P&L history
- **Analytics** — Sharpe, Sortino, max drawdown, signal attribution
- **Angel One** — positions, holdings, orders, manual order placement
- **Zerodha** — positions, holdings, orders, manual order placement
- **Reports** — EOD HTML reports
- **Strategy** — live strategy parameter editing
- **System** — service health, VPS status
- **Profile** — broker credentials, Telegram setup, session management

## Disclaimer

This software is for educational purposes. Automated trading carries significant financial risk. Past performance does not guarantee future results. Use at your own risk.
