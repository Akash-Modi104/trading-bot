# AlgoTrader — System Architecture

## Overview

AlgoTrader is a full-stack intraday trading platform that supports **three brokers**:
- **Alpaca** — US equities (NASDAQ / NYSE), paper + live trading
- **Angel One** — Indian equities (NSE / BSE / NFO / MCX), via SmartAPI + TOTP
- **Zerodha** — Indian equities (NSE / BSE / NFO / MCX / CDS), via Kite Connect OAuth

The system is composed of four major layers:

```
┌──────────────────────────────────────────────────────────────────┐
│                    WEB DASHBOARD  (React SPA)                    │
│   Login · Register · Profile · Alpaca/Angel One/Zerodha Connect  │
│   Overview · Trades · History · Analytics · Reports · Strategy   │
│   Angel One Tab (positions, holdings, orders, place/cancel)      │
│   Zerodha Tab   (positions, holdings, orders, place/cancel)      │
└─────────────────────────┬────────────────────────────────────────┘
                          │ HTTPS REST + SSE
┌─────────────────────────▼────────────────────────────────────────┐
│               API SERVER  (Flask — api_server.py)                │
│  Auth: bcrypt passwords · session cookies · rate limiting        │
│  Broker endpoints: /api/alpaca/*, /api/angelone/*, /api/zerodha/*│
│  Dashboard data:  /api/data · /api/stream (SSE) · /api/history   │
│  Bot control:     /api/action (start/stop/restart/close_all)     │
└───────┬───────────────────────┬──────────────────────────────────┘
        │                       │
┌───────▼──────────┐   ┌────────▼───────────────────────────────┐
│  SQLite Database │   │       JSON State Files                 │
│  (users.db)      │   │  bot_state.json   — live bot snapshot  │
│  users           │   │  trade_log.json   — all trades ever    │
│  user_sessions   │   │  strategy_params.json — live config    │
│  user_alpaca_creds│   │  positions_state.json — open stops     │
│  user_angelone_creds│  │  claude_picks.json  — AI picks        │
│  user_zerodha_creds │  │  equity_history.json — daily equity   │
│  audit_log       │   │  negative_news.json — kill-switch list │
│  login_attempts  │   └────────────────────────────────────────┘
└──────────────────┘
        │
┌───────▼──────────────────────────────────────────────────────────┐
│            TRADING BOT ENGINE  (intraday_bot_v2.py)              │
│  Runs as a supervisor service — 60-second main loop              │
│  US stocks only (Alpaca) — 9:45 AM – 3:20 PM ET                 │
│  Strategy: EMA/RSI/VWAP/ORB/MACD/Bollinger + AI scanner picks   │
│  Risk: ATR sizing · VIX filter · SPY trend · sector cap · PDT   │
│  Orders: bracket (SL+TP atomic) · trailing stop · partial TP    │
└──────────┬───────────────────────────────────────────────────────┘
           │
┌──────────▼─────────────────────────────────────────────────────────┐
│                     BROKER ADAPTERS  (brokers/)                    │
│  brokers/angelone.py — Angel One SmartAPI (REST + pyotp TOTP)      │
│  brokers/zerodha.py  — Zerodha Kite Connect v3 (OAuth)             │
│  (Alpaca calls are inline in bot + api_server — no separate class) │
└────────────────────────────────────────────────────────────────────┘
```

---

## File-by-File Reference

### `api_server.py` — Flask REST API (≈ 1 100 lines)

The single-process web server. Runs on port 5001 (configurable via `PORT` env var).

| Endpoint group | Path prefix | Purpose |
|---|---|---|
| Auth | `/api/login`, `/api/register`, `/api/logout` | Session-based authentication |
| Profile | `/api/me`, `/api/profile`, `/api/change_password` | User account management |
| Alpaca | `/api/alpaca/connect`, `/api/alpaca/disconnect` | Connect US broker |
| Angel One | `/api/angelone/*` | Full Angel One broker management |
| Zerodha | `/api/zerodha/*` | Full Zerodha broker management |
| Dashboard | `/api/data`, `/api/stream` | Bot state (polling + SSE) |
| History | `/api/history` | Filterable trade history |
| Analytics | `/api/analytics` | Sharpe, Sortino, signal attribution |
| Reports | `/api/reports`, `/api/reports/<file>` | EOD HTML reports |
| Config | `/api/config` | Read/write strategy params |
| Actions | `/api/action` | Start/stop/restart bot, close all |
| Misc | `/api/equity`, `/api/scan_stats`, `/api/export.csv` | Equity curve, scan stats, CSV |

### `auth.py` — Authentication & Credential Management (≈ 360 lines)

Handles passwords (bcrypt), session tokens, and **encrypted broker credentials** for all three brokers using Fernet symmetric encryption. The master key is auto-generated on first run and appended to `.env`.

**Credential flow for each broker:**

| Broker | Tables | Key fields |
|---|---|---|
| Alpaca | `user_alpaca_creds` | api_key, secret_key (both Fernet-encrypted) |
| Angel One | `user_angelone_creds` | api_key, client_id, password, totp_secret, jwt_token, refresh_token |
| Zerodha | `user_zerodha_creds` | api_key, api_secret, access_token, request_token |

### `db.py` — SQLite Layer (≈ 120 lines)

Thin wrapper around SQLite. All tables:

```
users                — accounts (email, password_hash, theme, plan, role)
user_sessions        — auth tokens (30-day expiry, per IP/UA)
user_alpaca_creds    — encrypted Alpaca API key + secret
user_angelone_creds  — encrypted Angel One credentials + JWT cache
user_zerodha_creds   — encrypted Zerodha API key + access token
audit_log            — security event trail (login, connect, orders)
login_attempts       — rate-limit tracking (5 fails = 15-min block)
```

### `brokers/angelone.py` — Angel One SmartAPI Client (≈ 300 lines)

Full stateful client for [Angel One SmartAPI](https://smartapi.angelbroking.com/docs).

**Authentication:**
- Requires: `api_key`, `client_id`, `password` (MPIN), `totp_secret` (base-32)
- `pyotp.TOTP(totp_secret).now()` generates the one-time password at login time
- Returns `jwtToken` + `refreshToken` (cached, auto-refreshed after 23 h)

**Key methods:**

| Method | Description |
|---|---|
| `login()` | Generate JWT session with TOTP |
| `refresh_tokens()` | Refresh without TOTP |
| `place_order(...)` | MARKET / LIMIT / STOPLOSS orders |
| `place_bracket_order(...)` | ROBO variety (entry + SL + target atomic) |
| `place_cover_order(...)` | CO variety (entry + exchange-level SL) |
| `modify_order(...)` | Change qty/price on pending order |
| `cancel_order(order_id)` | Cancel pending order |
| `get_order_book()` | All today's orders |
| `get_trade_book()` | Executed trades |
| `get_positions()` | Open intraday positions |
| `get_holdings()` | Long-term CNC portfolio |
| `get_funds()` | Cash balance, used margin, M2M P&L |
| `get_profile()` | Account profile |
| `square_off_all_positions()` | Close all open positions at market |
| `get_ltp(exchange, symbol, token)` | Last traded price |
| `get_quote(exchange, symbol, token)` | Full quote (bid/ask/OHLC/volume) |
| `get_candles(exchange, token, interval, from, to)` | OHLCV historical data |
| `search_symbol(exchange, query)` | Find symbol + token by name |

**Order types:** `MARKET`, `LIMIT`, `STOPLOSS_LIMIT`, `STOPLOSS_MARKET`  
**Product types:** `INTRADAY`, `DELIVERY`, `MARGIN`, `CARRYFORWARD`, `BO`, `CO`  
**Exchanges:** `NSE`, `BSE`, `NFO`, `MCX`  
**Varieties:** `NORMAL`, `STOPLOSS`, `AMO`, `ROBO`

### `brokers/zerodha.py` — Zerodha Kite Connect v3 Client (≈ 280 lines)

Full client for [Zerodha Kite Connect v3](https://kite.trade/docs/connect/v3/).

**Authentication (OAuth, daily):**
1. Direct user to `login_url()` → `https://kite.trade/connect/login?api_key=...`
2. After login, Kite redirects back with `?request_token=abc123`
3. Call `generate_session(request_token)` → receives `access_token`
4. Access token is valid until ~6 AM IST next day

**Key methods:**

| Method | Description |
|---|---|
| `login_url()` | Returns Kite OAuth URL |
| `generate_session(request_token)` | Exchange for access_token |
| `invalidate_session()` | Logout / revoke token |
| `place_order(...)` | MARKET / LIMIT / SL / SL-M orders |
| `modify_order(order_id, ...)` | Modify pending order |
| `cancel_order(order_id, variety)` | Cancel order |
| `get_orders()` | All today's orders |
| `get_trades()` | Executed fills |
| `get_order_trades(order_id)` | Fills for specific order |
| `get_positions()` | Returns `{day: [...], net: [...]}` |
| `get_holdings()` | Long-term CNC portfolio |
| `convert_position(...)` | MIS ↔ CNC / NRML conversion |
| `square_off_all_positions()` | Close all MIS positions at market |
| `get_funds()` | Equity + commodity margins |
| `get_profile()` | Account profile |
| `get_quote(instruments)` | Full quote for list of instruments |
| `get_ltp(instruments)` | Last traded price |
| `get_ohlc(instruments)` | OHLC data |
| `get_candles(token, interval, from, to)` | Historical OHLCV candles |
| `get_instruments(exchange)` | Full instrument master (CSV) |
| `search_instruments(query, exchange)` | Filter instruments by name |

**Order types:** `MARKET`, `LIMIT`, `SL`, `SL-M`  
**Product types:** `MIS` (intraday), `CNC` (delivery), `NRML` (F&O overnight)  
**Varieties:** `regular`, `amo`, `co` (cover order), `bo` (bracket order, auto-promoted)  
**Exchanges:** `NSE`, `BSE`, `NFO`, `BFO`, `MCX`, `CDS`

### `intraday_bot_v2.py` — Autonomous Trading Bot (≈ 1 150 lines)

Runs as a separate supervisor process. Trades **US equities only** via Alpaca.

**Main loop (every 60 s, 9:45 AM – 3:20 PM ET):**

```
1. Fetch Alpaca positions → check exits on each open position
   ├─ News kill-switch (negative_news.json)
   ├─ Partial profit-taking (sell half at +2%, move stop to breakeven)
   ├─ Stop loss / take profit hit
   ├─ EMA bearish crossover signal exit
   └─ Trailing stop update

2. Market-wide risk filters (skip entries if triggered)
   ├─ VIX > 35 → stop all trading
   ├─ VIX > 28 → reduce position size 50%
   ├─ 7-day rolling drawdown > 8% → pause entries
   ├─ 3+ consecutive losses → 30-min cooldown
   ├─ Daily P&L < –3% → pause entries
   └─ SPY below EMA-21 → skip long entries

3. Score watchlist candidates (EMA/RSI/VWAP/ORB/MACD/BBands + AI picks)
   Each stock gets a 0–100 score; only scores ≥ min_confidence (default 60) proceed

4. For qualified candidates:
   ├─ Check earnings calendar (skip if reporting today)
   ├─ Check negative news (skip if flagged)
   ├─ Sector cap (max 1 position per sector)
   ├─ Spread check (reject illiquid stocks)
   ├─ ATR-scaled position sizing (VIX-adjusted)
   └─ Place bracket order (LIMIT entry + SL + TP, atomically attached)

5. Persist state → bot_state.json (read by dashboard every 5 s via SSE)
6. EOD at 3:45 PM: close all positions, send Telegram report
```

**Risk layers:**

| Layer | Mechanism |
|---|---|
| Per-trade | ATR-scaled qty, spread filter, limit order (not market) |
| Portfolio | Max 3 positions, max 1 per sector |
| Daily | –3% P&L pause, 3-loss consecutive cooldown |
| Market | VIX 28/35 thresholds, SPY EMA-21 filter, 8% drawdown pause |
| Position | Trailing stop, partial profit-taking, news kill-switch |
| Crash safety | Bracket orders survive bot restart (exchange holds SL+TP) |

### `templates/react_index.html` — Dashboard SPA (React in-browser)

Single-file React app (no build step — Babel in-browser transpile).

**Tabs:**

| Tab | Content |
|---|---|
| 📊 Overview | Metrics, positions, P&L chart, picks, trades, activity log, controls |
| 📋 Trades | Full trade list for today |
| 📅 History | Daily/monthly P&L, date range filter |
| 📈 Analytics | Sharpe, Sortino, max drawdown, signal attribution, slippage |
| 🏦 Angel One | Account summary · Positions · Holdings · Order book · Place/cancel orders · Symbol search |
| 📈 Zerodha | Account summary · Positions · Holdings · Order book · Place/cancel orders · Daily OAuth flow |
| 📄 Reports | EOD HTML report viewer |
| ⚙️ Strategy | Live strategy parameter editor |
| 💻 System | Service health (supervisor) + Ollama status |
| 👤 Profile | Account settings · Alpaca / Angel One / Zerodha connect/disconnect · Security |

---

## Authentication & Security

```
1. Passwords hashed with bcrypt (cost=12)
2. Session tokens: 32-byte URL-safe random, stored in HttpOnly cookie (30-day expiry)
3. Broker credentials encrypted with Fernet (AES-128-CBC + HMAC-SHA256)
   - Master key auto-generated on first run, stored in .env as MASTER_ENCRYPTION_KEY
   - All API keys / secrets / passwords / TOTP secrets stored as encrypted BLOBs in SQLite
4. Rate limiting: 5 failed logins per IP per 15 min → 429
5. CSRF protection: SameSite=Lax cookie, no CSRF token needed for same-origin
6. Security headers: X-Content-Type-Options, X-Frame-Options, CSP, Referrer-Policy
7. Audit log: every login, broker connect/disconnect, order placed, password change
```

---

## Broker Authentication Flows

### Angel One (one-time setup)

```
User → Profile tab → enters API key + client_id + MPIN + TOTP secret
  → POST /api/angelone/connect
  → server calls AngelOneBroker.login() using pyotp.TOTP(secret).now()
  → receives jwtToken + refreshToken
  → saves all credentials + tokens encrypted in user_angelone_creds
  → subsequent calls use cached JWT; auto-refreshes after 23 h
```

### Zerodha (daily OAuth)

```
Day 0 (one-time):
  User → Profile tab → enters API key + API secret
  → POST /api/zerodha/connect → credentials saved → login_url returned

Every day:
  User → Profile tab → clicks "Open Kite Login" link → Kite OAuth in new tab
  → after login, redirect URL contains ?request_token=abc123
  → user copies token → pastes in Profile tab → clicks Activate
  → POST /api/zerodha/session → server calls ZerodhaBroker.generate_session()
  → receives access_token → saved encrypted → valid until ~6 AM IST next day
```

---

## Data Flow: Order Placement (Angel One / Zerodha)

```
Dashboard (React)
  ↓  POST /api/angelone/order  or  /api/zerodha/order
  
API Server
  ↓  _get_angelone_broker(user_id)  /  _get_zerodha_broker(user_id)
      └─ load encrypted creds from SQLite
      └─ build broker instance with decrypted credentials
  ↓  broker.place_order(symbol, side, qty, price, order_type, ...)
  
Broker class  (brokers/angelone.py  or  brokers/zerodha.py)
  ↓  HTTPS POST to broker REST API
      Angel One: https://apiconnect.angelbroking.com/rest/secure/.../placeOrder
      Zerodha:   https://api.kite.trade/orders/{variety}
  
Broker API  (Angel One SmartAPI  /  Zerodha Kite Connect v3)
  ↓  returns order_id
  
API Server  → _persist_angelone_tokens()  (save refreshed JWT if needed)
           → audit log entry
           → return {ok: true, order_id: "..."}  to dashboard
```

---

## Configuration Files

| File | Purpose | Hot-reload? |
|---|---|---|
| `.env` | API keys, Telegram token, master encryption key | On restart |
| `alpaca_config.json` | Risk parameters for Alpaca bot | On restart |
| `strategy_params.json` | Live strategy tuning (editable from dashboard) | Every cycle |
| `bot_state.json` | Bot writes every 60 s, dashboard reads every 5 s | Live |
| `trade_log.json` | Append-only trade history | Live |
| `positions_state.json` | Trailing stops, partial-TP state | Live |
| `claude_picks.json` | AI scanner output (updated every 15 min pre-market) | Live |
| `negative_news.json` | Kill-switch list (updated by scanner) | Live |
| `equity_history.json` | Daily equity snapshots (180-day rolling) | Daily |

---

## Deployment

The application runs on a Linux VPS managed by **Supervisor**.

```
Supervisor processes:
  dashboard     → python api_server.py          (port 5001)
  trading-bot   → python intraday_bot_v2.py     (background)
  scanner       → python local_scanner.py        (pre-market)

Nginx reverse proxy:
  HTTPS → 443  →  localhost:5001
  HTTP  → 80   →  301 redirect to HTTPS

SSL: Let's Encrypt (auto-renewed via certbot)
```

**Requirements:**

```
Python 3.10+
Flask 2.3 · Flask-Cors 4.0
requests · numpy · pandas · pytz
python-dotenv · schedule
bcrypt · cryptography   (auth + encryption)
pyotp                   (Angel One TOTP)
ddgs · beautifulsoup4   (news scanner)
matplotlib · ollama     (EOD reports + AI scanner)
```

Install: `pip install -r requirements.txt`

---

## Adding a New Broker (Extension Guide)

1. **Create `brokers/<name>.py`** — implement:
   - `login()` / `generate_session()`
   - `place_order(symbol, side, qty, price, order_type, ...)`
   - `cancel_order(order_id)` / `modify_order(...)`
   - `get_positions()` / `get_holdings()` / `get_orders()`
   - `get_funds()` / `get_profile()`
   - `square_off_all_positions()`

2. **Add DB table** in `db.py` SCHEMA:
   ```sql
   CREATE TABLE IF NOT EXISTS user_<broker>_creds ( ... )
   ```

3. **Add auth functions** in `auth.py`:
   - `save_<broker>_creds()`, `get_<broker>_creds()`, `get_<broker>_status()`, `delete_<broker>_creds()`

4. **Add API endpoints** in `api_server.py`:
   - `/api/<broker>/connect` · `/disconnect` · `/account` · `/positions` · `/holdings`
   - `/api/<broker>/order` (POST=place, PUT=modify, DELETE=cancel)
   - `/api/<broker>/squareoff`

5. **Add dashboard tab** in `react_index.html`:
   - Add `{ id:'<broker>', label:'...' }` to the TABS array
   - Create a `<BrokerTab me={me} />` component
   - Add connect/disconnect form to `ProfileTab`
   - Add `{tab === '<broker>' && <BrokerTab me={me} />}` in the render
