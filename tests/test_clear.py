"""Тесты команды /clear: разбор периода, подтверждение кнопкой и текстом.

Команда разрушительная, поэтому проверяем и «счастливый путь», и отказы:
неизвестная дата, отмена, чужой чат, протухшее подтверждение.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

import bot.telegram_bot as module
from bot.sender import TelegramSender
from bot.telegram_bot import (
    BotContext,
    build_router,
    clear_confirm_text,
    clear_confirm_markup,
    clear_result_text,
    clear_scope_text,
    parse_clear_request,
    parse_confirmation,
    pending_clear,
    remember_clear,
    forget_clear,
)
from collectors.normalize import build_item
from config.loader import AppConfig, DedupConfig, Label, Query, Source
from database import connect, init_schema, repo
from database.models import RawItem

DEDUP = DedupConfig()
ALLOWED = 987674829
FOREIGN = 555000111
BOT_INFO = User(id=8955675631, is_bot=True, first_name="Агрегатор", username="bot")
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
QUERIES = (Query(id="daily_news", label="news", schedule="0 9 * * *", limit=10, tg_chat_id=str(ALLOWED)),)


class RecordingSession(BaseSession):
    """Сессия aiogram, которая не ходит в сеть, а записывает исходящие тексты."""

    def __init__(self) -> None:
        super().__init__()
        self.out: list[tuple[int, str]] = []
        self.alerts: list[str] = []

    async def close(self) -> None:
        pass

    async def stream_content(self, *args, **kwargs):
        raise NotImplementedError

    async def make_request(self, bot, method, timeout=None):  # noqa: ANN001
        if isinstance(method, (SendMessage, EditMessageText)):
            chat_id = int(method.chat_id) if getattr(method, "chat_id", None) else ALLOWED
            self.out.append((chat_id, getattr(method, "text", "") or ""))
            return Message(
                message_id=len(self.out),
                date=datetime.now(timezone.utc),
                chat=Chat(id=chat_id, type="private"),
                from_user=BOT_INFO,
                text=getattr(method, "text", None),
            )
        if isinstance(method, AnswerCallbackQuery):
            self.alerts.append(method.text or "")
            return True
        raise AssertionError(f"неожиданный метод: {type(method).__name__}")


@dataclass
class Harness:
    context: BotContext
    session: RecordingSession
    bot: Bot
    dispatcher: Dispatcher
    update_id: int = 0

    @property
    def out(self) -> list[str]:
        return [text for _, text in self.session.out]

    async def feed(self, text: str, chat_id: int = ALLOWED) -> Message:
        self.update_id += 1
        message = Message(
            message_id=self.update_id,
            date=datetime.now(timezone.utc),
            chat=Chat(id=chat_id, type="private"),
            from_user=User(id=chat_id, is_bot=False, first_name="Тестер"),
            text=text,
        )
        self.bot._me = BOT_INFO
        await self.dispatcher.feed_update(self.bot, Update(update_id=self.update_id, message=message))
        return message

    async def press(self, sent: Message, data: str, chat_id: int = ALLOWED) -> None:
        self.update_id += 1
        callback = CallbackQuery(
            id=str(self.update_id),
            from_user=User(id=chat_id, is_bot=False, first_name="Тестер"),
            chat_instance="chat",
            data=data,
            message=sent,
        )
        await self.dispatcher.feed_update(self.bot, Update(update_id=self.update_id, callback_query=callback))


def make_context(conn) -> BotContext:
    cfg = AppConfig(
        labels=LABELS,
        sources=SOURCES,
        queries=QUERIES,
        dedup=DEDUP,
        run_on_start=False,
    )
    return BotContext(
        conn=conn,
        cfg=cfg,
        sender=TelegramSender(dry_run=True, env={}),
        allowed_chat_ids={str(ALLOWED)},
        timezone="Europe/Moscow",
    )


@pytest.fixture
def conn(workdir):
    connection = connect(workdir / "clear.db")
    init_schema(connection)
    yield connection
    connection.close()


@pytest.fixture
def harness(conn):
    forget_clear(ALLOWED)
    forget_clear(FOREIGN)
    session = RecordingSession()
    bot = Bot(token="123:TEST", session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    context = make_context(conn)
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(context))
    return Harness(context=context, session=session, bot=bot, dispatcher=dispatcher)


def add_items(conn, count: int = 3, day: int = 1) -> None:
    for index in range(count):
        raw = RawItem(
            title=f"Материал {index}",
            url=f"https://example.com/{day}-{index}",
            created_at=f"2026-10-{day:02d}T10:00:00+00:00",
        )
        repo.save_items(conn, [build_item(raw, "tass_news", "news", DEDUP)])


class TestParseClearRequest:
    def test_no_argument_means_whole_base(self):
        request, error = parse_clear_request(None)

        assert error is None
        assert request.is_full is True
        assert request.lower_bound is None and request.upper_bound is None

    def test_blank_argument_means_whole_base(self):
        request, _ = parse_clear_request("   ")

        assert request.is_full is True

    def test_iso_period(self):
        request, error = parse_clear_request("2026-10-01 2026-10-05")

        assert error is None
        assert request.lower_bound == "2026-10-01T00:00:00+00:00"
        assert request.upper_bound == "2026-10-06T00:00:00+00:00"      # конец включительно

    def test_day_first_period(self):
        request, error = parse_clear_request("01.10.2026 05.10.2026")

        assert error is None
        assert request.lower_bound.startswith("2026-10-01")
        assert request.upper_bound.startswith("2026-10-06")

    def test_single_date_covers_that_day(self):
        request, _ = parse_clear_request("2026-10-03")

        assert request.lower_bound.startswith("2026-10-03")
        assert request.upper_bound.startswith("2026-10-04")

    def test_reversed_period_is_rejected(self):
        request, error = parse_clear_request("2026-10-05 2026-10-01")

        assert request is None
        assert "раньше начала" in error

    def test_bad_date_is_rejected(self):
        request, error = parse_clear_request("вчера")

        assert request is None
        assert "Не понял дату" in error

    def test_too_many_arguments(self):
        request, error = parse_clear_request("2026-10-01 2026-10-05 2026-10-09")

        assert request is None
        assert "Слишком много" in error

    def test_blank_argument_can_mean_not_a_request(self):
        assert parse_clear_request(None, force_full=False) == (None, None)


class TestParseConfirmation:
    @pytest.mark.parametrize("token", ["да", "Да", " ДА ", "yes", "y", "+", "подтверждаю"])
    def test_yes(self, token):
        assert parse_confirmation(token) is True

    @pytest.mark.parametrize("token", ["нет", "Нет", "no", "отмена", "-"])
    def test_no(self, token):
        assert parse_confirmation(token) is False

    @pytest.mark.parametrize("token", ["ага", "", None, "статус"])
    def test_unclear_is_not_a_decision(self, token):
        assert parse_confirmation(token) is None


class TestClearTexts:
    def test_confirm_text_warns_and_counts(self, conn):
        add_items(conn, 3)

        text = clear_confirm_text(conn, parse_clear_request(None)[0])

        assert "вся база" in text
        assert "Материалов под удаление: <b>3</b>" in text
        assert "Подтвердите" in text

    def test_confirm_text_for_period(self, conn):
        add_items(conn, 2, day=1)
        add_items(conn, 1, day=7)

        request, _ = parse_clear_request("2026-10-01 2026-10-02")
        text = clear_confirm_text(conn, request)

        assert "01.10.2026 — 02.10.2026" in text
        assert "Материалов под удаление: <b>2</b>" in text

    def test_scope_text_forms(self):
        assert clear_scope_text(parse_clear_request(None)[0]) == "вся база"
        assert "01.10.2026 — 05.10.2026" in clear_scope_text(parse_clear_request("2026-10-01 2026-10-05")[0])

    def test_result_text_reports_counts(self):
        request = parse_clear_request(None)[0]

        text = clear_result_text(request, {"items": 7, "digests": 2})

        assert "Удалено материалов: <b>7</b>" in text
        assert "истории дайджестов: 2" in text
        assert "Очищено: вся база" in text

    def test_keyboard_has_confirm_and_cancel(self):
        data = [button.callback_data for row in clear_confirm_markup().inline_keyboard for button in row]

        assert data == ["clear:yes", "clear:no"]

    def test_help_lists_clear(self):
        assert "/clear" in module.COMMANDS_HELP


class TestBotRepliesAreValidHtml:
    """Регрессия: справка с «<метка>» ломала /start целиком.

    Telegram при parse_mode=HTML видит «<метка>» как тег и отклоняет всё
    сообщение ошибкой «Unsupported start tag». Поэтому любой текст, который бот
    отправляет, обязан содержать только допустимые теги.
    """

    ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre", "tg-spoiler"}
    TAG_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9-]*)[^>]*>")

    @classmethod
    def suspicious_tags(cls, text: str) -> list[str]:
        return [tag for tag in cls.TAG_RE.findall(text) if tag.lower() not in cls.ALLOWED_TAGS]

    async def test_command_replies_have_no_foreign_tags(self, harness):
        for command in ("/start", "/help", "/status", "/labels", "/sources", "/id", "/last"):
            await harness.feed(command)

        for text in harness.out:
            assert self.suspicious_tags(text) == [], f"{text[:120]}"

    def test_help_text_is_safe(self, harness):
        text = module.help_text(harness.context)

        assert self.suspicious_tags(text) == []
        assert "/clear" in text and "/digest" in text

    def test_help_has_no_angle_brackets_at_all(self, harness):
        # «<метка>» — именно так сломался /start, поэтому скобок тут быть не должно
        assert "<" not in module.COMMANDS_HELP
        assert ">" not in module.COMMANDS_HELP

    def test_clear_texts_are_safe(self, harness, conn):
        add_items(conn, 2)
        request, _ = parse_clear_request("2026-10-01 2026-10-05")

        for text in (clear_confirm_text(conn, request), clear_result_text(request, {"items": 2, "digests": 1})):
            assert self.suspicious_tags(text) == []

    def test_summary_results_is_safe(self, harness):
        from pipeline import QueryResult, SourceResult
        from bot.telegram_bot import summarize_results

        results = [
            QueryResult(query_id="daily_news", label="news", status="empty",
                        empty_report="📰 <b>Новости</b> — нового нет"),
            QueryResult(query_id="daily_tech", label="tech", status="sent", items_sent=2,
                        collected=[SourceResult("habr", "tech", "ok", fetched=3, new=2)]),
        ]

        assert self.suspicious_tags(summarize_results(results, harness.context)) == []


class TestPendingClear:
    def test_remember_and_read(self):
        request = parse_clear_request("2026-10-01")[0]
        remember_clear(ALLOWED, request)

        assert pending_clear(ALLOWED) is request
        forget_clear(ALLOWED)
        assert pending_clear(ALLOWED) is None

    def test_expired_confirmation_is_forgotten(self, monkeypatch):
        remember_clear(ALLOWED, parse_clear_request(None)[0])
        monkeypatch.setattr(module, "CLEAR_CONFIRM_TIMEOUT", -1)

        assert pending_clear(ALLOWED) is None


class TestClearCommand:
    async def test_without_argument_asks_confirmation_for_whole_base(self, harness, conn):
        add_items(conn, 3)

        await harness.feed("/clear")

        assert "вся база" in harness.out[-1]
        assert pending_clear(ALLOWED) is not None
        assert repo.count_items(conn) == 3                    # ничего не удалено до подтверждения

    async def test_with_period_shows_window(self, harness, conn):
        add_items(conn, 2, day=1)
        add_items(conn, 1, day=7)

        await harness.feed("/clear 2026-10-01 2026-10-02")

        assert "01.10.2026 — 02.10.2026" in harness.out[-1]
        assert "Материалов под удаление: <b>2</b>" in harness.out[-1]

    async def test_bad_date_gives_error_and_no_pending(self, harness):
        await harness.feed("/clear вчера")

        assert "Не понял дату" in harness.out[-1]
        assert pending_clear(ALLOWED) is None

    async def test_text_yes_performs_deletion(self, harness, conn):
        add_items(conn, 3)
        await harness.feed("/clear")

        await harness.feed("да")

        assert repo.count_items(conn) == 0
        assert "Удалено материалов: <b>3</b>" in harness.out[-1]
        assert pending_clear(ALLOWED) is None

    async def test_text_no_cancels_without_deleting(self, harness, conn):
        add_items(conn, 3)
        await harness.feed("/clear")

        await harness.feed("нет")

        assert repo.count_items(conn) == 3
        assert "отменена" in harness.out[-1]

    async def test_period_confirmation_deletes_only_window(self, harness, conn):
        add_items(conn, 2, day=1)
        add_items(conn, 1, day=7)
        await harness.feed("/clear 2026-10-01 2026-10-02")

        await harness.feed("да")

        assert repo.count_items(conn) == 1
        assert "Удалено материалов: <b>2</b>" in harness.out[-1]

    async def test_button_yes_performs_deletion(self, harness, conn):
        add_items(conn, 2)
        sent = await harness.feed("/clear")

        await harness.press(sent, "clear:yes")

        assert repo.count_items(conn) == 0
        assert "Удалено материалов: <b>2</b>" in harness.out[-1]

    async def test_button_no_cancels(self, harness, conn):
        add_items(conn, 2)
        sent = await harness.feed("/clear")

        await harness.press(sent, "clear:no")

        assert repo.count_items(conn) == 2
        assert "отменена" in harness.out[-1]

    async def test_button_after_expiry_is_rejected(self, harness, conn, monkeypatch):
        add_items(conn, 2)
        sent = await harness.feed("/clear")
        monkeypatch.setattr(module, "CLEAR_CONFIRM_TIMEOUT", -1)

        await harness.press(sent, "clear:yes")

        assert repo.count_items(conn) == 2                    # протухшее подтверждение не удаляет
        assert any("устарело" in text for text in harness.session.alerts)

    async def test_foreign_chat_cannot_clear(self, harness, conn):
        add_items(conn, 2)

        await harness.feed("/clear", chat_id=FOREIGN)

        assert repo.count_items(conn) == 2
        assert pending_clear(FOREIGN) is None
        assert any("Доступ ограничен" in text for text in harness.out)

    async def test_digest_history_is_removed_with_items(self, harness, conn):
        add_items(conn, 1)
        item_id = conn.execute("SELECT id FROM items").fetchone()["id"]
        digest_id = repo.log_digest(conn, "daily_news", str(ALLOWED), 1)
        repo.attach_digest_items(conn, digest_id, [item_id])

        await harness.feed("/clear")
        await harness.feed("да")

        assert conn.execute("SELECT count(*) FROM digest_log").fetchone()[0] == 0

    async def test_unrelated_text_still_gets_fallback(self, harness):
        await harness.feed("привет")

        assert "Не понял" in harness.out[-1]

    async def test_second_clear_replaces_pending_request(self, harness, conn):
        add_items(conn, 1, day=1)
        add_items(conn, 1, day=7)
        await harness.feed("/clear 2026-10-01")

        await harness.feed("/clear 2026-10-07")               # передумали: новый период
        await harness.feed("да")

        assert repo.count_items(conn) == 1
        assert conn.execute("SELECT created_at FROM items").fetchone()["created_at"].startswith("2026-10-01")
