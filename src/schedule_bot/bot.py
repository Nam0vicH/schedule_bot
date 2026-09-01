"""Telegram-бот расписания МИИГАиК на aiogram 3.x с поддержкой групп и эфемерных сообщений."""

from __future__ import annotations

import asyncio
import datetime
import logging
import zoneinfo

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from schedule_bot import cache, config, parser
from schedule_bot.diff import compare_schedules, format_changes
from schedule_bot.formatter import (
    format_day,
    format_notification,
    format_week,
    get_day_keyboard,
    get_week_keyboard,
)

logger = logging.getLogger(__name__)

TZ = zoneinfo.ZoneInfo(config.TIMEZONE)

router = Router(name="schedule_router")


# ── Вспомогательные функции дат ─────────────────────────────

def _now_moscow() -> datetime.datetime:
    """Текущее время в часовом поясе Europe/Moscow."""
    return datetime.datetime.now(tz=TZ)


def _today() -> datetime.date:
    return _now_moscow().date()


def _tomorrow() -> datetime.date:
    return _today() + datetime.timedelta(days=1)


# ── Проверка прав и отправка сообщений ───────────────────────

def _is_group_chat(chat_type: str | None) -> bool:
    """Проверить, что чат является групповым."""
    return chat_type in (ChatType.GROUP, ChatType.SUPERGROUP)


async def _is_chat_admin(bot: Bot, chat_id: int, user_id: int, chat_type: str) -> bool:
    """Проверить, является ли пользователь администратором чата.

    Для личных сообщений всегда True.
    Для групп проверяет права в Telegram или список ADMIN_USER_IDS.
    """
    if chat_type == ChatType.PRIVATE:
        return True
    if user_id in config.ADMIN_USER_IDS:
        return True

    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        logger.exception("Ошибка при проверке прав администратора")
        return False


async def _send_response(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    user_id: int | None = None,
    chat_type: str = ChatType.PRIVATE,
) -> None:
    """Отправить сообщение (с ephemeral_message_parameters в групповых чатах)."""
    if _is_group_chat(chat_type) and user_id:
        # Пробуем отправить как эфемерное сообщение
        try:
            payload: dict = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "ephemeral_message_parameters": {
                    "receiver_user_id": user_id,
                },
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup.model_dump(exclude_none=True)

            url = f"https://api.telegram.org/bot{bot.token}/sendMessage"
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
                data = resp.json()
                if data.get("ok"):
                    return
                logger.debug("Эфемерная отправка не удалась: %s, переход на fallback", data)
        except Exception:
            logger.debug("Исключение при эфемерной отправке, переход на fallback", exc_info=True)

    # Обычная отправка (в ЛС или при fallback)
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
    )


# ── Обработчики команд ─────────────────────────────────────

@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message, bot: Bot) -> None:
    """Обработка /start и /help."""
    is_group = _is_group_chat(message.chat.type)
    if is_group:
        text = (
            "🎓 <b>Бот расписания МИИГАиК</b>\n"
            f"👥 Группа: {config.GROUP_NAME}\n\n"
            "Доступные команды:\n"
            "/today — расписание на сегодня\n"
            "/tomorrow — расписание на завтра\n"
            "/week — расписание на текущую неделю\n\n"
            "Для управления рассылкой (администраторы):\n"
            "/subscribe — подписать чат на рассылку\n"
            "/unsubscribe — отключить рассылку\n"
            "/notify — статус подписки"
        )
    else:
        text = (
            "🎓 <b>Бот расписания МИИГАиК</b>\n"
            f"👥 Группа: {config.GROUP_NAME}\n\n"
            "/today — расписание на сегодня\n"
            "/tomorrow — расписание на завтра\n"
            "/week — расписание на текущую неделю\n\n"
            "/subscribe — подписаться на ежедневную рассылку\n"
            "/unsubscribe — отключить рассылку\n"
            "/notify — статус подписки\n\n"
            "💡 Бот также работает в групповых чатах!\n"
            "Добавьте его в чат группы одногруппников."
        )

    await _send_response(
        bot=bot,
        chat_id=message.chat.id,
        text=text,
        user_id=message.from_user.id if message.from_user else None,
        chat_type=message.chat.type,
    )
    logger.info("Пользователь %s выполнил /start в чате %s", message.from_user, message.chat.id)


@router.message(Command("today"))
async def cmd_today(message: Message, bot: Bot) -> None:
    """Обработка /today."""
    try:
        day = await cache.get_or_fetch(_today())
        text = format_day(day, label="Сегодня")
        keyboard = get_day_keyboard(_today())
    except Exception:
        logger.exception("Ошибка при получении расписания на сегодня")
        text = "⚠️ Не удалось получить актуальное расписание. Попробуйте позже."
        keyboard = None

    await _send_response(
        bot=bot,
        chat_id=message.chat.id,
        text=text,
        reply_markup=keyboard,
        user_id=message.from_user.id if message.from_user else None,
        chat_type=message.chat.type,
    )


@router.message(Command("tomorrow"))
async def cmd_tomorrow(message: Message, bot: Bot) -> None:
    """Обработка /tomorrow."""
    try:
        day = await cache.get_or_fetch(_tomorrow())
        text = format_day(day, label="Завтра")
        keyboard = get_day_keyboard(_tomorrow())
    except Exception:
        logger.exception("Ошибка при получении расписания на завтра")
        text = "⚠️ Не удалось получить актуальное расписание. Попробуйте позже."
        keyboard = None

    await _send_response(
        bot=bot,
        chat_id=message.chat.id,
        text=text,
        reply_markup=keyboard,
        user_id=message.from_user.id if message.from_user else None,
        chat_type=message.chat.type,
    )


@router.message(Command("week"))
async def cmd_week(message: Message, bot: Bot) -> None:
    """Обработка /week."""
    try:
        monday, sunday = parser.get_week_bounds(_today())
        days = await cache.get_or_fetch_week(monday, sunday)
        text = format_week(days)
        keyboard = get_week_keyboard(monday)
    except Exception:
        logger.exception("Ошибка при получении расписания на неделю")
        text = "⚠️ Не удалось получить актуальное расписание. Попробуйте позже."
        keyboard = None

    await _send_response(
        bot=bot,
        chat_id=message.chat.id,
        text=text,
        reply_markup=keyboard,
        user_id=message.from_user.id if message.from_user else None,
        chat_type=message.chat.type,
    )


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message, bot: Bot) -> None:
    """Обработка /subscribe — подписать чат на ежедневную рассылку."""
    if not message.from_user:
        return

    is_admin = await _is_chat_admin(
        bot=bot,
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        chat_type=message.chat.type,
    )
    if not is_admin:
        await message.reply("🚫 Управление рассылкой доступно только администраторам чата.")
        return

    title = message.chat.title or message.from_user.full_name or "Чат"
    cache.subscribe_chat(
        chat_id=message.chat.id,
        chat_type=message.chat.type,
        title=title,
    )

    if _is_group_chat(message.chat.type):
        await message.reply("✅ Чат подписан на ежедневную рассылку расписания на завтра и оповещения об изменениях.")
    else:
        await message.reply("✅ Вы подписаны на ежедневную рассылку расписания на завтра и оповещения об изменениях.")


@router.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message, bot: Bot) -> None:
    """Обработка /unsubscribe — отписать чат от рассылки."""
    if not message.from_user:
        return

    is_admin = await _is_chat_admin(
        bot=bot,
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        chat_type=message.chat.type,
    )
    if not is_admin:
        await message.reply("🚫 Управление рассылкой доступно только администраторам чата.")
        return

    deleted = cache.unsubscribe_chat(message.chat.id)
    if deleted:
        await message.reply("🔕 Рассылка отключена.")
    else:
        await message.reply("ℹ️ Этот чат не был подписан на рассылку.")


@router.message(Command("notify"))
async def cmd_notify(message: Message) -> None:
    """Обработка /notify — показать статус подписки."""
    subscribed = cache.is_subscribed(message.chat.id)
    if subscribed:
        await message.reply(
            "🔔 <b>Рассылка включена</b>\n\n"
            f"⏰ Ежедневное расписание на завтра: {config.NOTIFICATION_TIME}\n"
            "📢 Оповещения об изменениях: включены\n\n"
            "Для отключения: /unsubscribe"
        )
    else:
        await message.reply(
            "🔕 <b>Рассылка отключена</b>\n\n"
            "Для подписки: /subscribe"
        )


# ── Обработчики callback-запросов ──────────────────────────

@router.callback_query(F.data.startswith("day:"))
async def on_day_callback(query: CallbackQuery) -> None:
    """Обработка переключения дня через inline-кнопку."""
    await query.answer()
    if not query.data or not query.message:
        return

    date_str = query.data[4:]
    if date_str == "today":
        target = _today()
        label = "Сегодня"
    elif date_str == "tomorrow":
        target = _tomorrow()
        label = "Завтра"
    else:
        try:
            target = datetime.date.fromisoformat(date_str)
            if target == _today():
                label = "Сегодня"
            elif target == _tomorrow():
                label = "Завтра"
            else:
                label = None
        except ValueError:
            return

    try:
        day = await cache.get_or_fetch(target)
        text = format_day(day, label=label)
        keyboard = get_day_keyboard(target)
        if isinstance(query.message, Message):
            await query.message.edit_text(text=text, reply_markup=keyboard)
    except Exception:
        logger.exception("Ошибка при обработке day callback: %s", date_str)


@router.callback_query(F.data.startswith("week:"))
async def on_week_callback(query: CallbackQuery) -> None:
    """Обработка переключения недели через inline-кнопку."""
    await query.answer()
    if not query.data or not query.message:
        return

    date_str = query.data[5:]
    if date_str == "current":
        monday, sunday = parser.get_week_bounds(_today())
    else:
        try:
            target = datetime.date.fromisoformat(date_str)
            monday, sunday = parser.get_week_bounds(target)
        except ValueError:
            return

    try:
        days = await cache.get_or_fetch_week(monday, sunday)
        text = format_week(days)
        keyboard = get_week_keyboard(monday)
        if isinstance(query.message, Message):
            await query.message.edit_text(text=text, reply_markup=keyboard)
    except Exception:
        logger.exception("Ошибка при обработке week callback: %s", date_str)


# ── Периодические задачи (APScheduler) ──────────────────────

async def _daily_notification(bot: Bot) -> None:
    """Ежедневная отправка расписания на завтра во все подписанные чаты."""
    try:
        tomorrow = _tomorrow()
        day = await cache.get_or_fetch(tomorrow)
        text = format_notification(day)
        subscribers = cache.get_daily_subscribers()

        sent_count = 0
        for chat_id in subscribers:
            try:
                await bot.send_message(chat_id=chat_id, text=text)
                sent_count += 1
            except Exception:
                logger.warning("Не удалось отправить ежедневное уведомление в чат %s", chat_id)

        logger.info(
            "Отправлено ежедневное уведомление на %s в %d/%d чатов",
            tomorrow, sent_count, len(subscribers),
        )
    except Exception:
        logger.exception("Ошибка при отправке ежедневного уведомления")


async def _refresh_cache(bot: Bot) -> None:
    """Периодическое обновление кэша (2 недели) + проверка изменений."""
    try:
        today = _today()

        # Текущая неделя
        mon1, sun1 = parser.get_week_bounds(today)
        days1 = await parser.fetch_week_schedule(mon1, sun1)

        # Следующая неделя
        next_mon = sun1 + datetime.timedelta(days=1)
        mon2, sun2 = parser.get_week_bounds(next_mon)
        days2 = await parser.fetch_week_schedule(mon2, sun2)

        # Проверяем изменения и рассылаем diff-уведомления
        if config.AUTO_DIFF_NOTIFY:
            all_changes = []
            for day in days1 + days2:
                old = cache.get_cached_raw(day.date)
                if old is not None:
                    changes = compare_schedules(old, day)
                    all_changes.extend(changes)

            if all_changes:
                diff_text = format_changes(all_changes)
                diff_subscribers = cache.get_diff_subscribers()
                for chat_id in diff_subscribers:
                    try:
                        await bot.send_message(chat_id=chat_id, text=diff_text)
                    except Exception:
                        logger.warning("Не удалось отправить diff в чат %s", chat_id)
                logger.info(
                    "Обнаружено %d изменений, отправлено в %d чатов",
                    len(all_changes), len(diff_subscribers),
                )

        # Сохраняем в кэш (после проверки diff!)
        cache.save_week_cache(days1)
        cache.save_week_cache(days2)

        total = sum(len(d.lessons) for d in days1 + days2)
        logger.info("Кэш обновлён: %d занятий за 2 недели", total)
    except Exception:
        logger.exception("Ошибка при обновлении кэша")


def _setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    """Настроить планировщик APScheduler."""
    scheduler = AsyncIOScheduler(timezone=TZ)

    try:
        hour, minute = map(int, config.NOTIFICATION_TIME.split(":"))
    except ValueError:
        hour, minute = 19, 0
        logger.warning(
            "Некорректный NOTIFICATION_TIME=%s, используется 19:00",
            config.NOTIFICATION_TIME,
        )

    # Ежедневное уведомление
    scheduler.add_job(
        _daily_notification,
        trigger=CronTrigger(hour=hour, minute=minute, timezone=TZ),
        args=[bot],
        id="daily_notification",
        replace_existing=True,
    )
    logger.info("Ежедневное уведомление запланировано на %02d:%02d (%s)", hour, minute, config.TIMEZONE)

    # Периодическое обновление кэша каждые 4 часа
    scheduler.add_job(
        _refresh_cache,
        trigger=IntervalTrigger(hours=4, timezone=TZ),
        args=[bot],
        id="refresh_cache",
        replace_existing=True,
    )
    logger.info("Обновление кэша запланировано каждые 4 часа")

    return scheduler


# ── Запуск бота ─────────────────────────────────────────────

async def start_bot() -> None:
    """Запустить бота на aiogram 3."""
    config.validate()

    bot = Bot(
        token=config.TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    scheduler = _setup_scheduler(bot)
    scheduler.start()

    logger.info(
        "Бот aiogram 3 запущен (polling). Группа: %s, админы: %s",
        config.GROUP_NAME,
        config.ADMIN_USER_IDS or "не указаны",
    )

    try:
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


def run_bot() -> None:
    """Точка входа для запуска бота."""
    asyncio.run(start_bot())
