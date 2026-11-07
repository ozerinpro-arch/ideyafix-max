"""Read-only personal history and scoped totals from the confirmed lesson journal."""

from collections import defaultdict
from contextlib import ExitStack, closing
from datetime import date, datetime, timedelta, timezone
from math import fsum, isfinite
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from . import school_calendar, school_ops, school_publications
from .school_lesson_progress import PublishedLessons, ledger
from .scheduling_models import canonical
from .student_schedule import StudentScheduleService
from .workspace import empty_school_state


Role = Literal['student', 'teacher', 'director']
PAGE_SIZE = 25
DATE_PATTERN = r'^\d{4}-\d{2}-\d{2}$'
ATTENDANCE_FIELDS = {'present': 'present', 'absent': 'absent', 'excused': 'excused', None: 'attendanceUnknown'}


def _period(start, end, domain_state, school_id):
    if start is None and end is None:
        config = school_calendar._calendar(domain_state, school_id)['config']
        school_zone = ZoneInfo(config['timezone']) if config else timezone.utc
        last = datetime.now(school_zone).date()
        return (last - timedelta(days=30)).isoformat(), last.isoformat()
    if start is None or end is None:
        raise HTTPException(422, 'Укажите начало и конец периода')
    try:
        days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    except ValueError:
        raise HTTPException(422, 'Проверьте даты периода') from None
    if not 0 <= days < 366:
        raise HTTPException(422, 'Период должен включать от 1 до 366 дней')
    return start, end


def _summary(rows):
    grades = []
    totals = {'recordCount': len(rows), 'present': 0, 'absent': 0, 'excused': 0, 'attendanceUnknown': 0}
    for row in rows:
        grade = row['grade']
        if grade is not None:
            grades.append(grade)
        totals[ATTENDANCE_FIELDS[row['attendance']]] += 1
    return {**totals, 'gradeCount': len(grades),
            'averageGrade': round(fsum(grades) / len(grades), 2) if grades else None}


def _visible_row(record):
    try:
        attendance, grade = record['attendance'], record['grade']
        if ((attendance is None and grade is None) or attendance not in ATTENDANCE_FIELDS or (grade is not None and
                (type(grade) not in {float, int} or not 0 <= grade <= 5 or not isfinite(grade)))):
            raise ValueError()
        return {key: record[key] for key in ('lessonDate', 'className', 'subject', 'attendance', 'grade')}
    except (TypeError, KeyError, ValueError):
        raise HTTPException(503, 'Строка школьного журнала требует проверки') from None


def _teacher_rows(learning, actor, connection, domain_state, personal, candidates):
    from .dated_student_groups import _validated_snapshot
    students = learning._students(connection=connection)
    teachers = set(actor['teacherRefs'])
    schedule = StudentScheduleService(learning)
    dated_groups = _validated_snapshot(schedule, personal)
    published = PublishedLessons(connection, domain_state, learning.school)
    active = school_publications._active_row(learning.school, connection)
    prepared = []
    for record in candidates:
        student = students.get(record.get('studentId'))
        if not student or canonical(student['className']) != canonical(record['className']):
            continue
        try:
            version = record['publicationVersion']
            start, lessons, snapshot = published.publication(version)
            lesson = lessons[record['lessonId']]
            if (lesson['subject'] != record['subject'] or lesson['className'] != record['className']
                    or (start + timedelta(days=lesson['day'])).isoformat() != record['lessonDate']):
                raise ValueError()
            if teachers.isdisjoint(lesson['teachers']):
                continue
            model, membership = published.lesson_membership(schedule, version, snapshot, lesson)
        except (ValueError, KeyError, TypeError):
            raise HTTPException(503, 'Опубликованная привязка журнала требует проверки') from None
        if membership is not None and not any(group['kind'] == 'whole' for group in membership):
            dated = dated_groups.resolve(student, version, record['lessonDate'], snapshot, model)
            if dated is not None:
                if dated['status'] == 'confirmed' and schedule._attends(membership, dated):
                    prepared.append((record, student, model, membership, True))
                continue
            if not active or active['version'] != version:
                continue
        prepared.append((record, student, model, membership, False))
    roster = {}
    if any(not dated for *_, dated in prepared):
        try:
            roster = learning._teacher_students(actor, state=personal, domain=connection)
        except HTTPException as error:
            if error.status_code != 403 or not any(dated for *_, dated in prepared):
                raise
            # An exact dated teaching assignment remains usable after the current
            # teaching roster changes. It does not grant unrelated legacy rows.
            learning._actor(actor, {'teacher'})
    for record, student, model, membership, dated in prepared:
        if not dated:
            current = roster.get(student['id'])
            if not current or record['subject'] not in current['subjects']:
                continue
            if membership is not None and not any(group['kind'] == 'whole' for group in membership):
                groups, signature = schedule._membership(model, student['className'])
                binding = schedule._binding(personal, student, groups, signature)
                if not schedule._attends(membership, binding):
                    continue
        yield record


def _selected(records, start, end, class_name, subject):
    class_key, subject_key = canonical(class_name or ''), canonical(subject or '')
    return [row for row in records if isinstance(row, dict)
            and isinstance(row.get('lessonDate'), str) and start <= row['lessonDate'] <= end
            and isinstance(row.get('className'), str) and isinstance(row.get('subject'), str)
            and (not class_key or canonical(row['className']) == class_key)
            and (not subject_key or canonical(row['subject']) == subject_key)]


def read_view(learning, actor, role, start, end, class_name, subject, page, *, connection=None):
    learning._actor(actor, {role})
    with ExitStack() as resources:
        connection = resources.enter_context(learning.domain_scope(connection))
        if connection is None:
            connection = school_publications._read_connection()
            if connection is not None:
                resources.enter_context(closing(connection))
                connection.execute('BEGIN')
        state = empty_school_state() if connection is None else school_ops._read_state(
            connection, required=learning.domain_storage is not None)
        start, end = _period(start, end, state, learning.school)
        records = _selected(ledger(state, learning.school)['records'], start, end, class_name, subject)
        if role == 'student' or (role == 'teacher' and records):
            personal_connection = learning._connect(readonly=True)
            if personal_connection is None:
                personal = learning._empty()
            else:
                resources.enter_context(closing(personal_connection))
                personal_connection.execute('BEGIN')
                personal = learning._load(personal_connection)
            if role == 'student':
                student = learning._student(actor, personal, connection=connection)
                records = [row for row in records if row.get('studentId') == student['id']]
            else:
                records = list(_teacher_rows(learning, actor, connection, state, personal, records))
        rows = [_visible_row(row) for row in records]
        grouped = defaultdict(list)
        for row in rows:
            grouped[row['className'], row['subject']].append(row)
        subjects = [{'className': name, 'subject': title, **_summary(items)}
                    for (name, title), items in sorted(grouped.items())]
        history = None
        if role == 'student':
            rows.sort(key=lambda row: row['lessonDate'], reverse=True)
            offset = (page - 1) * PAGE_SIZE
            history = {'total': len(rows), 'page': page, 'pageSize': PAGE_SIZE, 'rows': rows[offset:offset + PAGE_SIZE]}
        result = {'source': 'school_lesson_journal', 'period': {'start': start, 'end': end},
                  'summary': _summary(rows), 'subjects': subjects, 'history': history}
        learning._actor(actor, {role})
        return result


def router_for(learning, actor, require_session):
    router = APIRouter()

    @router.get('/api/{role}/lesson-progress')
    def get_progress(request: Request, role: Role, start: str | None = Query(default=None, pattern=DATE_PATTERN),
                     end: str | None = Query(default=None, pattern=DATE_PATTERN),
                     className: str | None = Query(default=None, max_length=80),
                     subject: str | None = Query(default=None, max_length=160),
                     page: int = Query(default=1, ge=1, le=100_000), profile=Depends(actor)):
        result = read_view(learning, profile, role, start, end, className, subject, page)
        require_session(request, profile)
        return result

    return router
