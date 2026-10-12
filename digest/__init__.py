"""Дайджест: отбор материалов и форматирование для Telegram."""

from .builder import Digest, Section, build_digest, format_salary, render_section, select_items, select_last
from .llm import DeepSeekSummarizer, make_summarizer

__all__ = [
    "Digest",
    "Section",
    "build_digest",
    "format_salary",
    "render_section",
    "select_items",
    "select_last",
    "DeepSeekSummarizer",
    "make_summarizer",
]
