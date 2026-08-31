"""Парсер расписания с сайта study.miigaik.ru."""

from __future__ import annotations

import datetime
import logging
import re

import httpx
from bs4 import BeautifulSoup, Tag

from schedule_bot import config
from schedule_bot.models import DaySchedule, Lesson

logger = logging.getLogger(__name__)

# Маппинг русских названий месяцев (для разбора дат вида "02.09.2026")
_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")

# Максимальное количество попыток при HTTP-ошибках
MAX_RETRIES = 3
RETRY_DELAY = 2  # секунды


def get_week_bounds(date: datetime.date) -> tuple[datetime.date, datetime.date]:
    """Вернуть понедельник и воскресенье для недели, содержащей *date*."""
    monday = date - datetime.timedelta(days=date.weekday())
    sunday = monday + datetime.timedelta(days=6)
    return monday, sunday


def _build_url(date_start: datetime.date, date_end: datetime.date) -> str:
    """Сформировать URL для запроса расписания на заданную неделю."""
    params = {
        "orgId": config.ORG_ID,
        "groupId": config.GROUP_ID,
        "dateStart": date_start.isoformat(),
        "dateEnd": date_end.isoformat(),
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{config.SOURCE_URL}?{query}"


def _parse_date(text: str) -> datetime.date | None:
    """Разобрать дату из строки вида '02.09.2026'."""
    m = _DATE_RE.search(text.strip())
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def _parse_lesson(block: Tag) -> Lesson | None:
    """Извлечь занятие из ``div.lesson-block``."""
    try:
        time_start = block.get("data-lesson-start", "")
        time_end = block.get("data-lesson-end", "")

        # Номер пары
        num_el = block.select_one("span.lesson-num")
        number = int(num_el.get_text(strip=True)) if num_el else 0

        # Название дисциплины
        subject_el = block.select_one("div.lesson-left h4")
        subject = subject_el.get_text(strip=True) if subject_el else ""

        # Тип занятия — первый <p> после h4 в lesson-left
        lesson_left = block.select_one("div.lesson-left")
        lesson_type = ""
        if lesson_left:
            p_tags = lesson_left.find_all("p")
            # Первый <p> который не внутри lesson-time-num
            for p in p_tags:
                parent_div = p.find_parent("div", class_="lesson-time-num")
                if parent_div is None:
                    lesson_type = p.get_text(strip=True)
                    break

        # Преподаватель — первый <span> в lesson-right (не aud-num)
        lesson_right = block.select_one("div.lesson-right")
        teacher = ""
        if lesson_right:
            # Ищем span напрямую (не .aud-num)
            for span in lesson_right.find_all("span", recursive=False):
                if "aud-num" not in (span.get("class") or []):
                    teacher = span.get_text(strip=True)
                    if teacher:
                        break
            # Если не нашли, пробуем все span внутри
            if not teacher:
                for span in lesson_right.find_all("span"):
                    if "aud-num" not in (span.get("class") or []):
                        text = span.get_text(strip=True)
                        if text:
                            teacher = text
                            break

        # Аудитория
        aud_el = block.select_one("span.aud-num")
        classroom = aud_el.get_text(strip=True) if aud_el else ""

        return Lesson(
            number=number,
            time_start=time_start,
            time_end=time_end,
            subject=subject,
            lesson_type=lesson_type,
            teacher=teacher,
            classroom=classroom,
        )
    except Exception:
        logger.exception("Ошибка при разборе занятия")
        return None


def _parse_html(html: str) -> list[DaySchedule]:
    """Разобрать HTML-страницу и вернуть список дней с занятиями."""
    soup = BeautifulSoup(html, "lxml")
    days: list[DaySchedule] = []

    for day_block in soup.select("div.day-block"):
        # День недели и дата
        weekday_el = day_block.select_one("div.weekday-block h3")
        weekday = weekday_el.get_text(strip=True) if weekday_el else ""

        date_el = day_block.select_one("div.weekday-block span")
        date_str = date_el.get_text(strip=True) if date_el else ""
        date = _parse_date(date_str)

        if not date:
            logger.warning("Не удалось разобрать дату: %s", date_str)
            continue

        # Занятия
        lessons: list[Lesson] = []
        for lesson_block in day_block.select("div.lesson-block"):
            lesson = _parse_lesson(lesson_block)
            if lesson:
                lessons.append(lesson)

        days.append(DaySchedule(weekday=weekday, date=date, lessons=lessons))

    return days


async def fetch_week_schedule(
    date_start: datetime.date,
    date_end: datetime.date,
) -> list[DaySchedule]:
    """Получить расписание на неделю с сайта.

    Возвращает список дней (только те дни, у которых есть занятия на сайте).
    При ошибках HTTP делает до *MAX_RETRIES* повторных попыток.
    """
    url = _build_url(date_start, date_end)
    logger.info("Запрос расписания: %s", url)

    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()

            days = _parse_html(resp.text)
            total_lessons = sum(len(d.lessons) for d in days)
            logger.info(
                "Получено дней: %d, занятий: %d",
                len(days),
                total_lessons,
            )
            return days

        except httpx.HTTPStatusError as exc:
            last_error = exc
            logger.warning(
                "HTTP %d при попытке %d/%d: %s",
                exc.response.status_code,
                attempt,
                MAX_RETRIES,
                url,
            )
        except httpx.HTTPError as exc:
            last_error = exc
            logger.warning(
                "Ошибка HTTP при попытке %d/%d: %s",
                attempt,
                MAX_RETRIES,
                exc,
            )

        if attempt < MAX_RETRIES:
            import asyncio
            await asyncio.sleep(RETRY_DELAY * attempt)

    logger.error("Не удалось получить расписание после %d попыток", MAX_RETRIES)
    raise RuntimeError(
        f"Не удалось получить расписание: {last_error}"
    ) from last_error


async def get_schedule_for_date(target: datetime.date) -> DaySchedule:
    """Получить расписание на конкретную дату.

    Если на эту дату занятий нет — возвращает пустой DaySchedule.
    """
    monday, sunday = get_week_bounds(target)
    days = await fetch_week_schedule(monday, sunday)

    for day in days:
        if day.date == target:
            return day

    # Занятий на эту дату нет — возвращаем пустой день
    weekdays = [
        "Понедельник", "Вторник", "Среда",
        "Четверг", "Пятница", "Суббота", "Воскресенье",
    ]
    weekday_name = weekdays[target.weekday()]
    return DaySchedule(weekday=weekday_name, date=target, lessons=[])
