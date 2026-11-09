"""Рендеринг графической карточки опроса (Pillow).

Карточка повторяет визуальный язык тёмной темы Telegram: круглая аватарка,
имя участника, статус варианта и общий счётчик доступных мест.
"""

from __future__ import annotations

import datetime
import io
import logging
import zoneinfo
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from schedule_bot import config
from schedule_bot.polls.models import Booking, STATUS_ACTIVE, STATUS_EXPIRED, PollView

logger = logging.getLogger(__name__)

_TZ = zoneinfo.ZoneInfo(config.TIMEZONE)

WIDTH = 1080
PAD = 40
INNER = 24
RADIUS = 26
NUMBER_SIZE = 48
AVATAR_SIZE = 58
AVATAR_OVERLAP = 16
MAX_AVATARS = 4

BG_TOP = (14, 21, 29)
BG_BOTTOM = (24, 34, 45)
CARD_FREE = (26, 38, 50)
CARD_TAKEN = (37, 31, 39)
TEXT = (245, 247, 250)
MUTED = (147, 163, 179)
SUBTLE = (100, 116, 132)
ACCENT = (51, 144, 236)
GREEN = (72, 209, 137)
RED = (242, 104, 104)
BORDER = (45, 60, 76)

_FONT_FILES_REGULAR = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
)
_FONT_FILES_BOLD = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/seguisb.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
)


@lru_cache(maxsize=64)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = _FONT_FILES_BOLD if bold else _FONT_FILES_REGULAR
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _text_width(font: ImageFont.FreeTypeFont, text: str) -> int:
    try:
        return int(font.getlength(text))
    except AttributeError:  # pragma: no cover
        return font.getbbox(text)[2]


def _hard_split(word: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    parts: list[str] = []
    current = ""
    for char in word:
        if _text_width(font, current + char) <= max_width or not current:
            current += char
        else:
            parts.append(current)
            current = char
    if current:
        parts.append(current)
    return parts


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int, max_lines: int) -> list[str]:
    text = " ".join(text.split())
    if not text:
        return []

    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = f"{current} {word}".strip()
        if _text_width(font, candidate) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        pieces = _hard_split(word, font, max_width)
        if len(pieces) == 1:
            current = pieces[0]
        else:
            lines.extend(pieces[:-1])
            current = pieces[-1]

    if current:
        lines.append(current)

    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and _text_width(font, last + "…") > max_width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


def _ellipsize(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    text = " ".join(text.split())
    if _text_width(font, text) <= max_width:
        return text
    while text and _text_width(font, text + "…") > max_width:
        text = text[:-1]
    return text.rstrip() + "…"


def _participant_label(booking: Booking) -> str:
    if booking.username:
        return f"{booking.user_name} (@{booking.username})"
    return booking.user_name


def _initials(name: str) -> str:
    parts = [part for part in name.replace("_", " ").split() if part]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


_AVATAR_COLORS = (
    (51, 144, 236),
    (94, 129, 244),
    (151, 116, 232),
    (232, 112, 164),
    (236, 122, 95),
    (232, 164, 76),
    (86, 184, 122),
    (65, 176, 190),
)


def _avatar_color(user_id: int) -> tuple[int, int, int]:
    return _AVATAR_COLORS[user_id % len(_AVATAR_COLORS)]


def _draw_avatar(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    size: int,
    *,
    path: Path | None,
    name: str,
    user_id: int,
) -> None:
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rendered = False

    if path is not None:
        try:
            photo = Image.open(path)
            photo = ImageOps.exif_transpose(photo).convert("RGB")
            side = min(photo.size)
            left = (photo.width - side) // 2
            top = (photo.height - side) // 2
            photo = photo.crop((left, top, left + side, top + side))
            photo = photo.resize((size, size), Image.Resampling.LANCZOS)

            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
            layer.paste(photo, (0, 0), mask)
            rendered = True
        except Exception:
            logger.debug("Не удалось прочитать аватарку %s", path, exc_info=True)

    if not rendered:
        circle = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        circle_draw = ImageDraw.Draw(circle)
        circle_draw.ellipse((0, 0, size - 1, size - 1), fill=(*_avatar_color(user_id), 255))
        initials = _initials(name)
        font = _font(max(18, size // 2 - 4), bold=True)
        text_w = _text_width(font, initials)
        bbox = font.getbbox(initials)
        text_h = bbox[3] - bbox[1]
        circle_draw.text(
            ((size - text_w) / 2, (size - text_h) / 2 - bbox[1]),
            initials,
            font=font,
            fill=(255, 255, 255),
        )
        layer = circle

    canvas.alpha_composite(layer, (x, y))
    draw.ellipse((x, y, x + size - 1, y + size - 1), outline=BG_TOP, width=3)


def _draw_status_badge(
    draw: ImageDraw.ImageDraw,
    right: int,
    y: int,
    option_view,
) -> tuple[int, int]:
    if option_view.is_full:
        color = RED
        background = (59, 31, 38)
        if option_view.option.max_participants > 1:
            label = f"Занято {option_view.taken}/{option_view.option.max_participants}"
        else:
            label = "Занято"
    else:
        color = GREEN
        background = (24, 53, 43)
        if option_view.option.max_participants > 1:
            label = f"Свободно {option_view.taken}/{option_view.option.max_participants}"
        else:
            label = "Свободно"

    font = _font(22, bold=True)
    text_w = _text_width(font, label)
    pill_w = text_w + 46
    pill_h = 38
    x = right - pill_w

    draw.rounded_rectangle(
        (x, y, right, y + pill_h), radius=pill_h // 2, fill=background
    )
    draw.ellipse((x + 15, y + pill_h // 2 - 5, x + 25, y + pill_h // 2 + 5), fill=color)
    bbox = font.getbbox(label)
    draw.text(
        (x + 33, y + (pill_h - (bbox[3] - bbox[1])) / 2 - bbox[1]),
        label,
        font=font,
        fill=color,
    )
    return pill_w, pill_h


def _format_expiry(poll) -> str:
    if not poll.expires_at:
        return "Бессрочно"
    try:
        expires = datetime.datetime.fromisoformat(poll.expires_at)
    except ValueError:
        return ""
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=_TZ)
    local = expires.astimezone(_TZ)
    return f"до {local:%d.%m.%Y %H:%M}"


def _header_status(poll_view: PollView) -> tuple[str, tuple[int, int, int]]:
    if poll_view.poll.status != STATUS_ACTIVE:
        return "Опрос закрыт", RED
    return (
        f"Доступно: {poll_view.available_options} из {poll_view.total_options}",
        GREEN if poll_view.available_options else RED,
    )


def render_poll_card(
    view: PollView,
    avatars: dict[int, Path | None] | None = None,
    *,
    now: datetime.datetime | None = None,
) -> bytes:
    """Отрисовать карточку опроса и вернуть PNG в виде байтов."""
    avatars = avatars or {}
    now = now or datetime.datetime.now(tz=_TZ)

    title_font = _font(46, bold=True)
    desc_font = _font(26)
    option_font = _font(29, bold=True)
    meta_font = _font(23)
    small_font = _font(21)
    number_font = _font(24, bold=True)

    content_width = WIDTH - 2 * PAD
    title_lines = _wrap(view.poll.title, title_font, content_width, max_lines=3)
    desc_lines = (
        _wrap(view.poll.description, desc_font, content_width, max_lines=3)
        if view.poll.description
        else []
    )

    blocks: list[dict] = []
    for index, option_view in enumerate(view.options, start=1):
        badge_text_width = 0
        if option_view.option.max_participants > 1:
            badge_text_width = _text_width(meta_font, "Свободно 0/0") + 46
        else:
            badge_text_width = _text_width(meta_font, "Свободно") + 46

        title_max = content_width - 2 * INNER - NUMBER_SIZE - 20 - badge_text_width - 18
        title_lines_opt = _wrap(option_view.option.title, option_font, title_max, max_lines=2)

        participants_height = 0
        show_people = view.poll.show_participants and bool(option_view.bookings)
        if show_people:
            participants_height = AVATAR_SIZE + 12

        block_height = INNER + max(
            NUMBER_SIZE,
            len(title_lines_opt) * 36 + (participants_height or 0),
        ) + INNER
        blocks.append(
            {
                "index": index,
                "view": option_view,
                "title_lines": title_lines_opt,
                "participants_height": participants_height,
                "show_people": show_people,
                "height": int(block_height),
            }
        )

    header_height = 34 + len(title_lines) * 56
    if desc_lines:
        header_height += 12 + len(desc_lines) * 36
    header_height += 26 + 38 + 26

    notice_height = 54 if view.poll.status != STATUS_ACTIVE else 0
    blocks_height = sum(block["height"] for block in blocks) + max(0, len(blocks) - 1) * 16
    footer_height = 70
    height = PAD + header_height + notice_height + blocks_height + footer_height + PAD

    image = Image.new("RGBA", (WIDTH, height), BG_BOTTOM)
    background = Image.new("RGBA", (WIDTH, height))
    bg_draw = ImageDraw.Draw(background)
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = tuple(
            int(BG_TOP[channel] + (BG_BOTTOM[channel] - BG_TOP[channel]) * ratio)
            for channel in range(3)
        )
        bg_draw.line((0, y, WIDTH, y), fill=(*color, 255))
    image.alpha_composite(background)

    draw = ImageDraw.Draw(image)
    y = PAD

    draw.text((PAD, y), "ОПРОС", font=_font(20, bold=True), fill=ACCENT)
    y += 34

    for line in title_lines:
        draw.text((PAD, y), line, font=title_font, fill=TEXT)
        y += 56

    if desc_lines:
        y += 12
        for line in desc_lines:
            draw.text((PAD, y), line, font=desc_font, fill=MUTED)
            y += 36

    y += 26
    status_text, status_color = _header_status(view)
    status_background = (24, 53, 43) if status_color == GREEN else (59, 31, 38)
    status_font = _font(25, bold=True)
    text_w = _text_width(status_font, status_text)
    pill_h = 42
    draw.rounded_rectangle(
        (PAD, y, PAD + text_w + 58, y + pill_h),
        radius=pill_h // 2,
        fill=status_background,
        outline=(*status_color, 255),
        width=1,
    )
    draw.ellipse((PAD + 18, y + pill_h // 2 - 5, PAD + 28, y + pill_h // 2 + 5), fill=status_color)
    bbox = status_font.getbbox(status_text)
    draw.text(
        (PAD + 38, y + (pill_h - (bbox[3] - bbox[1])) / 2 - bbox[1]),
        status_text,
        font=status_font,
        fill=status_color,
    )

    expiry_text = _format_expiry(view.poll)
    if expiry_text:
        expiry_font = _font(23)
        expiry_text = _ellipsize(expiry_text, expiry_font, content_width // 2)
        expiry_w = _text_width(expiry_font, expiry_text)
        draw.text((PAD + content_width - expiry_w, y + 10), expiry_text, font=expiry_font, fill=SUBTLE)

    y += pill_h + 26

    if notice_height:
        notice_text = "Опрос закрыт организатором"
        if view.poll.status == STATUS_EXPIRED:
            notice_text = "Срок действия опроса истёк"
        draw.rounded_rectangle(
            (PAD, y, PAD + content_width, y + 48), radius=16, fill=(61, 30, 37)
        )
        notice_font = _font(23, bold=True)
        draw.text((PAD + 20, y + 11), notice_text, font=notice_font, fill=RED)
        y += notice_height

    for block_index, block in enumerate(blocks):
        option_view = block["view"]
        block_height = block["height"]
        card_fill = CARD_TAKEN if option_view.is_full else CARD_FREE
        draw.rounded_rectangle(
            (PAD, y, PAD + content_width, y + block_height),
            radius=RADIUS,
            fill=card_fill,
            outline=BORDER,
            width=1,
        )

        number_y = y + INNER
        draw.ellipse(
            (PAD + INNER, number_y, PAD + INNER + NUMBER_SIZE, number_y + NUMBER_SIZE),
            fill=(*ACCENT, 255) if not option_view.is_full else (68, 82, 98, 255),
        )
        number_text = str(block["index"])
        number_w = _text_width(number_font, number_text)
        nb = number_font.getbbox(number_text)
        draw.text(
            (
                PAD + INNER + (NUMBER_SIZE - number_w) / 2,
                number_y + (NUMBER_SIZE - (nb[3] - nb[1])) / 2 - nb[1],
            ),
            number_text,
            font=number_font,
            fill=(255, 255, 255),
        )

        badge_w, _badge_h = _draw_status_badge(
            draw,
            PAD + content_width - INNER,
            y + INNER + 4,
            option_view,
        )

        text_x = PAD + INNER + NUMBER_SIZE + 20
        title_y = y + INNER + 4
        for line in block["title_lines"]:
            draw.text((text_x, title_y), line, font=option_font, fill=TEXT)
            title_y += 36

        if block["show_people"]:
            avatar_y = y + INNER + max(NUMBER_SIZE, len(block["title_lines"]) * 36) + 8
            count = min(len(option_view.bookings), MAX_AVATARS)
            avatar_x = text_x
            for booking in option_view.bookings[:count]:
                _draw_avatar(
                    image,
                    draw,
                    avatar_x,
                    avatar_y,
                    AVATAR_SIZE,
                    path=avatars.get(booking.user_id),
                    name=booking.user_name,
                    user_id=booking.user_id,
                )
                avatar_x += AVATAR_SIZE - AVATAR_OVERLAP

            extra = len(option_view.bookings) - count
            if extra > 0:
                circle_x = avatar_x
                draw.ellipse(
                    (circle_x, avatar_y, circle_x + AVATAR_SIZE, avatar_y + AVATAR_SIZE),
                    fill=(54, 69, 86),
                    outline=BG_TOP,
                    width=3,
                )
                extra_text = f"+{extra}"
                extra_font = _font(22, bold=True)
                ew = _text_width(extra_font, extra_text)
                eb = extra_font.getbbox(extra_text)
                draw.text(
                    (
                        circle_x + (AVATAR_SIZE - ew) / 2,
                        avatar_y + (AVATAR_SIZE - (eb[3] - eb[1])) / 2 - eb[1],
                    ),
                    extra_text,
                    font=extra_font,
                    fill=TEXT,
                )
                avatar_x += AVATAR_SIZE - AVATAR_OVERLAP

            names = [_participant_label(booking) for booking in option_view.bookings]
            if extra > 0:
                names = names[:count]
                names.append(f"+{extra}")
            names_text = ", ".join(names)
            names_max = PAD + content_width - INNER - avatar_x - 26
            names_text = _ellipsize(names_text, meta_font, max(60, names_max))
            nb2 = meta_font.getbbox(names_text)
            names_y = avatar_y + (AVATAR_SIZE - (nb2[3] - nb2[1])) / 2 - nb2[1]
            draw.text((avatar_x + 18, names_y), names_text, font=meta_font, fill=MUTED)

        y += block_height + (16 if block_index < len(blocks) - 1 else 0)

    footer_y = height - PAD - 24
    draw.line((PAD, footer_y - 18, PAD + content_width, footer_y - 18), fill=BORDER, width=1)
    footer_left = "Опросы с бронированием"
    draw.text((PAD, footer_y), footer_left, font=small_font, fill=SUBTLE)
    footer_right = f"Обновлено {now:%d.%m.%Y %H:%M}"
    footer_w = _text_width(small_font, footer_right)
    draw.text((PAD + content_width - footer_w, footer_y), footer_right, font=small_font, fill=SUBTLE)

    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
