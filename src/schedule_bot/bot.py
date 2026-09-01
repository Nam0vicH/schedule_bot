"""Telegram-бот расписания МИИГАиК на aiogram 3.x с поддержкой групп и эфемерных сообщений."""

from __future__ import annotations

import asyncio
import datetime
import logging
import zoneinfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    EphemeralMessageParameters,
    InlineKeyboardMarkup,
    Message,
)

from schedule_bot import cache, config, parser
from schedule_bot.formatter import (
    format_day,
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


# ── Проверка типа чата и отправка сообщений ──────────────────

def _is_group_chat(chat_type: str | None) -> bool:
    """Проверить, что чат является групповым."""
    return chat_type in (ChatType.GROUP, ChatType.SUPERGROUP)


async def _send_response(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    user_id: int | None = None,
    chat_type: str = ChatType.PRIVATE,
) -> None:
    """Отправить сообщение (в группах пробуем эфемерно, при любой ошибке — штатно)."""
    if _is_group_chat(chat_type) and user_id:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                ephemeral_message_parameters=EphemeralMessageParameters(
                    receiver_user_id=user_id,
                ),
            )
            return
        except Exception as exc:
            logger.debug("Эфемерная отправка не удалась (%s), выполняем обычную отправку", exc)

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
    text = (
        "/today — расписание на сегодня\n"
        "/tomorrow — расписание на завтра\n"
        "/week — расписание на текущую неделю"
    )

    await _send_response(
        bot=bot,
        chat_id=message.chat.id,
        text=text,
        user_id=message.from_user.id if message.from_user else None,
        chat_type=message.chat.type,
    )


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


# ── Обработчики callback-запросов ──────────────────────────

@router.callback_query(F.data.startswith("day:"))
async def on_day_callback(query: CallbackQuery) -> None:
    """Обработка переключения дня через inline-кнопку."""
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

        if _is_group_chat(query.message.chat.type):
            try:
                await query.bot.send_message(
                    chat_id=query.message.chat.id,
                    text=text,
                    reply_markup=keyboard,
                    ephemeral_message_parameters=EphemeralMessageParameters(
                        receiver_user_id=query.from_user.id,
                        callback_query_id=query.id,
                    ),
                )
                return
            except Exception:
                pass

        await query.answer()
        if isinstance(query.message, Message):
            await query.message.edit_text(text=text, reply_markup=keyboard)
    except Exception:
        logger.exception("Ошибка при обработке day callback: %s", date_str)
        try:
            await query.answer()
        except Exception:
            pass


@router.callback_query(F.data.startswith("week:"))
async def on_week_callback(query: CallbackQuery) -> None:
    """Обработка переключения недели через inline-кнопку."""
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

        if _is_group_chat(query.message.chat.type):
            try:
                await query.bot.send_message(
                    chat_id=query.message.chat.id,
                    text=text,
                    reply_markup=keyboard,
                    ephemeral_message_parameters=EphemeralMessageParameters(
                        receiver_user_id=query.from_user.id,
                        callback_query_id=query.id,
                    ),
                )
                return
            except Exception:
                pass

        await query.answer()
        if isinstance(query.message, Message):
            await query.message.edit_text(text=text, reply_markup=keyboard)
    except Exception:
        logger.exception("Ошибка при обработке week callback: %s", date_str)
        try:
            await query.answer()
        except Exception:
            pass


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

    logger.info(
        "Бот aiogram 3 запущен (polling). Группа: %s",
        config.GROUP_NAME,
    )

    try:
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        await bot.session.close()


def run_bot() -> None:
    """Точка входа для запуска бота."""
    asyncio.run(start_bot())
