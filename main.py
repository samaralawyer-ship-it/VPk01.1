"""Точка входа проекта «Агрегатор новостей и вакансий».

Текущий объём (этапы 1–2 ТЗ): загрузка и валидация конфигурации, инициализация
схемы SQLite, синхронизация подписок из queries.yaml в таблицу queries.
Коллекторы, дайджест, бот и планировщик подключаются на следующих этапах.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Консоль Windows по умолчанию в cp1251 и падает на эмодзи из реестра меток
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

try:
    from config import ConfigError, load_config  # noqa: E402
    from bot.sender import TelegramSender  # noqa: E402
    from bot.telegram_bot import BotContext, allowed_chat_ids, run_bot  # noqa: E402
    from database import connect, init_schema, repo  # noqa: E402
    from digest.llm import make_summarizer  # noqa: E402
    from digest.translate import make_translator  # noqa: E402
    from pipeline import collect_sources_sync, collection_lock, run_query  # noqa: E402
    from scheduler import (  # noqa: E402
        SchedulerContext,
        make_scheduler,
        next_runs,
        register_jobs,
        run_on_start,
    )
except ModuleNotFoundError as exc:  # зависимость не установлена в текущем интерпретаторе
    print(
        f"Не установлена зависимость: {exc.name or 'неизвестная'}.\n"
        "Похоже, что запущен интерпретатор, в который не поставлены зависимости проекта.\n"
        "Установите их (лучше в отдельном окружении):\n"
        "    python -m venv .venv\n"
        "    .venv\\Scripts\\activate           # Windows\n"
        "    source .venv/bin/activate         # Linux/macOS\n"
        "    pip install -r requirements.txt\n"
        "и запустите снова: python main.py\n"
        "Проверить, какой интерпретатор используется: python -c \"import sys; print(sys.executable)\"",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc

logger = logging.getLogger("main")


def load_env(env_file: str | Path | None = None) -> None:
    """Прочитать .env, если установлен python-dotenv. Отсутствие файла — не ошибка."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)


def setup_logging() -> None:
    """Логи в stdout (для Docker) и в файл с ротацией, как требует п. 13 ТЗ."""
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    root = logging.getLogger()
    root.setLevel(level)
    if root.handlers:                      # повторный вызов не должен дублировать хендлеры
        return

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    log_file = Path(os.environ.get("LOG_FILE", "logs/collector.log"))
    if not log_file.is_absolute():
        log_file = PROJECT_ROOT / log_file
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        rotating = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        rotating.setFormatter(formatter)
        root.addHandler(rotating)
    except OSError as exc:
        print(f"не удалось открыть лог-файл {log_file}: {exc}", file=sys.stderr)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Агрегатор новостей и вакансий: RSS/API → SQLite → дайджест в Telegram",
    )
    parser.add_argument("--config-dir", default=None, help="каталог с labels/sources/queries (по умолчанию ./config)")
    parser.add_argument("--db", default=None, help="путь к SQLite (по умолчанию DB_PATH из .env)")
    parser.add_argument(
        "--check",
        action="store_true",
        help="проверить конфигурацию и схему БД и выйти",
    )
    parser.add_argument(
        "--collect",
        action="store_true",
        help="выполнить сбор по включённым источникам (нормализация + дедупликация в БД)",
    )
    parser.add_argument(
        "--source",
        action="append",
        default=None,
        help="ограничить сбор конкретным источником; можно указать несколько раз",
    )
    parser.add_argument(
        "--digest",
        action="store_true",
        help="собрать и отправить дайджест (при DRY_RUN=1 — напечатать в консоль)",
    )
    parser.add_argument("--label", default=None, help="ограничить дайджест одной меткой")
    parser.add_argument(
        "--no-collect",
        action="store_true",
        help="для --digest: не ходить в источники, собрать дайджест из того, что уже в базе",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        help="переопределить глубину сбора в днях (по умолчанию — из queries.yaml)",
    )
    return parser.parse_args(argv)


def print_summary(cfg, conn, db_path: Path) -> None:
    print(f"Конфигурация: OK — метки: {len(cfg.labels)}, источники: {len(cfg.sources)} "
          f"(включено {len(cfg.enabled_sources)}), запросы: {len(cfg.queries)}")
    print(f"run_on_start: {cfg.run_on_start} | DRY_RUN: {os.environ.get('DRY_RUN', 'false')}")
    print(f"БД: {db_path} | записей в items: {repo.count_items(conn)}")
    print()

    by_label = repo.count_items_by_label(conn)
    print("Метки и источники:")
    for label in cfg.labels:
        sources = [src.id for src in cfg.enabled_sources if src.label == label.id]
        print(f"  {label.header:<28} источников: {len(sources):<2} записей: {by_label.get(label.id, 0)}")
        for source_id in sources:
            source = cfg.source(source_id)
            print(f"      - {source.id:<22} {source.type:<7} {source.url}")

    print()
    print("Запросы (подписки):")
    for query in cfg.queries:
        sources = cfg.sources_for_query(query)
        flags = []
        if query.summarize:
            flags.append("summarize")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        print(
            f"  {query.id:<20} метка: {query.label:<12} cron: {query.schedule:<12} "
            f"глубина: {query.lookback_days} дн.  лимит: {query.limit:<3} источников: {len(sources)}{suffix}"
        )


def select_sources(cfg, only: list[str] | None):
    """Источники для сбора: все включённые либо явно указанные через --source."""
    if not only:
        return list(cfg.enabled_sources)
    known = {src.id: src for src in cfg.sources}
    unknown = [source_id for source_id in only if source_id not in known]
    if unknown:
        raise ConfigError(f"неизвестные источники: {', '.join(unknown)}")
    return [known[source_id] for source_id in only]


def print_collection(results) -> int:
    """Таблица результатов сбора. Возвращает число источников с ошибкой."""
    header = (
        f"{'источник':<22}{'метка':<13}{'статус':<7}"
        f"{'fetched':>8}{'new':>6}{'dup':>6}{'filtered':>10}{'сек':>7}"
    )
    print(header)
    print("-" * len(header))

    errors = 0
    for result in results:
        if result.status != "ok":
            errors += 1
        print(
            f"{result.source_id:<22}{result.label:<13}{result.status:<7}"
            f"{result.fetched:>8}{result.new:>6}{result.dup:>6}{result.filtered:>10}{result.duration:>7.1f}"
        )
        if result.error:
            print(f"    └─ {result.error}")

    print()
    print(
        f"ИТОГО: источников {len(results)}, ошибок {errors}, "
        f"fetched {sum(r.fetched for r in results)}, new {sum(r.new for r in results)}, "
        f"dup {sum(r.dup for r in results)}, filtered {sum(r.filtered for r in results)}"
    )
    return errors


async def run_app(cfg, conn) -> int:
    """Сервисный режим: планировщик и бот в одном событийном цикле.

    Порядок как в ТЗ: инициализация БД и подписок (уже сделана) → регистрация задач →
    (опционально) один прогон при старте → бот принимает команды, планировщик работает рядом.
    """
    tz_name = os.environ.get("TZ")
    sender = TelegramSender()
    summarizer = make_summarizer()
    translator = make_translator()
    if translator is not None:
        translated_sources = [src.id for src in cfg.enabled_sources if src.translation]
        if translated_sources:
            logger.info("перевод включён для источников: %s", ", ".join(translated_sources))

    # Прогоны, оборванные прошлой остановкой, остались бы в статусе running навсегда
    stale = repo.close_dangling_runs(conn)
    if stale:
        logger.warning("закрыто незавершённых прогонов прошлого запуска: %s", stale)

    context = SchedulerContext(
        conn=conn,
        cfg=cfg,
        sender=sender,
        summarizer=summarizer,
        tz_name=tz_name,
        lock=collection_lock(),
        collect=True,
        translator=translator,
    )
    scheduler = make_scheduler(tz_name)
    registered = register_jobs(scheduler, context)
    scheduler.start()
    logger.info("планировщик запущен: задач %s, таймзона %s", registered, tz_name or "Europe/Moscow")
    for name, when in next_runs(scheduler, limit=10):
        logger.info("  следующее срабатывание: %s — %s", name, when)

    try:
        if cfg.run_on_start:
            logger.info("run_on_start: прогон по активным запросам сразу после старта")
            results = await run_on_start(context)
            for result in results:
                logger.info(
                    "  %s: %s, материалов %s%s",
                    result.query_id,
                    result.status,
                    result.items_sent,
                    f", ошибка: {result.error}" if result.error else "",
                )

        if sender.dry_run:
            logger.warning("DRY_RUN=1: бот не запускается, дайджесты печатаются в консоль. Ctrl+C — выход")
            await asyncio.Event().wait()
        elif not sender.token:
            logger.error("не задан TELEGRAM_BOT_TOKEN — бот не запускается, планировщик работает")
            await asyncio.Event().wait()
        else:
            bot_context = BotContext(
                conn=conn,
                cfg=cfg,
                sender=sender,
                summarizer=summarizer,
                allowed_chat_ids=allowed_chat_ids(),
                scheduler=scheduler,
                timezone=tz_name,
                lock=collection_lock(),
                translator=translator,
            )
            logger.info("бот запущен, ожидаю команды")
            await run_bot(bot_context, sender.token)
    finally:
        scheduler.shutdown(wait=False)
        await sender.close()
        logger.info("сервис остановлен")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_env()
    setup_logging()

    try:
        cfg = load_config(args.config_dir or PROJECT_ROOT / "config")
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    db_setting = args.db or os.environ.get("DB_PATH", "data/aggregator.db")
    db_path = Path(db_setting)
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path

    conn = connect(db_path)
    try:
        init_schema(conn)
        for query in cfg.queries:
            repo.save_query(conn, query)

        if args.check:
            print_summary(cfg, conn, db_path)
            print()
            print("Проверка пройдена: конфигурация валидна, схема БД на месте.")
            return 0

        if args.collect:
            try:
                targets = select_sources(cfg, args.source)
            except ConfigError as exc:
                print(exc, file=sys.stderr)
                return 2

            depth = args.lookback_days if args.lookback_days else "из queries.yaml"
            print(f"Сбор: источников {len(targets)}, глубина: {depth}")
            print()
            results = collect_sources_sync(
                conn, cfg, targets, lookback_days=args.lookback_days, translator=make_translator()
            )
            errors = print_collection(results)
            print(f"Записей в БД items: {repo.count_items(conn)}")
            for label in cfg.labels:
                count = repo.count_items(conn, label.id)
                print(f"  {label.header}: {count}")
            return 1 if errors else 0

        if args.digest:
            queries = [query for query in cfg.queries if not args.label or query.label == args.label]
            if not queries:
                print(f"Нет запросов для метки {args.label}", file=sys.stderr)
                return 2

            sender = TelegramSender()
            summarizer = make_summarizer()
            translator = make_translator()
            tz_name = os.environ.get("TZ")

            async def run_all():
                try:
                    return [
                        await run_query(
                            conn,
                            cfg,
                            query,
                            sender=sender,
                            summarizer=summarizer,
                            tz_name=tz_name,
                            collect=not args.no_collect,
                            dry_run=sender.dry_run,
                            translator=translator,
                        )
                        for query in queries
                    ]
                finally:
                    await sender.close()

            results = asyncio.run(run_all())
            for result in results:
                line = (
                    f"{result.query_id:<20} {result.status:<7} материалов {result.items_sent:<4} "
                    f"сообщений {result.messages_sent}"
                )
                if result.error:
                    line += f" — {result.error}"
                print(line)
            print(f"\nЗаписей в БД items: {repo.count_items(conn)}")
            return 1 if any(result.status == "failed" for result in results) else 0

        print_summary(cfg, conn, db_path)
        print()
        print(
            "Доступные режимы: --check (проверка), --collect (сбор), --digest (дайджест).\n"
            "Без флагов запускается сервис: планировщик и бот в одном процессе."
        )
        print()
        return asyncio.run(run_app(cfg, conn))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
