# AlgoTrader — Workflows & Operations Guide

_Companion document to [ARCHITECTURE.md](./ARCHITECTURE.md). This document describes end-to-end workflows: how a user signs up and starts trading, how the bots operate day-to-day, and how to operate the system as an admin._

---

## Table of Contents

1. [End-User Workflows](#end-user-workflows)
   - [W1: First-time signup and Zerodha autonomous trading](#w1-first-time-signup--zerodha-autonomous-trading)
   - [W2: Daily Zerodha login (every morning)](#w2-daily-zerodha-login-every-morning)
   - [W3: Pause / resume autonomous trading](#w3-pause--resume-autonomous-trading)
   - [W4: Adjust allocation mid-session](#w4-adjust-allocation-mid-session)
   - [W5: Manual order placement](#w5-manual-order-placement)
   - [W6: Switch between brokers](#w6-switch-between-brokers)
   - [W7: View end-of-day reports](#w7-view-end-of-day-reports)
   - [W8: Set up Telegram notifications](#w8-set-up-telegram-notifications)
   - [W9: Disconnect a broker](#w9-disconnect-a-broker)
   - [W10: Change password](#w10-change-password)
2. [Bot Workflows](#bot-workflows)
   - [B1: Indian bot — full daily lifecycle](#b1-indian-bot--full-daily-lifecycle)
   - [B2: Alpaca bot — full daily lifecycle](#b2-alpaca-bot--full-daily-lifecycle)
   - [B3: Position entry decision tree](#b3-position-entry-decision-tree)
   - [B4: Position exit decision tree](#b4-position-exit-decision-tree)
   - [B5: Bot crash recovery](#b5-bot-crash-recovery)
3. [Admin / Ops Workflows](#admin--ops-workflows)
   - [A1: Deploy a code change](#a1-deploy-a-code-change)
   - [A2: Restart services safely](#a2-restart-services-safely)
   - [A3: Reset a user's password](#a3-reset-a-users-password)
   - [A4: Investigate a failing broker connection](#a4-investigate-a-failing-broker-connection)
   - [A5: Recover from corrupted credentials](#a5-recover-from-corrupted-credentials)
   - [A6: Add a new user](#a6-add-a-new-user)
   - [A7: Monitor system health](#a7-monitor-system-health)
   - [A8: Run smoke tests after deployment](#a8-run-smoke-tests-after-deployment)

---

# End-User Workflows

## W1: First-time signup → Zerodha autonomous trading

The full journey from "I don't have an account" to "the bot is trading on my behalf".

```
┌──────────────────────────────────────────────────────────────────────┐
│ STEP 1 — Sign up                                                     │
│ Visit https://your-domain  → click "Register"                        │
│ Enter email + password (min 8 chars, mixed case, digit)              │
│ → POST /api/register → user created, auto-login, session cookie set  │
└──────────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│ STEP 2 — Connect Zerodha (one-time)                                  │
│ Profile → Zerodha section                                            │
│ • Get API Key + Secret from https://developers.kite.trade            │
│   (create a new app, set redirect to                                 │
│    https://your-domain/api/zerodha/callback)                         │
│ • Paste API Key + API Secret in the form                             │
│ • Click Save                                                         │
│ → POST /api/zerodha/connect → encrypted creds saved → login_url ready│
└──────────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│ STEP 3 — Daily Kite OAuth                                            │
│ Click "Open Kite Login (daily)"                                      │
│ → Redirects to kite.trade login                                      │
│ • Login with Zerodha Client ID + password + PIN                      │
│ → Kite redirects back to /api/zerodha/callback?request_token=...     │
│ → Server exchanges request_token → access_token (valid until ~6 AM)  │
│ → Encrypted access_token saved → "SESSION ACTIVE" badge shown        │
└──────────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│ STEP 4 — Allocate funds                                              │
│ Sidebar → Overview (with Zerodha selected in account switcher)       │
│ Indian Bot Controls card → click "Edit Allocation"                   │
│ Set:                                                                 │
│   • Budget per trade        (e.g. ₹2 000)                            │
│   • Max simultaneous open   (e.g. 5)                                 │
│   • Stop-loss override %    (e.g. 1.5)                               │
│   • Take-profit override %  (e.g. 3.0)                               │
│ Click Save                                                           │
│ → POST /api/allocations/zerodha (validated 0–₹10 Cr / 0–50 / 0–50%)  │
└──────────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│ STEP 5 — Enable autonomous trading                                   │
│ Indian Bot Controls → flip Auto-Trade toggle to ON                   │
│ → POST /api/allocations/zerodha/toggle                               │
│ → DB: auto_trade = 1                                                 │
│ → Server runs: supervisorctl start indian-bot                        │
│ → Bot enters scan loop within 60 s                                   │
│ → KPI card "Bot Status" flips to ACTIVE (green)                      │
│ → Activity Log starts populating: "RELIANCE score=65 ['ema','rsi']"  │
└──────────────────────────────────────────────────────────────────────┘
                               │
                               ▼
                    Bot is now trading.
       Open positions, P&L, and trades update live in the UI.
```

**Time to first trade:** ~30 seconds after enabling Auto-Trade, assuming any stock crosses the entry threshold during a scan cycle.

---

## W2: Daily Zerodha login (every morning)

Zerodha access tokens expire daily at ~6:00 AM IST. Every trading day you'll need a fresh token.

```
Morning routine (~9:00 AM IST recommended):

1. Open https://your-domain
2. Sidebar → Profile → Zerodha section
3. Banner at top: "⚠️ Daily login required"
4. Click "Open Kite Login (daily)"
5. Complete Kite login (PIN/biometric)
6. Auto-redirect back → "SESSION ACTIVE" green badge
7. Done. Bot will resume trading automatically when market opens at 9:30 AM IST.
```

**If you forget:** the bot won't enter new positions but **will not crash**. It logs `Waiting on credentials: Zerodha access_token missing`. As soon as you re-login, it picks up on the next 60-s cycle. Existing positions opened the day before will still be managed (stops, TPs).

---

## W3: Pause / resume autonomous trading

```
Pause:
  Sidebar → Overview (Zerodha selected)
  Indian Bot Controls → Auto-Trade toggle → OFF
  → Bot stops opening new positions
  → Existing positions still managed (stops, TPs, EOD square-off)
  → Bot supervisor process is stopped (not killed — clean shutdown)

Resume:
  Auto-Trade toggle → ON
  → supervisorctl starts indian-bot
  → New positions can open within 60 s
```

**Emergency stop everything:** Use the **"Stop"** button in Indian Bot Controls (not the toggle). This calls `supervisorctl stop indian-bot` immediately, even if there are open positions. **Use only in emergencies** — open positions remain at the broker without bot supervision.

---

## W4: Adjust allocation mid-session

You can change budget/stops/TPs at any time. The bot re-reads allocation **every 60 s**, so changes take effect on the next scan cycle.

```
Overview → Indian Bot Controls → Edit Allocation
Change values → Save
→ POST /api/allocations/zerodha
→ DB updated
→ Bot picks up new values within 60 s

Existing open positions are NOT modified (their stops/TPs were set at entry).
Only new entries use the new allocation.
```

**Validation guards:**
- Negative values rejected (400)
- Budget capped at ₹10 Cr
- Max positions capped at 50
- Stop-loss capped at 50%
- Take-profit capped at 100%

---

## W5: Manual order placement

You can place orders manually via the **Zerodha tab** (bypasses the bot).

```
Sidebar → Zerodha (under Brokers)
Tab: "Place Order"
Fill: symbol, side (BUY/SELL), qty, order type (MARKET/LIMIT/SL/SL-M),
      product (MIS/CNC/NRML), price (if LIMIT), trigger (if SL)
Click Place
→ POST /api/zerodha/order
→ ZerodhaBroker.place_order(...)
→ Returns order_id
→ Toast: "Order placed: 240507000123456"

The bot will NOT manage manually-placed orders — it only tracks positions
opened through its own scan loop.
```

For Cancel: `Order book` tab → click ✕ next to the order.

---

## W6: Switch between brokers

The dashboard remembers your last-selected broker via `localStorage.sel_broker`.

```
Top-right account switcher (avatar dropdown)
→ Select broker (only connected brokers shown)
→ All tabs (Overview, Trades, History, Analytics, Reports) refresh
   to show that broker's data
→ Selection persists across page reloads and sessions
```

The dropdown also offers:
- ➕ **Connect new broker** → goes to Profile
- ⚙️ **Account settings** → goes to Profile
- 🚪 **Sign out** → POST /api/logout, redirects to login

---

## W7: View end-of-day reports

```
Alpaca user:
  Sidebar → Reports (Alpaca selected)
  Left panel: list of past EOD reports (date)
  Right panel: iframe showing the selected report (HTML, charts, trade list)
  Generated daily at market close by intraday_bot_v2.py

Zerodha / Angel One user:
  Sidebar → Reports
  Shows: KPI summary (total trades, win rate, P&L) + filterable trade history table
  No HTML report (Indian bot uses Telegram daily report instead)
```

---

## W8: Set up Telegram notifications

```
Step 1: Create a Telegram bot
  • Message @BotFather on Telegram → /newbot
  • Choose name & username → receive bot_token

Step 2: Get your chat_id
  • Start a chat with your new bot → say anything
  • Visit: https://api.telegram.org/bot<TOKEN>/getUpdates
  • Find: "chat":{"id":123456789}

Step 3: Configure in dashboard
  Profile → Telegram section
  • Paste bot_token
  • Paste chat_id
  • Choose events: BUY · SELL · EOD report · VIX alerts · Startup
  • Click Save & Test → "✓ Telegram message received"

Updates: edit settings without re-entering bot_token (server reuses stored).
Remove: click "Remove" → encrypted creds deleted.
```

The bots send alerts via `auth.get_telegram(user_id)` — per-user config.

---

## W9: Disconnect a broker

```
Profile → broker section → Disconnect
→ POST /api/<broker>/disconnect
→ Encrypted creds deleted from DB
→ Account switcher dropdown removes the broker
→ Aggregate overview shows "Not Connected" for that broker
```

For Zerodha: this also kills the daily access token. Reconnecting requires fresh API key + secret + Kite OAuth.

---

## W10: Change password

```
Profile → Security → Change Password
• Enter current password
• Enter new password (8+ chars, mixed case, digit)
• Click Change
→ POST /api/change_password
→ bcrypt verify current → if valid, hash new with cost=12
→ Session cookie cleared → user redirected to login
→ All other sessions remain valid until they expire (30 days)
   (force-revoke them via Profile → Sessions → Revoke)
```

---

# Bot Workflows

## B1: Indian bot — full daily lifecycle

```
00:00  IST  ──┐
              │  Bot is in wait loop
              │  (auto_trade may be OFF, or pre-market)
              │  Polls every 20s, sleeps cheaply
05:30  IST  ──┘
              │
06:00  IST  ──── Zerodha access_token expires
              │
              │  Bot detects: ZerodhaError("token expired")
              │  Logs: "Waiting on credentials..."
              │  Stays in wait loop
              │
~09:00 IST  ──── User performs daily Kite OAuth
              │  New access_token saved to DB
              │
09:15  IST  ──── NSE opens
              │
09:30  IST  ──── Bot entry window opens
              │  • Reads bot_fund_allocations
              │  • If auto_trade=1: enters main scan loop
              │
              │  Main loop (every 60s):
09:30─14:45   │  ┌─────────────────────────────────────┐
              │  │ 1. broker.get_positions()           │
              │  │ 2. check_exits_indian()             │
              │  │    • stop-loss / take-profit hit    │
              │  │    • EMA bearish exit               │
              │  │    • EOD time → square off          │
              │  │ 3. risk filters                     │
              │  │    • India VIX > 20 → reduce sizing │
              │  │    • India VIX > 25 → halt entries  │
              │  │    • daily P&L < -3% → halt         │
              │  │    • NIFTY below EMA-21 → no longs  │
              │  │ 4. score watchlist (48 NIFTY 50)    │
              │  │ 5. enter qualified candidates       │
              │  │    (score ≥ 60, sector cap, spread) │
              │  │ 6. persist indian_bot_state.json    │
              │  └─────────────────────────────────────┘
              │
14:45  IST  ──── Entry window closes (no new positions)
              │  Bot continues exit management only
              │
15:15  IST  ──── EOD square-off
              │  • Cancel all pending orders
              │  • broker.square_off_all_positions()
              │  • Send Telegram daily report
              │  • Reset daily counters
              │
15:30  IST  ──── NSE closes
              │
              │  Bot back to wait loop
              │  Idle until next morning's Kite re-login
              │
00:00  IST  ──── Day rolls over → loop continues
```

**Watchlist:** 48 NIFTY 50 stocks (HDFC removed post-merger).

**Dynamic instrument tokens:** On first scan of each symbol, calls `broker.get_ltp(["NSE:RELIANCE"])` to resolve the Zerodha instrument_token. Cached in-memory for the rest of the bot's uptime.

---

## B2: Alpaca bot — full daily lifecycle

```
00:00  ET  ──┐  Bot in idle loop, sleeping
             │
06:00  ET  ──── Pre-market scanner (separate process) starts
             │  Generates claude_picks.json every 15 min
             │
09:30  ET  ──── NYSE opens
             │
09:45  ET  ──── Bot entry window opens
             │  Main scan loop (every 60s)
             │
             │  Same structure as Indian bot, plus:
             │  • Earnings calendar check (skip if reporting today)
             │  • Negative news check (kill-switch)
             │  • PDT rule check (5+ day trades in 5 days)
             │  • Bracket orders (LIMIT entry + SL + TP atomic)
             │
14:45  ET  ──── Trailing stops activate (after 2% gain)
             │
15:20  ET  ──── No new entries
             │
15:45  ET  ──── EOD square-off + EOD HTML report generation
             │  • Generates trading_report_<date>.html
             │  • Updates equity_history.json
             │  • Sends Telegram report
             │
16:00  ET  ──── NYSE closes
             │
             │  Bot back to idle
```

---

## B3: Position entry decision tree

```
Symbol from watchlist
        │
        ▼
┌──────────────────────────────┐
│ Fetch 80 bars (5-min candles)│
│ < 25 bars? → score=0         │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│ EMA-9 / EMA-21 check         │
│ Bearish cross? → return 0    │
│ Bullish cross? → +25         │
│ Neutral? → +8                │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│ RSI(14)                      │
│ > 82 or < 30? → return 0     │
│ 45–75? → +15                 │
│ 35–45? → +5                  │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│ VWAP                         │
│ price > VWAP? → +15          │
│ price < VWAP? → -8           │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│ MACD(12,26,9)                │
│ Bullish (line > sig, hist>0)?│
│   → +10                      │
│ Bearish? → -5                │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│ Bollinger Bands              │
│ Lower third? → +10           │
│ Upper 12%?  → -10            │
└──────────────┬───────────────┘
               ▼
       Total score (0–100)
               │
               ▼
       score ≥ min_confidence (60)?
        │              │
        No             Yes
        │              ▼
        skip   ┌──────────────────────┐
               │ Sector cap reached?  │
               │ Yes → skip           │
               │ Liquidity OK?        │
               │ No → skip            │
               └─────────┬────────────┘
                         ▼
                ┌────────────────────────┐
                │ qty = budget / price   │
                │ Place limit order      │
                │ + bracket SL + TP      │
                └────────────────────────┘
```

---

## B4: Position exit decision tree

Run on every open position, every 60 s:

```
Position
   │
   ▼
┌─────────────────────────────────────┐
│ Negative news kill-switch?          │
│ Yes → market sell now, log REASON   │
└──────────────┬──────────────────────┘
               ▼
┌─────────────────────────────────────┐
│ Stop-loss hit?                      │
│ price ≤ stop_price → market sell    │
└──────────────┬──────────────────────┘
               ▼
┌─────────────────────────────────────┐
│ Take-profit hit?                    │
│ price ≥ tp_price → market sell      │
└──────────────┬──────────────────────┘
               ▼
┌─────────────────────────────────────┐
│ Partial profit-taking trigger?      │
│ pnl_pct ≥ 2% AND not yet partial?   │
│ → sell half, move stop to breakeven │
└──────────────┬──────────────────────┘
               ▼
┌─────────────────────────────────────┐
│ Trailing stop update?               │
│ new_high > prev_high?               │
│ → trail_stop = high × (1 - trail%)  │
└──────────────┬──────────────────────┘
               ▼
┌─────────────────────────────────────┐
│ EMA bearish crossover after entry?  │
│ → market sell                       │
└──────────────┬──────────────────────┘
               ▼
┌─────────────────────────────────────┐
│ EOD time? (15:15 IST / 15:45 ET)    │
│ → market sell all                   │
└─────────────────────────────────────┘
```

---

## B5: Bot crash recovery

The bots are configured for `autorestart=true` in supervisor. If a bot crashes:

```
Crash detected by supervisor
   │
   ▼
Wait `startsecs=5` then restart
   │
   ▼
Bot reads positions_state.json (or indian_positions_state.json)
   │
   ▼
Reconciles with broker positions:
   broker_positions = broker.get_positions()
   for each saved_position:
     if exists in broker → keep tracking
     if NOT in broker → drop (was closed externally or manually)
   for each broker_position not in saved:
     adopt with current entry (may be inaccurate but better than ignoring)
   │
   ▼
Resume normal scan loop
```

**Bracket orders** (Alpaca only) survive crashes natively — the SL+TP are held by Alpaca's exchange, not the bot. Indian bot uses **GTT triggers** for stop-loss persistence after crash.

---

# Admin / Ops Workflows

## A1: Deploy a code change

```
LOCAL:
  # edit code in c:\Users\devel\OneDrive\Desktop\stock market\git\trading-bot\
  scp <file> root@VPS:/opt/trading-bot/<file>

VPS:
  ssh root@VPS
  python3 -c "import ast; ast.parse(open('/opt/trading-bot/<file>').read()); print('ok')"
  supervisorctl restart <relevant-service>
  supervisorctl status

  # for HTML changes (templates/), no restart needed — Flask serves directly
```

**Which service to restart for which file:**

| File changed | Service |
|---|---|
| `api_server.py` | `dashboard` |
| `auth.py`, `db.py` | `dashboard` (and potentially bots if they use it) |
| `intraday_bot_v2.py` | `trading-bot` |
| `indian_bot.py` | `indian-bot` |
| `brokers/*.py` | `dashboard`, `trading-bot`, `indian-bot` (any user) |
| `local_scanner.py` | `scanner` |
| `templates/react_index.html` | none (live) |

## A2: Restart services safely

```
# Single service
supervisorctl restart dashboard

# All services
supervisorctl restart dashboard trading-bot indian-bot scanner

# Stop services with open positions:
# Alpaca bracket orders survive (held at exchange)
# Indian bot positions: ensure GTT stop-loss is set, OR
#   call /api/zerodha/squareoff first to flatten cleanly
```

## A3: Reset a user's password

When a user is locked out:

```python
# On VPS:
ssh root@VPS
cd /opt/trading-bot
python3 -c "
import sys; sys.path.insert(0, '.')
from dotenv import load_dotenv; load_dotenv()
import auth
u = auth.get_user_by_email('user@example.com')
auth.change_password(u['id'], 'NewSecurePass@123')
print('Password reset for', u['email'])
"
```

Then notify the user out-of-band (the password isn't sent automatically).

## A4: Investigate a failing broker connection

```
1. Check /api/me → does it show connected:true ?
2. Check /api/<broker>/session_status (Zerodha) or /account (others)
3. Check actual decryption:

   ssh root@VPS
   python3 -c "
   import sys; sys.path.insert(0, '/opt/trading-bot')
   from dotenv import load_dotenv; load_dotenv('/opt/trading-bot/.env')
   import auth
   creds = auth.get_zerodha_creds(USER_ID)
   print('decrypts ok:', creds is not None and bool(creds.get('api_key')))
   "

If creds is None or api_key is empty → encryption key mismatch (see A5)
If creds OK but API call fails → expired token / broker-side issue
```

## A5: Recover from corrupted credentials

If the encryption key was rotated (e.g., before the bulletproof loader was deployed) and stored creds can't decrypt:

```
ssh root@VPS
python3 -c "
import sqlite3
con = sqlite3.connect('/opt/trading-bot/users.db')
con.execute('DELETE FROM user_zerodha_creds WHERE user_id=USER_ID')
con.execute('DELETE FROM user_alpaca_creds WHERE user_id=USER_ID')
con.execute('DELETE FROM user_telegram WHERE user_id=USER_ID')
con.commit()
"
```

Then the user re-enters credentials via Profile (encrypted with current key).

**Prevention:** the v2026.05 bulletproof key loader prevents this from recurring — `auth.py` reads `.env` directly and never auto-rotates if a key already exists on disk.

## A6: Add a new user

```
# Self-service: user signs up at /register

# Admin-created:
ssh root@VPS
cd /opt/trading-bot
python3 -c "
import sys; sys.path.insert(0, '.')
from dotenv import load_dotenv; load_dotenv()
import auth
auth.create_user('new@example.com', 'TempPass@123', name='Jane Doe')
print('Created.')
"
```

User can change password after first login.

## A7: Monitor system health

```
# Quick check
curl -s http://localhost:5001/api/health | python3 -m json.tool

# All supervisor processes
supervisorctl status

# Indian bot live state
curl -s -H "Cookie: algotrader_session=$TOKEN" \
  http://localhost:5001/api/indian/state | python3 -m json.tool

# Recent dashboard errors
tail -50 /var/log/dashboard.err.log

# Recent indian-bot output
tail -50 /var/log/supervisor/indian-bot.out.log

# Audit log (last 50 events)
sqlite3 /opt/trading-bot/users.db \
  "SELECT created_at, event, ip FROM audit_log ORDER BY id DESC LIMIT 50"
```

## A8: Run smoke tests after deployment

```bash
# /tmp/smoke.sh on VPS — full end-to-end auth + endpoint sweep:

#!/bin/bash
BASE='http://localhost:5001'
EMAIL='developer.akash1043@gmail.com'
PW='Admin@123'

TOKEN=$(curl -s -i -X POST $BASE/api/login \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PW\"}" \
  | grep -i 'set-cookie' \
  | sed -E 's/.*algotrader_session=([^;]+);.*/\1/' | tr -d '\r\n')

if [ -z "$TOKEN" ]; then
  echo "❌ Login failed — server may be down"
  exit 1
fi

COOKIE="algotrader_session=$TOKEN"

# Critical endpoints — every one MUST return 200
for ep in /api/me /api/health /api/aggregate/overview \
          /api/data /api/history /api/reports /api/audit /api/sessions \
          /api/allocations /api/allocations/zerodha \
          /api/zerodha/session_status /api/indian/state \
          /api/trades/combined /api/equity; do
  S=$(curl -s -o /dev/null -w '%{http_code}' -H "Cookie: $COOKIE" "$BASE$ep")
  if [ "$S" = "200" ]; then
    printf '✓ %s\n' "$ep"
  else
    printf '✗ %s [%s]\n' "$ep" "$S"
  fi
done

echo ""
echo "Services:"
supervisorctl status | grep -E "RUNNING|STOPPED"
```

Run after every deployment. **Expected output:** all `✓` and all services `RUNNING` (except `indian-bot` if no Indian broker has Auto-Trade enabled).

---

## Common Diagnostic Commands

```bash
# Show what's running on each port
ss -tlnp | grep python

# Show last bot scan
tail -f /var/log/supervisor/indian-bot.out.log

# Watch dashboard requests live
tail -f /var/log/dashboard.log

# Check encryption key matches between .env and what auth uses
ssh root@VPS 'cd /opt/trading-bot && python3 -c "
from dotenv import load_dotenv; load_dotenv()
import os, sys; sys.path.insert(0, \".\")
import auth
print(\"env key:\", os.getenv(\"MASTER_ENCRYPTION_KEY\")[:16])
print(\"auth key:\", auth._MASTER_KEY[:16])
print(\"match:\", os.getenv(\"MASTER_ENCRYPTION_KEY\") == auth._MASTER_KEY)
"'

# Force daily token refresh check
ssh root@VPS 'cd /opt/trading-bot && python3 -c "
from dotenv import load_dotenv; load_dotenv()
import sys; sys.path.insert(0, \".\")
import auth
from brokers.zerodha import ZerodhaBroker
c = auth.get_zerodha_creds(USER_ID)
b = ZerodhaBroker(c[\"api_key\"], c[\"api_secret\"], c[\"access_token\"])
print(b.get_profile())
"'
```

---

## Glossary

| Term | Meaning |
|---|---|
| **MIS** | Margin Intraday Square-off (Indian intraday product) |
| **CNC** | Cash and Carry (Indian delivery product) |
| **NRML** | Normal F&O carry-forward |
| **GTT** | Good Till Triggered (Zerodha persistent stop-loss) |
| **TOTP** | Time-based One-Time Password (Angel One auth) |
| **Bracket order** | Atomic entry + SL + TP order (Alpaca) |
| **Square-off** | Close all open positions at market |
| **Auto-Trade** | DB flag controlling whether bot opens new positions |
| **Allocation** | Per-broker config: budget, max_positions, stops, TPs |
| **Watchlist** | Symbols the bot scans every cycle for signals |
| **Score** | 0–100 confidence from indicator confluence |
| **EOD** | End of Day — typically 15:15 IST / 15:45 ET |
| **VIX** | Volatility index — used as a global risk gate |
