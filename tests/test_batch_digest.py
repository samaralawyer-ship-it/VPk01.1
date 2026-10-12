"""Тесты общей сборки дайджеста: все разделы одного прогона — одним сообщением.

Причина: четыре отдельных сообщения подряд Telegram склеивает в одно уведомление,
и пользователь видит только первый раздел («новости»), решая, что остальных нет.
"""

from __future__ import annotations

import re

import pytest

from collectors.normalize import build_item
from config.loader import AppConfig, DedupConfig, Label, Query, Source
from database import connect, init_schema, repo
from database.db import iso_now
from database.models import RawItem
from digest.builder import Digest, Section, render_section
from pipeline import (
    batch_summary,
    build_batch_messages,
    cut_is_safe,
    deliver_sections,
    fits_limit,
    split_rendered_message,
)

DEDUP = DedupConfig()
LABELS = (
    Label(id="news", title="Новости отрасли", emoji="📰", order=1),
    Label(id="tech", title="Обновления технологий", emoji="🛠", order=3),
    Label(id="competitors", title="Публикации конкурентов", emoji="🏢", order=4),
)
SOURCES = (
    Source(id="tass_news", name="ТАСС", label="news", type="rss",
           url="https://tass.ru/rss/v2.xml", fields={"title": "title", "url": "link"}),
    Source(id="habr", name="Хабр", label="tech", type="rss",
           url="https://habr.com/ru/rss/all/all/?fl=ru", fields={"title": "title", "url": "link"}),
    Source(id="vcru", name="VC.ru", label="competitors", type="rss",
           url="https://vc.ru/rss/all", fields={"title": "title", "url": "link"}),
)
QUERIES = (
    Query(id="daily_news", label="news", schedule="0 9 * * *", tg_chat_id="555"),
    Query(id="daily_tech", label="tech", schedule="0 10 * * *", tg_chat_id="555"),
    Query(id="weekly_competitors", label="competitors", schedule="0 11 * * 1", tg_chat_id="555"),
)


@pytest.fixture
def conn(workdir):
    connection = connect(workdir / "batch.db")
    init_schema(connection)
    yield connection
    connection.close()


def make_cfg() -> AppConfig:
    return AppConfig(labels=LABELS, sources=SOURCES, queries=QUERIES, dedup=DEDUP, run_on_start=False)


def add_items(conn, label: str, source: str, count: int, title: str = "Материал") -> list[int]:
    ids = []
    for index in range(count):
        raw = RawItem(
            title=f"{title} {label} №{index}",
            url=f"https://example.com/{label}/{index}",
            created_at=iso_now(),
            source=source,
            label=label,
        )
        repo.save_items(conn, [build_item(raw, source, label, DEDUP)])
    for row in conn.execute("SELECT id FROM items WHERE label = ? ORDER BY id", (label,)):
        ids.append(int(row["id"]))
    return ids


def make_groups(conn, cfg: AppConfig, items_by_label: dict[str, int]):
    """Собрать группы (query, sections, item_ids) как в run_query, но без сети."""
    groups = []
    for query in cfg.queries:
        count = items_by_label.get(query.label, 0)
        if not count:
            continue
        label = cfg.label(query.label)
        rows = conn.execute(
            "SELECT * FROM items WHERE label = ? ORDER BY id LIMIT ?", (query.label, count)
        ).fetchall()
        messages = render_section(label, rows, cfg, tz_name="Europe/Moscow")
        section = Section(
            label=label,
            messages=messages,
            item_ids=[int(row["id"]) for row in rows],
            rows=list(rows),
        )
        groups.append((query, [section], [int(row["id"]) for row in rows]))
    return groups


class RecordingSender:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.batches: list[list[str]] = []

    async def send_many(self, chat_id, messages, pause=None) -> int:
        if self.fail:
            raise RuntimeError("Telegram недоступен")
        self.batches.append(list(messages))
        return len(messages)

    async def send(self, chat_id, text) -> int:
        if self.fail:
            raise RuntimeError("Telegram недоступен")
        self.batches.append([text])
        return 1


class TestSplitRenderedMessage:
    def test_splits_header_and_items(self, conn):
        rows = conn.execute("SELECT * FROM items").fetchall()
        cfg = make_cfg()
        raw = RawItem(title="Заголовок", url="https://e.com/x", created_at=iso_now(),
                      source="tass_news", label="news")
        repo.save_items(conn, [build_item(raw, "tass_news", "news", DEDUP)])
        rows = conn.execute("SELECT * FROM items").fetchall()

        parts = split_rendered_message(render_section(cfg.label("news"), rows, cfg, tz_name="Europe/Moscow")[0])

        assert len(parts) == 2                     # заголовок + один материал
        assert parts[0].startswith("📰 Новости отрасли")
        assert parts[1].startswith("1. ")          # нумерация сохранена, без «1. 1.»

    def test_item_with_multiline_description_stays_whole(self, conn):
        cfg = make_cfg()
        raw = RawItem(
            title="Новость с описанием",
            url="https://e.com/desc",
            description="Первая строка.\n\nВторая строка после пустой строки.",
            created_at=iso_now(),
            source="tass_news",
            label="news",
        )
        repo.save_items(conn, [build_item(raw, "tass_news", "news", DEDUP)])
        rows = conn.execute("SELECT * FROM items").fetchall()

        parts = split_rendered_message(render_section(cfg.label("news"), rows, cfg, tz_name="Europe/Moscow")[0])

        assert len(parts) == 2
        assert "Вторая строка" in parts[1]         # описание не оторвалось от материала


class TestBuildBatchMessages:
    def test_all_sections_go_into_one_message(self, conn):
        cfg = make_cfg()
        for label, source in (("news", "tass_news"), ("tech", "habr"), ("competitors", "vcru")):
            add_items(conn, label, source, 2)

        messages = build_batch_messages(cfg, make_groups(conn, cfg, {"news": 2, "tech": 2, "competitors": 2}))

        assert len(messages) == 1
        text = messages[0]
        assert "Дайджест за" in text
        for header in ("📰 Новости отрасли", "🛠 Обновления технологий", "🏢 Публикации конкурентов"):
            assert header in text
        # материал из каждого раздела на месте
        assert "Материал news №0" in text and "Материал tech №0" in text and "Материал competitors №0" in text

    def test_section_header_carries_item_count(self, conn):
        cfg = make_cfg()
        add_items(conn, "tech", "habr", 3)

        text = build_batch_messages(cfg, make_groups(conn, cfg, {"tech": 3}))[0]

        assert "<b>🛠 Обновления технологий</b> — 3" in text

    def test_section_headers_are_not_duplicated(self, conn):
        cfg = make_cfg()
        add_items(conn, "news", "tass_news", 1)

        text = build_batch_messages(cfg, make_groups(conn, cfg, {"news": 1}))[0]

        assert text.count("📰 Новости отрасли") == 1     # остался только общий заголовок раздела
        assert text.count("1. ") == 1                    # нумерация не задвоена

    def test_only_sections_with_items_are_listed(self, conn):
        cfg = make_cfg()
        add_items(conn, "news", "tass_news", 1)

        text = build_batch_messages(cfg, make_groups(conn, cfg, {"news": 1}))[0]

        assert "Обновления технологий" not in text       # пустой раздел сюда не попадает

    def test_long_digest_splits_within_telegram_limit(self, conn):
        cfg = make_cfg()
        add_items(conn, "news", "tass_news", 60)         # заведомо больше 4096 символов

        messages = build_batch_messages(cfg, make_groups(conn, cfg, {"news": 60}))

        assert len(messages) > 1
        assert all(len(message) <= 4096 for message in messages)
        # все материалы на месте, ничего не потеряно при разбивке
        joined = "\n".join(messages)
        assert all(f"Материал news №{index}" in joined for index in range(60))

    def test_split_does_not_break_html_entity(self, conn):
        cfg = make_cfg()
        add_items(conn, "news", "tass_news", 40, title="Новость &amp; тег")

        messages = build_batch_messages(cfg, make_groups(conn, cfg, {"news": 40}))

        for message in messages:
            assert not re.search(r"&[a-z]*$", message)   # не обрываем сущность на границе


class TestRealWorldLayout:
    """Регрессии по реальному ответу бота от 12.10.2026.

    В том дайджесте заголовок раздела печатался дважды, а заголовок конкурентов
    оказался в конце первого сообщения — из-за этого уведомление выглядело как
    «дайджест только по новостям».
    """

    @staticmethod
    def big_items(conn, label: str, source: str, count: int, description_len: int = 0) -> None:
        for index in range(count):
            raw = RawItem(
                title=f"{label}: материал номер {index} — заголовок для реалистичного объёма",
                url=f"https://example.com/{label}/{index}",
                description="Описание материала. " * (description_len // 20) if description_len else "",
                created_at=iso_now(),
                source=source,
                label=label,
            )
            repo.save_items(conn, [build_item(raw, source, label, DEDUP)])

    def test_section_header_printed_once(self, conn):
        cfg = make_cfg()
        self.big_items(conn, "news", "tass_news", 3, 200)
        self.big_items(conn, "tech", "habr", 10)
        self.big_items(conn, "competitors", "vcru", 2, 100)

        messages = build_batch_messages(cfg, make_groups(conn, cfg, {"news": 3, "tech": 10, "competitors": 2}))
        joined = "\n".join(messages)

        for header in ("📰 Новости отрасли", "🛠 Обновления технологий", "🏢 Публикации конкурентов"):
            assert joined.count(header) == 1, f"{header}: {joined.count(header)} раз(а)"
        assert "12.10.2026 (3)" not in joined            # заголовок рендера не дублируется

    def test_sections_fit_into_one_message_when_small(self, conn):
        """Раньше лишние пустые строки съедали место и дайджест рвался надвое."""
        cfg = make_cfg()
        self.big_items(conn, "news", "tass_news", 3, 200)
        self.big_items(conn, "tech", "habr", 10)
        self.big_items(conn, "competitors", "vcru", 2, 100)

        messages = build_batch_messages(cfg, make_groups(conn, cfg, {"news": 3, "tech": 10, "competitors": 2}))

        assert len(messages) == 1
        assert len(messages[0]) <= 4000

    def test_no_orphan_section_header_at_end(self, conn):
        cfg = make_cfg()
        self.big_items(conn, "news", "tass_news", 30, 200)      # заведомо на несколько сообщений
        self.big_items(conn, "competitors", "vcru", 2)

        messages = build_batch_messages(cfg, make_groups(conn, cfg, {"news": 30, "competitors": 2}))

        assert len(messages) > 1
        for message in messages[:-1]:
            last_line = message.rstrip().split("\n")[-1]
            # сообщение не должно заканчиваться заголовком раздела без материалов
            assert not (last_line.startswith("<b>") and last_line.rstrip().endswith((")", "— 2", "— 1")))

    def test_summary_survives_header_stripping(self, conn):
        cfg = make_cfg()
        self.big_items(conn, "news", "tass_news", 2)
        label = cfg.label("news")
        rows = conn.execute("SELECT * FROM items WHERE label = 'news' ORDER BY id").fetchall()
        rendered = render_section(label, rows, cfg, summary="Короткое саммари раздела.", tz_name="Europe/Moscow")
        section = Section(label=label, messages=rendered, item_ids=[int(row["id"]) for row in rows])
        query = QUERIES[0]

        text = build_batch_messages(cfg, [(query, [section], section.item_ids)], "Europe/Moscow")[0]

        assert "Короткое саммари раздела." in text
        assert text.count("Новости отрасли") == 1

    def test_tags_balanced_in_every_message(self, conn):
        cfg = make_cfg()
        self.big_items(conn, "news", "tass_news", 40, 120)
        self.big_items(conn, "tech", "habr", 20, 120)

        messages = build_batch_messages(cfg, make_groups(conn, cfg, {"news": 40, "tech": 20}))

        assert len(messages) > 1
        for message in messages:
            assert len(message) <= 4096
            assert cut_is_safe(message) is True          # нет незакрытых тегов на границе


class TestBatchSummary:
    """Резюме должно быть одно и по всем разделам — иначе оно читается как «сводка новостей»."""

    class StubSummarizer:
        def __init__(self, answer: str = "Общее резюме.") -> None:
            self.answer = answer
            self.sections: dict[str, list] | None = None
            self.calls = 0

        async def __call__(self, rows):
            self.calls += 1
            return self.answer

        async def summarize_sections(self, sections):
            self.calls += 1
            self.sections = sections
            return self.answer

    async def test_summary_covers_every_section(self, conn):
        cfg = make_cfg()
        for label, source in (("news", "tass_news"), ("tech", "habr"), ("competitors", "vcru")):
            add_items(conn, label, source, 2)
        groups = make_groups(conn, cfg, {"news": 2, "tech": 2, "competitors": 2})
        rows_by_group = [group[1][0].rows for group in groups]
        summarizer = self.StubSummarizer()

        summary = await batch_summary(summarizer, cfg, groups, rows_by_group)

        assert summary == "Общее резюме."
        assert summarizer.calls == 1                      # один вызов на весь дайджест
        assert set(summarizer.sections) == {
            "📰 Новости отрасли", "🛠 Обновления технологий", "🏢 Публикации конкурентов",
        }
        for rows in summarizer.sections.values():
            assert len(rows) == 2

    async def test_single_section_uses_plain_call(self, conn):
        cfg = make_cfg()
        add_items(conn, "tech", "habr", 1)
        groups = make_groups(conn, cfg, {"tech": 1})
        summarizer = self.StubSummarizer()

        await batch_summary(summarizer, cfg, groups, [groups[0][1][0].rows])

        assert summarizer.calls == 1
        assert summarizer.sections is None                # для одного раздела обычный путь

    async def test_no_summarizer_means_no_summary(self, conn):
        cfg = make_cfg()
        add_items(conn, "tech", "habr", 1)
        groups = make_groups(conn, cfg, {"tech": 1})

        assert await batch_summary(None, cfg, groups, [groups[0][1][0].rows]) is None

    def test_prompt_lists_all_sections(self):
        from digest.llm import DeepSeekSummarizer

        summarizer = DeepSeekSummarizer("key", client=object())
        rows = [{"title": "Заголовок", "description": "Описание"}]

        prompt = summarizer.build_batch_prompt({
            "📰 Новости отрасли": rows,
            "🛠 Обновления технологий": rows,
        })

        assert "📰 Новости отрасли:" in prompt
        assert "🛠 Обновления технологий:" in prompt
        assert "Заголовок" in prompt

    def test_prompt_of_empty_sections_is_empty(self):
        from digest.llm import DeepSeekSummarizer

        summarizer = DeepSeekSummarizer("key", client=object())

        assert summarizer.build_batch_prompt({}) == ""
        assert summarizer.build_batch_prompt({"📰 Новости": []}) == ""

    def test_summary_is_rendered_in_header_before_sections(self, conn):
        cfg = make_cfg()
        add_items(conn, "news", "tass_news", 1)
        add_items(conn, "tech", "habr", 1)
        groups = make_groups(conn, cfg, {"news": 1, "tech": 1})

        text = build_batch_messages(cfg, groups, "Europe/Moscow", summary="Короткое общее резюме.")[0]

        assert text.index("Короткое общее резюме.") < text.index("📰 Новости отрасли")
        assert text.startswith("🗓 ")

    def test_summary_is_html_escaped(self, conn):
        cfg = make_cfg()
        add_items(conn, "news", "tass_news", 1)
        groups = make_groups(conn, cfg, {"news": 1})

        text = build_batch_messages(cfg, groups, "Europe/Moscow", summary="Формула a<b и <script>")[0]

        assert "<script>" not in text
        assert "&lt;script&gt;" in text

    async def test_delivery_passes_rows_for_summary(self, conn):
        cfg = make_cfg()
        add_items(conn, "news", "tass_news", 2)
        add_items(conn, "tech", "habr", 1)
        groups = make_groups(conn, cfg, {"news": 2, "tech": 1})
        rows_by_group = [group[1][0].rows for group in groups]
        summarizer = self.StubSummarizer("Резюме по двум разделам.")

        await deliver_sections(
            conn, groups, RecordingSender(), cfg=cfg, tz_name="Europe/Moscow",
            summarizer=summarizer, rows_by_group=rows_by_group,
        )

        assert summarizer.sections is not None
        assert len(summarizer.sections) == 2


class TestFitsLimit:
    def test_short_part_fits(self):
        assert fits_limit("x" * 10, "y" * 10, 100) is True

    def test_part_fits_without_cutting(self):
        # запаса 8 символов, часть 20 — решение принимает проверка границы
        assert fits_limit("x" * 90, "y" * 20, 100) is True

    def test_no_room_at_all(self):
        assert fits_limit("x" * 120, "часть", 100) is False

    def test_cut_inside_open_tag_is_refused(self):
        # на границе остаётся незакрытый <b>
        assert cut_is_safe("<b>длинный") is False

    def test_cut_inside_closing_tag_is_refused(self):
        assert cut_is_safe("текст</b") is False

    def test_cut_between_tags_is_safe(self):
        assert cut_is_safe("<b>заголовок</b>") is True
        assert cut_is_safe("обычный текст без тегов") is True

    def test_nested_tags_balanced(self):
        assert cut_is_safe("<b>жирный <i>курсив</i></b>") is True
        assert cut_is_safe("<b>жирный <i>курсив</i>") is False


class TestDeliverSections:
    async def test_one_message_carries_all_sections(self, conn):
        cfg = make_cfg()
        items = {}
        for label, source in (("news", "tass_news"), ("tech", "habr")):
            add_items(conn, label, source, 2)
            items[label] = 2
        groups = make_groups(conn, cfg, items)
        sender = RecordingSender()

        messages_sent, attached, error = await deliver_sections(conn, groups, sender, cfg=cfg, tz_name="Europe/Moscow")

        assert error is None
        assert messages_sent == 1
        assert len(sender.batches) == 1                  # одно сообщение на прогон
        assert "🛠 Обновления технологий" in sender.batches[0][0]
        assert attached == 4

    async def test_delivery_marks_each_section_once(self, conn):
        cfg = make_cfg()
        add_items(conn, "news", "tass_news", 1)
        add_items(conn, "tech", "habr", 1)
        groups = make_groups(conn, cfg, {"news": 1, "tech": 1})

        await deliver_sections(conn, groups, RecordingSender(), cfg=cfg, tz_name="Europe/Moscow")

        rows = conn.execute("SELECT query_id, items_count FROM digest_log ORDER BY id").fetchall()
        assert [(row["query_id"], row["items_count"]) for row in rows] == [("daily_news", 1), ("daily_tech", 1)]
        assert conn.execute("SELECT count(*) FROM digest_items").fetchone()[0] == 2

    async def test_failed_delivery_consumes_nothing(self, conn):
        cfg = make_cfg()
        for query in QUERIES:
            repo.save_query(conn, query)
        add_items(conn, "news", "tass_news", 2)
        groups = make_groups(conn, cfg, {"news": 2})

        messages_sent, attached, error = await deliver_sections(
            conn, groups, RecordingSender(fail=True), cfg=cfg, tz_name="Europe/Moscow"
        )

        assert error is not None and "Telegram недоступен" in error
        assert attached == 0
        assert conn.execute("SELECT count(*) FROM digest_items").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM digest_log").fetchone()[0] == 0
        assert conn.execute("SELECT last_run FROM queries WHERE query_id='daily_news'").fetchone()["last_run"] is None

    async def test_repeat_run_sends_nothing_new(self, conn):
        cfg = make_cfg()
        add_items(conn, "news", "tass_news", 1)
        groups = make_groups(conn, cfg, {"news": 1})
        sender = RecordingSender()

        await deliver_sections(conn, groups, sender, cfg=cfg, tz_name="Europe/Moscow")
        # второй прогон того же набора: материалы уже отмечены отправленными
        from digest import builder as digest_builder

        assert digest_builder.select_items(conn, cfg, cfg.queries[0]) == []


class TestDigestCommandSendsOneMessage:
    """Сценарий пользователя: /digest по всем разделам — одно сообщение, а не серия."""

    @staticmethod
    async def harness(workdir, monkeypatch, items: dict[str, int]):
        from datetime import datetime, timezone

        from aiogram import Bot, Dispatcher
        from aiogram.client.default import DefaultBotProperties
        from aiogram.client.session.base import BaseSession
        from aiogram.enums import ParseMode
        from aiogram.methods import AnswerCallbackQuery, SendMessage
        from aiogram.types import Chat, Message, Update, User

        from bot.telegram_bot import BotContext, build_router

        conn = connect(workdir / "cmd.db")
        init_schema(conn)
        cfg = make_cfg()
        for query in QUERIES:
            repo.save_query(conn, query)
        for label, count in items.items():
            source = next(src.id for src in SOURCES if src.label == label)
            add_items(conn, label, source, count)

        async def no_collection(*args, **kwargs):
            return []

        monkeypatch.setattr("pipeline.collect_sources", no_collection)

        class Sender:
            """Записывает то, что реально уходит в чат (дайджест идёт через sender)."""

            dry_run = False

            def __init__(self) -> None:
                self.messages: list[str] = []

            async def send_many(self, chat_id, messages, pause=None) -> int:
                self.messages.extend(messages)
                return len(messages)

            async def send(self, chat_id, text) -> int:
                self.messages.append(text)
                return 1

            async def close(self) -> None:
                pass

        class Session(BaseSession):
            def __init__(self) -> None:
                super().__init__()
                self.answers: list[str] = []

            async def close(self) -> None:
                pass

            async def stream_content(self, *args, **kwargs):
                raise NotImplementedError

            async def make_request(self, bot, method, timeout=None):  # noqa: ANN001
                if isinstance(method, SendMessage):
                    self.answers.append(method.text or "")
                    return Message(
                        message_id=len(self.answers),
                        date=datetime.now(timezone.utc),
                        chat=Chat(id=int(method.chat_id), type="private"),
                        from_user=User(id=1, is_bot=True, first_name="b"),
                        text=method.text,
                    )
                if isinstance(method, AnswerCallbackQuery):
                    return True
                raise AssertionError(type(method).__name__)

        session = Session()
        sender = Sender()
        bot = Bot(token="1:TEST", session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        bot._me = User(id=1, is_bot=True, first_name="b")
        context = BotContext(
            conn=conn,
            cfg=cfg,
            sender=sender,
            allowed_chat_ids={"555"},
            timezone="Europe/Moscow",
        )
        dispatcher = Dispatcher()
        dispatcher.include_router(build_router(context))
        update = Update(
            update_id=1,
            message=Message(
                message_id=1,
                date=datetime.now(timezone.utc),
                chat=Chat(id=555, type="private"),
                from_user=User(id=555, is_bot=False, first_name="t"),
                text="/digest",
            ),
        )
        await dispatcher.feed_update(bot, update)
        return sender.messages, session.answers, conn

    async def test_two_sections_arrive_in_one_message(self, workdir, monkeypatch):
        sent, answers, conn = await self.harness(workdir, monkeypatch, {"tech": 3, "competitors": 2})

        digest_messages = [text for text in sent if "Дайджест за" in text]
        assert len(digest_messages) == 1                     # одно сообщение на прогон
        assert "🛠 Обновления технологий" in digest_messages[0]
        assert "🏢 Публикации конкурентов" in digest_messages[0]
        assert conn.execute("SELECT count(*) FROM digest_log WHERE status='sent'").fetchone()[0] == 2
        assert any("Готово" in text for text in answers)     # итоговая сводка бота
        conn.close()

    async def test_summary_mentions_sent_and_empty_sections(self, workdir, monkeypatch):
        _sent, answers, conn = await self.harness(workdir, monkeypatch, {"tech": 2})

        summary = answers[-1]
        assert "Готово" in summary
        assert "без нового" in summary          # новости/вакансии/конкуренты пусты
        conn.close()

    async def test_empty_sections_get_their_own_report(self, workdir, monkeypatch):
        sent, _answers, conn = await self.harness(workdir, monkeypatch, {"tech": 1})

        reports = [text for text in sent if "сегодня нового нет" in text]
        assert reports                               # «не может молчать»
        assert any("Окно проверки" in text for text in reports)
        sections = [text for text in sent if text.startswith("🗓")]
        assert len(sections) == 1                    # пустые разделы не подмешиваются в дайджест
        conn.close()
