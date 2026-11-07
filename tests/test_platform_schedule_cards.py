from base64 import b64decode
from datetime import date
from io import BytesIO
import unittest

from PIL import Image

from app.platform_schedule_cards import day_pages, render_card, timetable_days, week_pages


def lesson(index, day=0, **overrides):
    return {
        'id': str(index), 'day': day, 'slot': index + 1, 'className': 'ИФ-7А',
        'start': '08:00', 'end': '08:40', 'subject': 'Математика',
        'teachers': ['Марина Демо'], 'rooms': ['Кабинет 101'], **overrides,
    }


class ScheduleCardsTests(unittest.TestCase):
    def assert_preview_safe(self, page, *, week, page_count=1):
        rendered = render_card(page, week=week, scope='Славик Демо',
                               period='2 ноября 2026', state='Публикация №1',
                               page=1, page_count=page_count)
        with Image.open(BytesIO(b64decode(rendered['base64']))) as image:
            self.assertEqual((image.format, image.size), ('JPEG', (960, 960)))
            # Bright pixels identify the text and accent. Compare their bounds
            # with both the reported MAX frame and a wider 4:3 cover preview.
            bright = image.convert('L').point(lambda level: 255 if level > 145 else 0)
            left, top, right, bottom = bright.getbbox()
            self.assertGreaterEqual(left, 90)
            self.assertLessEqual(right, 870)
            for width, height in ((500, 436), (4, 3)):
                lost = (image.height - image.width * height / width) / 2
                self.assertGreaterEqual(top, lost + 8)
                self.assertLessEqual(bottom, image.height - lost - 8)

    def test_week_keeps_empty_days_and_fits_the_preview(self):
        days = timetable_days([lesson(0), lesson(1, 3)], date(2026, 11, 2))
        pages = week_pages(days)
        self.assertEqual(len(pages), 1)
        self.assertEqual([day.index for day in pages[0]], list(range(6)))
        self.assertEqual([day.index for day in pages[0] if not day.blocks], [1, 2, 4, 5])
        self.assert_preview_safe(pages[0], week=True)
        self.assert_preview_safe(day_pages(days[1])[0], week=False)

    def test_dense_day_and_week_paginate_without_losing_lessons(self):
        days = timetable_days([lesson(index) for index in range(14)], date(2026, 11, 2))
        for week, pages in ((False, day_pages(days[0])), (True, week_pages(days))):
            with self.subTest(week=week):
                self.assertGreater(len(pages), 1)
                self.assertEqual([row['id'] for page in pages for day in page for row in day.lessons],
                                 [str(index) for index in range(14)])
                for page in pages:
                    self.assert_preview_safe(page, week=week, page_count=len(pages))

    def test_long_lesson_continues_on_next_page_without_dropping_details(self):
        row = lesson(0, subject='Ж' * 160, teachers=['Ж' * 119 + str(i) for i in range(8)],
                     rooms=['Ж' * 79 + str(i) for i in range(8)])
        day = timetable_days([row], date(2026, 11, 2))[0]
        pages = day_pages(day)
        self.assertGreater(len(pages), 1)
        blocks = [block for page in pages for part in page for block in part.blocks]
        lines = [line.text for block in blocks for line in block.lines]
        count = sum(line.count('Ж') for line in lines)
        self.assertEqual(count, 160 + 8 * (119 + 79))
        self.assertEqual([item for page in pages for part in page for item in part.lessons], [row])
        for page in pages:
            self.assert_preview_safe(page, week=False, page_count=len(pages))
