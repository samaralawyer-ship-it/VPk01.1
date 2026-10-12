"""Тесты бота: allowlist, разбор аргументов, отправка, DRY_RUN, тексты команд."""

from __future__ import annotations

import httpx
import pytest

from bot.sender import SenderError, TelegramSender, split_text
from bot.telegram_bot import (
    BotContext,
    allowed_chat_ids,
    labels_text,
    parse_label_argument,
    sources_text,
    status_text,
    summarize_results,
)
from config.loader import AppConfig, DedupConfig, Label, Query, Source
from database import connect, init_schema, repo
from pipeline import QueryResult, SourceResult

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
        enabled=False,
    ),
)
QUERIES = (
    Query(id="daily_news", label="news", schedule="0 9 * * *", limit=10, lookback_days=1, tg_chat_id="123"),
    Query(id="daily_vacancies", label="vacancies", schedule="0 10 * * *", limit=5, lookback_days=3, tg_chat_id="123"),
)


def make_cfg() -> AppConfig:
    return AppConfig(labels=LABELS, sources=SOURCES, queries=QUERIES, dedup=DEDUP, run_on_start=False)


@pytest.fixture
def conn(workdir):
    connection = connect(workdir / "bot.db")
    init_schema(connection)
    yield connection
    connection.close()


@pytest.fixture
def context(conn):
    return BotContext(
        conn=conn,
        cfg=make_cfg(),
        sender=TelegramSender(dry_run=True, env={}),
        allowed_chat_ids={"123"},
    )


class TestAllowedChatIds:
    def test_single_and_list(self):
        env = {"TG_CHAT_ID": "111", "TG_ALLOWED_CHAT_IDS": "222, 333 ,"}
        assert allowed_chat_ids(env) == {"111", "222", "333"}

    def test_empty_env(self):
        assert allowed_chat_ids({}) == set()

    def test_duplicates_are_merged(self):
        env = {"TG_CHAT_ID": "111", "TG_ALLOWED_CHAT_IDS": "111,222"}
        assert allowed_chat_ids(env) == {"111", "222"}


class TestParseLabelArgument:
    def test_empty_means_all_labels(self, context):
        assert parse_label_argument(context, None) == (None, None)
        assert parse_label_argument(context, "   ") == (None, None)

    def test_known_label(self, context):
        assert parse_label_argument(context, "news") == ("news", None)
        assert parse_label_argument(context, " NEWS ") == ("news", None)
        assert parse_label_argument(context, "#vacancies") == ("vacancies", None)

    def test_unknown_label_returns_help(self, context):
        label, error = parse_label_argument(context, "погода")
        assert label is None
        assert "Неизвестная метка" in error
        assert "news" in error


class TestCommandTexts:
    def test_labels_text_lists_every_label(self, context):
        text = labels_text(context)
        assert "📰 Новости отрасли (news)" in text
        assert "💼 Вакансии (vacancies)" in text

    def test_status_text_contains_schedule_and_dry_run(self, context):
        repo.save_query(context.conn, QUERIES[0])
        text = status_text(context)

        assert "Записей в базе: 0" in text
        assert "daily_news" in text and "0 9 * * *" in text
        assert "DRY_RUN: True" in text

    def test_sources_text_marks_disabled(self, context):
        text = sources_text(context)
        assert "tass_news [вкл]" in text
        assert "trudvsem_vacancies [выкл]" in text

    def test_sources_text_shows_last_run(self, context):
        run_id = repo.log_run_start(context.conn, "tass_news")
        repo.log_run_finish(context.conn, run_id, "ok", items_new=5, items_dup=2, items_filtered=1)

        assert "new=5, dup=2, filtered=1" in sources_text(context)

    def test_status_text_reports_last_run(self, context):
        run_id = repo.log_run_start(context.conn, "tass_news")
        repo.log_run_finish(context.conn, run_id, "error", error="HTTP 500")

        text = status_text(context)
        assert "tass_news — error" in text
        assert "HTTP 500" in text


class TestSummarizeResults:
    def test_reports_counts(self, context):
        result = QueryResult(
            query_id="daily_news",
            label="news",
            status="sent",
            items_sent=7,
            collected=[SourceResult("tass_news", "news", "ok", fetched=10, new=7, dup=3)],
        )

        text = summarize_results([result], context)

        assert "разделов 1" in text
        assert "материалов 7" in text
        assert "new=7" in text and "dup=3" in text

    def test_empty_when_nothing_sent(self, context):
        result = QueryResult(query_id="daily_news", label="news", status="empty")

        text = summarize_results([result], context)

        # молчать нельзя: даже без отчёта называем раздел, а не только «нового нет»
        assert "Новости отрасли" in text
        assert "нового нет" in text

    def test_empty_report_is_forwarded(self, context):
        result = QueryResult(
            query_id="daily_news",
            label="news",
            status="empty",
            empty_report="📰 <b>Новости отрасли</b> — сегодня нового нет\nПричина: ключи",
        )

        text = summarize_results([result], context)

        assert "Причина: ключи" in text

    def test_mixed_result_mentions_empty_sections(self, context):
        sent = QueryResult(
            query_id="daily_news",
            label="news",
            status="sent",
            items_sent=2,
            collected=[SourceResult("tass_news", "news", "ok", fetched=5, new=2)],
        )
        empty = QueryResult(query_id="daily_vacancies", label="vacancies", status="empty")

        text = summarize_results([sent, empty], context)

        assert "разделов 1" in text
        assert "без нового: vacancies" in text

    def test_lists_broken_sources(self, context):
        result = QueryResult(
            query_id="daily_news",
            label="news",
            status="sent",
            items_sent=1,
            collected=[
                SourceResult("tass_news", "news", "ok", fetched=1, new=1),
                SourceResult("broken", "news", "error", error="timeout"),
            ],
        )

        assert "источники с ошибкой: broken" in summarize_results([result], context)


class TestSplitText:
    def test_short_text_stays_whole(self):
        assert split_text("привет") == ["привет"]

    def test_long_text_is_split_within_limit(self):
        text = "\n".join(f"строка номер {index} " + "x" * 60 for index in range(200))
        parts = split_text(text, limit=500)

        assert len(parts) > 1
        assert all(len(part) <= 500 for part in parts)

    def test_monster_line_is_cut(self):
        parts = split_text("y" * 1200, limit=500)
        assert [len(part) for part in parts] == [500, 500, 200]


class TestTelegramSender:
    async def test_dry_run_prints_instead_of_sending(self, capsys):
        sender = TelegramSender(dry_run=True, env={})

        parts = await sender.send("123", "<b>Привет</b>")

        assert parts == 1
        assert "DRY_RUN" in capsys.readouterr().out

    async def test_sends_html_message(self):
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"ok": True})

        sender = TelegramSender(
            token="123:TEST",
            env={},
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        assert await sender.send("555", "<b>Дайджест</b>") == 1

        request = captured[0]
        assert request.url.path.endswith("/bot123:TEST/sendMessage")
        body = request.content.decode("utf-8")
        assert '"chat_id":"555"' in body
        assert "parse_mode" in body and "HTML" in body

    async def test_retries_after_429(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 0}})
            return httpx.Response(200, json={"ok": True})

        sender = TelegramSender(
            token="t",
            env={},
            pause=0,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        assert await sender.send("1", "текст") == 1
        assert calls["n"] == 2

    async def test_bad_request_raises_with_description(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"ok": False, "description": "can't parse entities"})

        sender = TelegramSender(
            token="t",
            env={},
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        with pytest.raises(SenderError) as exc:
            await sender.send("1", "<b>сломанный")

        assert "can't parse entities" in str(exc.value)

    async def test_missing_token_raises(self):
        sender = TelegramSender(token="", env={})
        with pytest.raises(SenderError, match="TELEGRAM_BOT_TOKEN"):
            await sender.send("1", "текст")

    async def test_send_many_counts_messages(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True})

        sender = TelegramSender(
            token="t",
            env={},
            pause=0,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        assert await sender.send_many("1", ["раз", "два", "три"]) == 3

    def test_dry_run_flag_from_env(self):
        assert TelegramSender(token="t", env={"DRY_RUN": "true"}).dry_run is True
        assert TelegramSender(token="t", env={"DRY_RUN": "0"}).dry_run is False

    def test_proxy_is_taken_from_tg_proxy(self):
        sender = TelegramSender(token="t", env={"TG_PROXY": "http://127.0.0.1:3128"})
        assert sender.proxy == "http://127.0.0.1:3128"

    def test_proxy_falls_back_to_system_variable(self):
        sender = TelegramSender(token="t", env={"HTTPS_PROXY": "http://proxy.local:8080"})
        assert sender.proxy == "http://proxy.local:8080"

    def test_no_proxy_by_default(self):
        assert TelegramSender(token="t", env={}).proxy is None


class TestRunBotProxy:
    """Бот должен ходить через тот же прокси, что и отправка.

    Иначе в сети, где api.telegram.org доступен только через прокси, дайджесты
    уходят, а поллинг падает и роняет весь сервис.
    """

    @staticmethod
    def captured_session(monkeypatch, env):
        """Поймать сессию, которую run_bot создаёт для Bot, без реального поллинга."""
        import asyncio

        from aiogram import Dispatcher

        import bot.telegram_bot as module

        created: dict[str, object] = {}

        class FakeAiohttpSession:
            def __init__(self, **kwargs):
                created["proxy"] = kwargs.get("proxy")
                self.proxy = kwargs.get("proxy")

            async def close(self) -> None:
                created["closed"] = True

        async def fake_start_polling(self, bot, **kwargs):  # noqa: ANN001, ANN002, ANN003
            created["bot"] = bot

        monkeypatch.setattr(module, "AiohttpSession", FakeAiohttpSession)
        monkeypatch.setattr(Dispatcher, "start_polling", fake_start_polling)
        for name in ("TG_PROXY", "ALL_PROXY", "HTTPS_PROXY"):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)

        context = BotContext(conn=None, cfg=None, sender=None)
        asyncio.run(module.run_bot(context, "123:TEST"))
        return created

    def test_proxy_from_tg_proxy_reaches_bot_session(self, monkeypatch):
        created = self.captured_session(monkeypatch, {"TG_PROXY": "socks5://127.0.0.1:10808"})

        assert created["proxy"] == "socks5://127.0.0.1:10808"

    def test_without_proxy_connection_is_direct(self, monkeypatch):
        created = self.captured_session(monkeypatch, {})

        assert created["proxy"] is None
