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
