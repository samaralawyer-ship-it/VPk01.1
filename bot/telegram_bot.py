"""Telegram-бот: единственный интерфейс пользователя (aiogram 3.x).

Бот принимает команды, а отправляет через bot/sender.py — так одна и та же
отправка работает и в боте, и в планировщике, и в DRY_RUN.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from config.loader import AppConfig
from database import repo
from digest import builder
from pipeline import collection_lock, deliver_sections, run_query
from scheduler.jobs import next_runs, pause_all, resume_all

from .sender import SenderError, TelegramSender, proxy_from_env

logger = logging.getLogger(__name__)

# Справка уходит с parse_mode=HTML, и это не место для угловых скобок:
# Telegram видит «<метка>» как HTML-тег и отклоняет ВСЁ сообщение ошибкой
# «Unsupported start tag "метка"», то есть человек не получает даже /help.
# Поэтому обязательные аргументы пишем без < > — просто словом.
COMMANDS_HELP = (
    "/digest — собрать и прислать дайджест по всем разделам\n"
    "/digest метка — только по указанной метке, например /digest news\n"
    "        метки: news, tech, competitors,vacancies"
    "/last [метка] — последние материалы из базы, без обращения к источникам\n"
    "/labels — разделы и количество записей\n"
    "/sources — источники и статус последнего прогона\n"
    "/status — последний запуск, размер базы, расписание\n"
    "/clear — очистить базу (материалы и историю дайджестов), с подтверждением\n"
    "/clear дата1 [дата2] — очистить записи за период, например /clear 2026-10-01 2026-10-05\n"
    "/pause, /resume — выключить или включить автозапуск\n"
    "/id — ваш chat_id"
)

TRUE_VALUES = {"да", "yes", "y", "д", "+", "confirm", "подтверждаю", "подтвердить", "ок", "ok"}
FALSE_VALUES = {"нет", "no", "n", "н", "-", "cancel", "отмена", "отменить", "стоп"}
CLEAR_CONFIRM_TIMEOUT = 120          # секунд на подтверждение, потом запрос забывается
CLEAR_TTL_MAX = 15 * 60              # страховка от утечки словаря ожиданий


@dataclass
class BotContext:
    conn: sqlite3.Connection
    cfg: AppConfig
    sender: TelegramSender | None = None
    summarizer: Any | None = None
    allowed_chat_ids: set[str] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=collection_lock)
    timezone: str | None = None
    scheduler: Any | None = None
    translator: Any | None = None      # перевод иностранных источников

    @property
    def tz_name(self) -> str | None:
        """Синоним timezone: под этим именем таймзону ждут общие функции планировщика.

        /pause и /resume передают в них BotContext, а register_jobs умеет работать
        и с SchedulerContext (см. QueryStore).
        """
        return self.timezone


def allowed_chat_ids(env: Mapping[str, str] | None = None) -> set[str]:
    """Разрешённые чаты: TG_CHAT_ID плюс необязательный список TG_ALLOWED_CHAT_IDS."""
    environment = os.environ if env is None else env
    raw = [environment.get("TG_CHAT_ID", "")]
    raw.extend((environment.get("TG_ALLOWED_CHAT_IDS") or "").split(","))
    return {value.strip() for value in raw if value and value.strip()}


class AllowlistMiddleware(BaseMiddleware):
    """Токен бота находится поиском, поэтому чужие чаты не обслуживаются."""

    def __init__(self, allowed: set[str]) -> None:
        self.allowed = allowed

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]):
        message = event if isinstance(event, Message) else None
        if message is None:
            return await handler(event, data)

        chat_id = str(message.chat.id)
        if self.allowed and chat_id not in self.allowed:
            logger.warning("доступ запрещён чату %s", chat_id)
            try:
                await message.answer("Доступ ограничен.")
            except Exception:  # noqa: BLE001 — ответ может не дойти, это не повод падать
                pass
            return None
        return await handler(event, data)


def parse_label_argument(context: BotContext, raw: str | None) -> tuple[str | None, str | None]:
    """Разобрать аргумент команды: возвращает (метка, текст ошибки)."""
    if not raw or not raw.strip():
        return None, None
    candidate = raw.strip().split()[0].lower().lstrip("#")
    known = {label.id for label in context.cfg.labels}
    if candidate not in known:
        return None, f"Неизвестная метка «{candidate}». Доступные: {', '.join(sorted(known))}"
    return candidate, None


def help_text(context: BotContext) -> str:
    labels = ", ".join(label.header for label in context.cfg.labels)
    return (
        "Агрегатор новостей и вакансий\n\n"
        f"Разделы: {labels}\n\n"
        f"{COMMANDS_HELP}"
    )


# --------------------------------------------------------------------------- #
# /clear — очистка базы с подтверждением


class ClearRequest:
    """Что именно попросили очистить: без дат — всю базу, с датами — период."""

    def __init__(self, start: date | None = None, end: date | None = None) -> None:
        self.start = start
        self.end = end

    @property
    def is_full(self) -> bool:
        return self.start is None

    @property
    def lower_bound(self) -> str | None:
        """Начало периода как ISO-строка UTC.

        Даты в базе хранятся в ISO 8601 UTC (см. database/db.py), поэтому границы
        переводим в тот же формат: полуночь указанного дня по UTC. Материалов
        старше 2020 года в базе нет, так что «вся база» и «с 2020-01-01» совпадают.
        """
        if self.start is None:
            return None
        return datetime(self.start.year, self.start.month, self.start.day, tzinfo=timezone.utc).isoformat()

    @property
    def upper_bound(self) -> str | None:
        """Конец периода (не включается) — следующий день, чтобы дата 2 попала в очистку.

        В запросах к БД верхняя граница строгая (`<`), поэтому «01.10 — 05.10»
        превращается в [01.10 00:00, 06.10 00:00). Без +1 день последние сутки
        периода не удалялись бы — самая частая ошибка в таких командах.
        """
        if self.end is None:
            return None
        exclusive = self.end + timedelta(days=1)
        return datetime(exclusive.year, exclusive.month, exclusive.day, tzinfo=timezone.utc).isoformat()


@dataclass
class PendingClear:
    """Запрос на очистку, ждущий подтверждения, и время его создания."""

    request: ClearRequest
    created_at: float


# Ожидания подтверждения по чатам. Хранится в памяти процесса: если сервис
# перезапустится, неподтверждённая очистка просто забывается — это безопаснее,
# чем восстанавливать разрушительное действие из базы.
_PENDING_CLEAR: dict[str, PendingClear] = {}


def parse_clear_date(raw: str) -> date | None:
    """Дата из аргумента: 2026-10-01 (ISO) или 01.10.2026 (день первым).

    Полный `date.fromisoformat` не используем: он принял бы «2026-10-1» и так же
    молча пропустил бы «2026-1-1» — а такие опечатки означают не то, что человек
    думал, и в команде удаления это недопустимо. Поэтому формат строгий.
    """
    text = raw.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_clear_request(raw: str | None, *, force_full: bool = True) -> tuple[ClearRequest | None, str | None]:
    """Разобрать аргумент /clear. Возвращает (запрос, текст ошибки).

    Пустой аргумент — вся база (force_full). При force_full=False пустой
    аргумент означает «это не запрос на очистку»: так /clear отличает ответ
    «да» из чата от собственно команды.
    """
    tokens = (raw or "").split()
    if not tokens:
        return (ClearRequest(), None) if force_full else (None, None)
    if len(tokens) > 2:
        return None, "Слишком много аргументов. Формат: /clear [дата1] [дата2]"

    parsed: list[date] = []
    for token in tokens:
        value = parse_clear_date(token)
        if value is None:
            return None, (
                f"Не понял дату «{token}». Формат: <code>ГГГГ-ММ-ДД</code> "
                "или <code>ДД.ММ.ГГГГ</code>, например <code>/clear 2026-10-01 2026-10-05</code>"
            )
        parsed.append(value)

    start = parsed[0]
    end = parsed[1] if len(parsed) == 2 else parsed[0]
    if end < start:
        return None, f"Конец периода ({end:%d.%m.%Y}) раньше начала ({start:%d.%m.%Y})."
    return ClearRequest(start, end), None


def parse_confirmation(raw: str | None) -> bool | None:
    """Ответ текстом: True — да, False — нет, None — это не ответ на подтверждение."""
    token = (raw or "").strip().lower()
    if token in TRUE_VALUES:
        return True
    if token in FALSE_VALUES:
        return False
    return None


def pending_clear(chat_id: int | str) -> ClearRequest | None:
    """Запрос, ждущий подтверждения. Просроченный забываем, чтобы «да» через час не сработало.

    Срок годности — CLEAR_CONFIRM_TIMEOUT (2 минуты). Это защита от случайного
    совпадения: человек ответил «да» на другой вопрос, а подтвердилась очистка.
    """
    key = str(chat_id)
    item = _PENDING_CLEAR.get(key)
    if item is None:
        return None
    if time.monotonic() - item.created_at > CLEAR_CONFIRM_TIMEOUT:
        _PENDING_CLEAR.pop(key, None)
        return None
    return item.request


def remember_clear(chat_id: int | str, request: ClearRequest) -> None:
    """Запомнить запрос до подтверждения, попутно подчистив давние записи.

    Новый /clear заменяет предыдущий неподтверждённый: так «передумал, указал
    другой период» работает без отдельной команды отмены.
    """
    now = time.monotonic()
    for key, item in list(_PENDING_CLEAR.items()):       # не растим словарь бесконечно
        if now - item.created_at > CLEAR_TTL_MAX:
            _PENDING_CLEAR.pop(key, None)
    _PENDING_CLEAR[str(chat_id)] = PendingClear(request, now)


def forget_clear(chat_id: int | str) -> None:
    _PENDING_CLEAR.pop(str(chat_id), None)


def clear_scope_text(request: ClearRequest) -> str:
    """Человекочитаемое описание того, что очищаем: «вся база» или «период …»."""
    if request.is_full:
        return "вся база"
    if request.end and request.end != request.start:
        return f"период {request.start:%d.%m.%Y} — {request.end:%d.%m.%Y}"
    return f"период {request.start:%d.%m.%Y}"


def clear_confirm_text(conn: sqlite3.Connection, request: ClearRequest) -> str:
    """Предупреждение перед удалением: что, сколько и чего не коснётся.

    Считаем именно количество попадающих под удаление материалов, а не общее
    число в базе: человек должен видеть объём последствий, а не догадываться.
    """
    pending = repo.count_items_in_range(conn, request.lower_bound, request.upper_bound)
    return (
        "⚠️ <b>Очистка базы</b>\n\n"
        f"Будет удалено: <b>{clear_scope_text(request)}</b>\n"
        f"Материалов под удаление: <b>{pending}</b>\n"
        f"Всего в базе: {repo.count_items(conn)}\n\n"
        "Вместе с материалами уйдёт история дайджестов по ним.\n"
        "Подписки, расписание и журнал прогонов источников не затрагиваются.\n\n"
        "Подтвердите: кнопка ниже, либо ответьте <b>да</b> или <b>нет</b>."
    )


def clear_confirm_markup() -> InlineKeyboardMarkup:
    """Кнопки подтверждения. callback_data разбирается в обработчике callback_query."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Да, очистить", callback_data="clear:yes"),
            InlineKeyboardButton(text="↩️ Отмена", callback_data="clear:no"),
        ]]
    )


def perform_clear(conn: sqlite3.Connection, request: ClearRequest) -> dict[str, int]:
    """Выполнить очистку. Вызывать под collection_lock, чтобы не пересечься со сбором.

    Лок нужен потому, что сбор тоже пишет в items: удаление во время вставки
    пакета дало бы несогласованный результат (часть материалов уже отправлена,
    часть удалена).
    """
    return repo.clear_items(conn, request.lower_bound, request.upper_bound)


def clear_result_text(request: ClearRequest, result: Mapping[str, int]) -> str:
    return (
        "✅ Очистка выполнена\n\n"
        f"Удалено материалов: <b>{result.get('items', 0)}</b>\n"
        f"Удалено записей истории дайджестов: {result.get('digests', 0)}\n"
        f"Очищено: {clear_scope_text(request)}"
    )


def status_text(context: BotContext) -> str:
    conn, cfg = context.conn, context.cfg
    last = repo.last_run(conn)
    lines = [
        f"Записей в базе: {repo.count_items(conn)} (за сутки: {repo.count_items_since_hours(conn, 24)})",
        f"Источников включено: {len(cfg.enabled_sources)} из {len(cfg.sources)}",
        f"DRY_RUN: {context.sender.dry_run}",
    ]
    if last:
        lines.append(
            f"Последний прогон: {last['source']} — {last['status']}, new={last['items_new']}, "
            f"dup={last['items_dup']}, filtered={last['items_filtered']} ({last['started_at']})"
        )
        if last["error"]:
            lines.append(f"Ошибка: {last['error']}")
    else:
        lines.append("Прогонов ещё не было.")

    lines.append("")
    lines.append("Расписание:")
    for query in cfg.queries:
        row = conn.execute("SELECT is_active FROM queries WHERE query_id = ?", (query.id,)).fetchone()
        active = "включено" if (row is None or row["is_active"]) else "выключено"
        lines.append(
            f"  {query.id}: {query.schedule}, метка {query.label}, "
            f"глубина {query.lookback_days} дн., лимит {query.limit} — {active}"
        )

    if context.scheduler is not None:
        runs = next_runs(context.scheduler, limit=4)
        if runs:
            lines.append("")
            lines.append("Ближайшие срабатывания:")
            lines.extend(f"  {name}: {when}" for name, when in runs)
    return "\n".join(lines)


def sources_text(context: BotContext) -> str:
    conn, cfg = context.conn, context.cfg
    latest: dict[str, sqlite3.Row] = {}
    for row in repo.recent_runs(conn, limit=100):
        latest.setdefault(row["source"], row)

    lines = ["Источники:"]
    for source in cfg.sources:
        mark = "вкл" if source.enabled else "выкл"
        run = latest.get(source.id)
        if run is None:
            state = "прогонов не было"
        else:
            state = f"{run['status']}: new={run['items_new']}, dup={run['items_dup']}, filtered={run['items_filtered']}"
            if run["error"]:
                state += f" ({run['error'][:60]})"
        lines.append(f"  {source.id} [{mark}] метка: {source.label}, тип: {source.type} — {state}")
    return "\n".join(lines)


def labels_text(context: BotContext) -> str:
    counts = repo.count_items_by_label(context.conn)
    lines = ["Разделы:"]
    for label in context.cfg.labels:
        lines.append(f"  {label.header} ({label.id}) — записей: {counts.get(label.id, 0)}")
    return "\n".join(lines)


def build_router(context: BotContext) -> Router:
    router = Router()
    router.message.middleware(AllowlistMiddleware(context.allowed_chat_ids))

    @router.message(CommandStart())
    @router.message(Command("help"))
    async def cmd_start(message: Message) -> None:
        await message.answer(help_text(context))

    @router.message(Command("id"))
    async def cmd_id(message: Message) -> None:
        username = f" (@{message.from_user.username})" if message.from_user and message.from_user.username else ""
        await message.answer(f"Ваш chat_id: <code>{message.chat.id}</code>{username}")

    @router.message(Command("labels"))
    async def cmd_labels(message: Message) -> None:
        await message.answer(labels_text(context))

    @router.message(Command("sources"))
    async def cmd_sources(message: Message) -> None:
        await message.answer(sources_text(context))

    @router.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        await message.answer(status_text(context))

    @router.message(Command("pause"))
    async def cmd_pause(message: Message) -> None:
        changed = pause_all(context.scheduler, context)
        await message.answer(f"Автозапуск выключен для запросов: {changed}.")

    @router.message(Command("resume"))
    async def cmd_resume(message: Message) -> None:
        changed = resume_all(context.scheduler, context)
        await message.answer(f"Автозапуск включён для запросов: {changed}.")

    @router.message(Command("clear"))
    async def cmd_clear(message: Message, command: CommandObject) -> None:
        """Очистка базы: /clear (вся) или /clear <дата1> [дата2] — с подтверждением.

        Команда разрушительная, поэтому здесь НИЧЕГО не удаляется: мы только
        разбираем аргумент, запоминаем запрос и показываем предупреждение.
        Удаление выполняют обработчики подтверждения (кнопка или слово «да»).
        """
        request, error = parse_clear_request(command.args)
        if error:
            # Ошибка в аргументе отменяет и ранее запомненный запрос: иначе человек
            # поправит дату, ошибётся ещё раз, а подтвердится старый период.
            forget_clear(message.chat.id)
            await message.answer(error)
            return

        remember_clear(message.chat.id, request)
        await message.answer(
            clear_confirm_text(context.conn, request),
            reply_markup=clear_confirm_markup(),
        )

    # Callback-запросы не проходят через AllowlistMiddleware (она смотрит Message),
    # поэтому доступ проверяем здесь ещё раз, до любого удаления.
    @router.callback_query(F.data.in_({"clear:yes", "clear:no"}))
    async def cb_clear(callback: CallbackQuery) -> None:
        chat_id = callback.message.chat.id if callback.message else None
        if chat_id is None:
            await callback.answer("Не вижу чат", show_alert=True)
            return
        if context.allowed_chat_ids and str(chat_id) not in context.allowed_chat_ids:
            logger.warning("доступ запрещён чату %s", chat_id)
            await callback.answer("Доступ ограничен.", show_alert=True)
            return

        request = pending_clear(chat_id)
        if callback.data == "clear:no":
            forget_clear(chat_id)
            await callback.message.edit_text("Очистка отменена.")
            await callback.answer()
            return

        if request is None:
            # Кнопку могли нажать позже срока годности — молча ничего не удаляем.
            await callback.answer("Подтверждение устарело — вызовите /clear заново", show_alert=True)
            return

        forget_clear(chat_id)                     # подтверждение одноразовое
        await callback.answer()                   # гасим «часики» на кнопке сразу
        async with context.lock:                  # см. perform_clear: не мешаем сбору
            result = perform_clear(context.conn, request)
        logger.info(
            "очистка базы по /clear: удалено материалов %s, записей дайджестов %s (%s)",
            result["items"], result["digests"], clear_scope_text(request),
        )
        await callback.message.edit_text(clear_result_text(request, result))

    @router.message(Command("last"))
    async def cmd_last(message: Message, command: CommandObject) -> None:
        label_id, error = parse_label_argument(context, command.args)
        if error:
            await message.answer(error)
            return

        targets = [label for label in context.cfg.labels if label_id is None or label.id == label_id]
        parts: list[str] = []
        for label in targets:
            rows = builder.select_last(context.conn, label.id, limit=5)
            parts.extend(builder.render_section(label, rows, context.cfg, tz_name=context.timezone))
        if not parts:
            await message.answer("В базе пока пусто — запустите /digest.")
            return
        try:
            await context.sender.send_many(message.chat.id, parts, pause=0.5)
        except SenderError as exc:
            await message.answer(f"Не удалось отправить: {exc}")

    @router.message(Command("digest"))
    async def cmd_digest(message: Message, command: CommandObject) -> None:
        label_id, error = parse_label_argument(context, command.args)
        if error:
            await message.answer(error)
            return

        queries = [query for query in context.cfg.queries if label_id is None or query.label == label_id]
        if not queries:
            await message.answer("Для этой метки не объявлено ни одного запроса.")
            return

        if context.lock.locked():
            await message.answer("Сбор уже идёт, дождитесь завершения.")
            return

        titles = ", ".join(query.label for query in queries)
        await message.answer(f"🔄 Собираю: {titles}…")

        results = []
        for query in queries:
            # sender=None: run_query только собирает и отбирает, но НЕ отправляет.
            # Отправку делает ниже deliver_sections — одним сообщением на все разделы.
            result = await run_query(
                context.conn,
                context.cfg,
                query,
                sender=None,                 # отправку делает cmd_digest одним сообщением
                summarizer=context.summarizer,
                # «Может не находить, но не может молчать»: пустой раздел тоже
                # приходит сообщением с объяснением, а не тишиной.
                notify_empty=False,          # отчёт о пустоте приложим к общей сводке
                tz_name=context.timezone,
                lock=context.lock,
                translator=context.translator,
                trigger=f"по команде /digest {label_id}" if label_id else "по команде /digest",
            )
            results.append(result)

        # Все разделы одного прогона уходят одним сообщением: иначе Telegram
        # склеивает уведомления и видимым остаётся только первый раздел.
        # В groups попадают только разделы с материалами; строки БД (rows) нужны,
        # чтобы общее резюме описывало весь дайджест, а не первый раздел.
        groups = [
            (query, result.sections, result.item_ids)
            for query, result in zip(queries, results)
            if result.has_items()
        ]
        if groups:
            _messages, _attached, error = await deliver_sections(
                context.conn,
                groups,
                context.sender,
                tz_name=context.timezone,
                cfg=context.cfg,
                summarizer=context.summarizer,
                rows_by_group=[result.rows for result in results if result.has_items()],
            )
            if error is not None:
                # Доставка не удалась: материал не расходуем (deliver_sections его
                # не отметил), но факт сбоя фиксируем в журнале дайджестов.
                for query, result in zip(queries, results):
                    if result.has_items():
                        repo.log_digest(context.conn, query.id, query.tg_chat_id, 0, status="failed")
                        logger.error("[%s] отправка не удалась: %s", query.id, error)

        # Отчёты по пустым разделам идут отдельными сообщениями: они объясняют,
        # почему раздела нет в дайджесте, и подмешивать их в сам дайджест нельзя.
        for query, result in zip(queries, results):
            if result.is_empty() and query.notify_empty:
                await context.sender.send(query.tg_chat_id, result.empty_report)
                logger.info("[%s] отчёт о пустом разделе отправлен", query.id)

        await message.answer(summarize_results(results, context))

    @router.message()
    async def fallback(message: Message) -> None:
        """Ответ на подтверждение очистки («да»/«нет») и всё остальное."""
        request = pending_clear(message.chat.id)
        if request is not None:
            decision = parse_confirmation(message.text)
            if decision is False:
                forget_clear(message.chat.id)
                await message.answer("Очистка отменена.")
                return
            if decision is True:
                forget_clear(message.chat.id)
                await message.answer("⏳ Удаляю…")
                async with context.lock:
                    result = perform_clear(context.conn, request)
                logger.info(
                    "очистка базы по /clear: удалено материалов %s, записей дайджестов %s (%s)",
                    result["items"], result["digests"], clear_scope_text(request),
                )
                await message.answer(clear_result_text(request, result))
                return
        await message.answer("Не понял. Список команд: /help")

    return router


def summarize_results(results: Sequence[Any], context: BotContext) -> str:
    sections = len([result for result in results if result.status in ("sent", "preview")])
    items = sum(result.items_sent for result in results)
    duplicates = sum(source.dup for result in results for source in result.collected)
    fresh = sum(source.new for result in results for source in result.collected)
    errors = [source for result in results for source in result.collected if source.status == "error"]

    empty = [result for result in results if result.status == "empty"]
    if not sections:
        # Молчать нельзя: объясняем, почему ничего не пришло, по каждому разделу.
        lines = []
        for result in empty:
            if result.empty_report:
                lines.append(result.empty_report)
            else:
                lines.append(f"Раздел {context.cfg.label(result.label).header}: нового нет")
        if not lines:
            lines.append("Новых материалов нет.")
        return "\n\n".join(lines)

    text = f"✅ Готово: разделов {sections}, материалов {items}, new={fresh}, dup={duplicates}"
    if empty:
        text += f", без нового: {', '.join(result.label for result in empty)}"
    if errors:
        text += f", источники с ошибкой: {', '.join(source.source_id for source in errors)}"
    return text


async def run_bot(context: BotContext, token: str, *, drop_pending_updates: bool = True) -> None:
    """Запустить long polling. Планировщик подключается рядом, в том же цикле.

    Прокси берётся из тех же переменных, что и у отправки (TG_PROXY и т. п.):
    api.telegram.org недоступен из части российских сетей, и без этого бот не
    поднимался бы там, где доставка дайджестов через sender работает.
    """
    session = AiohttpSession(proxy=proxy_from_env("TG_PROXY", "ALL_PROXY", "HTTPS_PROXY"))
    if session.proxy:
        logger.info("бот использует прокси для Telegram")
    bot = Bot(token=token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(context))
    try:
        await dispatcher.start_polling(bot, drop_pending_updates=drop_pending_updates)
    finally:
        await bot.session.close()
