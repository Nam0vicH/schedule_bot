"""SQLite-кэш расписания."""

from __future__ import annotations

import datetime
import json
import logging
import sqlite3
from pathlib import Path

from schedule_bot import config
from schedule_bot.models import DaySchedule
from schedule_bot import parser

logger = logging.getLogger(__name__)


def _get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """Создать или открыть соединение с БД кэша."""
    path = db_path or config.CACHE_DB_PATH
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schedule_cache (
            date       TEXT PRIMARY KEY,
            data       TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def get_cached(target: datetime.date) -> DaySchedule | None:
    """Получить расписание из кэша, если оно ещё свежее.

    Возвращает *None*, если записи нет или она устарела (> CACHE_TTL_HOURS).
    """
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT data, fetched_at FROM schedule_cache WHERE date = ?",
            (target.isoformat(),),
        ).fetchone()
        if not row:
            return None

        data_json, fetched_at_str = row
        fetched_at = datetime.datetime.fromisoformat(fetched_at_str)
        age = datetime.datetime.now() - fetched_at

        if age.total_seconds() > config.CACHE_TTL_HOURS * 3600:
            logger.debug("Кэш устарел для %s (возраст: %s)", target, age)
            return None

        data = json.loads(data_json)
        day = DaySchedule.from_dict(data)
        logger.debug("Кэш-попадание для %s", target)
        return day
    finally:
        conn.close()


def save_cache(day: DaySchedule) -> None:
    """Сохранить расписание дня в кэш."""
    conn = _get_connection()
    try:
        data_json = json.dumps(day.to_dict(), ensure_ascii=False)
        now = datetime.datetime.now().isoformat()
        conn.execute(
            """
            INSERT OR REPLACE INTO schedule_cache (date, data, fetched_at)
            VALUES (?, ?, ?)
            """,
            (day.date.isoformat(), data_json, now),
        )
        conn.commit()
        logger.debug("Кэш обновлён для %s", day.date)
    finally:
        conn.close()


def save_week_cache(days: list[DaySchedule]) -> None:
    """Сохранить все дни недели в кэш."""
    for day in days:
        save_cache(day)


async def get_or_fetch(target: datetime.date) -> DaySchedule:
    """Основной метод: сначала кэш, потом парсер.

    Если данные есть в кэше и не устарели — вернёт из кэша.
    Иначе — скачает с сайта, обновит кэш и вернёт.
    """
    cached = get_cached(target)
    if cached is not None:
        return cached

    logger.info("Кэш промах для %s, загружаю с сайта…", target)
    day = await parser.get_schedule_for_date(target)
    save_cache(day)
    return day


async def get_or_fetch_week(
    date_start: datetime.date,
    date_end: datetime.date,
) -> list[DaySchedule]:
    """Получить расписание на неделю: из кэша или с сайта.

    Если хотя бы один день не в кэше — загружает всю неделю.
    """
    all_cached: list[DaySchedule] = []
    need_fetch = False

    current = date_start
    while current <= date_end:
        cached = get_cached(current)
        if cached is not None:
            all_cached.append(cached)
        else:
            need_fetch = True
            break
        current += datetime.timedelta(days=1)

    if not need_fetch:
        return [d for d in all_cached if d.has_lessons]

    logger.info("Загружаю неделю %s — %s с сайта…", date_start, date_end)
    days = await parser.fetch_week_schedule(date_start, date_end)

    fetched_dates = {d.date for d in days}
    weekdays = [
        "Понедельник", "Вторник", "Среда",
        "Четверг", "Пятница", "Суббота", "Воскресенье",
    ]
    current = date_start
    while current <= date_end:
        if current not in fetched_dates:
            empty = DaySchedule(
                weekday=weekdays[current.weekday()],
                date=current,
                lessons=[],
            )
            save_cache(empty)
        current += datetime.timedelta(days=1)

    save_week_cache(days)
    return days
