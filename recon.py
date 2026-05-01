#!/usr/bin/env python3
"""Reconcile trade_log.json against Alpaca actual fills.
Usage: recon.py [--fix]   # --fix rewrites trade_log.json keeping only matched rows
"""
import os, sys, json, time, shutil, requests
from collections import defaultdict
from datetime import datetime
from dotenv import load_dotenv
import pytz

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))
ET = pytz.timezone("America/New_York")
KEY = os.environ["ALPACA_API_KEY"]
SEC = os.environ["ALPACA_SECRET_KEY"]
URL = os.environ["ALPACA_BASE_URL"].rstrip("/")
HDR = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC}

def alpaca_fills(after="2026-01-01T00:00:00Z"):
    out, cursor = [], after
    while True:
        r = requests.get(f"{URL}/orders", headers=HDR,
                         params={"status":"all","limit":500,"after":cursor,"direction":"asc"},
                         timeout=20)
        r.raise_for_status()
        page = r.json()
        if not page: break
        out.extend(o for o in page if o["status"] == "filled")
        if len(page) < 500: break
        cursor = page[-1]["submitted_at"]
    return out

def reconcile(fix=False):
    log_path = os.path.join(BASE, "trade_log.json")
    log = json.load(open(log_path))
    fills = alpaca_fills()

    broker_idx = defaultdict(list)
    for f in fills:
        date = (f["filled_at"] or "")[:10]
        broker_idx[(date, f["symbol"], f["side"])].append({
            "qty": int(float(f["filled_qty"])),
            "px":  float(f["filled_avg_price"] or 0),
        })

    print(f"trade_log.json   : {len(log)} entries")
    print(f"Alpaca fills     : {len(fills)} entries")

    matched, phantom = [], []
    used = defaultdict(set)
    for entry in log:
        date = (entry.get("time","") or "")[:10]
        key = (date, entry.get("sym",""), entry.get("action",""))
        cands = broker_idx.get(key, [])
        want_qty = int(entry.get("qty", 0))
        match_idx = None
        for i, b in enumerate(cands):
            if i in used[key]: continue
            if abs(b["qty"] - want_qty) <= 1:
                match_idx = i; break
        if match_idx is not None:
            used[key].add(match_idx)
            matched.append(entry)
        else:
            phantom.append(entry)

    by_reason = defaultdict(int)
    for p in phantom: by_reason[p.get("reason","?")] += 1

    real_pnl = round(sum(t.get("pnl_abs",0) for t in matched if t.get("action")=="sell"), 2)
    fake_pnl = round(sum(t.get("pnl_abs",0) for t in phantom if t.get("action")=="sell"), 2)

    print("\n=== RESULT ===")
    print(f"  matched : {len(matched)} (real)")
    print(f"  phantom : {len(phantom)} (no broker match)")
    if phantom:
        print("  phantom reasons:")
        for r, n in sorted(by_reason.items(), key=lambda x:-x[1]):
            print(f"    {r:30} {n}")
    print(f"\n  P&L from MATCHED sells : ${real_pnl:+.2f}  <-- truth")
    print(f"  P&L from PHANTOM sells : ${fake_pnl:+.2f}  <-- inflation")

    if fix and phantom:
        bak = log_path + f".bak.{int(time.time())}"
        shutil.copy(log_path, bak)
        json.dump(matched, open(log_path,"w"), indent=2)
        print(f"\n  REWROTE {log_path}: removed {len(phantom)} phantoms (backup: {bak})")
    elif phantom:
        print("\n  Pass --fix to rewrite trade_log.json with phantoms removed.")

if __name__ == "__main__":
    reconcile(fix="--fix" in sys.argv)
