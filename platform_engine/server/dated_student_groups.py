"""Explicit director-confirmed subgroup composition for a published lesson date."""
from copy import deepcopy
from datetime import date, timedelta
import re

from fastapi import HTTPException
from pydantic import Field, field_validator, model_validator

from . import school_ops
from .scheduling_models import Identifier, StrictModel, canonical


KEY = 'datedScheduleBindings'
OPERATION = 'dated-schedule-binding'
MAX_REVISION = 2**31 - 1
MAX_ENTRIES = 5000


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('Invalid explicit lesson date')
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError('Invalid explicit lesson date')
    return value


class DatedGroupInput(StrictModel):
    requestId: str = Field(pattern=r'^[A-Za-z0-9_.:-]{16,80}$')
    studentId: str = Field(min_length=1, max_length=80)
    expectedRevision: int = Field(ge=0, le=MAX_REVISION)
    publicationVersion: int = Field(ge=1, le=MAX_REVISION)
    lessonDate: str
    groupIds: list[Identifier] = Field(max_length=200)

    @field_validator('lessonDate')
    @classmethod
    def explicit_date(cls, value):
        return _date(value)

    @field_validator('studentId')
    @classmethod
    def single_line(cls, value):
        if any(ord(char) < 32 for char in value):
            raise ValueError('Invalid student identifier')
        return value

    @model_validator(mode='after')
    def unique_groups(self):
        if len(self.groupIds) != len(set(self.groupIds)):
            raise ValueError('Повторная подгруппа')
        return self


class _Proof(StrictModel):
    schoolId: str = Field(pattern=r'^[A-Za-z0-9_-]{1,80}$')
    studentId: str = Field(min_length=1, max_length=80)
    memberId: str = Field(min_length=1, max_length=80)
    className: str = Field(min_length=1, max_length=160)
    publicationVersion: int = Field(ge=1, le=MAX_REVISION)
    publicationId: str = Field(min_length=1, max_length=160)
    lessonDate: str
    membershipHash: str = Field(pattern=r'^[a-f0-9]{64}$')
    groupIds: list[Identifier] = Field(max_length=200)
    approvedBy: str = Field(min_length=1, max_length=80)
    approvedRevision: int = Field(ge=1, le=MAX_REVISION)

    @field_validator('lessonDate')
    @classmethod
    def explicit_date(cls, value):
        return _date(value)

    @model_validator(mode='after')
    def canonical_proof(self):
        if (self.className != canonical(self.className)
                or self.groupIds != sorted(set(self.groupIds))
                or any(ord(char) < 32 for field in
                       (self.studentId, self.memberId, self.publicationId, self.approvedBy) for char in field)):
            raise ValueError('Invalid dated proof')
        return self


def _key(version, lesson_date):
    return str(version) + ':' + lesson_date


def validate_state(state, school):
    """Pure structural validation. Legacy states without the new branch are valid."""
    for student_id, account in state['bindings'].items():
        if not isinstance(account, dict) or KEY not in account:
            continue
        branch = account[KEY]
        try:
            if (account.get('studentId') != student_id
                    or not isinstance(branch, dict) or set(branch) != {'formatVersion', 'revision', 'entries'}
                    or type(branch['formatVersion']) is not int or branch['formatVersion'] != 1
                    or type(branch['revision']) is not int or not 0 <= branch['revision'] <= MAX_REVISION
                    or not isinstance(branch['entries'], dict) or len(branch['entries']) > MAX_ENTRIES
                    or branch['entries'] and branch['revision'] == 0):
                raise ValueError()
            for key, value in branch['entries'].items():
                proof = _Proof.model_validate(value)
                if (proof.model_dump() != value or proof.schoolId != school or proof.studentId != student_id
                        or key != _key(proof.publicationVersion, proof.lessonDate)):
                    raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise ValueError('Invalid dated subgroup state') from None


def _checked(state, school):
    try:
        validate_state(state, school)
    except (ValueError, TypeError, KeyError):
        raise HTTPException(503, 'Датированный состав подгрупп требует проверки') from None


def _student_id(student):
    ident = student.get('id', student.get('studentId'))
    if (not isinstance(ident, str) or not ident
            or 'id' in student and 'studentId' in student and student['id'] != student['studentId']):
        raise HTTPException(503, 'Личная привязка ученика требует проверки')
    return ident


def _selection(groups, selected_ids):
    options = {group['id']: group for group in groups if group['kind'] == 'subgroup'}
    if not set(selected_ids) <= options.keys():
        return False
    selected = [options[ident] for ident in selected_ids]
    partitions = [group['partition'] for group in selected if group['partition']]
    units = [set(group.get('attendanceUnitIds', [])) for group in selected if group.get('attendanceUnitIds')]
    return len(partitions) == len(set(partitions)) and (not units or bool(set.intersection(*units)))


def resolve(schedule, personal, student, publication_version, lesson_date, snapshot, model):
    """None means no exact proof; an explicit stale proof never permits fallback."""
    _checked(personal, schedule.learning.school)
    return _resolve_validated(schedule, personal, student, publication_version, lesson_date, snapshot, model)


def _resolve_validated(schedule, personal, student, publication_version, lesson_date, snapshot, model):
    ident = _student_id(student)
    account = personal['bindings'].get(ident)
    branch = account.get(KEY) if isinstance(account, dict) else None
    proof = branch['entries'].get(_key(publication_version, lesson_date)) if branch is not None else None
    if proof is None:
        return None
    member = schedule.learning._bound_account(personal, ident)
    groups, signature = schedule._membership(model, student['className']) if model is not None else ([], '')
    allowed = {group['id'] for group in groups if group['kind'] == 'subgroup'}
    current = bool(member and proof['schoolId'] == schedule.learning.school
        and proof['studentId'] == ident and account['studentId'] == ident
        and proof['memberId'] == member['membershipId']
        and proof['className'] == canonical(student['className'])
        and proof['membershipHash'] == signature
        and snapshot.get('schoolId') == schedule.learning.school
        and proof['publicationVersion'] == publication_version == snapshot.get('version')
        and proof['publicationId'] == snapshot.get('publicationId')
        and proof['lessonDate'] == lesson_date and set(proof['groupIds']) <= allowed)
    if current and not _selection(groups, proof['groupIds']):
        raise HTTPException(503, 'Подтверждённый состав подгрупп несовместим')
    return {'revision': branch['revision'], 'status': 'confirmed' if current else 'stale',
            'groupIds': list(proof['groupIds']) if current else []}


class _ValidatedSnapshot:
    def __init__(self, schedule, personal):
        self._schedule = schedule
        self._school = schedule.learning.school
        # Request-local evidence only: caller mutation cannot replace this proof.
        self._personal = deepcopy(personal)
        _checked(self._personal, self._school)

    def resolve(self, student, publication_version, lesson_date, snapshot, model):
        if self._schedule.learning.school != self._school:
            raise HTTPException(503, 'Школьный контекст датированного состава изменился')
        # The evidence is frozen, while _bound_account still checks live staff
        # membership on every call. This is not a cross-request authority cache.
        return _resolve_validated(self._schedule, self._personal, student, publication_version,
                                  lesson_date, snapshot, model)


def _validated_snapshot(schedule, personal):
    """Private per-read snapshot; whole-state validation runs exactly once."""
    return _ValidatedSnapshot(schedule, personal)


class DatedGroupService:
    def __init__(self, learning):
        from .student_schedule import StudentScheduleService
        self.learning = learning
        self.schedule = StudentScheduleService(learning)

    def _context(self, connection, student_id, version, lesson_date):
        from .school_lesson_progress import PublishedLessons
        if connection is None:
            raise HTTPException(409, 'Нужна подтверждённая публикация')
        student = self.learning._students(connection=connection).get(student_id)
        if student is None:
            raise HTTPException(404, 'Запись ученика недоступна')
        source = PublishedLessons(connection, school_ops._read_state(connection, required=True), self.learning.school)
        try:
            start, lessons, snapshot = source.publication(version)
            model = self.schedule._model(connection, snapshot)
            if model is None or not any(canonical(row['className']) == canonical(student['className'])
                    and (start + timedelta(days=row['day'])).isoformat() == lesson_date for row in lessons.values()):
                raise ValueError()
            groups, signature = self.schedule._membership(model, student['className'])
            if not groups:
                raise ValueError()
        except (ValueError, TypeError, KeyError, OverflowError):
            raise HTTPException(409, 'Нужны подтверждённая публикация и дата урока этого класса') from None
        return student, snapshot, model, groups, signature

    def _response(self, state, student, snapshot, model, groups, lesson_date):
        member = self.learning._bound_account(state, student['id'])
        branch = state['bindings'].get(student['id'], {}).get(KEY)
        binding = resolve(self.schedule, state, student, snapshot['version'], lesson_date, snapshot, model)
        if binding is None:
            binding = {'revision': branch['revision'] if branch is not None else 0,
                       'status': 'missing', 'groupIds': []}
        return {'student': student, 'publicationVersion': snapshot['version'], 'lessonDate': lesson_date,
                'binding': binding,
                'groups': [{key: group[key] for key in ('id', 'name', 'className', 'kind', 'partition', 'size')}
                           for group in groups if group['kind'] == 'subgroup'],
                'ready': bool(member), 'blockers': [] if member else [
                    {'code': 'student_binding_required', 'message': 'Нужен действующий личный доступ ученика.'}]}

    def details(self, actor, student_id, publication_version, lesson_date, *, connection=None):
        self.learning._actor(actor, {'director'})
        try:
            selector = DatedGroupInput.model_validate({'requestId': 'dated-read-selector', 'studentId': student_id,
                'expectedRevision': 0, 'publicationVersion': publication_version, 'lessonDate': lesson_date, 'groupIds': []})
        except (ValueError, TypeError):
            raise HTTPException(422, 'Укажите ученика, публикацию и явную дату урока') from None
        with self.schedule._published(connection) as (connection, _active):
            student, snapshot, model, groups, _signature = self._context(
                connection, selector.studentId, selector.publicationVersion, selector.lessonDate)
            state = self.learning._read()
            _checked(state, self.learning.school)
            result = self._response(state, student, snapshot, model, groups, selector.lessonDate)
            self.learning._actor(actor, {'director'})
            return result

    def save(self, actor, body, *, connection=None, before_return=None):
        from .student_learning import parse
        self.learning._actor(actor, {'director'})
        data = parse(DatedGroupInput, body)
        with self.schedule._published(connection) as (connection, _active):
            student, snapshot, model, groups, signature = self._context(
                connection, data['studentId'], data['publicationVersion'], data['lessonDate'])
            with self.learning._transaction() as (_personal, state):
                _checked(state, self.learning.school)
                self.learning._actor(actor, {'director'})
                member = self.learning._bound_account(state, student['id'])
                if not member:
                    raise HTTPException(409, 'Нужен действующий личный доступ ученика')
                # A same-model proof with impossible parallel groups is corrupt,
                # rather than an implicit opportunity to repair stored evidence.
                resolve(self.schedule, state, student, data['publicationVersion'], data['lessonDate'], snapshot, model)
                previous = self.learning._receipt(state, actor, data, OPERATION)
                if previous is not None:
                    saved = state['commands'][actor['membershipId'] + ':' + data['requestId']].get('datedGroupProof')
                    try:
                        audit = _Proof.model_validate(saved)
                        if audit.model_dump() != saved:
                            raise ValueError()
                    except (ValueError, TypeError):
                        raise HTTPException(503, 'Квитанция датированного состава требует проверки') from None
                    if (audit.schoolId != self.learning.school or audit.studentId != student['id']
                            or audit.memberId != member['membershipId']
                            or audit.className != canonical(student['className'])
                            or audit.publicationVersion != data['publicationVersion']
                            or audit.lessonDate != data['lessonDate']
                            or audit.publicationId != snapshot['publicationId']
                            or audit.membershipHash != signature or audit.groupIds != sorted(data['groupIds'])
                            or audit.approvedBy != actor['membershipId']
                            or audit.approvedRevision != actor['membershipRevision']):
                        raise HTTPException(409, 'Личная привязка или класс ученика изменились')
                    self.learning._actor(actor, {'director'})
                    latest = self.learning._bound_account(state, student['id'])
                    if not latest or latest['membershipId'] != member['membershipId']:
                        raise HTTPException(409, 'Личная привязка ученика изменилась')
                    if before_return is not None:
                        before_return()
                    return previous
                account = state['bindings'][student['id']]
                branch = account.get(KEY, {'formatVersion': 1, 'revision': 0, 'entries': {}})
                if branch['revision'] != data['expectedRevision'] or branch['revision'] == MAX_REVISION:
                    raise HTTPException(409, 'Датированный состав изменился; обновите данные')
                key = _key(data['publicationVersion'], data['lessonDate'])
                if key not in branch['entries'] and len(branch['entries']) >= MAX_ENTRIES:
                    raise HTTPException(429, 'Достигнут лимит датированных подтверждений')
                options = {group['id']: group for group in groups if group['kind'] == 'subgroup'}
                if not set(data['groupIds']) <= options.keys():
                    raise HTTPException(422, 'Выберите подгруппы из подтверждённой модели этого класса')
                if not _selection(groups, data['groupIds']):
                    raise HTTPException(422, 'Выбранные подгруппы несовместимы')
                proof = {'schoolId': self.learning.school, 'studentId': student['id'],
                    'memberId': member['membershipId'], 'className': canonical(student['className']),
                    'publicationVersion': data['publicationVersion'], 'publicationId': snapshot['publicationId'],
                    'lessonDate': data['lessonDate'], 'membershipHash': signature, 'groupIds': sorted(data['groupIds']),
                    'approvedBy': actor['membershipId'], 'approvedRevision': actor['membershipRevision']}
                self.learning._actor(actor, {'director'})
                latest = self.learning._bound_account(state, student['id'])
                if not latest or latest['membershipId'] != member['membershipId']:
                    raise HTTPException(409, 'Личная привязка ученика изменилась')
                branch = deepcopy(branch)
                branch['revision'] += 1
                branch['entries'][key] = proof
                account[KEY] = branch
                _checked(state, self.learning.school)
                result = self._response(state, student, snapshot, model, groups, data['lessonDate'])
                self.learning._actor(actor, {'director'})
                latest = self.learning._bound_account(state, student['id'])
                if not latest or latest['membershipId'] != member['membershipId']:
                    raise HTTPException(409, 'Личная привязка ученика изменилась')
                self.learning._command(state, actor, data, OPERATION, 'completed', result)
                state['commands'][actor['membershipId'] + ':' + data['requestId']]['datedGroupProof'] = deepcopy(proof)
                if before_return is not None:
                    before_return()
                return result
