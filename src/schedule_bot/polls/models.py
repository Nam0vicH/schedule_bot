"""Модели данных модуля опросов."""

from __future__ import annotations

import datetime
import sqlite3
from dataclasses import dataclass, field

STATUS_ACTIVE = "active"
STATUS_CLOSED = "closed"
STATUS_EXPIRED = "expired"

CARD_PHOTO = "photo"
CARD_TEXT = "text"


@dataclass
class Poll:
    """Опрос с настройками."""

    id: int
    creator_id: int
    title: str
    description: str
    chat_id: int | None
    message_id: int | None
    status: str
    created_at: str
    expires_at: str | None
    allow_change: bool
    allow_cancel: bool
    show_participants: bool
    default_max: int
    revision: int
    card_mode: str
    rendered_revision: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Poll:
        return cls(
            id=row["id"],
            creator_id=row["creator_id"],
            title=row["title"],
            description=row["description"],
            chat_id=row["chat_id"],
            message_id=row["message_id"],
            status=row["status"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            allow_change=bool(row["allow_change"]),
            allow_cancel=bool(row["allow_cancel"]),
            show_participants=bool(row["show_participants"]),
            default_max=int(row["default_max"]),
            revision=int(row["revision"]),
            card_mode=row["card_mode"],
            rendered_revision=int(row["rendered_revision"]),
        )

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE


@dataclass
class Option:
    """Вариант ответа."""

    id: int
    poll_id: int
    title: str
    position: int
    max_participants: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Option:
        return cls(
            id=row["id"],
            poll_id=row["poll_id"],
            title=row["title"],
            position=int(row["position"]),
            max_participants=int(row["max_participants"]),
        )


@dataclass
class Booking:
    """Бронирование варианта участником."""

    id: int
    poll_id: int
    option_id: int
    user_id: int
    user_name: str
    username: str | None
    photo_file_id: str | None
    photo_unique_id: str | None
    photo_path: str | None
    slot: int
    booked_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Booking:
        return cls(
            id=row["id"],
            poll_id=row["poll_id"],
            option_id=row["option_id"],
            user_id=row["user_id"],
            user_name=row["user_name"],
            username=row["username"],
            photo_file_id=row["photo_file_id"],
            photo_unique_id=row["photo_unique_id"],
            photo_path=row["photo_path"],
            slot=int(row["slot"]),
            booked_at=row["booked_at"],
            updated_at=row["updated_at"],
        )


@dataclass
class OptionView:
    """Вариант вместе с его бронированиями."""

    option: Option
    bookings: list[Booking] = field(default_factory=list)

    @property
    def taken(self) -> int:
        return len(self.bookings)

    @property
    def is_full(self) -> bool:
        return self.taken >= self.option.max_participants

    @property
    def free_slots(self) -> int:
        return max(0, self.option.max_participants - self.taken)


@dataclass
class PollView:
    """Полное состояние опроса для отображения."""

    poll: Poll
    options: list[OptionView]

    @property
    def total_options(self) -> int:
        return len(self.options)

    @property
    def available_options(self) -> int:
        return sum(1 for item in self.options if not item.is_full)

    @property
    def total_bookings(self) -> int:
        return sum(item.taken for item in self.options)

    def find_option(self, option_id: int) -> OptionView | None:
        for item in self.options:
            if item.option.id == option_id:
                return item
        return None

    def find_user_booking(self, user_id: int) -> tuple[OptionView, Booking] | None:
        for item in self.options:
            for booking in item.bookings:
                if booking.user_id == user_id:
                    return item, booking
        return None


@dataclass
class ChatTarget:
    """Чат, в котором бот может публиковать опросы."""

    chat_id: int
    title: str
    chat_type: str
    username: str | None = None


@dataclass
class AvatarRecord:
    """Кэшированная аватарка пользователя."""

    user_id: int
    file_id: str | None
    file_unique_id: str | None
    path: str | None
    found: bool
    updated_at: str


@dataclass
class ActionResult:
    """Результат операции с бронированием."""

    code: str
    poll_id: int
    option: Option | None = None
    message: str = ""
    changed: bool = False


@dataclass
class PollCopyResult:
    new_poll_id: int


@dataclass
class OptionDeleteResult:
    code: str
    option_title: str = ""
    bookings: list[Booking] = field(default_factory=list)


@dataclass
class LimitResult:
    code: str
    option_title: str = ""
    limit: int = 0
    taken: int = 0


def parse_dt(value: str | None) -> datetime.datetime | None:
    """Разобрать ISO-дату, сохранённую в БД."""
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed
