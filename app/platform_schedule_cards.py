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
WIDTH = 960
CONTENT_LEFT = 104
CONTENT_RIGHT = 856
BODY_TOP = 291
BODY_BOTTOM = 794
BODY_HEIGHT = BODY_BOTTOM - BODY_TOP
SECTION_HEIGHT = 41
ROW_PADDING = 21
FONT_PATH = Path(__file__).resolve().parents[1] / 'platform_engine/server/assets/Inter.ttf'
TEXT = '#f5f6fc'
MUTED = '#bfc8e0'
ACCENT = '#c5b8ff'


@dataclass(frozen=True)
class Day:
    index: int
    date: date | None
    lessons: tuple[dict, ...]
    blocks: tuple['LessonBlock', ...] = ()


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


@dataclass(frozen=True)
class Line:
    text: str
    size: int
    height: int
    color: str
    bold: bool = False


@dataclass(frozen=True)
class LessonBlock:
    row: dict
    lines: tuple[Line, ...]
    continuation: bool = False

    @property
    def height(self):
        return ROW_PADDING + sum(line.height for line in self.lines)


@lru_cache(maxsize=12)
def _font(size):
    return ImageFont.truetype(str(FONT_PATH), size)


def _wrap(value, width, size):
    font = _font(size)
    line = ''
    for word in value.split():
        candidate = f'{line} {word}' if line else word
        if font.getlength(candidate) <= width:
            line = candidate
            continue
        if line:
            yield line
        # Imported names may have no whitespace. Break at the widest prefix.
        while font.getlength(word) > width:
            stop = 1
            while stop < len(word) and font.getlength(word[:stop + 1]) <= width:
                stop += 1
            yield word[:stop]
            word = word[stop:]
        line = word
    if line:
        yield line


def _lesson_blocks(row, *, week):
    width = CONTENT_RIGHT - CONTENT_LEFT - 53
    time = '–'.join(row[key] for key in ('start', 'end') if row.get(key))
    summary = '  ·  '.join(filter(None, (time, row.get('className'))))
    subject = row.get('subject') or 'Занятие'
    teachers = ', '.join(row.get('teachers') or ())
    rooms = ', '.join(row.get('rooms') or ())
    details = '  ·  '.join(filter(None, (teachers, rooms)))
    title_size = 30 if week else 33
    lines = [
        *(Line(text, 24, 28, ACCENT, True) for text in _wrap(summary, width, 24)),
        *(Line(text, title_size, title_size + 6, TEXT, True) for text in _wrap(subject, width, title_size)),
        *(Line(text, 22, 26, MUTED) for text in _wrap(details, width, 22)),
    ]
    max_height = BODY_HEIGHT - ROW_PADDING - (SECTION_HEIGHT if week else 0)
    chunk = []
    height = 0
    continued = False
    for line in lines:
        if height + line.height > max_height:
            yield LessonBlock(row, tuple(chunk), continued)
            continued = True
            chunk = [Line('Продолжение занятия', 24, 29, ACCENT, True)]
            height = 29
        chunk.append(line)
        height += line.height
    yield LessonBlock(row, tuple(chunk), continued)


def _pages(days, *, week):
    pages = []
    page = []
    used = 0
    for day in days:
        blocks = [block for row in day.lessons for block in _lesson_blocks(row, week=week)]
        if not blocks:
            if used + SECTION_HEIGHT > BODY_HEIGHT:
                pages.append(tuple(page))
                page, used = [], 0
            page.append(day)
            used += SECTION_HEIGHT
            continue
        for block in blocks:
            same_day = bool(page and page[-1].index == day.index)
            heading = 0 if same_day or not week else SECTION_HEIGHT
            if used + heading + block.height > BODY_HEIGHT:
                pages.append(tuple(page))
                page, used = [], 0
                same_day = False
                heading = SECTION_HEIGHT if week else 0
            # A split lesson keeps its details on the next page. The message
            # caption lists the lesson once, alongside the first fragment.
            lessons = () if block.continuation else (block.row,)
            if same_day:
                previous = page[-1]
                page[-1] = Day(day.index, day.date, previous.lessons + lessons,
                               previous.blocks + (block,))
            else:
                page.append(Day(day.index, day.date, lessons, (block,)))
            used += heading + block.height
    if page:
        pages.append(tuple(page))
    return pages


def week_pages(days):
    return _pages(days, week=True)


def day_pages(day):
    return _pages((day,), week=False)


def _draw_text(draw, x, y, value, size, color=TEXT, bold=False):
    draw.text((x, y), value, font=_font(size), fill=color, anchor='lt',
              stroke_width=int(bold), stroke_fill=color)


def _draw_block(draw, y, block):
    draw.rounded_rectangle((CONTENT_LEFT, y, CONTENT_RIGHT, y + block.height - 8),
                           radius=17, fill='#26324f', outline='#3b4b6d', width=1)
    draw.rounded_rectangle((CONTENT_LEFT + 12, y + 12, CONTENT_LEFT + 17,
                            y + block.height - 19), radius=2, fill='#8d79e7')
    line_y = y + 9
    for line in block.lines:
        _draw_text(draw, CONTENT_LEFT + 32, line_y, line.text, line.size, line.color, line.bold)
        line_y += line.height


def render_card(days, *, week, scope, period, state='', page=1, page_count=1):
    """Render a square with all content inside the central landscape preview."""
    image = Image.new('RGB', (WIDTH, WIDTH))
    draw = ImageDraw.Draw(image)
    for y in range(WIDTH):
        fraction = y / (WIDTH - 1)
        draw.line((0, y, WIDTH, y), fill=(int(32 - 16 * fraction), int(39 - 14 * fraction),
                                        int(71 - 25 * fraction)))
    draw.rounded_rectangle((46, 48, WIDTH - 47, WIDTH - 49), radius=42, fill='#1a233b',
                           outline='#465271', width=2)
    used = sum(
        sum(block.height for block in day.blocks)
        + (SECTION_HEIGHT if week else 50 if not day.blocks else 0)
        for day in days
    )
    # Keep sparse day cards balanced while reserving the same safe area for
    # dense days, page labels and the cropped chat preview.
    offset = min(108, max(0, (BODY_HEIGHT - used) // 2))
    x = CONTENT_LEFT
    draw.rounded_rectangle((x, 133 + offset, x + 5, 157 + offset), radius=2, fill='#a991ff')
    _draw_text(draw, x + 17, 134 + offset, 'ИДЕЯФИКС  /  РАСПИСАНИЕ', 21, ACCENT, True)
    title = 'Расписание недели' if week else WEEKDAYS[days[0].index]
    _draw_text(draw, x, 165 + offset, title, 46, bold=True)
    _draw_text(draw, x, 225 + offset, period, 25, MUTED)
    meta = '  ·  '.join(filter(None, (scope, state)))
    for index, line in enumerate(_wrap(meta, CONTENT_RIGHT - x - 4, 20)):
        if index >= 2:
            break
        _draw_text(draw, x, 252 + 20 * index + offset, line, 20, ACCENT, True)

    y = BODY_TOP + offset
    for day in days:
        if week:
            label = WEEKDAYS[day.index]
            if day.date:
                label += f'  ·  {day.date:%d.%m}'
            if not day.blocks:
                label += '  ·  Нет занятий'
            _draw_text(draw, x, y + 8, label, 25, ACCENT if day.blocks else MUTED, bool(day.blocks))
            y += SECTION_HEIGHT
        elif not day.blocks:
            draw.rounded_rectangle((x, y, CONTENT_RIGHT, y + 50), radius=14, fill='#232e48')
            _draw_text(draw, x + 18, y + 13, 'Нет занятий в этой сетке', 24, MUTED)
        for block in day.blocks:
            _draw_block(draw, y, block)
            y += block.height
    if page_count > 1:
        _draw_text(draw, x, 803, f'Страница {page} из {page_count}', 21, MUTED)
    output = BytesIO()
    image.save(output, 'JPEG', quality=90, optimize=True, subsampling=0)
    return {'name': 'schedule-week.jpg' if week else 'schedule-day.jpg',
            'contentType': 'image/jpeg', 'base64': b64encode(output.getvalue()).decode('ascii')}
