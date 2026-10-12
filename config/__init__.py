"""Конфигурация проекта: загрузка, разрешение ${ENV} и валидация."""

from .loader import (
    AppConfig,
    ConfigError,
    DedupConfig,
    Label,
    Query,
    Source,
    load_config,
    validate_cron,
)

__all__ = [
    "AppConfig",
    "ConfigError",
    "DedupConfig",
    "Label",
    "Query",
    "Source",
    "load_config",
    "validate_cron",
]
