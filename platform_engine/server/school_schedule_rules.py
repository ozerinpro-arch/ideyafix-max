"""School rules explicitly given by the user, with narrow course evidence.

Saturday eligibility is a category decision, never a room/teacher heuristic.
The reviewed tariff row IDs below identify courses in the actual DO curriculum
or a specific DO condition; generic English/Math/CS labels are not promoted.
"""
from collections import defaultdict
import re

from .scheduling_models import canonical

MAX_LESSONS_PER_DAY = 10
CURRICULUM_SHA256 = '30e1a55ea93e2cf10cd1f618873d08bbb68296e2884f9776c15681af61fc95cc'
# tariff row -> exact course labels permitted by the reviewed DO evidence.
# Locators refer to the 65-row curriculum table, not a supposed main curriculum.
DO_TARIFF_ROWS = {
    2: ({'Arduino'}, 'curriculum:tbl[1]/r17'),
    3: ({'Deutsch', 'Китайский'}, 'curriculum:tbl[1]/r7,r15'),
    4: ({'Deutsch', 'Китайский'}, 'curriculum:tbl[1]/r7,r15'),
    5: ({'Deutsch', 'Китайский'}, 'curriculum:tbl[1]/r7,r15'),
    6: ({'Deutsch', 'Китайский'}, 'curriculum:tbl[1]/r7,r15,r25;reviewed_course_correction'),
    7: ({'Deutsch'}, 'curriculum:tbl[1]/r7,r15,r25,r34'),
    8: ({'IoT'}, 'curriculum:tbl[1]/r27'),
    9: ({'Kerbal Space'}, 'curriculum:tbl[1]/r4;DO2026-p6'),
    10: ({'Kerbal Space'}, 'curriculum:tbl[1]/r4;DO2026-p6'),
    11: ({'Native English (extra)'}, 'curriculum:tbl[1]/r54'),
    12: ({'Native English'}, 'curriculum:tbl[1]/r6,r14,r20,r31,r41,r46'),
    13: ({'Native English'}, 'curriculum:tbl[1]/r6,r14,r20,r31,r41,r46'),
    14: ({'Python'}, 'curriculum:tbl[1]/r19'),
    15: ({'Science'}, 'curriculum:tbl[1]/r5'),
    16: ({'Science'}, 'curriculum:tbl[1]/r13'),
    17: ({'Science'}, 'curriculum:tbl[1]/r21'),
    18: ({'Science'}, 'curriculum:tbl[1]/r30'),
    27: ({'БПЛА'}, 'curriculum:tbl[1]/r37,r49'),
    41: ({'Интернет вещей'}, 'curriculum:tbl[1]/r36,r48;DO2026-p7'),
    45: ({'Искусственный интеллект'}, 'curriculum:tbl[1]/r28'),
    49: ({'Кибербезопасность'}, 'curriculum:tbl[1]/r18'),
    50: ({'Курс по выбору английский'}, 'curriculum:tbl[1]/r23,r33,r40'),
    55: ({'Математика на английском'}, 'curriculum:tbl[1]/r3,r12'),
    59: ({'Начертательная геометрия'}, 'curriculum:tbl[1]/r24'),
    63: ({'Основы физических явлений'}, 'curriculum:tbl[1]/r2'),
    64: ({'Основы электроники'}, 'curriculum:tbl[1]/r10'),
    90: ({'САПР'}, 'curriculum:tbl[1]/r32'),
    109: ({'Китайский'}, 'supplementary:DO2026-p23'),
    110: ({'Китайский'}, 'supplementary:DO2026-p23'),
}


def _rows(provenance):
    result = set(provenance.get('sourceRows', []))
    for cell in provenance.get('sourceCells', []):
        match = re.fullmatch(r'[A-Z]{1,3}([1-9][0-9]*)', cell) if isinstance(cell, str) else None
        if match:
            result.add(int(match[1]))
    return result


def saturday_eligibility(demand, provenance=None):
    """Return explicit eligibility; a missing/ambiguous category stays blocked."""
    subject = canonical(demand['subject'])
    if subject.startswith('спец'):
        return {'allowed': True, 'status': 'explicit_special', 'evidence': ['user:saturday_only_DO_or_SPECIAL']}
    provenance = provenance or {}
    extra_courses = {'DO2026-p10': 'Школа дронов', 'DO2026-p5': 'Программирование Unity'}
    conditions = [ident for ident in provenance.get('sourceConditions', [])
                  if ident in extra_courses and subject == canonical(extra_courses[ident])]
    if conditions:
        return {'allowed': True, 'status': 'source_confirmed_DO_category',
                'evidence': ['supplementary:' + ident for ident in sorted(conditions)]}
    rows = _rows(provenance)
    # These display aliases were explicitly approved earlier; raw tariff rows
    # identify the special course. An unlinked manually added alias is blocked.
    if ((subject == canonical('Доп.математика') and rows and rows <= {91, 92, 93, 94})
            or (subject == canonical('Доп.английский') and rows and rows <= {95, 96, 97, 98, 99})):
        return {'allowed': True, 'status': 'source_special_alias',
                'evidence': [f'tariff:row{row}' for row in sorted(rows)]}
    matched = []
    for row in rows:
        entry = DO_TARIFF_ROWS.get(row)
        if entry and subject in {canonical(label) for label in entry[0]}:
            matched.append((row, entry[1]))
    if rows and len(matched) == len(rows):
        return {'allowed': True, 'status': 'source_confirmed_DO_category',
                'evidence': [f'tariff:row{row};{locator}' for row, locator in sorted(matched)]}
    return {'allowed': False, 'status': 'category_not_confirmed_for_saturday',
            'evidence': [], 'reason': 'Нет отдельного доказательства категории ДО/СПЕЦ; суббота запрещена.'}


def validate_school_schedule_rules(plan, assignments, provenance=()):
    """Independent school-only checks for builder and manually edited versions.

    The caller supplies immutable course provenance keyed by current demandId.
    Existing structural/interval validators remain authoritative for other rules.
    """
    source = {row['demandId']: row for row in provenance}
    slots = {row['id']: row for row in plan['slots']}
    demands = {f"{row['id']}_{n}": row for row in plan['demands'] for n in range(row['weeklyCount'])}
    active = []
    issues = []
    teacher_days = defaultdict(set)
    group_days = defaultdict(set)
    for assignment in assignments:
        demand, slot = demands.get(assignment['eventId']), slots.get(assignment['slotId'])
        if not demand or not slot:
            continue  # The structural validator reports bad/missing references.
        active.append((assignment['eventId'], demand, slot['day']))
        if slot['day'] == 5 and not saturday_eligibility(demand, source.get(demand['id']))['allowed']:
            issues.append({'code': 'school_saturday_category',
                'message': 'В субботу разрешены только явно подтверждённые ДО или СПЕЦ.',
                'entityIds': [assignment['eventId'], demand['id']]})
        for teacher in demand['teacherIds']:
            teacher_days[(teacher, slot['day'])].add(assignment['eventId'])
        for group in demand['groupIds']:
            group_days[(group, slot['day'])].add(assignment['eventId'])
    for (teacher, _day), event_ids in teacher_days.items():
        if len(event_ids) > MAX_LESSONS_PER_DAY:
            issues.append({'code': 'school_daily_limit', 'message': 'Школьный предел — 10 уроков в день.',
                           'entityIds': [teacher, *sorted(event_ids)]})
    classes = defaultdict(list)
    for group in plan['groups']:
        classes[canonical(group['className'])].append(group)
    for roster in classes.values():
        whole = [row for row in roster if row['kind'] == 'whole']
        parts = [row for row in roster if row['kind'] == 'subgroup']
        if parts and all(row.get('attendanceUnitIds') for row in parts):
            membership = defaultdict(set)
            for group in parts:
                for unit in group['attendanceUnitIds']:
                    membership[unit].add(group['id'])
            patterns = {tuple(sorted(owners)) for owners in membership.values()}
            routes = [{row['id'] for row in whole} | set(pattern) for pattern in patterns]
        else:
            routes = []
            for representative in parts or roster:
                owners = {row['id'] for row in roster if row['kind'] == 'whole' or row['id'] == representative['id']
                    or (row['kind'] == representative['kind'] == 'subgroup'
                        and (not row.get('partition') or row.get('partition') != representative.get('partition')))}
                routes.append(owners)
        for owners in routes:
            by_day = defaultdict(set)
            for owner in owners:
                for day in range(6):
                    by_day[day].update(group_days[(owner, day)])
            for event_ids in by_day.values():
                if len(event_ids) > MAX_LESSONS_PER_DAY:
                    issues.append({'code': 'school_daily_limit', 'message': 'Школьный предел — 10 уроков в день.',
                                   'entityIds': [*sorted(owners), *sorted(event_ids)]})
                    if len(issues) >= 256:
                        return issues
    return issues
