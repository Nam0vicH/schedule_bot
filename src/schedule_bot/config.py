"""Конфигурация бота расписания МИИГАиК."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Загрузка .env из корня проекта
_project_root = Path(__file__).resolve().parent.parent.parent
load_dotenv(_project_root / ".env")

# ── Расписание ──────────────────────────────────────────────
SOURCE_URL = "https://study.miigaik.ru/"
ORG_ID = 2
GROUP_ID = 1999
GROUP_NAME = "ИБО-26-2"
TIMEZONE = "Europe/Moscow"

# ── Telegram ────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALLOWED_TELEGRAM_USER_ID: int = 0

_raw_user_id = os.getenv("ALLOWED_TELEGRAM_USER_ID", "")
if _raw_user_id.isdigit():
    ALLOWED_TELEGRAM_USER_ID = int(_raw_user_id)

# ── Уведомления ────────────────────────────────────────────
NOTIFICATION_TIME: str = os.getenv("NOTIFICATION_TIME", "19:00")

# ── Кэш ────────────────────────────────────────────────────
CACHE_DB_PATH = _project_root / "schedule_cache.db"
CACHE_TTL_HOURS = 2  # время жизни кэша в часах

# ── Валидация ───────────────────────────────────────────────
def validate() -> None:
    """Проверить, что все необходимые переменные заданы."""
    errors: list[str] = []
    if not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN не задан в .env")
    if not ALLOWED_TELEGRAM_USER_ID:
        errors.append("ALLOWED_TELEGRAM_USER_ID не задан в .env")
    if errors:
        for e in errors:
            print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)
