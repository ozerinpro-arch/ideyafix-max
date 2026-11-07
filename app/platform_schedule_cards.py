"""Deterministic, phone-width JPEGs built from authorized timetable projections."""
from base64 import b64encode
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


WEEKDAYS = ('Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота')
MONTHS = ('января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
          'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря')
WIDTH = 720
FONT_PATH = Path(__file__).resolve().parents[1] / 'platform_engine/server/assets/Inter.ttf'
TEXT = '#f5f6fc'
MUTED = '#bfc8e0'
ACCENT = '#c5b8ff'


@dataclass(frozen=True)
class Day:
    index: int
    date: date | None
    lessons: tuple[dict, ...]


def date_label(value):
    return f'{value.day} {MONTHS[value.month - 1]} {value.year}'


def timetable_days(lessons, period_start, class_name=None):
    """Use the source period when supplied. An undated draft has weekdays only."""
    groups = [[] for _ in WEEKDAYS]
    for row in lessons:
        index = row.get('day')
        if type(index) is int and 0 <= index < len(groups) and (
            class_name is None or row.get('className') == class_name
        ):
            groups[index].append(row)
    return tuple(
        Day(index, period_start + timedelta(days=index) if period_start else None,
            tuple(sorted(rows, key=lambda row: (row.get('start', ''), row.get('slot', 0),
                                                row.get('className', ''), row.get('subject', '')))))
        for index, rows in enumerate(groups)
    )


def lesson_count(count):
    if count % 100 in range(11, 15):
        noun = 'занятий'
    else:
        noun = {1: 'занятие', 2: 'занятия', 3: 'занятия', 4: 'занятия'}.get(count % 10, 'занятий')
    return f'{count} {noun}'


def week_pages(days, *, row_limit=9):
    """Bound picture height without hiding lessons or empty days."""
    pages = []
    page = []
    used = 0
    for day in days:
        for start in range(0, max(1, len(day.lessons)), row_limit):
            rows = day.lessons[start:start + row_limit]
            cost = max(1, len(rows))
            if page and used + cost > row_limit:
                pages.append(tuple(page))
                page, used = [], 0
            page.append(Day(day.index, day.date, rows))
            used += cost
    if page:
        pages.append(tuple(page))
    return pages


@lru_cache(maxsize=12)
def _font(size):
    return ImageFont.truetype(str(FONT_PATH), size)


def _line(draw, value, width, size, weight=400):
    value = ' '.join(value.split())
    font = _font(size)
    if draw.textlength(value, font=font) <= width:
        return value
    while value and draw.textlength(value + '…', font=font) > width:
        value = value[:-1]
    return value.rstrip() + '…'


def _wrap(draw, value, width, size, weight=400):
    first, remaining = '', value.split()
    while remaining:
        candidate = ' '.join(filter(None, (first, remaining[0])))
        if draw.textlength(candidate, font=_font(size)) > width:
            break
        first = candidate
        remaining.pop(0)
    if not first:
        return [_line(draw, value, width, size, weight)]
    return [first, _line(draw, ' '.join(remaining), width, size, weight)] if remaining else [first]


def _draw_text(draw, x, y, value, size, color=TEXT, weight=400):
    draw.text((x, y), value, font=_font(size), fill=color, anchor='lt',
              stroke_width=int(weight >= 600), stroke_fill=color)


def render_card(days, *, week, scope, period, state='', page=1, page_count=1):
    compact = week
    row_height = 151
    section_height = 53 if compact else 0
    sections = sum(section_height + (row_height * len(day.lessons) if day.lessons else 67)
                   for day in days)
    height = 235 + (38 if state else 0) + sections + 68
    image = Image.new('RGB', (WIDTH, height), '#11182b')
    draw = ImageDraw.Draw(image)
    for y in range(height):
        fraction = y / max(1, height - 1)
        draw.line((0, y, WIDTH, y), fill=(int(27 - 12 * fraction), int(31 - 10 * fraction),
                                        int(58 - 24 * fraction)))
    draw.rounded_rectangle((25, 25, WIDTH - 26, height - 26), radius=32, fill='#1a233b',
                           outline='#465271', width=2)
    draw.rounded_rectangle((49, 50, 54, 78), radius=2, fill='#a991ff')
    _draw_text(draw, 68, 51, 'ИДЕЯФИКС  /  РАСПИСАНИЕ', 22, ACCENT, 650)
    title = 'Расписание недели' if week else WEEKDAYS[days[0].index]
    _draw_text(draw, 49, 101, title, 43, weight=720)
    _draw_text(draw, 49, 163, _line(draw, period, 620, 25), 25, MUTED)
    _draw_text(draw, 49, 201, _line(draw, scope, 620, 24, 620), 24, ACCENT, 620)
    y = 241
    if state:
        _draw_text(draw, 49, y, _line(draw, state, 620, 22), 22, MUTED)
        y += 38
    for day in days:
        if compact:
            label = WEEKDAYS[day.index]
            if day.date:
                label += f'  ·  {day.date:%d.%m}'
            _draw_text(draw, 49, y + 8, label, 26, ACCENT, 650)
            y += section_height
        if not day.lessons:
            draw.rounded_rectangle((49, y, 670, y + 49), radius=14, fill='#232e48')
            _draw_text(draw, 67, y + 12, 'Нет занятий в этой сетке', 23, MUTED)
            y += 67
            continue
        for row in day.lessons:
            draw.rounded_rectangle((49, y, 670, y + row_height - 10), radius=17,
                                   fill='#26324f', outline='#3b4b6d', width=1)
            draw.rounded_rectangle((62, y + 15, 68, y + row_height - 25), radius=3, fill='#8d79e7')
            time = '–'.join(str(row[key]) for key in ('start', 'end') if row.get(key))
            class_name = str(row.get('className') or '')
            top = '  ·  '.join(filter(None, (time, class_name)))
            _draw_text(draw, 83, y + 14, _line(draw, top, 568, 25, 640), 25, ACCENT, 640)
            subject = str(row.get('subject') or 'Занятие')
            size = 32 if compact else 35
            lines = _wrap(draw, subject, 565, size, 700)
            for index, line in enumerate(lines):
                _draw_text(draw, 83, y + 48 + index * 34, line, size, TEXT, 700)
            teachers = ', '.join(row.get('teachers') or ())
            rooms = ', '.join(row.get('rooms') or ())
            details = '  ·  '.join(filter(None, (teachers, rooms)))
            if details:
                detail_y = y + (91 if len(lines) == 1 else 119)
                _draw_text(draw, 83, detail_y, _line(draw, details, 565, 22), 22, MUTED)
            y += row_height
    if page_count > 1:
        _draw_text(draw, 49, height - 55, f'Страница {page} из {page_count}', 21, MUTED)
    output = BytesIO()
    image.save(output, 'JPEG', quality=90, optimize=True, subsampling=0)
    return {'name': 'schedule-week.jpg' if week else 'schedule-day.jpg',
            'contentType': 'image/jpeg', 'base64': b64encode(output.getvalue()).decode('ascii')}
