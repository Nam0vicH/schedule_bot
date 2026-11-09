"""SQLite-хранилище опросов.

Модуль использует ту же базу, что и кэш расписания, но только новые
таблицы с префиксом ``poll_``. Схема создаётся через ``CREATE TABLE IF NOT EXISTS``
и версионируется через ``PRAGMA user_version``, поэтому обновление безопасно
для уже работающего бота.

Все операции с бронированиями выполняются в транзакциях ``BEGIN IMMEDIATE``.
Уникальные индексы ``(poll_id, user_id)`` и ``(option_id, slot)`` гарантируют
корректность даже при одновременных запросах из нескольких процессов.
"""

from __future__ import annotations

import datetime
import logging
import sqlite3
import threading
import zoneinfo
from collections import defaultdict
from pathlib import Path

from schedule_bot import config
from schedule_bot.polls.models import (
    CARD_PHOTO,
    STATUS_ACTIVE,
    STATUS_CLOSED,
    STATUS_EXPIRED,
    ActionResult,
    AvatarRecord,
    Booking,
    ChatTarget,
    LimitResult,
    Option,
    OptionDeleteResult,
    OptionView,
    Poll,
    PollView,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_db_path: Path = config.POLLS_DB_PATH
_init_lock = threading.Lock()
_initialized_paths: set[str] = set()


def set_db_path(path: Path | str) -> None:
    """Переключить путь к БД (используется тестами)."""
    global _db_path
    _db_path = Path(path)


def _tz() -> datetime.tzinfo:
    return zoneinfo.ZoneInfo(config.TIMEZONE)


def now_iso() -> str:
    """Текущее время в ISO-формате с часовым поясом проекта."""
    return datetime.datetime.now(tz=_tz()).isoformat(timespec="seconds")


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS poll_polls (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id        INTEGER NOT NULL,
    title             TEXT    NOT NULL,
    description       TEXT    NOT NULL DEFAULT '',
    chat_id           INTEGER,
    message_id        INTEGER,
    status            TEXT    NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active', 'closed', 'expired')),
    created_at        TEXT    NOT NULL,
    expires_at        TEXT,
    allow_change      INTEGER NOT NULL DEFAULT 1,
    allow_cancel      INTEGER NOT NULL DEFAULT 1,
    show_participants INTEGER NOT NULL DEFAULT 1,
    default_max       INTEGER NOT NULL DEFAULT 1 CHECK (default_max >= 1),
    revision          INTEGER NOT NULL DEFAULT 1,
    rendered_revision INTEGER NOT NULL DEFAULT 0,
    card_mode         TEXT    NOT NULL DEFAULT 'photo'
);

CREATE INDEX IF NOT EXISTS idx_poll_polls_creator ON poll_polls(creator_id, created_at);
CREATE INDEX IF NOT EXISTS idx_poll_polls_due
    ON poll_polls(status, expires_at);

CREATE TABLE IF NOT EXISTS poll_options (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    poll_id          INTEGER NOT NULL REFERENCES poll_polls(id) ON DELETE CASCADE,
    title            TEXT    NOT NULL,
    position         INTEGER NOT NULL,
    max_participants INTEGER NOT NULL DEFAULT 1 CHECK (max_participants >= 1)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_poll_option_position
    ON poll_options(poll_id, position);
CREATE INDEX IF NOT EXISTS idx_poll_options_poll ON poll_options(poll_id, position);

CREATE TABLE IF NOT EXISTS poll_bookings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    poll_id         INTEGER NOT NULL REFERENCES poll_polls(id) ON DELETE CASCADE,
    option_id       INTEGER NOT NULL REFERENCES poll_options(id) ON DELETE CASCADE,
    user_id         INTEGER NOT NULL,
    user_name       TEXT    NOT NULL,
    username        TEXT,
    photo_file_id   TEXT,
    photo_unique_id TEXT,
    photo_path      TEXT,
    slot            INTEGER NOT NULL CHECK (slot >= 0),
    booked_at       TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    UNIQUE (poll_id, user_id),
    UNIQUE (option_id, slot)
);

CREATE INDEX IF NOT EXISTS idx_poll_bookings_poll ON poll_bookings(poll_id);
CREATE INDEX IF NOT EXISTS idx_poll_bookings_option ON poll_bookings(option_id);
CREATE INDEX IF NOT EXISTS idx_poll_bookings_user ON poll_bookings(user_id);

CREATE TABLE IF NOT EXISTS poll_chats (
    chat_id      INTEGER PRIMARY KEY,
    title        TEXT    NOT NULL DEFAULT '',
    chat_type    TEXT    NOT NULL DEFAULT 'group',
    username     TEXT,
    is_forum     INTEGER NOT NULL DEFAULT 0,
    added_at     TEXT    NOT NULL,
    last_seen_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS poll_avatar_cache (
    user_id        INTEGER PRIMARY KEY,
    file_id        TEXT,
    file_unique_id TEXT,
    path           TEXT,
    found          INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT    NOT NULL
);
"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    conn.executescript(_SCHEMA_SQL)
    if version < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
        logger.info("Схема опросов обновлена до версии %d", SCHEMA_VERSION)


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Открыть соединение с БД опросов и при необходимости создать схему."""
    db_path = Path(path) if path is not None else _db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")

    key = str(db_path.resolve())
    if key not in _initialized_paths:
        with _init_lock:
            if key not in _initialized_paths:
                try:
                    conn.execute("PRAGMA journal_mode = WAL")
                except sqlite3.DatabaseError:
                    logger.warning("Не удалось включить WAL для %s", db_path)
                _ensure_schema(conn)
                _initialized_paths.add(key)
    return conn


# ── Чтение ─────────────────────────────────────────────────

def _fetch_poll(conn: sqlite3.Connection, poll_id: int) -> Poll | None:
    row = conn.execute("SELECT * FROM poll_polls WHERE id = ?", (poll_id,)).fetchone()
    return Poll.from_row(row) if row else None


def get_poll(poll_id: int) -> Poll | None:
    conn = connect()
    try:
        return _fetch_poll(conn, poll_id)
    finally:
        conn.close()


def get_option(option_id: int) -> Option | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM poll_options WHERE id = ?", (option_id,)).fetchone()
        return Option.from_row(row) if row else None
    finally:
        conn.close()


def get_view(poll_id: int) -> PollView | None:
    """Полное состояние опроса. Просроченный опрос автоматически закрывается."""
    expire_if_due(poll_id)

    conn = connect()
    try:
        poll = _fetch_poll(conn, poll_id)
        if poll is None:
            return None

        option_rows = conn.execute(
            "SELECT * FROM poll_options WHERE poll_id = ? ORDER BY position, id",
            (poll_id,),
        ).fetchall()
        booking_rows = conn.execute(
            "SELECT * FROM poll_bookings WHERE poll_id = ? ORDER BY booked_at, id",
            (poll_id,),
        ).fetchall()

        by_option: dict[int, list[Booking]] = defaultdict(list)
        for row in booking_rows:
            by_option[row["option_id"]].append(Booking.from_row(row))

        options = [
            OptionView(option=Option.from_row(row), bookings=by_option.get(row["id"], []))
            for row in option_rows
        ]
        return PollView(poll=poll, options=options)
    finally:
        conn.close()


def list_polls_by_creator(creator_id: int, limit: int = 50) -> list[Poll]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM poll_polls WHERE creator_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (creator_id, limit),
        ).fetchall()
        return [Poll.from_row(row) for row in rows]
    finally:
        conn.close()


def get_user_booking(poll_id: int, user_id: int) -> Booking | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM poll_bookings WHERE poll_id = ? AND user_id = ?",
            (poll_id, user_id),
        ).fetchone()
        return Booking.from_row(row) if row else None
    finally:
        conn.close()


# ── Создание и публикация ──────────────────────────────────

def create_poll(
    *,
    creator_id: int,
    title: str,
    description: str,
    chat_id: int | None,
    options: list[tuple[str, int]],
    default_max: int,
    allow_change: bool,
    allow_cancel: bool,
    show_participants: bool,
    expires_at: str | None,
) -> int:
    if len(options) < 2:
        raise ValueError("Опрос должен содержать не менее двух вариантов")
    if len(options) > config.POLL_MAX_OPTIONS:
        raise ValueError(f"Не более {config.POLL_MAX_OPTIONS} вариантов")

    now = now_iso()
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """
            INSERT INTO poll_polls (
                creator_id, title, description, chat_id, message_id, status,
                created_at, expires_at, allow_change, allow_cancel,
                show_participants, default_max, revision, rendered_revision, card_mode
            ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?)
            """,
            (
                creator_id,
                title,
                description,
                chat_id,
                STATUS_ACTIVE,
                now,
                expires_at,
                int(allow_change),
                int(allow_cancel),
                int(show_participants),
                max(1, default_max),
                CARD_PHOTO,
            ),
        )
        poll_id = int(cursor.lastrowid)
        for position, (option_title, option_max) in enumerate(options, start=1):
            conn.execute(
                """
                INSERT INTO poll_options (poll_id, title, position, max_participants)
                VALUES (?, ?, ?, ?)
                """,
                (poll_id, option_title, position, max(1, option_max)),
            )
        conn.commit()
        logger.info("Создан опрос #%d (%d вариантов)", poll_id, len(options))
        return poll_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_published_message(
    poll_id: int,
    chat_id: int,
    message_id: int,
    card_mode: str,
) -> None:
    conn = connect()
    try:
        conn.execute(
            """
            UPDATE poll_polls
               SET chat_id = ?, message_id = ?, card_mode = ?
             WHERE id = ?
            """,
            (chat_id, message_id, card_mode, poll_id),
        )
    finally:
        conn.close()


def mark_rendered(poll_id: int, revision: int) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE poll_polls SET rendered_revision = ? WHERE id = ?",
            (revision, poll_id),
        )
    finally:
        conn.close()


# ── Бронирование ───────────────────────────────────────────

def book(
    poll_id: int,
    option_id: int,
    user_id: int,
    user_name: str,
    username: str | None,
) -> ActionResult:
    """Занять вариант или изменить уже существующее бронирование.

    Атомарно: проверка статуса, вместимости, поиск свободного слота и запись
    выполняются под эксклюзивной блокировкой записи SQLite.
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        poll = _fetch_poll(conn, poll_id)
        if poll is None:
            conn.rollback()
            return ActionResult(code="not_found", poll_id=poll_id)

        if poll.status != STATUS_ACTIVE:
            conn.rollback()
            return ActionResult(code="inactive", poll_id=poll_id)

        if poll.expires_at and poll.expires_at <= now_iso():
            conn.execute(
                "UPDATE poll_polls SET status = ?, revision = revision + 1 WHERE id = ?",
                (STATUS_EXPIRED, poll_id),
            )
            conn.commit()
            return ActionResult(code="expired", poll_id=poll_id)

        option_row = conn.execute(
            "SELECT * FROM poll_options WHERE id = ? AND poll_id = ?",
            (option_id, poll_id),
        ).fetchone()
        if option_row is None:
            conn.rollback()
            return ActionResult(code="option_not_found", poll_id=poll_id)
        option = Option.from_row(option_row)

        existing_row = conn.execute(
            "SELECT * FROM poll_bookings WHERE poll_id = ? AND user_id = ?",
            (poll_id, user_id),
        ).fetchone()
        existing = Booking.from_row(existing_row) if existing_row else None

        if existing and existing.option_id == option_id:
            conn.rollback()
            return ActionResult(
                code="same", poll_id=poll_id, option=option, message="Этот вариант уже выбран"
            )

        if existing and not poll.allow_change:
            conn.rollback()
            return ActionResult(
                code="change_forbidden",
                poll_id=poll_id,
                option=option,
                message="Организатор запретил изменение выбора",
            )

        taken_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM poll_bookings WHERE option_id = ?",
            (option_id,),
        ).fetchone()
        taken = int(taken_row["cnt"])
        if taken >= option.max_participants:
            conn.rollback()
            return ActionResult(
                code="full",
                poll_id=poll_id,
                option=option,
                message="Вариант уже занят",
            )

        used_slots = {
            int(row["slot"])
            for row in conn.execute(
                "SELECT slot FROM poll_bookings WHERE option_id = ?",
                (option_id,),
            ).fetchall()
        }
        slot = next(
            (index for index in range(option.max_participants) if index not in used_slots),
            None,
        )
        if slot is None:
            conn.rollback()
            return ActionResult(code="full", poll_id=poll_id, option=option)

        now = now_iso()
        try:
            if existing:
                conn.execute(
                    """
                    UPDATE poll_bookings
                       SET option_id = ?, slot = ?, updated_at = ?
                     WHERE id = ?
                    """,
                    (option_id, slot, now, existing.id),
                )
                code = "changed"
                changed = True
            else:
                conn.execute(
                    """
                    INSERT INTO poll_bookings (
                        poll_id, option_id, user_id, user_name, username,
                        photo_file_id, photo_unique_id, photo_path, slot,
                        booked_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?)
                    """,
                    (poll_id, option_id, user_id, user_name, username, slot, now, now),
                )
                code = "booked"
                changed = True

            conn.execute(
                "UPDATE poll_polls SET revision = revision + 1 WHERE id = ?",
                (poll_id,),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            logger.warning(
                "Конфликт бронирования опроса #%d варианта #%d пользователем %d",
                poll_id,
                option_id,
                user_id,
            )
            return ActionResult(code="conflict", poll_id=poll_id, option=option)

        return ActionResult(code=code, poll_id=poll_id, option=option, changed=changed)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cancel_booking(poll_id: int, user_id: int) -> ActionResult:
    """Отменить собственное бронирование (только в активном опросе)."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        poll = _fetch_poll(conn, poll_id)
        if poll is None:
            conn.rollback()
            return ActionResult(code="not_found", poll_id=poll_id)
        if poll.status != STATUS_ACTIVE:
            conn.rollback()
            return ActionResult(code="inactive", poll_id=poll_id)
        if poll.expires_at and poll.expires_at <= now_iso():
            conn.execute(
                "UPDATE poll_polls SET status = ?, revision = revision + 1 WHERE id = ?",
                (STATUS_EXPIRED, poll_id),
            )
            conn.commit()
            return ActionResult(code="expired", poll_id=poll_id)
        if not poll.allow_cancel:
            conn.rollback()
            return ActionResult(
                code="cancel_forbidden",
                poll_id=poll_id,
                message="Организатор запретил отмену выбора",
            )

        row = conn.execute(
            "SELECT * FROM poll_bookings WHERE poll_id = ? AND user_id = ?",
            (poll_id, user_id),
        ).fetchone()
        if row is None:
            conn.rollback()
            return ActionResult(code="no_booking", poll_id=poll_id)

        booking = Booking.from_row(row)
        option_row = conn.execute(
            "SELECT * FROM poll_options WHERE id = ?",
            (booking.option_id,),
        ).fetchone()
        option = Option.from_row(option_row) if option_row else None

        conn.execute("DELETE FROM poll_bookings WHERE id = ?", (booking.id,))
        conn.execute(
            "UPDATE poll_polls SET revision = revision + 1 WHERE id = ?",
            (poll_id,),
        )
        conn.commit()
        return ActionResult(code="cancelled", poll_id=poll_id, option=option, changed=True)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Управление опросом ─────────────────────────────────────

def _check_owner(conn: sqlite3.Connection, poll_id: int, creator_id: int) -> str:
    poll = _fetch_poll(conn, poll_id)
    if poll is None:
        return "not_found"
    if poll.creator_id != creator_id:
        return "forbidden"
    return "ok"


def update_poll_settings(
    poll_id: int,
    creator_id: int,
    *,
    title: str | None = None,
    description: str | None = None,
    allow_change: bool | None = None,
    allow_cancel: bool | None = None,
    show_participants: bool | None = None,
) -> str:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        status = _check_owner(conn, poll_id, creator_id)
        if status != "ok":
            conn.rollback()
            return status

        fields: list[str] = []
        values: list[object] = []
        if title is not None:
            fields.append("title = ?")
            values.append(title)
        if description is not None:
            fields.append("description = ?")
            values.append(description)
        if allow_change is not None:
            fields.append("allow_change = ?")
            values.append(int(allow_change))
        if allow_cancel is not None:
            fields.append("allow_cancel = ?")
            values.append(int(allow_cancel))
        if show_participants is not None:
            fields.append("show_participants = ?")
            values.append(int(show_participants))
        if not fields:
            conn.rollback()
            return "no_changes"

        fields.append("revision = revision + 1")
        values.append(poll_id)
        conn.execute(f"UPDATE poll_polls SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
        return "ok"
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def add_options(
    poll_id: int,
    creator_id: int,
    titles: list[str],
    max_participants: int | None = None,
) -> tuple[str, int]:
    """Добавить варианты. Возвращает (код, добавлено)."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        status = _check_owner(conn, poll_id, creator_id)
        if status != "ok":
            conn.rollback()
            return status, 0

        poll = _fetch_poll(conn, poll_id)
        assert poll is not None
        row = conn.execute(
            "SELECT COUNT(*) AS cnt, COALESCE(MAX(position), 0) AS pos FROM poll_options WHERE poll_id = ?",
            (poll_id,),
        ).fetchone()
        current_count = int(row["cnt"])
        position = int(row["pos"])
        capacity = config.POLL_MAX_OPTIONS - current_count
        if capacity <= 0:
            conn.rollback()
            return "limit_reached", 0

        to_add = titles[:capacity]
        limit = max(1, max_participants if max_participants is not None else poll.default_max)
        for title in to_add:
            position += 1
            conn.execute(
                """
                INSERT INTO poll_options (poll_id, title, position, max_participants)
                VALUES (?, ?, ?, ?)
                """,
                (poll_id, title, position, limit),
            )
        conn.execute(
            "UPDATE poll_polls SET revision = revision + 1 WHERE id = ?",
            (poll_id,),
        )
        conn.commit()
        return "ok", len(to_add)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_option(
    poll_id: int,
    creator_id: int,
    option_id: int,
    *,
    force: bool = False,
) -> OptionDeleteResult:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        status = _check_owner(conn, poll_id, creator_id)
        if status != "ok":
            conn.rollback()
            return OptionDeleteResult(code=status)

        option_row = conn.execute(
            "SELECT * FROM poll_options WHERE id = ? AND poll_id = ?",
            (option_id, poll_id),
        ).fetchone()
        if option_row is None:
            conn.rollback()
            return OptionDeleteResult(code="option_not_found")
        option = Option.from_row(option_row)

        count_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM poll_options WHERE poll_id = ?",
            (poll_id,),
        ).fetchone()
        if int(count_row["cnt"]) <= 2:
            conn.rollback()
            return OptionDeleteResult(code="min_options", option_title=option.title)

        booking_rows = conn.execute(
            "SELECT * FROM poll_bookings WHERE option_id = ? ORDER BY booked_at, id",
            (option_id,),
        ).fetchall()
        bookings = [Booking.from_row(row) for row in booking_rows]

        if bookings and not force:
            conn.rollback()
            return OptionDeleteResult(
                code="needs_confirm",
                option_title=option.title,
                bookings=bookings,
            )

        conn.execute("DELETE FROM poll_options WHERE id = ?", (option_id,))
        conn.execute(
            "UPDATE poll_polls SET revision = revision + 1 WHERE id = ?",
            (poll_id,),
        )
        conn.commit()
        return OptionDeleteResult(
            code="deleted",
            option_title=option.title,
            bookings=bookings,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_option_limit(
    poll_id: int,
    creator_id: int,
    option_id: int,
    new_limit: int,
) -> LimitResult:
    new_limit = max(1, min(99, new_limit))
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        status = _check_owner(conn, poll_id, creator_id)
        if status != "ok":
            conn.rollback()
            return LimitResult(code=status)

        option_row = conn.execute(
            "SELECT * FROM poll_options WHERE id = ? AND poll_id = ?",
            (option_id, poll_id),
        ).fetchone()
        if option_row is None:
            conn.rollback()
            return LimitResult(code="option_not_found")

        count_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM poll_bookings WHERE option_id = ?",
            (option_id,),
        ).fetchone()
        taken = int(count_row["cnt"])
        if new_limit < taken:
            conn.rollback()
            return LimitResult(code="below_taken", limit=new_limit, taken=taken)

        conn.execute(
            "UPDATE poll_options SET max_participants = ? WHERE id = ?",
            (new_limit, option_id),
        )
        conn.execute(
            "UPDATE poll_polls SET revision = revision + 1 WHERE id = ?",
            (poll_id,),
        )
        conn.commit()
        return LimitResult(
            code="ok",
            option_title=option_row["title"],
            limit=new_limit,
            taken=taken,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_status(poll_id: int, creator_id: int, status: str) -> str:
    if status not in (STATUS_ACTIVE, STATUS_CLOSED):
        raise ValueError(f"Недопустимый статус: {status}")
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = _check_owner(conn, poll_id, creator_id)
        if result != "ok":
            conn.rollback()
            return result

        poll = _fetch_poll(conn, poll_id)
        assert poll is not None
        expires_at = poll.expires_at
        if status == STATUS_ACTIVE and expires_at and expires_at <= now_iso():
            expires_at = None

        conn.execute(
            "UPDATE poll_polls SET status = ?, expires_at = ?, revision = revision + 1 WHERE id = ?",
            (status, expires_at, poll_id),
        )
        conn.commit()
        return "ok"
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_poll(poll_id: int, creator_id: int) -> tuple[str, Poll | None]:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = _check_owner(conn, poll_id, creator_id)
        if result != "ok":
            conn.rollback()
            return result, None
        poll = _fetch_poll(conn, poll_id)
        conn.execute("DELETE FROM poll_polls WHERE id = ?", (poll_id,))
        conn.commit()
        return "ok", poll
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def copy_poll(poll_id: int, creator_id: int) -> tuple[str, int]:
    """Создать копию опроса без бронирований. Возвращает (код, новый id)."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = _check_owner(conn, poll_id, creator_id)
        if result != "ok":
            conn.rollback()
            return result, 0

        poll = _fetch_poll(conn, poll_id)
        assert poll is not None
        options = conn.execute(
            "SELECT * FROM poll_options WHERE poll_id = ? ORDER BY position, id",
            (poll_id,),
        ).fetchall()

        now = now_iso()
        title = f"{poll.title} (копия)"[:128]
        expires_at = poll.expires_at
        if expires_at and expires_at <= now:
            expires_at = None

        cursor = conn.execute(
            """
            INSERT INTO poll_polls (
                creator_id, title, description, chat_id, message_id, status,
                created_at, expires_at, allow_change, allow_cancel,
                show_participants, default_max, revision, rendered_revision, card_mode
            ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?)
            """,
            (
                poll.creator_id,
                title,
                poll.description,
                poll.chat_id,
                STATUS_ACTIVE,
                now,
                expires_at,
                int(poll.allow_change),
                int(poll.allow_cancel),
                int(poll.show_participants),
                poll.default_max,
                CARD_PHOTO,
            ),
        )
        new_id = int(cursor.lastrowid)
        for row in options:
            conn.execute(
                """
                INSERT INTO poll_options (poll_id, title, position, max_participants)
                VALUES (?, ?, ?, ?)
                """,
                (new_id, row["title"], row["position"], row["max_participants"]),
            )
        conn.commit()
        logger.info("Опрос #%d скопирован в #%d", poll_id, new_id)
        return "ok", new_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Срок действия ──────────────────────────────────────────

def expire_if_due(poll_id: int) -> bool:
    """Закрыть опрос, если истёк его срок. Возвращает True при изменении."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, expires_at FROM poll_polls WHERE id = ?",
            (poll_id,),
        ).fetchone()
        if row is None or row["status"] != STATUS_ACTIVE or not row["expires_at"]:
            conn.rollback()
            return False
        if row["expires_at"] > now_iso():
            conn.rollback()
            return False
        conn.execute(
            "UPDATE poll_polls SET status = ?, revision = revision + 1 WHERE id = ?",
            (STATUS_EXPIRED, poll_id),
        )
        conn.commit()
        logger.info("Срок опроса #%d истёк", poll_id)
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def expire_due_polls() -> list[int]:
    """Закрыть все опросы с истёкшим сроком, вернуть их ID."""
    now = now_iso()
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT id FROM poll_polls
             WHERE status = ? AND expires_at IS NOT NULL AND expires_at <= ?
            """,
            (STATUS_ACTIVE, now),
        ).fetchall()
        ids = [int(row["id"]) for row in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"""
                UPDATE poll_polls
                   SET status = ?, revision = revision + 1
                 WHERE id IN ({placeholders})
                """,
                (STATUS_EXPIRED, *ids),
            )
        conn.commit()
        if ids:
            logger.info("Автоматически закрыты просроченные опросы: %s", ids)
        return ids
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Чаты и аватарки ────────────────────────────────────────

def record_chat(
    chat_id: int,
    title: str,
    chat_type: str,
    username: str | None = None,
    is_forum: bool = False,
) -> None:
    if chat_type == "private":
        return
    now = now_iso()
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO poll_chats (chat_id, title, chat_type, username, is_forum, added_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                chat_type = excluded.chat_type,
                username = excluded.username,
                is_forum = excluded.is_forum,
                last_seen_at = excluded.last_seen_at
            """,
            (chat_id, title or f"Чат {chat_id}", chat_type, username, int(is_forum), now, now),
        )
    except Exception:
        logger.exception("Не удалось сохранить чат %s", chat_id)
    finally:
        conn.close()


def list_chat_targets(limit: int = 20) -> list[ChatTarget]:
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT chat_id, title, chat_type, username
              FROM poll_chats
             WHERE chat_type IN ('group', 'supergroup')
             ORDER BY last_seen_at DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            ChatTarget(
                chat_id=int(row["chat_id"]),
                title=row["title"],
                chat_type=row["chat_type"],
                username=row["username"],
            )
            for row in rows
        ]
    finally:
        conn.close()


def get_chat_target(chat_id: int) -> ChatTarget | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT chat_id, title, chat_type, username FROM poll_chats WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        if row is None:
            return None
        return ChatTarget(
            chat_id=int(row["chat_id"]),
            title=row["title"],
            chat_type=row["chat_type"],
            username=row["username"],
        )
    finally:
        conn.close()


def get_avatar(user_id: int) -> AvatarRecord | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM poll_avatar_cache WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            return None
        return AvatarRecord(
            user_id=int(row["user_id"]),
            file_id=row["file_id"],
            file_unique_id=row["file_unique_id"],
            path=row["path"],
            found=bool(row["found"]),
            updated_at=row["updated_at"],
        )
    finally:
        conn.close()


def save_avatar(
    user_id: int,
    *,
    file_id: str | None,
    file_unique_id: str | None,
    path: str | None,
    found: bool,
) -> None:
    now = now_iso()
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO poll_avatar_cache (user_id, file_id, file_unique_id, path, found, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                file_id = excluded.file_id,
                file_unique_id = excluded.file_unique_id,
                path = excluded.path,
                found = excluded.found,
                updated_at = excluded.updated_at
            """,
            (user_id, file_id, file_unique_id, path, int(found), now),
        )
        if found and path:
            conn.execute(
                """
                UPDATE poll_bookings
                   SET photo_file_id = ?, photo_unique_id = ?, photo_path = ?
                 WHERE user_id = ?
                """,
                (file_id, file_unique_id, path, user_id),
            )
    finally:
        conn.close()
