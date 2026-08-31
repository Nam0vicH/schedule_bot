"""Модели данных расписания."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field


@dataclass
class Lesson:
    """Одно учебное занятие."""

    number: int  # номер пары (1, 2, 3, …)
    time_start: str  # "09:00"
    time_end: str  # "10:30"
    subject: str  # "Основы права"
    lesson_type: str  # "Практические занятия"
    teacher: str  # "Новичков Андрей Вячеславович"
    classroom: str  # "Аудитория 1308"

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "time_start": self.time_start,
            "time_end": self.time_end,
            "subject": self.subject,
            "lesson_type": self.lesson_type,
            "teacher": self.teacher,
            "classroom": self.classroom,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Lesson:
        return cls(
            number=data["number"],
            time_start=data["time_start"],
            time_end=data["time_end"],
            subject=data["subject"],
            lesson_type=data["lesson_type"],
            teacher=data["teacher"],
            classroom=data["classroom"],
        )


@dataclass
class DaySchedule:
    """Расписание на один день."""

    weekday: str  # "Среда"
    date: datetime.date  # 2026-09-02
    lessons: list[Lesson] = field(default_factory=list)

    @property
    def has_lessons(self) -> bool:
        return len(self.lessons) > 0

    def to_dict(self) -> dict:
        return {
            "weekday": self.weekday,
            "date": self.date.isoformat(),
            "lessons": [lesson.to_dict() for lesson in self.lessons],
        }

    @classmethod
    def from_dict(cls, data: dict) -> DaySchedule:
        return cls(
            weekday=data["weekday"],
            date=datetime.date.fromisoformat(data["date"]),
            lessons=[Lesson.from_dict(l) for l in data["lessons"]],
        )
