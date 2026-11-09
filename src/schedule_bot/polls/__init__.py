"""Модуль интерактивных опросов с бронированием вариантов."""

from __future__ import annotations

from schedule_bot.polls import db  # noqa: F401  (инициализация схемы при импорте не выполняется)

__all__ = ["db"]
