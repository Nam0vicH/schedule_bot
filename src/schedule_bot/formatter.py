"""Форматирование расписания для Telegram-сообщений."""

from __future__ import annotations

import datetime

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
    header_parts.append(_format_date(day.date))
    header = " ".join(header_parts)

    parts = [
        header,
        f"👥 {_get_group_name()}",
    ]

    if not day.has_lessons:
        parts.append("")
        parts.append("Занятий нет.")
        return "\n".join(parts)

    for lesson in day.lessons:
        parts.append("")
        parts.append(_lesson_block(lesson))

    return "\n".join(parts)


def format_day_compact(day: DaySchedule) -> str:
    """Компактный формат дня для недельного расписания."""
    header = f"📅 <b>{day.weekday}</b>, {_format_date(day.date)}"

    if not day.has_lessons:
        return f"{header}\nЗанятий нет."

    lines = [header]
    for lesson in day.lessons:
        lines.append(
            f"  {_NUM_EMOJI.get(lesson.number, '·')} "
            f"{lesson.time_start}–{lesson.time_end} — "
            f"{lesson.subject} — {lesson.classroom}"
        )
    return "\n".join(lines)


def format_week(days: list[DaySchedule]) -> str:
    """Форматирование расписания на неделю."""
    from schedule_bot.config import GROUP_NAME

    if not days:
        return "📅 На этой неделе занятий нет."

    # Определяем даты начала и конца
    dates = sorted(d.date for d in days)
    start = _format_date(dates[0])
    end = _format_date(dates[-1])

    parts = [
        f"📅 <b>Неделя {start} — {end}</b>",
        f"👥 {GROUP_NAME}",
        "",
    ]

    total_lessons = 0
    for day in sorted(days, key=lambda d: d.date):
        parts.append(format_day_compact(day))
        parts.append("")
        total_lessons += len(day.lessons)

    parts.append(f"Всего пар: {total_lessons}")
    return "\n".join(parts)


def format_no_lessons(date: datetime.date, label: str | None = None) -> str:
    """Сообщение об отсутствии занятий."""
    parts = ["📅"]
    if label:
        parts.append(f"{label} —")
    parts.append(_format_date(date))
    header = " ".join(parts)
    return f"{header}\n\nЗанятий нет."


def format_next_lesson(lesson: Lesson, day: DaySchedule) -> str:
    """Форматирование ближайшего следующего занятия."""
    return (
        f"⏭ <b>Ближайшее занятие</b>\n"
        f"📅 {day.weekday}, {_format_date(day.date)}\n\n"
        f"{_lesson_block(lesson)}"
    )


def format_notification(day: DaySchedule) -> str:
    """Форматирование автоматического уведомления (расписание на завтра)."""
    parts = [f"📚 <b>Расписание на завтра</b>"]
    parts.append(f"👥 {_get_group_name()}")

    if not day.has_lessons:
        parts.append("")
        parts.append("Занятий нет. Отдыхайте! 😊")
        return "\n".join(parts)

    parts.append(f"\n{len(day.lessons)} пар(ы):")

    for lesson in day.lessons:
        parts.append("")
        parts.append(_lesson_block(lesson))

    return "\n".join(parts)


def _get_group_name() -> str:
    """Вернуть имя группы из конфигурации."""
    from schedule_bot.config import GROUP_NAME
    return GROUP_NAME
