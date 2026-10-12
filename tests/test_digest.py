"""Тесты дайджеста: отбор, HTML, разбивка на сообщения, саммари."""

from __future__ import annotations

import pytest

from collectors.normalize import build_item
from config.loader import AppConfig, DedupConfig, Label, Query, Source
from database import connect, init_schema, repo
from database.db import days_ago_iso, iso_now
from database.models import RawItem
from digest import builder

DEDUP = DedupConfig()

LABELS = (
    Label(id="news", title="Новости отрасли", emoji="📰", order=1),
    Label(id="vacancies", title="Вакансии", emoji="💼", order=2),
)
SOURCES = (
    Source(
        id="tass_news",
        name="ТАСС",
        label="news",
        type="rss",
        url="https://tass.ru/rss/v2.xml",
        fields={"title": "title", "url": "link"},
    ),
    Source(
        id="trudvsem_vacancies",
        name="Работа России",
        label="vacancies",
        type="api",
        url="https://opendata.trudvsem.ru/api/v1/vacancies",
        fields={"title": "title", "url": "url"},
    ),
)


def make_cfg(**overrides) -> AppConfig:
    data = dict(labels=LABELS, sources=SOURCES, queries=(), dedup=DEDUP, run_on_start=False)
    data.update(overrides)
    return AppConfig(**data)


def make_query(**overrides) -> Query:
    data = dict(
        id="daily_news",
        label="news",
        schedule="0 9 * * *",
        limit=10,
        lookback_days=1,
        tg_chat_id="123",
    )
    data.update(overrides)
    return Query(**data)


@pytest.fixture
def conn(workdir):
    connection = connect(workdir / "digest.db")
    init_schema(connection)
    yield connection
    connection.close()


def add_item(
    conn,
    title: str,
    *,
    label: str = "news",
    source: str = "tass_news",
    url: str | None = None,
    description: str = "",
    created_at: str | None = None,
    extra: dict | None = None,
):
    raw = RawItem(
        title=title,
        url=f"https://example.com/{abs(hash(title))}" if url is None else url,
        description=description,
        created_at=created_at or iso_now(),
        source=source,
        label=label,
        extra=extra or {},
    )
    item = build_item(raw, source, label, DEDUP)
    repo.save_items(conn, [item])
    return conn.execute("SELECT * FROM items WHERE dedup_hash = ?", (item.dedup_hash,)).fetchone()


class TestFormatSalary:
    def test_range(self):
        extra = {"salary_min": 250000, "salary_max": 300000, "salary_currency": "RUR"}
        assert builder.format_salary(extra) == "250 000–300 000 ₽"

    def test_from_and_to(self):
        assert builder.format_salary({"salary_min": 250000, "salary_currency": "RUR"}) == "от 250 000 ₽"
        assert builder.format_salary({"salary_max": 300000, "salary_currency": "USD"}) == "до 300 000 $"

    def test_unknown_currency_is_kept(self):
        assert builder.format_salary({"salary_min": 100, "salary_currency": "CHF"}) == "от 100 CHF"

    def test_equal_bounds_render_once(self):
        extra = {"salary_min": 200000, "salary_max": 200000, "salary_currency": "RUR"}
        assert builder.format_salary(extra) == "от 200 000 ₽"

    def test_empty(self):
        assert builder.format_salary({}) == ""
        assert builder.format_salary({"region": "Москва"}) == ""


class TestItemBlock:
    def test_title_is_cleaned_then_escaped(self, conn):
        row = add_item(conn, "<b>взлом</b> & Co", url="https://example.com/a?x=1&y=2")
        block = builder.item_block(1, row, make_cfg())

        # теги источника вычищены ещё при сохранении, а амперсанд экранирован для HTML
        assert block.startswith("1. <b>взлом &amp; Co</b>")
        assert "https://example.com/a?x=1&amp;y=2" in block
        assert "ТАСС" in block

    def test_missing_url(self, conn):
        add_item(conn, "Новость без ссылки", url="")
        row = conn.execute("SELECT * FROM items WHERE title = ?", ("Новость без ссылки",)).fetchone()
        assert "ссылки нет" in builder.item_block(1, row, make_cfg())

    def test_vacancy_shows_company_region_and_salary(self, conn):
        row = add_item(
            conn,
            "Python-разработчик",
            label="vacancies",
            source="trudvsem_vacancies",
            extra={"company": "ООО Ромашка", "region": "Москва", "salary_min": 250000, "salary_currency": "RUR"},
        )
        block = builder.item_block(1, row, make_cfg())

        assert "ООО Ромашка · Москва · от 250 000 ₽" in block

    def test_long_description_is_truncated(self, conn):
        row = add_item(conn, "Заголовок", description="слово " * 100)
        block = builder.item_block(1, row, make_cfg())

        assert "…" in block
        assert len(block) < 600


class TestRenderSection:
    def test_empty_rows_produce_nothing(self, conn):
        assert builder.render_section(LABELS[0], [], make_cfg()) == []

    def test_header_has_label_emoji_date_and_count(self, conn):
        add_item(conn, "Первая")
        rows = builder.select_last(conn, "news", limit=5)
        messages = builder.render_section(LABELS[0], rows, make_cfg(), tz_name="Europe/Moscow")

        assert len(messages) == 1
        assert messages[0].startswith("📰 Новости отрасли — ")
        assert "(1)" in messages[0]

    def test_summary_goes_into_first_message(self, conn):
        add_item(conn, "Первая")
        rows = builder.select_last(conn, "news", limit=5)
        messages = builder.render_section(LABELS[0], rows, make_cfg(), summary="Кратко: спрос растёт")

        assert "<i>Кратко: спрос растёт</i>" in messages[0]

    def test_splitting_keeps_blocks_and_limit(self, conn):
        for index in range(40):
            add_item(conn, f"Материал номер {index} " + "подробности " * 12)
        rows = builder.select_last(conn, "news", limit=100)
        messages = builder.render_section(LABELS[0], rows, make_cfg(), tz_name="Europe/Moscow")

        assert len(messages) > 1
        assert all(len(message) <= builder.TELEGRAM_LIMIT for message in messages)
        assert messages[0].startswith("📰 Новости отрасли — ")
        assert messages[1].startswith("📰 Новости отрасли — ")
        assert "(продолжение)" in messages[1]

        # ни один элемент не разрезан: открытых и закрытых тегов поровну в каждом сообщении
        for message in messages:
            assert message.count("<b>") == message.count("</b>")
        total = sum(message.count("<b>") for message in messages)
        assert total == 40


class TestSelectItems:
    def test_keywords_filter(self, conn):
        add_item(conn, "Python-разработчик")
        add_item(conn, "Менеджер по продажам")
        add_item(conn, "Senior Python engineer")
        query = make_query(keywords={"any": ["python"]})

        rows = builder.select_items(conn, make_cfg(), query)

        assert len(rows) == 2
        assert all("python" in row["title"].lower() for row in rows)

    def test_limit_applies_after_filtering(self, conn):
        add_item(conn, "Python-разработчик")
        add_item(conn, "Python Senior")
        for index in range(5):
            add_item(conn, f"Менеджер {index}")
        query = make_query(keywords={"any": ["python"]}, limit=1)

        rows = builder.select_items(conn, make_cfg(), query)

        assert len(rows) == 1

    def test_query_filters_apply_to_extra(self, conn):
        add_item(
            conn,
            "Дорогая вакансия",
            label="vacancies",
            source="trudvsem_vacancies",
            extra={"salary_min": 300000, "salary_currency": "RUR"},
        )
        add_item(
            conn,
            "Дешёвая вакансия",
            label="vacancies",
            source="trudvsem_vacancies",
            extra={"salary_min": 100000, "salary_currency": "RUR"},
        )
        add_item(conn, "Вакансия без зарплаты", label="vacancies", source="trudvsem_vacancies")
        query = make_query(
            id="daily_vacancies",
            label="vacancies",
            filters={"salary_min": 200000, "salary_currency": "RUR", "include_unspecified_salary": True},
        )

        rows = builder.select_items(conn, make_cfg(), query)
        titles = {row["title"] for row in rows}

        assert titles == {"Дорогая вакансия", "Вакансия без зарплаты"}

    def test_old_items_outside_lookback_are_skipped(self, conn):
        add_item(conn, "Свежая")
        add_item(conn, "Старая", created_at=days_ago_iso(10))
        query = make_query(lookback_days=1)

        rows = builder.select_items(conn, make_cfg(), query)

        assert [row["title"] for row in rows] == ["Свежая"]

    def test_sent_items_are_not_selected_again(self, conn):
        add_item(conn, "Первая")
        query = make_query()
        rows = builder.select_items(conn, make_cfg(), query)
        digest_id = repo.log_digest(conn, query.id, "123", len(rows))
        repo.attach_digest_items(conn, digest_id, [row["id"] for row in rows])

        assert builder.select_items(conn, make_cfg(), query) == []


class TestBuildDigest:
    async def test_digest_with_summary(self, conn):
        add_item(conn, "Python-разработчик")
        calls: list[int] = []

        async def summarizer(rows):
            calls.append(len(rows))
            return "Кратко: спрос на Python растёт"

        digest = await builder.build_digest(conn, make_cfg(), make_query(), summarizer=summarizer)

        assert calls == [1]
        assert digest.item_ids and len(digest.item_ids) == 1
        assert "Кратко: спрос на Python растёт" in digest.messages[0]
        assert not digest.is_empty()

    async def test_empty_digest(self, conn):
        digest = await builder.build_digest(conn, make_cfg(), make_query())

        assert digest.is_empty()
        assert digest.messages == []
        assert digest.sections == []

    async def test_summarizer_not_called_without_items(self, conn):
        async def summarizer(rows):  # pragma: no cover — не должен вызываться
            raise AssertionError("саммари не нужен для пустого дайджеста")

        digest = await builder.build_digest(conn, make_cfg(), make_query(), summarizer=summarizer)

        assert digest.is_empty()


class TestSummarizer:
    """deepseek-flash считает рассуждения в тот же бюджет, что и ответ."""

    @staticmethod
    def make_client(content: str | None, finish_reason: str = "stop", reasoning: str = ""):
        from types import SimpleNamespace

        message = SimpleNamespace(content=content, reasoning_content=reasoning)
        choice = SimpleNamespace(message=message, finish_reason=finish_reason)
        response = SimpleNamespace(choices=[choice], usage=SimpleNamespace(completion_tokens=42))

        class Completions:
            async def create(self, **kwargs):
                return response

        return SimpleNamespace(chat=SimpleNamespace(completions=Completions()))

    async def test_summary_is_returned_and_stripped(self):
        from digest.llm import DeepSeekSummarizer

        summarizer = DeepSeekSummarizer("key", client=self.make_client("  Кратко: спрос растёт  "))
        rows = [{"title": "Python-разработчик", "description": "FastAPI"}]

        assert await summarizer(rows) == "Кратко: спрос растёт"

    async def test_empty_content_returns_none(self):
        from digest.llm import DeepSeekSummarizer

        summarizer = DeepSeekSummarizer(
            "key", client=self.make_client("", finish_reason="length", reasoning="долго думал")
        )

        assert await summarizer([{"title": "Новость", "description": ""}]) is None

    async def test_prompt_limits_items_and_length(self):
        from digest.llm import MAX_ITEMS, TITLE_LIMIT, DeepSeekSummarizer

        summarizer = DeepSeekSummarizer("key", client=self.make_client("ок"))
        rows = [{"title": "т" * 400, "description": "о" * 400} for _ in range(MAX_ITEMS + 5)]

        prompt = summarizer.build_prompt(rows)
        lines = [line for line in prompt.splitlines() if line.startswith("— ")]

        assert len(lines) == MAX_ITEMS
        assert all(len(line) <= TITLE_LIMIT + 200 for line in lines)

    async def test_no_api_key_means_no_summarizer(self):
        from digest.llm import make_summarizer

        assert make_summarizer({}) is None
        assert make_summarizer({"DEEPSEEK_API_KEY": "sk-x"}) is not None

    async def test_model_is_taken_from_env(self):
        from digest.llm import make_summarizer

        summarizer = make_summarizer({"DEEPSEEK_API_KEY": "sk-x", "DEEPSEEK_MODEL": "deepseek-v4-pro"})

        assert summarizer is not None and summarizer.model == "deepseek-v4-pro"


class TestSelectLast:
    def test_returns_newest_and_ignores_sent_state(self, conn):
        add_item(conn, "Старая", created_at=days_ago_iso(3))
        add_item(conn, "Новая")
        newest = add_item(conn, "Самая новая")
        digest_id = repo.log_digest(conn, "daily_news", "123", 1)
        repo.attach_digest_items(conn, digest_id, [newest["id"]])

        rows = builder.select_last(conn, "news", limit=2)

        assert [row["title"] for row in rows] == ["Самая новая", "Новая"]
