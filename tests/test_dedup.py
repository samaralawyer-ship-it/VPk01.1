"""Тесты дедупликации, окон отбора и служебных таблиц.

Здесь живут критерии приёмки №3 (дедупликация) и №12 (перезапуск без повторов).
"""

from __future__ import annotations

import pytest

from collectors.normalize import build_item
from config.loader import DedupConfig, Query
from database import connect, init_schema, repo
from database.db import days_ago_iso, iso_now
from database.models import RawItem

DEDUP = DedupConfig(strip_params=("utm_source", "utm_medium", "gclid", "fbclid"))


@pytest.fixture
def conn(workdir):
    connection = connect(workdir / "test.db")
    init_schema(connection)
    yield connection
    connection.close()


def make_item(
    title: str,
    url: str | None = None,
    *,
    source: str = "tass_news",
    label: str = "news",
    created_at: str | None = None,
    description: str = "",
):
    if created_at is None:
        created_at = iso_now()
    raw = RawItem(
        title=title,
        url=url,
        description=description,
        created_at=created_at,
        source=source,
        label=label,
    )
    return build_item(raw, source, label, DEDUP)


class TestSchema:
    def test_init_schema_is_idempotent(self, conn):
        init_schema(conn)
        init_schema(conn)
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        assert {"items", "queries", "digest_log", "digest_items", "run_log"} <= tables


class TestDeduplication:
    def test_first_save_all_new(self, conn):
        items = [make_item("A", "https://e.com/a"), make_item("B", "https://e.com/b")]
        assert repo.save_items(conn, items) == (2, 0)
        assert repo.count_items(conn) == 2

    def test_second_save_all_duplicates(self, conn):
        items = [make_item("A", "https://e.com/a"), make_item("B", "https://e.com/b")]
        repo.save_items(conn, items)
        assert repo.save_items(conn, items) == (0, 2)
        assert repo.count_items(conn) == 2

    def test_mixed_batch(self, conn):
        repo.save_items(conn, [make_item("A", "https://e.com/a")])
        batch = [
            make_item("A", "https://e.com/a"),          # дубль
            make_item("B", "https://e.com/b"),          # новый
            make_item("C", "https://e.com/c"),          # новый
        ]
        assert repo.save_items(conn, batch) == (2, 1)

    def test_tracking_params_do_not_create_duplicates(self, conn):
        repo.save_items(conn, [make_item("A", "https://e.com/a")])
        again = make_item("A", "https://e.com/a?utm_source=telegram&utm_medium=social")
        assert repo.save_items(conn, [again]) == (0, 1)
        assert repo.count_items(conn) == 1

    def test_without_url_dedup_by_title_and_source(self, conn):
        assert repo.save_items(conn, [make_item("Python-разработчик")]) == (1, 0)
        assert repo.save_items(conn, [make_item("  python-разработчик ")]) == (0, 1)
        assert repo.save_items(conn, [make_item("Python-разработчик", source="habr_career")]) == (1, 0)
        assert repo.count_items(conn) == 2

    def test_same_title_different_urls_are_different_items(self, conn):
        assert repo.save_items(conn, [make_item("A", "https://e.com/a")]) == (1, 0)
        assert repo.save_items(conn, [make_item("A", "https://e.com/a2")]) == (1, 0)

    def test_restart_does_not_duplicate(self, workdir):
        """Критерий №12: остановили, запустили снова — повторов нет."""
        db_path = workdir / "restart.db"
        items = [make_item("A", "https://e.com/a"), make_item("B", "https://e.com/b")]

        first = connect(db_path)
        init_schema(first)
        assert repo.save_items(first, items) == (2, 0)
        first.close()

        second = connect(db_path)
        init_schema(second)
        assert repo.save_items(second, items) == (0, 2)
        assert repo.count_items(second) == 2
        second.close()

    def test_not_null_violation_is_not_silently_swallowed(self, conn):
        """ON CONFLICT DO NOTHING не должен глушить другие нарушения схемы."""
        import sqlite3

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO items (title, source, label, created_at, fetched_at, dedup_hash) "
                "VALUES (NULL, 's', 'news', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 'h')"
            )


class TestNewItemsSelection:
    def test_lookback_window_applies_every_run(self, conn):
        repo.save_items(
            conn,
            [
                make_item("Свежая", "https://e.com/fresh", created_at=iso_now()),
                make_item("Старая", "https://e.com/old", created_at=days_ago_iso(10)),
            ],
        )
        fresh_only = repo.get_new_items(conn, "news", limit=10, lookback_days=1)
        assert [row["title"] for row in fresh_only] == ["Свежая"]

        wide = repo.get_new_items(conn, "news", limit=10, lookback_days=30)
        assert len(wide) == 2

    def test_limit_and_order(self, conn):
        repo.save_items(
            conn,
            [
                make_item("A", "https://e.com/a", created_at=days_ago_iso(3)),
                make_item("B", "https://e.com/b", created_at=days_ago_iso(2)),
                make_item("C", "https://e.com/c", created_at=days_ago_iso(1)),
            ],
        )
        rows = repo.get_new_items(conn, "news", limit=2, lookback_days=7)
        assert [row["title"] for row in rows] == ["C", "B"]

    def test_label_filter(self, conn):
        repo.save_items(
            conn,
            [
                make_item("Новость", "https://e.com/news", label="news"),
                make_item("Вакансия", "https://e.com/vac", label="vacancies"),
            ],
        )
        rows = repo.get_new_items(conn, "vacancies", limit=10, lookback_days=1)
        assert [row["title"] for row in rows] == ["Вакансия"]

    def test_sent_items_are_excluded_from_next_digest(self, conn):
        repo.save_items(conn, [make_item("A", "https://e.com/a"), make_item("B", "https://e.com/b")])
        rows = repo.get_new_items(conn, "news", limit=10, lookback_days=1)
        assert len(rows) == 2

        digest_id = repo.log_digest(conn, "daily_news", "123", len(rows))
        assert repo.attach_digest_items(conn, digest_id, [row["id"] for row in rows]) == 2

        assert repo.get_new_items(conn, "news", limit=10, lookback_days=1) == []
        assert sorted(repo.digest_item_ids(conn, digest_id)) == sorted(row["id"] for row in rows)


class TestServiceTables:
    def test_digest_log_records_failure(self, conn):
        digest_id = repo.log_digest(conn, "daily_news", "123", 0, status="failed")
        row = conn.execute("SELECT * FROM digest_log WHERE id = ?", (digest_id,)).fetchone()
        assert row["status"] == "failed"
        assert row["sent_at"].endswith("+00:00")

    def test_attach_digest_items_is_idempotent(self, conn):
        repo.save_items(conn, [make_item("A", "https://e.com/a")])
        item_id = conn.execute("SELECT id FROM items").fetchone()["id"]
        digest_id = repo.log_digest(conn, "daily_news", "123", 1)
        assert repo.attach_digest_items(conn, digest_id, [item_id]) == 1
        assert repo.attach_digest_items(conn, digest_id, [item_id]) == 0

    def test_run_log_lifecycle(self, conn):
        run_id = repo.log_run_start(conn, "tass_news")
        running = conn.execute("SELECT * FROM run_log WHERE id = ?", (run_id,)).fetchone()
        assert running["status"] == "running" and running["finished_at"] is None

        repo.log_run_finish(conn, run_id, "ok", items_new=5, items_dup=2, items_filtered=1)
        done = repo.last_run(conn)
        assert done["status"] == "ok"
        assert (done["items_new"], done["items_dup"], done["items_filtered"]) == (5, 2, 1)
        assert done["finished_at"].endswith("+00:00")

    def test_run_log_error_text(self, conn):
        run_id = repo.log_run_start(conn, "github_trending")
        repo.log_run_finish(conn, run_id, "error", error="403 Forbidden: rate limit")
        assert "403" in repo.last_run(conn)["error"]

    def test_dangling_runs_are_closed_on_start(self, conn):
        """Прогон, оборванный остановкой сервиса, не должен вечно висеть в running."""
        run_id = repo.log_run_start(conn, "kommersant_news")
        finished_id = repo.log_run_start(conn, "tass_news")
        repo.log_run_finish(conn, finished_id, "ok", items_new=1)

        assert repo.close_dangling_runs(conn) == 1

        closed = conn.execute("SELECT * FROM run_log WHERE id = ?", (run_id,)).fetchone()
        assert closed["status"] == "interrupted"
        assert closed["finished_at"].endswith("+00:00")
        assert closed["error"]

        done = conn.execute("SELECT * FROM run_log WHERE id = ?", (finished_id,)).fetchone()
        assert done["status"] == "ok"                  # завершённые не трогаем
        assert repo.close_dangling_runs(conn) == 0     # повторный вызов ничего не находит

    def test_save_query_upsert_keeps_runtime_state(self, conn):
        query = Query(
            id="daily_news",
            label="news",
            schedule="0 9 * * *",
            limit=10,
            lookback_days=1,
            tg_chat_id="123",
        )
        repo.save_query(conn, query)
        assert repo.set_query_active(conn, "daily_news", False) == 1

        updated = Query(
            id="daily_news",
            label="news",
            schedule="30 8 * * *",
            limit=5,
            lookback_days=2,
            tg_chat_id="123",
        )
        repo.save_query(conn, updated)

        row = conn.execute("SELECT * FROM queries WHERE query_id = 'daily_news'").fetchone()
        assert row["is_active"] == 0            # рантайм-состояние не затёрто конфигом
        assert row["limit_n"] == 5
        assert row["lookback_days"] == 2
        assert row["schedule"] == "30 8 * * *"

    def test_update_last_run_only_after_send(self, conn):
        repo.save_query(conn, Query(id="daily_news", label="news", schedule="0 9 * * *", tg_chat_id="123"))
        assert conn.execute("SELECT last_run FROM queries").fetchone()["last_run"] is None
        repo.update_last_run(conn, "daily_news")
        assert conn.execute("SELECT last_run FROM queries").fetchone()["last_run"].endswith("+00:00")


def iso(day: int, hour: int = 12) -> str:
    return f"2026-10-{day:02d}T{hour:02d}:00:00+00:00"


class TestClearItems:
    """/clear: удаление материалов за период или целиком, с историей дайджестов."""

    def test_counts_items_in_range(self, conn):
        for day in (1, 2, 3, 4):
            repo.save_items(conn, [make_item(f"Новость {day}", f"https://e.com/{day}", created_at=iso(day))])

        assert repo.count_items_in_range(conn) == 4
        assert repo.count_items_in_range(conn, iso(2), iso(4)) == 2      # 2 и 3
        assert repo.count_items_in_range(conn, iso(5)) == 0

    def test_period_deletes_only_that_window(self, conn):
        for day in (1, 2, 3, 4, 5):
            repo.save_items(conn, [make_item(f"Новость {day}", f"https://e.com/{day}", created_at=iso(day))])

        result = repo.clear_items(conn, iso(2), iso(4))                  # [2, 4) — дни 2 и 3

        assert result["items"] == 2
        left = [row["title"] for row in conn.execute("SELECT title FROM items ORDER BY id")]
        assert left == ["Новость 1", "Новость 4", "Новость 5"]

    def test_end_boundary_is_exclusive(self, conn):
        repo.save_items(conn, [make_item("Внутри", "https://e.com/a", created_at=iso(3))])
        repo.save_items(conn, [make_item("На границе", "https://e.com/b", created_at=iso(4))])

        repo.clear_items(conn, iso(3), iso(4))                           # день 4 не трогаем

        assert [row["title"] for row in conn.execute("SELECT title FROM items")] == ["На границе"]

    def test_full_clear_removes_everything(self, conn):
        for day in (1, 2, 3):
            repo.save_items(conn, [make_item(f"Новость {day}", f"https://e.com/{day}", created_at=iso(day))])

        assert repo.clear_items(conn)["items"] == 3
        assert repo.count_items(conn) == 0
        assert repo.clear_items(conn) == {"items": 0, "digests": 0}      # повторная очистка безопасна

    def test_digest_history_goes_with_items(self, conn):
        repo.save_items(conn, [make_item("Новость", "https://e.com/a", created_at=iso(1))])
        item_id = conn.execute("SELECT id FROM items").fetchone()["id"]
        digest_id = repo.log_digest(conn, "daily_news", "555", 1)
        repo.attach_digest_items(conn, digest_id, [item_id])

        result = repo.clear_items(conn)

        assert result == {"items": 1, "digests": 1}
        assert conn.execute("SELECT count(*) FROM digest_items").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM digest_log").fetchone()[0] == 0

    def test_digest_kept_while_it_has_other_items(self, conn):
        for day in (1, 5):
            repo.save_items(conn, [make_item(f"Новость {day}", f"https://e.com/{day}", created_at=iso(day))])
        ids = [row["id"] for row in conn.execute("SELECT id FROM items ORDER BY id")]
        digest_id = repo.log_digest(conn, "daily_news", "555", 2)
        repo.attach_digest_items(conn, digest_id, ids)

        result = repo.clear_items(conn, iso(1), iso(2))                  # уходит только 1-й

        assert result["digests"] == 0                                    # дайджест ещё не пуст
        assert repo.digest_item_ids(conn, digest_id) == [ids[1]]
        assert conn.execute("SELECT count(*) FROM digest_items").fetchone()[0] == 1

    def test_no_orphan_digest_items(self, conn):
        repo.save_items(conn, [make_item("Новость", "https://e.com/a", created_at=iso(1))])
        item_id = conn.execute("SELECT id FROM items").fetchone()["id"]
        digest_id = repo.log_digest(conn, "daily_news", "555", 1)
        repo.attach_digest_items(conn, digest_id, [item_id])

        repo.clear_items(conn, iso(1), iso(2))

        orphans = conn.execute(
            "SELECT count(*) FROM digest_items di LEFT JOIN items i ON i.id = di.item_id WHERE i.id IS NULL"
        ).fetchone()[0]
        assert orphans == 0

    def test_queries_and_run_log_survive(self, conn):
        repo.save_query(conn, Query(id="daily_news", label="news", schedule="0 9 * * *", tg_chat_id="123"))
        repo.set_query_active(conn, "daily_news", False)
        run_id = repo.log_run_start(conn, "tass_news")
        repo.log_run_finish(conn, run_id, "ok", items_new=1)
        repo.save_items(conn, [make_item("Новость", "https://e.com/a", created_at=iso(1))])

        repo.clear_items(conn)

        assert repo.count_items(conn) == 0
        row = conn.execute("SELECT * FROM queries WHERE query_id = 'daily_news'").fetchone()
        assert row is not None
        assert row["is_active"] == 0                    # /clear не включает автозапуск обратно
        assert row["schedule"] == "0 9 * * *"
        assert conn.execute("SELECT count(*) FROM run_log").fetchone()[0] == 1

    def test_batching_deletes_more_than_one_batch(self, conn):
        for index in range(12):
            repo.save_items(conn, [make_item(f"Новость {index}", f"https://e.com/{index}", created_at=iso(1))])

        assert repo.clear_items(conn, batch_size=5)["items"] == 12
        assert repo.count_items(conn) == 0
