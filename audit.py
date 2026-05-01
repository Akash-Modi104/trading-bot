#!/usr/bin/env python3
"""System health audit. Usage: audit.py"""
import os, json, sys, subprocess, requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))

results = []
def chk(label, ok, detail=""):
    results.append((label, ok, str(detail)))

# 1) supervisor
try:
    out = subprocess.check_output(["supervisorctl","status"], text=True, timeout=10)
    for line in out.strip().splitlines():
        parts = line.split()
        if len(parts) >= 2:
            chk(f"supervisor:{parts[0]}", parts[1] == "RUNNING", parts[1])
except Exception as e:
    chk("supervisorctl", False, e)

# 2) Alpaca
KEY = os.environ.get("ALPACA_API_KEY","")
SEC = os.environ.get("ALPACA_SECRET_KEY","")
URL = os.environ.get("ALPACA_BASE_URL","").rstrip("/")
HDR = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC}
pos_list = []
try:
    r = requests.get(f"{URL}/account", headers=HDR, timeout=10)
    chk("alpaca:auth", r.status_code == 200, f"HTTP {r.status_code}")
    if r.status_code == 200:
        a = r.json()
        chk("alpaca:account_active", a.get("status") == "ACTIVE", a.get("status",""))
        chk("alpaca:trading_not_blocked", not a.get("trading_blocked"), a.get("trading_blocked"))
        chk("alpaca:is_paper_account", "paper" in URL.lower(), URL)
        chk("alpaca:equity_present", float(a.get("equity",0)) > 0, f"${a.get('equity')}")
    pos_list = requests.get(f"{URL}/positions", headers=HDR, timeout=10).json()
    chk("alpaca:positions_endpoint", isinstance(pos_list, list),
        f"{len(pos_list) if isinstance(pos_list,list) else 0} pos")
    clk = requests.get(f"{URL}/clock", headers=HDR, timeout=10).json()
    chk("alpaca:clock", "is_open" in clk, f"is_open={clk.get('is_open')}")
except Exception as e:
    chk("alpaca", False, e)

# 3) state files
for f in ["bot_state.json","positions_state.json","trade_log.json",
          "strategy_params.json","alpaca_config.json"]:
    p = os.path.join(BASE, f)
    chk(f"file:{f}", os.path.exists(p), f"{os.path.getsize(p) if os.path.exists(p) else 0}b")

# 4) positions_state vs Alpaca
try:
    state_pos = json.load(open(os.path.join(BASE,"positions_state.json")))
    alp_syms = {p["symbol"] for p in pos_list} if isinstance(pos_list, list) else set()
    state_syms = set(state_pos.keys())
    only_state = state_syms - alp_syms
    only_alp = alp_syms - state_syms
    chk("state:positions_match_alpaca", not (only_state or only_alp),
        f"state_only={list(only_state)} alpaca_only={list(only_alp)}")
except Exception as e:
    chk("state:positions_match_alpaca", False, e)

# 5) phantom check
try:
    log = json.load(open(os.path.join(BASE,"trade_log.json")))
    sells = [t for t in log if t.get("action")=="sell"]
    after = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat().replace("+00:00","Z")
    fills = requests.get(f"{URL}/orders", headers=HDR,
                         params={"status":"filled","limit":500,"after":after}, timeout=15).json()
    real_sells = sum(1 for f in fills if f["side"]=="sell")
    chk("trade_log:phantom_ratio",
        len(sells) <= max(real_sells * 1.5, real_sells + 5),
        f"log_sells={len(sells)} alpaca_7d_sells={real_sells}")
except Exception as e:
    chk("trade_log:phantom_ratio", False, e)

# 6) bot state freshness
try:
    bs = json.load(open(os.path.join(BASE, "bot_state.json")))
    chk("bot:state_loaded", bool(bs), f"keys={len(bs)}")
    chk("bot:not_paused", not bs.get("trading_paused"), bs.get("pause_reason",""))
except Exception as e:
    chk("bot:state_loaded", False, e)

# 7) disk
try:
    df = subprocess.check_output(["df","-h","/"], text=True).splitlines()[1].split()
    chk("system:disk_under_90pct", int(df[4].rstrip("%")) < 90, f"used={df[4]}")
except Exception:
    pass

# 8) DB
try:
    import sqlite3
    con = sqlite3.connect(os.path.join(BASE,"users.db"), timeout=5)
    n = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    con.close()
    chk("db:users_table", True, f"{n} users")
except Exception as e:
    chk("db:users_table", False, e)

print(f"\n=== AUDIT  {datetime.now().isoformat(timespec='seconds')} ===\n")
total = len(results); failed = sum(1 for _,ok,_ in results if not ok)
for label, ok, detail in results:
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {label:38} {detail}")
print(f"\n{total-failed}/{total} checks passed")
sys.exit(0 if failed == 0 else 1)
