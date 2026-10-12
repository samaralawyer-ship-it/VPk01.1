"""Сбор: источник → нормализация → дедупликация → БД + run_log.

Стадия ничего не знает про расписание и Telegram: её вызывают и ручной прогон
(`main.py --collect`), и планировщик на этапе 10. Ошибка одного источника не
прерывает остальные — она логируется и попадает в run_log.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

import httpx

from collectors import factory, normalize
from config.loader import AppConfig, Query, Source
from database import db, repo
from digest import builder, translate

logger = logging.getLogger(__name__)

# Один сбор на процесс: ручной /digest и cron не должны идти одновременно
_COLLECTION_LOCK = asyncio.Lock()


def collection_lock() -> asyncio.Lock:
    return _COLLECTION_LOCK


@dataclass
class SourceResult:
    source_id: str
    label: str
    status: str                      # ok | error
    fetched: int = 0
    new: int = 0
    dup: int = 0
    filtered: int = 0
    candidates: int = 0              # прошли жёсткие item_filters источника и дошли до БД
    duration: float = 0.0
    error: str | None = None


@dataclass
class CollectionStats:
    """Что произошло на каждом этапе отбора — основа для отчёта «почему пусто»."""

    results: list[SourceResult] = field(default_factory=list)
    candidates: int = 0              # дошли до БД после item_filters источника
    hard_filtered: int = 0           # отсеяны фильтрами источника (зарплата, регион)
    new: int = 0                     # реально записано в БД
    duplicates: int = 0              # уже были в базе
    errors: list[SourceResult] = field(default_factory=list)

    @classmethod
    def from_results(cls, results: Sequence[SourceResult]) -> "CollectionStats":
        return cls(
            results=list(results),
            candidates=sum(result.candidates for result in results),
            hard_filtered=sum(result.filtered for result in results),
            new=sum(result.new for result in results),
            duplicates=sum(result.dup for result in results),
            errors=[result for result in results if result.status != "ok"],
        )

    @property
    def failed_sources(self) -> list[str]:
        return [result.source_id for result in self.errors]

    @property
    def ok(self) -> bool:
        return not self.errors


def lookback_for_source(cfg: AppConfig, source: Source) -> int:
    """Глубина для источника — максимум по запросам, которые его используют."""
    values = [query.lookback_days for query in cfg.queries if source in cfg.sources_for_query(query)]
    return max(values) if values else 1


async def collect_source(
    conn,
    cfg: AppConfig,
    source: Source,
    *,
    lookback_days: int | None = None,
    client: httpx.AsyncClient | None = None,
    translator=None,
) -> SourceResult:
    """Собрать один источник: загрузка → нормализация → перевод → дедупликация → БД.

    Порядок шагов важен, и вот почему именно такой:

        1. коллектор качает материалы и отсеивает их по item_filters источника;
        2. build_item превращает сырьё в записи items и считает dedup_hash
           ПО ОРИГИНАЛУ — не по переводу. Иначе один и тот же материал в двух
           прогонах получил бы разные хеши (перевод каждый раз чуть разный)
           и записался бы в базу дважды;
        3. перевод делается только для тех хешей, которых в базе ещё нет:
           перевод платный, а повторов на каждом прогоне большинство
           (у Hacker News это ~1–5 новых из 30 полученных);
        4. save_items вставляет всё пачкой, а ON CONFLICT(dedup_hash) отсеивает
           то, что могло появиться между шагами 3 и 4 (например, параллельный прогон).

    Ошибка любого шага не выходит наружу: она пишется в run_log и возвращается
    в SourceResult со статусом error, чтобы остальные источники продолжили сбор.
    """
    started = time.monotonic()
    depth = lookback_days or lookback_for_source(cfg, source)
    run_id = repo.log_run_start(conn, source.id)

    try:
        collector = factory.get_collector(source, lookback_days=depth, client=client)
        batch = await collector.collect()
        items = [normalize.build_item(raw, source.id, source.label, cfg.dedup) for raw in batch.raw_items]

        # Перевод — только для новых материалов и только после дедупликации:
        # перевод платный, а повторов на каждом прогоне большинство.
        translated = 0
        if source.translation and translator is not None and items:
            known = repo.existing_dedup_hashes(conn, [item.dedup_hash for item in items])
            fresh = [item for item in items if item.dedup_hash not in known]
            if fresh:
                translated = await translate.translate_items(fresh, source.translation, translator)

        new, dup = repo.save_items(conn, items)
        repo.log_run_finish(conn, run_id, "ok", items_new=new, items_dup=dup, items_filtered=batch.filtered)

        duration = time.monotonic() - started
        logger.info(
            "[%s] fetched=%s new=%s dup=%s filtered=%s%s за %.1f с",
            source.id,
            batch.fetched,
            new,
            dup,
            batch.filtered,
            f" переведено={translated}" if translated else "",
            duration,
        )
        return SourceResult(
            source_id=source.id,
            label=source.label,
            status="ok",
            fetched=batch.fetched,
            new=new,
            dup=dup,
            filtered=batch.filtered,
            candidates=len(batch.raw_items),
            duration=duration,
        )
    except Exception as exc:  # noqa: BLE001 — изоляция: падение источника не роняет прогон
        message = f"{type(exc).__name__}: {exc}"[:500]
        repo.log_run_finish(conn, run_id, "error", error=message)
        logger.error("[%s] ошибка сбора: %s", source.id, message)
        return SourceResult(
            source_id=source.id,
            label=source.label,
            status="error",
            duration=time.monotonic() - started,
            error=message,
        )


async def collect_sources(
    conn,
    cfg: AppConfig,
    sources: Sequence[Source],
    *,
    lookback_days: int | None = None,
    client: httpx.AsyncClient | None = None,
    translator=None,
) -> list[SourceResult]:
    """Источники обходим последовательно — так вежливее к лимитам источников."""
    results: list[SourceResult] = []
    for source in sources:
        results.append(
            await collect_source(
                conn, cfg, source, lookback_days=lookback_days, client=client, translator=translator
            )
        )
    return results


def collect_sources_sync(
    conn,
    cfg: AppConfig,
    sources: Sequence[Source] | None = None,
    *,
    lookback_days: int | None = None,
    client: httpx.AsyncClient | None = None,
    translator=None,
) -> list[SourceResult]:
    targets = list(sources if sources is not None else cfg.enabled_sources)
    return asyncio.run(
        collect_sources(conn, cfg, targets, lookback_days=lookback_days, client=client, translator=translator)
    )


# --------------------------------------------------------------------------- #
# Полный цикл запроса: сбор → дайджест → отправка → отметки в БД


@dataclass
class QueryResult:
    """Итог одного прогона подписки: что собрали, что отправили, почему пусто.

    Поля делятся на три смысловые группы:
      * отчёт для лога и бота: status, error, empty_report, collected;
      * что отправили: items_sent, messages_sent, digest_id;
      * материал для общей отправки: sections и rows (нужны, когда один прогон
        собирает несколько разделов и они уходят одним сообщением).
    """

    query_id: str
    label: str
    status: str                                   # sent | empty | failed | busy
    collected: list[SourceResult] = field(default_factory=list)
    items_sent: int = 0
    messages_sent: int = 0
    digest_id: int | None = None
    error: str | None = None
    messages: list[str] = field(default_factory=list)
    empty_report: str | None = None                # объяснение «почему пусто», для бота и лога
    item_ids: list[int] = field(default_factory=list)   # что нужно отметить отправленным
    sections: list[Any] = field(default_factory=list)   # разделы для общей сборки дайджеста
    rows: list[Any] = field(default_factory=list)       # материалы разделов: нужны для общего резюме

    def is_empty(self) -> bool:
        return self.status == "empty"

    def has_items(self) -> bool:
        # Именно по item_ids, а не по items_sent: при общей отправке счётчик
        # заполняется после доставки, а решение «включать раздел в сообщение»
        # принимается до неё.
        return bool(self.item_ids)


# --------------------------------------------------------------------------- #
# Общая сборка и доставка: все разделы одного прогона — одним сообщением


def mark_sections_sent(
    conn,
    cfg: AppConfig,
    groups: Sequence[tuple[Any, Sequence[Any], Sequence[int]]],
    messages_sent: int,
) -> int:
    """Отметить материалы доставленных разделов: запись в digest_log и digest_items.

    Вызывается ТОЛЬКО после успешной отправки: сбой доставки не должен «съедать»
    материалы, иначе они не попадут в следующий дайджест.
    """
    attached = 0
    for query, sections, item_ids in groups:
        if not item_ids:
            continue
        digest_id = repo.log_digest(conn, query.id, query.tg_chat_id, len(item_ids))
        attached += repo.attach_digest_items(conn, digest_id, list(item_ids))
        repo.update_last_run(conn, query.id)
    logger.info("дайджест отправлен: сообщений %s, материалов зафиксировано %s", messages_sent, attached)
    return attached


async def batch_summary(
    summarizer,
    cfg: AppConfig,
    groups: Sequence[tuple[Any, Sequence[Any], Sequence[int]]],
    rows_by_group: Sequence[Sequence[Any]] | None = None,
) -> str | None:
    """Одно общее резюме по всем разделам дайджеста.

    Резюме считается по всему дайджесту, а не по каждому разделу: иначе в шапке
    оказывается пересказ только новостей, и выглядит это как «дайджест по новостям».
    """
    if summarizer is None:
        return None
    sections: dict[str, list[Any]] = {}
    for index, (query, group_sections, _item_ids) in enumerate(groups):
        rows: list[Any] = []
        if rows_by_group and index < len(rows_by_group) and rows_by_group[index]:
            rows = list(rows_by_group[index])
        else:                                   # запасной путь: материалы лежат в разделах
            for section in group_sections:
                rows.extend(list(getattr(section, "rows", []) or []))
        if not rows:
            continue
        sections[cfg.label(query.label).header] = rows

    if not sections:
        return None
    if len(sections) == 1:
        return await summarizer(next(iter(sections.values())))

    summarize_sections = getattr(summarizer, "summarize_sections", None)
    if summarize_sections is None:                    # произвольный вызываемый объект
        return await summarizer([row for rows in sections.values() for row in rows])
    return await summarize_sections(sections)


async def deliver_sections(
    conn,
    groups: Sequence[tuple[Any, Sequence[Any], Sequence[int]]],
    sender,
    *,
    dry_run: bool = False,
    tz_name: str | None = None,
    cfg: AppConfig | None = None,
    summarizer=None,
    rows_by_group: Sequence[Sequence[Any]] | None = None,
) -> tuple[int, int, str | None]:
    """Собрать разделы одного чата в одно сообщение и отправить его.

    Одно сообщение на прогон: когда разделы уходят четырьмя сообщениями подряд,
    Telegram склеивает уведомления, и видимым остаётся только первый — «новости».

    groups — список «раздел к отправке»: (подписка, её разделы, id материалов).
    rows_by_group — те же материалы как строки БД: нужны для общего резюме.

    Возвращает (сообщений, зафиксировано материалов, текст ошибки). Ошибка
    возвращается строкой, а не исключением: вызывающий код решает, что писать
    в digest_log, и продолжает работу.
    """
    chat_id = str(groups[0][0].tg_chat_id)
    summary = await batch_summary(summarizer, cfg, groups, rows_by_group)
    messages = build_batch_messages(cfg, groups, tz_name, summary=summary)

    if dry_run or sender is None:
        # Предпросмотр: печатаем ровно то, что ушло бы в чат, и ничего не отмечаем.
        for message in messages:
            print(message)
            print()
        return 0, 0, None

    try:
        messages_sent = await sender.send_many(chat_id, messages)
    except Exception as exc:  # noqa: BLE001 — сбой доставки фиксируем, материалы не теряем
        message = f"{type(exc).__name__}: {exc}"[:500]
        logger.error("отправка дайджеста не удалась: %s", message)
        return 0, 0, message

    # Отмечаем отправленным ТОЛЬКО после успешной доставки.
    attached = mark_sections_sent(conn, cfg, groups, messages_sent)
    return messages_sent, attached, None


def split_rendered_message(message: str) -> list[str]:
    """Разбить готовое сообщение раздела: [заголовок, блок материала, блок, ...].

    Не режем по «\\n\\n» вслепую: блок материала сам содержит переводы строк,
    а многострочное описание может дать разделитель внутри блока. Поэтому делим
    по границам перед нумерованным элементом «N. » — блоки остаются целыми,
    а нумерация из рендера сохраняется.

    Пример: сообщение с заголовком и двумя материалами даст три части:
    заголовок, «1. …», «2. …».
    """
    lines = message.split("\n")
    parts: list[str] = []
    current: list[str] = []

    for index, line in enumerate(lines):
        # Строка вида «12. Заголовок материала» начинает новый блок.
        is_element = re.match(r"^\d+\. ", line) is not None
        if index == 0 or (is_element and current):
            if current:
                parts.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        parts.append("\n".join(current).strip())
    return parts


def fits_limit(text: str, part: str, limit: int) -> bool:
    """Поместится ли part в text, не разорвав HTML-разметку на границе.

    Простое сравнение длин не годится: Telegram с parse_mode=HTML отвергает
    сообщение с оборванным тегом, поэтому на границе проверяем ещё и разметку.
    """
    unused = limit - len(text) - 2                 # −2 на разделитель «\\n\\n»
    if unused <= 0:
        return False
    if unused >= len(part):
        return True
    # Короткий запас: разрыв «<i>описание…</i>» целиком допустим (это целый блок,
    # не влезший в лимит), но разрезанный тег Telegram не примет.
    return cut_is_safe(part[:unused])


def cut_is_safe(head: str) -> bool:
    """Не остаётся ли на границе незакрытый тег (для parse_mode=HTML).

    Считаем глубину вложенности: <b> увеличивает, </b> уменьшает. Если к концу
    проверяемого куска глубина не ноль — значит тег остался открытым и Telegram
    такое сообщение отклонит. Оборванный «<b» без «>» — тоже отказ.
    """
    depth = 0
    index = head.find("<")
    while index != -1:
        end = head.find(">", index)
        if end == -1:
            return False                     # тег оборван
        if head[index + 1 : index + 2] == "/":
            depth = max(0, depth - 1)        # закрывающий тег
        else:
            depth += 1                       # открывающий тег
        index = head.find("<", end)
    return depth == 0


def strip_section_header(parts: list[str], section: Any) -> list[str]:
    """Убрать из частей раздела его собственный заголовок — он заменяется общим.

    Заголовок рендера выглядит как «📰 Новости отрасли — 12.10.2026 (3)».
    В общем дайджесте заголовок ставит build_batch_messages, поэтому свой надо
    снять, иначе он печатался дважды (это была реальная ошибка вёрстки).

    Блок саммари (если он есть) сохраняем: это часть содержимого раздела.
    """
    if not parts:
        return parts
    head = parts[0]
    first_line = head.split("\n", 1)[0]
    looks_like_header = first_line.startswith(section.label.header) and first_line.rstrip().endswith(")")
    if not looks_like_header:
        return parts
    rest = [line for line in head.split("\n")[1:] if line.strip()]
    # «(3)» в конце означает, что после заголовка идёт либо первый материал,
    # либо текст саммари — и то и другое возвращаем как содержимое раздела.
    return [*rest, *parts[1:]]


def build_batch_messages(
    cfg: AppConfig,
    groups: Sequence[tuple[Any, Sequence[Any], Sequence[int]]],
    tz_name: str | None = None,
    *,
    summary: str | None = None,
) -> list[str]:
    """Одно сообщение со всеми разделами; при нехватке места — продолжения по границам.

    Собираем «плотно»: каждый раздел начинается со своего заголовка вплотную к
    предыдущему. Так разделы укладываются в одно сообщение и уведомление в Telegram
    не схлопывается в один раздел. Лишние пустые строки тут были реальной ошибкой:
    они съедали запас лимита и дайджест без нужды рвался на два сообщения.

    Два правила, за которые отвечает функция:
      * заголовок раздела никогда не остаётся в конце сообщения без материалов
        (иначе видно «🏢 Публикации конкурентов — 3» и ничего под ним);
      * сообщение не режется внутри HTML-тега.

    summary — общее резюме по всем разделам: ставится в шапку, до первого раздела,
    чтобы не читалось как резюме только новостей.
    """
    head = f"🗓 <b>Дайджест за {builder.today(tz_name)}</b>"
    if summary:
        # Резюме приходит от LLM, то есть это чужой текст: экранируем.
        head = f"{head}\n\n<i>{html.escape(summary)}</i>"
    chunks: list[str] = [head]
    limit = builder.SAFE_LIMIT

    for query, sections, item_ids in groups:
        for section in sections:
            if not section.item_ids:
                continue                               # пустой раздел в дайджест не попадает
            parts = [
                part
                for message in section.messages
                for part in split_rendered_message(message)
            ]
            parts = strip_section_header(parts, section)
            if not parts:
                continue

            header = f"<b>{section.label.header}</b> — {len(section.item_ids)}"
            current = f"{chunks[-1]}\n\n{header}"
            if not fits_limit(current, parts[0], limit) and chunks[-1] != chunks[0]:
                # Заголовок переносим вместе с первым материалом, чтобы не оторвать его.
                # На первом сообщении так делать нельзя: там шапка с датой, и раздел
                # обязан начинаться в ней же, иначе дайджест выглядит пустым.
                chunks.append(f"{header}\n\n{parts[0]}")
                parts = parts[1:]
            else:
                chunks[-1] = current

            for part in parts:
                if not fits_limit(chunks[-1], part, limit):
                    chunks.append(part)
                else:
                    chunks[-1] = f"{chunks[-1]}\n\n{part}"

    return chunks


def describe_filters(filters) -> str:
    """Человекочитаемый список фильтров: «зарплата, регион»."""
    names = {"salary_min": "зарплата", "salary_max": "зарплата", "salary_currency": "валюта",
             "region": "регион", "include_unspecified_salary": "зарплата"}
    seen: list[str] = []
    for key in (filters or {}):
        human = names.get(key)
        if human and human not in seen:
            seen.append(human)
    return ", ".join(seen)


def describe_keywords(keywords) -> str:
    """Короткое описание тематического отбора: сколько ключей и какие (до трёх)."""
    if not keywords:
        return ""
    any_of = keywords.get("any") or []
    if not any_of:
        return ""
    shown = [item if isinstance(item, str) else " + ".join(str(part) for part in item) for item in any_of[:3]]
    tail = f" и ещё {len(any_of) - 3}" if len(any_of) > 3 else ""
    return ", ".join(shown) + tail


def lookback_hint(conn, cfg: AppConfig, query: Query) -> str | None:
    """Если в окне пусто, но в базе есть более старые материалы метки — подсказываем окно.

    Это ответ на вопрос «почему ничего нет, если в базе сотни материалов»: они есть,
    но лежат глубже `lookback_days`. Без такой подсказки ситуация выглядит как сбой.
    """
    cutoff = db.days_ago_iso(query.lookback_days)
    row = conn.execute(
        "SELECT count(*) AS n, min(created_at) AS oldest FROM items WHERE label = ? AND created_at < ?",
        (query.label, cutoff),
    ).fetchone()
    if not row or not row["n"]:
        return None
    oldest = str(row["oldest"] or "")[:10]
    return (
        f"Глубже окна ({query.lookback_days} дн.) в базе лежит материалов: {row['n']}"
        + (f", самый старый — {oldest}" if oldest else "")
    )


def sources_phrase(count: int) -> str:
    """«в 1 источнике», «в 2 источниках» — без «1 источниках»."""
    if count % 10 == 1 and count % 100 != 11:
        return f"в {count} источнике"
    return f"в {count} источниках"


def report_stamp(trigger: str | None, tz_name: str | None) -> str:
    """Строка «когда и почему» для отчёта: время прогона и его повод.

    Зачем: отчёты об одном разделе выглядят идентично, если в них нет времени
    и источника. Два отчёта из разных часов с одними и теми же счётчиками
    читаются как дубль, хотя это два честных прогона. Строка их различает.
    """
    try:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(tz_name) if tz_name else None
        moment = datetime.now(zone) if zone else datetime.now()
    except Exception:  # noqa: BLE001 — неизвестная таймзона не повод ломать отчёт
        moment = datetime.now()

    stamp = moment.strftime("%d.%m.%Y %H:%M")
    return f"🕒 {stamp} — {trigger}" if trigger else f"🕒 {stamp}"


def build_empty_report(
    conn,
    cfg: AppConfig,
    query: Query,
    stats: CollectionStats,
    *,
    trigger: str | None = None,
    tz_name: str | None = None,
) -> str:
    """Почему раздел пуст. Принцип: можем не найти — но не можем молчать.

    Отчёт собирается из статистики каждого этапа отбора, потому что «нового нет»
    бывает по совершенно разным причинам, и без разбора они неотличимы:

        * источник упал — часть данных могла не дойти (это не «нет новостей»);
        * материалы есть, но их отсеяли item_filters источника (зарплата, регион);
        * всё, что пришло, уже лежит в базе — повторы;
        * материалы новые, но не совпали с keywords раздела (именно так молча
          пропадали вакансии: ключ «промпт» не встречался ни в одной);
        * материалов нет вообще за это окно.

    Строки добавляются только те, что подтверждаются данными: отчёт не должен
    объяснять пустоту догадками. `trigger` — повод прогона («по команде /digest»,
    «по расписанию»), он попадает в строку со временем.
    """
    label = cfg.label(query.label)
    lines = [
        f"<b>{label.header}</b> — сегодня нового нет",
        f"<i>{report_stamp(trigger, tz_name)}</i>",
    ]

    if not stats.ok:
        lines.append(
            "Источники с ошибкой: " + ", ".join(stats.failed_sources)
            + ". Часть данных могла не дойти."
        )

    if stats.results:
        # candidates + hard_filtered = всё, что источник вернул до своих фильтров.
        lines.append(
            f"Проверено: материалов {stats.candidates + stats.hard_filtered}, "
            f"{sources_phrase(len(stats.results))}, записано в базу {stats.new}, "
            f"повторов {stats.duplicates}"
        )

    filtered = [
        f"{cfg.source(result.source_id).name} ({result.filtered})"
        for result in stats.results
        if result.status == "ok" and result.filtered
    ]
    reasons = [f"фильтры источника: {describe_filters(cfg.source(result.source_id).item_filters)}"
               for result in stats.results
               if result.status == "ok" and result.filtered]
    if filtered:
        lines.append(f"Отсеяно при сборе: {', '.join(filtered)} — {reasons[0]}")

    if stats.new == 0 and stats.duplicates:
        lines.append(f"Все материалы уже были в базе (повторов {stats.duplicates})")
    elif stats.new == 0 and stats.candidates == 0 and stats.hard_filtered == 0 and stats.ok:
        lines.append("Источники не вернули ни одного материала за это окно")

    words = describe_keywords(query.keywords)
    if words:
        lines.append(f"Ключевые слова раздела: {words}")
        if stats.new:
            lines.append(f"Ни один из {stats.new} новых материалов не совпал с ключами")

    hint = lookback_hint(conn, cfg, query)
    if hint:
        lines.append(hint)

    lines.append(f"Окно проверки: {query.lookback_days} дн.")
    return "\n".join(lines)


async def run_query(
    conn,
    cfg: AppConfig,
    query: Query,
    *,
    sender=None,
    summarizer=None,
    notify_empty: bool = False,
    dry_run: bool = False,
    collect: bool = True,
    tz_name: str | None = None,
    lock: asyncio.Lock | None = None,
    translator=None,
    trigger: str | None = None,
) -> QueryResult:
    """Прогон одного запроса.

    Материалы помечаются отправленными и last_run обновляется ТОЛЬКО после реальной
    доставки: сбой отправки не должен «съедать» материалы навсегда.
    Режим предпросмотра (DRY_RUN или отсутствие отправителя) печатает дайджест и
    ничего не расходует — иначе предпросмотр забрал бы материалы у следующего прогона.
    """
    gate = lock or _COLLECTION_LOCK
    if gate.locked():
        return QueryResult(query_id=query.id, label=query.label, status="busy", error="сбор уже идёт")

    async with gate:
        collected: list[SourceResult] = []
        if collect:
            collected = await collect_sources(conn, cfg, cfg.sources_for_query(query), translator=translator)
        stats = CollectionStats.from_results(collected)

        digest = await builder.build_digest(
            conn,
            cfg,
            query,
            tz_name=tz_name,
        )
        preview = dry_run or sender is None

        if digest.is_empty():
            report = build_empty_report(conn, cfg, query, stats, trigger=trigger, tz_name=tz_name)
            if notify_empty and sender is not None and not preview:
                try:
                    await sender.send(query.tg_chat_id, report)
                    logger.info("[%s] отчёт о пустом разделе отправлен", query.id)
                except Exception as exc:  # noqa: BLE001 — сообщение о пустоте не критично
                    logger.warning("[%s] не удалось сообщить о пустом дайджесте: %s", query.id, exc)
            if not preview:
                repo.update_last_run(conn, query.id)
            logger.info("[%s] новых материалов нет", query.id)
            return QueryResult(
                query_id=query.id,
                label=query.label,
                status="empty",
                collected=collected,
                messages=digest.messages,
                empty_report=report,
                rows=digest.rows,
            )

        if preview:
            if dry_run:
                repo.log_digest(conn, query.id, query.tg_chat_id, len(digest.item_ids), status="dry_run")
            if sender is None:
                # sender=None означает «собирает вызывающий»: run_on_start так копит
                # разделы, чтобы отправить их одним сообщением. Предпросмотр — это dry_run.
                logger.info("[%s] дайджест собран: материалов %s", query.id, len(digest.item_ids))
            else:
                await deliver_sections(
                    conn,
                    [(query, digest.sections, digest.item_ids)],
                    sender,
                    dry_run=True,
                    tz_name=tz_name,
                    cfg=cfg,
                    summarizer=summarizer if query.summarize else None,
                    rows_by_group=[digest.rows],
                )
                logger.info(
                    "[%s] предпросмотр: материалов %s (ничего не отправлено и не израсходовано)",
                    query.id,
                    len(digest.item_ids),
                )
            return QueryResult(
                query_id=query.id,
                label=query.label,
                status="preview",
                collected=collected,
                digest_id=repo.last_digest_id(conn, query.id) if dry_run else None,
                messages=digest.messages,
                item_ids=digest.item_ids,
                sections=digest.sections,
                rows=digest.rows,
            )

        messages_sent, attached, error = await deliver_sections(
            conn,
            [(query, digest.sections, digest.item_ids)],
            sender,
            dry_run=dry_run,
            tz_name=tz_name,
            cfg=cfg,
            summarizer=summarizer if query.summarize else None,
            rows_by_group=[digest.rows],
        )
        if error is not None:
            digest_id = repo.log_digest(conn, query.id, query.tg_chat_id, 0, status="failed")
            logger.error("[%s] отправка не удалась: %s", query.id, error)
            return QueryResult(
                query_id=query.id,
                label=query.label,
                status="failed",
                collected=collected,
                digest_id=digest_id,
                error=error,
                messages=digest.messages,
                item_ids=digest.item_ids,
                sections=digest.sections,
                rows=digest.rows,
            )

        logger.info(
            "[%s] дайджест отправлен: материалов %s, сообщений %s, зафиксировано %s",
            query.id,
            len(digest.item_ids),
            messages_sent,
            attached,
        )
        return QueryResult(
            query_id=query.id,
            label=query.label,
            status="sent",
            collected=collected,
            items_sent=len(digest.item_ids),
            messages_sent=messages_sent,
            digest_id=repo.last_digest_id(conn, query.id),
            messages=digest.messages,
            item_ids=digest.item_ids,
            sections=digest.sections,
            rows=digest.rows,
        )


async def run_queries(
    conn,
    cfg: AppConfig,
    queries: Sequence[Query] | None = None,
    **kwargs,
) -> list[QueryResult]:
    results: list[QueryResult] = []
    for query in queries if queries is not None else cfg.queries:
        results.append(await run_query(conn, cfg, query, **kwargs))
    return results
