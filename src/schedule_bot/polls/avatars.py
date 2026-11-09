"""Загрузка и кэширование фотографий профиля участников."""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import zoneinfo
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError

from schedule_bot import config
from schedule_bot.polls import db
from schedule_bot.polls.models import AvatarRecord, PollView, parse_dt

logger = logging.getLogger(__name__)

_TZ = zoneinfo.ZoneInfo(config.TIMEZONE)
_semaphore = asyncio.Semaphore(4)


def _is_fresh(record: AvatarRecord) -> bool:
    updated = parse_dt(record.updated_at)
    if updated is None:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=_TZ)
    age = datetime.datetime.now(tz=_TZ) - updated
    return age < datetime.timedelta(hours=config.POLL_AVATAR_TTL_HOURS)


def _cleanup_old_files(directory: Path, user_id: int, keep: Path) -> None:
    try:
        for old in directory.glob(f"{user_id}_*"):
            if old != keep:
                old.unlink(missing_ok=True)
    except OSError:
        logger.debug("Не удалось удалить старые аватарки пользователя %d", user_id)


async def _download_avatar(bot: Bot, user_id: int) -> tuple[Path, str, str] | None:
    photos = await bot.get_user_profile_photos(user_id=user_id, limit=1)
    if not photos.photos:
        return None

    sizes = photos.photos[0]
    best = sizes[-1]

    config.POLL_AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    destination = config.POLL_AVATAR_DIR / f"{user_id}_{best.file_unique_id}.jpg"

    if not destination.exists():
        file = await bot.get_file(best.file_id)
        if not file.file_path:
            return None
        await bot.download_file(file.file_path, destination=destination)

    if not destination.exists() or destination.stat().st_size == 0:
        return None

    _cleanup_old_files(config.POLL_AVATAR_DIR, user_id, destination)
    return destination, best.file_id, best.file_unique_id


async def _resolve_one(bot: Bot, user_id: int, force: bool) -> Path | None:
    record = db.get_avatar(user_id)

    if record and not force and _is_fresh(record):
        if not record.found:
            return None
        if record.path and Path(record.path).exists():
            return Path(record.path)

    cached_path = Path(record.path) if record and record.path else None

    async with _semaphore:
        try:
            downloaded = await _download_avatar(bot, user_id)
        except TelegramNetworkError as exc:
            logger.warning("Сеть недоступна при загрузке аватарки %d: %s", user_id, exc)
            return cached_path if cached_path and cached_path.exists() else None
        except Exception as exc:
            # Фото может быть скрыто настройками приватности — это нормальный сценарий.
            logger.debug("Аватарка пользователя %d недоступна: %s", user_id, exc)
            if cached_path and cached_path.exists():
                return cached_path
            return None

    if downloaded is None:
        db.save_avatar(
            user_id,
            file_id=None,
            file_unique_id=None,
            path=None,
            found=False,
        )
        return None

    path, file_id, unique_id = downloaded
    db.save_avatar(
        user_id,
        file_id=file_id,
        file_unique_id=unique_id,
        path=str(path),
        found=True,
    )
    return path


async def resolve_avatars(
    bot: Bot,
    view: PollView,
    *,
    force: bool = False,
) -> dict[int, Path | None]:
    """Вернуть пути к аватаркам всех участников опроса.

    Для пользователей без фото или со скрытым фото возвращается ``None`` —
    рендерер нарисует инициалы.
    """
    if not view.poll.show_participants:
        return {}

    user_ids = {
        booking.user_id
        for option in view.options
        for booking in option.bookings
    }
    if not user_ids:
        return {}

    pairs = await asyncio.gather(
        *(_resolve_one(bot, user_id, force) for user_id in user_ids)
    )
    return dict(zip(user_ids, pairs, strict=True))


def avatar_cache_size_bytes() -> int:
    """Размер дискового кэша аватарок (для диагностики)."""
    if not config.POLL_AVATAR_DIR.exists():
        return 0
    total = 0
    for root, _dirs, files in os.walk(config.POLL_AVATAR_DIR):
        for name in files:
            try:
                total += Path(root, name).stat().st_size
            except OSError:
                pass
    return total
