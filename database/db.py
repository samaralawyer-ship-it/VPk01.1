"""Подключение к SQLite, идемпотентная схема и утилиты времени.

Правила проекта:
  * все даты в БД — ISO 8601 в UTC с 'T' и '+00:00' (единый формат => корректное
    лексикографическое сравнение строк);
  * время ставит Python, а не SQLite (никаких datetime('now'));
  * запись идёт через один writer-лок, журнал — WAL.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    url          TEXT,
    source       TEXT NOT NULL,
    label        TEXT NOT NULL,
    description  TEXT,
    created_at   TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    date_missing INTEGER NOT NULL DEFAULT 0,
    dedup_hash   TEXT NOT NULL,
    extra        TEXT,
    UNIQUE (dedup_hash)
);
CREATE INDEX IF NOT EXISTS idx_items_label   ON items(label);
CREATE INDEX IF NOT EXISTS idx_items_source  ON items(source);
CREATE INDEX IF NOT EXISTS idx_items_created ON items(created_at DESC);

CREATE TABLE IF NOT EXISTS queries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id       TEXT UNIQUE NOT NULL,
    label          TEXT NOT NULL,
    sources        TEXT NOT NULL,
    keywords       TEXT,
    filters        TEXT,
    limit_n        INTEGER NOT NULL DEFAULT 10,
    lookback_days  INTEGER NOT NULL DEFAULT 1,
    summarize      INTEGER NOT NULL DEFAULT 0,
    schedule       TEXT NOT NULL,
    is_active      INTEGER NOT NULL DEFAULT 1,
    last_run       TEXT
);

CREATE TABLE IF NOT EXISTS digest_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id    TEXT NOT NULL,
    chat_id     TEXT,
    sent_at     TEXT NOT NULL,
    items_count INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'sent'
);

CREATE TABLE IF NOT EXISTS digest_items (
    digest_id INTEGER NOT NULL REFERENCES digest_log(id) ON DELETE CASCADE,
    item_id   INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    PRIMARY KEY (digest_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_digest_items_item ON digest_items(item_id);

CREATE TABLE IF NOT EXISTS run_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source         TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL,
    items_new      INTEGER DEFAULT 0,
    items_dup      INTEGER DEFAULT 0,
    items_filtered INTEGER DEFAULT 0,
    error          TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_log_started ON run_log(started_at DESC);
"""

_WRITE_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Время


def utc_now() -> datetime:
    """Текущее время UTC с точностью до секунды."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(value: datetime) -> str:
    """Привести datetime к единому формату хранения: 2026-10-09T05:09:15+00:00."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def iso_now() -> str:
    return to_iso(utc_now())


def days_ago_iso(days: int) -> str:
    return to_iso(utc_now() - timedelta(days=days))


def minutes_ago_iso(minutes: int) -> str:
    return to_iso(utc_now() - timedelta(minutes=minutes))


# --------------------------------------------------------------------------- #
# Подключение


def connect(db_path: str | Path, *, create_dirs: bool = True) -> sqlite3.Connection:
    """Открыть БД с нужными PRAGMA. Для ':memory:' каталоги не создаются."""
    target = str(db_path)
    if target != ":memory:":
        path = Path(target)
        if create_dirs and path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(target, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Создать схему и дотянуть недостающие колонки. Идемпотентно."""
    with writer_lock():
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
        conn.commit()


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Простейшие миграции для БД, созданных более ранней версией."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    if "extra" not in existing:
        conn.execute("ALTER TABLE items ADD COLUMN extra TEXT")


@contextmanager
def writer_lock() -> Iterator[None]:
    """Единый writer на процесс: SQLite не любит параллельную запись."""
    with _WRITE_LOCK:
        yield
