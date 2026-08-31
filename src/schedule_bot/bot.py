"""Telegram-бот расписания МИИГАиК."""

from __future__ import annotations

import datetime
import logging
import zoneinfo

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    filters,
)

from schedule_bot import config
from schedule_bot import cache
from schedule_bot import parser
from schedule_bot.formatter import (
    format_day,
    format_next_lesson,
    format_notification,
    format_week,
)
from schedule_bot.models import DaySchedule

logger = logging.getLogger(__name__)

TZ = zoneinfo.ZoneInfo(config.TIMEZONE)


# ── Фильтр авторизации ─────────────────────────────────────

class _AllowedUserFilter(filters.MessageFilter):
    """Пропускает только сообщения от разрешённого пользователя."""

    def filter(self, message) -> bool:  # type: ignore[override]
        if not message.from_user:
            return False
        return message.from_user.id == config.ALLOWED_TELEGRAM_USER_ID


_allowed = _AllowedUserFilter()


# ── Вспомогательные функции ─────────────────────────────────

def _now_moscow() -> datetime.datetime:
    """Текущее время в часовом поясе Europe/Moscow."""
    return datetime.datetime.now(tz=TZ)


def _today() -> datetime.date:
    return _now_moscow().date()


def _tomorrow() -> datetime.date:
    return _today() + datetime.timedelta(days=1)


async def _send(update: Update, text: str) -> None:
    """Отправить HTML-сообщение."""
    if update.effective_chat:
        await update.effective_chat.send_message(
            text=text,
            parse_mode=ParseMode.HTML,
        )


# ── Обработчики команд ─────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка /start."""
    text = (
        "🎓 <b>Бот расписания МИИГАиК</b>\n"
        f"👥 Группа: {config.GROUP_NAME}\n\n"
        "/today — расписание на сегодня\n"
        "/tomorrow — расписание на завтра\n"
        "/week — расписание на текущую неделю\n"
        "/next — ближайшее следующее занятие"
    )
    await _send(update, text)
    logger.info("Пользователь %s выполнил /start", update.effective_user)


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка /today."""
    try:
        day = await cache.get_or_fetch(_today())
        text = format_day(day, label="Сегодня")
    except Exception:
        logger.exception("Ошибка при получении расписания на сегодня")
        text = "⚠️ Не удалось получить актуальное расписание. Попробую обновить данные позже."
    await _send(update, text)


async def cmd_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка /tomorrow."""
    try:
        day = await cache.get_or_fetch(_tomorrow())
        text = format_day(day, label="Завтра")
    except Exception:
        logger.exception("Ошибка при получении расписания на завтра")
        text = "⚠️ Не удалось получить актуальное расписание. Попробую обновить данные позже."
    await _send(update, text)


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка /week."""
    try:
        monday, sunday = parser.get_week_bounds(_today())
        days = await cache.get_or_fetch_week(monday, sunday)
        text = format_week(days)
    except Exception:
        logger.exception("Ошибка при получении расписания на неделю")
        text = "⚠️ Не удалось получить актуальное расписание. Попробую обновить данные позже."
    await _send(update, text)


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка /next — ближайшее следующее занятие."""
    try:
        now = _now_moscow()
        current_time = now.strftime("%H:%M")

        # Проверяем сегодня — есть ли ещё занятия
        today_schedule = await cache.get_or_fetch(_today())
        for lesson in today_schedule.lessons:
            if lesson.time_start > current_time:
                text = format_next_lesson(lesson, today_schedule)
                await _send(update, text)
                return

        # Смотрим следующие 14 дней
        for offset in range(1, 15):
            target = _today() + datetime.timedelta(days=offset)
            day = await cache.get_or_fetch(target)
            if day.has_lessons:
                text = format_next_lesson(day.lessons[0], day)
                await _send(update, text)
                return

        await _send(update, "📅 В ближайшие 2 недели занятий не найдено.")
    except Exception:
        logger.exception("Ошибка при поиске ближайшего занятия")
        await _send(
            update,
            "⚠️ Не удалось получить актуальное расписание. Попробую обновить данные позже.",
        )


async def cmd_unauthorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ответ неавторизованному пользователю."""
    logger.warning(
        "Неавторизованный доступ от user_id=%s",
        update.effective_user.id if update.effective_user else "unknown",
    )
    await _send(update, "🚫 У вас нет доступа к этому боту.")


# ── Планировщик (автоматические уведомления) ────────────────

async def _daily_notification(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ежедневная отправка расписания на завтра."""
    try:
        tomorrow = _tomorrow()
        day = await cache.get_or_fetch(tomorrow)
        text = format_notification(day)
        await context.bot.send_message(
            chat_id=config.ALLOWED_TELEGRAM_USER_ID,
            text=text,
            parse_mode=ParseMode.HTML,
        )
        logger.info("Отправлено ежедневное уведомление на %s", tomorrow)
    except Exception:
        logger.exception("Ошибка при отправке ежедневного уведомления")


async def _refresh_cache(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Периодическое обновление кэша (текущая и следующая неделя)."""
    try:
        today = _today()
        # Текущая неделя
        mon1, sun1 = parser.get_week_bounds(today)
        days1 = await parser.fetch_week_schedule(mon1, sun1)
        cache.save_week_cache(days1)

        # Следующая неделя
        next_mon = sun1 + datetime.timedelta(days=1)
        mon2, sun2 = parser.get_week_bounds(next_mon)
        days2 = await parser.fetch_week_schedule(mon2, sun2)
        cache.save_week_cache(days2)

        total = sum(len(d.lessons) for d in days1 + days2)
        logger.info("Кэш обновлён: %d занятий за 2 недели", total)
    except Exception:
        logger.exception("Ошибка при обновлении кэша")


def _setup_jobs(app: Application) -> None:
    """Настроить периодические задачи."""
    job_queue = app.job_queue
    if job_queue is None:
        logger.error("JobQueue недоступен, уведомления не будут работать")
        return

    # Ежедневное уведомление
    try:
        hour, minute = map(int, config.NOTIFICATION_TIME.split(":"))
    except ValueError:
        hour, minute = 19, 0
        logger.warning(
            "Некорректный NOTIFICATION_TIME=%s, используется 19:00",
            config.NOTIFICATION_TIME,
        )

    notify_time = datetime.time(hour=hour, minute=minute, tzinfo=TZ)
    job_queue.run_daily(
        _daily_notification,
        time=notify_time,
        name="daily_notification",
    )
    logger.info("Ежедневное уведомление запланировано на %s", notify_time)

    # Обновление кэша каждые 4 часа
    job_queue.run_repeating(
        _refresh_cache,
        interval=datetime.timedelta(hours=4),
        first=datetime.timedelta(minutes=5),
        name="refresh_cache",
    )
    logger.info("Обновление кэша запланировано каждые 4 часа")


# ── Запуск бота ─────────────────────────────────────────────

def run_bot() -> None:
    """Запустить Telegram-бота в polling-режиме."""
    config.validate()

    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .build()
    )

    # Команды для авторизованного пользователя
    app.add_handler(CommandHandler("start", cmd_start, filters=_allowed))
    app.add_handler(CommandHandler("today", cmd_today, filters=_allowed))
    app.add_handler(CommandHandler("tomorrow", cmd_tomorrow, filters=_allowed))
    app.add_handler(CommandHandler("week", cmd_week, filters=_allowed))
    app.add_handler(CommandHandler("next", cmd_next, filters=_allowed))

    # Все остальные сообщения — unauthorized
    app.add_handler(CommandHandler("start", cmd_unauthorized))
    app.add_handler(CommandHandler("today", cmd_unauthorized))
    app.add_handler(CommandHandler("tomorrow", cmd_unauthorized))
    app.add_handler(CommandHandler("week", cmd_unauthorized))
    app.add_handler(CommandHandler("next", cmd_unauthorized))

    # Периодические задачи
    _setup_jobs(app)

    logger.info(
        "Бот запущен (polling). Группа: %s, user_id: %s",
        config.GROUP_NAME,
        config.ALLOWED_TELEGRAM_USER_ID,
    )

    app.run_polling(drop_pending_updates=True)
