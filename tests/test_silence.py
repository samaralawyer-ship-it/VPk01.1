"""Тесты принципа «может не находить, но не может молчать».

Пустой раздел обязан прийти сообщением с объяснением причины: это ровно тот случай,
когда 65 собранных вакансий молча резались ключом «промпт».
"""

from __future__ import annotations

import re

import pytest

from config.loader import AppConfig, DedupConfig, Label, Query, Source
from database import connect, init_schema, repo
from database.db import days_ago_iso, iso_now
from digest.builder import Digest
from pipeline import (
    CollectionStats,
    SourceResult,
    build_empty_report,
    describe_filters,
    describe_keywords,
    lookback_hint,
    report_stamp,
    run_query,
)

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
        extra_fields={"salary_min": "vacancy.salary_min", "region": "vacancy.region.name"},
        item_filters={"salary_min": 200000, "salary_currency": "RUR"},
    ),
)

VACANCY_QUERY = Query(
    id="daily_vacancies",
    label="vacancies",
    schedule="0 10 * * *",
    sources=("trudvsem_vacancies",),
    keywords={"any": ["промпт"], "exclude": ["1С", "стажёр"]},
    filters={"salary_min": 100000, "include_unspecified_salary": True},
    lookback_days=3,
    tg_chat_id="555",
)


@pytest.fixture
def conn(workdir):
    connection = connect(workdir / "silence.db")
    init_schema(connection)
    yield connection
    connection.close()


def make_cfg(query: Query = VACANCY_QUERY) -> AppConfig:
    return AppConfig(labels=LABELS, sources=SOURCES, queries=(query,), dedup=DEDUP, run_on_start=False)


class RecordingSender:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[tuple[str, str]] = []

    async def send(self, chat_id, text) -> int:
        if self.fail:
            raise RuntimeError("Telegram недоступен")
        self.messages.append((str(chat_id), text))
        return 1

    async def send_many(self, chat_id, messages, pause=None) -> int:
        self.messages.extend((str(chat_id), text) for text in messages)
        return len(messages)


def failing_empty_digest(monkeypatch) -> None:
    """Пустой дайджест без обращения к БД и сети."""
    async def empty_digest(*args, **kwargs) -> Digest:
        return Digest(query_id="daily_vacancies", chat_id="555", label=LABELS[1])

    monkeypatch.setattr("pipeline.builder.build_digest", empty_digest)


class TestStats:
    def test_aggregates_results(self):
        stats = CollectionStats.from_results([
            SourceResult("trudvsem_vacancies", "vacancies", "ok", fetched=50, new=0, dup=10,
                         filtered=40, candidates=10),
            SourceResult("habr_career", "vacancies", "ok", fetched=50, new=0, dup=50, candidates=50),
        ])

        assert stats.candidates == 60
        assert stats.hard_filtered == 40
        assert stats.duplicates == 60
        assert stats.new == 0
        assert stats.ok is True
        assert stats.failed_sources == []

    def test_lists_failed_sources(self):
        stats = CollectionStats.from_results([
            SourceResult("tass_news", "news", "error", error="ConnectError"),
            SourceResult("habr_career", "vacancies", "ok", fetched=50),
        ])

        assert stats.failed_sources == ["tass_news"]
        assert stats.ok is False


class TestReportTexts:
    def test_keyword_reason_is_named(self, conn):
        """Главный сценарий: материалы есть, но ключи их не пропустили."""
        stats = CollectionStats.from_results([
            SourceResult("trudvsem_vacancies", "vacancies", "ok", fetched=50, new=0, dup=10,
                         filtered=40, candidates=10),
        ])

        report = build_empty_report(conn, make_cfg(), VACANCY_QUERY, stats)

        assert report.startswith("<b>💼 Вакансии</b> — сегодня нового нет")   # эмодзи один раз
        assert report.count("💼") == 1
        assert "промпт" in report                                  # ключ назван
        assert "Ни один из 0 новых материалов не совпал с ключами" not in report  # new=0 — другая ветка
        assert "фильтры источника" in report
        assert "Работа России" in report
        assert "в 1 источнике" in report                            # без «1 источниках»
        assert "Окно проверки: 3 дн." in report

    def test_sources_phrase_plural(self):
        from pipeline import sources_phrase

        assert sources_phrase(1) == "в 1 источнике"
        assert sources_phrase(2) == "в 2 источниках"
        assert sources_phrase(5) == "в 5 источниках"
        assert sources_phrase(11) == "в 11 источниках"
        assert sources_phrase(21) == "в 21 источнике"
        assert sources_phrase(0) == "в 0 источниках"

    def test_keywords_are_listed_when_new_items_exist(self, conn):
        stats = CollectionStats.from_results([
            SourceResult("trudvsem_vacancies", "vacancies", "ok", fetched=50, new=7, dup=3, candidates=10),
        ])

        report = build_empty_report(conn, make_cfg(), VACANCY_QUERY, stats)

        assert "Ключевые слова раздела: промпт" in report
        assert "Ни один из 7 новых материалов не совпал с ключами" in report

    def test_already_sent_is_explained(self, conn):
        stats = CollectionStats.from_results([
            SourceResult("trudvsem_vacancies", "vacancies", "ok", fetched=50, new=0, dup=50, candidates=50),
        ])

        report = build_empty_report(conn, make_cfg(), VACANCY_QUERY, stats)

        assert "уже были в базе" in report

    def test_failed_sources_are_reported(self, conn):
        stats = CollectionStats.from_results([
            SourceResult("trudvsem_vacancies", "vacancies", "error", error="ConnectError: timeout"),
        ])

        report = build_empty_report(conn, make_cfg(), VACANCY_QUERY, stats)

        assert "Источники с ошибкой: trudvsem_vacancies" in report
        assert "Часть данных могла не дойти" in report

    def test_empty_sources_are_reported(self, conn):
        stats = CollectionStats.from_results([
            SourceResult("trudvsem_vacancies", "vacancies", "ok", fetched=0, candidates=0),
        ])

        report = build_empty_report(conn, make_cfg(), VACANCY_QUERY, stats)

        assert "не вернули ни одного материала" in report

    def test_lookback_hint_when_older_items_exist(self, conn):
        from collectors.normalize import build_item
        from database.models import RawItem

        raw = RawItem(title="Старая вакансия", url="https://example.com/old",
                      created_at=days_ago_iso(10), source="trudvsem_vacancies", label="vacancies")
        repo.save_items(conn, [build_item(raw, "trudvsem_vacancies", "vacancies", DEDUP)])
        stats = CollectionStats.from_results([
            SourceResult("trudvsem_vacancies", "vacancies", "ok", fetched=0),
        ])

        report = build_empty_report(conn, make_cfg(), VACANCY_QUERY, stats)

        assert "Глубже окна" in report
        assert "1" in report

    def test_no_hint_when_base_is_empty(self, conn):
        stats = CollectionStats.from_results([])

        assert lookback_hint(conn, make_cfg(), VACANCY_QUERY) is None
        report = build_empty_report(conn, make_cfg(), VACANCY_QUERY, stats)
        assert "Глубже окна" not in report

    def test_report_html_is_escaped(self, conn):
        """Отчёт уходит с parse_mode=HTML: посторонних тегов быть не должно."""
        stats = CollectionStats.from_results([
            SourceResult("trudvsem_vacancies", "vacancies", "error", error="<script>alert(1)</script>"),
        ])

        report = build_empty_report(conn, make_cfg(), VACANCY_QUERY, stats)

        tags = re.findall(r"</?([a-zA-Z][a-zA-Z0-9]*)[^>]*>", report)
        assert set(tags) <= {"b", "i", "code", "a"}

    def test_describe_filters_is_human_readable(self):
        assert describe_filters({"salary_min": 1, "salary_currency": "RUR"}) == "зарплата, валюта"
        assert describe_filters({"region": "Москва"}) == "регион"
        assert describe_filters({}) == ""


class TestReportStamp:
    """Регрессия: без времени и повода два отчёта из разных часов выглядят дублем."""

    @staticmethod
    def stats():
        return CollectionStats.from_results([
            SourceResult("trudvsem_vacancies", "vacancies", "ok", fetched=50, new=0,
                         dup=10, filtered=40, candidates=10),
        ])

    def test_stamp_has_time_and_trigger(self, conn):
        report = build_empty_report(
            conn, make_cfg(), VACANCY_QUERY, self.stats(),
            trigger="по команде /digest vacancies", tz_name="Europe/Moscow",
        )

        assert "🕒" in report
        assert "по команде /digest vacancies" in report
        # год и время на месте (формат ДД.ММ.ГГГГ ЧЧ:ММ)
        assert re.search(r"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}", report)

    def test_stamp_reports_schedule_and_start(self, conn):
        for trigger in ("по расписанию", "при старте сервиса"):
            report = build_empty_report(
                conn, make_cfg(), VACANCY_QUERY, self.stats(), trigger=trigger, tz_name="Europe/Moscow"
            )
            assert trigger in report

    def test_stamp_without_trigger_still_has_time(self, conn):
        report = build_empty_report(conn, make_cfg(), VACANCY_QUERY, self.stats(), tz_name="Europe/Moscow")

        assert "🕒" in report
        assert "None" not in report

    def test_stamp_uses_project_timezone(self, conn):
        """Москва и Калининград должны дать разное время (часовой пояс учитывается)."""
        moscow = report_stamp("повод", "Europe/Moscow")
        kaliningrad = report_stamp("повод", "Europe/Kaliningrad")

        assert moscow != kaliningrad

    def test_unknown_timezone_does_not_break_report(self, conn):
        report = build_empty_report(
            conn, make_cfg(), VACANCY_QUERY, self.stats(), trigger="повод", tz_name="Nowhere/Unknown"
        )

        assert "🕒" in report                       # время местное, но отчёт цел

    def test_two_reports_from_different_times_differ(self, conn, monkeypatch):
        """Тот самый случай: одинаковые цифры, разное время — тексты должны отличаться."""
        import pipeline as pipeline_module

        first = build_empty_report(conn, make_cfg(), VACANCY_QUERY, self.stats(),
                                   trigger="по расписанию", tz_name="Europe/Moscow")

        class FakeDatetime:
            @classmethod
            def now(cls, tz=None):
                from datetime import datetime as real, timedelta, timezone as tzinfo

                return real.now(tz) + timedelta(minutes=5) if tz else real.now() + timedelta(minutes=5)

        monkeypatch.setattr(pipeline_module, "datetime", FakeDatetime)
        second = build_empty_report(conn, make_cfg(), VACANCY_QUERY, self.stats(),
                                    trigger="по расписанию", tz_name="Europe/Moscow")

        assert first != second

    def test_describe_keywords_truncates(self):
        assert describe_keywords({"any": ["промпт"]}) == "промпт"
        assert describe_keywords({"any": ["a", "b", "c", "d", "e"]}) == "a, b, c и ещё 2"
        assert describe_keywords({"any": [["машинн", "обучени"]]}) == "машинн + обучени"
        assert describe_keywords({}) == ""


def canned_results(*results: SourceResult):
    """Подменить сбор фиксированными результатами: тесты не должны ходить в сеть."""
    async def fake_collect_sources(conn, cfg, sources, **kwargs):
        return list(results)

    return fake_collect_sources


class TestRunQueryReportsEmptiness:
    async def test_alert_is_sent_when_nothing_matched(self, conn, monkeypatch):
        failing_empty_digest(monkeypatch)
        monkeypatch.setattr("pipeline.collect_sources", canned_results(
            SourceResult("trudvsem_vacancies", "vacancies", "ok", fetched=50, new=0,
                         dup=10, filtered=40, candidates=10),
        ))
        sender = RecordingSender()

        result = await run_query(conn, make_cfg(), VACANCY_QUERY, sender=sender, notify_empty=True)

        assert result.status == "empty"
        assert len(sender.messages) == 1                    # молчания нет
        assert result.empty_report and result.empty_report == sender.messages[0][1]
        assert "промпт" in sender.messages[0][1]
        assert sender.messages[0][0] == "555"

    async def test_no_alert_when_disabled(self, conn, monkeypatch):
        failing_empty_digest(monkeypatch)
        monkeypatch.setattr("pipeline.collect_sources", canned_results())
        sender = RecordingSender()
        query = Query(**{**VACANCY_QUERY.__dict__, "notify_empty": False})

        result = await run_query(conn, make_cfg(query), query, sender=sender, notify_empty=query.notify_empty)

        assert result.status == "empty"
        assert sender.messages == []
        assert result.empty_report                            # отчёт всё равно собран (для лога и бота)

    async def test_alert_failure_does_not_break_run(self, conn, monkeypatch):
        failing_empty_digest(monkeypatch)
        monkeypatch.setattr("pipeline.collect_sources", canned_results())
        sender = RecordingSender(fail=True)

        result = await run_query(conn, make_cfg(), VACANCY_QUERY, sender=sender, notify_empty=True)

        assert result.status == "empty"                       # сбой доставки отчёта не роняет прогон

    async def test_preview_prints_instead_of_sending(self, conn, monkeypatch):
        failing_empty_digest(monkeypatch)
        monkeypatch.setattr("pipeline.collect_sources", canned_results())
        sender = RecordingSender()

        result = await run_query(conn, make_cfg(), VACANCY_QUERY, sender=sender, notify_empty=True, dry_run=True)

        assert result.status == "empty"
        assert sender.messages == []                          # предпросмотр не отправляет


class TestNotifyEmptyConfig:
    def test_defaults_to_true(self):
        assert Query(id="q", label="news", schedule="0 9 * * *").notify_empty is True

    def test_can_be_disabled(self):
        assert Query(id="q", label="news", schedule="0 9 * * *", notify_empty=False).notify_empty is False
