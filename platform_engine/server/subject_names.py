"""Conservative, class-aware subject label proposals; never changes a timetable.

Only the two explicitly authorised abbreviated labels are matched. Full course
names, combined class labels and all unrecognised strings remain unchanged.
This module is intentionally not wired into import, draft or publication code.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# Excel commonly contains NBSP/narrow NBSP. Do not treat newlines, tabs, Unicode
# digits, dashes, multiple class letters or combined classes as one class.
_SPACES = ' \u00a0\u202f'
_CLASS = re.compile(r'(?P<grade>[1-9]|1[01])(?:[ \u00a0\u202f]?[A-Za-zА-Яа-яЁё])?\Z')
_ALIASES = {
    'спец. математика': ('Доп.математика', frozenset({7})),
    'спец.математика': ('Доп.математика', frozenset({7})),
    'спецматематика': ('Доп.математика', frozenset({7})),
    'спец. английский': ('Доп.английский', frozenset({7, 8, 9})),
    'спец.английский': ('Доп.английский', frozenset({7, 8, 9})),
    'спецанглийский': ('Доп.английский', frozenset({7, 8, 9})),
}
_REASONS = {
    'Доп.математика': 'Согласованное переименование сокращения спецматематики только для 7 класса.',
    'Доп.английский': 'Согласованное переименование сокращения спецанглийского только для 7–9 классов.',
}


def class_grade(class_name: str) -> int | None:
    """Return a grade only for an unambiguous full class label, e.g. 7А/7 A."""
    if not isinstance(class_name, str):
        raise TypeError('class_name must be a string')
    match = _CLASS.fullmatch(class_name.strip(_SPACES))
    return int(match['grade']) if match else None


def normalise_subject_name(class_name: str, subject: str) -> str:
    """Return the authorised display name, or the exact original subject.

    Matching is case-insensitive and tolerates only ordinary/NBSP/narrow-NBSP
    surrounding spaces and the single space in the explicit dotted alias. It
    does not infer equivalence from other spellings or from a full course name.
    """
    if not isinstance(subject, str):
        raise TypeError('subject must be a string')
    grade = class_grade(class_name)
    key = subject.strip(_SPACES).replace('\u00a0', ' ').replace('\u202f', ' ').casefold()
    rule = _ALIASES.get(key)
    return rule[0] if rule and grade in rule[1] else subject


def propose_subject_renames(draft: Mapping[str, Any]) -> list[dict[str, str]]:
    """Return an allowlisted dry-run diff without mutating or copying raw data.

    The entire lessons envelope is validated first so malformed input cannot
    yield a misleading partial report. Only id/class/before/after/reason leave
    the function. This helper does not read/write storage or apply proposals.
    """
    if not isinstance(draft, Mapping) or not isinstance(draft.get('lessons'), list):
        raise ValueError('Ожидается JSON-объект черновика с массивом lessons.')
    seen: set[str] = set()
    for lesson in draft['lessons']:
        if not isinstance(lesson, Mapping) or any(
            not isinstance(lesson.get(key), str) for key in ('id', 'className', 'subject')
        ):
            raise ValueError('Каждое занятие должно содержать строковые id, className и subject.')
        if not lesson['id'].strip() or lesson['id'] in seen:
            raise ValueError('Идентификаторы занятий должны быть непустыми и уникальными.')
        seen.add(lesson['id'])
    result: list[dict[str, str]] = []
    for lesson in draft['lessons']:
        after = normalise_subject_name(lesson['className'], lesson['subject'])
        if after != lesson['subject']:
            result.append({'id': lesson['id'], 'class': lesson['className'],
                           'before': lesson['subject'], 'after': after,
                           'reason': _REASONS[after]})
    return result
