"""Тексты и inline-клавиатуры модуля опросов (полностью на русском)."""

from __future__ import annotations

import datetime
import html
import zoneinfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from schedule_bot import config
from schedule_bot.polls.models import (
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    Booking,
    ChatTarget,
    Poll,
    PollView,
)

_TZ = zoneinfo.ZoneInfo(config.TIMEZONE)
CAPTION_LIMIT = 1000
TEXT_LIMIT = 3900


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _clip(text: str, limit: int = 58) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def format_dt(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_TZ)
    return parsed.astimezone(_TZ).strftime("%d.%m.%Y %H:%M")


def status_label(poll: Poll) -> str:
    if poll.status == STATUS_ACTIVE:
        return "🟢 Активен"
    if poll.status == STATUS_EXPIRED:
        return "⌛ Срок истёк"
    return "🔒 Закрыт"


def _person(booking: Booking) -> str:
    name = esc(booking.user_name)
    if booking.username:
        return f"{name} (@{esc(booking.username)})"
    return name


def option_line(view: PollView, index: int, option_view) -> str:
    option = option_view.option
    if option_view.is_full:
        if option.max_participants > 1:
            head = f"🔴 Занято {option_view.taken}/{option.max_participants}"
        else:
            head = "🔴 Занято"
        if view.poll.show_participants and option_view.bookings:
            people = ", ".join(_person(booking) for booking in option_view.bookings)
            head = f"{head} — {people}"
    else:
        if option.max_participants > 1:
            head = f"🟢 Свободно ({option_view.taken}/{option.max_participants})"
        else:
            head = "🟢 Свободно"
    return f"{index}. <b>{esc(option.title)}</b>\n{head}"


def _poll_footer(view: PollView) -> list[str]:
    poll = view.poll
    lines = ["", f"<b>Доступно: {view.available_options} из {view.total_options} вариантов.</b>"]
    if poll.expires_at:
        lines.append(f"⏳ До {format_dt(poll.expires_at)}")
    else:
        lines.append("⏳ Бессрочно")
    if poll.status != STATUS_ACTIVE:
        if poll.status == STATUS_EXPIRED:
            lines.append("⌛ Срок действия опроса истёк.")
        else:
            lines.append("🔒 Опрос закрыт организатором.")
    return lines


def poll_caption(view: PollView, *, limit: int = CAPTION_LIMIT) -> str:
    """HTML-подпись к карточке: полный текстовый статус для доступности."""
    poll = view.poll
    parts: list[str] = [f"📚 <b>{esc(poll.title)}</b>"]
    if poll.description:
        parts.append("")
        parts.append(esc(poll.description))

    header = "\n".join(parts)
    footer = "\n".join(_poll_footer(view))
    option_lines = [option_line(view, index, item) for index, item in enumerate(view.options, 1)]

    full = "\n\n".join([header, "\n\n".join(option_lines), footer])
    if len(full) <= limit:
        return full

    compact_header = [
        f"📚 <b>{esc(_clip(poll.title, 90))}</b>",
        f"🟢 Доступно: {view.available_options} из {view.total_options}",
        f"👥 Занято мест: {view.total_bookings}",
    ]
    compact_lines: list[str] = []
    for index, item in enumerate(view.options, 1):
        mark = "🔴" if item.is_full else "🟢"
        compact_lines.append(f"{index}. {esc(_clip(item.option.title, 48))} — {mark}")

    compact_footer = "\n".join(_poll_footer(view))
    head = "\n".join(compact_header)
    while compact_lines:
        candidate = "\n\n".join([head, "\n".join(compact_lines), compact_footer])
        if len(candidate) <= limit:
            return candidate
        compact_lines.pop()

    candidate = "\n\n".join([head, "Полный список — на карточке.", compact_footer])
    return candidate[:limit]


def poll_text(view: PollView, *, limit: int = TEXT_LIMIT) -> str:
    """Полный текстовый вариант (для сообщений без фото)."""
    return poll_caption(view, limit=limit)


def poll_keyboard(view: PollView) -> InlineKeyboardMarkup:
    poll = view.poll
    rows: list[list[InlineKeyboardButton]] = []
    for index, item in enumerate(view.options, 1):
        mark = "🔒" if item.is_full else "✅"
        label = _clip(f"{index}. {item.option.title}")
        rows.append([btn(f"{label} {mark}", f"poll:pick:{poll.id}:{item.option.id}:{poll.revision}")])

    rows.append(
        [
            btn("🔄 Изменить выбор", f"poll:change:{poll.id}"),
            btn("❌ Отменить выбор", f"poll:cancel:{poll.id}"),
        ]
    )
    rows.append(
        [
            btn("🔄 Обновить", f"poll:refresh:{poll.id}"),
            btn("📋 Мои опросы", "poll:mine"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def picker_keyboard(view: PollView) -> InlineKeyboardMarkup:
    poll = view.poll
    rows: list[list[InlineKeyboardButton]] = []
    for index, item in enumerate(view.options, 1):
        mark = "🔒" if item.is_full else "✅"
        label = _clip(f"{index}. {item.option.title}")
        rows.append(
            [
                btn(
                    f"{label} {mark}",
                    f"poll:picksel:{poll.id}:{item.option.id}:{poll.revision}",
                )
            ]
        )
    rows.append([btn("⬅️ К карточке", f"poll:refresh:{poll.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Главное меню ───────────────────────────────────────────

def main_menu_text() -> str:
    return (
        "🗳 <b>Опросы с бронированием</b>\n\n"
        "Создавайте собственные опросы, публикуйте их в группе и выбирайте "
        "свободные варианты. Занятые варианты видны сразу — вместе с именами "
        "участников и их аватарками.\n\n"
        "Выберите действие:"
    )


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("➕ Создать опрос", "poll:new")],
            [btn("📋 Мои опросы", "poll:mine")],
            [btn("ℹ️ Как это работает", "poll:help")],
        ]
    )


def help_text() -> str:
    return (
        "ℹ️ <b>Как работают опросы</b>\n\n"
        "1. Создайте опрос: название, варианты и настройки.\n"
        "2. Бот опубликует карточку в выбранной группе.\n"
        "3. Нажмите на свободный вариант под карточкой.\n"
        "4. Занятые варианты помечены 🔒, свободные — ✅.\n"
        "5. Свой выбор можно изменить или отменить, если организатор разрешил.\n\n"
        "Создатель опроса может закрыть его, добавить или удалить варианты, "
        "изменить лимит участников, посмотреть результаты и сделать копию."
    )


# ── Создание опроса ───────────────────────────────────────

def wizard_settings_text(draft: dict, chat_title: str) -> str:
    options = draft.get("options", [])
    limit = int(draft.get("default_max", 1))
    allow_change = "✅ включено" if draft.get("allow_change", True) else "⛔ выключено"
    allow_cancel = "✅ включено" if draft.get("allow_cancel", True) else "⛔ выключено"
    show_people = "✅ показывать" if draft.get("show_participants", True) else "⛔ скрывать"
    expiry = draft.get("expiry_days")
    expiry_text = f"через {expiry} дн." if expiry else "бессрочно"

    return (
        "⚙️ <b>Настройки опроса</b>\n\n"
        f"<b>Название:</b> {esc(draft.get('title', ''))}\n"
        f"<b>Вариантов:</b> {len(options)}\n\n"
        f"<b>Лимит участников на вариант:</b> {limit}\n"
        f"<b>Изменение выбора:</b> {allow_change}\n"
        f"<b>Отмена выбора:</b> {allow_cancel}\n"
        f"<b>Участники в карточке:</b> {show_people}\n"
        f"<b>Срок действия:</b> {expiry_text}\n"
        f"<b>Куда опубликовать:</b> {esc(chat_title) if chat_title else 'не выбран'}"
    )


def wizard_settings_keyboard(draft: dict, chats: list[ChatTarget]) -> InlineKeyboardMarkup:
    limit = int(draft.get("default_max", 1))
    allow_change = bool(draft.get("allow_change", True))
    allow_cancel = bool(draft.get("allow_cancel", True))
    show_people = bool(draft.get("show_participants", True))
    expiry_days = draft.get("expiry_days")

    rows: list[list[InlineKeyboardButton]] = [
        [
            btn("➖", "poll:w:lim:-"),
            btn(f"Лимит: {limit}", "poll:w:noop"),
            btn("➕", "poll:w:lim:+"),
        ],
        [btn(("✅ " if allow_change else "⛔ ") + "Изменение выбора", "poll:w:tgl:change")],
        [btn(("✅ " if allow_cancel else "⛔ ") + "Отмена выбора", "poll:w:tgl:cancel")],
        [btn(("✅ " if show_people else "⛔ ") + "Показывать участников", "poll:w:tgl:show")],
    ]

    expiry_row: list[InlineKeyboardButton] = [
        btn(("✅ " if expiry_days is None else "⏳ ") + "Бессрочно", "poll:w:exp:none")
    ]
    for days in (1, 3, 7):
        expiry_row.append(
            btn(
                ("✅ " if expiry_days == days else "⏳ ") + f"{days} дн.",
                f"poll:w:exp:{days}",
            )
        )
    rows.append(expiry_row)

    chat_title = draft.get("chat_title") or "Выбрать чат"
    rows.append([btn(f"📍 {_clip(chat_title, 40)}", "poll:w:chats")])
    rows.append([btn("👁 Предпросмотр", "poll:w:preview")])
    rows.append([btn("❌ Отмена", "poll:w:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def wizard_chats_keyboard(chats: list[ChatTarget]) -> InlineKeyboardMarkup:
    rows = [
        [btn(_clip(f"📍 {chat.title}", 52), f"poll:w:chat:{chat.chat_id}")]
        for chat in chats
    ]
    rows.append([btn("🔗 Указать вручную", "poll:w:chatmanual")])
    rows.append([btn("⬅️ Назад", "poll:w:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def wizard_preview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("✅ Опубликовать", "poll:w:publish")],
            [btn("⬅️ Настройки", "poll:w:settings"), btn("❌ Отмена", "poll:w:cancel")],
        ]
    )


# ── Мои опросы и управление ───────────────────────────────

def my_polls_keyboard(polls: list[Poll]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [[btn("➕ Создать опрос", "poll:new")]]
    for poll in polls:
        icon = "🟢" if poll.status == STATUS_ACTIVE else ("⌛" if poll.status == STATUS_EXPIRED else "🔒")
        rows.append([btn(f"{icon} {_clip(poll.title, 45)}", f"poll:manage:{poll.id}")])
    rows.append([btn("⬅️ Меню", "poll:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def manage_text(view: PollView, chat_title: str) -> str:
    poll = view.poll
    expires = f"до {format_dt(poll.expires_at)}" if poll.expires_at else "бессрочно"
    return (
        f"⚙️ <b>Управление опросом</b>\n\n"
        f"<b>{esc(poll.title)}</b>\n"
        f"{status_label(poll)}\n"
        f"Участников: <b>{view.total_bookings}</b>\n"
        f"Доступно вариантов: <b>{view.available_options} из {view.total_options}</b>\n"
        f"Срок: {expires}\n"
        f"Группа: {esc(chat_title or '—')}\n\n"
        "Выберите действие:"
    )


def manage_keyboard(poll: Poll) -> InlineKeyboardMarkup:
    status_button = (
        btn("🔒 Закрыть опрос", f"poll:m:close:{poll.id}")
        if poll.status == STATUS_ACTIVE
        else btn("🔓 Возобновить опрос", f"poll:m:reopen:{poll.id}")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                btn("📊 Результаты", f"poll:m:results:{poll.id}"),
                btn("🔄 Обновить карточку", f"poll:m:refresh:{poll.id}"),
            ],
            [btn("✏️ Изменить название", f"poll:m:rename:{poll.id}")],
            [
                btn("➕ Добавить варианты", f"poll:m:add:{poll.id}"),
                btn("🗑 Удалить вариант", f"poll:m:delmenu:{poll.id}"),
            ],
            [btn("⚙️ Лимит варианта", f"poll:m:limmenu:{poll.id}")],
            [status_button],
            [
                btn("📄 Создать копию", f"poll:m:copy:{poll.id}"),
                btn("❌ Удалить опрос", f"poll:m:delete:{poll.id}"),
            ],
            [btn("⬅️ Мои опросы", "poll:mine")],
        ]
    )


def confirm_delete_keyboard(poll_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("🗑 Да, удалить опрос", f"poll:m:deleteyes:{poll_id}")],
            [btn("⬅️ Отмена", f"poll:manage:{poll_id}")],
        ]
    )


def options_manage_keyboard(
    view: PollView,
    *,
    action: str,
    back_callback: str,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index, item in enumerate(view.options, 1):
        people = f" {item.taken}/{item.option.max_participants}"
        rows.append(
            [
                btn(
                    _clip(f"{index}. {item.option.title}{people}", 52),
                    f"poll:m:{action}:{view.poll.id}:{item.option.id}",
                )
            ]
        )
    rows.append([btn("⬅️ Назад", back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_option_delete_keyboard(
    poll_id: int,
    option_id: int,
    *,
    bookings: int,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn(f"🗑 Удалить вариант ({bookings} брони)", f"poll:m:delyes:{poll_id}:{option_id}")],
            [btn("⬅️ Отмена", f"poll:m:delmenu:{poll_id}")],
        ]
    )


def limit_keyboard(poll_id: int, option_id: int, limit: int, taken: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                btn("➖", f"poll:m:limdec:{poll_id}:{option_id}"),
                btn(f"{limit} (занято {taken})", "poll:noop"),
                btn("➕", f"poll:m:liminc:{poll_id}:{option_id}"),
            ],
            [btn("✅ Готово", f"poll:m:limdone:{poll_id}")],
            [btn("⬅️ Назад", f"poll:m:limmenu:{poll_id}")],
        ]
    )


def results_text(view: PollView) -> str:
    poll = view.poll
    parts = [
        f"📊 <b>Результаты: {esc(poll.title)}</b>",
        f"{status_label(poll)} · участников: <b>{view.total_bookings}</b>",
        "",
    ]
    for index, item in enumerate(view.options, 1):
        option = item.option
        parts.append(
            f"<b>{index}. {esc(option.title)}</b> — "
            f"{item.taken}/{option.max_participants}"
        )
        if item.bookings:
            for booking in item.bookings:
                parts.append(f"   • {_person(booking)}")
        else:
            parts.append("   • пока никто не выбрал")
        parts.append("")
    return "\n".join(parts).strip()


def results_keyboard(poll_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("🔄 Обновить", f"poll:m:results:{poll_id}")],
            [btn("⬅️ Управление", f"poll:manage:{poll_id}")],
        ]
    )
