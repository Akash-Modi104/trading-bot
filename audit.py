#!/usr/bin/env python3
"""
End-to-end system audit. Tests every layer for a client-demo-ready report.
Usage: audit.py
"""
import os, json, sys, subprocess, requests, sqlite3
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))
DB = os.path.join(BASE, "users.db")
DASH = "http://127.0.0.1:5001"

results = []
def chk(category, label, ok, detail=""):
    results.append((category, label, ok, str(detail)[:120]))

# ─── Process / supervisor ─────────────────────────────────────
try:
    out = subprocess.check_output(["supervisorctl","status"], text=True, timeout=10)
    for line in out.strip().splitlines():
        parts = line.split()
        if len(parts) >= 2:
            chk("PROCESS", f"supervisor:{parts[0]}", parts[1] == "RUNNING", parts[1])
except Exception as e:
    chk("PROCESS", "supervisorctl", False, e)

# ─── Dashboard reachable ──────────────────────────────────────
try:
    r = requests.get(f"{DASH}/api/health", timeout=5)
    chk("DASHBOARD", "dashboard:reachable", r.status_code in (200,401), f"HTTP {r.status_code}")
except Exception as e:
    chk("DASHBOARD", "dashboard:reachable", False, e)

# ─── Database schema ──────────────────────────────────────────
try:
    con = sqlite3.connect(DB, timeout=5)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type=\"table\"")}
    expected = {"users","user_alpaca_creds","user_angelone_creds","user_zerodha_creds",
                "user_telegram","user_sessions","audit_log"}
    for t in expected:
        chk("DB", f"table:{t}", t in tables, "")
    duplicates = {"user_alpaca_paper","user_alpaca_live","user_angelone"}.intersection(tables)
    chk("DB", "no_duplicate_tables", not duplicates,
        f"unused legacy tables: {sorted(duplicates)}" if duplicates else "")
    chk("DB", "users_count", True, f"{con.execute('SELECT COUNT(*) FROM users').fetchone()[0]} users")
    chk("DB", "alpaca_creds_count", True,
        f"{con.execute('SELECT COUNT(*) FROM user_alpaca_creds').fetchone()[0]} configured")
    chk("DB", "angelone_creds_count", True,
        f"{con.execute('SELECT COUNT(*) FROM user_angelone_creds').fetchone()[0]} configured")
    chk("DB", "zerodha_creds_count", True,
        f"{con.execute('SELECT COUNT(*) FROM user_zerodha_creds').fetchone()[0]} configured")
    con.close()
except Exception as e:
    chk("DB", "schema", False, e)

# ─── Alpaca live API (the one the bot uses via .env) ──────────
KEY = os.environ.get("ALPACA_API_KEY","")
SEC = os.environ.get("ALPACA_SECRET_KEY","")
URL = os.environ.get("ALPACA_BASE_URL","").rstrip("/")
HDR = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC}
pos_list = []
try:
    r = requests.get(f"{URL}/account", headers=HDR, timeout=10)
    chk("ALPACA", "auth", r.status_code == 200, f"HTTP {r.status_code}")
    if r.status_code == 200:
        a = r.json()
        chk("ALPACA", "account_active", a.get("status") == "ACTIVE", a.get("status",""))
        chk("ALPACA", "trading_not_blocked", not a.get("trading_blocked"), a.get("trading_blocked"))
        chk("ALPACA", "is_paper", "paper" in URL.lower(), URL)
        chk("ALPACA", "equity_present", float(a.get("equity",0)) > 0, f"${a.get('equity')}")
    pos_list = requests.get(f"{URL}/positions", headers=HDR, timeout=10).json()
    chk("ALPACA", "positions_endpoint", isinstance(pos_list, list),
        f"{len(pos_list)} positions" if isinstance(pos_list,list) else "")
except Exception as e:
    chk("ALPACA", "alpaca", False, e)

# ─── Endpoint surface area (unauthenticated probe) ─────────────
endpoints = [
    ("/api/health", "GET", 200),
    ("/api/me", "GET", 401),
    ("/api/alpaca/connect", "POST", 401),
    ("/api/angelone/connect", "POST", 401),
    ("/api/angelone/positions", "GET", 401),
    ("/api/angelone/holdings", "GET", 401),
    ("/api/angelone/orders", "GET", 401),
    ("/api/angelone/trades", "GET", 401),
    ("/api/angelone/funds", "GET", 401),
    ("/api/angelone/account", "GET", 401),
    ("/api/angelone/order", "POST", 401),
    ("/api/angelone/squareoff", "POST", 401),
    ("/api/angelone/search", "GET", 401),
    ("/api/angelone/quote", "GET", 401),
    ("/api/angelone/candles", "GET", 401),
    ("/api/zerodha/connect", "POST", 401),
    ("/api/zerodha/account", "GET", 401),
    ("/api/zerodha/funds", "GET", 401),
    ("/api/telegram/status", "GET", 401),
    ("/api/admin/bot_mode", "GET", 401),
    ("/api/aggregate/overview", "GET", 401),
    ("/api/audit_full", "GET", 401),
]
for path, method, expected in endpoints:
    try:
        r = requests.request(method, DASH + path, timeout=5)
        chk("ENDPOINT", f"{method} {path}", r.status_code == expected,
            f"got {r.status_code} (expected {expected})")
    except Exception as e:
        chk("ENDPOINT", f"{method} {path}", False, e)

# ─── State files ──────────────────────────────────────────────
for f in ["bot_state.json","positions_state.json","trade_log.json",
          "strategy_params.json","alpaca_config.json","equity_history.json"]:
    p = os.path.join(BASE, f)
    chk("STATE", f"file:{f}", os.path.exists(p),
        f"{os.path.getsize(p) if os.path.exists(p) else 0}b")

# ─── Position consistency ─────────────────────────────────────
try:
    state_pos = json.load(open(os.path.join(BASE,"positions_state.json")))
    alp_syms = {p["symbol"] for p in pos_list} if isinstance(pos_list, list) else set()
    state_syms = set(state_pos.keys())
    drift = (state_syms ^ alp_syms)
    chk("STATE", "positions_match_alpaca", not drift,
        f"drift: {sorted(drift)}" if drift else "")
except Exception as e:
    chk("STATE", "positions_match_alpaca", False, e)

# ─── Phantom trade-log check ──────────────────────────────────
try:
    log = json.load(open(os.path.join(BASE,"trade_log.json")))
    sells = [t for t in log if t.get("action")=="sell"]
    after = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat().replace("+00:00","Z")
    fills = requests.get(f"{URL}/orders", headers=HDR,
                         params={"status":"filled","limit":500,"after":after}, timeout=15).json()
    real_sells = sum(1 for f in fills if f["side"]=="sell")
    chk("TRADELOG", "phantom_ratio_ok",
        len(sells) <= max(real_sells * 1.5, real_sells + 5),
        f"log_sells={len(sells)} alpaca_7d_sells={real_sells}")
except Exception as e:
    chk("TRADELOG", "phantom_ratio_ok", False, e)

# ─── Bot architecture audit ────────────────────────────────────
try:
    bot_src = open(os.path.join(BASE, "intraday_bot_v2.py")).read()
    chk("BOT", "uses_broker_abstraction", "from brokers" in bot_src,
        "Bot still hardcoded to Alpaca" if "from brokers" not in bot_src else "")
    chk("BOT", "phantom_sell_fixed", "close_attempt_cooldown_until" in bot_src,
        "Cooldown guard present")
    chk("BOT", "eod_close_cancels_orders", 'cancel-orders' in bot_src or
        '"DELETE", "/orders"' in bot_src,
        "close_all cancels open orders first")
except Exception as e:
    chk("BOT", "bot_audit", False, e)

# ─── Brokers module exports ───────────────────────────────────
try:
    sys.path.insert(0, BASE)
    from brokers.angelone import AngelOneBroker
    from brokers.zerodha import ZerodhaBroker
    chk("BROKERS", "angelone_module_loadable", True, f"class={AngelOneBroker.__name__}")
    chk("BROKERS", "zerodha_module_loadable", True, f"class={ZerodhaBroker.__name__}")
    chk("BROKERS", "angelone_has_place_order", hasattr(AngelOneBroker, "place_order"), "")
    chk("BROKERS", "zerodha_has_place_order", hasattr(ZerodhaBroker, "place_order"), "")
except Exception as e:
    chk("BROKERS", "module_import", False, e)

# ─── System ──────────────────────────────────────────────────
try:
    df = subprocess.check_output(["df","-h","/"], text=True).splitlines()[1].split()
    chk("SYSTEM", "disk_under_90pct", int(df[4].rstrip("%")) < 90, f"used={df[4]}")
except Exception:
    pass

# ─── Output ──────────────────────────────────────────────────
print(f"\n=== AUDIT  {datetime.now().isoformat(timespec='seconds')} ===\n")
total = len(results)
failed = sum(1 for _,_,ok,_ in results if not ok)
groups = {}
for cat, lbl, ok, det in results:
    groups.setdefault(cat, []).append((lbl, ok, det))
for cat, items in groups.items():
    bad = sum(1 for _, ok, _ in items if not ok)
    print(f"[{cat}]  {len(items)-bad}/{len(items)} pass" + (f"  ({bad} fail)" if bad else ""))
    for lbl, ok, det in items:
        m = "OK  " if ok else "FAIL"
        print(f"  [{m}] {lbl:42} {det}")
    print()
print(f"OVERALL: {total-failed}/{total} checks passed")
sys.exit(0 if failed == 0 else 1)
