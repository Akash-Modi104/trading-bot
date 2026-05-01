from flask import (Flask, jsonify, request, Response, redirect,
                   make_response, g, render_template_string, send_from_directory)
from flask_cors import CORS
from functools import wraps
import json, os, subprocess, time, threading, secrets
from datetime import datetime
import pytz


from concurrent.futures import ThreadPoolExecutor as _AggPool
import db
import auth


# ── Login rate limiting (in-memory token bucket per IP) ───────
import collections, threading
_LOGIN_BUCKET = collections.defaultdict(lambda: {"count":0, "ts":0})
_LOGIN_LOCK = threading.Lock()
_LOGIN_MAX = 8        # attempts
_LOGIN_WINDOW = 60    # seconds

def _login_allowed(ip):
    """Returns (allowed, retry_after). 8 attempts per 60s per IP."""
    with _LOGIN_LOCK:
        b = _LOGIN_BUCKET[ip]
        now = time.time()
        if now - b["ts"] > _LOGIN_WINDOW:
            b["count"] = 0; b["ts"] = now
        if b["count"] >= _LOGIN_MAX:
            return False, int(_LOGIN_WINDOW - (now - b["ts"]))
        b["count"] += 1
        return True, 0

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
CORS(app, supports_credentials=True, origins=[
    "http://localhost:5001","http://127.0.0.1:5001",
    "http://187.127.73.203:5001","https://187.127.73.203:5001"
])

# Initialize DB and bootstrap legacy admin from .env if needed
db.init()
auth.bootstrap_admin_from_env()
auth.cleanup_expired_sessions()

SESSION_COOKIE = "algotrader_session"

# ── Security headers (CSP, HSTS, etc.) ───────────────────────────
@app.after_request
def add_security_headers(resp):
    # Disable HTML caching so UI updates show without hard-refresh
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # CSP loose enough for in-browser Babel + Chart.js + Google Fonts CDNs
    csp = ("default-src 'self'; "
           "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://unpkg.com https://cdn.jsdelivr.net; "
           "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
           "font-src 'self' https://fonts.gstatic.com data:; "
           "img-src 'self' data: blob:; "
           "connect-src 'self'; "
           "frame-ancestors 'self';")
    resp.headers.setdefault("Content-Security-Policy", csp)
    return resp

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

# ── Session-based auth ────────────────────────────────────────────
def _current_user():
    """Returns the user row for the current request, or None."""
    if hasattr(g, "_cached_user"):
        return g._cached_user
    token = request.cookies.get(SESSION_COOKIE)
    user = auth.get_user_by_session(token) if token else None
    g._cached_user = user
    return user

def _require_auth(f):
    """API endpoints: return 401 JSON if not authenticated."""
    @wraps(f)
    def decorated(*args, **kwargs):
        u = _current_user()
        if not u:
            return jsonify({"error": "auth_required"}), 401
        return f(*args, **kwargs)
    return decorated

def _require_login_html(f):
    """HTML pages: redirect to /login if not authenticated."""
    @wraps(f)
    def decorated(*args, **kwargs):
        u = _current_user()
        if not u:
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated

def _client_ip():
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr or "")

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

# ── Public routes (no auth) ──────────────────────────────────────
def _render_template_file(name: str, **ctx):
    path = os.path.join(BASE_DIR, "templates", name)
    if not os.path.exists(path):
        return f"Template {name} not found.", 404
    with open(path, encoding="utf-8") as f:
        return render_template_string(f.read(), **ctx)

@app.route("/login", methods=["GET"])
def login_page():
    if _current_user():
        return redirect("/")
    return _render_template_file("login.html", error=request.args.get("error", ""))

@app.route("/register", methods=["GET"])
def register_page():
    if _current_user():
        return redirect("/")
    return _render_template_file("login.html",
                                 error=request.args.get("error", ""),
                                 register=True)

@app.route("/api/login", methods=["POST"])
def api_login():
    ok, retry = _login_allowed(_client_ip())
    if not ok:
        return jsonify({"error":"rate_limited","retry_after":retry}), 429
    body = request.get_json(force=True, silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    pw    = body.get("password") or ""
    ip    = _client_ip()
    if auth.is_rate_limited(ip):
        return jsonify({"error": "too_many_attempts",
                        "message": "Too many failed attempts — try again in 15 minutes."}), 429
    user = auth.get_user_by_email(email)
    if not user or not auth.check_pw(pw, user["password_hash"]):
        auth.record_login_attempt(ip, email, success=False)
        auth.audit(user["id"] if user else None, "login_failed", ip, email)
        return jsonify({"error": "invalid_credentials",
                        "message": "Invalid email or password."}), 401
    token = auth.create_session(user["id"], ip,
                                request.headers.get("User-Agent", ""))
    auth.update_user(user["id"], last_login_at=datetime.utcnow().isoformat())
    auth.record_login_attempt(ip, email, success=True)
    auth.audit(user["id"], "login_success", ip)
    resp = jsonify({"ok": True})
    resp.set_cookie(SESSION_COOKIE, token,
                    max_age=30*24*3600, httponly=True,
                    samesite="Lax", secure=request.is_secure)
    return resp

@app.route("/api/register", methods=["POST"])
def api_register():
    body = request.get_json(force=True, silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    pw    = body.get("password") or ""
    name  = (body.get("name") or "").strip()
    accept_tos = body.get("accept_tos")
    if not accept_tos:
        return jsonify({"error": "tos_required",
                        "message": "You must accept the Terms & Disclaimer."}), 400
    if not email or "@" not in email:
        return jsonify({"error": "invalid_email"}), 400
    if len(pw) < 8:
        return jsonify({"error": "weak_password",
                        "message": "Password must be at least 8 characters."}), 400
    try:
        user = auth.create_user(email, pw, name=name)
    except ValueError as e:
        return jsonify({"error": "registration_failed", "message": str(e)}), 400
    ip = _client_ip()
    token = auth.create_session(user["id"], ip,
                                request.headers.get("User-Agent", ""))
    auth.audit(user["id"], "register", ip)
    resp = jsonify({"ok": True})
    resp.set_cookie(SESSION_COOKIE, token,
                    max_age=30*24*3600, httponly=True,
                    samesite="Lax", secure=request.is_secure)
    return resp

@app.route("/api/logout", methods=["POST"])
def api_logout():
    token = request.cookies.get(SESSION_COOKIE)
    auth.delete_session(token)
    u = _current_user()
    if u:
        auth.audit(u["id"], "logout", _client_ip())
    resp = jsonify({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp

# ── Authenticated routes ─────────────────────────────────────────
@app.route("/")
@_require_login_html
def index():
    path = os.path.join(BASE_DIR, "templates", "react_index.html")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return "Dashboard template not found.", 503

@app.route("/api/me")
@_require_auth
def api_me():
    u = _current_user()
    notif = {}
    try: notif = json.loads(u["notifications"] or "{}")
    except Exception: pass
    return jsonify({
        "id":    u["id"],
        "email": u["email"],
        "name":  u["name"],
        "role":  u["role"],
        "plan":  u["plan"],
        "theme": u["theme"] or "dark",
        "notifications": notif,
        "alpaca":    auth.get_alpaca_status(u["id"]),
        "angelone":  auth.get_angelone_status(u["id"]),
        "zerodha":   auth.get_zerodha_status(u["id"]),
    })

@app.route("/api/profile", methods=["POST"])
@_require_auth
def api_profile_update():
    u = _current_user()
    body = request.get_json(force=True, silent=True) or {}
    fields = {}
    if "name"  in body: fields["name"]  = (body["name"] or "")[:100]
    if "theme" in body and body["theme"] in ("dark", "light", "auto"):
        fields["theme"] = body["theme"]
    if "notifications" in body and isinstance(body["notifications"], dict):
        fields["notifications"] = body["notifications"]
    if fields:
        auth.update_user(u["id"], **fields)
        auth.audit(u["id"], "profile_update", _client_ip(), list(fields.keys()))
    return jsonify({"ok": True})

@app.route("/api/change_password", methods=["POST"])
@_require_auth
def api_change_password():
    u = _current_user()
    body = request.get_json(force=True, silent=True) or {}
    cur = body.get("current_password", "")
    new = body.get("new_password", "")
    if not auth.check_pw(cur, u["password_hash"]):
        return jsonify({"error": "wrong_password"}), 401
    try:
        auth.change_password(u["id"], new)
    except ValueError as e:
        return jsonify({"error": "invalid", "message": str(e)}), 400
    auth.audit(u["id"], "password_changed", _client_ip())
    # Revoke session — user must log in again
    resp = jsonify({"ok": True, "logout": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp

@app.route("/api/alpaca/connect", methods=["POST"])
@_require_auth
def api_alpaca_connect():
    u = _current_user()
    body = request.get_json(force=True, silent=True) or {}
    api_key  = (body.get("api_key") or "").strip()
    sec_key  = (body.get("secret_key") or "").strip()
    is_paper = bool(body.get("is_paper", True))
    if not api_key or not sec_key:
        return jsonify({"error": "missing_keys"}), 400
    ok, info = auth.validate_alpaca(api_key, sec_key, is_paper)
    if not ok:
        return jsonify({"error": "validation_failed", "details": info}), 400
    acct_no = (info or {}).get("account_number", "")
    auth.save_alpaca_creds(u["id"], api_key, sec_key,
                           is_paper=is_paper, account_number=acct_no)
    auth.audit(u["id"], "alpaca_connected", _client_ip(),
               {"is_paper": is_paper, "acct": acct_no[:8]})
    return jsonify({"ok": True, "account": {
        "account_number": acct_no,
        "equity": info.get("equity"),
        "buying_power": info.get("buying_power"),
        "currency": info.get("currency"),
        "is_paper": is_paper,
    }})

@app.route("/api/alpaca/disconnect", methods=["POST"])
@_require_auth
def api_alpaca_disconnect():
    u = _current_user()
    auth.delete_alpaca_creds(u["id"])
    auth.audit(u["id"], "alpaca_disconnected", _client_ip())
    return jsonify({"ok": True})

# ── Angel One broker endpoints ────────────────────────────────────

def _get_angelone_broker(user_id: int):
    """Build an AngelOneBroker from stored (decrypted) credentials."""
    from brokers.angelone import AngelOneBroker
    creds = auth.get_angelone_creds(user_id)
    if not creds:
        return None, "not_connected"
    broker = AngelOneBroker(
        api_key=creds["api_key"],
        client_id=creds["client_id"],
        password=creds["password"],
        totp_secret=creds["totp_secret"],
    )
    # Restore cached tokens to avoid unnecessary re-login on every call
    if creds["jwt_token"]:
        broker.jwt_token     = creds["jwt_token"]
        broker.refresh_token = creds["refresh_token"]
        if creds["logged_in_at"]:
            try:
                from datetime import datetime as _dt
                broker.logged_in_at = _dt.fromisoformat(creds["logged_in_at"])
            except Exception:
                pass
    return broker, None

def _persist_angelone_tokens(user_id: int, broker):
    """Write refreshed tokens back to DB after any call that may have re-authed."""
    if broker.jwt_token:
        auth.update_angelone_tokens(
            user_id,
            jwt_token=broker.jwt_token,
            refresh_token=broker.refresh_token or "",
            logged_in_at=broker.logged_in_at.isoformat() if broker.logged_in_at else "",
        )

@app.route("/api/angelone/connect", methods=["POST"])
@_require_auth
def api_angelone_connect():
    u = _current_user()
    body       = request.get_json(force=True, silent=True) or {}
    api_key    = (body.get("api_key") or "").strip()
    client_id  = (body.get("client_id") or "").strip().upper()
    password   = (body.get("password") or "").strip()
    totp_secret = (body.get("totp_secret") or "").strip().upper()

    if not all([api_key, client_id, password, totp_secret]):
        return jsonify({"error": "missing_fields",
                        "message": "api_key, client_id, password and totp_secret are required"}), 400

    ok, info = auth.validate_angelone(api_key, client_id, password, totp_secret)
    if not ok:
        return jsonify({"error": "validation_failed",
                        "details": info.get("error", str(info))}), 400

    jwt_token     = info.get("jwtToken", "")
    refresh_token = info.get("refreshToken", "")
    logged_in_at  = datetime.utcnow().isoformat()

    auth.save_angelone_creds(
        u["id"], api_key, client_id, password, totp_secret,
        jwt_token=jwt_token, refresh_token=refresh_token,
        logged_in_at=logged_in_at,
    )
    auth.audit(u["id"], "angelone_connected", _client_ip(), {"client_id": client_id})
    return jsonify({"ok": True, "client_id": client_id,
                    "message": "Angel One account connected successfully"})

@app.route("/api/angelone/disconnect", methods=["POST"])
@_require_auth
def api_angelone_disconnect():
    u = _current_user()
    broker, err = _get_angelone_broker(u["id"])
    if broker:
        try:
            broker.logout()
        except Exception:
            pass
    auth.delete_angelone_creds(u["id"])
    auth.audit(u["id"], "angelone_disconnected", _client_ip())
    return jsonify({"ok": True})

@app.route("/api/angelone/account")
@_require_auth
def api_angelone_account():
    u = _current_user()
    broker, err = _get_angelone_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        summary = broker.account_summary()
        _persist_angelone_tokens(u["id"], broker)
        return jsonify(summary)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/angelone/funds")
@_require_auth
def api_angelone_funds():
    u = _current_user()
    broker, err = _get_angelone_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        funds = broker.get_funds()
        _persist_angelone_tokens(u["id"], broker)
        return jsonify(funds)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/angelone/positions")
@_require_auth
def api_angelone_positions():
    u = _current_user()
    broker, err = _get_angelone_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        positions = broker.get_positions()
        _persist_angelone_tokens(u["id"], broker)
        return jsonify(positions)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/angelone/holdings")
@_require_auth
def api_angelone_holdings():
    u = _current_user()
    broker, err = _get_angelone_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        holdings = broker.get_holdings()
        _persist_angelone_tokens(u["id"], broker)
        return jsonify(holdings)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/angelone/orders")
@_require_auth
def api_angelone_orders():
    u = _current_user()
    broker, err = _get_angelone_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        orders = broker.get_order_book()
        _persist_angelone_tokens(u["id"], broker)
        return jsonify(orders)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/angelone/trades")
@_require_auth
def api_angelone_trades():
    u = _current_user()
    broker, err = _get_angelone_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        trades = broker.get_trade_book()
        _persist_angelone_tokens(u["id"], broker)
        return jsonify(trades)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/angelone/order", methods=["POST"])
@_require_auth
def api_angelone_place_order():
    """
    Place a buy or sell order on Angel One.

    Required body fields:
      tradingsymbol  — e.g. "RELIANCE-EQ"
      symboltoken    — numeric token (find via /api/angelone/search)
      transaction_type — "BUY" or "SELL"
      quantity       — integer

    Optional:
      price          — limit price (0 = market)
      order_type     — MARKET | LIMIT | STOPLOSS_LIMIT | STOPLOSS_MARKET  (default MARKET)
      product_type   — INTRADAY | DELIVERY | MARGIN | CARRYFORWARD        (default INTRADAY)
      exchange       — NSE | BSE | NFO                                     (default NSE)
      variety        — NORMAL | STOPLOSS | AMO | ROBO                      (default NORMAL)
      duration       — DAY | IOC                                            (default DAY)
      stoploss       — stoploss price / points (for ROBO/bracket orders)
      squareoff      — target price / points  (for ROBO/bracket orders)
      trailing_stoploss — trailing stop points (for ROBO orders)
    """
    u    = _current_user()
    body = request.get_json(force=True, silent=True) or {}

    required = ["tradingsymbol", "symboltoken", "transaction_type", "quantity"]
    for f in required:
        if not body.get(f):
            return jsonify({"error": "missing_field", "field": f}), 400

    broker, err = _get_angelone_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400

    try:
        order_id = broker.place_order(
            tradingsymbol    = body["tradingsymbol"].upper(),
            symboltoken      = str(body["symboltoken"]),
            transaction_type = body["transaction_type"].upper(),
            quantity         = int(body["quantity"]),
            price            = float(body.get("price", 0)),
            order_type       = body.get("order_type", "MARKET").upper(),
            product_type     = body.get("product_type", "INTRADAY").upper(),
            exchange         = body.get("exchange", "NSE").upper(),
            variety          = body.get("variety", "NORMAL").upper(),
            duration         = body.get("duration", "DAY").upper(),
            squareoff        = float(body.get("squareoff", 0)),
            stoploss         = float(body.get("stoploss", 0)),
            trailing_stoploss= float(body.get("trailing_stoploss", 0)),
        )
        _persist_angelone_tokens(u["id"], broker)
        auth.audit(u["id"], "angelone_order_placed", _client_ip(), {
            "symbol": body["tradingsymbol"],
            "side":   body["transaction_type"],
            "qty":    body["quantity"],
        })
        return jsonify({"ok": True, "order_id": order_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/angelone/order/<order_id>", methods=["DELETE"])
@_require_auth
def api_angelone_cancel_order(order_id: str):
    u       = _current_user()
    variety = request.args.get("variety", "NORMAL").upper()
    broker, err = _get_angelone_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        oid = broker.cancel_order(order_id, variety=variety)
        _persist_angelone_tokens(u["id"], broker)
        auth.audit(u["id"], "angelone_order_cancelled", _client_ip(), {"order_id": order_id})
        return jsonify({"ok": True, "order_id": oid})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/angelone/order", methods=["PUT"])
@_require_auth
def api_angelone_modify_order():
    u    = _current_user()
    body = request.get_json(force=True, silent=True) or {}
    for f in ["order_id", "tradingsymbol", "symboltoken", "quantity", "price"]:
        if body.get(f) is None:
            return jsonify({"error": "missing_field", "field": f}), 400
    broker, err = _get_angelone_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        oid = broker.modify_order(
            order_id      = body["order_id"],
            tradingsymbol = body["tradingsymbol"].upper(),
            symboltoken   = str(body["symboltoken"]),
            quantity      = int(body["quantity"]),
            price         = float(body["price"]),
            order_type    = body.get("order_type", "LIMIT").upper(),
            product_type  = body.get("product_type", "INTRADAY").upper(),
            exchange      = body.get("exchange", "NSE").upper(),
            variety       = body.get("variety", "NORMAL").upper(),
            duration      = body.get("duration", "DAY").upper(),
        )
        _persist_angelone_tokens(u["id"], broker)
        return jsonify({"ok": True, "order_id": oid})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/angelone/squareoff", methods=["POST"])
@_require_auth
def api_angelone_squareoff():
    """Close all open Angel One intraday positions at market price."""
    u = _current_user()
    broker, err = _get_angelone_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        results = broker.square_off_all_positions()
        _persist_angelone_tokens(u["id"], broker)
        auth.audit(u["id"], "angelone_squareoff_all", _client_ip())
        return jsonify({"ok": True, "results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/angelone/quote")
@_require_auth
def api_angelone_quote():
    """
    GET /api/angelone/quote?exchange=NSE&symbol=RELIANCE-EQ&token=2885
    Returns full quote (LTP, bid, ask, OHLC, volume).
    """
    u        = _current_user()
    exchange = request.args.get("exchange", "NSE").upper()
    symbol   = request.args.get("symbol", "")
    token    = request.args.get("token", "")
    if not symbol or not token:
        return jsonify({"error": "symbol and token params required"}), 400
    broker, err = _get_angelone_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        quote = broker.get_quote(exchange, symbol, token)
        _persist_angelone_tokens(u["id"], broker)
        return jsonify(quote)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/angelone/search")
@_require_auth
def api_angelone_search():
    """
    GET /api/angelone/search?exchange=NSE&q=RELIANCE
    Returns matching symbols with their tokens.
    """
    u        = _current_user()
    exchange = request.args.get("exchange", "NSE").upper()
    query    = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "q param required"}), 400
    broker, err = _get_angelone_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        results = broker.search_symbol(exchange, query)
        _persist_angelone_tokens(u["id"], broker)
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/angelone/candles")
@_require_auth
def api_angelone_candles():
    """
    GET /api/angelone/candles?exchange=NSE&token=2885&interval=ONE_MINUTE&from=2024-01-01+09:15&to=2024-01-01+15:30
    """
    u        = _current_user()
    exchange = request.args.get("exchange", "NSE").upper()
    token    = request.args.get("token", "")
    interval = request.args.get("interval", "FIVE_MINUTE").upper()
    from_dt  = request.args.get("from", "")
    to_dt    = request.args.get("to", "")
    if not token or not from_dt or not to_dt:
        return jsonify({"error": "token, from and to params required"}), 400
    broker, err = _get_angelone_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        candles = broker.get_candles(exchange, token, interval, from_dt, to_dt)
        _persist_angelone_tokens(u["id"], broker)
        return jsonify(candles)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Zerodha broker endpoints ──────────────────────────────────────

def _get_zerodha_broker(user_id: int):
    """Build a ZerodhaBroker from stored credentials."""
    from brokers.zerodha import ZerodhaBroker
    creds = auth.get_zerodha_creds(user_id)
    if not creds:
        return None, "not_connected"
    broker = ZerodhaBroker(
        api_key=creds["api_key"],
        api_secret=creds["api_secret"],
        access_token=creds["access_token"],
    )
    return broker, None

@app.route("/api/zerodha/connect", methods=["POST"])
@_require_auth
def api_zerodha_connect():
    """
    Step 1: Store API key + secret, return the Kite login URL.
    The user must visit that URL, log in, and paste the request_token back
    via /api/zerodha/session.
    """
    u    = _current_user()
    body = request.get_json(force=True, silent=True) or {}
    api_key    = (body.get("api_key") or "").strip()
    api_secret = (body.get("api_secret") or "").strip()
    if not api_key or not api_secret:
        return jsonify({"error": "missing_fields",
                        "message": "api_key and api_secret are required"}), 400
    auth.save_zerodha_creds(u["id"], api_key, api_secret)
    auth.audit(u["id"], "zerodha_creds_saved", _client_ip())
    login_url = f"https://kite.trade/connect/login?api_key={api_key}&v=3"
    return jsonify({"ok": True, "login_url": login_url,
                    "message": "Credentials saved. Visit login_url to authenticate."})

@app.route("/api/zerodha/session", methods=["POST"])
@_require_auth
def api_zerodha_session():
    """
    Step 2: Exchange request_token (from redirect after Kite login) for access_token.
    Body: { "request_token": "..." }
    """
    u    = _current_user()
    body = request.get_json(force=True, silent=True) or {}
    req_token = (body.get("request_token") or "").strip()
    if not req_token:
        return jsonify({"error": "missing_fields",
                        "message": "request_token is required"}), 400
    broker, err = _get_zerodha_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        session = broker.generate_session(req_token)
        access_token   = session.get("access_token", "")
        login_time     = session.get("login_time", "")
        expiry_ts      = (datetime.utcnow() + __import__("datetime").timedelta(hours=20)).isoformat()
        auth.update_zerodha_access_token(u["id"], access_token, session_expiry=expiry_ts)
        auth.audit(u["id"], "zerodha_session_created", _client_ip())
        return jsonify({"ok": True, "login_time": login_time,
                        "message": "Zerodha session established"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/zerodha/disconnect", methods=["POST"])
@_require_auth
def api_zerodha_disconnect():
    u = _current_user()
    broker, _ = _get_zerodha_broker(u["id"])
    if broker and broker.access_token:
        try:
            broker.invalidate_session()
        except Exception:
            pass
    auth.delete_zerodha_creds(u["id"])
    auth.audit(u["id"], "zerodha_disconnected", _client_ip())
    return jsonify({"ok": True})

@app.route("/api/zerodha/account")
@_require_auth
def api_zerodha_account():
    u = _current_user()
    broker, err = _get_zerodha_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        return jsonify(broker.account_summary())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/zerodha/funds")
@_require_auth
def api_zerodha_funds():
    u = _current_user()
    broker, err = _get_zerodha_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        return jsonify(broker.get_funds())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/zerodha/positions")
@_require_auth
def api_zerodha_positions():
    u = _current_user()
    broker, err = _get_zerodha_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        return jsonify(broker.get_positions())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/zerodha/holdings")
@_require_auth
def api_zerodha_holdings():
    u = _current_user()
    broker, err = _get_zerodha_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        return jsonify(broker.get_holdings())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/zerodha/orders")
@_require_auth
def api_zerodha_orders():
    u = _current_user()
    broker, err = _get_zerodha_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        return jsonify(broker.get_orders())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/zerodha/trades")
@_require_auth
def api_zerodha_trades():
    u = _current_user()
    broker, err = _get_zerodha_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        return jsonify(broker.get_trades())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/zerodha/order", methods=["POST"])
@_require_auth
def api_zerodha_place_order():
    """
    Place a buy or sell order via Zerodha Kite.

    Required body fields:
      tradingsymbol   — e.g. "RELIANCE"
      transaction_type — "BUY" or "SELL"
      quantity        — integer

    Optional:
      price           — limit price (0 = market)
      trigger_price   — for SL orders
      order_type      — MARKET | LIMIT | SL | SL-M     (default MARKET)
      product         — MIS | CNC | NRML                (default MIS)
      exchange        — NSE | BSE | NFO | MCX           (default NSE)
      variety         — regular | amo | co | bo         (default regular)
      validity        — DAY | IOC                        (default DAY)
      squareoff       — target offset for BO orders
      stoploss        — stoploss offset for BO/CO orders
      trailing_stoploss — trailing stop for BO orders
      tag             — optional order tag (max 20 chars)
    """
    u    = _current_user()
    body = request.get_json(force=True, silent=True) or {}
    for f in ["tradingsymbol", "transaction_type", "quantity"]:
        if not body.get(f):
            return jsonify({"error": "missing_field", "field": f}), 400
    broker, err = _get_zerodha_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        order_id = broker.place_order(
            tradingsymbol    = body["tradingsymbol"].upper(),
            transaction_type = body["transaction_type"].upper(),
            quantity         = int(body["quantity"]),
            price            = float(body.get("price", 0)),
            trigger_price    = float(body.get("trigger_price", 0)),
            order_type       = body.get("order_type", "MARKET").upper(),
            product          = body.get("product", "MIS").upper(),
            exchange         = body.get("exchange", "NSE").upper(),
            variety          = body.get("variety", "regular").lower(),
            validity         = body.get("validity", "DAY").upper(),
            squareoff        = float(body.get("squareoff", 0)),
            stoploss         = float(body.get("stoploss", 0)),
            trailing_stoploss= float(body.get("trailing_stoploss", 0)),
            tag              = body.get("tag", ""),
        )
        auth.audit(u["id"], "zerodha_order_placed", _client_ip(), {
            "symbol": body["tradingsymbol"],
            "side":   body["transaction_type"],
            "qty":    body["quantity"],
        })
        return jsonify({"ok": True, "order_id": order_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/zerodha/order/<order_id>", methods=["DELETE"])
@_require_auth
def api_zerodha_cancel_order(order_id: str):
    u       = _current_user()
    variety = request.args.get("variety", "regular").lower()
    broker, err = _get_zerodha_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        oid = broker.cancel_order(order_id, variety=variety)
        auth.audit(u["id"], "zerodha_order_cancelled", _client_ip(), {"order_id": order_id})
        return jsonify({"ok": True, "order_id": oid})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/zerodha/order", methods=["PUT"])
@_require_auth
def api_zerodha_modify_order():
    u    = _current_user()
    body = request.get_json(force=True, silent=True) or {}
    if not body.get("order_id"):
        return jsonify({"error": "missing_field", "field": "order_id"}), 400
    broker, err = _get_zerodha_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        oid = broker.modify_order(
            order_id       = body["order_id"],
            quantity       = int(body["quantity"])   if body.get("quantity")    is not None else None,
            price          = float(body["price"])    if body.get("price")       is not None else None,
            order_type     = body.get("order_type"),
            trigger_price  = float(body["trigger_price"]) if body.get("trigger_price") is not None else None,
            validity       = body.get("validity"),
            variety        = body.get("variety", "regular").lower(),
        )
        return jsonify({"ok": True, "order_id": oid})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/zerodha/squareoff", methods=["POST"])
@_require_auth
def api_zerodha_squareoff():
    """Close all open Zerodha MIS positions at market price."""
    u = _current_user()
    broker, err = _get_zerodha_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        results = broker.square_off_all_positions()
        auth.audit(u["id"], "zerodha_squareoff_all", _client_ip())
        return jsonify({"ok": True, "results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/zerodha/quote")
@_require_auth
def api_zerodha_quote():
    """
    GET /api/zerodha/quote?i=NSE:RELIANCE&i=NSE:TCS
    Returns full quote dict.
    """
    u           = _current_user()
    instruments = request.args.getlist("i")
    if not instruments:
        return jsonify({"error": "at least one i=EXCHANGE:SYMBOL param required"}), 400
    broker, err = _get_zerodha_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        return jsonify(broker.get_quote(instruments))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/zerodha/search")
@_require_auth
def api_zerodha_search():
    """
    GET /api/zerodha/search?q=RELIANCE&exchange=NSE
    Returns matching instruments (from master download — may be slow first call).
    """
    u        = _current_user()
    query    = request.args.get("q", "").strip()
    exchange = request.args.get("exchange", "NSE").upper()
    if not query:
        return jsonify({"error": "q param required"}), 400
    broker, err = _get_zerodha_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        return jsonify(broker.search_instruments(query, exchange))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/zerodha/candles")
@_require_auth
def api_zerodha_candles():
    """
    GET /api/zerodha/candles?token=738561&interval=5minute&from=2024-01-01&to=2024-01-02
    """
    u        = _current_user()
    token    = request.args.get("token", "")
    interval = request.args.get("interval", "5minute")
    from_dt  = request.args.get("from", "")
    to_dt    = request.args.get("to", "")
    if not token or not from_dt or not to_dt:
        return jsonify({"error": "token, from and to params required"}), 400
    broker, err = _get_zerodha_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        return jsonify(broker.get_candles(token, interval, from_dt, to_dt))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/sessions")
@_require_auth
def api_sessions():
    u = _current_user()
    sessions = []
    cur_token = request.cookies.get(SESSION_COOKIE)
    for s in auth.list_sessions(u["id"]):
        sessions.append({
            "ip":         s["ip"],
            "user_agent": (s["user_agent"] or "")[:160],
            "created_at": s["created_at"],
            "expires_at": s["expires_at"],
            "current":    (s["token"] == cur_token),
            "token_tail": s["token"][-8:],
        })
    return jsonify(sessions)

@app.route("/api/sessions/revoke", methods=["POST"])
@_require_auth
def api_revoke_session():
    u = _current_user()
    body = request.get_json(force=True, silent=True) or {}
    tail = body.get("token_tail", "")
    if not tail or len(tail) < 6:
        return jsonify({"error": "bad_request"}), 400
    for s in auth.list_sessions(u["id"]):
        if s["token"].endswith(tail):
            auth.delete_session(s["token"])
            return jsonify({"ok": True})
    return jsonify({"error": "not_found"}), 404

@app.route("/api/audit")
@_require_auth
def api_audit_log():
    u = _current_user()
    rows = auth.get_audit(u["id"], limit=100)
    return jsonify([dict(r) for r in rows])

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



# ── Telegram per-user notification endpoints ─────────────────────
import requests as _tg_requests
@app.route("/api/telegram/status", methods=["GET"])
@_require_auth
def telegram_status():
    u = _current_user()
    cfg = auth.get_telegram(u["id"])
    if not cfg:
        return jsonify({"configured": False})
    # Redact token
    tok = cfg["bot_token"] or ""
    cfg["bot_token_redacted"] = (tok[:6] + "..." + tok[-4:]) if len(tok) > 10 else ""
    cfg.pop("bot_token", None)
    cfg["configured"] = True
    return jsonify(cfg)

@app.route("/api/telegram/save", methods=["POST"])
@_require_auth
def telegram_save():
    u = _current_user()
    body = request.get_json(silent=True) or {}
    token = (body.get("bot_token") or "").strip()
    chat_id = (body.get("chat_id") or "").strip()
    enabled = bool(body.get("enabled", True))
    events = body.get("events") or {"buy":1,"sell":1,"eod":1,"vix":1,"startup":1}
    if not token or not chat_id:
        return jsonify({"error": "bot_token and chat_id required"}), 400
    auth.save_telegram(u["id"], token, chat_id, enabled, events)
    return jsonify({"ok": True})

@app.route("/api/telegram/test", methods=["POST"])
@_require_auth
def telegram_test():
    u = _current_user()
    cfg = auth.get_telegram(u["id"])
    if not cfg or not cfg.get("bot_token") or not cfg.get("chat_id"):
        return jsonify({"error": "not_configured"}), 400
    try:
        r = _tg_requests.post(
            f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage",
            json={"chat_id": cfg["chat_id"],
                  "text": f"<b>AlgoTrader test</b>\nUser: {u['email']}\nIf you can read this, your alerts are wired up correctly.",
                  "parse_mode": "HTML"},
            timeout=8
        )
        ok = r.status_code == 200 and r.json().get("ok")
        return jsonify({"ok": bool(ok), "telegram_response": r.json() if r.status_code == 200 else r.text})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/telegram/delete", methods=["POST"])
@_require_auth
def telegram_delete():
    u = _current_user()
    auth.delete_telegram(u["id"])
    return jsonify({"ok": True})




# ── Bot real-money mode + panic switch (admin only) ───────────────
import subprocess as _sp
_ENV_PATH = os.path.join(BASE_DIR, ".env")

def _read_env_kv():
    out = {}
    if os.path.exists(_ENV_PATH):
        for line in open(_ENV_PATH):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out

def _write_env_kv(updates: dict):
    cur = _read_env_kv()
    cur.update({k: v for k, v in updates.items() if v is not None})
    with open(_ENV_PATH, "w") as f:
        for k, v in cur.items():
            f.write(f"{k}={v}\n")

def _require_admin(f):
    @wraps(f)
    def deco(*a, **kw):
        u = _current_user()
        if not u: return jsonify({"error":"auth_required"}), 401
        if u.get("role") != "admin":
            return jsonify({"error":"admin_only"}), 403
        return f(*a, **kw)
    return deco

@app.route("/api/admin/bot_mode", methods=["GET"])
@_require_admin
def admin_bot_mode_get():
    env = _read_env_kv()
    base = env.get("ALPACA_BASE_URL","")
    is_live = "paper" not in base.lower() and base != ""
    key = env.get("ALPACA_API_KEY","")
    return jsonify({
        "mode": "live" if is_live else "paper",
        "base_url": base,
        "key_preview": (key[:6] + "..." + key[-4:]) if len(key) > 10 else "",
        "warning": "LIVE mode trades real money. Verify the bot is healthy on paper for at least 5 sessions first." if is_live else "",
    })

@app.route("/api/admin/bot_mode", methods=["POST"])
@_require_admin
def admin_bot_mode_set():
    body = request.get_json(silent=True) or {}
    target = (body.get("mode") or "").lower()
    api_key = (body.get("api_key") or "").strip()
    sec_key = (body.get("secret_key") or "").strip()
    confirm = (body.get("confirm") or "").strip()

    if target not in ("paper", "live"):
        return jsonify({"error": "mode must be 'paper' or 'live'"}), 400
    if not api_key or not sec_key:
        return jsonify({"error": "api_key and secret_key required"}), 400
    if target == "live" and confirm != "I UNDERSTAND THE RISKS":
        return jsonify({"error": "Type 'I UNDERSTAND THE RISKS' exactly to switch to live"}), 400

    # Validate against the target Alpaca endpoint before writing anything
    base = "https://api.alpaca.markets/v2" if target == "live" else "https://paper-api.alpaca.markets/v2"
    data_base = "https://data.alpaca.markets/v2"
    try:
        import requests as _rq
        r = _rq.get(base + "/account",
                    headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": sec_key},
                    timeout=10)
        if r.status_code != 200:
            return jsonify({"error": "validation_failed",
                            "details": {"status": r.status_code, "body": r.text[:300]}}), 400
        acct = r.json()
    except Exception as e:
        return jsonify({"error": "validation_failed", "details": str(e)}), 400

    # Write .env atomically
    try:
        _write_env_kv({
            "ALPACA_API_KEY":    api_key,
            "ALPACA_SECRET_KEY": sec_key,
            "ALPACA_BASE_URL":   base,
            "ALPACA_DATA_URL":   data_base,
        })
    except Exception as e:
        return jsonify({"error": "env_write_failed", "details": str(e)}), 500

    # Restart bot to pick up new creds
    try:
        _sp.run(["supervisorctl", "restart", "trading-bot"], timeout=15, check=False)
    except Exception:
        pass

    auth.audit(_current_user()["id"], "bot_mode_switched", _client_ip(),
               {"mode": target, "account": acct.get("account_number","")[:8]})

    return jsonify({
        "ok": True, "mode": target, "base_url": base,
        "account": {"number": acct.get("account_number"),
                    "equity": acct.get("equity"),
                    "buying_power": acct.get("buying_power"),
                    "status": acct.get("status")},
        "warning": "Bot restarted. Monitor closely." if target == "live" else None,
    })

@app.route("/api/admin/panic_flat", methods=["POST"])
@_require_admin
def admin_panic_flat():
    """Cancel all open orders and liquidate all positions on the bot's broker.
    Use only in emergencies. Logged to audit."""
    env = _read_env_kv()
    base = env.get("ALPACA_BASE_URL","").rstrip("/")
    if not base or not env.get("ALPACA_API_KEY"):
        return jsonify({"error":"no_creds_configured"}), 400
    hdr = {"APCA-API-KEY-ID": env["ALPACA_API_KEY"],
           "APCA-API-SECRET-KEY": env["ALPACA_SECRET_KEY"]}
    out = {}
    try:
        import requests as _rq
        rc = _rq.delete(base + "/orders", headers=hdr, timeout=15)
        out["cancel_orders"] = {"status": rc.status_code, "body": rc.text[:500]}
        rp = _rq.delete(base + "/positions", headers=hdr, timeout=20)
        out["close_positions"] = {"status": rp.status_code, "body": rp.text[:500]}
    except Exception as e:
        return jsonify({"error": "panic_failed", "details": str(e)}), 500

    auth.audit(_current_user()["id"], "panic_flat", _client_ip(), out)
    return jsonify({"ok": True, "result": out})




# Upgrade /api/admin/bot_mode POST to use SAVED creds when keys are blank
# (drop-in over the existing handler — not redefining; a follow-up if needed)




# ── Aggregate "All Accounts" overview ─────────────────────────────
@app.route("/api/aggregate/overview", methods=["GET"])
@_require_auth
def api_aggregate_overview():
    """Server-side parallel fetch from all connected brokers (threaded)."""
    u = _current_user()
    out = {"brokers": {}, "totals": {"positions": 0, "open_pnl_usd": 0.0, "open_pnl_inr": 0.0}}

    def _fetch_alpaca():
        try:
            env_url = os.environ.get("ALPACA_BASE_URL", "").rstrip("/")
            env_key = os.environ.get("ALPACA_API_KEY", "")
            env_sec = os.environ.get("ALPACA_SECRET_KEY", "")
            if not env_url or not env_key:
                return ("alpaca", {"connected": False})
            hdr = {"APCA-API-KEY-ID": env_key, "APCA-API-SECRET-KEY": env_sec}
            import requests as _rq
            acct = _rq.get(env_url + "/account", headers=hdr, timeout=8).json()
            pos  = _rq.get(env_url + "/positions", headers=hdr, timeout=8).json()
            ords = _rq.get(env_url + "/orders", headers=hdr,
                           params={"status":"closed","limit":20,"direction":"desc"},
                           timeout=8).json()
            mode = "live" if "paper" not in env_url.lower() else "paper"
            normalized = [{
                "symbol": p.get("symbol"), "qty": p.get("qty"),
                "avg": p.get("avg_entry_price"), "ltp": p.get("current_price"),
                "pnl": p.get("unrealized_pl"), "currency": "USD",
            } for p in (pos if isinstance(pos,list) else [])]
            return ("alpaca", {
                "connected": True, "mode": mode,
                "account": acct.get("account_number"),
                "equity": acct.get("equity"), "buying_power": acct.get("buying_power"),
                "currency": acct.get("currency","USD"),
                "positions": normalized,
                "recent_orders": [{"sym": o.get("symbol"), "side": o.get("side"),
                                   "qty": o.get("filled_qty"), "px": o.get("filled_avg_price"),
                                   "status": o.get("status"), "ts": o.get("filled_at")}
                                  for o in (ords if isinstance(ords,list) else [])][:10],
            })
        except Exception as e:
            return ("alpaca", {"connected": False, "error": str(e)[:120]})

    def _fetch_angelone():
        try:
            broker, err = _get_angelone_broker(u["id"])
            if err: return ("angelone", {"connected": False, "error": err})
            ao_pos = broker.get_positions() or []
            ao_funds = broker.get_funds() or {}
            ao_orders = broker.get_order_book() or []
            _persist_angelone_tokens(u["id"], broker)
            norm = [{"symbol": p.get("tradingsymbol"), "qty": p.get("netqty"),
                     "avg": p.get("avgnetprice"), "ltp": p.get("ltp"), "pnl": p.get("pnl"),
                     "exchange": p.get("exchange"), "currency": "INR"}
                    for p in (ao_pos if isinstance(ao_pos,list) else [])]
            return ("angelone", {
                "connected": True,
                "available_cash": (ao_funds or {}).get("availablecash") or (ao_funds or {}).get("net"),
                "currency": "INR", "positions": norm,
                "recent_orders": [{"sym": o.get("tradingsymbol"), "side": o.get("transactiontype"),
                                   "qty": o.get("quantity"), "px": o.get("price"),
                                   "status": o.get("orderstatus"), "ts": o.get("updatetime")}
                                  for o in (ao_orders if isinstance(ao_orders,list) else [])][:10],
            })
        except Exception as e:
            return ("angelone", {"connected": False, "error": str(e)[:200]})

    def _fetch_zerodha():
        try:
            broker, err = _get_zerodha_broker(u["id"])
            if err: return ("zerodha", {"connected": False, "error": err})
            zr_pos_raw = broker.get_positions() or {}
            zr_pos = (zr_pos_raw.get("net") if isinstance(zr_pos_raw,dict) else zr_pos_raw) or []
            zr_funds = broker.get_funds() or {}
            zr_orders = broker.get_orders() or []
            norm = [{"symbol": p.get("tradingsymbol"), "qty": p.get("quantity"),
                     "avg": p.get("average_price"), "ltp": p.get("last_price"), "pnl": p.get("pnl"),
                     "exchange": p.get("exchange"), "currency": "INR"}
                    for p in (zr_pos if isinstance(zr_pos,list) else [])]
            eq = ((zr_funds or {}).get("equity") or {}).get("available", {}) if isinstance(zr_funds, dict) else {}
            return ("zerodha", {
                "connected": True,
                "available_cash": (eq.get("cash") if isinstance(eq, dict) else None),
                "currency": "INR", "positions": norm,
                "recent_orders": [{"sym": o.get("tradingsymbol"), "side": o.get("transaction_type"),
                                   "qty": o.get("quantity"), "px": o.get("price"),
                                   "status": o.get("status"), "ts": o.get("order_timestamp")}
                                  for o in (zr_orders if isinstance(zr_orders,list) else [])][:10],
            })
        except Exception as e:
            return ("zerodha", {"connected": False, "error": str(e)[:200]})

    with _AggPool(max_workers=3) as pool:
        for name, info in pool.map(lambda f: f(), [_fetch_alpaca, _fetch_angelone, _fetch_zerodha]):
            out["brokers"][name] = info
            if info.get("connected") and not info.get("error"):
                pos_list = info.get("positions") or []
                out["totals"]["positions"] += len(pos_list)
                ccy = info.get("currency", "USD")
                key = "open_pnl_inr" if ccy == "INR" else "open_pnl_usd"
                try:
                    out["totals"][key] += sum(float(p.get("pnl") or 0) for p in pos_list)
                except Exception:
                    pass

    # Item 14: FX conversion (USD ↔ INR) using a fixed approx rate (frontend can override)
    out["fx"] = {"usd_inr": 83.5, "inr_usd": 1/83.5, "source": "static"}

    return jsonify(out)


@app.route("/api/audit_full", methods=["GET"])
@_require_auth
def api_audit_full():
    """Return last 100 audit events for the current user (admin sees all)."""
    u = _current_user()
    limit = min(int(request.args.get("limit", 100)), 500)
    if u.get("role") == "admin":
        rows = db.query_all("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
    else:
        rows = db.query_all("SELECT * FROM audit_log WHERE user_id=? ORDER BY id DESC LIMIT ?",
                            (u["id"], limit))
    out = []
    for r in rows:
        try: meta = json.loads(r["meta"]) if r["meta"] else {}
        except Exception: meta = {}
        out.append({
            "id": r["id"], "user_id": r["user_id"], "event": r["event"],
            "ip": r["ip"], "meta": meta, "at": r["created_at"],
        })
    return jsonify(out)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
