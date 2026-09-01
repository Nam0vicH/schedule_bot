"""Форматирование расписания для Telegram-сообщений (aiogram 3.x)."""

from __future__ import annotations

import datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from schedule_bot.models import DaySchedule, Lesson

# Эмодзи для номеров пар
_NUM_EMOJI = {
    1: "1️⃣",
    2: "2️⃣",
    3: "3️⃣",
    4: "4️⃣",
    5: "5️⃣",
    6: "6️⃣",
    7: "7️⃣",
    8: "8️⃣",
}

# Русские названия дней недели
_WEEKDAYS_RU = {
    0: "Понедельник",
    1: "Вторник",
    2: "Среда",
    3: "Четверг",
    4: "Пятница",
    5: "Суббота",
    6: "Воскресенье",
}


def _format_date(date: datetime.date) -> str:
    """Форматирование даты: '02.09.2026'."""
    return date.strftime("%d.%m.%Y")


def _get_group_name() -> str:
    """Вернуть имя группы из конфигурации."""
    from schedule_bot.config import GROUP_NAME
    return GROUP_NAME


def _lesson_block(lesson: Lesson) -> str:
    """Форматирование одного занятия."""
    num_emoji = _NUM_EMOJI.get(lesson.number, f"{lesson.number}️⃣")
    lines = [
        f"{num_emoji} {lesson.time_start}–{lesson.time_end}",
        f"<b>{lesson.subject}</b>",
        f"<i>{lesson.lesson_type}</i>",
    ]
    if lesson.teacher:
        lines.append(f"👨‍🏫 {lesson.teacher}")
    if lesson.classroom:
        lines.append(f"📍 {lesson.classroom}")
    return "\n".join(lines)


def format_day(day: DaySchedule, label: str | None = None) -> str:
    """Форматирование расписания на один день.

    *label* — необязательный заголовок ("Сегодня", "Завтра" и т.д.).
    """
    header_parts = ["📅"]
    if label:
        header_parts.append(f"<b>{label}</b> —")
    header_parts.append(f"<b>{day.weekday}</b>, {_format_date(day.date)}")
    header = " ".join(header_parts)

    parts = [
        header,
        f"👥 {_get_group_name()}",
    ]

    if not day.has_lessons:
        parts.append("")
        parts.append("Занятий нет. Отдыхайте! 😊")
        return "\n".join(parts)

    for lesson in day.lessons:
        parts.append("")
        parts.append(_lesson_block(lesson))

    return "\n".join(parts)


def _format_day_expandable(day: DaySchedule) -> str:
    """Компактный формат дня для недельного расписания с раскрывающимся блоком."""
    header = f"📅 <b>{day.weekday}</b>, {_format_date(day.date)}"

    if not day.has_lessons:
        return f"{header} — <i>выходной</i>"

    count = len(day.lessons)
    summary = f"{header} — {count} пар(ы)"

    lesson_lines = []
    for lesson in day.lessons:
        num_emoji = _NUM_EMOJI.get(lesson.number, "·")
        lesson_lines.append(
            f"  {num_emoji} {lesson.time_start}–{lesson.time_end} — "
            f"<b>{lesson.subject}</b>"
        )
        details = []
        if lesson.lesson_type:
            details.append(f"<i>{lesson.lesson_type}</i>")
        if lesson.classroom:
            details.append(f"📍 {lesson.classroom}")
        if lesson.teacher:
            details.append(f"👨‍🏫 {lesson.teacher}")
        if details:
            lesson_lines.append(f"      {' · '.join(details)}")

    inner = "\n".join(lesson_lines)
    return f"{summary}\n<blockquote expandable>{inner}</blockquote>"


def format_week(days: list[DaySchedule]) -> str:
    """Форматирование расписания на неделю с раскрывающимися блоками."""
    if not days:
        return "📅 На этой неделе занятий нет."

    dates = sorted(d.date for d in days)
    start = _format_date(dates[0])
    end = _format_date(dates[-1])

    parts = [
        f"🗓 <b>Неделя {start} — {end}</b>",
        f"👥 {_get_group_name()}",
        "",
    ]

    total_lessons = 0
    for day in sorted(days, key=lambda d: d.date):
        parts.append(_format_day_expandable(day))
        total_lessons += len(day.lessons)

    parts.append("")
    parts.append(f"📊 Всего пар за неделю: <b>{total_lessons}</b>")
    return "\n".join(parts)


# ── Inline-клавиатуры ──────────────────────────────────────

def get_day_keyboard(target_date: datetime.date) -> InlineKeyboardMarkup:
    """Создать инлайн-клавиатуру для навигации по дням."""
    prev_day = target_date - datetime.timedelta(days=1)
    next_day = target_date + datetime.timedelta(days=1)

    mon = target_date - datetime.timedelta(days=target_date.weekday())

    buttons = [
        [
            InlineKeyboardButton(text="◀️ День", callback_data=f"day:{prev_day.isoformat()}"),
            InlineKeyboardButton(text="📅 Сегодня", callback_data="day:today"),
            InlineKeyboardButton(text="День ▶️", callback_data=f"day:{next_day.isoformat()}"),
        ],
        [
            InlineKeyboardButton(text="🗓 Вся неделя", callback_data=f"week:{mon.isoformat()}"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_week_keyboard(monday_date: datetime.date) -> InlineKeyboardMarkup:
    """Создать инлайн-клавиатуру для навигации по неделям."""
    prev_mon = monday_date - datetime.timedelta(days=7)
    next_mon = monday_date + datetime.timedelta(days=7)

    buttons = [
        [
            InlineKeyboardButton(text="◀️ Пред. неделя", callback_data=f"week:{prev_mon.isoformat()}"),
            InlineKeyboardButton(text="🗓 Тек. неделя", callback_data="week:current"),
            InlineKeyboardButton(text="След. неделя ▶️", callback_data=f"week:{next_mon.isoformat()}"),
        ],
        [
            InlineKeyboardButton(text="📅 Сегодня", callback_data="day:today"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
