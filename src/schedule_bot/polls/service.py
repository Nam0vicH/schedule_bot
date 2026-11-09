"""Сервисные операции модуля опросов: публикация, обновление карточек, сроки."""

from __future__ import annotations

import asyncio
import datetime
import logging
import zoneinfo

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import BufferedInputFile, InputMediaPhoto, InlineKeyboardMarkup, Message

from schedule_bot import config
from schedule_bot.polls import avatars as avatars_module
from schedule_bot.polls import card as card_module
from schedule_bot.polls import db, ui
from schedule_bot.polls.models import (
    CARD_PHOTO,
    CARD_TEXT,
    STATUS_ACTIVE,
    Booking,
    ChatTarget,
    Option,
    OptionView,
    Poll,
    PollView,
)

logger = logging.getLogger(__name__)

_TZ = zoneinfo.ZoneInfo(config.TIMEZONE)

_card_locks: dict[int, asyncio.Lock] = {}
_refresh_jobs: dict[int, asyncio.Task] = {}
_refresh_dirty: set[int] = set()
_full_caption_polls: set[int] = set()
_bot_username: str | None = None


def _lock(poll_id: int) -> asyncio.Lock:
    lock = _card_locks.get(poll_id)
    if lock is None:
        lock = asyncio.Lock()
        _card_locks[poll_id] = lock
    return lock


async def get_bot_username(bot: Bot) -> str:
    global _bot_username
    if _bot_username is None:
        me = await bot.me()
        _bot_username = me.username or ""
    return _bot_username


def deep_link(username: str, payload: str) -> str:
    return f"https://t.me/{username}?start={payload}"


async def resolve_chat(bot: Bot, chat_id: int) -> ChatTarget | None:
    """Получить данные чата из БД или напрямую из Telegram."""
    target = db.get_chat_target(chat_id)
    if target is not None:
        return target
    try:
        chat = await bot.get_chat(chat_id)
    except Exception as exc:
        logger.warning("Не удалось получить чат %s: %s", chat_id, exc)
        return None
    return ChatTarget(
        chat_id=chat.id,
        title=chat.title or f"Чат {chat.id}",
        chat_type=chat.type,
        username=chat.username,
    )


def _build_preview_view(draft: dict) -> PollView:
    now = datetime.datetime.now(tz=_TZ)
    expiry_days = draft.get("expiry_days")
    expires_at = (
        (now + datetime.timedelta(days=int(expiry_days))).isoformat(timespec="seconds")
        if expiry_days
        else None
    )
    poll = Poll(
        id=0,
        creator_id=0,
        title=draft.get("title", ""),
        description=draft.get("description", ""),
        chat_id=draft.get("target_chat_id"),
        message_id=None,
        status=STATUS_ACTIVE,
        created_at=now.isoformat(timespec="seconds"),
        expires_at=expires_at,
        allow_change=bool(draft.get("allow_change", True)),
        allow_cancel=bool(draft.get("allow_cancel", True)),
        show_participants=bool(draft.get("show_participants", True)),
        default_max=int(draft.get("default_max", 1)),
        revision=1,
        card_mode="photo",
        rendered_revision=0,
    )
    options = [
        OptionView(
            option=Option(
                id=index,
                poll_id=0,
                title=title,
                position=index,
                max_participants=int(draft.get("default_max", 1)),
            ),
            bookings=[],
        )
        for index, title in enumerate(draft.get("options", []), start=1)
    ]
    return PollView(poll=poll, options=options)


async def build_card_image(
    bot: Bot,
    view: PollView,
    *,
    force_avatars: bool = False,
) -> bytes | None:
    try:
        avatar_map = await avatars_module.resolve_avatars(bot, view, force=force_avatars)
    except Exception:
        logger.exception("Не удалось получить аватарки для опроса #%s", view.poll.id)
        avatar_map = {}
    try:
        return await asyncio.to_thread(card_module.render_poll_card, view, avatar_map)
    except Exception:
        logger.exception("Не удалось отрисовать карточку опроса #%s", view.poll.id)
        return None


async def render_draft_preview(bot: Bot, draft: dict) -> tuple[bytes | None, str]:
    view = _build_preview_view(draft)
    image = await build_card_image(bot, view)
    caption = "👁 <b>Предпросмотр</b>\n\n" + ui.card_caption(view)
    return image, caption


async def _send_text_card(
    bot: Bot,
    chat_id: int,
    view: PollView,
) -> Message:
    return await bot.send_message(
        chat_id=chat_id,
        text=ui.poll_text(view),
        parse_mode=ParseMode.HTML,
        reply_markup=ui.poll_keyboard(view, text_mode=True),
    )


async def _send_new_card(
    bot: Bot,
    chat_id: int,
    view: PollView,
    image: bytes | None,
    caption: str,
    keyboard: InlineKeyboardMarkup,
) -> tuple[Message, str]:
    if image:
        try:
            message = await bot.send_photo(
                chat_id=chat_id,
                photo=BufferedInputFile(image, filename="poll.png"),
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            return message, "photo"
        except TelegramBadRequest as exc:
            logger.warning("Фото опроса не отправилось, публикую текстом: %s", exc)
    message = await _send_text_card(bot, chat_id, view)
    return message, "text"


async def publish_poll(bot: Bot, poll_id: int) -> tuple[bool, str]:
    """Опубликовать карточку опроса в чате и сохранить ID сообщения."""
    async with _lock(poll_id):
        view = db.get_view(poll_id)
        if view is None:
            return False, "Опрос не найден."
        if not view.poll.chat_id:
            return False, "Не выбран чат для публикации."

        image = await build_card_image(bot, view)
        caption = ui.card_caption(view)
        keyboard = ui.poll_keyboard(view)

        try:
            message, mode = await _send_new_card(
                bot, view.poll.chat_id, view, image, caption, keyboard
            )
        except TelegramForbiddenError:
            return False, "Бот не имеет прав на публикацию в этом чате."
        except TelegramAPIError as exc:
            logger.exception("Ошибка публикации опроса #%d", poll_id)
            return False, f"Telegram отклонил публикацию: {exc.message}"
        except Exception:
            logger.exception("Неожиданная ошибка публикации опроса #%d", poll_id)
            return False, "Не удалось опубликовать опрос."

        db.set_published_message(poll_id, view.poll.chat_id, message.message_id, mode)
        db.mark_rendered(poll_id, view.poll.revision)
        _full_caption_polls.discard(poll_id)
        return True, ""


def _is_missing_message_error(exc: TelegramBadRequest) -> bool:
    text = (exc.message or "").lower()
    return any(
        marker in text
        for marker in (
            "message to edit not found",
            "message can't be edited",
            "message_id_invalid",
        )
    )


def _is_text_message_error(exc: TelegramBadRequest) -> bool:
    text = (exc.message or "").lower()
    return any(
        marker in text
        for marker in (
            "there is no photo in the message",
            "message is not a photo",
        )
    )


async def refresh_published_card(
    bot: Bot,
    poll_id: int,
    *,
    force: bool = False,
) -> bool:
    """Обновить единственное сообщение опроса на месте."""
    async with _lock(poll_id):
        poll = db.get_poll(poll_id)
        if poll is None or not poll.chat_id or not poll.message_id:
            return False
        if not force and poll.rendered_revision == poll.revision:
            return True

        view = db.get_view(poll_id)
        if view is None:
            return False

        image = (
            None
            if poll.card_mode == CARD_TEXT
            else await build_card_image(bot, view)
        )
        text_version = poll_id in _full_caption_polls
        caption = (
            ui.poll_caption(view, limit=1000)
            if text_version
            else ui.card_caption(view)
        )
        keyboard = ui.poll_keyboard(view, text_version=text_version)
        chat_id = poll.chat_id
        message_id = poll.message_id
        mode = poll.card_mode

        async def _switch_to_text() -> None:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=ui.poll_text(view),
                parse_mode=ParseMode.HTML,
                reply_markup=ui.poll_keyboard(view, text_mode=True),
            )

        try:
            if poll.card_mode == CARD_TEXT:
                await _switch_to_text()
                mode = CARD_TEXT
            elif image:
                try:
                    await bot.edit_message_media(
                        chat_id=chat_id,
                        message_id=message_id,
                        media=InputMediaPhoto(
                            media=BufferedInputFile(image, filename="poll.png"),
                            caption=caption,
                            parse_mode=ParseMode.HTML,
                        ),
                        reply_markup=keyboard,
                    )
                    mode = CARD_PHOTO
                except TelegramBadRequest as exc:
                    if "message is not modified" in (exc.message or "").lower():
                        db.mark_rendered(poll_id, view.poll.revision)
                        return True
                    if _is_missing_message_error(exc):
                        raise
                    await _switch_to_text()
                    mode = CARD_TEXT
            elif poll.card_mode == CARD_PHOTO:
                try:
                    await bot.edit_message_caption(
                        chat_id=chat_id,
                        message_id=message_id,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=keyboard,
                    )
                    mode = CARD_PHOTO
                except TelegramBadRequest as exc:
                    if "message is not modified" in (exc.message or "").lower():
                        db.mark_rendered(poll_id, view.poll.revision)
                        return True
                    if _is_missing_message_error(exc):
                        raise
                    if _is_text_message_error(exc):
                        await _switch_to_text()
                        mode = CARD_TEXT
                    else:
                        raise
            else:
                await _switch_to_text()
                mode = CARD_TEXT
        except TelegramBadRequest as exc:
            if not _is_missing_message_error(exc):
                logger.warning("Не удалось обновить карточку опроса #%d: %s", poll_id, exc)
                return False
            # Сообщение удалено или уже не редактируется — публикуем заново.
            logger.info("Карточка опроса #%d не найдена, публикую заново", poll_id)
            try:
                await bot.delete_message(chat_id, message_id)
            except Exception:
                pass
            try:
                message, mode = await _send_new_card(
                    bot, chat_id, view, image, caption, keyboard
                )
                db.set_published_message(poll_id, chat_id, message.message_id, mode)
                db.mark_rendered(poll_id, view.poll.revision)
                if mode == CARD_TEXT:
                    _full_caption_polls.discard(poll_id)
                return True
            except Exception:
                logger.exception("Не удалось переопубликовать карточку опроса #%d", poll_id)
                return False
        except TelegramForbiddenError:
            logger.warning("Нет прав на обновление карточки опроса #%d", poll_id)
            return False
        except TelegramAPIError as exc:
            logger.warning("Ошибка Telegram при обновлении опроса #%d: %s", poll_id, exc)
            return False

        db.mark_rendered(poll_id, view.poll.revision)
        if mode != poll.card_mode:
            db.set_published_message(poll_id, chat_id, message_id, mode)
            if mode == CARD_TEXT:
                _full_caption_polls.discard(poll_id)
        return True


async def set_caption_full(bot: Bot, poll_id: int, *, full: bool) -> bool:
    """Переключить подпись карточки: краткая или полная текстовая версия."""
    async with _lock(poll_id):
        poll = db.get_poll(poll_id)
        if poll is None or not poll.chat_id or not poll.message_id:
            return False
        view = db.get_view(poll_id)
        if view is None:
            return False
        if poll.card_mode == CARD_TEXT:
            return False

        caption = (
            ui.poll_caption(view, limit=1000)
            if full
            else ui.card_caption(view)
        )
        keyboard = ui.poll_keyboard(view, text_version=full)
        try:
            await bot.edit_message_caption(
                chat_id=poll.chat_id,
                message_id=poll.message_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in (exc.message or "").lower():
                logger.warning("Не удалось переключить подпись опроса #%d: %s", poll_id, exc)
                return False
        except TelegramAPIError as exc:
            logger.warning("Ошибка Telegram при переключении подписи #%d: %s", poll_id, exc)
            return False

        if full:
            _full_caption_polls.add(poll_id)
        else:
            _full_caption_polls.discard(poll_id)
        return True


async def _refresh_loop(bot: Bot, poll_id: int, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
        while True:
            _refresh_dirty.discard(poll_id)
            try:
                await refresh_published_card(bot, poll_id)
            except Exception:
                logger.exception("Ошибка фонового обновления опроса #%d", poll_id)
            if poll_id not in _refresh_dirty:
                break
            await asyncio.sleep(0.5)
    finally:
        _refresh_jobs.pop(poll_id, None)
        if poll_id in _refresh_dirty:
            _refresh_jobs[poll_id] = asyncio.create_task(_refresh_loop(bot, poll_id, 0.2))


def schedule_refresh(bot: Bot, poll_id: int, delay: float = 0.6) -> None:
    """Запросить обновление карточки с дебаунсом (защита от flood limit)."""
    _refresh_dirty.add(poll_id)
    job = _refresh_jobs.get(poll_id)
    if job is None or job.done():
        _refresh_jobs[poll_id] = asyncio.create_task(_refresh_loop(bot, poll_id, delay))


async def delete_published_message(bot: Bot, poll: Poll) -> None:
    _full_caption_polls.discard(poll.id)
    if not poll.chat_id or not poll.message_id:
        return
    try:
        await bot.delete_message(poll.chat_id, poll.message_id)
    except Exception:
        logger.debug(
            "Не удалось удалить сообщение опроса #%d (возможно, уже удалено)",
            poll.id,
        )


async def notify_user(
    bot: Bot,
    user_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    try:
        await bot.send_message(
            user_id,
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
        return True
    except Exception:
        logger.debug("Не удалось отправить уведомление пользователю %d", user_id)
        return False


async def notify_booking_removed(
    bot: Bot,
    poll: Poll,
    option_title: str,
    bookings: list[Booking],
) -> None:
    text = (
        "ℹ️ <b>Ваш вариант удалён</b>\n\n"
        f"Организатор удалил вариант «{ui.esc(option_title)}» в опросе "
        f"«{ui.esc(poll.title)}».\n\n"
        "Вы можете выбрать другой свободный вариант в карточке опроса."
    )
    for booking in bookings:
        await notify_user(bot, booking.user_id, text)


async def expiry_watcher(bot: Bot, interval: int = 30) -> None:
    """Фоновый обработчик истечения срока опросов."""
    while True:
        try:
            expired_ids = db.expire_due_polls()
            for poll_id in expired_ids:
                schedule_refresh(bot, poll_id, delay=0.2)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка проверки сроков опросов")
        await asyncio.sleep(interval)


async def send_long_text(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Отправить длинный текст несколькими сообщениями (лимит 4096)."""
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.split("\n"):
        if current_len + len(line) + 1 > 3800 and current:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line) + 1
        else:
            current.append(line)
            current_len += len(line) + 1
    if current:
        chunks.append("\n".join(current))

    for index, chunk in enumerate(chunks):
        markup = reply_markup if index == len(chunks) - 1 else None
        await bot.send_message(
            chat_id,
            chunk,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )


async def send_file_preview(
    bot: Bot,
    chat_id: int,
    image: bytes | None,
    caption: str,
    keyboard: InlineKeyboardMarkup,
) -> Message:
    if image:
        try:
            return await bot.send_photo(
                chat_id=chat_id,
                photo=BufferedInputFile(image, filename="poll_preview.png"),
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        except TelegramBadRequest as exc:
            logger.warning("Не удалось отправить предпросмотр: %s", exc)
    return await bot.send_message(
        chat_id,
        caption,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
