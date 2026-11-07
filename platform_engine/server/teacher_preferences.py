"""Teacher-owned recurring preferences, explicitly reviewed by a school manager.

This ledger writes only itself and, after approval, a calendar recurrence. It
never calculates, edits or publishes a timetable, and never calls a model.
"""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import json
import re
import sqlite3
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Path as APIPath
from pydantic import Field, ValidationError, model_validator

from . import db, launch_identity, school_calendar as calendar
from .auth import session_profile
from .calendar_models import MAX_REVISION, Recurrence, StrictInput
from .request_planning import canonical_manager, digest
from .school_requests import SafeRoute, _atomic_domain, require_bound_school
from .workspace import school_workspace

KEY = '_teacherPreferences'
MAX_RECORDS = 500
MAX_RECEIPTS = 1000
ID_PATTERN = r'^preference_[a-f0-9]{32}$'
NOTICE = 'Пожелание не меняет опубликованное расписание. Недоступность действует в новых расчётах только после согласования; текстовое пожелание не добавляется в цель оптимизации.'
router = APIRouter(tags=['teacher preferences'], route_class=SafeRoute)


class PreferenceInput(StrictInput):
    kind: Literal['unavailable', 'wish']
    teacherId: str = Field(min_length=1, max_length=160)
    startDate: str
    endDate: str
    weekdays: list[int] = Field(min_length=1, max_length=7)
    startTime: str | None = None
    endTime: str | None = None
    note: str = Field(default='', max_length=2000)

    @model_validator(mode='after')
    def period(self):
        # Reuse the calendar's finite date/time/weekday contract for both kinds.
        value = Recurrence(startDate=self.startDate, endDate=self.endDate,
            weekdays=self.weekdays, startTime=self.startTime, endTime=self.endTime,
            kind='teacher_unavailable', targetId=self.teacherId, source='Teacher preference')
        self.weekdays = value.weekdays
        calendar._events('rules', value.model_dump())
        if self.kind == 'wish' and not self.note:
            raise ValueError('Опишите пожелание для рассмотрения')
        return self


class RevisionInput(StrictInput):
    expectedRevision: int = Field(ge=0, le=MAX_REVISION)


class SubmitInput(RevisionInput):
    requestId: str = Field(pattern=r'^[A-Za-z0-9_-]{16,80}$')
    preference: PreferenceInput
    replacesId: str | None = Field(default=None, pattern=ID_PATTERN)


class DecisionInput(RevisionInput):
    requestId: str = Field(pattern=r'^[A-Za-z0-9_-]{16,80}$')
    previewHash: str = Field(pattern=r'^[a-f0-9]{64}$')
    decision: Literal['approve', 'reject']
    reason: str = Field(min_length=1, max_length=2000)


def _body(model, value):
    try:
        return value if isinstance(value, model) else model.model_validate(value)
    except (ValidationError, ValueError, OverflowError):
        raise HTTPException(422, 'Проверьте даты, дни недели, время и текст пожелания') from None


def _actor(profile, manager=False):
    if not school_workspace():
        raise HTTPException(404, 'Раздел недоступен')
    value = canonical_manager(profile)  # Shared authoritative membership check.
    role = value.get('staffRole', value.get('role'))
    if role not in ({'director', 'planner'} if manager else {'teacher'}):
        raise HTTPException(403, 'Решение принимает директор или завуч' if manager else 'Пожелание подаёт сам педагог')
    if not manager and value.get('staffRole') != 'teacher':
        raise HTTPException(403, 'Нужна подтверждённая учётная запись педагога')
    require_bound_school(value['schoolId'])
    return value


def _binding(profile):
    return {key: deepcopy(profile.get(key)) for key in ('schoolId', 'userId',
        'membershipId', 'membershipRevision', 'staffRole', 'role', 'permissions',
        'teacherRefs', 'calendarTeacherIds')}


def _teacher_ids(profile):
    refs, ids = profile.get('teacherRefs'), profile.get('calendarTeacherIds')
    if (not isinstance(refs, list) or not refs or any(not isinstance(v, str) or not v.strip() for v in refs)
            or not isinstance(ids, list) or not ids or len(ids) > 200
            or len(set(ids)) != len(ids) or any(not isinstance(v, str) or not v.strip() for v in ids)):
        raise HTTPException(409, 'Подтвердите связь учётной записи с ID педагога календаря')
    from .staff_access import validate_calendar_bindings
    try:
        validate_calendar_bindings(ids)
    except HTTPException:
        raise HTTPException(409, 'Привязка педагога больше не подтверждена текущей нормативной моделью') from None
    return ids


def _empty(school):
    return {'version': 1, 'schoolId': school, 'revision': 0, 'records': {}, 'receipts': {}}


def _ledger(state, school):
    value = state.get(KEY)
    if value is None:
        return _empty(school)
    if not isinstance(value, dict) or value.get('schoolId') != school:
        raise HTTPException(403, 'Пожелания относятся к другой школе')
    try:
        if (value.get('version') != 1 or type(value['revision']) is not int
                or not 0 <= value['revision'] <= MAX_REVISION
                or not isinstance(value['records'], dict) or len(value['records']) > MAX_RECORDS
                or not isinstance(value['receipts'], dict) or len(value['receipts']) > MAX_RECEIPTS):
            raise ValueError()
        for key, row in value['records'].items():
            required = {'id', 'schoolId', 'revision', 'owner', 'ownerName', 'preference',
                'status', 'replacesId', 'calendarRecordId', 'calendarRecordHash',
                'createdAt', 'updatedAt', 'history', 'objectiveApplied'}
            if (not isinstance(row, dict) or set(row) != required
                    or not re.fullmatch(ID_PATTERN, key) or row['id'] != key or row['schoolId'] != school
                    or row['owner']['schoolId'] != school or not row['owner']['membershipId']
                    or type(row['revision']) is not int or row['revision'] < 1
                    or row['status'] not in {'pending', 'approved', 'rejected', 'superseded'}
                    or not isinstance(row['history'], list) or len(row['history']) > 200
                    or type(row['objectiveApplied']) is not bool or row['objectiveApplied']
                    or (row['calendarRecordId'] is None) != (row['calendarRecordHash'] is None)
                    or (row['calendarRecordId'] is not None and
                        (row['calendarRecordId'] != key or not re.fullmatch(r'[a-f0-9]{64}', row['calendarRecordHash'])))):
                raise ValueError()
            PreferenceInput.model_validate(row['preference'])
        for key, receipt in value['receipts'].items():
            if (not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', key)
                    or not re.fullmatch(r'[a-f0-9]{64}', receipt['fingerprint'])
                    or receipt['actor']['schoolId'] != school
                    or not isinstance(receipt['response'], dict)):
                raise ValueError()
    except (KeyError, TypeError, AttributeError, ValueError, ValidationError):
        raise HTTPException(409, 'История пожеланий требует проверки; записи не заменены') from None
    return deepcopy(value)


def _get(data, identifier):
    row = data['records'].get(identifier)
    if row is None:
        raise HTTPException(404, 'Пожелание не найдено')
    return row


def _public(row, cal):
    value = {key: deepcopy(row[key]) for key in ('id', 'revision', 'ownerName',
        'preference', 'status', 'replacesId', 'calendarRecordId', 'createdAt', 'updatedAt',
        'history', 'objectiveApplied')}
    rule = next((r for r in cal['records']['rules'] if r['id'] == row['calendarRecordId']), None)
    value['calendarState'] = ('not_applied' if not row['calendarRecordId'] else
        'missing' if rule is None else 'archived' if rule['archived'] else
        'active' if digest(rule) == row['calendarRecordHash'] else 'changed')
    return value


def _cas(data, revision):
    if data['revision'] != revision or revision >= MAX_REVISION:
        raise HTTPException(409, 'Пожелания изменились; обновите список, сохранив свой ввод')


def _replay(data, body, fp, profile):
    previous = data['receipts'].get(body.requestId)
    if previous:
        if previous['fingerprint'] != fp or previous['actor'] != _binding(profile):
            raise HTTPException(409, 'Идентификатор запроса уже использован другим действием')
        return deepcopy(previous['response'])
    if len(data['receipts']) >= MAX_RECEIPTS:
        raise HTTPException(429, 'Достигнут предел квитанций; прежние данные сохранены')
    return None


def _save(state, data, body, fp, profile, row, cal):
    data['revision'] += 1
    response = {'revision': data['revision'], 'record': _public(row, cal), 'notice': NOTICE}
    data['receipts'][body.requestId] = {'fingerprint': fp, 'actor': _binding(profile), 'response': deepcopy(response)}
    if len(json.dumps(data, ensure_ascii=False).encode()) > 8 * 1024 * 1024:
        raise HTTPException(429, 'Достигнут предел истории пожеланий; прежние данные сохранены')
    state[KEY] = data
    return response


def list_preferences(profile, *, manager=False):
    profile = _actor(profile, manager)
    state = calendar._read_state()  # Read-only, including an absent domain DB.
    data, cal = _ledger(state, profile['schoolId']), calendar._calendar(state, profile['schoolId'])
    rows = [row for row in data['records'].values() if manager or row['owner']['membershipId'] == profile['membershipId']]
    ids, blockers = [], []
    if not manager:
        try:
            ids = _teacher_ids(profile)
        except HTTPException as error:
            blockers.append({'code': 'teacher_binding_required', 'message': error.detail})
    options = []
    if ids:
        with closing(sqlite3.connect(db.database_path().as_uri() + '?mode=ro', uri=True)) as connection:
            raw = connection.execute('SELECT input_json FROM planning_config WHERE id=1').fetchone()
            roster = json.loads(raw[0])['teachers'] if raw else []
            options = [{'id': t['id'], 'name': t['name']} for t in roster if t['id'] in ids]
    return {'revision': data['revision'], 'records': [_public(r, cal) for r in rows],
        'teacherIds': ids, 'canSubmit': bool(ids) and not manager, 'manager': manager,
        'teacherOptions': options, 'blockers': blockers, 'calendarRevision': cal['revision'], 'notice': NOTICE}


def submit(body, profile, identifier=None):
    profile = _actor(profile)
    body = _body(SubmitInput, body)
    if body.preference.teacherId not in _teacher_ids(profile):
        raise HTTPException(403, 'Можно указывать только свой подтверждённый ID педагога')
    fp = digest({'operation': 'submit', 'id': identifier, 'body': body.model_dump()})
    with _atomic_domain() as (_, state):
        _actor(profile)
        data, cal = _ledger(state, profile['schoolId']), calendar._calendar(state, profile['schoolId'])
        previous = _replay(data, body, fp, profile)
        if previous is not None:
            return previous
        _cas(data, body.expectedRevision)
        old = _get(data, identifier) if identifier else None
        if old and (old['owner']['membershipId'] != profile['membershipId'] or old['status'] != 'pending'):
            raise HTTPException(409, 'Изменять можно только своё пожелание на рассмотрении')
        if body.replacesId:
            replaced = _get(data, body.replacesId)
            if replaced['owner']['membershipId'] != profile['membershipId'] or replaced['status'] != 'approved':
                raise HTTPException(409, 'Заменить можно только своё согласованное пожелание')
        if old is None and len(data['records']) >= MAX_RECORDS:
            raise HTTPException(429, 'Достигнут предел пожеланий; прежние записи сохранены')
        stamp = datetime.now(timezone.utc).isoformat()
        row = old or {'id': 'preference_' + uuid4().hex, 'schoolId': profile['schoolId'],
            'revision': 0, 'createdAt': stamp, 'history': [], 'calendarRecordId': None,
            'calendarRecordHash': None, 'objectiveApplied': False}
        if len(row['history']) >= 200:
            raise HTTPException(429, 'Достигнут предел изменений пожелания')
        row.update(owner=_binding(profile), ownerName=profile.get('name', 'Педагог'),
            preference=body.preference.model_dump(), replacesId=body.replacesId,
            status='pending', revision=row['revision'] + 1, updatedAt=stamp)
        row['history'].append({'action': 'updated' if old else 'submitted', 'at': stamp, 'reason': ''})
        data['records'][row['id']] = row
        return _save(state, data, body, fp, profile, row, cal)


def _preview(data, cal, identifier, profile):
    row = _get(data, identifier)
    if row['status'] != 'pending':
        raise HTTPException(409, 'По этому пожеланию уже принято решение')
    blockers, candidate = [], deepcopy(cal)
    def block(code, message):
        blockers.append({'code': code, 'message': message})
    owner = launch_identity.resolve_membership(profile['schoolId'], row['owner']['membershipId'])
    if (not owner or not launch_identity.membership_current(owner) or _binding(owner) != row['owner']):
        block('teacher_binding_changed', 'Доступ или привязка автора изменились; педагог должен обновить пожелание')
    else:
        try:
            if row['preference']['teacherId'] not in _teacher_ids(owner):
                block('teacher_binding_changed', 'Педагог больше не связан с указанным ID')
        except HTTPException as error:
            block('teacher_binding_changed', str(error.detail))
    prior = _get(data, row['replacesId']) if row['replacesId'] else None
    if prior and (prior['status'] != 'approved' or prior['owner']['membershipId'] != row['owner']['membershipId']):
        block('replaced_preference_changed', 'Прежнее пожелание уже изменилось')
    if prior and owner and prior['preference']['teacherId'] not in owner.get('calendarTeacherIds', []):
        block('replaced_teacher_binding_changed', 'Прежнее условие относится к прежней привязке; его должен отдельно проверить составитель')
    changes_calendar = row['preference']['kind'] == 'unavailable' or bool(prior and prior['calendarRecordId'])
    if changes_calendar:
        config = candidate['config']
        pref = row['preference']
        if not config or not config['confirmed']:
            block('calendar_confirmation_required', 'Сначала подтвердите период и часовой пояс школьного календаря')
        elif not config['periodStart'] <= pref['startDate'] <= pref['endDate'] <= config['periodEnd']:
            block('period_outside_calendar', 'Период пожелания выходит за подтверждённый календарь')
        if prior and prior['calendarRecordId']:
            rule = next((r for r in candidate['records']['rules'] if r['id'] == prior['calendarRecordId']), None)
            if rule is None or rule['archived'] or digest(rule) != prior['calendarRecordHash']:
                block('previous_calendar_rule_changed', 'Прежнее календарное правило изменено; требуется отдельная проверка')
            else:
                rule['archived'] = True
    occurrences, new_rule = [], None
    if row['preference']['kind'] == 'unavailable':
        pref = row['preference']
        new_rule = Recurrence(**{k: pref[k] for k in ('startDate', 'endDate', 'weekdays', 'startTime', 'endTime')},
            kind='teacher_unavailable', targetId=pref['teacherId'], confirmed=True,
            source='Согласованное пожелание ' + row['id'], note=pref['note']).model_dump()
        new_rule.update(id=row['id'], archived=False, createdAt=row['createdAt'], updatedAt=row['updatedAt'])
        candidate['records']['rules'].append(new_rule)
        occurrences = calendar._events('rules', new_rule)
    if changes_calendar and not blockers:
        try:
            calendar._validate_calendar(candidate)
        except HTTPException as error:
            block('calendar_conflict', str(error.detail))
    binding = {'schoolId': profile['schoolId'], 'revision': data['revision'],
        'recordHash': digest(row), 'calendarRevision': cal['revision'], 'calendarHash': digest(cal),
        'ownerBinding': _binding(owner) if owner else None, 'managerBinding': _binding(profile)}
    public = {'id': identifier, 'revision': data['revision'], 'previewHash': digest(binding),
        'ready': not blockers, 'blockers': blockers, 'record': _public(row, cal),
        'calendarRevision': cal['revision'], 'changesCalendar': changes_calendar,
        'occurrences': [{k: event[k] for k in ('date', 'startTime', 'endTime')} for event in occurrences],
        'objectiveApplied': False, 'notice': NOTICE}
    return public, candidate, new_rule, prior


def preview(identifier, body, profile):
    profile = _actor(profile, True)
    body = _body(RevisionInput, body)
    path = db.database_path()
    if not path.exists():
        raise HTTPException(404, 'Пожелание не найдено')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
        connection.execute('BEGIN')
        state = calendar._read_state(connection)
        data, cal = _ledger(state, profile['schoolId']), calendar._calendar(state, profile['schoolId'])
        _cas(data, body.expectedRevision)
        return _preview(data, cal, identifier, profile)[0]


def decide(identifier, body, profile):
    profile = _actor(profile, True)
    body = _body(DecisionInput, body)
    fp = digest({'operation': 'decision', 'id': identifier, 'body': body.model_dump()})
    with _atomic_domain() as (_, state):
        _actor(profile, True)
        data, cal = _ledger(state, profile['schoolId']), calendar._calendar(state, profile['schoolId'])
        previous = _replay(data, body, fp, profile)
        if previous is not None:
            return previous
        _cas(data, body.expectedRevision)
        checked, candidate, rule, prior = _preview(data, cal, identifier, profile)
        if checked['previewHash'] != body.previewHash:
            raise HTTPException(409, 'Календарь или участники изменились; повторите предпросмотр')
        if body.decision == 'approve' and not checked['ready']:
            raise HTTPException(409, 'Пожелание пока нельзя применить: ' + '; '.join(b['message'] for b in checked['blockers']))
        row = _get(data, identifier)
        if len(row['history']) >= 200 or (prior and len(prior['history']) >= 200):
            raise HTTPException(429, 'Достигнут предел изменений пожелания')
        stamp = datetime.now(timezone.utc).isoformat()
        if body.decision == 'approve':
            if checked['changesCalendar']:
                if rule:
                    rule['updatedAt'] = stamp
                calendar._commit(state, candidate, profile, 'teacher_preference_approved', identifier)
                cal = candidate
            if rule:
                row.update(calendarRecordId=rule['id'], calendarRecordHash=digest(rule))
            if prior:
                prior.update(status='superseded', revision=prior['revision'] + 1, updatedAt=stamp)
                prior['history'].append({'action': 'superseded', 'at': stamp, 'reason': body.reason})
            current_owner = launch_identity.resolve_membership(profile['schoolId'], row['owner']['membershipId'])
            if not current_owner or not launch_identity.membership_current(current_owner) or _binding(current_owner) != row['owner']:
                raise HTTPException(409, 'Доступ или привязка автора изменились; обновите пожелание')
        row.update(status='approved' if body.decision == 'approve' else 'rejected',
            revision=row['revision'] + 1, updatedAt=stamp)
        row['history'].append({'action': body.decision, 'at': stamp, 'reason': body.reason})
        _actor(profile, True)
        return _save(state, data, body, fp, profile, row, cal)


@router.get('/api/teacher/preferences')
def own_route(profile=Depends(session_profile)):
    return list_preferences(profile)


@router.post('/api/teacher/preferences')
def submit_route(body: SubmitInput, profile=Depends(session_profile)):
    return submit(body, profile)


@router.post('/api/teacher/preferences/{identifier}')
def update_route(body: SubmitInput, identifier: str = APIPath(pattern=ID_PATTERN), profile=Depends(session_profile)):
    return submit(body, profile, identifier)


@router.get('/api/school/teacher-preferences')
def manager_route(profile=Depends(session_profile)):
    return list_preferences(profile, manager=True)


@router.post('/api/school/teacher-preferences/{identifier}/preview')
def preview_route(body: RevisionInput, identifier: str = APIPath(pattern=ID_PATTERN), profile=Depends(session_profile)):
    return preview(identifier, body, profile)


@router.post('/api/school/teacher-preferences/{identifier}/decision')
def decision_route(body: DecisionInput, identifier: str = APIPath(pattern=ID_PATTERN), profile=Depends(session_profile)):
    return decide(identifier, body, profile)
