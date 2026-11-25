"""SQLite 持久化：交付记录与清单内各片验收状态。"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    manifest_hash   TEXT    NOT NULL UNIQUE,
    manifest_name   TEXT,
    label           TEXT,
    manifest_json   TEXT    NOT NULL,
    total_shards    INTEGER NOT NULL,
    total_bytes     INTEGER NOT NULL,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    recovered_index INTEGER,
    recovered_at    TEXT,
    download_count  INTEGER NOT NULL DEFAULT 0,
    last_download_at TEXT
);

CREATE TABLE IF NOT EXISTS shards (
    delivery_id     INTEGER NOT NULL REFERENCES deliveries(id) ON DELETE CASCADE,
    shard_index     INTEGER NOT NULL,
    declared_size   INTEGER NOT NULL,
    declared_sha256 TEXT    NOT NULL,
    stored_size     INTEGER,
    stored_sha256   TEXT,
    source          TEXT    CHECK (source IN ('upload', 'recovered')),
    accepted_at     TEXT,
    PRIMARY KEY (delivery_id, shard_index)
);

CREATE INDEX IF NOT EXISTS idx_shards_pending
    ON shards (delivery_id, accepted_at);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def connect(path: str) -> sqlite3.Connection:
    """建立一个带统一 pragma 的连接（调用方负责关闭）。"""
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(path: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection):
    """立即取得写锁的事务；异常时回滚，绝不留下半条记录。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_app(app) -> None:
    app.teardown_appcontext(close_db)
