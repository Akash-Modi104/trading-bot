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

# ── Optional HTTP Basic Auth ──────────────────────────────────────
def _require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        pw = os.environ.get("DASHBOARD_PASS", "")
        if not pw:
            return f(*args, **kwargs)
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
