from flask import Flask, jsonify, request, Response
from flask_cors import CORS
from functools import wraps
import json, os, subprocess, time, threading
from datetime import datetime
import pytz

app = Flask(__name__)
CORS(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Load .env
_env_file = os.path.join(BASE_DIR, ".env")
try:
    from dotenv import load_dotenv
    load_dotenv(_env_file)
except ImportError:
    if os.path.exists(_env_file):
        for _l in open(_env_file):
            _l = _l.strip()
            if _l and not _l.startswith("#") and "=" in _l:
                k, v = _l.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

STATE_F = os.path.join(BASE_DIR, "bot_state.json")
LOG_F   = os.path.join(BASE_DIR, "trade_log.json")
PICKS_F = os.path.join(BASE_DIR, "claude_picks.json")
STRAT_F = os.path.join(BASE_DIR, "strategy_params.json")
ET      = pytz.timezone("America/New_York")

# ── HTTP Basic Auth (fail-closed by default) ─────────────────────
# To intentionally disable auth (NOT recommended), set DASHBOARD_AUTH_DISABLED=1.
# If DASHBOARD_PASS is empty AND override is not set, all endpoints return 503.
def _require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        pw = os.environ.get("DASHBOARD_PASS", "")
        disabled = os.environ.get("DASHBOARD_AUTH_DISABLED", "").lower() in ("1", "true", "yes")
        if not pw:
            if disabled:
                return f(*args, **kwargs)
            # Fail-closed: refuse to serve until password is set
            return Response(
                "Server misconfigured: DASHBOARD_PASS not set. "
                "Set the env var or DASHBOARD_AUTH_DISABLED=1 to override.",
                503,
            )
        auth = request.authorization
        user = os.environ.get("DASHBOARD_USER", "admin")
        if not auth or auth.username != user or auth.password != pw:
            return Response(
                "Authentication required.",
                401,
                {"WWW-Authenticate": 'Basic realm="AlgoTrader"'},
            )
        return f(*args, **kwargs)
    return decorated

# ── Helpers ───────────────────────────────────────────────────────
def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}

def write_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def _build_curve(today_sells, budget=333):
    running = 0.0
    curve = []
    for t in sorted(today_sells, key=lambda x: str(x.get("time", ""))):
        pnl = t.get("pnl_abs") or (t.get("pct", 0) / 100.0 * budget)
        running += pnl
        ts = str(t.get("time", ""))
        curve.append({"time": ts[11:16] if len(ts) > 15 else ts, "pnl": round(running, 2)})
    return curve

def build_data():
    s       = read_json(STATE_F, {})
    picks   = read_json(PICKS_F, [])
    all_log = read_json(LOG_F, [])
    params  = read_json(STRAT_F, {})

    today        = datetime.now(ET).strftime("%Y-%m-%d")
    today_picks  = [p for p in picks if p.get("date") == today]
    today_trades = [t for t in all_log if str(t.get("time", "")).startswith(today)]

    sells  = [t for t in today_trades if t.get("action") == "sell"]
    wins   = sum(1 for t in sells if t.get("pct", 0) > 0)
    losses = sum(1 for t in sells if t.get("pct", 0) <= 0)
    total  = len(sells)
    realized_pnl_today = round(sum(float(t.get("pnl_abs") or 0) for t in sells), 2)
    unrealized_pnl     = round(sum(
        (float(pos.get("curr", 0)) - float(pos.get("entry", 0))) * float(pos.get("qty", 0))
        for pos in s.get("positions", [])
    ), 2)
    avg_win  = (sum(t.get("pct", 0) for t in sells if t.get("pct", 0) > 0) / wins)  if wins   else 0.0
    avg_loss = (sum(t.get("pct", 0) for t in sells if t.get("pct", 0) <= 0) / losses) if losses else 0.0

    equity    = s.get("equity", 100000)
    daily_pnl = s.get("daily_pnl", 0)
    budget    = params.get("budget_per_trade", 333)
    curve     = _build_curve(sells, budget)

    return {
        "timestamp": datetime.now(ET).strftime("%H:%M:%S ET"),
        "metrics": {
            "equity":       round(equity, 2),
            "buying_power": round(s.get("buying_power", 0), 2),
            "daily_pnl":      round(daily_pnl, 2),
            "daily_pnl_pct":  round(daily_pnl / equity * 100, 3) if equity else 0,
            "realized_pnl":   realized_pnl_today,
            "unrealized_pnl": unrealized_pnl,
            "trades_count": s.get("daily_trades", len(today_trades)),
            "open_positions": len(s.get("positions", [])),
            "vix":          s.get("vix"),
            "regime":       s.get("regime", "unknown"),
            "paused":       s.get("trading_paused", False),
            "pause_reason": s.get("pause_reason", ""),
            "started":      s.get("started"),
            "last_scan":    s.get("last_scan"),
        },
        "stats": {
            "total_trades": len(today_trades),
            "wins":         wins,
            "losses":       losses,
            "win_rate":     round(wins / total * 100) if total else 0,
            "avg_win":      round(avg_win, 2),
            "avg_loss":     round(avg_loss, 2),
        },
        "picks":        today_picks,
        "trades":       today_trades[-25:][::-1],
        "positions":    s.get("positions", []),
        "activity_log": s.get("log", [])[:50],
        "curve":        curve,
        "params":       params,
    }

# ── Routes ────────────────────────────────────────────────────────
@app.route("/")
@_require_auth
def index():
    path = os.path.join(BASE_DIR, "templates", "react_index.html")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return "Dashboard template not found.", 503

@app.route("/api/data")
@_require_auth
def api_data():
    return jsonify(build_data())

@app.route("/api/stream")
@_require_auth
def api_stream():
    """Server-Sent Events — pushes a snapshot every 5 s."""
    def generate():
        while True:
            try:
                payload = json.dumps(build_data())
                yield f"event: snapshot\ndata: {payload}\n\n"
            except Exception:
                pass
            time.sleep(5)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.route("/api/health")
@_require_auth
def api_health():
    # Real supervisor service names on this server
    svc_names = {
        "trading-bot": "trading-bot",
        "scanner":     "scanner",
        "dashboard":   "dashboard",
    }
    svcs = {}
    for label, name in svc_names.items():
        try:
            out = subprocess.check_output(
                ["supervisorctl", "status", name],
                stderr=subprocess.DEVNULL, timeout=3,
            ).decode()
            svcs[label] = "running" if "RUNNING" in out else "stopped"
        except Exception:
            svcs[label] = "unknown"

    ollama = "offline"
    try:
        import requests as _req
        r = _req.get(
            os.environ.get("OLLAMA_URL", "http://localhost:11434") + "/api/tags",
            timeout=2,
        )
        ollama = "online" if r.status_code == 200 else "error"
    except Exception:
        pass

    return jsonify({"services": svcs, "ollama": ollama})

@app.route("/api/history")
@_require_auth
def api_history():
    """
    Filterable trade history.
      ?range=day   → today only
      ?range=week  → last 7 days
      ?range=month → last 30 days
      ?range=all   → everything in trade_log.json
      ?from=YYYY-MM-DD&to=YYYY-MM-DD → custom range
    Returns aggregated daily P&L + per-trade list, both realized.
    """
    from datetime import timedelta
    rng   = request.args.get("range", "day")
    today = datetime.now(ET).date()

    # Determine date window
    if request.args.get("from"):
        try:
            d_from = datetime.strptime(request.args["from"], "%Y-%m-%d").date()
            d_to   = datetime.strptime(request.args.get("to", str(today)), "%Y-%m-%d").date()
        except Exception:
            return jsonify({"error": "bad date format"}), 400
    else:
        spans = {"day": 0, "week": 6, "month": 29, "all": 9999}
        d_from = today - timedelta(days=spans.get(rng, 0))
        d_to   = today

    log = read_json(LOG_F, [])
    in_range = []
    for t in log:
        ts = str(t.get("time", ""))[:10]
        try:
            dt = datetime.strptime(ts, "%Y-%m-%d").date()
        except Exception:
            continue
        if d_from <= dt <= d_to:
            in_range.append(t)

    # Realized P&L per day
    sells = [t for t in in_range if t.get("action") == "sell"]
    by_day = {}
    for t in sells:
        d = str(t.get("time", ""))[:10]
        e = by_day.setdefault(d, {"date": d, "pnl_abs": 0.0, "trades": 0,
                                   "wins": 0, "losses": 0, "best": 0, "worst": 0})
        pnl = float(t.get("pnl_abs") or 0)
        pct = float(t.get("pct") or 0)
        e["pnl_abs"] += pnl
        e["trades"] += 1
        if pct > 0: e["wins"] += 1
        else:       e["losses"] += 1
        e["best"]  = max(e["best"], pct)
        e["worst"] = min(e["worst"], pct)
    daily = sorted(by_day.values(), key=lambda x: x["date"])
    for d in daily:
        d["pnl_abs"] = round(d["pnl_abs"], 2)
        d["win_rate"] = round(d["wins"] / d["trades"] * 100, 1) if d["trades"] else 0

    # Monthly aggregate (for /history?range=all dashboards)
    by_month = {}
    for d in daily:
        m = d["date"][:7]
        e = by_month.setdefault(m, {"month": m, "pnl_abs": 0.0, "trades": 0, "wins": 0})
        e["pnl_abs"] += d["pnl_abs"]
        e["trades"]  += d["trades"]
        e["wins"]    += d["wins"]
    monthly = sorted(by_month.values(), key=lambda x: x["month"])
    for m in monthly:
        m["pnl_abs"]  = round(m["pnl_abs"], 2)
        m["win_rate"] = round(m["wins"] / m["trades"] * 100, 1) if m["trades"] else 0

    total_pnl = round(sum(float(t.get("pnl_abs") or 0) for t in sells), 2)

    # Unrealized = current open positions from live state
    state = read_json(STATE_F, {})
    unrealized = sum(
        (float(p.get("curr", 0)) - float(p.get("entry", 0))) * float(p.get("qty", 0))
        for p in state.get("positions", [])
    )

    return jsonify({
        "range": rng,
        "from": str(d_from),
        "to":   str(d_to),
        "summary": {
            "total_trades":   len([t for t in in_range if t.get("action") == "buy"]),
            "closed_trades":  len(sells),
            "realized_pnl":   total_pnl,
            "unrealized_pnl": round(unrealized, 2),
            "wins":           sum(1 for t in sells if float(t.get("pct",0)) > 0),
            "losses":         sum(1 for t in sells if float(t.get("pct",0)) <= 0),
            "win_rate":       round(sum(1 for t in sells if float(t.get("pct",0)) > 0)
                                     / max(len(sells), 1) * 100, 1),
        },
        "daily":   daily,
        "monthly": monthly,
        "trades":  in_range[-200:],
    })

@app.route("/api/reports")
@_require_auth
def api_reports():
    """List all available daily HTML reports."""
    rdir = os.path.join(BASE_DIR, "reports")
    if not os.path.isdir(rdir):
        return jsonify([])
    out = []
    for f in sorted(os.listdir(rdir), reverse=True):
        if f.startswith("trading_report_") and f.endswith(".html"):
            out.append({
                "date": f.replace("trading_report_", "").replace(".html", ""),
                "url":  f"/api/reports/{f}",
            })
    return jsonify(out)

@app.route("/api/reports/<path:fname>")
@_require_auth
def api_report_file(fname):
    rdir = os.path.join(BASE_DIR, "reports")
    safe = os.path.basename(fname)  # prevent path traversal
    fp = os.path.join(rdir, safe)
    if not os.path.exists(fp) or not safe.startswith("trading_report_"):
        return "Not found", 404
    with open(fp, encoding="utf-8") as f:
        return f.read()

@app.route("/api/scan_stats")
@_require_auth
def api_scan_stats():
    return jsonify(read_json(os.path.join(BASE_DIR, "scan_stats.json"), {}))

@app.route("/api/equity")
@_require_auth
def api_equity():
    """Equity curve over time — daily snapshots from intraday_bot_v2."""
    h = read_json(os.path.join(BASE_DIR, "equity_history.json"), [])
    if not h: return jsonify({"history": [], "peak": 0, "current": 0, "drawdown_pct": 0})
    cur  = h[-1]["equity"]
    peak = max(e["equity"] for e in h)
    dd   = (cur - peak) / peak * 100 if peak else 0
    return jsonify({"history": h, "peak": peak, "current": cur,
                    "drawdown_pct": round(dd, 2)})

@app.route("/api/analytics")
@_require_auth
def api_analytics():
    """Sharpe, Sortino, max drawdown, win/loss expectancy, signal attribution."""
    import math
    log = read_json(LOG_F, [])
    sells = [t for t in log if t.get("action") == "sell"]
    pcts  = [float(t.get("pct", 0)) for t in sells if t.get("pct") is not None]

    if not pcts:
        return jsonify({"error": "no closed trades yet"})

    mean = sum(pcts) / len(pcts)
    var  = sum((x - mean) ** 2 for x in pcts) / len(pcts)
    sd   = math.sqrt(var) if var else 0
    # Sharpe: assume risk-free 0; annualize ~252 trading days, ~5 trades/day -> 1260
    sharpe = (mean / sd) * math.sqrt(1260) if sd else 0

    neg_pcts = [x for x in pcts if x < 0]
    downside_var = sum(x ** 2 for x in neg_pcts) / len(pcts) if neg_pcts else 0
    downside_sd  = math.sqrt(downside_var) if downside_var else 0
    sortino = (mean / downside_sd) * math.sqrt(1260) if downside_sd else 0

    # Max drawdown of the cumulative trade-pct curve
    cum = 0; peak = 0; mdd = 0
    for x in pcts:
        cum += x
        peak = max(peak, cum)
        mdd  = min(mdd, cum - peak)

    wins   = [x for x in pcts if x > 0]
    losses = [x for x in pcts if x <= 0]
    win_rate = len(wins) / len(pcts) * 100
    avg_win  = sum(wins)   / len(wins)   if wins   else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    expectancy = (win_rate/100 * avg_win) + ((1 - win_rate/100) * avg_loss)
    profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else 0

    # Signal attribution: which entry signal contributed most?
    buys = [t for t in log if t.get("action") == "buy"]
    sells_by_sym_date = {}
    for s in sells:
        key = (s.get("sym"), str(s.get("time", ""))[:10])
        sells_by_sym_date.setdefault(key, []).append(s)
    sig_stats = {}
    for b in buys:
        key = (b.get("sym"), str(b.get("time", ""))[:10])
        if key not in sells_by_sym_date: continue
        sells_for = sells_by_sym_date[key]
        avg_pct = sum(float(s.get("pct", 0)) for s in sells_for) / len(sells_for)
        for sig_name in (b.get("reasons") or {}).keys():
            d = sig_stats.setdefault(sig_name, {"trades": 0, "wins": 0, "pnl": 0.0})
            d["trades"] += 1
            d["pnl"]    += avg_pct
            if avg_pct > 0: d["wins"] += 1
    sig_table = sorted([
        {"signal": k, "trades": v["trades"],
         "win_rate": round(v["wins"]/v["trades"]*100, 1),
         "avg_pct": round(v["pnl"]/v["trades"], 2)}
        for k, v in sig_stats.items() if v["trades"] >= 1
    ], key=lambda x: -x["avg_pct"])

    # Slippage stats
    slips = [float(t.get("slippage_pct") or 0) for t in buys if t.get("slippage_pct") is not None]
    avg_slip = round(sum(slips)/len(slips), 3) if slips else 0

    return jsonify({
        "sharpe_ratio":  round(sharpe, 2),
        "sortino_ratio": round(sortino, 2),
        "max_drawdown":  round(mdd, 2),
        "profit_factor": round(profit_factor, 2),
        "expectancy":    round(expectancy, 3),
        "total_trades":  len(pcts),
        "win_rate":      round(win_rate, 1),
        "avg_win":       round(avg_win, 2),
        "avg_loss":      round(avg_loss, 2),
        "avg_slippage_pct": avg_slip,
        "signal_attribution": sig_table,
    })

@app.route("/api/export.csv")
@_require_auth
def api_export_csv():
    """Download all trades as CSV (for tax/audit)."""
    import csv, io
    log = read_json(LOG_F, [])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "symbol", "action", "qty", "price", "entry_price",
                "pct", "pnl_abs", "slippage_pct", "reason", "score", "regime"])
    for t in log:
        w.writerow([t.get("time"), t.get("sym"), t.get("action"),
                    t.get("qty"), t.get("price"), t.get("entry_price"),
                    t.get("pct"), t.get("pnl_abs"), t.get("slippage_pct"),
                    t.get("reason") or ",".join((t.get("reasons") or {}).keys()),
                    t.get("score"), t.get("regime")])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=trades.csv"})

@app.route("/disclaimer")
def disclaimer():
    """Terms & disclaimer page (no auth — must be public)."""
    path = os.path.join(BASE_DIR, "templates", "disclaimer.html")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return "Disclaimer page missing.", 404

@app.route("/api/config", methods=["GET"])
@_require_auth
def api_config_get():
    return jsonify(read_json(STRAT_F, {}))

@app.route("/api/config", methods=["POST"])
@_require_auth
def api_config_post():
    updates = request.get_json(force=True) or {}
    current = read_json(STRAT_F, {})
    current.update(updates)
    write_json(STRAT_F, current)
    return jsonify({"ok": True, "params": current})

@app.route("/api/action", methods=["POST"])
@_require_auth
def api_action():
    action = (request.get_json(force=True) or {}).get("action", "")

    cmd_map = {
        "start":   "supervisorctl start trading-bot",
        "stop":    "supervisorctl stop trading-bot",
        "restart": "supervisorctl restart trading-bot",
        "scan":    "supervisorctl restart scanner",
    }

    if action in cmd_map:
        try:
            subprocess.run(cmd_map[action].split(), capture_output=True, timeout=5)
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    if action == "close_all":
        _close_all()
        return jsonify({"status": "success"})

    return jsonify({"status": "error", "message": "Unknown action"}), 400

def _close_all():
    import requests as _req
    try:
        h = {
            "APCA-API-KEY-ID":     os.environ.get("ALPACA_API_KEY", ""),
            "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
        }
        base = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")
        _req.delete(f"{base}/positions", headers=h, timeout=10)
    except Exception:
        pass

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
