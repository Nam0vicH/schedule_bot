"""Обнаружение изменений в расписании."""

from __future__ import annotations

from dataclasses import dataclass

from schedule_bot.models import DaySchedule, Lesson


@dataclass
class Change:
    """Одно изменение в расписании."""

    date: str  # "03.09.2026"
    lesson_number: int
    subject: str
    change_type: str  # "added" | "removed" | "modified"
    field: str  # "classroom" | "teacher" | "time" | "" (для added/removed)
    old_value: str
    new_value: str


def compare_lessons(old: Lesson, new: Lesson) -> list[Change]:
    """Сравнить два занятия и вернуть список изменений."""
    changes: list[Change] = []

    fields = {
        "time_start": "время начала",
        "time_end": "время окончания",
        "subject": "дисциплина",
        "lesson_type": "тип занятия",
        "teacher": "преподаватель",
        "classroom": "аудитория",
    }

    for attr, label in fields.items():
        old_val = getattr(old, attr, "")
        new_val = getattr(new, attr, "")
        if old_val != new_val:
            changes.append(Change(
                date="",  # будет заполнено позже
                lesson_number=new.number,
                subject=new.subject,
                change_type="modified",
                field=label,
                old_value=str(old_val),
                new_value=str(new_val),
            ))

    return changes


def compare_schedules(
    old: DaySchedule,
    new: DaySchedule,
) -> list[Change]:
    """Сравнить два расписания одного дня."""
    changes: list[Change] = []
    date_str = new.date.strftime("%d.%m.%Y")

    old_by_num = {l.number: l for l in old.lessons}
    new_by_num = {l.number: l for l in new.lessons}

    # Удалённые занятия
    for num, lesson in old_by_num.items():
        if num not in new_by_num:
            changes.append(Change(
                date=date_str,
                lesson_number=num,
                subject=lesson.subject,
                change_type="removed",
                field="",
                old_value=f"{lesson.subject} ({lesson.time_start}–{lesson.time_end})",
                new_value="",
            ))

    # Добавленные занятия
    for num, lesson in new_by_num.items():
        if num not in old_by_num:
            changes.append(Change(
                date=date_str,
                lesson_number=num,
                subject=lesson.subject,
                change_type="added",
                field="",
                old_value="",
                new_value=f"{lesson.subject} ({lesson.time_start}–{lesson.time_end})",
            ))

    # Изменённые занятия
    for num in old_by_num:
        if num in new_by_num:
            lesson_changes = compare_lessons(old_by_num[num], new_by_num[num])
            for change in lesson_changes:
                change.date = date_str
            changes.extend(lesson_changes)

    return changes


def format_changes(changes: list[Change]) -> str:
    """Форматирование списка изменений для Telegram."""
    if not changes:
        return ""

    parts = ["⚠️ <b>Изменение расписания</b>\n"]

    for change in changes:
        if change.change_type == "added":
            parts.append(
                f"➕ {change.date}, {change.lesson_number} пара\n"
                f"Добавлено: {change.new_value}\n"
            )
        elif change.change_type == "removed":
            parts.append(
                f"➖ {change.date}, {change.lesson_number} пара\n"
                f"Удалено: {change.old_value}\n"
            )
        elif change.change_type == "modified":
            parts.append(
                f"✏️ {change.date}, {change.lesson_number} пара\n"
                f"<b>{change.subject}</b>\n"
                f"{change.field}:\n"
                f"  Было: {change.old_value}\n"
                f"  Стало: {change.new_value}\n"
            )

    return "\n".join(parts)
