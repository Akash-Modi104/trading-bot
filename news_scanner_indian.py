#!/usr/bin/env python3
"""
Indian Stock News Scanner — runs every 5 minutes via cron/supervisor.

Sources (all FREE, no API key required):
  1. NSE Corporate Announcements API
     - Detects: regulatory action, fraud, suspension, default,
       resignation of MD/CEO/CFO/auditor, debt-restructuring,
       insolvency, fine, penalty, raid, investigation
  2. Manual override file: data/manual_news_kill.json

Output: writes negative_news_in.json consumed by indian_bot.py
Format: {"date": "YYYY-MM-DD", "tickers": ["TCS","INFY"],
         "sources": {"TCS": ["NSE: MD resignation"]}}
"""
import json, os, sys, time, re
from datetime import datetime, timezone, timedelta
import requests
import pytz

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT   = os.path.join(BASE_DIR, "negative_news_in.json")
MANUAL_F = os.path.join(BASE_DIR, "manual_news_kill.json")
IST      = pytz.timezone("Asia/Kolkata")

# Keywords that mark an announcement as MATERIALLY NEGATIVE
NEGATIVE_KEYWORDS = [
    # Regulatory / legal
    "sebi action", "sebi order", "show cause", "show-cause", "raid", "search and seizure",
    "investigation", "probe", "ed investigation", "cbi", "cbi investigation",
    "income tax raid", "fraud", "fraudulent", "alleged fraud", "siphon",
    "suspension", "suspended", "debar", "delist",
    # Financial distress
    "default", "defaulted", "insolvency", "ibc", "nclt", "nclat",
    "debt restructur", "stretched liquid", "going concern",
    "credit downgrade", "rating downgrade", "rating cut",
    "negative outlook", "auditor resign", "auditor concerns",
    "qualified opinion", "going concern",
    # Top management exits
    "ceo resign", "managing director resign", "md resign",
    "cfo resign", "company secretary resign", "whole-time director resign",
    "independent director resign", "auditor resign",
    # Operational
    "fire at plant", "explosion", "plant shutdown",
    "production halt", "factory accident", "force majeure",
    "labour strike", "lockout",
    # Earnings shock
    "miss estimate", "below estimate", "profit warning",
    "guidance cut", "guidance withdrawn", "loss widen",
    # Penalty / fine
    "penalty", "fine of rs", "fine of ₹",
]

# Map of common short forms → official NSE symbols
SYMBOL_ALIASES = {
    "M&M": "M&M", "TATAMOT": "TATAMOTORS", "TATASTL": "TATASTEEL",
    "RELIN": "RELIANCE", "L&T": "LT", "HUL": "HINDUNILVR",
    "HDFCBNK": "HDFCBANK", "ICICIBNK": "ICICIBANK",
}

# Our bot's watchlist
WATCHLIST = {
    "TCS","INFY","WIPRO","HCLTECH","TECHM","LTIM","HDFCBANK","ICICIBANK",
    "SBIN","KOTAKBANK","AXISBANK","BAJFINANCE","BAJAJFINSV","INDUSINDBK",
    "SBILIFE","HDFCLIFE","SHRIRAMFIN","RELIANCE","ONGC","COALINDIA","BPCL",
    "MARUTI","TATAMOTORS","M&M","BAJAJ-AUTO","HEROMOTOCO","EICHERMOT",
    "HINDUNILVR","ITC","NESTLEIND","BRITANNIA","TATACONSUM","TITAN",
    "ASIANPAINT","SUNPHARMA","DRREDDY","DIVISLAB","CIPLA","APOLLOHOSP",
    "TATASTEEL","JSWSTEEL","HINDALCO","ULTRACEMCO","GRASIM","BHARTIARTL",
    "NTPC","POWERGRID","LT","ADANIENT","ADANIPORTS",
}

def fetch_nse_announcements() -> list:
    """NSE corporate announcements (free, no auth)."""
    url = "https://www.nseindia.com/api/corporate-announcements"
    # Today's date in DD-MM-YYYY
    today = datetime.now(IST).strftime("%d-%m-%Y")
    params = {"index": "equities", "from_date": today, "to_date": today}
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
    }
    s = requests.Session()
    s.headers.update(headers)
    try:
        # Warm up cookie
        s.get("https://www.nseindia.com/", timeout=10)
        r = s.get(url, params=params, timeout=15)
        if r.status_code != 200:
            return []
        return r.json() if r.headers.get("content-type","").startswith("application/json") else []
    except Exception as e:
        print(f"NSE fetch error: {e}", file=sys.stderr)
        return []

def fetch_manual_overrides() -> dict:
    """User can manually flag stocks via dashboard."""
    if not os.path.exists(MANUAL_F):
        return {}
    try:
        with open(MANUAL_F) as f:
            d = json.load(f)
        today = datetime.now(IST).strftime("%Y-%m-%d")
        if d.get("date") != today:
            return {}
        return {t.upper(): d.get("reasons", {}).get(t, "manual override")
                for t in d.get("tickers", [])}
    except Exception:
        return {}

def classify(text: str) -> str | None:
    """Return matched keyword if text contains negative phrase, else None."""
    if not text:
        return None
    low = text.lower()
    for kw in NEGATIVE_KEYWORDS:
        if kw in low:
            return kw
    return None

def main():
    flagged: dict[str, list[str]] = {}

    # 1) NSE announcements
    anns = fetch_nse_announcements()
    print(f"[news-scanner] Fetched {len(anns)} NSE announcements")
    for ann in anns:
        sym = (ann.get("symbol") or "").upper().strip()
        sym = SYMBOL_ALIASES.get(sym, sym)
        if sym not in WATCHLIST:
            continue
        subject = ann.get("subject", "") or ""
        desc    = ann.get("attchmntText", "") or ann.get("details", "") or ""
        full    = f"{subject} | {desc}"
        kw = classify(full)
        if kw:
            flagged.setdefault(sym, []).append(f"NSE: {subject[:80]} [{kw}]")

    # 2) Manual overrides from dashboard
    manuals = fetch_manual_overrides()
    for sym, reason in manuals.items():
        if sym in WATCHLIST:
            flagged.setdefault(sym, []).append(f"Manual: {reason}")

    # Write output
    output = {
        "date": datetime.now(IST).strftime("%Y-%m-%d"),
        "scanned_at": datetime.now(IST).isoformat(timespec="seconds"),
        "tickers": sorted(flagged.keys()),
        "sources": {k: v for k, v in flagged.items()},
        "watchlist_size": len(WATCHLIST),
        "announcements_scanned": len(anns),
    }
    with open(OUTPUT, "w") as f:
        json.dump(output, f, indent=2)

    if flagged:
        print(f"[news-scanner] 🚨 FLAGGED {len(flagged)} stocks: {', '.join(sorted(flagged.keys()))}")
    else:
        print(f"[news-scanner] ✅ No negative news for {len(WATCHLIST)} stocks")

if __name__ == "__main__":
    main()
