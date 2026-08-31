"""Бот расписания МИИГАиК — точка входа."""

from __future__ import annotations

import logging


def main() -> None:
    """Запуск бота."""
    # Настройка логирования
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Уменьшаем шум от httpx и httpcore
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    from schedule_bot.bot import run_bot
    run_bot()


if __name__ == "__main__":
    main()
