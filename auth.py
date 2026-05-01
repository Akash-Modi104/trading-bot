"""
Authentication, session, and per-user Alpaca credential management.
"""
import os
import json
import secrets
import bcrypt
import requests
from datetime import datetime, timedelta
from cryptography.fernet import Fernet, InvalidToken

import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")

# ── Fernet master key (auto-generated on first run, persisted to .env) ──
def _load_or_create_master_key():
    key = os.environ.get("MASTER_ENCRYPTION_KEY", "").strip()
    if key:
        try:
            Fernet(key.encode())
            return key
        except Exception:
            pass
    # Generate and persist
    new_key = Fernet.generate_key().decode()
    try:
        # Append to .env so subsequent restarts pick it up
        existing = ""
        if os.path.exists(ENV_FILE):
            with open(ENV_FILE) as f:
                existing = f.read()
        # Replace existing line if any, else append
        lines = [l for l in existing.split("\n") if not l.startswith("MASTER_ENCRYPTION_KEY=")]
        lines.append(f"MASTER_ENCRYPTION_KEY={new_key}")
        with open(ENV_FILE, "w") as f:
            f.write("\n".join(l for l in lines if l.strip()) + "\n")
    except Exception:
        pass
    os.environ["MASTER_ENCRYPTION_KEY"] = new_key
    return new_key

_MASTER_KEY = _load_or_create_master_key()
_fernet = Fernet(_MASTER_KEY.encode())

# ── Password hashing ──────────────────────────────────────────────
def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(12)).decode()

def check_pw(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except Exception:
        return False

# ── Encryption ────────────────────────────────────────────────────
def encrypt(s: str) -> bytes:
    return _fernet.encrypt(s.encode())

def decrypt(b) -> str:
    if isinstance(b, str):
        b = b.encode()
    try:
        return _fernet.decrypt(b).decode()
    except InvalidToken:
        return ""

# ── User CRUD ────────────────────────────────────────────────────
def create_user(email: str, password: str, name: str = "", role: str = "user"):
    email = email.lower().strip()
    if get_user_by_email(email):
        raise ValueError("Email already registered")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    db.execute(
        "INSERT INTO users(email, password_hash, name, role) VALUES (?,?,?,?)",
        (email, hash_pw(password), name, role),
    )
    return get_user_by_email(email)

def get_user(uid: int):
    return db.query_one("SELECT * FROM users WHERE id=?", (uid,))

def get_user_by_email(email: str):
    return db.query_one("SELECT * FROM users WHERE email=?", (email.lower().strip(),))

def update_user(uid: int, **fields):
    allowed = {"name", "theme", "notifications", "plan", "email_verified", "last_login_at"}
    sets = []
    vals = []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            vals.append(v if not isinstance(v, dict) else json.dumps(v))
    if not sets:
        return
    vals.append(uid)
    db.execute(f"UPDATE users SET {','.join(sets)} WHERE id=?", tuple(vals))

def change_password(uid: int, new_password: str):
    if len(new_password) < 8:
        raise ValueError("Password must be at least 8 characters")
    db.execute("UPDATE users SET password_hash=? WHERE id=?",
               (hash_pw(new_password), uid))
    # Revoke all sessions for this user
    db.execute("DELETE FROM user_sessions WHERE user_id=?", (uid,))

# ── Sessions ──────────────────────────────────────────────────────
def create_session(user_id: int, ip: str = "", ua: str = "", days: int = 30) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.utcnow() + timedelta(days=days)).isoformat()
    db.execute(
        "INSERT INTO user_sessions(token,user_id,ip,user_agent,expires_at) VALUES (?,?,?,?,?)",
        (token, user_id, ip, ua[:500], expires),
    )
    return token

def get_user_by_session(token: str):
    if not token:
        return None
    s = db.query_one(
        "SELECT user_id, expires_at FROM user_sessions WHERE token=?",
        (token,),
    )
    if not s:
        return None
    try:
        if datetime.fromisoformat(s["expires_at"]) < datetime.utcnow():
            return None
    except Exception:
        return None
    return get_user(s["user_id"])

def delete_session(token: str):
    if not token:
        return
    db.execute("DELETE FROM user_sessions WHERE token=?", (token,))

def list_sessions(user_id: int):
    return db.query_all(
        "SELECT token, ip, user_agent, created_at, expires_at "
        "FROM user_sessions WHERE user_id=? ORDER BY created_at DESC",
        (user_id,),
    )

def cleanup_expired_sessions():
    db.execute(
        "DELETE FROM user_sessions WHERE expires_at < ?",
        (datetime.utcnow().isoformat(),),
    )

# ── Login rate limiting ──────────────────────────────────────────
def record_login_attempt(ip: str, email: str, success: bool):
    db.execute(
        "INSERT INTO login_attempts(ip, email, success) VALUES (?,?,?)",
        (ip, email.lower(), 1 if success else 0),
    )

def is_rate_limited(ip: str, window_min: int = 15, max_attempts: int = 5) -> bool:
    cutoff = (datetime.utcnow() - timedelta(minutes=window_min)).isoformat()
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM login_attempts "
        "WHERE ip=? AND success=0 AND created_at > ?",
        (ip, cutoff),
    )
    return (row["n"] if row else 0) >= max_attempts

# ── Audit ────────────────────────────────────────────────────────
def audit(user_id, event: str, ip: str = "", meta=""):
    if isinstance(meta, dict):
        meta = json.dumps(meta)
    db.execute(
        "INSERT INTO audit_log(user_id, event, ip, meta) VALUES (?,?,?,?)",
        (user_id, event, ip, meta),
    )

def get_audit(user_id: int, limit: int = 50):
    return db.query_all(
        "SELECT event, ip, meta, created_at FROM audit_log "
        "WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    )

# ── Per-user Alpaca credentials ──────────────────────────────────
def validate_alpaca(api_key: str, secret_key: str, is_paper: bool = True):
    """Hit Alpaca /account to verify the keys work. Returns (ok, account_dict_or_error)."""
    base = ("https://paper-api.alpaca.markets/v2" if is_paper
            else "https://api.alpaca.markets/v2")
    try:
        r = requests.get(
            f"{base}/account",
            headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key},
            timeout=10,
        )
        if r.status_code == 200:
            return True, r.json()
        return False, r.json() if r.content else {"error": f"HTTP {r.status_code}"}
    except Exception as e:
        return False, {"error": str(e)}

def save_alpaca_creds(user_id: int, api_key: str, secret_key: str,
                      is_paper: bool = True, account_number: str = ""):
    base_url = ("https://paper-api.alpaca.markets/v2" if is_paper
                else "https://api.alpaca.markets/v2")
    db.execute(
        """INSERT OR REPLACE INTO user_alpaca_creds
           (user_id, api_key_enc, secret_key_enc, base_url, is_paper, account_number, validated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (user_id, encrypt(api_key), encrypt(secret_key), base_url,
         1 if is_paper else 0, account_number,
         datetime.utcnow().isoformat()),
    )

def get_alpaca_creds(user_id: int):
    r = db.query_one("SELECT * FROM user_alpaca_creds WHERE user_id=?", (user_id,))
    if not r:
        return None
    return {
        "api_key":        decrypt(r["api_key_enc"]),
        "secret_key":     decrypt(r["secret_key_enc"]),
        "base_url":       r["base_url"],
        "data_url":       r["data_url"],
        "is_paper":       bool(r["is_paper"]),
        "account_number": r["account_number"],
        "validated_at":   r["validated_at"],
    }

def get_alpaca_status(user_id: int) -> dict:
    """Public-safe credential status (no secrets)."""
    r = db.query_one("SELECT * FROM user_alpaca_creds WHERE user_id=?", (user_id,))
    if not r:
        return {"connected": False}
    return {
        "connected":      True,
        "is_paper":       bool(r["is_paper"]),
        "account_number": r["account_number"],
        "validated_at":   r["validated_at"],
        "key_preview":    "****" + decrypt(r["api_key_enc"])[-4:] if r["api_key_enc"] else "",
    }

def delete_alpaca_creds(user_id: int):
    db.execute("DELETE FROM user_alpaca_creds WHERE user_id=?", (user_id,))

# ── Per-user Angel One credentials ───────────────────────────────

def validate_angelone(api_key: str, client_id: str, password: str, totp_secret: str):
    """
    Try to log in to Angel One SmartAPI.
    Returns (ok: bool, data_or_error: dict).
    """
    try:
        from brokers.angelone import AngelOneBroker, AngelOneError
        broker = AngelOneBroker(api_key, client_id, password, totp_secret)
        data = broker.login()
        return True, data
    except Exception as e:
        return False, {"error": str(e)}

def save_angelone_creds(user_id: int, api_key: str, client_id: str,
                        password: str, totp_secret: str,
                        jwt_token: str = "", refresh_token: str = "",
                        logged_in_at: str = ""):
    db.execute(
        """INSERT OR REPLACE INTO user_angelone_creds
           (user_id, api_key_enc, client_id_enc, password_enc, totp_secret_enc,
            jwt_token_enc, refresh_token_enc, logged_in_at, validated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            user_id,
            encrypt(api_key),
            encrypt(client_id),
            encrypt(password),
            encrypt(totp_secret),
            encrypt(jwt_token) if jwt_token else b"",
            encrypt(refresh_token) if refresh_token else b"",
            logged_in_at or "",
            datetime.utcnow().isoformat(),
        ),
    )

def get_angelone_creds(user_id: int):
    r = db.query_one("SELECT * FROM user_angelone_creds WHERE user_id=?", (user_id,))
    if not r:
        return None
    return {
        "api_key":       decrypt(r["api_key_enc"]),
        "client_id":     decrypt(r["client_id_enc"]),
        "password":      decrypt(r["password_enc"]),
        "totp_secret":   decrypt(r["totp_secret_enc"]),
        "jwt_token":     decrypt(r["jwt_token_enc"]) if r["jwt_token_enc"] else "",
        "refresh_token": decrypt(r["refresh_token_enc"]) if r["refresh_token_enc"] else "",
        "logged_in_at":  r["logged_in_at"],
        "validated_at":  r["validated_at"],
    }

def get_angelone_status(user_id: int) -> dict:
    r = db.query_one("SELECT * FROM user_angelone_creds WHERE user_id=?", (user_id,))
    if not r:
        return {"connected": False}
    client_id = decrypt(r["client_id_enc"]) if r["client_id_enc"] else ""
    return {
        "connected":    True,
        "client_id":    client_id,
        "validated_at": r["validated_at"],
        "logged_in_at": r["logged_in_at"],
    }

def update_angelone_tokens(user_id: int, jwt_token: str, refresh_token: str,
                            logged_in_at: str = ""):
    """Persist refreshed tokens without touching the core credentials."""
    ts = logged_in_at or datetime.utcnow().isoformat()
    db.execute(
        """UPDATE user_angelone_creds
           SET jwt_token_enc=?, refresh_token_enc=?, logged_in_at=?
           WHERE user_id=?""",
        (encrypt(jwt_token), encrypt(refresh_token), ts, user_id),
    )

def delete_angelone_creds(user_id: int):
    db.execute("DELETE FROM user_angelone_creds WHERE user_id=?", (user_id,))

# ── Per-user Zerodha credentials ─────────────────────────────────

def save_zerodha_creds(user_id: int, api_key: str, api_secret: str,
                       access_token: str = "", request_token: str = "",
                       session_expiry: str = ""):
    db.execute(
        """INSERT OR REPLACE INTO user_zerodha_creds
           (user_id, api_key_enc, api_secret_enc, access_token_enc,
            request_token_enc, session_expiry, validated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (
            user_id,
            encrypt(api_key),
            encrypt(api_secret),
            encrypt(access_token) if access_token else b"",
            encrypt(request_token) if request_token else b"",
            session_expiry or "",
            datetime.utcnow().isoformat(),
        ),
    )

def get_zerodha_creds(user_id: int):
    r = db.query_one("SELECT * FROM user_zerodha_creds WHERE user_id=?", (user_id,))
    if not r:
        return None
    return {
        "api_key":       decrypt(r["api_key_enc"]),
        "api_secret":    decrypt(r["api_secret_enc"]),
        "access_token":  decrypt(r["access_token_enc"]) if r["access_token_enc"] else "",
        "request_token": decrypt(r["request_token_enc"]) if r["request_token_enc"] else "",
        "session_expiry": r["session_expiry"],
        "validated_at":  r["validated_at"],
    }

def get_zerodha_status(user_id: int) -> dict:
    r = db.query_one("SELECT * FROM user_zerodha_creds WHERE user_id=?", (user_id,))
    if not r:
        return {"connected": False}
    has_token = bool(r["access_token_enc"])
    api_key   = decrypt(r["api_key_enc"]) if r["api_key_enc"] else ""
    return {
        "connected":      True,
        "has_access_token": has_token,
        "session_expiry": r["session_expiry"],
        "validated_at":   r["validated_at"],
        "login_url":      f"https://kite.trade/connect/login?api_key={api_key}&v=3" if api_key else "",
    }

def update_zerodha_access_token(user_id: int, access_token: str, session_expiry: str = ""):
    db.execute(
        """UPDATE user_zerodha_creds
           SET access_token_enc=?, session_expiry=?
           WHERE user_id=?""",
        (encrypt(access_token), session_expiry, user_id),
    )

def delete_zerodha_creds(user_id: int):
    db.execute("DELETE FROM user_zerodha_creds WHERE user_id=?", (user_id,))

# ── Bootstrap admin from .env on first run ───────────────────────
def bootstrap_admin_from_env():
    """Migrate the legacy DASHBOARD_USER/DASHBOARD_PASS into a real user account.
    Runs once on first startup if no users exist."""
    db.init()
    n = db.query_one("SELECT COUNT(*) AS n FROM users")
    if n and n["n"] > 0:
        return
    user = os.environ.get("DASHBOARD_USER", "admin").strip()
    pw   = os.environ.get("DASHBOARD_PASS", "").strip()
    if not pw:
        return  # nothing to migrate
    # Treat DASHBOARD_USER as email if it has @, else synthesize one
    email = user if "@" in user else f"{user}@local"
    try:
        create_user(email=email, password=pw, name=user, role="admin")
        # Auto-link to the operator's Alpaca creds from .env (if present)
        api_k  = os.environ.get("ALPACA_API_KEY", "").strip()
        sec_k  = os.environ.get("ALPACA_SECRET_KEY", "").strip()
        is_pap = "paper" in os.environ.get("ALPACA_BASE_URL", "paper").lower()
        if api_k and sec_k:
            u = get_user_by_email(email)
            save_alpaca_creds(u["id"], api_k, sec_k, is_paper=is_pap,
                              account_number="bootstrapped")
        print(f"[auth] Bootstrapped admin user: {email}")
    except Exception as e:
        print(f"[auth] Bootstrap failed: {e}")

# ── Telegram per-user credentials ─────────────────────────────────
def save_telegram(user_id: int, bot_token: str, chat_id: str,
                  enabled: bool = True, events: dict = None):
    """Encrypt + persist a user's Telegram bot credentials. Upsert."""
    token_enc = encrypt(bot_token.strip()) if bot_token else None
    ev = json.dumps(events) if events else '{"buy":1,"sell":1,"eod":1,"vix":1,"startup":1}'
    db.execute("""
        INSERT INTO user_telegram(user_id, bot_token_enc, chat_id, enabled, events, validated_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(user_id) DO UPDATE SET
            bot_token_enc=excluded.bot_token_enc,
            chat_id=excluded.chat_id,
            enabled=excluded.enabled,
            events=excluded.events,
            validated_at=datetime('now')
    """, (user_id, token_enc, chat_id.strip(), 1 if enabled else 0, ev))

def get_telegram(user_id: int):
    row = db.query_one("SELECT * FROM user_telegram WHERE user_id=?", (user_id,))
    if not row:
        return None
    return {
        "user_id": row["user_id"],
        "bot_token": decrypt(row["bot_token_enc"]) if row["bot_token_enc"] else "",
        "chat_id": row["chat_id"] or "",
        "enabled": bool(row["enabled"]),
        "events": json.loads(row["events"] or "{}"),
        "validated_at": row["validated_at"],
    }

def delete_telegram(user_id: int):
    db.execute("DELETE FROM user_telegram WHERE user_id=?", (user_id,))

def list_active_telegram():
    """Used by the bot to dispatch alerts to all subscribed users."""
    rows = db.query_all("SELECT * FROM user_telegram WHERE enabled=1")
    out = []
    for r in rows:
        token = decrypt(r["bot_token_enc"]) if r["bot_token_enc"] else ""
        if not token or not r["chat_id"]:
            continue
        try:
            events = json.loads(r["events"] or "{}")
        except Exception:
            events = {}
        out.append({"token": token, "chat_id": r["chat_id"], "events": events})
    return out

