"""Тесты интеграции перевода со сбором: переводятся только новые материалы.

Главное, что здесь проверяется: дедупликация считается по оригиналу и не ломается
переводом, а повторный прогон не тратит деньги на уже сохранённые материалы.
"""

from __future__ import annotations

import pytest

from collectors.base import Batch
from collectors.normalize import build_item
from config.loader import AppConfig, DedupConfig, Label, Query, Source
from database import connect, init_schema, repo
from database.models import RawItem
from pipeline import collect_source

DEDUP = DedupConfig()
LABELS = (Label(id="tech", title="Обновления технологий", emoji="🛠", order=3),)


def make_source(**overrides) -> Source:
    data = dict(
        id="hackernews",
        name="Hacker News",
        label="tech",
        type="api",
        url="https://hn.algolia.com/api/v1/search_by_date",
        fields={"title": "title", "url": "url", "description": "description"},
    )
    data.update(overrides)
    return Source(**data)


def make_cfg(source: Source | None = None) -> AppConfig:
    return AppConfig(
        labels=LABELS,
        sources=(source or make_source(),),
        queries=(Query(id="daily_tech", label="tech", schedule="0 10 * * *", tg_chat_id="1"),),
        dedup=DEDUP,
        run_on_start=False,
    )


class FakeTranslator:
    """Переводчик, который ничего не знает про сеть: «переводит» префиксом."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[list[str]] = []

    async def translate_many(self, texts, source_lang=None):
        self.calls.append(list(texts))
        if self.fail:
            return [None] * len(texts)
        return [f"RU:{text}" for text in texts]


class FakeCollector:
    """Коллектор, отдающий заранее заданные сырые материалы."""

    def __init__(self, raws: list[RawItem], filtered: int = 0) -> None:
        self._raws = raws
        self._filtered = filtered

    async def collect(self) -> Batch:
        return Batch(source_id="hackernews", raw_items=list(self._raws), fetched=len(self._raws), filtered=self._filtered)


@pytest.fixture
def conn(workdir):
    connection = connect(workdir / "translate.db")
    init_schema(connection)
    yield connection
    connection.close()


def patch_collector(monkeypatch, *collectors: FakeCollector) -> None:
    queue = list(collectors)

    def fake_get_collector(source, **kwargs):
        return queue.pop(0)

    monkeypatch.setattr("collectors.factory.get_collector", fake_get_collector)


def raw(title: str, url: str, description: str = "") -> RawItem:
    return RawItem(
        title=title,
        url=url,
        description=description,
        created_at="2026-10-12T10:00:00+00:00",
        source="hackernews",
        label="tech",
    )


class TestCollectWithTranslation:
    async def test_translates_new_items(self, conn, monkeypatch):
        patch_collector(monkeypatch, FakeCollector([raw("Hello world", "https://e.com/1", "Some text")]))
        translator = FakeTranslator()

        result = await collect_source(conn, make_cfg(), make_source(translation="eng"), translator=translator)

        assert result.new == 1
        row = conn.execute("SELECT title, description, extra FROM items").fetchone()
        assert row["title"] == "RU:Hello world"
        assert row["description"] == "RU:Some text"
        assert "Hello world" in row["extra"]                 # в extra остался оригинал
        assert "RU:Hello world" not in row["extra"]

    async def test_translation_is_skipped_without_field(self, conn, monkeypatch):
        patch_collector(monkeypatch, FakeCollector([raw("Hello world", "https://e.com/1")]))
        translator = FakeTranslator()

        await collect_source(conn, make_cfg(), make_source(), translator=translator)

        assert translator.calls == []                        # источник не помечен как иностранный
        assert conn.execute("SELECT title FROM items").fetchone()["title"] == "Hello world"

    async def test_translation_is_skipped_without_translator(self, conn, monkeypatch):
        patch_collector(monkeypatch, FakeCollector([raw("Hello world", "https://e.com/1")]))

        await collect_source(conn, make_cfg(), make_source(translation="eng"), translator=None)

        assert conn.execute("SELECT title FROM items").fetchone()["title"] == "Hello world"

    async def test_duplicates_are_not_translated_again(self, conn, monkeypatch):
        """Повторный прогон: материалы уже в базе — платить за перевод не за что."""
        patch_collector(
            monkeypatch,
            FakeCollector([raw("Hello world", "https://e.com/1")]),
            FakeCollector([raw("Hello world", "https://e.com/1")]),
        )
        translator = FakeTranslator()
        source = make_source(translation="eng")

        first = await collect_source(conn, make_cfg(source), source, translator=translator)
        calls_after_first = len(translator.calls)
        second = await collect_source(conn, make_cfg(source), source, translator=translator)

        assert first.new == 1 and second.new == 0 and second.dup == 1
        assert len(translator.calls) == calls_after_first     # новых вызовов нет

    async def test_only_new_items_sent_to_translator(self, conn, monkeypatch):
        patch_collector(
            monkeypatch,
            FakeCollector([raw("Old news", "https://e.com/old")]),
            FakeCollector([raw("Old news", "https://e.com/old"), raw("Fresh news", "https://e.com/new")]),
        )
        translator = FakeTranslator()
        source = make_source(translation="eng")

        await collect_source(conn, make_cfg(source), source, translator=translator)
        translator.calls.clear()
        await collect_source(conn, make_cfg(source), source, translator=translator)

        assert translator.calls == [["Fresh news"]]          # старый материал не переводился

    async def test_dedup_hash_is_computed_on_original(self, conn, monkeypatch):
        """Хеш не должен зависеть от перевода, иначе один материал сохранится дважды."""
        patch_collector(
            monkeypatch,
            FakeCollector([raw("Same title", "https://e.com/x")]),
            FakeCollector([raw("Same title", "https://e.com/x")]),
        )
        translator = FakeTranslator()
        source = make_source(translation="eng")

        await collect_source(conn, make_cfg(source), source, translator=translator)
        stored = conn.execute("SELECT dedup_hash FROM items").fetchone()["dedup_hash"]
        # хеш совпадает с тем, что даёт нормализация по нетронутому оригиналу
        assert stored == build_item(raw("Same title", "https://e.com/x"), "hackernews", "tech", DEDUP).dedup_hash

    async def test_failed_translation_keeps_item(self, conn, monkeypatch):
        patch_collector(monkeypatch, FakeCollector([raw("Hello world", "https://e.com/1", "Text")]))
        translator = FakeTranslator(fail=True)

        result = await collect_source(conn, make_cfg(), make_source(translation="eng"), translator=translator)

        assert result.new == 1                               # материал не потерян из-за сбоя
        row = conn.execute("SELECT title, extra FROM items").fetchone()
        assert row["title"] == "Hello world"
        assert "translation_failed" in row["extra"]

    async def test_original_survives_in_extra(self, conn, monkeypatch):
        patch_collector(monkeypatch, FakeCollector([raw("English title", "https://e.com/1", "English text")]))
        translator = FakeTranslator()

        await collect_source(conn, make_cfg(), make_source(translation="eng"), translator=translator)

        import json

        extra = json.loads(conn.execute("SELECT extra FROM items").fetchone()["extra"])
        assert extra["original_title"] == "English title"
        assert extra["original_description"] == "English text"
        assert extra["translated_from"] == "eng"

    async def test_empty_batch_makes_no_translation_calls(self, conn, monkeypatch):
        patch_collector(monkeypatch, FakeCollector([]))
        translator = FakeTranslator()

        await collect_source(conn, make_cfg(), make_source(translation="eng"), translator=translator)

        assert translator.calls == []
