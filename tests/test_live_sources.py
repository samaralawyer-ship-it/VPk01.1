"""Реальные интеграционные тесты: настоящие источники, без моков и фикстур.

Запуск: pytest -m live        (нужен доступ в интернет и .env с TG_CHAT_ID)
По умолчанию эти тесты исключены из прогона (см. pytest.ini).
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest
from dotenv import load_dotenv

from bot.sender import SenderError, TelegramSender
from config import ConfigError, load_config
from database import connect, init_schema, repo
from pipeline import collect_source, collect_sources

pytestmark = pytest.mark.live

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def cfg():
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    try:
        return load_config(PROJECT_ROOT / "config")
    except ConfigError as exc:
        pytest.skip(f"нет пригодной конфигурации: {exc}")


@pytest.fixture
def conn(workdir):
    connection = connect(workdir / "live.db")
    init_schema(connection)
    yield connection
    connection.close()


async def test_every_enabled_source_responds(cfg, conn):
    """Каждый включённый источник должен отдать хотя бы один материал."""
    failures: list[str] = []
    report: list[str] = []

    for source in cfg.enabled_sources:
        result = await collect_source(conn, cfg, source)
        report.append(
            f"  {source.id:<22}{result.status:<7}fetched={result.fetched:<4}"
            f"new={result.new:<4}dup={result.dup:<4}filtered={result.filtered:<4}"
            f"{result.error or ''}"
        )
        if result.status != "ok" or result.fetched < 1:
            failures.append(f"{source.id}: status={result.status}, fetched={result.fetched}, {result.error or ''}")

    print("\nЖивой прогон по источникам:\n" + "\n".join(report))
    assert not failures, "источники не ответили:\n" + "\n".join(failures)


async def test_repeat_collection_adds_nothing(cfg, conn):
    """Повторный сбор того же среза не должен давать новые материалы."""
    source = next(src for src in cfg.enabled_sources if src.id == "habr")

    first = await collect_source(conn, cfg, source)
    assert first.status == "ok" and first.fetched >= 1, first.error

    second = await collect_source(conn, cfg, source)
    assert second.status == "ok", second.error
    assert second.new == 0, f"второй сбор добавил {second.new} материалов — дедупликация не работает"
    assert repo.count_items(conn, source.label) == first.new


async def test_broken_source_does_not_break_others(cfg, conn):
    """Ошибка одного источника изолирована: остальные собираются."""
    good = next(src for src in cfg.enabled_sources if src.id == "habr")
    broken = replace(good, id="broken_source", url="https://this-host-does-not-exist.invalid/rss")

    results = await collect_sources(conn, cfg, [broken, good])
    by_id = {result.source_id: result for result in results}

    assert by_id["broken_source"].status == "error"
    assert by_id["broken_source"].error
    assert by_id["habr"].status == "ok"
    assert repo.count_items(conn) >= 1


async def test_run_log_is_filled(cfg, conn):
    source = next(src for src in cfg.enabled_sources if src.id == "habr")
    await collect_source(conn, cfg, source)

    last = repo.last_run(conn)
    assert last["source"] == "habr"
    assert last["status"] == "ok"
    assert last["started_at"].endswith("+00:00")
    assert last["finished_at"].endswith("+00:00")


@pytest.mark.live
@pytest.mark.live_tg
async def test_telegram_delivery_works(cfg):
    """Реальная отправка в Telegram. Пропускается, если нет токена или сеть блокирует API."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if not token or not chat_id:
        pytest.skip("нет TELEGRAM_BOT_TOKEN или TG_CHAT_ID")

    sender = TelegramSender(token=token, env=os.environ, dry_run=False)
    try:
        try:
            parts = await sender.send(chat_id, "🧪 <b>Тест</b> доставки от агрегатора — подключение работает.")
        except SenderError as exc:
            if "сеть недоступна" in str(exc):
                pytest.skip(f"api.telegram.org недоступен из этой сети: {exc}")
            raise
    finally:
        await sender.close()

    assert parts == 1
