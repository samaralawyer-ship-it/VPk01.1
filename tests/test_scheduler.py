"""Тесты планировщика: cron-триггеры, регистрация задач, пауза, прогон при старте."""

from __future__ import annotations

import pytest

from collectors.normalize import build_item
from config.loader import AppConfig, DedupConfig, Label, Query, Source
from database import connect, init_schema, repo
from database.db import iso_now
from database.models import RawItem
from scheduler import jobs as sched

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
QUERIES = (
    Query(id="daily_news", label="news", schedule="0 9 * * *", limit=10, lookback_days=1, tg_chat_id="555"),
    Query(id="daily_tech", label="news", schedule="30 18 * * 1-5", limit=5, lookback_days=2, tg_chat_id="555"),
)


def make_cfg() -> AppConfig:
    return AppConfig(labels=LABELS, sources=SOURCES, queries=QUERIES, dedup=DEDUP, run_on_start=True)


@pytest.fixture
def conn(workdir):
    connection = connect(workdir / "scheduler.db")
    init_schema(connection)
    for query in QUERIES:
        repo.save_query(connection, query)
    yield connection
    connection.close()


@pytest.fixture
def context(conn):
    # collect=False: планировщик тестируется без обращения к сети
    return sched.SchedulerContext(conn=conn, cfg=make_cfg(), tz_name="Europe/Moscow", collect=False)


@pytest.fixture
async def scheduler():
    instance = sched.make_scheduler("Europe/Moscow")
    instance.start()
    try:
        yield instance
    finally:
        instance.shutdown(wait=False)


def add_item(conn, title: str):
    raw = RawItem(
        title=title,
        url=f"https://example.com/{abs(hash(title))}",
        created_at=iso_now(),
        source="tass_news",
        label="news",
    )
    repo.save_items(conn, [build_item(raw, "tass_news", "news", DEDUP)])


class TestTriggers:
    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("0 9 * * *", ["hour='9'", "minute='0'"]),
            ("30 18 * * 1-5", ["hour='18'", "minute='30'", "day_of_week='1-5'"]),
            ("*/15 * * * *", ["minute='*/15'"]),
        ],
    )
    def test_cron_is_parsed_into_trigger(self, expr, expected):
        query = Query(id="q", label="news", schedule=expr, tg_chat_id="1")
        trigger = sched.build_trigger(query, "Europe/Moscow")

        text = str(trigger)
        for fragment in expected:
            assert fragment in text, text
        assert str(trigger.timezone) == "Europe/Moscow"

    def test_default_timezone_is_moscow(self):
        assert str(sched.make_scheduler().timezone) == "Europe/Moscow"

    def test_job_id_is_prefixed(self):
        assert sched.job_id("daily_news") == "query:daily_news"


class TestRegisterJobs:
    def test_both_active_queries_are_registered(self, scheduler, context):
        assert sched.register_jobs(scheduler, context) == 2
        assert scheduler.get_job("query:daily_news") is not None
        assert scheduler.get_job("query:daily_tech") is not None

    def test_disabled_query_is_skipped(self, scheduler, context):
        repo.set_query_active(context.conn, "daily_tech", False)

        assert sched.register_jobs(scheduler, context) == 1
        assert scheduler.get_job("query:daily_news") is not None
        assert scheduler.get_job("query:daily_tech") is None

    def test_job_options_prevent_duplicate_runs(self, scheduler, context):
        sched.register_jobs(scheduler, context)
        job = scheduler.get_job("query:daily_news")

        assert job.max_instances == 1
        assert job.coalesce is True
        assert job.misfire_grace_time == sched.MISFIRE_GRACE_TIME
        assert job.next_run_time is not None

    def test_register_is_idempotent(self, scheduler, context):
        sched.register_jobs(scheduler, context)
        assert sched.register_jobs(scheduler, context) == 2
        assert len(scheduler.get_jobs()) == 2      # replace_existing, а не дубли


class TestRunOnStart:
    async def test_runs_only_active_queries(self, context):
        add_item(context.conn, "Новость для старта")
        repo.set_query_active(context.conn, "daily_tech", False)

        results = await sched.run_on_start(context)

        assert [result.query_id for result in results] == ["daily_news"]
        assert results[0].status == "preview"      # без отправителя — предпросмотр, не расходует

    async def test_query_job_returns_result(self, context):
        add_item(context.conn, "Ещё новость")

        result = await sched.run_query_job(context, QUERIES[0])

        assert result.query_id == "daily_news"
        assert result.status in ("preview", "sent", "empty")


class TestPauseResume:
    def test_pause_disables_queries_and_pauses_jobs(self, scheduler, context):
        sched.register_jobs(scheduler, context)

        assert sched.pause_all(scheduler, context) == 2

        assert scheduler.get_job("query:daily_news").next_run_time is None      # задача на паузе
        assert all(row["is_active"] == 0 for row in repo.list_queries(context.conn))

    def test_resume_enables_queries_and_restores_jobs(self, scheduler, context):
        sched.pause_all(scheduler, context)
        scheduler.remove_all_jobs()

        assert sched.resume_all(scheduler, context) == 2

        assert all(row["is_active"] == 1 for row in repo.list_queries(context.conn))
        assert scheduler.get_job("query:daily_news").next_run_time is not None

    def test_next_runs_lists_schedule(self, scheduler, context):
        sched.register_jobs(scheduler, context)

        runs = sched.next_runs(scheduler, limit=5)

        assert len(runs) == 2
        assert {name for name, _ in runs} == {"daily_news", "daily_tech"}
        assert all(when != "—" for _, when in runs)


class TestQueryStoreProtocol:
    def test_bot_context_satisfies_protocol(self, context):
        from bot.telegram_bot import BotContext

        bot_context = BotContext(conn=context.conn, cfg=context.cfg, sender=None)
        assert isinstance(bot_context, sched.QueryStore)
        assert isinstance(context, sched.QueryStore)


class TestResumeFromBot:
    """Регрессия: /pause и /resume работают с BotContext, а не с SchedulerContext.

    У них разные имена таймзоны (timezone против tz_name), из-за чего /resume
    падал с AttributeError и автозапуск после /pause было не вернуть.
    """

    def test_bot_context_exposes_tz_name(self, context):
        from bot.telegram_bot import BotContext

        bot_context = BotContext(conn=context.conn, cfg=context.cfg, timezone="Europe/Moscow")

        assert bot_context.tz_name == "Europe/Moscow"

    def test_resolve_timezone_understands_both_names(self, context):
        from bot.telegram_bot import BotContext

        bot_context = BotContext(conn=context.conn, cfg=context.cfg, timezone="Asia/Yekaterinburg")

        assert sched.resolve_timezone(context) == "Europe/Moscow"
        assert sched.resolve_timezone(bot_context) == "Asia/Yekaterinburg"
        assert sched.resolve_timezone(None) == sched.DEFAULT_TIMEZONE

    def test_resume_after_pause_from_bot_context(self, scheduler, context):
        from bot.telegram_bot import BotContext

        bot_context = BotContext(
            conn=context.conn,
            cfg=context.cfg,
            sender=None,
            timezone="Europe/Moscow",
            scheduler=scheduler,
        )

        assert sched.pause_all(scheduler, bot_context) == 2
        assert all(row["is_active"] == 0 for row in repo.list_queries(context.conn))

        assert sched.resume_all(scheduler, bot_context) == 2

        assert all(row["is_active"] == 1 for row in repo.list_queries(context.conn))
        assert scheduler.get_job("query:daily_news").next_run_time is not None
        assert str(scheduler.get_job("query:daily_news").trigger.timezone) == "Europe/Moscow"
