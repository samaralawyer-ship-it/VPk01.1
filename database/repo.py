"""Репозиторий: вставка с дедупликацией, выборки для дайджеста, служебные логи."""

from __future__ import annotations

import json
import sqlite3
from typing import Iterable, Sequence

from .db import days_ago_iso, iso_now, writer_lock
from .models import INSERT_COLUMNS, Item

_INSERT_SQL = (
    f"INSERT INTO items ({', '.join(INSERT_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(INSERT_COLUMNS))}) "
    "ON CONFLICT(dedup_hash) DO NOTHING"
)

_SELECT_NEW_SQL = """
SELECT i.* FROM items i
WHERE i.label = ?
  AND i.created_at >= ?
  AND NOT EXISTS (SELECT 1 FROM digest_items di WHERE di.item_id = i.id)
ORDER BY i.created_at DESC, i.id DESC
LIMIT ?
"""


# --------------------------------------------------------------------------- #
# Материалы


def save_items(conn: sqlite3.Connection, items: Iterable[Item]) -> tuple[int, int]:
    """Вставить материалы с дедупликацией. Возвращает (new, dup).

    ON CONFLICT(dedup_hash) DO NOTHING вместо INSERT OR IGNORE: последний глушит
    и нарушения NOT NULL/CHECK, из-за чего не-дубликаты молча теряются.
    Статистика — по conn.total_changes, потому что changes() в executemany
    описывает только последнюю строку пакета.
    """
    rows = [item.as_row() for item in items]
    if not rows:
        return 0, 0
    with writer_lock():
        before = conn.total_changes
        conn.executemany(_INSERT_SQL, rows)
        conn.commit()
        inserted = conn.total_changes - before
    return inserted, len(rows) - inserted


def get_new_items(
    conn: sqlite3.Connection,
    label: str,
    limit: int,
    lookback_days: int,
) -> list[sqlite3.Row]:
    """Материалы метки в окне lookback_days, которых ещё не было в дайджестах.

    Окно применяется на каждом запуске, поэтому результат не зависит от того,
    первый это запуск или перезапуск после простоя.
    """
    cutoff = days_ago_iso(lookback_days)
    return conn.execute(_SELECT_NEW_SQL, (label, cutoff, limit)).fetchall()


def clear_items(
    conn: sqlite3.Connection,
    start: str | None = None,
    end: str | None = None,
    *,
    batch_size: int = 5000,
) -> dict[str, int]:
    """Удалить материалы (и связанную историю дайджестов) за период или целиком.

    Границы — ISO-строки в UTC: [start, end), то есть конец периода не включается.
    Подписки (queries), их is_active и run_log не трогаются: /clear не должен
    ломать автозапуск и историю прогонов источников.

    Почему пакетами, а не одним DELETE:
      * SQLite пишет в одном writer-локе, и один DELETE на сотни тысяч строк держал
        бы его минутами — сбор в это время стоял бы;
      * пакет по batch_size строк даёт предсказуемые паузы между транзакциями;
      * каждый пакет выбирается отдельным SELECT, поэтому не нужна поддержка
        DELETE ... LIMIT, которая есть не во всех сборках SQLite.

    Порядок удаления важен: сначала digest_items, потом items. Формально хватает
    ON DELETE CASCADE, но каскад работает только для баз, созданных с ним, а строки
    снимаем руками — так очистка одинаково работает и на старых файлах БД.
    """
    where, params = "", []
    if start is not None:
        where += " AND created_at >= ?"
        params.append(start)
    if end is not None:
        where += " AND created_at < ?"
        params.append(end)

    with writer_lock():
        deleted_items = 0
        while True:
            # Сначала находим пакет, потом удаляем по id: так не зависит от того,
            # поддерживает ли сборка SQLite DELETE ... LIMIT.
            conn.execute("BEGIN")
            ids = [
                row["id"]
                for row in conn.execute(
                    f"SELECT id FROM items WHERE 1 = 1{where} LIMIT ?", (*params, batch_size)
                )
            ]
            if not ids:
                conn.execute("COMMIT")
                break
            placeholders = ",".join("?" * len(ids))
            # ON DELETE CASCADE снимает digest_items; вручную — для БД, созданных
            # до появления каскада (PRAGMA foreign_keys их не спасёт).
            conn.execute(f"DELETE FROM digest_items WHERE item_id IN ({placeholders})", ids)
            cursor = conn.execute(f"DELETE FROM items WHERE id IN ({placeholders})", ids)
            deleted_items += cursor.rowcount
            conn.execute("COMMIT")

        # Дайджест без материалов — пустая шапка в истории: удаляем, чтобы
        # /status и список дайджестов не показывали призрачные записи.
        cursor = conn.execute(
            "DELETE FROM digest_log WHERE NOT EXISTS "
            "(SELECT 1 FROM digest_items di WHERE di.digest_id = digest_log.id)"
        )
        removed_digests = cursor.rowcount
        conn.commit()

    return {"items": deleted_items, "digests": removed_digests}


def count_items_in_range(conn: sqlite3.Connection, start: str | None = None, end: str | None = None) -> int:
    """Сколько материалов попадёт под очистку — для предупреждения перед удалением."""
    where, params = "", []
    if start is not None:
        where += " AND created_at >= ?"
        params.append(start)
    if end is not None:
        where += " AND created_at < ?"
        params.append(end)
    row = conn.execute(f"SELECT COUNT(*) AS n FROM items WHERE 1 = 1{where}", params).fetchone()
    return int(row["n"])


def existing_dedup_hashes(conn: sqlite3.Connection, hashes: Sequence[str]) -> set[str]:
    """Какие из хешей уже есть в базе — чтобы не переводить повторы.

    Перевод платный, а на каждом прогоне новыми бывают единицы материалов из сотен
    полученных. Спрашиваем базу до перевода, одним запросом.

    Запрос идёт порциями по 500 значений: у SQLite есть лимит на число параметров
    в одном запросе, а материалов от источника может прийти несколько тысяч.
    """
    unique = [value for value in dict.fromkeys(hashes) if value]     # без дублей и None
    if not unique:
        return set()
    found: set[str] = set()
    for start in range(0, len(unique), 500):
        chunk = unique[start : start + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT dedup_hash FROM items WHERE dedup_hash IN ({placeholders})", chunk
        ).fetchall()
        found.update(row["dedup_hash"] for row in rows)
    return found


def count_items(conn: sqlite3.Connection, label: str | None = None) -> int:
    if label:
        row = conn.execute("SELECT COUNT(*) AS n FROM items WHERE label = ?", (label,)).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()
    return int(row["n"])


def count_items_by_label(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT label, COUNT(*) AS n FROM items GROUP BY label").fetchall()
    return {row["label"]: int(row["n"]) for row in rows}


def count_items_since_hours(conn: sqlite3.Connection, hours: int) -> int:
    from .db import to_iso, utc_now
    from datetime import timedelta

    cutoff = to_iso(utc_now() - timedelta(hours=hours))
    row = conn.execute("SELECT COUNT(*) AS n FROM items WHERE fetched_at >= ?", (cutoff,)).fetchone()
    return int(row["n"])


def distinct_sources(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT DISTINCT source FROM items ORDER BY source").fetchall()
    return [row["source"] for row in rows]


# --------------------------------------------------------------------------- #
# Запросы (подписки)


def save_query(conn: sqlite3.Connection, query) -> None:
    """Upsert запроса из конфига. is_active — состояние рантайма, его не затираем."""
    with writer_lock():
        conn.execute(
            """
            INSERT INTO queries (query_id, label, sources, keywords, filters,
                                 limit_n, lookback_days, summarize, schedule, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE((SELECT is_active FROM queries WHERE query_id = ?), 1))
            ON CONFLICT(query_id) DO UPDATE SET
                label         = excluded.label,
                sources       = excluded.sources,
                keywords      = excluded.keywords,
                filters       = excluded.filters,
                limit_n       = excluded.limit_n,
                lookback_days = excluded.lookback_days,
                summarize     = excluded.summarize,
                schedule      = excluded.schedule
            """,
            (
                query.id,
                query.label,
                json.dumps(list(query.sources), ensure_ascii=False),
                json.dumps(query.keywords, ensure_ascii=False),
                json.dumps(query.filters, ensure_ascii=False),
                query.limit,
                query.lookback_days,
                int(query.summarize),
                query.schedule,
                query.id,
            ),
        )
        conn.commit()


def list_queries(conn: sqlite3.Connection, *, only_active: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM queries"
    if only_active:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY id"
    return conn.execute(sql).fetchall()


def set_query_active(conn: sqlite3.Connection, query_id: str, active: bool) -> int:
    with writer_lock():
        cur = conn.execute("UPDATE queries SET is_active = ? WHERE query_id = ?", (int(active), query_id))
        conn.commit()
    return cur.rowcount


def update_last_run(conn: sqlite3.Connection, query_id: str, when: str | None = None) -> None:
    """Отметить успешную отправку. Вызывается ПОСЛЕ отправки, а не до."""
    with writer_lock():
        conn.execute("UPDATE queries SET last_run = ? WHERE query_id = ?", (when or iso_now(), query_id))
        conn.commit()


# --------------------------------------------------------------------------- #
# Дайджесты


def log_digest(
    conn: sqlite3.Connection,
    query_id: str,
    chat_id: str | None,
    items_count: int,
    status: str = "sent",
) -> int:
    with writer_lock():
        cur = conn.execute(
            "INSERT INTO digest_log (query_id, chat_id, sent_at, items_count, status) VALUES (?, ?, ?, ?, ?)",
            (query_id, chat_id, iso_now(), items_count, status),
        )
        conn.commit()
    return int(cur.lastrowid)


def attach_digest_items(conn: sqlite3.Connection, digest_id: int, item_ids: Sequence[int]) -> int:
    """Зафиксировать, какие материалы ушли в дайджест. Повторная вставка игнорируется."""
    if not item_ids:
        return 0
    with writer_lock():
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO digest_items (digest_id, item_id) VALUES (?, ?)",
            [(digest_id, item_id) for item_id in item_ids],
        )
        conn.commit()
        return conn.total_changes - before


def digest_item_ids(conn: sqlite3.Connection, digest_id: int) -> list[int]:
    rows = conn.execute("SELECT item_id FROM digest_items WHERE digest_id = ?", (digest_id,)).fetchall()
    return [int(row["item_id"]) for row in rows]


def last_digests(conn: sqlite3.Connection, limit: int = 5) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM digest_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def last_digest_id(conn: sqlite3.Connection, query_id: str | None = None) -> int | None:
    """id последней записи журнала дайджестов — для отчёта о прогоне."""
    if query_id:
        row = conn.execute(
            "SELECT id FROM digest_log WHERE query_id = ? ORDER BY id DESC LIMIT 1", (query_id,)
        ).fetchone()
    else:
        row = conn.execute("SELECT id FROM digest_log ORDER BY id DESC LIMIT 1").fetchone()
    return int(row["id"]) if row else None


# --------------------------------------------------------------------------- #
# Логи запусков сборщика


def log_run_start(conn: sqlite3.Connection, source: str) -> int:
    with writer_lock():
        cur = conn.execute(
            "INSERT INTO run_log (source, started_at, status) VALUES (?, ?, 'running')",
            (source, iso_now()),
        )
        conn.commit()
    return int(cur.lastrowid)


def log_run_finish(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    *,
    items_new: int = 0,
    items_dup: int = 0,
    items_filtered: int = 0,
    error: str | None = None,
) -> None:
    with writer_lock():
        conn.execute(
            """
            UPDATE run_log
               SET finished_at = ?, status = ?, items_new = ?, items_dup = ?,
                   items_filtered = ?, error = ?
             WHERE id = ?
            """,
            (iso_now(), status, items_new, items_dup, items_filtered, error, run_id),
        )
        conn.commit()


def last_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM run_log ORDER BY id DESC LIMIT 1").fetchone()


def close_dangling_runs(conn: sqlite3.Connection, reason: str = "прерван остановкой сервиса") -> int:
    """Закрыть прогоны, оставшиеся в статусе running после падения или остановки.

    Сервис — долгоживущий процесс, и без этой уборки /status и /sources вечно
    показывают «running» для источников, которые на самом деле давно не бегут.
    Вызывается один раз при старте, до регистрации задач.
    """
    with writer_lock():
        cur = conn.execute(
            """
            UPDATE run_log
               SET finished_at = ?, status = 'interrupted', error = COALESCE(error, ?)
             WHERE status = 'running'
            """,
            (iso_now(), reason),
        )
        conn.commit()
    return cur.rowcount


def recent_runs(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM run_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
