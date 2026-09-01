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

# Список администраторов бота (опционально, через запятую в .env)
# Обратная совместимость: если задан ALLOWED_TELEGRAM_USER_ID — используем его
ADMIN_USER_IDS: list[int] = []
_raw_admin_ids = os.getenv("ADMIN_USER_IDS", "") or os.getenv("ALLOWED_TELEGRAM_USER_ID", "")
for _part in _raw_admin_ids.replace(",", " ").split():
    if _part.strip().isdigit():
        ADMIN_USER_IDS.append(int(_part.strip()))

# ── Кэш ────────────────────────────────────────────────────
CACHE_DB_PATH = _project_root / "schedule_cache.db"
CACHE_TTL_HOURS = 2  # время жизни кэша в часах


# ── Валидация ───────────────────────────────────────────────
def validate() -> None:
    """Проверить, что все необходимые переменные заданы."""
    errors: list[str] = []
    if not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN не задан в .env")
    if errors:
        for e in errors:
            print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)

