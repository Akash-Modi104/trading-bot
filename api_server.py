import _force_ipv4_kite  # Force IPv4 for kite.trade per NSE IP whitelist
import os

# Load .env FIRST so auth.py picks up MASTER_ENCRYPTION_KEY before generating a new one
_env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
try:
    from dotenv import load_dotenv as _ld; _ld(_env_file)
except ImportError:
    if os.path.exists(_env_file):
        for _l in open(_env_file):
            _l = _l.strip()
            if _l and not _l.startswith("#") and "=" in _l:
                k, v = _l.split("=", 1); os.environ.setdefault(k.strip(), v.strip())

from flask import (Flask, jsonify, request, Response, redirect,
                   make_response, g, render_template_string, send_from_directory)
from flask_cors import CORS
from functools import wraps
import json, subprocess, time, threading, secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
import pytz


from concurrent.futures import ThreadPoolExecutor as _AggPool
import db
import auth


# ── Login rate limiting (in-memory token bucket per IP) ───────
import collections, threading, re

# Trading-symbol whitelist regex (used by order/quote/search endpoints
# to reject anything that's not a plain ticker).
_SYMBOL_RE = re.compile(r"^[A-Z0-9\-&]{1,32}$")
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
    "http://localhost:5001", "http://127.0.0.1:5001",     # dev only
    "https://187.127.73.203:5001",                         # production server (HTTPS)
    "https://dilipcentralacademy.tech",                    # production domain
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
# Note: .env already loaded at top of file before auth import

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


# ── Numeric input validation helpers ─────────────────────────────────
class _ValidationError(Exception):
    """Raised by _validate_* helpers; callers convert to JSON 400."""

def _bounded_number(value, *, name: str, lo, hi, allow_zero: bool = True,
                    cast=float):
    """Cast `value` to int/float and ensure lo <= value <= hi.
    Returns the cast value or raises _ValidationError. Treat None/'' as
    'field absent' — caller decides whether that's required."""
    if value is None or value == "":
        return None
    try:
        v = cast(value)
    except (TypeError, ValueError):
        raise _ValidationError(f"{name}: must be a number")
    if not allow_zero and v == 0:
        raise _ValidationError(f"{name}: must be non-zero")
    if v < lo or v > hi:
        raise _ValidationError(f"{name}: must be between {lo} and {hi}")
    return v


# Bounds (broad enough not to surprise legitimate users, tight enough to
# block typos that move real money):
#   budget: ₹0 – ₹10 Cr  /  $0 – $1 M
#   max_positions: 0 – 50
#   stop_pct / tp_pct: 0% – 50%
#   order qty: 1 – 100,000
#   order price: 0 – ₹1,00,000 (per share, intraday); 0 means MARKET
_BOUNDS = {
    "budget":         (0, 100_000_000),
    "max_positions":  (0, 50),
    "stop_pct":       (0, 50),
    "tp_pct":         (0, 50),
    "min_confidence": (0, 100),
    "position_pct":   (0, 100),
    "qty":            (1, 100_000),
    "price":          (0, 100_000),
}

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
    auth.update_user(user["id"], last_login_at=auth.utcnow().isoformat())
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
            html = f.read()
        mtime = int(os.path.getmtime(path))
        resp = make_response(html)
        # Tell browsers (esp. mobile Safari) NOT to cache the HTML shell
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
        resp.headers["Pragma"]        = "no-cache"
        resp.headers["Expires"]       = "0"
        resp.headers["X-App-Version"] = str(mtime)
        return resp
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
    logged_in_at  = auth.utcnow().isoformat()

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

    side = str(body["transaction_type"]).upper()
    if side not in ("BUY", "SELL"):
        return jsonify({"error": "invalid_input",
                        "message": "transaction_type must be BUY or SELL"}), 400
    sym = str(body["tradingsymbol"]).upper().strip()
    if not _SYMBOL_RE.match(sym.replace("-EQ", "")):  # tolerate "-EQ" suffix
        return jsonify({"error": "invalid_input",
                        "message": "tradingsymbol must be 1-32 alphanumerics"}), 400
    try:
        qty   = _bounded_number(body["quantity"], name="quantity",
                                lo=_BOUNDS["qty"][0], hi=_BOUNDS["qty"][1],
                                allow_zero=False, cast=int)
        price = _bounded_number(body.get("price", 0), name="price",
                                lo=_BOUNDS["price"][0], hi=_BOUNDS["price"][1],
                                cast=float) or 0
        squareoff         = _bounded_number(body.get("squareoff", 0), name="squareoff",
                                            lo=0, hi=_BOUNDS["price"][1], cast=float) or 0
        stoploss          = _bounded_number(body.get("stoploss", 0), name="stoploss",
                                            lo=0, hi=_BOUNDS["price"][1], cast=float) or 0
        trailing_stoploss = _bounded_number(body.get("trailing_stoploss", 0), name="trailing_stoploss",
                                            lo=0, hi=_BOUNDS["price"][1], cast=float) or 0
    except _ValidationError as e:
        return jsonify({"error": "invalid_input", "message": str(e)}), 400
    order_type = str(body.get("order_type", "MARKET")).upper()
    if order_type in ("LIMIT", "STOPLOSS_LIMIT") and price <= 0:
        return jsonify({"error": "invalid_input",
                        "message": f"{order_type} order requires price > 0"}), 400

    broker, err = _get_angelone_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400

    try:
        order_id = broker.place_order(
            tradingsymbol    = sym,
            symboltoken      = str(body["symboltoken"]),
            transaction_type = side,
            quantity         = qty,
            price            = price,
            order_type       = order_type,
            product_type     = body.get("product_type", "INTRADAY").upper(),
            exchange         = body.get("exchange", "NSE").upper(),
            variety          = body.get("variety", "NORMAL").upper(),
            duration         = body.get("duration", "DAY").upper(),
            squareoff        = squareoff,
            stoploss         = stoploss,
            trailing_stoploss= trailing_stoploss,
        )
        _persist_angelone_tokens(u["id"], broker)
        auth.audit(u["id"], "angelone_order_placed", _client_ip(), {
            "symbol": sym, "side": side, "qty": qty, "price": price,
            "type": order_type, "order_id": str(order_id),
        })
        return jsonify({"ok": True, "order_id": order_id})
    except Exception as e:
        auth.audit(u["id"], "angelone_order_failed", _client_ip(), {
            "symbol": sym, "side": side, "qty": qty, "error": str(e)[:200],
        })
        return jsonify({"error": "broker_error", "message": str(e)}), 400

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

def _get_zerodha_broker(user_id: int, require_token: bool = True):
    """Build a ZerodhaBroker from stored credentials.

    require_token: when True (default), api_key AND access_token must both
                   exist — used by trading endpoints (orders, positions, etc).
                   When False, only api_key + api_secret are required —
                   used by /api/zerodha/session which is the very call that
                   creates the access_token (chicken-and-egg).
    """
    from brokers.zerodha import ZerodhaBroker
    creds = auth.get_zerodha_creds(user_id)
    if not creds:
        return None, "not_connected"
    if not creds.get("api_key") or not creds.get("api_secret"):
        return None, "credentials_invalid — re-enter API key and secret in Profile"
    if require_token and not creds.get("access_token"):
        return None, "login_required — click Open Kite Login (daily) in Profile"
    broker = ZerodhaBroker(
        api_key=creds["api_key"],
        api_secret=creds["api_secret"],
        access_token=creds.get("access_token", ""),
    )
    return broker, None

@app.route("/api/zerodha/postback", methods=["POST"])
def api_zerodha_postback():
    """
    Kite Connect v3 Postback receiver.
    https://kite.trade/docs/connect/v3/postbacks/

    Zerodha POSTs JSON when order status changes (COMPLETE / CANCELLED /
    REJECTED / UPDATE). The payload includes a SHA-256 checksum of
        order_id + order_timestamp + api_secret
    which we MUST verify to confirm the update is genuine.

    No dashboard auth required — this endpoint is called by Zerodha's
    servers, not by browsers.
    """
    import hashlib
    payload = request.get_json(force=True, silent=True) or {}

    order_id   = str(payload.get("order_id", ""))
    timestamp  = str(payload.get("order_timestamp", ""))
    checksum   = str(payload.get("checksum", ""))
    status     = str(payload.get("status", "")).upper()

    # ── HMAC verification ────────────────────────────────────────
    verified = False
    user_id  = 1            # Single-user system; admin owns the Zerodha account
    expected = ""           # Always defined so we can log a prefix
    debug    = ""
    try:
        creds = auth.get_zerodha_creds(user_id)
        if not creds:
            debug = "no_creds_in_db"
        elif not creds.get("api_secret"):
            debug = "api_secret_empty"
        else:
            expected = hashlib.sha256(
                f"{order_id}{timestamp}{creds['api_secret']}".encode()
            ).hexdigest()
            verified = (expected == checksum)
            if not verified:
                debug = "checksum_mismatch"
    except Exception as e:
        debug = f"exception:{type(e).__name__}:{str(e)[:60]}"

    if not verified:
        auth.audit(user_id, "zerodha_postback_invalid_checksum",
                   request.remote_addr or "kite",
                   {"order_id": order_id, "status": status,
                    "expected_prefix": expected[:12],
                    "got_prefix": checksum[:12],
                    "debug": debug})
        return jsonify({"error": "invalid_checksum", "debug": debug}), 200

    # ── Extract full order details per Kite v3 spec ──────────────
    meta = {
        "order_id":           order_id,
        "exchange_order_id":  payload.get("exchange_order_id"),
        "status":             status,
        "tradingsymbol":      payload.get("tradingsymbol"),
        "exchange":           payload.get("exchange"),
        "transaction_type":   payload.get("transaction_type"),
        "order_type":         payload.get("order_type"),
        "product":            payload.get("product"),
        "quantity":           payload.get("quantity"),
        "filled_quantity":    payload.get("filled_quantity"),
        "pending_quantity":   payload.get("pending_quantity"),
        "cancelled_quantity": payload.get("cancelled_quantity"),
        "price":              payload.get("price"),
        "average_price":      payload.get("average_price"),
        "trigger_price":      payload.get("trigger_price"),
        "status_message":     payload.get("status_message"),
        "order_timestamp":    timestamp,
        "tag":                payload.get("tag"),
    }
    # Drop nulls for cleaner audit row
    meta = {k: v for k, v in meta.items() if v not in (None, "", 0)}

    # ── Audit log entry (shows in Activity tab) ──────────────────
    event_name = f"zerodha_order_{status.lower() or 'update'}"
    auth.audit(user_id or 1, event_name, request.remote_addr or "kite", meta)

    # ── Trigger SSE event so dashboard refreshes immediately ─────
    try:
        if hasattr(app, "_broadcast_event"):
            app._broadcast_event("zerodha_order_update", meta)
    except Exception:
        pass

    return jsonify({"ok": True, "verified": True, "order_id": order_id}), 200


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
    # Session creation — access_token is what we are about to mint, so do NOT require it
    broker, err = _get_zerodha_broker(u["id"], require_token=False)
    if err:
        return jsonify({"error": err}), 400
    try:
        session = broker.generate_session(req_token)
        access_token   = session.get("access_token", "")
        login_time     = session.get("login_time", "")
        expiry_ts      = (auth.utcnow() + timedelta(hours=20)).isoformat()
        auth.update_zerodha_access_token(u["id"], access_token, session_expiry=expiry_ts)
        auth.audit(u["id"], "zerodha_session_created", _client_ip())
        return jsonify({"ok": True, "login_time": login_time,
                        "message": "Zerodha session established"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/zerodha/callback")
def api_zerodha_callback():
    """
    Kite OAuth redirect target. Set this URL in your Kite Developer Console:
        https://<your-domain>/api/zerodha/callback

    Kite redirects the browser here as a GET request with:
        ?request_token=XXX&action=login&status=success

    We exchange the request_token for an access_token using the user's stored
    api_key + api_secret, persist it, and bounce back to the dashboard.

    Requires the user to be logged in to the dashboard (session cookie). If
    the cookie has expired, send them to /login first.
    """
    u = _current_user()
    if not u:
        return redirect("/login")

    request_token = (request.args.get("request_token") or "").strip()
    status        = request.args.get("status", "")

    if not request_token:
        return redirect("/?zerodha_error=" + quote_plus("missing_token"))
    if status and status != "success":
        return redirect("/?zerodha_error=" + quote_plus(status))

    # Same chicken-and-egg as /api/zerodha/session: this is the call that
    # creates the access_token, so do not require it to be present.
    broker, err = _get_zerodha_broker(u["id"], require_token=False)
    if err:
        return redirect("/?zerodha_error=" + quote_plus(str(err)))

    try:
        session      = broker.generate_session(request_token)
        access_token = session.get("access_token", "")
        if not access_token:
            return redirect("/?zerodha_error=" + quote_plus("no_access_token"))
        expiry_ts = (auth.utcnow() + timedelta(hours=20)).isoformat()
        auth.update_zerodha_access_token(u["id"], access_token, session_expiry=expiry_ts)
        auth.audit(u["id"], "zerodha_session_created", _client_ip(), {"via": "callback"})
        return redirect("/?zerodha_ok=1")
    except Exception as e:
        return redirect("/?zerodha_error=" + quote_plus(str(e)[:200]))

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
    # Validate side, symbol, qty, price
    side = str(body["transaction_type"]).upper()
    if side not in ("BUY", "SELL"):
        return jsonify({"error": "invalid_input",
                        "message": "transaction_type must be BUY or SELL"}), 400
    sym = str(body["tradingsymbol"]).upper().strip()
    if not _SYMBOL_RE.match(sym):
        return jsonify({"error": "invalid_input",
                        "message": "tradingsymbol must be 1-20 alphanumerics"}), 400
    try:
        qty   = _bounded_number(body["quantity"], name="quantity",
                                lo=_BOUNDS["qty"][0], hi=_BOUNDS["qty"][1],
                                allow_zero=False, cast=int)
        price = _bounded_number(body.get("price", 0), name="price",
                                lo=_BOUNDS["price"][0], hi=_BOUNDS["price"][1],
                                cast=float) or 0
        trigger_price     = _bounded_number(body.get("trigger_price", 0), name="trigger_price",
                                            lo=0, hi=_BOUNDS["price"][1], cast=float) or 0
        squareoff         = _bounded_number(body.get("squareoff", 0), name="squareoff",
                                            lo=0, hi=_BOUNDS["price"][1], cast=float) or 0
        stoploss          = _bounded_number(body.get("stoploss", 0), name="stoploss",
                                            lo=0, hi=_BOUNDS["price"][1], cast=float) or 0
        trailing_stoploss = _bounded_number(body.get("trailing_stoploss", 0), name="trailing_stoploss",
                                            lo=0, hi=_BOUNDS["price"][1], cast=float) or 0
    except _ValidationError as e:
        return jsonify({"error": "invalid_input", "message": str(e)}), 400
    # Cross-field check: LIMIT orders require non-zero price
    order_type = str(body.get("order_type", "MARKET")).upper()
    if order_type in ("LIMIT", "SL") and price <= 0:
        return jsonify({"error": "invalid_input",
                        "message": f"{order_type} order requires price > 0"}), 400

    broker, err = _get_zerodha_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        order_id = broker.place_order(
            tradingsymbol    = sym,
            transaction_type = side,
            quantity         = qty,
            price            = price,
            trigger_price    = trigger_price,
            order_type       = order_type,
            product          = body.get("product", "MIS").upper(),
            exchange         = body.get("exchange", "NSE").upper(),
            variety          = body.get("variety", "regular").lower(),
            validity         = body.get("validity", "DAY").upper(),
            squareoff        = squareoff,
            stoploss         = stoploss,
            trailing_stoploss= trailing_stoploss,
            tag              = body.get("tag", ""),
        )
        auth.audit(u["id"], "zerodha_order_placed", _client_ip(), {
            "symbol": sym, "side": side, "qty": qty, "price": price,
            "type": order_type, "order_id": str(order_id),
        })
        return jsonify({"ok": True, "order_id": order_id})
    except Exception as e:
        # Broker rejection (insufficient margin, market closed, etc.) is a
        # client/state error, not a server bug. Return 400 so frontend
        # surfaces a clean toast.
        auth.audit(u["id"], "zerodha_order_failed", _client_ip(), {
            "symbol": sym, "side": side, "qty": qty, "error": str(e)[:200],
        })
        return jsonify({"error": "broker_error", "message": str(e)}), 400

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

# ── Zerodha GTT endpoints ────────────────────────────────────────

@app.route("/api/zerodha/gtt", methods=["GET"])
@_require_auth
def api_zerodha_gtt_list():
    """List all active GTT orders."""
    u = _current_user()
    broker, err = _get_zerodha_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        return jsonify(broker.get_gtts())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/zerodha/gtt", methods=["POST"])
@_require_auth
def api_zerodha_gtt_place():
    """
    Place a GTT (Good Till Triggered) order.

    Body:
      tradingsymbol   — e.g. "RELIANCE"
      exchange        — NSE | BSE | NFO (default NSE)
      trigger_type    — "single" | "two-leg"
      trigger_values  — array of prices, e.g. [2400] or [2300, 2600]
      last_price      — current LTP (float)
      orders          — array of order dicts matching each trigger leg
                        each: {transaction_type, quantity, order_type, product, price}

    Shortcut for OCO (two-leg SL+TP):
      tradingsymbol, exchange, quantity, sl_price, tp_price, last_price, product
    """
    u    = _current_user()
    body = request.get_json(force=True, silent=True) or {}
    broker, err = _get_zerodha_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        # Shortcut OCO path
        if "sl_price" in body and "tp_price" in body:
            gtt_id = broker.place_oco_gtt(
                tradingsymbol = body.get("tradingsymbol", "").upper(),
                exchange      = body.get("exchange", "NSE").upper(),
                quantity      = int(body["quantity"]),
                sl_price      = float(body["sl_price"]),
                tp_price      = float(body["tp_price"]),
                last_price    = float(body["last_price"]),
                product       = body.get("product", "MIS"),
            )
        else:
            for f in ["tradingsymbol", "trigger_type", "trigger_values", "last_price", "orders"]:
                if f not in body:
                    return jsonify({"error": "missing_field", "field": f}), 400
            gtt_id = broker.place_gtt(
                tradingsymbol  = body["tradingsymbol"].upper(),
                exchange       = body.get("exchange", "NSE").upper(),
                trigger_type   = body["trigger_type"],
                trigger_values = body["trigger_values"],
                last_price     = float(body["last_price"]),
                orders         = body["orders"],
            )
        auth.audit(u["id"], "zerodha_gtt_placed", _client_ip(), {"gtt_id": gtt_id})
        return jsonify({"ok": True, "gtt_id": gtt_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/zerodha/gtt/<int:gtt_id>", methods=["GET"])
@_require_auth
def api_zerodha_gtt_get(gtt_id: int):
    u = _current_user()
    broker, err = _get_zerodha_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        return jsonify(broker.get_gtt(gtt_id))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/zerodha/gtt/<int:gtt_id>", methods=["DELETE"])
@_require_auth
def api_zerodha_gtt_delete(gtt_id: int):
    u = _current_user()
    broker, err = _get_zerodha_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        broker.delete_gtt(gtt_id)
        auth.audit(u["id"], "zerodha_gtt_deleted", _client_ip(), {"gtt_id": gtt_id})
        return jsonify({"ok": True, "gtt_id": gtt_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Fund allocation endpoints ─────────────────────────────────────

@app.route("/api/allocations", methods=["GET"])
@_require_auth
def api_get_allocations():
    """Return fund allocations for all brokers for the current user."""
    u = _current_user()
    rows = db.get_all_fund_allocations(u["id"])
    # Fill in any missing brokers with defaults
    existing = {r["broker"] for r in rows}
    for broker in ("zerodha", "angelone", "alpaca"):
        if broker not in existing:
            rows.append(db.get_fund_allocation(u["id"], broker))
    return jsonify(rows)


@app.route("/api/allocations/<broker>", methods=["GET"])
@_require_auth
def api_get_allocation(broker: str):
    u = _current_user()
    if broker not in ("zerodha", "angelone", "alpaca"):
        return jsonify({"error": "unknown broker"}), 400
    return jsonify(db.get_fund_allocation(u["id"], broker))


@app.route("/api/allocations/<broker>", methods=["POST"])
@_require_auth
def api_set_allocation(broker: str):
    """
    Configure fund allocation for a bot/broker.

    Body (all optional):
      budget        — capital ceiling in broker's native currency (INR or USD). 0 = no cap.
      max_positions — max simultaneous open positions. 0 = use bot default.
      stop_pct      — stop-loss % override. 0 = use bot default.
      tp_pct        — take-profit % override. 0 = use bot default.
      auto_trade    — 1 = bot may trade autonomously, 0 = paused.
    """
    u = _current_user()
    if broker not in ("zerodha", "angelone", "alpaca"):
        return jsonify({"error": "unknown broker"}), 400
    body = request.get_json(force=True, silent=True) or {}
    kwargs = {}
    try:
        for field, cast in [("budget", float), ("max_positions", int),
                            ("stop_pct", float), ("tp_pct", float),
                            ("min_confidence", int), ("position_pct", float)]:
            if field in body:
                lo, hi = _BOUNDS[field]
                v = _bounded_number(body[field], name=field, lo=lo, hi=hi, cast=cast)
                if v is not None:
                    kwargs[field] = v
        if "auto_trade" in body:
            try: kwargs["auto_trade"] = 1 if int(body["auto_trade"]) else 0
            except (TypeError, ValueError):
                raise _ValidationError("auto_trade: must be 0 or 1")
    except _ValidationError as e:
        return jsonify({"error": "invalid_input", "message": str(e)}), 400
    if "trading_mode" in body:
        kwargs["trading_mode"] = str(body["trading_mode"] or "").lower()
    if not kwargs:
        return jsonify({"error": "no fields to update"}), 400
    try:
        db.upsert_fund_allocation(u["id"], broker, **kwargs)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    auth.audit(u["id"], f"allocation_updated_{broker}", _client_ip(), kwargs)
    return jsonify({"ok": True, "allocation": db.get_fund_allocation(u["id"], broker)})


@app.route("/api/trading_modes", methods=["GET"])
@_require_auth
def api_trading_modes():
    """Catalog of available trading modes — for UI rendering."""
    catalog = []
    for key, p in db.get_trading_mode_presets().items():
        catalog.append({
            "id":            key,
            "label":         p.get("label", key),
            "tagline":       p.get("tagline", ""),
            "max_positions": p.get("max_positions"),
            "stop_pct":      p.get("stop_pct"),
            "tp_pct":        p.get("tp_pct"),
            "min_confidence": p.get("min_confidence"),
            "position_pct":  p.get("position_pct"),
        })
    return jsonify(catalog)


@app.route("/api/allocations/<broker>/toggle", methods=["POST"])
@_require_auth
def api_toggle_auto_trade(broker: str):
    """Quick toggle: flip auto_trade on/off for a broker."""
    u = _current_user()
    if broker not in ("zerodha", "angelone", "alpaca"):
        return jsonify({"error": "unknown broker"}), 400
    current = db.get_fund_allocation(u["id"], broker)
    new_val = 0 if current.get("auto_trade") else 1
    db.upsert_fund_allocation(u["id"], broker, auto_trade=new_val)
    auth.audit(u["id"], f"auto_trade_toggled_{broker}", _client_ip(), {"auto_trade": new_val})
    return jsonify({"ok": True, "auto_trade": new_val})


# ── Indian bot live state (read-only) ─────────────────────────────
INDIAN_BOT_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "indian_bot_state.json")
INDIAN_SCANNER_FILE = os.path.join(BASE_DIR, "indian_scanner.json")
INDIAN_TRADE_LOG_FILE = os.path.join(BASE_DIR, "indian_trade_log.json")
INDIAN_VIRTUAL_FILE = os.path.join(BASE_DIR, "indian_virtual_state.json")

@app.route("/api/indian/state")
@_require_auth
def api_indian_state():
    """
    Return the live state of the Indian bot (Zerodha/AngelOne).
    The bot writes indian_bot_state.json every loop iteration.
    Frontend polls this every 8s for the Zerodha tab.
    """
    try:
        if not os.path.exists(INDIAN_BOT_STATE_FILE):
            return jsonify({
                "running": False,
                "broker": None,
                "daily_pnl": 0,
                "daily_trades": 0,
                "watchlist": [],
                "log": [],
                "vix": None,
                "allocation": {},
                "trading_paused": False,
                "pause_reason": "Bot has not started yet",
                "last_scan": None,
            })
        with open(INDIAN_BOT_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        # Best-effort merge with current allocation from DB (lets UI reflect
        # changes the user just made without waiting for the bot to reload)
        try:
            u = _current_user()
            broker = state.get("broker") or "zerodha"
            state["allocation"] = db.get_fund_allocation(u["id"], broker)
        except Exception:
            pass
        return jsonify(state)
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


def _num(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v, default=0):
    try:
        if v is None or v == "":
            return default
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _estimate_zerodha_equity_intraday_charges(buy_value: float, sell_value: float) -> dict:
    """Approximate Zerodha equity intraday round-trip charges in INR."""
    buy_value = max(0.0, _num(buy_value))
    sell_value = max(0.0, _num(sell_value))
    turnover = buy_value + sell_value
    brokerage = (min(20.0, buy_value * 0.0003) if buy_value else 0.0) + \
                (min(20.0, sell_value * 0.0003) if sell_value else 0.0)
    stt = sell_value * 0.00025
    exchange_txn = turnover * 0.0000297
    sebi = turnover * 0.000001
    stamp = buy_value * 0.00003
    gst = (brokerage + exchange_txn + sebi) * 0.18
    total = brokerage + stt + exchange_txn + sebi + stamp + gst
    return {
        "brokerage": round(brokerage, 2),
        "stt": round(stt, 2),
        "exchange_txn": round(exchange_txn, 2),
        "sebi": round(sebi, 2),
        "stamp": round(stamp, 2),
        "gst": round(gst, 2),
        "total": round(total, 2),
    }


def _parse_ist_datetime(value):
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value or "").strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+0000"
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z",
                    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s[:25], fmt)
                break
            except Exception:
                dt = None
        if dt is None:
            try:
                dt = datetime.fromisoformat(s)
            except Exception:
                return None
    ist = pytz.timezone("Asia/Kolkata")
    if dt.tzinfo is None:
        return ist.localize(dt)
    return dt.astimezone(ist)


def _normalise_zerodha_candles(raw: list) -> list:
    out = []
    for c in raw or []:
        try:
            if isinstance(c, dict):
                ts = c.get("date") or c.get("time") or c.get("timestamp")
                o = c.get("open")
                h = c.get("high")
                l = c.get("low")
                close = c.get("close")
                v = c.get("volume", 0)
            else:
                ts, o, h, l, close, v = c[:6]
            dt = _parse_ist_datetime(ts)
            out.append({
                "t": dt.isoformat() if dt else str(ts),
                "dt": dt,
                "o": _num(o),
                "h": _num(h),
                "l": _num(l),
                "c": _num(close),
                "v": _num(v),
            })
        except Exception:
            continue
    return [b for b in out if b["c"] > 0]


def _zerodha_symbol_token(broker, symbol: str):
    symbol = (symbol or "").upper().strip()
    try:
        data = broker.get_ltp([f"NSE:{symbol}"]) or {}
        tok = data.get(f"NSE:{symbol}", {}).get("instrument_token")
        if tok:
            return str(tok)
    except Exception:
        pass
    try:
        for inst in broker.search_instruments(symbol, "NSE") or []:
            if (inst.get("tradingsymbol") or "").upper() == symbol:
                return str(inst.get("instrument_token") or "")
    except Exception:
        pass
    return ""


def _score_backtest_bars(bars: list) -> tuple[int, dict]:
    try:
        from indian_bot import ema_cross, rsi_val, vwap_val, macd, bollinger
    except Exception:
        return 0, {"error": "strategy import failed"}
    if len(bars) < 25:
        return 0, {"error": "not enough candles"}
    closes = [b["c"] for b in bars]
    current = closes[-1]
    score = 0
    reasons = {}
    sig = ema_cross(closes)
    if sig == "sell":
        return 0, {"ema": "bearish"}
    if sig == "buy":
        score += 25
        reasons["ema"] = "EMA bullish cross"
    else:
        score += 8
        reasons["ema"] = "EMA holding"
    rsi = rsi_val(closes)
    if rsi > 82 or rsi < 30:
        return 0, {"skip": f"RSI {rsi:.0f} extreme"}
    if 45 <= rsi <= 75:
        score += 15
        reasons["rsi"] = f"RSI {rsi:.0f}"
    elif 35 <= rsi < 45:
        score += 5
    vw = vwap_val(bars)
    if vw and current > vw:
        score += 15
        reasons["vwap"] = "above VWAP"
    elif vw and current <= vw:
        score -= 8
    ml, sl, hist = macd(closes)
    if ml is not None:
        if ml > sl and hist > 0:
            score += 10
            reasons["macd"] = "MACD bullish"
        elif ml < sl:
            score -= 5
    bb_up, bb_mid, bb_lo = bollinger(closes)
    if bb_up and bb_lo and bb_up != bb_lo:
        bb_pct = (current - bb_lo) / (bb_up - bb_lo)
        if bb_pct < 0.4:
            score += 10
            reasons["bb"] = "BB lower-third"
        elif bb_pct > 0.88:
            score -= 10
    return min(score, 100), reasons


def _volume_surge(bars: list, threshold: float = 1.3) -> bool:
    if len(bars) < 25:
        return False
    vols = [_num(b.get("v")) for b in bars[-21:-1]]
    avg = sum(vols) / max(1, len(vols))
    return avg > 0 and _num(bars[-1].get("v")) >= avg * threshold


def _quality_entry_time(dt) -> bool:
    if not dt:
        return False
    mins = dt.hour * 60 + dt.minute
    return (9 * 60 + 45 <= mins < 11 * 60 + 30) or (13 * 60 <= mins < 14 * 60 + 30)


def _run_indian_backtest_sim(symbol: str, bars: list, *, budget: float,
                             stop_pct: float, tp_pct: float,
                             min_conf: int) -> dict:
    position = None
    trades = []
    curve = []
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    checked = 0
    passed_score = 0
    passed_volume = 0
    no_cash = 0
    last_signal = None

    def _close_position(pos, bar, exit_price, exit_reason):
        nonlocal equity, peak, max_dd
        buy_value = pos["entry"] * pos["qty"]
        sell_value = exit_price * pos["qty"]
        gross = sell_value - buy_value
        charges = _estimate_zerodha_equity_intraday_charges(buy_value, sell_value)
        net = gross - charges["total"]
        equity += net
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
        trade = {
            "entry_time": pos["time"],
            "exit_time": bar["t"],
            "symbol": symbol,
            "qty": pos["qty"],
            "entry": round(pos["entry"], 2),
            "exit": round(exit_price, 2),
            "reason": exit_reason,
            "score": pos["score"],
            "gross_pnl": round(gross, 2),
            "charges": charges["total"],
            "net_pnl": round(net, 2),
            "running_pnl": round(equity, 2),
            "signal_reasons": pos["reasons"],
        }
        trades.append(trade)
        curve.append({
            "time": str(bar["t"])[11:16] if len(str(bar["t"])) > 15 else str(bar["t"]),
            "pnl": round(equity, 2),
            "symbol": symbol,
            "reason": exit_reason,
        })

    for i in range(35, len(bars)):
        bar = bars[i]
        dt = bar.get("dt")
        if position:
            exit_price = None
            exit_reason = ""
            mins = dt.hour * 60 + dt.minute if dt else 0
            if bar["l"] <= position["stop"]:
                exit_price = position["stop"]
                exit_reason = "stop"
            elif bar["h"] >= position["target"]:
                exit_price = position["target"]
                exit_reason = "target"
            elif mins >= 15 * 60 + 10:
                exit_price = bar["c"]
                exit_reason = "eod"
            if exit_price:
                _close_position(position, bar, exit_price, exit_reason)
                position = None
            continue

        if not _quality_entry_time(dt):
            continue
        window = bars[max(0, i - 80):i + 1]
        checked += 1
        score, reasons = _score_backtest_bars(window)
        if score < min_conf:
            continue
        passed_score += 1
        if not _volume_surge(window):
            continue
        passed_volume += 1
        price = bar["c"]
        qty = int((budget * 0.85) // price) if price > 0 else 0
        last_signal = {"time": bar["t"], "score": score, "price": round(price, 2), "reasons": reasons}
        if qty <= 0:
            no_cash += 1
            continue
        position = {
            "time": bar["t"],
            "entry": price,
            "qty": qty,
            "stop": round(price * (1 - stop_pct / 100), 2),
            "target": round(price * (1 + tp_pct / 100), 2),
            "score": score,
            "reasons": reasons,
        }

    if position and bars:
        _close_position(position, bars[-1], bars[-1]["c"], "range_end")

    wins = sum(1 for t in trades if t["net_pnl"] > 0)
    gross = sum(t["gross_pnl"] for t in trades)
    charges = sum(t["charges"] for t in trades)
    net = sum(t["net_pnl"] for t in trades)
    return {
        "summary": {
            "trades": len(trades),
            "wins": wins,
            "losses": len(trades) - wins,
            "win_rate": round((wins / len(trades) * 100), 1) if trades else 0,
            "gross_pnl": round(gross, 2),
            "estimated_charges": round(charges, 2),
            "net_pnl": round(net, 2),
            "max_drawdown": round(max_dd, 2),
            "checked_windows": checked,
            "passed_score": passed_score,
            "passed_volume": passed_volume,
            "skipped_no_cash": no_cash,
            "last_signal": last_signal,
        },
        "curve": curve,
        "price_curve": [
            {
                "time": str(b.get("t", ""))[5:16].replace("T", " "),
                "close": round(_num(b.get("c")), 2),
            }
            for b in bars[-300:]
        ],
        "markers": [
            {"time": t["entry_time"], "price": t["entry"], "side": "buy", "symbol": symbol}
            for t in trades[-100:]
        ] + [
            {"time": t["exit_time"], "price": t["exit"], "side": "sell", "symbol": symbol, "reason": t["reason"], "net_pnl": t["net_pnl"]}
            for t in trades[-100:]
        ],
        "trades": trades[-100:],
    }


def _default_virtual_state():
    now = datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()
    return {
        "enabled": False,
        "start_cash": 2000.0,
        "cash": 2000.0,
        "positions": [],
        "trades": [],
        "created_at": now,
        "updated_at": now,
    }


def _load_virtual_state():
    state = read_json(INDIAN_VIRTUAL_FILE, None)
    if not isinstance(state, dict):
        return _default_virtual_state()
    base = _default_virtual_state()
    base.update(state)
    base["positions"] = state.get("positions") if isinstance(state.get("positions"), list) else []
    base["trades"] = state.get("trades") if isinstance(state.get("trades"), list) else []
    return base


def _save_virtual_state(state: dict):
    state["updated_at"] = datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()
    state["cash"] = round(_num(state.get("cash")), 2)
    state["start_cash"] = round(_num(state.get("start_cash"), 2000), 2)
    write_json(INDIAN_VIRTUAL_FILE, state)


def _mark_virtual_to_market(state: dict, scanner_rows: list) -> dict:
    price_map = {
        (r.get("symbol") or "").upper(): _num(r.get("price"))
        for r in scanner_rows or []
        if _num(r.get("price")) > 0
    }
    unrealized = 0.0
    for p in state.get("positions", []):
        sym = (p.get("symbol") or "").upper()
        last = price_map.get(sym) or _num(p.get("last_price")) or _num(p.get("entry"))
        p["last_price"] = round(last, 2)
        p["unrealized_pnl"] = round((last - _num(p.get("entry"))) * _int(p.get("qty")), 2)
        unrealized += p["unrealized_pnl"]
    realized = sum(_num(t.get("net_pnl")) for t in state.get("trades", []))
    equity = _num(state.get("cash")) + sum(
        _int(p.get("qty")) * _num(p.get("last_price") or p.get("entry"))
        for p in state.get("positions", [])
    )
    return {
        "cash": round(_num(state.get("cash")), 2),
        "equity": round(equity, 2),
        "unrealized_pnl": round(unrealized, 2),
        "realized_pnl": round(realized, 2),
        "total_pnl": round(equity - _num(state.get("start_cash"), 2000), 2),
        "open_positions": len(state.get("positions", [])),
        "closed_trades": len(state.get("trades", [])),
    }


def _scanner_alerts(payload: dict, state: dict = None) -> list:
    rows = payload.get("rows") or []
    meta = payload.get("meta") or {}
    state = state or {}
    alerts = []
    if state.get("pause_reason"):
        alerts.append({
            "level": "warn",
            "kind": "pause",
            "title": "Bot paused",
            "message": state.get("pause_reason"),
        })
    updated = _parse_ist_datetime(payload.get("updated_at") or state.get("last_scan"))
    if updated:
        age_min = (datetime.now(pytz.timezone("Asia/Kolkata")) - updated).total_seconds() / 60
        if age_min > 20 and state.get("market_open"):
            alerts.append({
                "level": "warn",
                "kind": "stale_scan",
                "title": "Scanner is stale",
                "message": f"Last scan was {age_min:.0f} minutes ago.",
            })
    for r in rows:
        sym = r.get("symbol")
        status = r.get("status")
        if status == "candidate":
            alerts.append({
                "level": "up",
                "kind": "buy_ready",
                "symbol": sym,
                "title": f"{sym} passed entry gates",
                "message": f"Score {r.get('score')}/{r.get('min_confidence')}, qty {r.get('qty_preview')}, stop {r.get('stop')}, target {r.get('target')}.",
            })
        elif status == "order_failed":
            alerts.append({
                "level": "down",
                "kind": "order_failed",
                "symbol": sym,
                "title": f"{sym} order failed",
                "message": r.get("note") or "Broker did not confirm the order.",
            })
        elif status == "no_cash":
            alerts.append({
                "level": "warn",
                "kind": "cash_block",
                "symbol": sym,
                "title": f"{sym} blocked by cash",
                "message": r.get("note") or "Per-trade cap or available margin is too low.",
            })
    if meta.get("candidates", 0) == 0 and rows:
        top = sorted(rows, key=lambda r: _num(r.get("score")), reverse=True)[:3]
        alerts.append({
            "level": "info",
            "kind": "no_candidates",
            "title": "No buy-ready symbols",
            "message": "Top scores: " + ", ".join(f"{r.get('symbol')} {r.get('score', 0)}" for r in top),
        })
    return alerts[:20]


@app.route("/api/indian/scanner")
@_require_auth
def api_indian_scanner():
    payload = read_json(INDIAN_SCANNER_FILE, {})
    state = read_json(INDIAN_BOT_STATE_FILE, {})
    rows = payload.get("rows") or state.get("scanner") or []
    meta = dict(payload.get("meta") or state.get("scanner_meta") or {})
    meta.setdefault("pause_reason", state.get("pause_reason") or "")
    meta.setdefault("market_open", state.get("market_open"))
    meta.setdefault("now_ist", state.get("now_ist"))
    payload = {**payload, "rows": rows, "meta": meta}
    return jsonify({
        "updated_at": payload.get("updated_at") or state.get("last_scan"),
        "rows": rows,
        "meta": meta,
        "alerts": _scanner_alerts(payload, state),
    })


@app.route("/api/indian/net_pnl")
@_require_auth
def api_indian_net_pnl():
    u = _current_user()
    day = request.args.get("date") or datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d")
    broker, err = _get_zerodha_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    try:
        trades = broker.get_trades() or []
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500

    lots = {}
    realized = []
    day_trades = []
    for t in sorted(trades, key=lambda x: str(x.get("fill_timestamp") or x.get("order_timestamp") or "")):
        ts = t.get("fill_timestamp") or t.get("order_timestamp") or ""
        dt = _parse_ist_datetime(ts)
        if day and dt and dt.strftime("%Y-%m-%d") != day:
            continue
        sym = (t.get("tradingsymbol") or "").upper()
        side = (t.get("transaction_type") or "").upper()
        qty = _int(t.get("filled_quantity") or t.get("quantity"))
        price = _num(t.get("average_price") or t.get("price"))
        if not sym or qty <= 0 or price <= 0 or side not in ("BUY", "SELL"):
            continue
        day_trades.append(t)
        if side == "BUY":
            lots.setdefault(sym, []).append({"qty": qty, "price": price, "time": str(ts)})
            continue
        remaining = qty
        while remaining > 0 and lots.get(sym):
            lot = lots[sym][0]
            matched = min(remaining, lot["qty"])
            buy_value = lot["price"] * matched
            sell_value = price * matched
            gross = sell_value - buy_value
            charges = _estimate_zerodha_equity_intraday_charges(buy_value, sell_value)
            realized.append({
                "symbol": sym,
                "qty": matched,
                "buy_price": round(lot["price"], 2),
                "sell_price": round(price, 2),
                "gross_pnl": round(gross, 2),
                "charges": charges["total"],
                "net_pnl": round(gross - charges["total"], 2),
                "buy_time": lot["time"],
                "sell_time": str(ts),
            })
            lot["qty"] -= matched
            remaining -= matched
            if lot["qty"] <= 0:
                lots[sym].pop(0)

    gross = sum(r["gross_pnl"] for r in realized)
    charges = sum(r["charges"] for r in realized)
    net = sum(r["net_pnl"] for r in realized)
    by_symbol = {}
    for r in realized:
        sym = r["symbol"]
        row = by_symbol.setdefault(sym, {
            "symbol": sym,
            "round_trips": 0,
            "gross_pnl": 0.0,
            "estimated_charges": 0.0,
            "net_pnl": 0.0,
            "wins": 0,
            "losses": 0,
        })
        row["round_trips"] += 1
        row["gross_pnl"] += _num(r["gross_pnl"])
        row["estimated_charges"] += _num(r["charges"])
        row["net_pnl"] += _num(r["net_pnl"])
        if _num(r["net_pnl"]) >= 0:
            row["wins"] += 1
        else:
            row["losses"] += 1
    by_symbol_rows = []
    for row in by_symbol.values():
        row["gross_pnl"] = round(row["gross_pnl"], 2)
        row["estimated_charges"] = round(row["estimated_charges"], 2)
        row["net_pnl"] = round(row["net_pnl"], 2)
        row["win_rate"] = round((row["wins"] / row["round_trips"] * 100), 1) if row["round_trips"] else 0
        by_symbol_rows.append(row)
    by_symbol_rows.sort(key=lambda x: x["net_pnl"], reverse=True)
    return jsonify({
        "date": day,
        "trade_count": len(day_trades),
        "closed_count": len(realized),
        "gross_pnl": round(gross, 2),
        "estimated_charges": round(charges, 2),
        "net_pnl": round(net, 2),
        "charges_pct_of_gross_abs": round((charges / max(1e-9, abs(gross))) * 100, 2) if gross else 0.0,
        "symbol_breakdown": by_symbol_rows,
        "open_lots": {
            sym: [{"qty": l["qty"], "price": l["price"], "time": l["time"]} for l in lots_ if l["qty"] > 0]
            for sym, lots_ in lots.items() if any(l["qty"] > 0 for l in lots_)
        },
        "round_trips": realized[-100:],
        "charge_model": "Approx Zerodha equity intraday: brokerage, STT, exchange transaction, SEBI, stamp duty, GST.",
    })


@app.route("/api/indian/backtest")
@_require_auth
def api_indian_backtest():
    u = _current_user()
    symbol = (request.args.get("symbol") or "ONGC").upper().strip()
    if not _SYMBOL_RE.match(symbol):
        return jsonify({"error": "bad_symbol"}), 400
    days = max(1, min(90, _int(request.args.get("days"), 30)))
    state = read_json(INDIAN_BOT_STATE_FILE, {})
    alloc = state.get("allocation") or {}
    stop_pct = max(0.2, min(10.0, _num(request.args.get("stop_pct"), _num(alloc.get("stop_pct"), 1.5))))
    tp_pct = max(0.2, min(20.0, _num(request.args.get("tp_pct"), _num(alloc.get("tp_pct"), 3.0))))
    min_conf = max(1, min(100, _int(request.args.get("min_confidence"), _int(alloc.get("min_confidence"), 65))))
    budget = max(0, _num(request.args.get("budget"), _num(alloc.get("budget_per_trade"), 1000)))

    broker, err = _get_zerodha_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400
    token = _zerodha_symbol_token(broker, symbol)
    if not token:
        return jsonify({"error": "instrument_not_found", "symbol": symbol}), 404

    ist = pytz.timezone("Asia/Kolkata")
    to_dt = datetime.now(ist)
    from_dt = to_dt - timedelta(days=days)
    try:
        raw = broker.get_candles(
            token, "5minute",
            from_dt.strftime("%Y-%m-%d %H:%M:%S"),
            to_dt.strftime("%Y-%m-%d %H:%M:%S"),
        )
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500
    bars = _normalise_zerodha_candles(raw)

    result = _run_indian_backtest_sim(
        symbol, bars,
        budget=budget,
        stop_pct=stop_pct,
        tp_pct=tp_pct,
        min_conf=min_conf,
    )
    return jsonify({
        "symbol": symbol,
        "days": days,
        "candles": len(bars),
        "params": {
            "budget": round(budget, 2),
            "stop_pct": stop_pct,
            "tp_pct": tp_pct,
            "min_confidence": min_conf,
            "margin_buffer_pct": 15,
        },
        "summary": result["summary"],
        "curve": result["curve"],
        "price_curve": result["price_curve"],
        "markers": result["markers"],
        "trades": result["trades"],
    })


@app.route("/api/indian/strategy_rank")
@_require_auth
def api_indian_strategy_rank():
    u = _current_user()
    raw_symbols = request.args.get("symbols") or request.args.get("symbol") or "ONGC"
    symbols = []
    for part in re.split(r"[\s,]+", raw_symbols.upper().strip()):
        if part and part not in symbols:
            if not _SYMBOL_RE.match(part):
                return jsonify({"error": "bad_symbol", "symbol": part}), 400
            symbols.append(part)
    symbols = symbols[:10] or ["ONGC"]
    days = max(1, min(60, _int(request.args.get("days"), 20)))
    state = read_json(INDIAN_BOT_STATE_FILE, {})
    alloc = state.get("allocation") or {}
    base_budget = max(0, _num(request.args.get("budget"), _num(alloc.get("budget_per_trade"), 1000)))

    broker, err = _get_zerodha_broker(u["id"])
    if err:
        return jsonify({"error": err}), 400

    ist = pytz.timezone("Asia/Kolkata")
    to_dt = datetime.now(ist)
    from_dt = to_dt - timedelta(days=days)
    presets = db.get_trading_mode_presets()
    aggregate = {
        mode: {
            "id": mode,
            "label": preset.get("label", mode),
            "tagline": preset.get("tagline", ""),
            "score": 0.0,
            "symbols_tested": 0,
            "summary": {"trades": 0, "wins": 0, "losses": 0, "gross_pnl": 0.0,
                        "estimated_charges": 0.0, "net_pnl": 0.0, "max_drawdown": 0.0},
            "params": {},
        }
        for mode, preset in presets.items()
    }
    per_symbol = []
    errors = []

    for symbol in symbols:
        token = _zerodha_symbol_token(broker, symbol)
        if not token:
            errors.append({"symbol": symbol, "error": "instrument_not_found"})
            continue
        try:
            raw = broker.get_candles(
                token, "5minute",
                from_dt.strftime("%Y-%m-%d %H:%M:%S"),
                to_dt.strftime("%Y-%m-%d %H:%M:%S"),
            )
        except Exception as e:
            errors.append({"symbol": symbol, "error": str(e)[:160]})
            continue
        bars = _normalise_zerodha_candles(raw)
        rows = []
        for mode, preset in presets.items():
            max_positions = max(1, _int(preset.get("max_positions"), 1))
            position_pct = _num(preset.get("position_pct"), 100)
            budget = base_budget * (position_pct / 100.0)
            sim = _run_indian_backtest_sim(
                symbol, bars,
                budget=budget,
                stop_pct=_num(preset.get("stop_pct"), 1.5),
                tp_pct=_num(preset.get("tp_pct"), 3.0),
                min_conf=_int(preset.get("min_confidence"), 65),
            )
            s = sim["summary"]
            score = (
                _num(s.get("net_pnl")) -
                abs(_num(s.get("max_drawdown"))) * 0.35 +
                _num(s.get("win_rate")) * 0.08 +
                min(_int(s.get("trades")), max_positions * days) * 0.15
            )
            row = {
                "id": mode,
                "label": preset.get("label", mode),
                "tagline": preset.get("tagline", ""),
                "score": round(score, 2),
                "params": {
                    "budget": round(budget, 2),
                    "stop_pct": preset.get("stop_pct"),
                    "tp_pct": preset.get("tp_pct"),
                    "min_confidence": preset.get("min_confidence"),
                    "max_positions": max_positions,
                    "position_pct": position_pct,
                },
                "summary": s,
            }
            rows.append(row)
            agg = aggregate[mode]
            agg["score"] += score
            agg["symbols_tested"] += 1
            agg["params"] = row["params"]
            for k in ("trades", "wins", "losses"):
                agg["summary"][k] += _int(s.get(k))
            for k in ("gross_pnl", "estimated_charges", "net_pnl"):
                agg["summary"][k] += _num(s.get(k))
            agg["summary"]["max_drawdown"] += _num(s.get("max_drawdown"))
        rows.sort(key=lambda r: r["score"], reverse=True)
        per_symbol.append({
            "symbol": symbol,
            "candles": len(bars),
            "best": rows[0] if rows else None,
            "ranking": rows,
        })

    rows = []
    for row in aggregate.values():
        tested = max(1, row["symbols_tested"])
        row["score"] = round(row["score"], 2)
        row["summary"]["win_rate"] = round(
            (row["summary"]["wins"] / row["summary"]["trades"] * 100), 1
        ) if row["summary"]["trades"] else 0
        row["summary"]["max_drawdown"] = round(row["summary"]["max_drawdown"], 2)
        for k in ("gross_pnl", "estimated_charges", "net_pnl"):
            row["summary"][k] = round(row["summary"][k], 2)
        row["avg_score"] = round(row["score"] / tested, 2)
        rows.append(row)
    rows.sort(key=lambda r: r["score"], reverse=True)
    if not per_symbol:
        return jsonify({"error": "no_symbols_ranked", "details": errors}), 400
    return jsonify({
        "symbol": symbols[0],
        "symbols": symbols,
        "days": days,
        "candles": sum(p.get("candles", 0) for p in per_symbol),
        "best": rows[0] if rows else None,
        "ranking": rows,
        "per_symbol": per_symbol,
        "errors": errors,
    })


@app.route("/api/indian/alerts")
@_require_auth
def api_indian_alerts():
    payload = read_json(INDIAN_SCANNER_FILE, {})
    state = read_json(INDIAN_BOT_STATE_FILE, {})
    return jsonify({
        "updated_at": payload.get("updated_at") or state.get("last_scan"),
        "alerts": _scanner_alerts(payload, state),
    })


@app.route("/api/indian/alerts/telegram", methods=["POST"])
@_require_auth
def api_indian_alerts_telegram():
    u = _current_user()
    cfg = auth.get_telegram(u["id"])
    if not cfg or not cfg.get("enabled") or not cfg.get("bot_token") or not cfg.get("chat_id"):
        return jsonify({"error": "telegram_not_configured"}), 400
    payload = read_json(INDIAN_SCANNER_FILE, {})
    state = read_json(INDIAN_BOT_STATE_FILE, {})
    alerts = _scanner_alerts(payload, state)
    if alerts:
        lines = ["<b>Zerodha action alerts</b>"]
        for a in alerts[:8]:
            sym = f" [{a.get('symbol')}]" if a.get("symbol") else ""
            lines.append(f"• <b>{a.get('title')}{sym}</b>\n  {a.get('message')}")
        msg = "\n".join(lines)
    else:
        msg = "<b>Zerodha action alerts</b>\nNo active alerts right now."
    try:
        import requests as _rq
        r = _rq.post(
            f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage",
            json={"chat_id": cfg["chat_id"], "text": msg, "parse_mode": "HTML"},
            timeout=8,
        )
        ok = r.status_code == 200 and r.json().get("ok")
        return jsonify({"ok": bool(ok), "sent": len(alerts), "telegram_response": r.json() if r.status_code == 200 else r.text})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/indian/virtual", methods=["GET", "POST"])
@_require_auth
def api_indian_virtual():
    state = _load_virtual_state()
    scanner = read_json(INDIAN_SCANNER_FILE, {})
    scanner_rows = scanner.get("rows") or []

    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        action = (body.get("action") or "").lower()
        now = datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()
        if action == "reset":
            cash = max(100.0, min(10_000_000.0, _num(body.get("cash"), 2000)))
            state = _default_virtual_state()
            state.update({"start_cash": cash, "cash": cash, "enabled": bool(body.get("enabled", True))})
        elif action == "toggle":
            state["enabled"] = bool(body.get("enabled"))
        elif action == "seed_from_scanner":
            if not state.get("enabled"):
                state["enabled"] = True
            held = {(p.get("symbol") or "").upper() for p in state.get("positions", [])}
            candidates = [
                r for r in sorted(scanner_rows, key=lambda x: _num(x.get("score")), reverse=True)
                if r.get("status") in ("candidate", "bought") and (r.get("symbol") or "").upper() not in held
            ]
            max_new = max(1, min(10, _int(body.get("limit"), 3)))
            for r in candidates[:max_new]:
                price = _num(r.get("price"))
                if price <= 0:
                    continue
                cash = _num(state.get("cash"))
                risk_cash = min(cash, max(0.0, _num(r.get("required_margin"), cash)))
                qty = _int(r.get("qty_preview")) or int((risk_cash * 0.85) // price)
                while qty > 0 and qty * price > cash:
                    qty -= 1
                if qty <= 0:
                    continue
                state["cash"] = round(cash - qty * price, 2)
                state.setdefault("positions", []).append({
                    "symbol": (r.get("symbol") or "").upper(),
                    "qty": qty,
                    "entry": round(price, 2),
                    "last_price": round(price, 2),
                    "stop": r.get("stop"),
                    "target": r.get("target"),
                    "score": r.get("score"),
                    "opened_at": now,
                    "mode": "scanner",
                })
        elif action == "close":
            symbol = (body.get("symbol") or "").upper().strip()
            price_map = {
                (r.get("symbol") or "").upper(): _num(r.get("price"))
                for r in scanner_rows if _num(r.get("price")) > 0
            }
            kept = []
            for p in state.get("positions", []):
                sym = (p.get("symbol") or "").upper()
                if symbol and sym != symbol:
                    kept.append(p)
                    continue
                exit_price = _num(body.get("price")) or price_map.get(sym) or _num(p.get("last_price")) or _num(p.get("entry"))
                qty = _int(p.get("qty"))
                buy_value = _num(p.get("entry")) * qty
                sell_value = exit_price * qty
                charges = _estimate_zerodha_equity_intraday_charges(buy_value, sell_value)
                net = sell_value - buy_value - charges["total"]
                state["cash"] = round(_num(state.get("cash")) + sell_value - charges["total"], 2)
                state.setdefault("trades", []).append({
                    "symbol": sym,
                    "qty": qty,
                    "entry": round(_num(p.get("entry")), 2),
                    "exit": round(exit_price, 2),
                    "net_pnl": round(net, 2),
                    "charges": charges["total"],
                    "opened_at": p.get("opened_at"),
                    "closed_at": now,
                    "reason": "manual",
                })
            state["positions"] = kept
        else:
            return jsonify({"error": "unknown_action"}), 400
        state["trades"] = state.get("trades", [])[-500:]
        _save_virtual_state(state)

    summary = _mark_virtual_to_market(state, scanner_rows)
    return jsonify({
        "state": state,
        "summary": summary,
        "scanner_updated_at": scanner.get("updated_at"),
    })


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
            # Expose a 16-char tail so revoke matches are strong (16 hex chars
            # = 96 bits of entropy → effectively unguessable). Frontend just
            # echoes whatever we send back.
            "token_tail": s["token"][-16:],
        })
    return jsonify(sessions)

@app.route("/api/sessions/revoke", methods=["POST"])
@_require_auth
def api_revoke_session():
    u = _current_user()
    body = request.get_json(force=True, silent=True) or {}
    tail = body.get("token_tail", "")
    # Require at least 16 chars and only the user's own tokens are searched.
    # Use constant-time compare to defeat timing oracles.
    if not tail or len(tail) < 16:
        return jsonify({"error": "bad_request",
                        "message": "token_tail must be at least 16 chars"}), 400
    for s in auth.list_sessions(u["id"]):
        if secrets.compare_digest(s["token"][-len(tail):], tail):
            auth.delete_session(s["token"])
            auth.audit(u["id"], "session_revoked", _client_ip(),
                       {"tail": tail[-8:]})
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
@_require_auth
def api_health():
    # Real supervisor service names on this server
    svc_names = {
        "trading-bot": "trading-bot",
        "scanner":     "scanner",
        "dashboard":   "dashboard",
        "indian-bot":  "indian-bot",
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
    """Download all Alpaca trades as CSV (for tax/audit)."""
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


@app.route("/api/trades/combined")
@_require_auth
def api_trades_combined():
    """
    Merge trade logs from all brokers:
      - trade_log.json (Alpaca bot)
      - indian_trade_log.json (Indian bot)
      - Angel One trade book (live, last 50)
      - Zerodha trade book (live, last 50)

    Query params:
      ?range=day|week|month|all  (default day)
      ?broker=alpaca|angelone|zerodha|indian|all  (default all)
    """
    from datetime import timedelta
    u   = _current_user()
    rng = request.args.get("range", "day")
    broker_filter = request.args.get("broker", "all").lower()

    today  = datetime.now(ET).date()
    spans  = {"day": 0, "week": 6, "month": 29, "all": 9999}
    d_from = today - timedelta(days=spans.get(rng, 0))
    d_to   = today

    def _in_range(ts_str):
        try:
            d = datetime.strptime(str(ts_str)[:10], "%Y-%m-%d").date()
            return d_from <= d <= d_to
        except Exception:
            return True  # include if unparseable

    entries = []

    # ── Alpaca bot log ────────────────────────────────────────────
    if broker_filter in ("all", "alpaca"):
        for t in read_json(LOG_F, []):
            if _in_range(t.get("time", "")):
                t.setdefault("broker", "alpaca")
                t.setdefault("currency", "USD")
                entries.append(t)

    # ── Indian bot log ─────────────────────────────────────────────
    indian_log_f = os.path.join(BASE_DIR, "indian_trade_log.json")
    if broker_filter in ("all", "indian", "angelone", "zerodha"):
        for t in read_json(indian_log_f, []):
            if _in_range(t.get("time", "")):
                t.setdefault("broker", "indian")
                t.setdefault("currency", "INR")
                entries.append(t)

    # ── Angel One live trade book ─────────────────────────────────
    if broker_filter in ("all", "angelone"):
        try:
            broker, err = _get_angelone_broker(u["id"])
            if not err:
                ao_trades = broker.get_trade_book() or []
                _persist_angelone_tokens(u["id"], broker)
                for t in (ao_trades if isinstance(ao_trades, list) else []):
                    ts = t.get("updatetime") or t.get("ordertime") or ""
                    entries.append({
                        "time":    ts,
                        "sym":     t.get("tradingsymbol", ""),
                        "action":  "buy" if (t.get("transactiontype","") or "").upper() == "BUY" else "sell",
                        "qty":     t.get("fillshares") or t.get("quantity"),
                        "price":   t.get("tradeprice") or t.get("averageprice"),
                        "broker":  "angelone",
                        "currency": "INR",
                        "order_id": t.get("orderid"),
                        "exchange": t.get("exchange"),
                    })
        except Exception:
            pass

    # ── Zerodha live trade book ───────────────────────────────────
    if broker_filter in ("all", "zerodha"):
        try:
            broker, err = _get_zerodha_broker(u["id"])
            if not err:
                zr_trades = broker.get_trades() or []
                for t in (zr_trades if isinstance(zr_trades, list) else []):
                    ts = t.get("fill_timestamp") or t.get("order_timestamp") or ""
                    entries.append({
                        "time":    ts,
                        "sym":     t.get("tradingsymbol", ""),
                        "action":  "buy" if (t.get("transaction_type","") or "").upper() == "BUY" else "sell",
                        "qty":     t.get("filled_quantity") or t.get("quantity"),
                        "price":   t.get("average_price"),
                        "broker":  "zerodha",
                        "currency": "INR",
                        "order_id": t.get("order_id"),
                        "exchange": t.get("exchange"),
                    })
        except Exception:
            pass

    # Sort newest first
    def _ts_key(e):
        return str(e.get("time", ""))
    entries.sort(key=_ts_key, reverse=True)

    sells  = [e for e in entries if e.get("action") == "sell"]
    total_pnl_usd = round(sum(float(e.get("pnl_abs") or 0) for e in sells
                              if e.get("currency","USD") == "USD"), 2)
    total_pnl_inr = round(sum(float(e.get("pnl_abs") or 0) for e in sells
                              if e.get("currency") == "INR"), 2)

    return jsonify({
        "range":   rng,
        "broker":  broker_filter,
        "total":   len(entries),
        "trades":  entries[:500],
        "summary": {
            "realized_pnl_usd": total_pnl_usd,
            "realized_pnl_inr": total_pnl_inr,
            "total_trades":     len(entries),
            "closed_trades":    len(sells),
        },
    })


@app.route("/api/export/combined.csv")
@_require_auth
def api_export_combined_csv():
    """Download combined trades from all brokers as CSV."""
    import csv, io
    # Re-use the combined endpoint logic inline
    from datetime import timedelta
    u   = _current_user()
    today  = datetime.now(ET).date()
    d_from = today - timedelta(days=9999)
    d_to   = today
    entries = []
    for t in read_json(LOG_F, []):
        t.setdefault("broker", "alpaca"); t.setdefault("currency", "USD"); entries.append(t)
    indian_log_f = os.path.join(BASE_DIR, "indian_trade_log.json")
    for t in read_json(indian_log_f, []):
        t.setdefault("broker", "indian"); t.setdefault("currency", "INR"); entries.append(t)
    entries.sort(key=lambda e: str(e.get("time", "")), reverse=True)
    buf = io.StringIO()
    w   = csv.writer(buf)
    w.writerow(["time","symbol","action","qty","price","entry_price",
                "pct","pnl_abs","broker","currency","reason","score"])
    for t in entries:
        w.writerow([t.get("time"), t.get("sym"), t.get("action"),
                    t.get("qty"), t.get("price"), t.get("entry_price"),
                    t.get("pct"), t.get("pnl_abs"), t.get("broker"), t.get("currency"),
                    t.get("reason") or ",".join((t.get("reasons") or {}).keys()),
                    t.get("score")])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=combined_trades.csv"})

@app.route("/disclaimer")
def disclaimer():
    """Terms & disclaimer page (no auth — must be public)."""
    path = os.path.join(BASE_DIR, "templates", "disclaimer.html")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return "Disclaimer page missing.", 404

# Allowed keys for /api/config — whitelist to prevent arbitrary writes
# to strategy_params.json. Keep aligned with intraday_bot_v2.py DEFAULTS.
_STRAT_ALLOWED_KEYS = {
    "ema_fast", "ema_slow", "rsi_buy_min", "rsi_buy_max",
    "stop_loss_pct", "take_profit_pct", "partial_tp_pct",
    "max_positions", "budget_per_trade", "min_confidence",
    "daily_loss_limit_pct", "vix_pause_threshold", "vix_stop_threshold",
    "rel_vol_min", "max_drawdown_pct", "consecutive_loss_pause",
    "consecutive_loss_cooldown_min", "risk_per_trade_pct",
    "max_spread_pct", "ollama_model",
}

@app.route("/api/config", methods=["GET"])
@_require_auth
def api_config_get():
    return jsonify(read_json(STRAT_F, {}))

@app.route("/api/config", methods=["POST"])
@_require_auth
def api_config_post():
    u = _current_user()
    raw = request.get_json(force=True) or {}
    # Whitelist + type/range validation
    updates = {}
    rejected = []
    for k, v in raw.items():
        if k not in _STRAT_ALLOWED_KEYS:
            rejected.append(k)
            continue
        # Numeric fields → float; ollama_model → str
        if k == "ollama_model":
            updates[k] = str(v)[:64]
        else:
            try:
                f = float(v)
                # Sanity range — same as our budget bounds (negative→reject, huge→reject)
                if f < 0 or f > 1_000_000:
                    rejected.append(k); continue
                updates[k] = f
            except (TypeError, ValueError):
                rejected.append(k); continue
    if not updates:
        return jsonify({"error": "no_valid_fields", "rejected": rejected}), 400
    current = read_json(STRAT_F, {})
    current.update(updates)
    write_json(STRAT_F, current)
    auth.audit(u["id"], "config_updated", _client_ip(),
               {"keys": list(updates.keys()), "rejected": rejected})
    return jsonify({"ok": True, "params": current,
                    "rejected": rejected})

@app.route("/api/action", methods=["POST"])
@_require_auth
def api_action():
    action = (request.get_json(force=True) or {}).get("action", "")

    cmd_map = {
        "start":         "supervisorctl start trading-bot",
        "stop":          "supervisorctl stop trading-bot",
        "restart":       "supervisorctl restart trading-bot",
        "scan":          "supervisorctl restart scanner",
        "indian_start":  "supervisorctl start indian-bot",
        "indian_stop":   "supervisorctl stop indian-bot",
        "indian_restart":"supervisorctl restart indian-bot",
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
    existing = auth.get_telegram(u["id"])
    if not token and existing:
        token = existing.get("bot_token") or ""
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


# ── Per-broker credential validators + cached health aggregate ─────
# Each /api/<broker>/validate hits the broker's cheapest "is this user
# logged in?" endpoint. Frontend wires this to a "Re-validate" button
# on each Profile card and to a top-of-app banner that calls
# /api/brokers/health (cached 5 min) to surface silently-broken creds.

_BROKER_HEALTH_CACHE = {"data": {}, "ts": 0, "ttl": 300}
_BROKER_HEALTH_LOCK = threading.Lock()


def _validate_alpaca(uid: int) -> dict:
    creds = auth.get_alpaca_creds(uid)
    if not creds:
        return {"connected": False, "ok": False,
                "error": "no_credentials", "fix": "reconnect"}
    # DB row exists but encrypted blob decrypts to ""—master key rotation
    # or DB tampering. Surface it as connected+broken so the banner fires.
    if not creds.get("api_key") or not creds.get("secret_key"):
        return {"connected": True, "ok": False,
                "error": "credentials_corrupted",
                "fix": "reconnect"}
    try:
        ok, data = auth.validate_alpaca(creds["api_key"], creds["secret_key"],
                                        is_paper=creds["is_paper"])
        if ok:
            return {"connected": True, "ok": True,
                    "account": data.get("account_number"),
                    "status":  data.get("status"),
                    "is_paper": creds["is_paper"]}
        # 401 / 403 / etc. — credentials are present but rejected
        msg = (data or {}).get("message") or (data or {}).get("error") \
              or "stored credentials rejected"
        return {"connected": True, "ok": False,
                "error": str(msg)[:200], "fix": "reconnect"}
    except Exception as e:
        return {"connected": True, "ok": False,
                "error": str(e)[:200], "fix": "retry_or_reconnect"}


def _validate_angelone(uid: int) -> dict:
    creds = auth.get_angelone_creds(uid)
    if not creds:
        return {"connected": False, "ok": False,
                "error": "no_credentials", "fix": "reconnect"}
    if not creds.get("api_key") or not creds.get("client_id"):
        return {"connected": True, "ok": False,
                "error": "credentials_corrupted",
                "fix": "reconnect"}
    try:
        broker, err = _get_angelone_broker(uid)
        if err:
            return {"connected": True, "ok": False,
                    "error": str(err)[:200], "fix": "reconnect"}
        # ensure_logged_in() refreshes JWT if needed — same call the bot makes
        broker.ensure_logged_in()
        _persist_angelone_tokens(uid, broker)
        prof = broker.get_profile() or {}
        return {"connected": True, "ok": True,
                "client_id": creds.get("client_id"),
                "name":      (prof or {}).get("name", "")}
    except Exception as e:
        return {"connected": True, "ok": False,
                "error": str(e)[:200], "fix": "reconnect"}


def _validate_zerodha(uid: int) -> dict:
    creds = auth.get_zerodha_creds(uid)
    if not creds:
        return {"connected": False, "ok": False,
                "error": "no_credentials", "fix": "reconnect"}
    if not creds.get("api_key"):
        return {"connected": True, "ok": False,
                "error": "credentials_corrupted",
                "fix": "reconnect"}
    if not creds.get("access_token"):
        # Connected (api_key/secret saved) but no daily token yet
        return {"connected": True, "ok": False,
                "error": "needs_daily_login",
                "fix": "kite_login",
                "login_url": f"https://kite.trade/connect/login?api_key={creds['api_key']}&v=3"}
    try:
        broker, err = _get_zerodha_broker(uid)
        if err:
            return {"connected": True, "ok": False,
                    "error": str(err)[:200], "fix": "reconnect"}
        prof = broker.get_profile() or {}
        return {"connected": True, "ok": True,
                "user_id": prof.get("user_id"),
                "name":    prof.get("user_name", ""),
                "session_expiry": creds.get("session_expiry")}
    except Exception as e:
        msg = str(e)
        # Kite's "Token is invalid or has expired" is the classic daily-expiry
        if "expired" in msg.lower() or "invalid" in msg.lower():
            return {"connected": True, "ok": False,
                    "error": "session_expired", "fix": "kite_login",
                    "login_url": f"https://kite.trade/connect/login?api_key={creds['api_key']}&v=3"}
        return {"connected": True, "ok": False,
                "error": msg[:200], "fix": "reconnect"}


_BROKER_VALIDATORS = {
    "alpaca":   _validate_alpaca,
    "angelone": _validate_angelone,
    "zerodha":  _validate_zerodha,
}


@app.route("/api/<broker>/validate", methods=["POST"])
@_require_auth
def api_broker_validate(broker: str):
    """Re-test stored creds for one broker. Bypasses the health cache
    so users get an immediate answer when they click 'Re-validate'."""
    if broker not in _BROKER_VALIDATORS:
        return jsonify({"error": "unknown_broker"}), 400
    u = _current_user()
    result = _BROKER_VALIDATORS[broker](u["id"])
    auth.audit(u["id"], f"creds_validated_{broker}", _client_ip(),
               {"ok": result.get("ok")})
    # Invalidate the health cache so the banner reflects this answer next poll
    with _BROKER_HEALTH_LOCK:
        _BROKER_HEALTH_CACHE["data"].pop((u["id"], broker), None)
    return jsonify(result)


@app.route("/api/brokers/health")
@_require_auth
def api_brokers_health():
    """Aggregate health of all 3 brokers for the current user. Cached
    for 5 minutes per (user, broker) pair to keep this cheap when the
    frontend polls. Pass ?force=1 to bypass the cache."""
    u    = _current_user()
    uid  = u["id"]
    force = request.args.get("force") == "1"
    now  = time.time()
    out  = {}
    to_validate = []
    with _BROKER_HEALTH_LOCK:
        for b in _BROKER_VALIDATORS:
            entry = _BROKER_HEALTH_CACHE["data"].get((uid, b))
            if (not force) and entry and (now - entry["ts"]) < _BROKER_HEALTH_CACHE["ttl"]:
                out[b] = entry["data"]
            else:
                to_validate.append(b)
    if to_validate:
        # Run validators in parallel — broker calls are network-bound
        with _AggPool(max_workers=len(to_validate)) as pool:
            future_map = {pool.submit(_BROKER_VALIDATORS[b], uid): b for b in to_validate}
            for fut in future_map:
                b = future_map[fut]
                try:
                    out[b] = fut.result(timeout=15)
                except Exception as e:
                    out[b] = {"connected": True, "ok": False,
                              "error": str(e)[:200], "fix": "retry"}
        with _BROKER_HEALTH_LOCK:
            for b in to_validate:
                _BROKER_HEALTH_CACHE["data"][(uid, b)] = {"ts": now, "data": out[b]}

    # Compact summary for the banner
    broken = [b for b, d in out.items()
              if d.get("connected") and not d.get("ok")]
    return jsonify({
        "brokers": out,
        "broken":  broken,
        "checked_at": auth.utcnow().isoformat() + "Z",
    })


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
    """Cancel all open orders and liquidate all positions across ALL connected brokers.
    Use only in emergencies. Logged to audit."""
    u   = _current_user()
    env = _read_env_kv()
    out = {}
    import requests as _rq

    # ── Alpaca ─────────────────────────────────────────────────
    base = env.get("ALPACA_BASE_URL", "").rstrip("/")
    if base and env.get("ALPACA_API_KEY"):
        hdr = {"APCA-API-KEY-ID": env["ALPACA_API_KEY"],
               "APCA-API-SECRET-KEY": env["ALPACA_SECRET_KEY"]}
        try:
            rc = _rq.delete(base + "/orders",    headers=hdr, timeout=15)
            rp = _rq.delete(base + "/positions", headers=hdr, timeout=20)
            out["alpaca"] = {
                "cancel_orders":   {"status": rc.status_code, "body": rc.text[:300]},
                "close_positions": {"status": rp.status_code, "body": rp.text[:300]},
            }
        except Exception as e:
            out["alpaca"] = {"error": str(e)[:200]}

    # ── Angel One ──────────────────────────────────────────────
    try:
        broker, err = _get_angelone_broker(u["id"])
        if not err:
            results = broker.square_off_all_positions()
            _persist_angelone_tokens(u["id"], broker)
            out["angelone"] = {"results": results}
    except Exception as e:
        out["angelone"] = {"error": str(e)[:200]}

    # ── Zerodha ────────────────────────────────────────────────
    try:
        broker, err = _get_zerodha_broker(u["id"])
        if not err:
            results = broker.square_off_all_positions()
            out["zerodha"] = {"results": results}
    except Exception as e:
        out["zerodha"] = {"error": str(e)[:200]}

    auth.audit(u["id"], "panic_flat", _client_ip(), out)
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

            def _safe_json(url, **kw):
                """GET, raise for non-2xx, return parsed JSON. Caller catches."""
                r = _rq.get(url, headers=hdr, timeout=8, **kw)
                if r.status_code >= 400:
                    raise RuntimeError(f"alpaca {url[-32:]} HTTP {r.status_code}: {r.text[:120]}")
                return r.json()

            acct = _safe_json(env_url + "/account")
            pos  = _safe_json(env_url + "/positions")
            ords = _safe_json(env_url + "/orders",
                              params={"status":"closed","limit":20,"direction":"desc"})
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
            # Zerodha cash semantics:
            #   equity.available.live_balance = real-time cash (cash + realised PnL today)
            #   equity.available.cash         = deposited cash (static)
            #   equity.net                    = available margin for new trades
            zr_eq    = (zr_funds or {}).get("equity") or {} if isinstance(zr_funds, dict) else {}
            zr_avail = zr_eq.get("available") or {}
            zr_util  = zr_eq.get("utilised")  or {}
            available_cash = zr_avail.get("live_balance")
            if available_cash in (None, ""):
                available_cash = zr_avail.get("cash")
            return ("zerodha", {
                "connected": True,
                "available_cash":   available_cash,
                "available_margin": zr_eq.get("net"),
                "used_margin":      zr_util.get("debits"),
                "opening_balance":  zr_avail.get("opening_balance"),
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


# ── Zerodha session status endpoint ──────────────────────────────

@app.route("/api/zerodha/session_status")
@_require_auth
def api_zerodha_session_status():
    """
    Check whether the stored Zerodha access_token is still valid.
    Tries a lightweight profile call and reports result.
    """
    u = _current_user()
    creds = auth.get_zerodha_creds(u["id"])
    if not creds:
        return jsonify({"connected": False, "reason": "no_credentials"})

    from brokers.zerodha import ZerodhaBroker, ZerodhaError
    broker = ZerodhaBroker(
        api_key=creds["api_key"],
        api_secret=creds["api_secret"],
        access_token=creds["access_token"],
    )
    try:
        profile = broker.get_profile()
        return jsonify({
            "connected": True,
            "user_id":   profile.get("user_id", ""),
            "user_name": profile.get("user_name", ""),
            "session_expiry": creds.get("session_expiry", ""),
            "login_url": broker.login_url(),
        })
    except Exception as e:
        return jsonify({
            "connected": False,
            "reason": str(e)[:200],
            "login_url": broker.login_url(),
        })


# ── Zerodha daily token refresh scheduler ────────────────────────
# Runs in background thread. Each morning at 08:30 IST it checks if the
# stored access_token is expired and sends a Telegram alert with the re-login
# URL if needed. Token TTL is ~6 AM IST so by 08:30 users must re-auth.

def _zerodha_token_refresh_loop():
    import requests as _rq
    IST = pytz.timezone("Asia/Kolkata")

    def _send_tg(bot_token, chat_id, msg):
        try:
            _rq.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
                timeout=8,
            )
        except Exception:
            pass

    while True:
        try:
            now_ist = datetime.now(IST)
            # Target: 08:30 IST — fire check
            target = now_ist.replace(hour=8, minute=30, second=0, microsecond=0)
            if now_ist >= target:
                # Already past 08:30 today — schedule for tomorrow
                target += timedelta(days=1)
            secs = (target - now_ist).total_seconds()
            time.sleep(max(secs, 1))

            # Check each user's Zerodha token
            users = db.query_all("SELECT id, email FROM users")
            for u_row in users:
                uid = u_row["id"]
                try:
                    creds = auth.get_zerodha_creds(uid)
                    if not creds or not creds.get("api_key"):
                        continue

                    from brokers.zerodha import ZerodhaBroker, ZerodhaError
                    broker = ZerodhaBroker(
                        api_key=creds["api_key"],
                        api_secret=creds["api_secret"],
                        access_token=creds.get("access_token", ""),
                    )
                    token_ok = True
                    try:
                        broker.get_profile()
                    except ZerodhaError:
                        token_ok = False

                    if not token_ok:
                        # Alert via Telegram if configured
                        tg = auth.get_telegram(uid)
                        if tg and tg.get("bot_token") and tg.get("chat_id"):
                            login_url = broker.login_url()
                            msg = (
                                f"⚠️ <b>Zerodha Re-Login Required</b>\n\n"
                                f"Your Zerodha session has expired (daily reset).\n"
                                f"<b>1.</b> Click to re-login:\n{login_url}\n\n"
                                f"<b>2.</b> After login, paste the <code>request_token</code> "
                                f"from the redirect URL into the dashboard → Zerodha Settings.\n\n"
                                f"The bot will not trade until the session is refreshed."
                            )
                            _send_tg(tg["bot_token"], tg["chat_id"], msg)
                            auth.audit(uid, "zerodha_token_expired_alert_sent", "scheduler")
                except Exception:
                    pass

        except Exception:
            time.sleep(300)


_refresh_thread = threading.Thread(target=_zerodha_token_refresh_loop, daemon=True)
_refresh_thread.start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
