"""Addressed consultation invitations using current learning identities."""
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, field_validator

from . import db, school_calendar, school_ops, staff_access
from .auth import session_profile
from .student_learning import AssignmentInput
from .teacher_preparation import Strict


Role = Literal['teacher', 'student']
LIMIT = 1000


class ConsultationCommand(Strict):
    requestId: str = Field(pattern=r'^[A-Za-z0-9_.:-]{16,80}$')


class ConsultationInput(ConsultationCommand):
    studentId: str = Field(min_length=1, max_length=80)
    topic: str = Field(min_length=1, max_length=240)
    place: str = Field(min_length=1, max_length=240)
    startAt: str = Field(max_length=64)
    duration: Literal[15, 20, 30, 45, 60]

    @field_validator('startAt')
    @classmethod
    def aware_time(cls, value):
        normalized = AssignmentInput.due_date(value)
        if not 2000 <= datetime.fromisoformat(normalized).year <= 2100:
            raise ValueError('Дата консультации вне допустимого периода')
        return normalized

    @field_validator('topic', 'place')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('Заполните тему и место консультации')
        return value.strip()


class ConsultationDecision(ConsultationCommand):
    expectedRevision: int = Field(ge=1, le=3)
    decision: Literal['book', 'cancel']


def utc_now():
    return datetime.now(timezone.utc)


def _rows(state):
    rows = state.get('consultations', {})
    if not isinstance(rows, dict) or len(rows) > LIMIT:
        raise HTTPException(503, 'Не удалось проверить реестр консультаций')
    return rows


def _overlap(start, end, other_start, other_end):
    return start < other_end and other_start < end


def _check_schedule(schedule, start, end, zone):
    first = date.fromisoformat(schedule['periodStart']) if schedule['periodStart'] else None
    for lesson in schedule['lessons']:
        day = lesson.get('date')
        if day is None and first is not None:
            day = (first + timedelta(days=lesson['day'])).isoformat()
        if day != start.date().isoformat():
            continue
        lesson_start = datetime.fromisoformat(f"{day}T{lesson['start']}").replace(tzinfo=zone)
        lesson_end = datetime.fromisoformat(f"{day}T{lesson['end']}").replace(tzinfo=zone)
        if _overlap(start, end, lesson_start, lesson_end):
            raise HTTPException(409, 'В это время есть урок в опубликованном расписании')


class Consultations:
    def __init__(self, learning, student_schedule):
        self.learning = learning
        self.student_schedule = student_schedule

    def _pair(self, state, teacher, student_id, *, connection=None):
        student = self.learning._teacher_students(teacher, state=state, domain=connection).get(student_id)
        member = self.learning._bound_account(state, student_id) if student else None
        return (student, member) if member else None

    def _visible(self, state, actor, row, student=None, teacher_pairs=None, *, connection=None):
        if actor['staffRole'] == 'teacher':
            if row['teacherMemberId'] != actor['membershipId']:
                return False
            teacher = actor
        else:
            if row['studentId'] != student['id'] or row['studentMemberId'] != actor['membershipId']:
                return False
            teacher = None
        return self._current_pair(state, row, teacher, teacher_pairs, connection=connection)

    def _current_pair(self, state, row, teacher=None, teacher_pairs=None, *, connection=None):
        teacher = teacher or staff_access.resolve_membership(self.learning.school, row['teacherMemberId'])
        if not teacher or teacher.get('staffRole') != 'teacher':
            return False
        try:
            pair = (teacher_pairs.get(row['studentId']) if teacher_pairs is not None
                    else self._pair(state, teacher, row['studentId'], connection=connection))
        except HTTPException as error:
            if error.status_code in {401, 403}:
                return False
            raise
        return bool(pair and pair[0]['className'] == row['className'] and
                    pair[1]['membershipId'] == row['studentMemberId'])

    @staticmethod
    def _public(row, role):
        keys = ('id', 'revision', 'status', 'topic', 'place', 'startAt', 'endAt')
        counterpart = row['studentName'] if role == 'teacher' else row['teacherName']
        return {**{key: row[key] for key in keys}, 'counterpart': counterpart,
                'past': datetime.fromisoformat(row['startAt']) <= utc_now()}

    def listing(self, actor, role, *, connection=None, before_return=None):
        self.learning._actor(actor, {role})
        with self.learning.domain_scope(connection) as connection:
            state = self.learning._read()
            student = self.learning._student(actor, state, connection=connection) if role == 'student' else None
            roster, teacher_pairs = [], None
            if role == 'teacher':
                try:
                    candidates = self.learning._teacher_students(actor, state=state, domain=connection)
                except HTTPException as error:
                    if error.status_code != 403:
                        raise
                    candidates = {}
                teacher_pairs = {}
                for ident, item in candidates.items():
                    member = self.learning._bound_account(state, ident)
                    if member:
                        teacher_pairs[ident] = (item, member)
                        roster.append({key: item[key] for key in ('id', 'name', 'className')})
            items = [self._public(row, role) for row in _rows(state).values()
                     if self._visible(state, actor, row, student, teacher_pairs, connection=connection)]
            booking_ready = role == 'student' and any(row['status'] == 'offered' and not row['past'] for row in items)
            if booking_ready:
                booking_ready = self.student_schedule.own(actor, connection=connection)['complete']
            self.learning._actor(actor, {role})
            if before_return is not None:
                before_return()
            return {'students': roster, 'items': sorted(items, key=lambda row: row['startAt']),
                    'bookingReady': booking_ready}

    def _check_slot(self, state, teacher, student_member, start, end, *, exclude=None, booking=False, connection=None):
        schedule = staff_access.own_timetable(teacher, connection=connection)
        domain_state = db.read_state() if connection is None else school_ops._read_state(connection, required=True)
        config = school_calendar._calendar(domain_state, self.learning.school)['config']
        if not config or not config['confirmed']:
            raise HTTPException(409, 'Подтвердите часовой пояс школьного календаря')
        zone = ZoneInfo(config['timezone'])
        local_start, local_end = start.astimezone(zone), end.astimezone(zone)
        day = local_start.date()
        first = date.fromisoformat(schedule['periodStart']) if schedule['periodStart'] else None
        if not first or not first <= day < first + timedelta(days=7) or day != local_end.date():
            raise HTTPException(409, 'Выберите время в пределах опубликованной недели и одного дня')
        if start <= utc_now():
            raise HTTPException(409, 'Время консультации уже наступило')
        _check_schedule(schedule, local_start, local_end, zone)
        if booking:
            student_schedule = self.student_schedule.own(student_member, connection=connection)
            if not student_schedule['complete'] or student_schedule['publicationVersion'] != schedule['publicationVersion']:
                raise HTTPException(409, 'Для записи нужно подтверждённое личное расписание ученика той же версии')
            _check_schedule(student_schedule, local_start, local_end, zone)
        for row in _rows(state).values():
            if row['id'] == exclude or row['status'] == 'cancelled':
                continue
            teacher_busy = row['teacherMemberId'] == teacher['membershipId']
            student_busy = row['status'] == 'booked' and row['studentMemberId'] == student_member['membershipId']
            if (teacher_busy or student_busy) and _overlap(start, end, datetime.fromisoformat(row['startAt']),
                                                          datetime.fromisoformat(row['endAt'])) and self._current_pair(state, row, connection=connection):
                raise HTTPException(409, 'Это время уже занято консультацией')

    def offer(self, actor, body, *, connection=None, before_return=None):
        role = 'teacher'
        self.learning._actor(actor, {role})
        with self.learning.domain_scope(connection) as connection:
            if not self._pair(self.learning._read(), actor, body.studentId, connection=connection):
                raise HTTPException(403, 'Для этого ученика нет подтверждённого доступа')
            data = body.model_dump()
            operation = 'consultation:offer'
            with self.learning._transaction() as (_, state):
                pair = self._pair(state, actor, body.studentId, connection=connection)
                if not pair:
                    raise HTTPException(403, 'Для этого ученика нет подтверждённого доступа')
                student, member = pair
                old = self.learning._receipt(state, actor, data, operation)
                if old:
                    saved = _rows(state).get(old['id'])
                    if saved is None or saved['studentMemberId'] != member['membershipId'] or saved['className'] != student['className']:
                        raise HTTPException(409, 'Привязка ученика изменилась')
                    if before_return is not None:
                        before_return()
                    return old
                rows = _rows(state)
                if len(rows) >= LIMIT:
                    raise HTTPException(429, 'Достигнут предел журнала консультаций')
                start = datetime.fromisoformat(body.startAt)
                end = start + timedelta(minutes=body.duration)
                self._check_slot(state, actor, member, start, end, connection=connection)
                identifier = uuid4().hex
                row = {'id': identifier, 'revision': 1, 'status': 'offered',
                       'studentId': student['id'], 'studentName': student['name'], 'className': student['className'],
                       'studentMemberId': member['membershipId'], 'teacherMemberId': actor['membershipId'],
                       'teacherName': actor['name'], 'topic': body.topic, 'place': body.place,
                       'startAt': start.isoformat(), 'endAt': end.isoformat()}
                if not self._current_pair(state, row, actor, connection=connection):
                    raise HTTPException(409, 'Привязка ученика изменилась')
                rows[identifier] = row
                state['consultations'] = rows
                result = self._public(row, role)
                self.learning._actor(actor, {role})
                self.learning._command(state, actor, data, operation, 'completed', result)
                if before_return is not None:
                    before_return()
                return result

    def decide(self, actor, role, identifier, body, *, connection=None, before_return=None):
        self.learning._actor(actor, {role})
        with self.learning.domain_scope(connection) as connection:
            data = body.model_dump()
            operation = 'consultation:decision:' + identifier
            with self.learning._transaction() as (_, state):
                rows = _rows(state)
                student = self.learning._student(actor, state, connection=connection) if role == 'student' else None
                row = rows.get(identifier)
                if row is None or not self._visible(state, actor, row, student, connection=connection):
                    raise HTTPException(404, 'Консультация недоступна')
                old = self.learning._receipt(state, actor, data, operation)
                if old:
                    if before_return is not None:
                        before_return()
                    return old
                start, end = datetime.fromisoformat(row['startAt']), datetime.fromisoformat(row['endAt'])
                if row['revision'] != body.expectedRevision or row['status'] == 'cancelled' or start <= utc_now():
                    raise HTTPException(409, 'Консультация изменилась или уже началась')
                if body.decision == 'book':
                    if role != 'student' or row['status'] != 'offered':
                        raise HTTPException(403, 'Запись подтверждает ученик, получивший предложение')
                    teacher = staff_access.resolve_membership(self.learning.school, row['teacherMemberId'])
                    if not teacher:
                        raise HTTPException(409, 'Доступ педагога изменился')
                    self._check_slot(state, teacher, actor, start, end, exclude=identifier, booking=True, connection=connection)
                if not self._current_pair(state, row, connection=connection):
                    raise HTTPException(409, 'Привязка педагога или ученика изменилась')
                row['status'] = 'booked' if body.decision == 'book' else 'cancelled'
                row['revision'] += 1
                result = self._public(row, role)
                self.learning._actor(actor, {role})
                self.learning._command(state, actor, data, operation, 'completed', result)
                if before_return is not None:
                    before_return()
                return result


def router_for(learning, student_schedule, require_session=None):
    router = APIRouter()
    service = Consultations(learning, student_schedule)

    @router.get('/api/{role}/consultations')
    def listing(request: Request, role: Role, actor=Depends(session_profile)):
        check = (lambda: require_session(request, actor)) if require_session is not None else None
        return service.listing(actor, role, before_return=check)

    @router.post('/api/teacher/consultations')
    def offer(request: Request, body: ConsultationInput, actor=Depends(session_profile)):
        check = (lambda: require_session(request, actor)) if require_session is not None else None
        return service.offer(actor, body, before_return=check)

    @router.post('/api/{role}/consultations/{identifier}/decision')
    def decide(request: Request, role: Role, identifier: str, body: ConsultationDecision, actor=Depends(session_profile)):
        check = (lambda: require_session(request, actor)) if require_session is not None else None
        return service.decide(actor, role, identifier, body, before_return=check)

    return router
