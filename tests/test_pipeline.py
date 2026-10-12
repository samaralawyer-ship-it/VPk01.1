"""Тесты полного прогона запроса: предпросмотр, доставка, сбой, блокировка.

Здесь проверяются критерии приёмки №3 (повторов нет) и №5 (дайджест + записи в БД).
"""

from __future__ import annotations

import asyncio

import pytest

from collectors.normalize import build_item
from config.loader import AppConfig, DedupConfig, Label, Query, Source
from database import connect, init_schema, repo
from database.db import iso_now
from database.models import RawItem
from digest import builder
from pipeline import run_query

DEDUP = DedupConfig()
LABELS = (Label(id="news", title="Новости отрасли", emoji="📰", order=1),)
SOURCES = (
    Source(
        id="tass_news",
        name="ТАСС",
        label="news",
        type="rss",
        url="https://tass.ru/rss/v2.xml",
        fields={"title": "title", "url": "link"},
    ),
)


class FakeSender:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[str, str]] = []

    async def send_many(self, chat_id, messages, pause=None) -> int:
        if self.fail:
            raise RuntimeError("Telegram недоступен")
        self.sent.extend((str(chat_id), message) for message in messages)
        return len(messages)

    async def send(self, chat_id, text) -> int:
        if self.fail:
            raise RuntimeError("Telegram недоступен")
        self.sent.append((str(chat_id), text))
        return 1


def make_query(**overrides) -> Query:
    data = dict(
        id="daily_news",
        label="news",
        schedule="0 9 * * *",
        limit=10,
        lookback_days=1,
        tg_chat_id="555",
    )
    data.update(overrides)
    return Query(**data)


def make_cfg(query: Query | None = None) -> AppConfig:
    return AppConfig(
        labels=LABELS,
        sources=SOURCES,
        queries=(query or make_query(),),
        dedup=DEDUP,
        run_on_start=False,
    )


@pytest.fixture
def conn(workdir):
    connection = connect(workdir / "pipeline.db")
    init_schema(connection)
    yield connection
    connection.close()


def add_item(conn, title: str):
    raw = RawItem(
        title=title,
        url=f"https://example.com/{abs(hash(title))}",
        created_at=iso_now(),
        source="tass_news",
        label="news",
    )
    repo.save_items(conn, [build_item(raw, "tass_news", "news", DEDUP)])


async def test_successful_send_consumes_items(conn):
    add_item(conn, "Первая новость")
    add_item(conn, "Вторая новость")
    sender = FakeSender()

    result = await run_query(conn, make_cfg(), make_query(), sender=sender, collect=False)

    assert result.status == "sent"
    assert result.items_sent == 2
    assert len(sender.sent) == 1                      # один раздел — одно сообщение
    assert repo.count_items(conn) == 2
    assert builder.select_items(conn, make_cfg(), make_query()) == []      # расходованы
    assert conn.execute("SELECT last_run FROM queries WHERE query_id = 'daily_news'").fetchone() is None

    digest_row = conn.execute("SELECT * FROM digest_log ORDER BY id DESC LIMIT 1").fetchone()
    assert digest_row["status"] == "sent" and digest_row["items_count"] == 2
    assert conn.execute("SELECT COUNT(*) AS n FROM digest_items").fetchone()["n"] == 2


async def test_failed_send_keeps_items_for_next_run(conn):
    add_item(conn, "Важная новость")
    sender = FakeSender(fail=True)

    result = await run_query(conn, make_cfg(), make_query(), sender=sender, collect=False)

    assert result.status == "failed"
    assert "Telegram недоступен" in result.error
    assert conn.execute("SELECT status FROM digest_log ORDER BY id DESC LIMIT 1").fetchone()["status"] == "failed"
    assert conn.execute("SELECT COUNT(*) AS n FROM digest_items").fetchone()["n"] == 0
    # главное: материал не потерян и попадёт в следующий дайджест
    assert len(builder.select_items(conn, make_cfg(), make_query())) == 1


async def test_preview_does_not_consume_items(conn):
    add_item(conn, "Новость для предпросмотра")

    result = await run_query(conn, make_cfg(), make_query(), collect=False, dry_run=True)

    assert result.status == "preview"
    assert result.messages
    assert conn.execute("SELECT COUNT(*) AS n FROM digest_items").fetchone()["n"] == 0
    assert len(builder.select_items(conn, make_cfg(), make_query())) == 1
    assert conn.execute("SELECT status FROM digest_log ORDER BY id DESC LIMIT 1").fetchone()["status"] == "dry_run"


async def test_empty_digest_reports_empty(conn):
    result = await run_query(conn, make_cfg(), make_query(), sender=FakeSender(), collect=False)

    assert result.status == "empty"
    assert result.items_sent == 0


async def test_second_run_after_send_is_empty(conn):
    add_item(conn, "Единственная новость")
    cfg = make_cfg()

    first = await run_query(conn, cfg, cfg.queries[0], sender=FakeSender(), collect=False)
    second = await run_query(conn, cfg, cfg.queries[0], sender=FakeSender(), collect=False)

    assert first.status == "sent"
    assert second.status == "empty"


async def test_busy_when_collection_already_running(conn):
    add_item(conn, "Новость")
    lock = asyncio.Lock()
    await lock.acquire()

    result = await run_query(conn, make_cfg(), make_query(), sender=FakeSender(), collect=False, lock=lock)

    assert result.status == "busy"
    assert "уже идёт" in result.error
    lock.release()
