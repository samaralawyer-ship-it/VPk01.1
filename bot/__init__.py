"""Telegram-бот: приём команд и отправка дайджеста."""

from .sender import SenderError, TelegramSender, make_sender, split_text
from .telegram_bot import BotContext, allowed_chat_ids, build_router, run_bot

__all__ = [
    "SenderError",
    "TelegramSender",
    "make_sender",
    "split_text",
    "BotContext",
    "allowed_chat_ids",
    "build_router",
    "run_bot",
]
