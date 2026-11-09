"""Обработчики Telegram для модуля опросов.

Роутер использует только префикс callback-данных ``poll:`` и собственные
команды, поэтому не пересекается с обработчиками расписания (``day:``/``week:``).
"""

from __future__ import annotations

import datetime
import logging
import re
import zoneinfo

from aiogram import Bot, F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    EphemeralMessageParameters,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)

from schedule_bot import config
from schedule_bot.polls import db, service, ui
from schedule_bot.polls.models import STATUS_ACTIVE, PollView
from schedule_bot.polls.states import CreatePoll, ManagePoll

logger = logging.getLogger(__name__)
router = Router(name="polls_router")

_TZ = zoneinfo.ZoneInfo(config.TIMEZONE)
_OPTION_PREFIX_RE = re.compile(r"^(?:[-–—•]\s*|\d{1,2}[.)]\s*)")


# ── Вспомогательные функции ────────────────────────────────

def _is_group(chat_type: str | None) -> bool:
    return chat_type in (ChatType.GROUP, ChatType.SUPERGROUP)


def _display_name(user: User) -> str:
    return user.full_name or user.username or "Участник"


def _record_chat(chat) -> None:
    if chat and _is_group(chat.type):
        db.record_chat(
            chat.id,
            chat.title or f"Чат {chat.id}",
            chat.type,
            getattr(chat, "username", None),
            bool(getattr(chat, "is_forum", False)),
        )


def _cancel_keyboard(back_callback: str | None = None) -> InlineKeyboardMarkup:
    row = [ui.btn("❌ Отмена", back_callback or "poll:w:cancel")]
    return InlineKeyboardMarkup(inline_keyboard=[row])


def _manage_back_keyboard(poll_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[ui.btn("⬅️ Назад", f"poll:manage:{poll_id}")]]
    )


async def _delete_message(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


async def _personal(
    query: CallbackQuery,
    bot: Bot,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    """Личное сообщение пользователю: эфемерное в группе, обычное в ЛС/канале."""
    user = query.from_user
    message = query.message
    chat = message.chat if message else None

    if chat and _is_group(chat.type):
        try:
            await bot.send_message(
                chat_id=chat.id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                ephemeral_message_parameters=EphemeralMessageParameters(
                    receiver_user_id=user.id
                ),
            )
            return
        except Exception as exc:
            logger.debug("Эфемерное сообщение недоступно: %s", exc)

    try:
        await bot.send_message(
            user.id,
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except Exception:
        logger.debug("Не удалось отправить личное сообщение пользователю %d", user.id)


async def _replace_panel(
    query: CallbackQuery,
    bot: Bot,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    """Отредактировать текущее личное/эфемерное сообщение-панель."""
    message = query.message
    if message is None:
        await _personal(query, bot, text, keyboard)
        return

    if getattr(message, "ephemeral_message_id", None):
        try:
            await bot.edit_ephemeral_message_text(
                chat_id=message.chat.id,
                receiver_user_id=query.from_user.id,
                ephemeral_message_id=message.ephemeral_message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            return
        except Exception as exc:
            logger.debug("Не удалось изменить эфемерное сообщение: %s", exc)
            await _personal(query, bot, text, keyboard)
            return

    if message.chat.type == ChatType.CHANNEL:
        await _personal(query, bot, text, keyboard)
        return

    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except Exception:
        await _personal(query, bot, text, keyboard)


async def _edit_or_send(
    message: Message,
    bot: Bot,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except Exception:
        await bot.send_message(
            message.chat.id,
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )


async def _redirect_to_dm(
    query: CallbackQuery,
    bot: Bot,
    payload: str,
    text: str,
) -> None:
    username = await service.get_bot_username(bot)
    url = service.deep_link(username, payload)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💬 Открыть в личном чате", url=url)]
        ]
    )
    await _personal(query, bot, text, keyboard)


async def _send_group_prompt(message: Message, bot: Bot, *, create_only: bool = False) -> None:
    chat = message.chat
    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        return
    username = await service.get_bot_username(bot)
    rows = []
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        rows.append(
            [
                InlineKeyboardButton(
                    text="➕ Создать опрос",
                    url=service.deep_link(username, f"poll_new_{chat.id}"),
                )
            ]
        )
        if not create_only:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="📋 Мои опросы",
                        url=service.deep_link(username, "poll_my"),
                    )
                ]
            )
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    text = (
        "🗳 <b>Опросы с бронированием</b>\n\n"
        "Создание и управление опросами выполняются в личном чате с ботом."
    )
    try:
        await bot.send_message(
            chat_id=chat.id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            ephemeral_message_parameters=EphemeralMessageParameters(receiver_user_id=user_id),
        )
    except Exception:
        try:
            await bot.send_message(
                user_id, text, parse_mode=ParseMode.HTML, reply_markup=keyboard
            )
        except Exception:
            logger.debug("Не удалось отправить подсказку в группе %s", chat.id)


def _parse_option_lines(text: str) -> list[str]:
    result: list[str] = []
    for raw in text.splitlines():
        line = _OPTION_PREFIX_RE.sub("", raw.strip()).strip()
        if line:
            result.append(line[:80])
    return result


def _compose_expires_at(days: int | None) -> str | None:
    if not days:
        return None
    moment = datetime.datetime.now(tz=_TZ) + datetime.timedelta(days=int(days))
    return moment.isoformat(timespec="seconds")


def _option_number(view: PollView, option_id: int) -> int:
    for index, item in enumerate(view.options, 1):
        if item.option.id == option_id:
            return index
    return 0


def _chat_title_for(poll) -> str:
    if not poll.chat_id:
        return "не опубликован"
    target = db.get_chat_target(poll.chat_id)
    return target.title if target else f"Чат {poll.chat_id}"


def _manage_artifacts(view: PollView) -> tuple[str, InlineKeyboardMarkup]:
    return ui.manage_text(view, _chat_title_for(view.poll)), ui.manage_keyboard(view.poll)


# ── Команды ────────────────────────────────────────────────

@router.message(Command("polls"))
async def cmd_polls(message: Message, bot: Bot, state: FSMContext) -> None:
    _record_chat(message.chat)
    if _is_group(message.chat.type):
        await _delete_message(message)
        await _send_group_prompt(message, bot)
        return
    await state.clear()
    await message.answer(ui.main_menu_text(), reply_markup=ui.main_menu_keyboard())


@router.message(Command("newpoll"))
async def cmd_newpoll(message: Message, bot: Bot, state: FSMContext) -> None:
    _record_chat(message.chat)
    if _is_group(message.chat.type):
        await _delete_message(message)
        await _send_group_prompt(message, bot, create_only=True)
        return
    await _begin_create(bot, message.chat.id, state, target_chat_id=None)


@router.message(Command("cancel"), F.chat.type == ChatType.PRIVATE)
async def cmd_cancel(message: Message, bot: Bot, state: FSMContext) -> None:
    current = await state.get_state()
    data = await state.get_data()
    preview_id = data.get("preview_message_id")
    await state.clear()
    if preview_id:
        try:
            await bot.delete_message(message.chat.id, preview_id)
        except Exception:
            pass
    text = "Действие отменено." if current else "Нет активного действия."
    await message.answer(text, reply_markup=ui.main_menu_keyboard())


# ── Главное меню ───────────────────────────────────────────

@router.callback_query(F.data == "poll:menu")
async def on_menu(query: CallbackQuery, bot: Bot) -> None:
    await query.answer()
    if query.message and _is_group(query.message.chat.type):
        await _redirect_to_dm(query, bot, "poll_menu", "🗳 Меню опросов доступно в личном чате.")
        return
    await _replace_panel(query, bot, ui.main_menu_text(), ui.main_menu_keyboard())


@router.callback_query(F.data == "poll:help")
async def on_help(query: CallbackQuery, bot: Bot) -> None:
    await query.answer()
    if query.message and _is_group(query.message.chat.type):
        await _redirect_to_dm(query, bot, "poll_menu", "ℹ️ Подробная справка — в личном чате.")
        return
    await _replace_panel(query, bot, ui.help_text(), ui.main_menu_keyboard())


@router.callback_query(F.data == "poll:new")
async def on_new_poll(query: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    await query.answer()
    if query.message and _is_group(query.message.chat.type):
        await _redirect_to_dm(
            query,
            bot,
            f"poll_new_{query.message.chat.id}",
            "➕ Создание опроса продолжится в личном чате. Ссылка на эту группу сохранится.",
        )
        return
    await state.clear()
    await _begin_create(
        bot,
        query.from_user.id,
        state,
        target_chat_id=None,
        edit_message=query.message,
    )


@router.callback_query(F.data == "poll:mine")
async def on_my_polls(query: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    await query.answer()
    if query.message and _is_group(query.message.chat.type):
        await _redirect_to_dm(
            query, bot, "poll_my", "📋 Список ваших опросов доступен в личном чате."
        )
        return
    await state.clear()
    await _show_my_polls(bot, query.from_user.id, edit_query=query)


async def _show_my_polls(
    bot: Bot,
    user_id: int,
    *,
    edit_query: CallbackQuery | None = None,
) -> None:
    polls = db.list_polls_by_creator(user_id)
    if polls:
        text = "📋 <b>Мои опросы</b>\n\nВыберите опрос для управления:"
    else:
        text = (
            "📋 <b>Мои опросы</b>\n\n"
            "У вас пока нет созданных опросов.\n"
            "Нажмите «➕ Создать опрос», чтобы создать первый."
        )
    keyboard = ui.my_polls_keyboard(polls)
    if edit_query is not None:
        await _replace_panel(edit_query, bot, text, keyboard)
    else:
        await bot.send_message(
            user_id, text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )


@router.callback_query(F.data == "poll:noop")
async def on_noop(query: CallbackQuery) -> None:
    await query.answer()


# ── Бронирование ───────────────────────────────────────────

async def _book_and_confirm(
    query: CallbackQuery,
    bot: Bot,
    poll_id: int,
    option_id: int,
    *,
    from_picker: bool,
) -> None:
    view_before = db.get_view(poll_id)
    result = db.book(
        poll_id,
        option_id,
        query.from_user.id,
        _display_name(query.from_user),
        query.from_user.username,
    )

    if result.code in ("booked", "changed"):
        service.schedule_refresh(bot, poll_id)
        view_after = db.get_view(poll_id)
        option_title = result.option.title if result.option else "выбранный вариант"
        number = _option_number(view_after, option_id) if view_after else 0

        if result.code == "changed" and view_before:
            previous = view_before.find_user_booking(query.from_user.id)
            old_text = ""
            if previous:
                old_number = _option_number(view_before, previous[0].option.id)
                old_text = f"\nБыло: №{old_number} — {ui.esc(previous[0].option.title)}"
            text = (
                "✅ <b>Выбор изменён!</b>\n\n"
                f"Стало: №{number} — {ui.esc(option_title)}"
                f"{old_text}\n\n"
                "Старый вариант освобождён для других участников."
            )
        else:
            text = (
                "✅ <b>Ваш выбор успешно сохранён!</b>\n\n"
                f"Ваша тема: №{number} — {ui.esc(option_title)}.\n\n"
                "Теперь этот вариант закреплён за вами.\n"
                "Другие участники увидят, что тема занята."
            )

        if from_picker:
            await _replace_panel(query, bot, text)
        else:
            await _personal(query, bot, text)
        return

    if result.code in ("full", "conflict", "inactive", "expired", "option_not_found"):
        service.schedule_refresh(bot, poll_id)

    messages = {
        "same": "ℹ️ Этот вариант уже выбран за вами.",
        "full": "⚠️ Вариант уже занят. Карточка обновлена — выберите другой.",
        "conflict": "⚠️ Вариант только что занял другой участник. Попробуйте другой.",
        "inactive": "🔒 Опрос закрыт. Новые выборы недоступны.",
        "expired": "⌛ Срок действия опроса истёк.",
        "change_forbidden": "⛔ Организатор запретил изменять выбор.",
        "not_found": "⚠️ Опрос больше не существует.",
        "option_not_found": "⚠️ Этот вариант больше не существует. Карточка обновлена.",
    }
    await _personal(query, bot, messages.get(result.code, "⚠️ Не удалось сохранить выбор."))


@router.callback_query(F.data.startswith("poll:pick:"))
async def on_pick(query: CallbackQuery, bot: Bot) -> None:
    await query.answer()
    parts = (query.data or "").split(":")
    try:
        poll_id, option_id = int(parts[2]), int(parts[3])
    except (IndexError, ValueError):
        return
    await _book_and_confirm(query, bot, poll_id, option_id, from_picker=False)


@router.callback_query(F.data.startswith("poll:picksel:"))
async def on_pick_select(query: CallbackQuery, bot: Bot) -> None:
    await query.answer()
    parts = (query.data or "").split(":")
    try:
        poll_id, option_id = int(parts[2]), int(parts[3])
    except (IndexError, ValueError):
        return
    await _book_and_confirm(query, bot, poll_id, option_id, from_picker=True)


@router.callback_query(F.data.startswith("poll:change:"))
async def on_change(query: CallbackQuery, bot: Bot) -> None:
    await query.answer()
    try:
        poll_id = int((query.data or "").split(":")[2])
    except (IndexError, ValueError):
        return

    view = db.get_view(poll_id)
    if view is None:
        await _personal(query, bot, "⚠️ Опрос больше не существует.")
        return
    if view.poll.status != STATUS_ACTIVE:
        await _personal(query, bot, "🔒 Опрос закрыт. Изменение выбора недоступно.")
        service.schedule_refresh(bot, poll_id)
        return
    if not view.poll.allow_change:
        await _personal(query, bot, "⛔ Организатор запретил изменять выбор.")
        return

    current = view.find_user_booking(query.from_user.id)
    if current is None:
        await _personal(
            query,
            bot,
            "ℹ️ У вас пока нет выбранного варианта. Нажмите на свободный вариант "
            "в карточке опроса.",
        )
        return

    number = _option_number(view, current[0].option.id)
    text = (
        "🔄 <b>Изменение выбора</b>\n\n"
        f"Текущий вариант: №{number} — {ui.esc(current[0].option.title)}.\n\n"
        "Выберите новый свободный вариант. Старый освободится автоматически."
    )
    await _personal(query, bot, text, ui.picker_keyboard(view))


@router.callback_query(F.data.startswith("poll:cancel:"))
async def on_cancel_booking(query: CallbackQuery, bot: Bot) -> None:
    await query.answer()
    try:
        poll_id = int((query.data or "").split(":")[2])
    except (IndexError, ValueError):
        return

    result = db.cancel_booking(poll_id, query.from_user.id)
    if result.code == "cancelled":
        service.schedule_refresh(bot, poll_id)
        title = result.option.title if result.option else "ваш вариант"
        await _personal(
            query,
            bot,
            "✅ <b>Ваш выбор отменён.</b>\n\n"
            f"Вариант «{ui.esc(title)}» снова доступен другим участникам.",
        )
        return

    if result.code in ("inactive", "expired"):
        service.schedule_refresh(bot, poll_id)

    messages = {
        "no_booking": "ℹ️ У вас нет бронирования в этом опросе.",
        "inactive": "🔒 Опрос закрыт. Отмена выбора недоступна.",
        "expired": "⌛ Срок действия опроса истёк.",
        "cancel_forbidden": "⛔ Организатор запретил отмену выбора.",
        "not_found": "⚠️ Опрос больше не существует.",
    }
    await _personal(query, bot, messages.get(result.code, "⚠️ Не удалось отменить выбор."))


@router.callback_query(F.data.startswith("poll:refresh:"))
async def on_refresh(query: CallbackQuery, bot: Bot) -> None:
    await query.answer()
    try:
        poll_id = int((query.data or "").split(":")[2])
    except (IndexError, ValueError):
        return

    if query.message and getattr(query.message, "ephemeral_message_id", None):
        await _replace_panel(
            query,
            bot,
            "✅ Карточка опроса обновлена.",
        )

    await service.refresh_published_card(bot, poll_id, force=True)


# ── Мои опросы: управление ─────────────────────────────────

@router.callback_query(F.data.startswith("poll:manage:"))
async def on_manage(query: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    await query.answer()
    try:
        poll_id = int((query.data or "").split(":")[2])
    except (IndexError, ValueError):
        return

    if query.message and _is_group(query.message.chat.type):
        await _redirect_to_dm(
            query,
            bot,
            f"poll_manage_{poll_id}",
            "⚙️ Управление опросом доступно в личном чате с ботом.",
        )
        return

    await state.clear()
    view = db.get_view(poll_id)
    if view is None:
        await _replace_panel(
            query, bot, "⚠️ Опрос не найден.", ui.main_menu_keyboard()
        )
        return
    if view.poll.creator_id != query.from_user.id:
        await _personal(query, bot, "⛔ Это чужой опрос. Управлять им нельзя.")
        return

    text, keyboard = _manage_artifacts(view)
    await _replace_panel(query, bot, text, keyboard)


@router.callback_query(F.data.startswith("poll:m:"))
async def on_manage_action(query: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    await query.answer()
    parts = (query.data or "").split(":")
    if len(parts) < 4:
        return
    action = parts[2]
    try:
        poll_id = int(parts[3])
        option_id = int(parts[4]) if len(parts) > 4 else None
    except ValueError:
        return

    if query.message and _is_group(query.message.chat.type):
        await _redirect_to_dm(
            query,
            bot,
            f"poll_manage_{poll_id}",
            "⚙️ Управление опросом доступно в личном чате с ботом.",
        )
        return

    view = db.get_view(poll_id)
    if view is None:
        await _replace_panel(query, bot, "⚠️ Опрос не найден.", ui.main_menu_keyboard())
        return
    poll = view.poll
    if poll.creator_id != query.from_user.id:
        await _personal(query, bot, "⛔ Это чужой опрос. Управлять им нельзя.")
        return

    if action == "results":
        await service.send_long_text(
            bot,
            query.from_user.id,
            ui.results_text(view),
            ui.results_keyboard(poll_id),
        )
        return

    if action == "refresh":
        if poll.message_id:
            ok = await service.refresh_published_card(bot, poll_id, force=True)
            error = "Не удалось обновить карточку."
        else:
            ok, error = await service.publish_poll(bot, poll_id)
        if ok:
            await _personal(query, bot, "✅ Карточка опроса обновлена.")
        else:
            await _personal(query, bot, f"⚠️ {error}")
        return

    if action == "rename":
        await state.set_state(ManagePoll.rename)
        await state.update_data(poll_id=poll_id)
        await _replace_panel(
            query,
            bot,
            "✏️ <b>Новое название опроса</b>\n\nОтправьте название одним сообщением (до 128 символов).",
            _manage_back_keyboard(poll_id),
        )
        return

    if action == "add":
        await state.set_state(ManagePoll.add_options)
        await state.update_data(poll_id=poll_id)
        await _replace_panel(
            query,
            bot,
            "➕ <b>Новые варианты</b>\n\nОтправьте варианты — каждый с новой строки. "
            f"Можно добавить ещё {config.POLL_MAX_OPTIONS - len(view.options)}.",
            _manage_back_keyboard(poll_id),
        )
        return

    if action == "delmenu":
        await _replace_panel(
            query,
            bot,
            "🗑 <b>Удаление варианта</b>\n\nВыберите вариант. Свободные удаляются сразу, "
            "для занятых потребуется подтверждение.",
            ui.options_manage_keyboard(
                view, action="del", back_callback=f"poll:manage:{poll_id}"
            ),
        )
        return

    if action == "del" and option_id is not None:
        result = db.delete_option(poll_id, query.from_user.id, option_id)
        if result.code == "needs_confirm":
            await _replace_panel(
                query,
                bot,
                f"⚠️ <b>Вариант «{ui.esc(result.option_title)}» забронирован "
                f"{len(result.bookings)} участниками.</b>\n\n"
                "Удаление отменит их бронирования. Продолжить?",
                ui.confirm_option_delete_keyboard(
                    poll_id, option_id, bookings=len(result.bookings)
                ),
            )
        elif result.code == "deleted":
            service.schedule_refresh(bot, poll_id)
            fresh = db.get_view(poll_id)
            text, keyboard = (
                _manage_artifacts(fresh) if fresh else ("Опрос не найден.", ui.main_menu_keyboard())
            )
            await _replace_panel(
                query, bot, f"🗑 Вариант «{ui.esc(result.option_title)}» удалён.\n\n{text}", keyboard
            )
        else:
            await _personal(query, bot, _delete_option_error(result.code))
        return

    if action == "delyes" and option_id is not None:
        result = db.delete_option(poll_id, query.from_user.id, option_id, force=True)
        if result.code == "deleted":
            await service.notify_booking_removed(
                bot, poll, result.option_title, result.bookings
            )
            service.schedule_refresh(bot, poll_id)
            fresh = db.get_view(poll_id)
            text, keyboard = (
                _manage_artifacts(fresh) if fresh else ("Опрос не найден.", ui.main_menu_keyboard())
            )
            await _replace_panel(
                query,
                bot,
                f"🗑 Вариант «{ui.esc(result.option_title)}» удалён. "
                f"Отменено бронирований: {len(result.bookings)}.\n\n{text}",
                keyboard,
            )
        else:
            await _personal(query, bot, _delete_option_error(result.code))
        return

    if action == "limmenu":
        await _replace_panel(
            query,
            bot,
            "⚙️ <b>Лимит участников</b>\n\nВыберите вариант, чтобы изменить "
            "максимальное количество участников.",
            ui.options_manage_keyboard(
                view, action="lim", back_callback=f"poll:manage:{poll_id}"
            ),
        )
        return

    if action == "lim" and option_id is not None:
        option_view = view.find_option(option_id)
        if option_view is None:
            await _personal(query, bot, "⚠️ Вариант не найден.")
            return
        await _replace_panel(
            query,
            bot,
            f"⚙️ <b>{ui.esc(option_view.option.title)}</b>\n\n"
            f"Занято: {option_view.taken}\n"
            f"Текущий лимит: {option_view.option.max_participants}",
            ui.limit_keyboard(
                poll_id,
                option_id,
                option_view.option.max_participants,
                option_view.taken,
            ),
        )
        return

    if action in ("limdec", "liminc") and option_id is not None:
        option = db.get_option(option_id)
        if option is None:
            await _personal(query, bot, "⚠️ Вариант не найден.")
            return
        new_limit = option.max_participants + (1 if action == "liminc" else -1)
        result = db.set_option_limit(poll_id, query.from_user.id, option_id, new_limit)
        if result.code == "ok":
            fresh = db.get_view(poll_id)
            option_view = fresh.find_option(option_id) if fresh else None
            taken = option_view.taken if option_view else 0
            await _replace_panel(
                query,
                bot,
                f"⚙️ <b>{ui.esc(result.option_title)}</b>\n\n"
                f"Занято: {taken}\n"
                f"Текущий лимит: {result.limit}",
                ui.limit_keyboard(poll_id, option_id, result.limit, taken),
            )
        elif result.code == "below_taken":
            await _personal(
                query,
                bot,
                f"⚠️ Нельзя установить лимит {result.limit}: уже занято {result.taken} мест.",
            )
        else:
            await _personal(query, bot, "⚠️ Не удалось изменить лимит.")
        return

    if action == "limdone":
        service.schedule_refresh(bot, poll_id)
        fresh = db.get_view(poll_id)
        text, keyboard = (
            _manage_artifacts(fresh) if fresh else ("Опрос не найден.", ui.main_menu_keyboard())
        )
        await _replace_panel(query, bot, text, keyboard)
        return

    if action in ("close", "reopen"):
        new_status = "closed" if action == "close" else STATUS_ACTIVE
        code = db.set_status(poll_id, query.from_user.id, new_status)
        if code != "ok":
            await _personal(query, bot, "⚠️ Не удалось изменить статус опроса.")
            return
        service.schedule_refresh(bot, poll_id)
        fresh = db.get_view(poll_id)
        text, keyboard = (
            _manage_artifacts(fresh) if fresh else ("Опрос не найден.", ui.main_menu_keyboard())
        )
        await _replace_panel(query, bot, text, keyboard)
        return

    if action == "copy":
        code, new_id = db.copy_poll(poll_id, query.from_user.id)
        if code != "ok":
            await _personal(query, bot, "⚠️ Не удалось создать копию.")
            return
        ok, error = await service.publish_poll(bot, new_id)
        fresh = db.get_view(new_id)
        if fresh:
            text, keyboard = _manage_artifacts(fresh)
        else:
            text, keyboard = "Копия создана.", ui.main_menu_keyboard()
        prefix = "📄 Копия создана и опубликована.\n\n" if ok else f"⚠️ {error}\n\n"
        await _replace_panel(query, bot, prefix + text, keyboard)
        return

    if action == "delete":
        await _replace_panel(
            query,
            bot,
            f"❌ <b>Удалить опрос?</b>\n\n"
            f"«{ui.esc(poll.title)}»\n"
            f"Будут удалены все бронирования: {view.total_bookings}.\n\n"
            "Это действие нельзя отменить.",
            ui.confirm_delete_keyboard(poll_id),
        )
        return

    if action == "deleteyes":
        code, deleted = db.delete_poll(poll_id, query.from_user.id)
        if code != "ok" or deleted is None:
            await _personal(query, bot, "⚠️ Не удалось удалить опрос.")
            return
        await service.delete_published_message(bot, deleted)
        await _replace_panel(
            query,
            bot,
            "🗑 <b>Опрос удалён.</b>\n\nВсе бронирования отменены.",
            ui.main_menu_keyboard(),
        )
        return


def _delete_option_error(code: str) -> str:
    if code == "min_options":
        return "⚠️ В опросе должно остаться минимум два варианта."
    if code == "option_not_found":
        return "⚠️ Вариант не найден."
    if code == "forbidden":
        return "⛔ Это чужой опрос."
    return "⚠️ Не удалось удалить вариант."


# ── Управление: ввод текста ────────────────────────────────

@router.message(ManagePoll.rename, F.chat.type == ChatType.PRIVATE)
async def on_rename_input(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not 1 <= len(title) <= 128:
        await message.answer("⚠️ Название должно быть от 1 до 128 символов. Попробуйте ещё раз.")
        return

    data = await state.get_data()
    poll_id = int(data.get("poll_id", 0))
    code = db.update_poll_settings(poll_id, message.from_user.id, title=title)
    await state.clear()
    if code != "ok":
        await message.answer("⚠️ Не удалось изменить название.")
        return

    service.schedule_refresh(message.bot, poll_id)
    view = db.get_view(poll_id)
    if view:
        text, keyboard = _manage_artifacts(view)
        await message.answer(f"✅ Название изменено.\n\n{text}", reply_markup=keyboard)
    else:
        await message.answer("Опрос не найден.")


@router.message(ManagePoll.add_options, F.chat.type == ChatType.PRIVATE)
async def on_add_options_input(message: Message, state: FSMContext) -> None:
    titles = _parse_option_lines(message.text or "")
    if not titles:
        await message.answer("⚠️ Не найдено ни одного варианта. Отправьте варианты с новой строки.")
        return

    data = await state.get_data()
    poll_id = int(data.get("poll_id", 0))
    view = db.get_view(poll_id)
    if view is None:
        await state.clear()
        await message.answer("⚠️ Опрос не найден.")
        return
    if view.poll.creator_id != message.from_user.id:
        await state.clear()
        await message.answer("⛔ Это чужой опрос.")
        return

    remaining = config.POLL_MAX_OPTIONS - len(view.options)
    if remaining <= 0:
        await state.clear()
        await message.answer(
            f"⚠️ Достигнут максимум {config.POLL_MAX_OPTIONS} вариантов."
        )
        return
    titles = titles[:remaining]

    code, added = db.add_options(poll_id, message.from_user.id, titles)
    await state.clear()
    if code != "ok":
        await message.answer("⚠️ Не удалось добавить варианты.")
        return

    service.schedule_refresh(message.bot, poll_id)
    fresh = db.get_view(poll_id)
    if fresh:
        text, keyboard = _manage_artifacts(fresh)
        await message.answer(f"✅ Добавлено вариантов: {added}.\n\n{text}", reply_markup=keyboard)


# ── Создание опроса ────────────────────────────────────────

async def _begin_create(
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    *,
    target_chat_id: int | None,
    edit_message: Message | None = None,
) -> None:
    await state.clear()
    data = {
        "options": [],
        "default_max": config.POLL_DEFAULT_MAX_PARTICIPANTS,
        "allow_change": True,
        "allow_cancel": True,
        "show_participants": True,
        "expiry_days": None,
        "target_chat_id": None,
        "chat_title": "",
    }
    if target_chat_id is not None:
        target = await service.resolve_chat(bot, target_chat_id)
        if target and _is_group(target.chat_type):
            data["target_chat_id"] = target.chat_id
            data["chat_title"] = target.title
            db.record_chat(
                target.chat_id,
                target.title,
                target.chat_type,
                target.username,
                False,
            )

    await state.set_data(data)
    await state.set_state(CreatePoll.title)
    text = (
        "➕ <b>Создание опроса</b>\n\n"
        "Шаг 1 из 3. Отправьте название опроса или вопрос (до 128 символов)."
    )
    keyboard = _cancel_keyboard()
    if edit_message is not None:
        await _edit_or_send(edit_message, bot, text, keyboard)
    else:
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


@router.message(CreatePoll.title, F.chat.type == ChatType.PRIVATE)
async def on_title_input(message: Message, bot: Bot, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not 1 <= len(title) <= 128:
        await message.answer("⚠️ Название должно быть от 1 до 128 символов. Попробуйте ещё раз.")
        return
    await state.update_data(title=title)
    await state.set_state(CreatePoll.description)
    await message.answer(
        "Шаг 2 из 3. Добавьте описание или пропустите этот шаг (до 500 символов).",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [ui.btn("⏭ Пропустить", "poll:w:descskip")],
                [ui.btn("❌ Отмена", "poll:w:cancel")],
            ]
        ),
    )


@router.message(CreatePoll.description, F.chat.type == ChatType.PRIVATE)
async def on_description_input(message: Message, state: FSMContext) -> None:
    description = (message.text or "").strip()
    if len(description) > 500:
        await message.answer("⚠️ Описание не должно превышать 500 символов.")
        return
    await state.update_data(description=description)
    await _ask_options(message, state)


async def _ask_options(message: Message, state: FSMContext) -> None:
    await state.set_state(CreatePoll.options)
    await message.answer(
        "Шаг 3 из 3. Отправьте варианты — каждый с новой строки.\n\n"
        f"Минимум 2, максимум {config.POLL_MAX_OPTIONS}. "
        "Например:\n\nИскусственный интеллект\nКибербезопасность\nКвантовые компьютеры",
        reply_markup=_cancel_keyboard(),
    )


@router.message(CreatePoll.options, F.chat.type == ChatType.PRIVATE)
async def on_options_input(message: Message, state: FSMContext) -> None:
    titles = _parse_option_lines(message.text or "")
    if len(titles) < 2:
        await message.answer("⚠️ Нужно минимум два варианта. Отправьте список ещё раз.")
        return
    if len(titles) > config.POLL_MAX_OPTIONS:
        await message.answer(
            f"⚠️ Максимум {config.POLL_MAX_OPTIONS} вариантов. Сейчас {len(titles)}."
        )
        return
    lowered = [title.lower() for title in titles]
    if len(set(lowered)) != len(lowered):
        await message.answer("⚠️ Варианты не должны повторяться. Отправьте список ещё раз.")
        return

    await state.update_data(options=titles)
    await state.set_state(CreatePoll.settings)
    _, text, keyboard = await _settings_payload(state)
    await message.answer(text, reply_markup=keyboard)


async def _settings_payload(state: FSMContext) -> tuple[dict, str, InlineKeyboardMarkup]:
    data = await state.get_data()
    chats = db.list_chat_targets()
    text = ui.wizard_settings_text(data, data.get("chat_title", ""))
    keyboard = ui.wizard_settings_keyboard(data, chats)
    return data, text, keyboard


async def _show_settings(
    query: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    *,
    previous_state: str | None,
) -> None:
    await state.set_state(CreatePoll.settings)
    _, text, keyboard = await _settings_payload(state)
    if previous_state == CreatePoll.preview.state and query.message:
        try:
            await query.message.delete()
        except Exception:
            pass
        await bot.send_message(
            query.from_user.id, text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
    else:
        await _replace_panel(query, bot, text, keyboard)


@router.callback_query(F.data.startswith("poll:w:"))
async def on_wizard(query: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    await query.answer()
    action = (query.data or "")[7:]
    previous_state = await state.get_state()
    data = await state.get_data()

    if action == "cancel":
        preview_id = data.get("preview_message_id")
        await state.clear()
        if preview_id and query.message:
            try:
                await bot.delete_message(query.message.chat.id, preview_id)
            except Exception:
                pass
        await _replace_panel(
            query,
            bot,
            "❌ Создание опроса отменено.",
            ui.main_menu_keyboard(),
        )
        return

    if action == "descskip":
        if previous_state != CreatePoll.description.state:
            return
        await state.update_data(description="")
        await state.set_state(CreatePoll.options)
        await _replace_panel(
            query,
            bot,
            "Шаг 3 из 3. Отправьте варианты — каждый с новой строки.\n\n"
            f"Минимум 2, максимум {config.POLL_MAX_OPTIONS}.",
            _cancel_keyboard(),
        )
        return

    if not data.get("title") and action not in ("restart",):
        await state.clear()
        await _replace_panel(
            query,
            bot,
            "⌛ Сессия создания истекла. Начните заново.",
            ui.main_menu_keyboard(),
        )
        return

    if action == "noop":
        return

    if action == "lim:+":
        data["default_max"] = min(50, int(data.get("default_max", 1)) + 1)
        await state.update_data(default_max=data["default_max"])
        await _show_settings(query, bot, state, previous_state=previous_state)
        return

    if action == "lim:-":
        data["default_max"] = max(1, int(data.get("default_max", 1)) - 1)
        await state.update_data(default_max=data["default_max"])
        await _show_settings(query, bot, state, previous_state=previous_state)
        return

    if action.startswith("tgl:"):
        key = action[4:]
        mapping = {
            "change": "allow_change",
            "cancel": "allow_cancel",
            "show": "show_participants",
        }
        field = mapping.get(key)
        if field:
            data[field] = not bool(data.get(field, True))
            await state.update_data(**{field: data[field]})
            await _show_settings(query, bot, state, previous_state=previous_state)
        return

    if action.startswith("exp:"):
        value = action[4:]
        if value == "none":
            await state.update_data(expiry_days=None)
        elif value.isdigit():
            await state.update_data(expiry_days=int(value))
        await _show_settings(query, bot, state, previous_state=previous_state)
        return

    if action == "chats":
        chats = db.list_chat_targets()
        current = data.get("chat_title") or "не выбран"
        text = (
            "📍 <b>Куда опубликовать опрос</b>\n\n"
            f"Текущий выбор: {ui.esc(current)}\n\n"
            "Выберите группу из списка или укажите чат вручную."
        )
        await _replace_panel(query, bot, text, ui.wizard_chats_keyboard(chats))
        return

    if action.startswith("chat:"):
        try:
            chat_id = int(action.split(":", 1)[1])
        except ValueError:
            return
        target = await service.resolve_chat(bot, chat_id)
        if target is None or not _is_group(target.chat_type):
            await _personal(query, bot, "⚠️ Не удалось использовать этот чат.")
            return
        await state.update_data(target_chat_id=target.chat_id, chat_title=target.title)
        await _show_settings(query, bot, state, previous_state=previous_state)
        return

    if action == "chatmanual":
        await state.set_state(CreatePoll.manual_chat)
        await _replace_panel(
            query,
            bot,
            "🔗 <b>Укажите чат</b>\n\n"
            "Отправьте @username группы или её числовой chat_id. "
            "Бот должен быть участником этой группы.",
            InlineKeyboardMarkup(
                inline_keyboard=[[ui.btn("⬅️ Назад", "poll:w:settings")]]
            ),
        )
        return

    if action == "settings":
        await _show_settings(query, bot, state, previous_state=previous_state)
        return

    if action == "preview":
        if not data.get("target_chat_id"):
            await _personal(
                query,
                bot,
                "📍 Сначала выберите группу, в которой нужно опубликовать опрос.",
                InlineKeyboardMarkup(
                    inline_keyboard=[[ui.btn("📍 Выбрать чат", "poll:w:chats")]]
                ),
            )
            return
        if len(data.get("options", [])) < 2:
            await _personal(query, bot, "⚠️ Добавьте минимум два варианта.")
            return

        image, caption = await service.render_draft_preview(bot, data)
        await state.set_state(CreatePoll.preview)
        if query.message:
            try:
                await query.message.delete()
            except Exception:
                pass
        try:
            message = await service.send_file_preview(
                bot,
                query.from_user.id,
                image,
                caption,
                ui.wizard_preview_keyboard(),
            )
        except Exception:
            logger.exception("Не удалось показать предпросмотр")
            await _personal(query, bot, "⚠️ Не удалось показать предпросмотр. Попробуйте ещё раз.")
            await state.set_state(CreatePoll.settings)
            return
        await state.update_data(preview_message_id=message.message_id)
        return

    if action == "publish":
        if not data.get("target_chat_id"):
            await _personal(query, bot, "⚠️ Чат не выбран.")
            return
        await _publish_from_draft(query, bot, state, data)
        return

    if action == "restart":
        await _begin_create(
            bot,
            query.from_user.id,
            state,
            target_chat_id=data.get("target_chat_id"),
            edit_message=query.message,
        )
        return


async def _publish_from_draft(
    query: CallbackQuery,
    bot: Bot,
    state: FSMContext,
    data: dict,
) -> None:
    options = data.get("options", [])
    limit = max(1, int(data.get("default_max", 1)))
    try:
        poll_id = db.create_poll(
            creator_id=query.from_user.id,
            title=data.get("title", ""),
            description=data.get("description", ""),
            chat_id=int(data["target_chat_id"]),
            options=[(title, limit) for title in options],
            default_max=limit,
            allow_change=bool(data.get("allow_change", True)),
            allow_cancel=bool(data.get("allow_cancel", True)),
            show_participants=bool(data.get("show_participants", True)),
            expires_at=_compose_expires_at(data.get("expiry_days")),
        )
    except Exception as exc:
        logger.exception("Не удалось создать опрос")
        await _personal(query, bot, f"⚠️ Не удалось создать опрос: {ui.esc(str(exc))}")
        return

    ok, error = await service.publish_poll(bot, poll_id)
    if not ok:
        db.delete_poll(poll_id, query.from_user.id)
        await _personal(
            query,
            bot,
            f"⚠️ {ui.esc(error)}\n\n"
            "Проверьте, что бот добавлен в группу и имеет право писать сообщения.",
            ui.wizard_preview_keyboard(),
        )
        return

    preview_id = data.get("preview_message_id")
    if preview_id:
        try:
            await bot.delete_message(query.from_user.id, preview_id)
        except Exception:
            pass

    chat_title = data.get("chat_title") or "выбранной группе"
    await state.clear()
    text = (
        "✅ <b>Опрос опубликован!</b>\n\n"
        f"«{ui.esc(data.get('title', ''))}»\n"
        f"Группа: {ui.esc(chat_title)}\n\n"
        "Участники уже могут выбирать свободные варианты. "
        "Управление доступно в «Мои опросы»."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [ui.btn("⚙️ Управление опросом", f"poll:manage:{poll_id}")],
            [ui.btn("📋 Мои опросы", "poll:mine")],
        ]
    )
    await bot.send_message(
        query.from_user.id, text, parse_mode=ParseMode.HTML, reply_markup=keyboard
    )


@router.message(CreatePoll.manual_chat, F.chat.type == ChatType.PRIVATE)
async def on_manual_chat(message: Message, bot: Bot, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    target = None
    if re.fullmatch(r"-?\d+", raw):
        target = await service.resolve_chat(bot, int(raw))
    elif raw.startswith("@") or raw:
        try:
            chat = await bot.get_chat(raw)
            if _is_group(chat.type):
                db.record_chat(
                    chat.id,
                    chat.title or f"Чат {chat.id}",
                    chat.type,
                    chat.username,
                    bool(getattr(chat, "is_forum", False)),
                )
                target = db.get_chat_target(chat.id)
        except TelegramAPIError as exc:
            logger.info("Чат %s не найден: %s", raw, exc)

    if target is None or not _is_group(target.chat_type):
        await message.answer(
            "⚠️ Чат не найден или бот не является его участником. "
            "Проверьте @username / chat_id и попробуйте снова."
        )
        return

    await state.update_data(target_chat_id=target.chat_id, chat_title=target.title)
    await state.set_state(CreatePoll.settings)
    _, text, keyboard = await _settings_payload(state)
    await message.answer(text, reply_markup=keyboard)


# ── Deep links (/start poll_...) ───────────────────────────

async def handle_deeplink(message: Message, bot: Bot, state: FSMContext) -> bool:
    """Обработать payload команды /start. Возвращает True, если payload наш."""
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return False
    payload = parts[1].strip()
    if not payload.startswith("poll"):
        return False
    if message.chat.type != ChatType.PRIVATE:
        return False

    user = message.from_user
    if user is None:
        return True

    if payload == "poll_my":
        await state.clear()
        await _show_my_polls(bot, user.id)
        return True

    if payload == "poll_menu":
        await state.clear()
        await bot.send_message(
            user.id,
            ui.main_menu_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=ui.main_menu_keyboard(),
        )
        return True

    if payload.startswith("poll_manage_"):
        try:
            poll_id = int(payload.rsplit("_", 1)[1])
        except ValueError:
            return True
        view = db.get_view(poll_id)
        if view is None or view.poll.creator_id != user.id:
            await bot.send_message(user.id, "⛔ Опрос не найден или принадлежит другому пользователю.")
            return True
        text, keyboard = _manage_artifacts(view)
        await bot.send_message(
            user.id, text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
        return True

    if payload == "poll_new":
        await _begin_create(bot, user.id, state, target_chat_id=None)
        return True

    if payload.startswith("poll_new_"):
        raw_chat = payload[len("poll_new_") :]
        try:
            chat_id = int(raw_chat)
        except ValueError:
            chat_id = None
        await _begin_create(bot, user.id, state, target_chat_id=chat_id)
        return True

    await _begin_create(bot, user.id, state, target_chat_id=None)
    return True
