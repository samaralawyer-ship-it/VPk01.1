"""Планировщик на APScheduler: расписание берётся из queries.yaml.

Ключевые правила из ТЗ:
  * cron-выражение у каждого запроса своё, таймзона — из TZ (Europe/Moscow);
  * max_instances=1 и coalesce=True, чтобы перекрывающиеся срабатывания не дублировали сбор;
  * run_on_start — один прогон при старте процесса («запустили и посмотрели на глубину»);
  * is_active живёт в БД: его меняют /pause и /resume, конфиг его не затирает.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Protocol, Sequence, runtime_checkable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config.loader import AppConfig, Query
from database import repo
from pipeline import QueryResult, deliver_sections, run_query

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "Europe/Moscow"
MISFIRE_GRACE_TIME = 300
JOB_PREFIX = "query:"


@dataclass
class SchedulerContext:
    """Всё, что нужно задаче, чтобы выполнить полный цикл запроса."""

    conn: sqlite3.Connection
    cfg: AppConfig
    sender: Any | None = None
    summarizer: Any | None = None
    tz_name: str | None = None
    lock: asyncio.Lock | None = None
    collect: bool = True
    translator: Any | None = None        # перевод иностранных источников


@runtime_checkable
class QueryStore(Protocol):
    """Минимум, нужный для управления автозапуском: БД и конфиг.

    Ему удовлетворяют и SchedulerContext, и BotContext — поэтому /pause и /resume
    работают из бота без дублирования логики.
    """

    conn: sqlite3.Connection
    cfg: AppConfig


def job_id(query_id: str) -> str:
    return f"{JOB_PREFIX}{query_id}"


def make_scheduler(tz_name: str | None = None) -> AsyncIOScheduler:
    return AsyncIOScheduler(timezone=tz_name or DEFAULT_TIMEZONE)


def resolve_timezone(context: Any, tz_name: str | None = None) -> str:
    """Имя таймзоны у контекста: у SchedulerContext это tz_name, у BotContext — timezone.

    Нужно потому, что /pause и /resume работают с BotContext, а register_jobs
    принимает любой из двух (см. QueryStore).
    """
    if tz_name:
        return tz_name
    for attribute in ("tz_name", "timezone"):
        value = getattr(context, attribute, None)
        if value:
            return str(value)
    return DEFAULT_TIMEZONE


def build_trigger(query: Query, tz_name: str | None = None) -> CronTrigger:
    """CronTrigger из 5-польного выражения; при ошибке конфиг уже падал на валидации."""
    return CronTrigger.from_crontab(query.schedule, timezone=tz_name or DEFAULT_TIMEZONE)


def is_query_active(conn: sqlite3.Connection, query_id: str) -> bool:
    """is_active живёт в БД; запрос, которого там ещё нет, считается включённым."""
    row = conn.execute("SELECT is_active FROM queries WHERE query_id = ?", (query_id,)).fetchone()
    return True if row is None else bool(row["is_active"])


def register_jobs(scheduler: AsyncIOScheduler, context: SchedulerContext) -> int:
    """Поставить задачи по активным запросам. Возвращает число зарегистрированных."""
    registered = 0

    for query in context.cfg.queries:
        if not is_query_active(context.conn, query.id):
            logger.info("запрос %s выключен — задача не ставится", query.id)
            continue
        scheduler.add_job(
            run_query_job,
            trigger=build_trigger(query, resolve_timezone(context)),
            id=job_id(query.id),
            name=query.id,
            args=[context, query],
            max_instances=1,
            coalesce=True,
            misfire_grace_time=MISFIRE_GRACE_TIME,
            replace_existing=True,
        )
        registered += 1
    return registered


async def run_query_job(context: SchedulerContext, query: Query) -> QueryResult:
    """Срабатывание cron: полный цикл запроса (сбор → дайджест → отправка)."""
    logger.info("[%s] сработала cron-задача (%s)", query.id, query.schedule)
    result = await run_query(
        context.conn,
        context.cfg,
        query,
        sender=context.sender,
        summarizer=context.summarizer,
        # Принцип «может не находить, но не может молчать»: пустой раздел тоже
        # приходит сообщением с объяснением причины, а не тишиной.
        notify_empty=query.notify_empty,
        tz_name=context.tz_name,
        lock=context.lock,
        collect=context.collect,
        translator=getattr(context, "translator", None),
        trigger="по расписанию",
    )
    if result.status == "failed":
        logger.error("[%s] прогон по расписанию не доставлен: %s", query.id, result.error)
    return result


async def run_on_start(context: SchedulerContext) -> list[QueryResult]:
    """Один прогон по всем активным запросам сразу после старта процесса.

    Отличие от cron-задачи (`run_query_job`): там каждый раздел живёт сам по себе
    и отправляется своим сообщением, а здесь разделы собираются вместе и уходят
    ОДНИМ сообщением с общей шапкой и общим резюме.

    Причина в уведомлениях Telegram: несколько сообщений подряд он склеивает,
    и видимым остаётся только первое («новости»), из-за чего кажется, что других
    разделов нет. Поэтому здесь двухфазный порядок:
        1. по очереди собираем разделы (sender=None — run_query не отправляет);
        2. одной операцией отправляем всё, что нашли, и только потом отмечаем
           материалы отправленными.

    Неактивные запросы (/pause) пропускаем: автозапуск для них выключен.
    """
    active = [query for query in context.cfg.queries if is_query_active(context.conn, query.id)]
    results: list[QueryResult] = []

    for query in active:
        logger.info("[%s] прогон при старте (%s)", query.id, query.schedule)
        results.append(
            await run_query(
                context.conn,
                context.cfg,
                query,
                sender=None,                 # отправку делает run_on_start, а не run_query
                summarizer=context.summarizer,
                notify_empty=False,          # отчёты о пустоте разошлём ниже
                tz_name=context.tz_name,
                lock=context.lock,
                collect=context.collect,
                translator=context.translator,
                trigger="при старте сервиса",
            )
        )

    # В groups только разделы с материалами: пустые в дайджест не подмешиваются,
    # для них ниже уйдёт отдельный отчёт с причиной.
    groups = [
        (query, result.sections, result.item_ids)
        for query, result in zip(active, results)
        if result.has_items()
    ]
    if groups:
        messages_sent, attached, error = await deliver_sections(
            context.conn,
            groups,
            context.sender,
            tz_name=context.tz_name,
            cfg=context.cfg,
            summarizer=context.summarizer,
            rows_by_group=[result.rows for result in results if result.has_items()],
        )
        if error is not None:
            for query, result in zip(active, results):
                if result.has_items():
                    repo.log_digest(context.conn, query.id, query.tg_chat_id, 0, status="failed")
                    logger.error("[%s] прогон при старте не доставлен: %s", query.id, error)
        else:
            logger.info("прогон при старте: сообщений %s, материалов %s", messages_sent, attached)

    for query, result in zip(active, results):
        if result.is_empty() and query.notify_empty and context.sender is not None:
            try:
                await context.sender.send(query.tg_chat_id, result.empty_report)
                logger.info("[%s] отчёт о пустом разделе отправлен", query.id)
            except Exception as exc:  # noqa: BLE001 — отчёт не критичен для прогона
                logger.warning("[%s] не удалось сообщить о пустом разделе: %s", query.id, exc)

    return results


def pause_all(scheduler: AsyncIOScheduler | None, context: QueryStore) -> int:
    """Выключить автозапуск: is_active = 0 в БД и пауза задач."""
    changed = 0
    for query in context.cfg.queries:
        repo.set_query_active(context.conn, query.id, False)
        changed += 1
        if scheduler is not None:
            try:
                scheduler.pause_job(job_id(query.id))
            except Exception:  # noqa: BLE001 — задачи могло не быть, это не ошибка
                pass
    logger.info("автозапуск выключен для %s запросов", changed)
    return changed


def resume_all(scheduler: AsyncIOScheduler | None, context: QueryStore) -> int:
    """Включить автозапуск и заново поставить задачи (вдруг их не было)."""
    changed = 0
    for query in context.cfg.queries:
        repo.set_query_active(context.conn, query.id, True)
        changed += 1
    if scheduler is not None:
        register_jobs(scheduler, context)
    logger.info("автозапуск включён для %s запросов", changed)
    return changed


def next_runs(scheduler: AsyncIOScheduler, limit: int = 10) -> list[tuple[str, str]]:
    """Ближайшие срабатывания — для /status."""
    rows: list[tuple[str, str]] = []
    for job in scheduler.get_jobs():
        when = getattr(job, "next_run_time", None)
        rows.append((job.name or job.id, when.strftime("%d.%m.%Y %H:%M") if when else "—"))
    rows.sort(key=lambda item: (item[1] == "—", item[1]))
    return rows[:limit]
