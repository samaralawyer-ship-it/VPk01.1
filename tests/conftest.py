"""Общие фикстуры тестов."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # страховка, если pytest.ini не подхватился
    sys.path.insert(0, str(PROJECT_ROOT))

WORK_ROOT = PROJECT_ROOT / ".work"


@pytest.fixture
def workdir(request):
    """Каталог для временных файлов теста.

    Не используем pytest tmp_path: в этом окружении каталог, созданный самим
    pytest в системном %TEMP%, оказывается недоступен процессу. .work лежит
    внутри проекта, поэтому работает везде и легко чистится.
    """
    safe_name = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in request.node.name)[:70]
    path = WORK_ROOT / f"{safe_name}-{os.getpid()}"
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)

TEST_ENV = {
    "TG_CHAT_ID": "123456789",
    "TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN",
    "GITHUB_TOKEN": "ghp_test",
    "DEEPSEEK_API_KEY": "sk-test",
}

MIN_LABEL = {"id": "news", "title": "Новости", "emoji": "📰", "order": 1}

MIN_SOURCE = {
    "id": "tass_news",
    "name": "ТАСС",
    "label": "news",
    "type": "rss",
    "url": "https://tass.ru/rss/v2.xml",
    "enabled": True,
    "fields": {
        "title": "title",
        "url": "link",
        "description": "summary",
        "created_at": "published_parsed",
    },
}

MIN_QUERY = {
    "id": "daily_news",
    "label": "news",
    "schedule": "0 9 * * *",
    "limit": 10,
    "lookback_days": 1,
    "tg_chat_id": "${TG_CHAT_ID}",
}


@pytest.fixture
def env() -> dict[str, str]:
    return dict(TEST_ENV)


@pytest.fixture
def project_config_dir() -> Path:
    return PROJECT_ROOT / "config"


@pytest.fixture
def write_config():
    """Собрать каталог конфигов из словарей — для проверок валидации."""

    def _write(
        directory: Path,
        *,
        labels=None,
        sources=None,
        queries=None,
        dedup=None,
        defaults=None,
        run_on_start=True,
    ) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        payloads = {
            "labels.yaml": {"labels": [MIN_LABEL] if labels is None else labels},
            "sources.yaml": {
                "dedup": dedup if dedup is not None else {"strip_params": ["utm_source"]},
                "sources": [MIN_SOURCE] if sources is None else sources,
            },
            "queries.yaml": {
                "run_on_start": run_on_start,
                "defaults": {"limit": 10, "lookback_days": 1, "summarize": False}
                if defaults is None
                else defaults,
                "queries": [MIN_QUERY] if queries is None else queries,
            },
        }
        for filename, payload in payloads.items():
            (directory / filename).write_text(
                yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
        return directory

    return _write
