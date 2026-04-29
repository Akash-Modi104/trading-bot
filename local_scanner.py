"""
local_scanner.py — Open-source LLM stock scanner
Uses Ollama (qwen2.5) for AI scoring + DuckDuckGo for news.
Runs every 15 min during market hours, writes claude_picks.json.
"""

import json, os, time, re, requests, sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
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

API_KEY      = os.environ["ALPACA_API_KEY"]
SECRET_KEY   = os.environ["ALPACA_SECRET_KEY"]
DATA_URL     = os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets/v2")
OLLAMA_URL   = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b")
PICKS_FILE   = os.path.join(BASE_DIR, "claude_picks.json")
NEG_NEWS_F   = os.path.join(BASE_DIR, "negative_news.json")
STRAT_F      = os.path.join(BASE_DIR, "strategy_params.json")

def _active_model():
    """Return model from strategy_params.json if set, else env var."""
    try:
        with open(STRAT_F) as f:
            m = json.load(f).get("ollama_model", "").strip()
        if m:
            return m
    except Exception:
        pass
    return OLLAMA_MODEL

HEADERS = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY}
ET = pytz.timezone("America/New_York")

# ── Watchlist ─────────────────────────────────────────────────
BASE_WATCHLIST = [
    "AAPL","TSLA","NVDA","MSFT","AMZN","META","GOOGL","AMD",
    "NFLX","UBER","COIN","ARM","PLTR","AVGO","MU","SMCI",
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
    return n.weekday() < 5 and dtime(9, 30) <= n.time() <= dtime(15, 45)

def premarket_window():
    n = now_et()
    from datetime import time as dtime
    return n.weekday() < 5 and dtime(9, 0) <= n.time() < dtime(9, 30)

def log(msg):
    ts = now_et().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# ── Alpaca data ───────────────────────────────────────────────
def get_bars(sym, tf="5Min", limit=80):
    try:
        r = requests.get(
            f"{DATA_URL}/stocks/{sym}/bars",
            headers=HEADERS,
            params={"timeframe": tf, "limit": limit, "feed": "iex"},
            timeout=10,
        )
        d = r.json()
        return d.get("bars") or [] if isinstance(d, dict) else []
    except Exception:
        return []

# ── Technical indicators ──────────────────────────────────────
def ema_series(vals, n):
    if len(vals) < n: return [None] * len(vals)
    k = 2 / (n + 1); out = [None] * (n - 1)
    s = sum(vals[:n]) / n; out.append(s)
    for v in vals[n:]: s = v * k + s * (1 - k); out.append(s)
    return out

def rsi_val(closes, n=14):
    if len(closes) < n + 1: return 50
    g = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    l = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(g[-n:]) / n, sum(l[-n:]) / n
    return 100 if al == 0 else 100 - 100 / (1 + ag / al)

def vwap_val(bars):
    num = den = 0
    for b in bars:
        tp = (b["h"] + b["l"] + b["c"]) / 3; num += tp * b["v"]; den += b["v"]
    return num / den if den else None

def rel_vol(bars, lb=20):
    if len(bars) < 5: return 1.0
    avg = sum(b["v"] for b in bars[-lb-1:-1]) / min(lb, len(bars) - 1)
    return bars[-1]["v"] / avg if avg else 1.0

def technical_score(sym):
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
    details = {"price": curr, "rsi": round(r, 1), "relvol": round(rv, 2)}

    if r > 78 or r < 35:
        return 0, {"skip": f"RSI {r:.0f} extreme"}

    if all(x is not None for x in [ef[-1], es[-1], ef[-2], es[-2]]):
        if ef[-1] > es[-1]:
            if ef[-2] <= es[-2]:
                score += 20; details["ema_cross"] = "fresh crossover"
            else:
                score += 10; details["ema"] = "above EMA"
        elif ef[-1] < es[-1]:
            # Bearish EMA — penalize but don't hard-reject at scanner level
            score -= 5; details["ema"] = "bearish"

    if 45 <= r <= 75:   # wider band matches bot's rsi_buy_min/max
        score += 15; details["rsi_ok"] = True
    elif 35 <= r < 45:
        score += 5; details["rsi"] = "acceptable"

    if vw and curr > vw:
        details["vwap"] = f"${vw:.2f}"; score += 15
    elif vw and curr <= vw:
        score -= 3   # mild penalty, not a reject

    if rv >= 1.5:
        score += 10; details["high_rvol"] = True
    elif rv >= 1.0:
        score += 5   # average volume is fine

    return max(0, min(score, 60)), details

# ── DuckDuckGo news ───────────────────────────────────────────
def search_news(query, max_results=4, retries=2):
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS
    for attempt in range(retries):
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=max_results))
            return [r.get("body", "") + " " + r.get("title", "") for r in results]
        except Exception as e:
            err = str(e).lower()
            if ("ratelimit" in err or "202" in err or "timeout" in err) and attempt < retries - 1:
                time.sleep(4 * (attempt + 1))
            else:
                break
    return []

def get_movers():
    searches = [
        "top stock gainers today premarket",
        "unusual options activity stocks today",
        "stocks breaking out today NYSE NASDAQ",
        "momentum stocks trending today",
    ]
    found = {}
    ticker_re = re.compile(r'\b([A-Z]{2,5})\b')
    # Extended noise set — financial/market terms that look like tickers
    noise = {
        "THE","FOR","AND","ARE","HAS","TOP","BIG","NEW","NOW","DAY","ALL","LOW",
        "BUY","SEC","FDA","CEO","GET","SET","USE","INC","LLC","ETF","IPO","USD",
        "NET","GDP","CPI","FED","API","EST","NYSE","NASDAQ","STOCK","YEAR",
        "HIGH","WEEK","TODAY","OPEN","CLOSE","SELL","HOLD","PUTS","CALL",
        # Technical analysis terms that are NOT tickers
        "RSI","EMA","MACD","ATR","VWAP","ORB","SMA","WMA","ADX","OBV","ROC",
        "BB","PPO","DMA","STOCH","CCI","MFI","CMF","ROI","PNL","PEG","TTM",
        # Financial terms
        "CNN","DCA","BOT","FAQ","API","URL","HTML","CSS","JSON","XML","PDF",
        "QE","PE","EPS","FCF","EV","IRR","NPV","DCF","LBO","IPO","SPO","APY",
        "BPS","YTD","QTD","MTD","YOY","QOQ","MOM","TTM","TBD","NDA","LOI",
        "ETF","SPX","NDX","RUT","DJI","VIX","VXX","XIV","UVXY","SVXY",
        # Common English words
        "BUT","NOT","WITH","FROM","THIS","THAT","THEY","THEIR","HAVE","WILL",
        "BEEN","MORE","ALSO","WHEN","THAN","INTO","THEN","OVER","SOME","EACH",
    }
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(search_news, q, 6): q for q in searches}
        for future in as_completed(futures):
            for s in future.result():
                for m in ticker_re.finditer(s):
                    t = m.group(1)
                    if t not in noise and len(t) >= 2:
                        found[t] = found.get(t, 0) + 1
    candidates = [t for t, c in sorted(found.items(), key=lambda x: -x[1]) if c >= 2 or t in SECTOR_MAP]
    return list(dict.fromkeys(BASE_WATCHLIST + candidates[:10]))[:25]

# ── Ollama JSON parsing ───────────────────────────────────────
def _parse_ollama_json(text):
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{"); end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass
    return None

# ── Ollama LLM scoring — compact prompt for speed ─────────────
def ollama_score(sym, tech_score, tech_details, news_snippets, sector):
    # Compact news: top 2 headlines, 120 chars each
    news_text = "\n".join(f"- {s[:120]}" for s in news_snippets[:2]) or "No news."

    prompt = (
        f"Stock: {sym} ({sector})\n"
        f"RSI:{tech_details.get('rsi','?')} RelVol:{tech_details.get('relvol','?')}x "
        f"VWAP:{'above' if 'vwap' in tech_details else 'below'} "
        f"EMA:{tech_details.get('ema_cross', tech_details.get('ema','no'))}\n"
        f"Tech:{tech_score}/60\n"
        f"News:\n{news_text}\n\n"
        f"Rate for intraday momentum. JSON only, no other text:\n"
        f'{{ "sentiment":"positive|negative|neutral","ai_score":<0-40>,"reason":"<1 sentence>" }}'
    )
    model = _active_model()
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": model, "prompt": prompt,
                  "stream": False, "options": {"temperature": 0.1, "num_predict": 80}},
            timeout=45,
        )
        text = r.json().get("response", "")
        data = _parse_ollama_json(text)
        if data:
            return (
                min(int(data.get("ai_score", 0)), 40),
                data.get("sentiment", "neutral"),
                str(data.get("reason", ""))[:150],
            )
    except Exception as e:
        log(f"  Ollama error {sym}: {e}")
    return 0, "neutral", ""

def premarket_gap(sym):
    try:
        bars = get_bars(sym, "1Day", 3)
        if len(bars) >= 2:
            return round((bars[-1]["o"] - bars[-2]["c"]) / bars[-2]["c"] * 100, 2)
    except Exception:
        pass
    return 0.0

def ollama_available():
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        models = [m["name"] for m in r.json().get("models", [])]
        active = _active_model()
        # Accept if active model is installed, or any qwen2.5 as fallback
        ok = any(active in m or "qwen2.5" in m for m in models)
        return ok, models
    except Exception:
        return False, []

# ── Analyse one ticker (called in thread pool) ────────────────
NEG_KEYWORDS = [
    "lawsuit","fraud","bankruptcy","investigation","downgrade",
    "miss","recall","fda reject","accounting","scandal","halt",
]

def analyse_ticker(sym, ollama_ok, in_premarket):
    """Returns a pick dict or None. Thread-safe."""
    t_score, t_details = technical_score(sym)
    if t_score == 0:
        return sym, None, None, f"technical fail {t_details}"

    snippets = search_news(f"{sym} stock news today", 4)

    if snippets and any(kw in " ".join(snippets).lower() for kw in NEG_KEYWORDS):
        return sym, None, "negative_news", "negative news"

    sector = SECTOR_MAP.get(sym, "Other")
    if ollama_ok:
        ai_score, sentiment, reason = ollama_score(sym, t_score, t_details, snippets, sector)
    else:
        ai_score  = min(int(t_score * 0.3), 18)
        sentiment = "neutral"
        reason    = "Technical only (AI offline)"

    total = t_score + ai_score
    gap   = premarket_gap(sym)
    if gap >= 2.0: total += 5; reason = reason + f" Gap +{gap:.1f}%."
    if gap >= 5.0: total += 5

    threshold = 45 if in_premarket else 50   # lowered to match bot's min_confidence=45
    status = f"tech={t_score} ai={ai_score} total={total} {sentiment}"

    if total >= threshold and sentiment != "negative":
        pick = {
            "date":               now_et().strftime("%Y-%m-%d"),
            "symbol":             sym,
            "confidence":         min(total, 100),
            "sector":             sector,
            "reason":             reason or t_details.get("ema_cross", "momentum setup"),
            "news_sentiment":     sentiment,
            "has_earnings":       False,
            "rel_volume":         t_details.get("relvol", 1.0),
            "pre_market_gap":     gap,
            "recommended_action": "buy",
            "analyst_notes":      f"Tech {t_score}/60 | AI {ai_score}/40 | RSI {t_details.get('rsi','?')}",
        }
        return sym, pick, None, status
    return sym, None, None, status

# ── Main scan ─────────────────────────────────────────────────
def run_scan(force=False):
    in_market    = market_open()
    in_premarket = premarket_window()

    if not force and not in_market and not in_premarket:
        log("Market closed — skipping scan")
        return

    scan_type = "pre-market" if in_premarket and not in_market else "intraday"
    today     = now_et().strftime("%Y-%m-%d")
    t0        = time.time()
    log(f"=== Starting {scan_type} scan ({today}) ===")

    ollama_ok, models = ollama_available()
    if not ollama_ok:
        log(f"WARNING: Ollama unavailable — technical-only scoring")
    else:
        log(f"Ollama OK: {models}")

    log("Fetching movers from web…")
    watchlist = get_movers()
    log(f"Watchlist ({len(watchlist)}): {watchlist}")

    picks = []
    neg_tickers = {}
    candidates = [s for s in watchlist if s not in ("SPY", "QQQ", "VIXY")]

    # ── Parallel analysis ─────────────────────────────────────
    # 4 workers chosen for 2-core server + qwen2.5:1.5b warm path.
    # With 1.5b @ ~5s warm: 12 stocks / 4 = 3 batches × 5s ≈ 15s Ollama.
    # Total scan target: <60s. Configurable via SCAN_WORKERS env.
    workers = int(os.environ.get("SCAN_WORKERS", "4"))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(analyse_ticker, sym, ollama_ok, in_premarket): sym
            for sym in candidates
        }
        for future in as_completed(futures):
            sym = futures[future]
            try:
                sym, pick, flag, status = future.result()
                log(f"  {sym:6s} {status}")
                if flag == "negative_news":
                    neg_tickers[sym] = "negative news detected"
                elif pick:
                    picks.append(pick)
            except Exception as e:
                log(f"  {sym} error: {e}")

    picks.sort(key=lambda x: -x["confidence"])

    # ── Merge with existing today's picks (best confidence wins) ──────────
    # This ensures morning picks are not erased when afternoon scan yields fewer results.
    today = now_et().strftime("%Y-%m-%d")
    existing_picks = []
    try:
        with open(PICKS_FILE) as f:
            existing_picks = [p for p in json.load(f) if p.get("date") == today]
    except Exception:
        pass

    pick_map = {p["symbol"]: p for p in existing_picks}
    for pick in picks:
        sym = pick["symbol"]
        if sym not in pick_map or pick["confidence"] >= pick_map[sym]["confidence"]:
            pick_map[sym] = pick
    merged = sorted(pick_map.values(), key=lambda x: -x["confidence"])[:10]

    with open(PICKS_FILE, "w") as f:
        json.dump(merged, f, indent=2)

    elapsed = round(time.time() - t0, 1)
    log(f"Wrote {len(merged)} picks ({len(picks)} new, {len(existing_picks)} existing) in {elapsed}s")
    # Persist scan stats so dashboard can show health (target: <60s)
    try:
        stats_f = os.path.join(BASE_DIR, "scan_stats.json")
        with open(stats_f, "w") as sf:
            json.dump({
                "last_scan": now_et().isoformat(),
                "duration_sec": elapsed,
                "picks": len(picks),
                "watchlist_size": len(watchlist),
                "model": _active_model(),
                "scan_type": scan_type,
            }, sf, indent=2)
    except Exception:
        pass
    for p in picks:
        log(f"  {p['symbol']:6s} conf={p['confidence']} | {p['reason'][:60]}")

    with open(NEG_NEWS_F, "w") as f:
        json.dump({"date": today, "tickers": list(neg_tickers.keys()), "reasons": neg_tickers}, f, indent=2)
    if neg_tickers:
        log(f"Kill-switch: {list(neg_tickers.keys())}")

    log("=== Scan complete ===\n")

# ── Scheduler ─────────────────────────────────────────────────
def main():
    force = "--force" in sys.argv
    log(f"Scanner starting (model: {_active_model()}, workers: {os.environ.get('SCAN_WORKERS','4')}"
        f"{'  [FORCE]' if force else ''})")

    # On startup: if we're inside the pre-market or market window, run immediately
    # (catches scenario where bot restart happens after 09:00 and before 09:30).
    if force or premarket_window() or market_open():
        run_scan(force=True)
    else:
        log("Outside market hours — first scan deferred to schedule")

    schedule.every(15).minutes.do(run_scan)
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday"):
        getattr(schedule.every(), day).at("09:00").do(run_scan, force=True)
        getattr(schedule.every(), day).at("09:15").do(run_scan, force=True)  # second pre-mkt pass

    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    main()
