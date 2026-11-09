"""Middleware: запоминает группы, в которых присутствует бот.

Это нужно, чтобы при создании опроса из личного чата пользователь мог выбрать
группу из списка, не вводя chat_id вручную.
"""

from __future__ import annotations

import time

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import TelegramObject

from schedule_bot.polls import db

_THROTTLE_SECONDS = 300


class PollChatTrackerMiddleware(BaseMiddleware):
    def __init__(self) -> None:
        self._last_seen: dict[int, float] = {}

    async def __call__(self, handler, event: TelegramObject, data: dict):
        new_member = getattr(event, "new_chat_member", None)
        if new_member is not None and getattr(new_member, "status", "") in ("left", "kicked"):
            return await handler(event, data)

        chat = getattr(event, "chat", None)
        if chat is not None and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            now = time.monotonic()
            if now - self._last_seen.get(chat.id, 0.0) > _THROTTLE_SECONDS:
                db.record_chat(
                    chat.id,
                    chat.title or f"Чат {chat.id}",
                    chat.type,
                    getattr(chat, "username", None),
                    bool(getattr(chat, "is_forum", False)),
                )
                self._last_seen[chat.id] = now
        return await handler(event, data)
