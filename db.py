"""
SQLite layer for AlgoTrader multi-user dashboard.
"""
import sqlite3
import os
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "users.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT    UNIQUE NOT NULL,
    password_hash   TEXT    NOT NULL,
    name            TEXT    DEFAULT '',
    role            TEXT    DEFAULT 'user',
    plan            TEXT    DEFAULT 'free',
    email_verified  INTEGER DEFAULT 0,
    theme           TEXT    DEFAULT 'dark',
    notifications   TEXT    DEFAULT '{}',
    created_at      TEXT    DEFAULT CURRENT_TIMESTAMP,
    last_login_at   TEXT
);

CREATE TABLE IF NOT EXISTS user_alpaca_creds (
    user_id         INTEGER PRIMARY KEY,
    api_key_enc     BLOB,
    secret_key_enc  BLOB,
    base_url        TEXT    DEFAULT 'https://paper-api.alpaca.markets/v2',
    data_url        TEXT    DEFAULT 'https://data.alpaca.markets/v2',
    is_paper        INTEGER DEFAULT 1,
    account_number  TEXT,
    validated_at    TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_angelone_creds (
    user_id          INTEGER PRIMARY KEY,
    api_key_enc      BLOB,
    client_id_enc    BLOB,
    password_enc     BLOB,
    totp_secret_enc  BLOB,
    jwt_token_enc    BLOB,
    refresh_token_enc BLOB,
    logged_in_at     TEXT,
    validated_at     TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_zerodha_creds (
    user_id             INTEGER PRIMARY KEY,
    api_key_enc         BLOB,
    api_secret_enc      BLOB,
    access_token_enc    BLOB,
    request_token_enc   BLOB,
    session_expiry      TEXT,
    validated_at        TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_sessions (
    token       TEXT    PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    ip          TEXT,
    user_agent  TEXT,
    created_at  TEXT    DEFAULT CURRENT_TIMESTAMP,
    expires_at  TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER,
    event       TEXT    NOT NULL,
    ip          TEXT,
    meta        TEXT,
    created_at  TEXT    DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS login_attempts (
    ip          TEXT,
    email       TEXT,
    success     INTEGER,
    created_at  TEXT    DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_telegram (
    user_id         INTEGER PRIMARY KEY,
    bot_token_enc   BLOB,
    chat_id         TEXT,
    enabled         INTEGER DEFAULT 1,
    events          TEXT    DEFAULT '{"buy":1,"sell":1,"eod":1,"vix":1,"startup":1}',
    validated_at    TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Fund allocations for each bot/broker combination.
-- budget_inr / budget_usd: max capital the bot may deploy (0 = use account default).
-- max_positions: override for this broker's bot (0 = use bot default).
-- auto_trade: 1 = bot runs fully autonomously, 0 = paused.
CREATE TABLE IF NOT EXISTS bot_fund_allocations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    broker          TEXT    NOT NULL,   -- 'zerodha' | 'angelone' | 'alpaca'
    budget          REAL    DEFAULT 0,  -- capital ceiling in broker's currency
    max_positions   INTEGER DEFAULT 0,  -- 0 = use bot default
    stop_pct        REAL    DEFAULT 0,  -- 0 = use bot default
    tp_pct          REAL    DEFAULT 0,  -- 0 = use bot default
    auto_trade      INTEGER DEFAULT 0,  -- 1 = fully autonomous
    created_at      TEXT    DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT    DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, broker),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sessions_user    ON user_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON user_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_attempts_ip      ON login_attempts(ip, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_user       ON audit_log(user_id, created_at);
"""

@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
    finally:
        c.close()

def init():
    """Create tables if they don't exist."""
    with conn() as c:
        c.executescript(SCHEMA)

def query_one(sql, params=()):
    with conn() as c:
        return c.execute(sql, params).fetchone()

def query_all(sql, params=()):
    with conn() as c:
        return c.execute(sql, params).fetchall()

def execute(sql, params=()):
    with conn() as c:
        cur = c.execute(sql, params)
        return cur.lastrowid


# ── Fund allocation helpers ────────────────────────────────────────

def get_fund_allocation(user_id: int, broker: str) -> dict:
    row = query_one(
        "SELECT * FROM bot_fund_allocations WHERE user_id=? AND broker=?",
        (user_id, broker),
    )
    if not row:
        return {"broker": broker, "budget": 0, "max_positions": 0,
                "stop_pct": 0, "tp_pct": 0, "auto_trade": 0}
    return dict(row)


def get_all_fund_allocations(user_id: int) -> list:
    rows = query_all(
        "SELECT * FROM bot_fund_allocations WHERE user_id=? ORDER BY broker",
        (user_id,),
    )
    return [dict(r) for r in rows]


def upsert_fund_allocation(user_id: int, broker: str, **kwargs) -> None:
    from datetime import datetime
    allowed = {"budget", "max_positions", "stop_pct", "tp_pct", "auto_trade"}
    fields  = {k: v for k, v in kwargs.items() if k in allowed}
    fields["updated_at"] = datetime.utcnow().isoformat()
    with conn() as c:
        row = c.execute(
            "SELECT id FROM bot_fund_allocations WHERE user_id=? AND broker=?",
            (user_id, broker),
        ).fetchone()
        if row:
            sets   = ", ".join(f"{k}=?" for k in fields)
            vals   = list(fields.values()) + [user_id, broker]
            c.execute(
                f"UPDATE bot_fund_allocations SET {sets} WHERE user_id=? AND broker=?",
                vals,
            )
        else:
            fields["user_id"] = user_id
            fields["broker"]  = broker
            cols = ", ".join(fields.keys())
            qs   = ", ".join("?" for _ in fields)
            c.execute(
                f"INSERT INTO bot_fund_allocations ({cols}) VALUES ({qs})",
                list(fields.values()),
            )
