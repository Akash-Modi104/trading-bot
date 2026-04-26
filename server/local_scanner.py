"""
local_scanner.py — Open-source LLM stock scanner
Replaces the Claude scheduled task on Hostinger KVM2.
Uses Ollama (qwen2.5:7b) for AI scoring + DuckDuckGo for news.
Runs every 15 min during market hours, writes claude_picks.json.
"""

import json, os, time, re, requests
from datetime import datetime, date
from duckduckgo_search import DDGS
import pytz, schedule

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Load .env ────────────────────────────────────────────────
ENV_FILE = os.path.join(BASE_DIR, ".env")
try:
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)
except ImportError:
    if os.path.exists(ENV_FILE):
        for line in open(ENV_FILE):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

API_KEY    = os.environ["ALPACA_API_KEY"]
SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
DATA_URL   = os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets/v2")
ALPACA_URL = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b-instruct-q4_K_M")
PICKS_FILE = os.path.join(BASE_DIR, "claude_picks.json")
NEG_NEWS_F = os.path.join(BASE_DIR, "negative_news.json")

HEADERS = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY}
ET = pytz.timezone("America/New_York")

# ── Watchlist ─────────────────────────────────────────────────
BASE_WATCHLIST = [
    "AAPL","TSLA","NVDA","MSFT","AMZN","META","GOOGL","AMD",
    "NFLX","UBER","COIN","ARM","PLTR","AVGO","MU","SMCI",
    "SPY","QQQ"
]

SECTOR_MAP = {
    "AAPL":"Technology","MSFT":"Technology","GOOGL":"Technology",
    "META":"Technology","NVDA":"Technology","AMD":"Technology",
    "ARM":"Technology","AVGO":"Technology","PLTR":"Technology",
    "MU":"Technology","SMCI":"Technology",
    "AMZN":"Consumer Discretionary","TSLA":"Consumer Discretionary",
    "NFLX":"Communication","COIN":"Crypto","UBER":"Industrial",
    "SPY":"ETF","QQQ":"ETF",
}

# ── Helpers ───────────────────────────────────────────────────
def now_et():
    return datetime.now(ET)

def market_open():
    n = now_et()
    from datetime import time as dtime
    return n.weekday() < 5 and dtime(9,30) <= n.time() <= dtime(15,45)

def log(msg):
    ts = now_et().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# ── Alpaca data ───────────────────────────────────────────────
def get_bars(sym, tf="5Min", limit=80):
    try:
        r = requests.get(f"{DATA_URL}/stocks/{sym}/bars",
                         headers=HEADERS,
                         params={"timeframe": tf, "limit": limit, "feed": "iex"},
                         timeout=10)
        d = r.json()
        return d.get("bars") or [] if isinstance(d, dict) else []
    except Exception:
        return []

def get_latest_price(sym):
    try:
        r = requests.get(f"{DATA_URL}/stocks/{sym}/trades/latest",
                         headers=HEADERS, timeout=5)
        return r.json().get("trade", {}).get("p")
    except Exception:
        return None

# ── Technical indicators ──────────────────────────────────────
def ema_series(vals, n):
    if len(vals) < n: return [None]*len(vals)
    k = 2/(n+1); out = [None]*(n-1)
    s = sum(vals[:n])/n; out.append(s)
    for v in vals[n:]: s = v*k+s*(1-k); out.append(s)
    return out

def rsi_val(closes, n=14):
    if len(closes) < n+1: return 50
    g = [max(closes[i]-closes[i-1],0) for i in range(1,len(closes))]
    l = [max(closes[i-1]-closes[i],0) for i in range(1,len(closes))]
    ag, al = sum(g[-n:])/n, sum(l[-n:])/n
    return 100 if al==0 else 100-100/(1+ag/al)

def vwap_val(bars):
    num=den=0
    for b in bars:
        tp=(b["h"]+b["l"]+b["c"])/3; num+=tp*b["v"]; den+=b["v"]
    return num/den if den else None

def rel_vol(bars, lb=20):
    if len(bars)<5: return 1.0
    avg = sum(b["v"] for b in bars[-lb-1:-1])/min(lb,len(bars)-1)
    return bars[-1]["v"]/avg if avg else 1.0

def technical_score(sym):
    """Calculate technical score 0-60. Returns (score, details_dict)."""
    bars = get_bars(sym, "5Min", 80)
    if len(bars) < 25:
        return 0, {}

    closes = [b["c"] for b in bars]
    curr   = closes[-1]
    ef = ema_series(closes, 9)
    es = ema_series(closes, 21)
    r  = rsi_val(closes)
    vw = vwap_val(bars)
    rv = rel_vol(bars)

    score = 0
    details = {"price": curr, "rsi": round(r,1), "relvol": round(rv,2)}

    # Disqualifiers
    if r > 78 or r < 35:
        return 0, {"skip": f"RSI {r:.0f} extreme"}

    # EMA crossover in last 3 bars
    if all(x is not None for x in [ef[-1],es[-1],ef[-2],es[-2]]):
        if ef[-1] > es[-1]:
            if ef[-2] <= es[-2]:
                score += 20; details["ema_cross"] = "fresh crossover"
            else:
                score += 10; details["ema"] = "above EMA"

    # RSI zone
    if 50 <= r <= 70:
        score += 15; details["rsi_ok"] = True

    # VWAP
    if vw and curr > vw:
        details["vwap"] = f"${vw:.2f}"
        score += 15
    elif vw and curr <= vw:
        score -= 5

    # Relative volume
    if rv >= 1.8:
        score += 10; details["high_rvol"] = True
    elif rv >= 1.3:
        score += 5

    return max(0, min(score, 60)), details

# ── DuckDuckGo news search ────────────────────────────────────
def search_news(query, max_results=5):
    """Search internet using DuckDuckGo. Returns list of result snippets."""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        return [r.get("body","") + " " + r.get("title","") for r in results]
    except Exception as e:
        log(f"Search error: {e}")
        return []

def get_movers():
    """Find trending tickers from web searches."""
    searches = [
        "top stock gainers today premarket",
        "unusual options activity stocks today",
        "stocks breaking out 52 week high today",
        "momentum stocks trending today NYSE NASDAQ",
    ]
    found = {}
    ticker_re = re.compile(r'\b([A-Z]{2,5})\b')
    noise = {"THE","FOR","AND","ARE","HAS","TOP","BIG","NEW","NOW","DAY",
             "ALL","LOW","BUY","SEC","FDA","CEO","GET","SET","USE","INC",
             "LLC","ETF","IPO","USD","NET","GDP","CPI","FED","API","EST"}
    for q in searches:
        snippets = search_news(q, 8)
        for s in snippets:
            for m in ticker_re.finditer(s):
                t = m.group(1)
                if t not in noise and len(t) >= 2:
                    found[t] = found.get(t,0) + 1
    # Filter to only known/valid-looking tickers (3-5 chars, in sector map or high freq)
    candidates = [t for t,c in sorted(found.items(),key=lambda x:-x[1])
                  if c >= 2 or t in SECTOR_MAP]
    return list(dict.fromkeys(BASE_WATCHLIST + candidates[:10]))[:25]

# ── Ollama LLM scoring ─────────────────────────────────────────
def ollama_score(sym, tech_score, tech_details, news_snippets, sector):
    """Ask local LLM to score sentiment + give final confidence. Returns (ai_score 0-40, sentiment, reason)."""
    news_text = "\n".join(f"- {s[:200]}" for s in news_snippets[:5]) or "No news found."

    prompt = f"""You are a stock trading analyst. Score this stock for an intraday momentum trade.

Symbol: {sym}
Sector: {sector}
Technical data:
  - Price: ${tech_details.get('price', 'N/A')}
  - RSI(14): {tech_details.get('rsi', 'N/A')}
  - Relative Volume: {tech_details.get('relvol', 'N/A')}x
  - Above VWAP: {'Yes' if 'vwap' in tech_details else 'No'}
  - EMA crossover: {tech_details.get('ema_cross', tech_details.get('ema', 'No'))}
Technical score: {tech_score}/60

Recent news:
{news_text}

Task: Give a sentiment and AI score (0-40) for trading this stock TODAY based on news.
Be strict. Only score high if there is a clear positive catalyst.

Respond ONLY in this exact JSON format (no other text):
{{"sentiment": "positive|negative|neutral", "ai_score": <number 0-40>, "reason": "<one sentence>"}}"""

    try:
        r = requests.post(f"{OLLAMA_URL}/api/generate",
                          json={"model": OLLAMA_MODEL, "prompt": prompt,
                                "stream": False, "options": {"temperature": 0.1}},
                          timeout=60)
        text = r.json().get("response", "{}")
        # Extract JSON from response
        match = re.search(r'\{[^}]+\}', text, re.DOTALL)
        if match:
            data = json.loads(match.group())
            return (int(data.get("ai_score", 0)),
                    data.get("sentiment", "neutral"),
                    data.get("reason", "")[:150])
    except Exception as e:
        log(f"  Ollama error for {sym}: {e}")
    return 0, "neutral", ""

# ── Pre-market gap ────────────────────────────────────────────
def premarket_gap(sym):
    """Estimate pre-market gap % from today's first bar vs yesterday's close."""
    try:
        bars = get_bars(sym, "1Day", 3)
        if len(bars) >= 2:
            prev_close = bars[-2]["c"]
            today_open = bars[-1]["o"]
            return round((today_open - prev_close) / prev_close * 100, 2)
    except Exception:
        pass
    return 0.0

# ── Main scan ─────────────────────────────────────────────────
def run_scan():
    if not market_open():
        log("Market closed — skipping scan")
        return

    today = now_et().strftime("%Y-%m-%d")
    log(f"=== Starting scan ({today}) ===")

    # Step 1: Get candidate tickers
    log("Fetching movers from web...")
    watchlist = get_movers()
    log(f"Watchlist ({len(watchlist)}): {watchlist}")

    # Step 2: Score each ticker
    picks = []
    neg_tickers = {}

    for sym in watchlist:
        if sym in ("SPY", "QQQ", "VIXY"): continue
        log(f"  Analyzing {sym}...")

        # Technical score
        t_score, t_details = technical_score(sym)
        if t_score == 0:
            log(f"  {sym} skipped — technical fail ({t_details})")
            continue

        # News search
        snippets = search_news(f"{sym} stock news today", 4)

        # Check for negative keywords
        neg_keywords = ["lawsuit","fraud","bankruptcy","investigation","downgrade",
                        "miss","recall","fda reject","accounting","scandal","halt"]
        has_negative = any(kw in " ".join(snippets).lower() for kw in neg_keywords)
        if has_negative:
            log(f"  {sym} → negative news detected, adding to kill-switch")
            neg_tickers[sym] = "negative news detected"
            continue

        # Ollama AI scoring
        sector = SECTOR_MAP.get(sym, "Other")
        ai_score, sentiment, reason = ollama_score(sym, t_score, t_details, snippets, sector)

        total = t_score + ai_score
        gap   = premarket_gap(sym)

        # Pre-market gap bonus
        if gap >= 2.0:
            total += 5; reason += f" Gap +{gap:.1f}%."
        if gap >= 5.0:
            total += 5

        log(f"  {sym}: tech={t_score} ai={ai_score} total={total} sentiment={sentiment}")

        if total >= 60 and sentiment != "negative":
            picks.append({
                "date": today,
                "symbol": sym,
                "confidence": min(total, 100),
                "sector": sector,
                "reason": reason or t_details.get("ema_cross","momentum setup"),
                "news_sentiment": sentiment,
                "has_earnings": False,
                "rel_volume": t_details.get("relvol", 1.0),
                "pre_market_gap": gap,
                "recommended_action": "buy",
                "analyst_notes": f"Tech {t_score}/60 | AI {ai_score}/40 | RSI {t_details.get('rsi','?')}",
            })

    # Sort and cap
    picks.sort(key=lambda x: -x["confidence"])
    picks = picks[:8]

    # Write picks
    with open(PICKS_FILE, "w") as f:
        json.dump(picks, f, indent=2)
    log(f"Wrote {len(picks)} picks to claude_picks.json")
    for p in picks:
        log(f"  {p['symbol']:6s} confidence={p['confidence']} | {p['reason'][:60]}")

    # Write negative news kill-switch
    with open(NEG_NEWS_F, "w") as f:
        json.dump({"date": today, "tickers": list(neg_tickers.keys()),
                   "reasons": neg_tickers}, f, indent=2)
    if neg_tickers:
        log(f"Kill-switch: {list(neg_tickers.keys())}")

    log("=== Scan complete ===\n")

# ── Scheduler ─────────────────────────────────────────────────
def main():
    log("Local scanner starting (Ollama model: " + OLLAMA_MODEL + ")")

    # Check Ollama is reachable
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        models = [m["name"] for m in r.json().get("models", [])]
        log(f"Ollama available. Models: {models}")
    except Exception:
        log("WARNING: Ollama not reachable at " + OLLAMA_URL)
        log("Run: ollama serve  and  ollama pull qwen2.5:7b-instruct-q4_K_M")

    # Run immediately on start, then every 15 min
    run_scan()
    schedule.every(15).minutes.do(run_scan)

    # Also run at 9:00 AM ET (pre-market)
    schedule.every().monday.at("09:00").do(run_scan)
    schedule.every().tuesday.at("09:00").do(run_scan)
    schedule.every().wednesday.at("09:00").do(run_scan)
    schedule.every().thursday.at("09:00").do(run_scan)
    schedule.every().friday.at("09:00").do(run_scan)

    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    main()
