"""Explicit facts for immutable published replacements; no timetable/payroll edits.

The assigned teacher may confirm a new conducted lesson after its confirmed
local end. Other outcomes and every correction require a separate manager
decision. An approved cancellation leaves an open case, never a hidden grid edit.
"""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field, ValidationError, field_validator

from . import db, launch_identity, school_calendar as calendar, school_publications as publications
from . import school_replacements as replacements
from .auth import session_profile
from .calendar_models import MAX_REVISION, StrictInput, iso_date
from .request_planning import canonical_manager, digest
from .school_requests import SafeRoute, _atomic_domain, require_bound_school
from .timetable import EditableLessonInput
from .workspace import school_workspace

KEY = '_replacementOutcomes'
REPLACEMENT_ID = r'^replacement_[a-f0-9]{32}$'
MAX_RECORDS = 1000
MAX_RECEIPTS = 2000
LIST_LIMIT = 100
NOTICE = 'Публикация и планирование не подтверждают проведение. Здесь сохраняются только явные исходы; правка расписания и публикация выполняются отдельно. Минуты приведены для информации, без начислений.'


class OutcomeTarget(StrictInput):
    replacementId: str = Field(pattern=REPLACEMENT_ID)
    publishedVersion: int = Field(ge=1, le=MAX_REVISION)
    lessonId: str = Field(min_length=1, max_length=80)
    date: str
    teacherRef: str = Field(min_length=1, max_length=120)
    recordHash: str = Field(pattern=r'^[a-f0-9]{64}$')
    eventHash: str = Field(pattern=r'^[a-f0-9]{64}$')

    @field_validator('date')
    @classmethod
    def day(cls, value):
        iso_date(value)
        return value


class RevisionInput(StrictInput):
    expectedRevision: int = Field(ge=0, le=MAX_REVISION)


class ReportInput(RevisionInput):
    requestId: str = Field(pattern=r'^[A-Za-z0-9_-]{16,80}$')
    target: OutcomeTarget
    kind: Literal['conducted', 'not_conducted', 'cancelled', 'other', 'retract']
    reason: str = Field(min_length=1, max_length=2000)


class DecisionInput(RevisionInput):
    requestId: str = Field(pattern=r'^[A-Za-z0-9_-]{16,80}$')
    previewHash: str = Field(pattern=r'^[a-f0-9]{64}$')
    decision: Literal['approve', 'reject']
    reason: str = Field(min_length=1, max_length=2000)


def utc_now():
    return datetime.now(timezone.utc)


def _body(model, value):
    try:
        return value if isinstance(value, model) else model.model_validate(value)
    except (ValidationError, ValueError, OverflowError):
        raise HTTPException(422, 'Проверьте ссылку на опубликованную замену, версию и основание исхода') from None


def _actor(profile, manager=False):
    if not school_workspace():
        raise HTTPException(404, 'Раздел недоступен')
    profile = canonical_manager(profile)
    role = profile.get('staffRole', profile.get('role'))
    if role not in ({'director', 'planner'} if manager else {'teacher', 'director', 'planner'}):
        raise HTTPException(403, 'Нет права фиксировать исход школьной замены')
    if role == 'teacher' and (profile.get('staffRole') != 'teacher'
            or not isinstance(profile.get('teacherRefs'), list) or not profile['teacherRefs']
            or any(not isinstance(ref, str) or not ref.strip() for ref in profile['teacherRefs'])):
        raise HTTPException(403, 'Подтвердите связь учётной записи с педагогом')
    require_bound_school(profile['schoolId'])
    return profile


def _manager(profile):
    return profile.get('staffRole', profile.get('role')) in {'director', 'planner'}


def _binding(profile):
    return {key: deepcopy(profile.get(key)) for key in ('schoolId', 'userId', 'membershipId',
        'membershipRevision', 'role', 'staffRole', 'permissions', 'teacherRefs', 'calendarTeacherIds')}


def _ledger(state, school):
    data = state.get(KEY)
    if data is None:
        return {'version': 1, 'schoolId': school, 'revision': 0, 'records': {}, 'receipts': {}}
    if not isinstance(data, dict) or data.get('schoolId') != school:
        raise HTTPException(403, 'Исходы относятся к другой школе')
    try:
        if (data.get('version') != 1 or type(data['revision']) is not int or not 0 <= data['revision'] <= MAX_REVISION
                or not isinstance(data['records'], dict) or len(data['records']) > MAX_RECORDS
                or not isinstance(data['receipts'], dict) or len(data['receipts']) > MAX_RECEIPTS):
            raise ValueError()
        for identifier, row in data['records'].items():
            if (not re.fullmatch(REPLACEMENT_ID, identifier) or set(row) != {'replacementId', 'revision', 'active', 'pending', 'history'}
                    or row['replacementId'] != identifier or type(row['revision']) is not int or row['revision'] < 1
                    or not isinstance(row['history'], list) or len(row['history']) > 200):
                raise ValueError()
            for value in (row['active'], row['pending']):
                if value is None:
                    continue
                if (set(value) != {'kind', 'reason', 'target', 'reportedBy', 'reportedAt', 'confirmedBy', 'confirmedAt', 'lessonMinutes'}
                        or value['kind'] not in {'conducted', 'not_conducted', 'cancelled', 'other', 'retract'}
                        or not isinstance(value['reason'], str) or not value['reason'].strip() or len(value['reason']) > 2000
                        or value['reportedBy']['schoolId'] != school
                        or type(value['lessonMinutes']) is not int or not 0 < value['lessonMinutes'] < 1440):
                    raise ValueError()
                target = OutcomeTarget.model_validate(value['target'])
                if target.replacementId != identifier:
                    raise ValueError()
            if row['active'] and (not row['active']['confirmedBy'] or not row['active']['confirmedAt']
                    or row['active']['confirmedBy']['schoolId'] != school or row['active']['kind'] == 'retract'):
                raise ValueError()
            if row['pending'] and (row['pending']['confirmedBy'] is not None or row['pending']['confirmedAt'] is not None):
                raise ValueError()
            for entry in row['history']:
                if (set(entry) != {'action', 'at', 'kind', 'reason', 'actor', 'before', 'after', 'reported'}
                        or entry['actor']['schoolId'] != school or not isinstance(entry['reason'], str)
                        or len(entry['reason']) > 2000 or not isinstance(entry['action'], str) or not isinstance(entry['at'], str)):
                    raise ValueError()
        for key, receipt in data['receipts'].items():
            if (not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', key)
                    or not re.fullmatch(r'[a-f0-9]{64}', receipt['fingerprint'])
                    or receipt['actor']['schoolId'] != school or not isinstance(receipt['response'], dict)):
                raise ValueError()
    except (KeyError, TypeError, AttributeError, ValueError, ValidationError):
        raise HTTPException(409, 'История исходов требует проверки; данные не заменены') from None
    return deepcopy(data)


def _snapshot(row, school):
    try:
        value = json.loads(row['snapshot_json'])
        first = iso_date(value['periodStart'])
        if (value['schoolId'] != school or value['version'] != row['version']
                or value['draftRevision'] != row['draft_revision'] or first.weekday() != 0
                or not isinstance(value['lessons'], list) or len(value['lessons']) > 2000):
            raise ValueError()
        return value
    except (KeyError, TypeError, ValueError, OverflowError):
        raise HTTPException(409, 'Опубликованная неделя повреждена; факт не сохранён') from None


def _latest_week(connection, school, week, cache):
    if week in cache:
        return cache[week]
    cursor = connection.execute('SELECT version,draft_revision,snapshot_json FROM school_publications WHERE school_id=? ORDER BY version DESC LIMIT ?',
        (school, publications.MAX_PUBLICATIONS + 1))
    for count, raw in enumerate(cursor):
        if count >= publications.MAX_PUBLICATIONS:
            raise HTTPException(429, 'Превышен предел истории публикаций')
        value = _snapshot(dict(zip(('version', 'draft_revision', 'snapshot_json'), raw)), school)
        if value['periodStart'] == week:
            cache[week] = value
            return value
    raise HTTPException(409, 'Не найдена опубликованная версия нужной недели')


def _target(connection, state, profile, identifier, cache=None):
    cache = {} if cache is None else cache
    source = replacements._ns(state, profile['schoolId'])['records'].get(identifier)
    if source is None or source.get('status') != 'published':
        raise HTTPException(404, 'Опубликованная замена не найдена')
    try:
        event = EditableLessonInput.model_validate({k: source['after'][k] for k in EditableLessonInput.model_fields}).model_dump()
        event['id'] = source['lessonId']
        if len(event['teachers']) != 1:
            raise ValueError()
        if not _manager(profile) and event['teachers'][0] not in profile['teacherRefs']:
            raise HTTPException(404, 'Опубликованная замена не найдена')
        day = iso_date(source['date']); first = day - timedelta(days=event['day'])
        if first.weekday() != 0:
            raise ValueError()
        original_row = publications._row(connection, 'SELECT * FROM school_publications WHERE school_id=? AND version=?',
            (profile['schoolId'], source['publishedVersion']))
        if original_row is None:
            raise ValueError()
        original = _snapshot(original_row, profile['schoolId'])
        parent = publications._row(connection, 'SELECT * FROM school_publications WHERE school_id=? AND id=?',
            (profile['schoolId'], original.get('parentPublicationId')))
        if parent is None:
            raise ValueError()
        parent = _snapshot(parent, profile['schoolId'])
        if (original.get('replacementId') != identifier or original['periodStart'] != first.isoformat()
                or publications._hash(parent) != source['proof']['activeHash']
                or publications._hash(replacements._grid(original['lessons'])) != source['appliedDraftHash']
                or publications._diff(replacements._grid(parent['lessons']), replacements._grid(original['lessons']))
                    != [{'lessonId': source['lessonId'], 'before': source['before'], 'after': source['after']}]):
            raise ValueError()
        latest = _latest_week(connection, profile['schoolId'], first.isoformat(), cache)
        # Equal teacher/grid values do not revive an earlier replacement in a
        # later A→B→A→B chain. The latest published identity owns this event.
        later = [row for row in replacements._ns(state, profile['schoolId'])['records'].values()
            if row.get('status') == 'published' and row.get('lessonId') == source['lessonId']
            and row.get('date') == source['date'] and type(row.get('publishedVersion')) is int
            and source['publishedVersion'] < row['publishedVersion'] <= latest['version']]
        if later:
            raise HTTPException(409, 'Эта замена заменена более поздней публикацией того же урока; прежняя ссылка доступна только в истории')
        current = [lesson for lesson in latest['lessons'] if lesson.get('id') == source['lessonId']]
        if len(current) != 1 or replacements._grid(current) != replacements._grid([source['after']]):
            raise HTTPException(409, 'Последняя публикация этой недели изменила замену; факт по прежнему уроку заблокирован')
        start = datetime.fromisoformat(source['date'] + 'T' + event['start'])
        end = datetime.fromisoformat(source['date'] + 'T' + event['end'])
        minutes = int((end - start).total_seconds() // 60)
        if not 0 < minutes < 1440:
            raise ValueError()
    except (KeyError, TypeError, ValueError, OverflowError, ValidationError):
        raise HTTPException(409, 'Ссылка на опубликованную замену требует проверки') from None
    target = {'replacementId': identifier, 'publishedVersion': latest['version'], 'lessonId': source['lessonId'],
        'date': source['date'], 'teacherRef': event['teachers'][0], 'recordHash': digest(source),
        'eventHash': digest({'date': source['date'], 'event': event})}
    return target, event, minutes


def _timing(state, profile, target, event, now=None):
    cal = calendar._calendar(state, profile['schoolId'])
    config, blockers = cal['config'], []
    result = {'calendarRevision': cal['revision'], 'calendarHash': digest(cal), 'timezone': None,
        'lessonEnded': False, 'blockers': blockers}
    if not config or not config['confirmed']:
        blockers.append({'code': 'confirmed_timezone_required', 'message': 'Для подтверждения проведения нужен подтверждённый часовой пояс школы'})
        return result
    now = utc_now() if now is None else now
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise HTTPException(409, 'Текущее время сервера не подтверждено')
    zone = ZoneInfo(config['timezone'])
    end = datetime.fromisoformat(target['date'] + 'T' + event['end']).replace(tzinfo=zone)
    result.update(timezone=config['timezone'], lessonEnded=now.astimezone(zone) >= end)
    if not result['lessonEnded']:
        blockers.append({'code': 'lesson_not_ended', 'message': 'Проведение можно подтвердить только после окончания опубликованного урока'})
    return result


def _public(row):
    def value(item):
        return None if item is None else {key: deepcopy(item[key]) for key in
            ('kind', 'reason', 'target', 'reportedAt', 'confirmedAt', 'lessonMinutes')}
    return {'replacementId': row['replacementId'], 'revision': row['revision'], 'active': value(row['active']),
        'pending': value(row['pending']), 'history': [{**{k: deepcopy(entry[k]) for k in
            ('action', 'at', 'kind', 'reason')}, 'before': value(entry['before']),
            'after': value(entry['after']), 'reported': value(entry['reported'])} for entry in row['history']],
        'openManagerCase': bool(row['pending']) or row['active'] is None or row['active']['kind'] != 'conducted'}


def _physical_rows(data, target):
    """History belongs to one physical event even when replacement IDs change."""
    key = (target['date'], target['lessonId'])
    result = []
    for row in data['records'].values():
        value = row['active'] or row['pending'] or next((entry['reported'] for entry in row['history']
            if entry['reported'] is not None), None)
        if value is not None and (value['target']['date'], value['target']['lessonId']) == key:
            result.append(row)
    return result


def _ever_confirmed(row):
    return any(entry['before'] is not None or entry['after'] is not None for entry in row['history'])


def _conducted_facts(data):
    return [row['active'] for row in data['records'].values()
            if row['active'] and row['active']['kind'] == 'conducted']


def conducted_summary(state, school):
    """Read only confirmed facts from the director's existing domain snapshot."""
    try:
        ledger = _ledger(state, school)
    except HTTPException as error:
        if error.status_code in {403, 409}:
            return None
        raise
    facts = _conducted_facts(ledger)
    physical_events = {(fact['target']['date'], fact['target']['lessonId']) for fact in facts}
    if len(physical_events) != len(facts):
        return None
    by_teacher = {}
    for fact in facts:
        name = fact['target']['teacherRef']
        row = by_teacher.setdefault(name, {'name': name, 'conductedLessons': 0, 'conductedMinutes': 0})
        row['conductedLessons'] += 1
        row['conductedMinutes'] += fact['lessonMinutes']
    dates = sorted(day for day, _ in physical_events)
    return {'revision': ledger['revision'], 'conductedLessons': len(facts),
            'conductedMinutes': sum(row['conductedMinutes'] for row in by_teacher.values()),
            'firstDate': dates[0] if dates else None, 'lastDate': dates[-1] if dates else None,
            'byTeacher': [by_teacher[name] for name in sorted(by_teacher)]}


def _cas(data, revision):
    if data['revision'] != revision or revision >= MAX_REVISION:
        raise HTTPException(409, 'Исходы изменились; обновите список перед подтверждением')


def _replay(data, body, fingerprint, profile):
    old = data['receipts'].get(body.requestId)
    if old:
        if old['fingerprint'] != fingerprint or old['actor'] != _binding(profile):
            raise HTTPException(409, 'Идентификатор запроса связан с другим действием')
        return deepcopy(old['response'])
    if len(data['receipts']) >= MAX_RECEIPTS:
        raise HTTPException(429, 'Достигнут предел квитанций исходов')
    return None


def _save(state, data, body, fingerprint, profile, row):
    data['revision'] += 1
    response = {'revision': data['revision'], 'record': _public(row), 'notice': NOTICE}
    data['receipts'][body.requestId] = {'fingerprint': fingerprint, 'actor': _binding(profile), 'response': deepcopy(response)}
    if len(json.dumps(data, ensure_ascii=False).encode()) > 8 * 1024 * 1024:
        raise HTTPException(429, 'Достигнут предел истории исходов; прежние данные сохранены')
    state[KEY] = data
    return response


def _read_connection(storage):
    return publications._read_connection() if storage is None else storage.read_connection()


def list_outcomes(profile, *, manager=False, replacement_id=None, storage=None):
    profile = _actor(profile, manager)
    connection = _read_connection(storage)
    if connection is None:
        return {'revision': 0, 'items': [], 'truncated': False, 'conductedLessons': 0, 'conductedMinutes': 0, 'notice': NOTICE}
    try:
        connection.execute('BEGIN')
        state = calendar._read_state(connection); data = _ledger(state, profile['schoolId'])
        sources = replacements._ns(state, profile['schoolId'])['records']
        selected = [row for row in sources.values() if row.get('status') == 'published' and
            (replacement_id is None or row.get('id') == replacement_id) and
            (manager or set(row.get('after', {}).get('teachers', [])) & set(profile['teacherRefs']))]
        selected.sort(key=lambda r: (r.get('createdAt', ''), r['id']), reverse=True)
        items, cache = [], {}
        for source in selected[:LIST_LIMIT]:
            try:
                target, event, minutes = _target(connection, state, profile, source['id'], cache)
                timing = _timing(state, profile, target, event)
                items.append({'target': target, 'lesson': {k: deepcopy(event[k]) for k in
                    ('className', 'subject', 'day', 'slot', 'start', 'end', 'rooms')},
                    'canConfirmConducted': not manager and not timing['blockers'], 'blockers': timing['blockers'],
                    'timezone': timing['timezone'], 'outcome': _public(data['records'][source['id']]) if source['id'] in data['records'] else None})
            except HTTPException as error:
                if error.status_code in {401, 403, 503}:raise
                items.append({'replacementId': source['id'], 'target': None, 'lesson': None,
                    'canConfirmConducted': False, 'blockers': [{'code': 'publication_changed', 'message': str(error.detail)}],
                    'timezone': None, 'outcome': _public(data['records'][source['id']]) if source['id'] in data['records'] else None})
        if replacement_id and not selected:
            raise HTTPException(404, 'Опубликованная замена не найдена')
        facts = [fact for fact in _conducted_facts(data)
                 if manager or fact['target']['teacherRef'] in profile['teacherRefs']]
        return {'revision': data['revision'], 'items': items, 'truncated': len(selected) > LIST_LIMIT,
            'conductedLessons': len(facts), 'conductedMinutes': sum(item['lessonMinutes'] for item in facts), 'notice': NOTICE}
    finally:
        connection.close()


def report(body, profile, *, storage=None):
    profile = _actor(profile)
    body = _body(ReportInput, body)
    if body.kind == 'conducted' and _manager(profile):
        raise HTTPException(403, 'Проведение сообщает назначенный педагог; составитель рассматривает остальные исходы')
    fingerprint = digest({'operation': 'report', 'body': body.model_dump()})
    with (_atomic_domain() if storage is None else storage.atomic_domain()) as (connection, state):
        _actor(profile)
        data = _ledger(state, profile['schoolId'])
        old = _replay(data, body, fingerprint, profile)
        if old is not None:return old
        _cas(data, body.expectedRevision)
        target, event, minutes = _target(connection, state, profile, body.target.replacementId)
        if target != body.target.model_dump():
            raise HTTPException(409, 'Опубликованная замена изменилась; обновите точную ссылку')
        now = utc_now()
        timing = _timing(state, profile, target, event, now)
        if body.kind == 'conducted' and timing['blockers']:
            raise HTTPException(409, '; '.join(b['message'] for b in timing['blockers']))
        row = data['records'].get(target['replacementId'])
        if row is None:
            if len(data['records']) >= MAX_RECORDS:raise HTTPException(429, 'Достигнут предел исходов замен')
            row = {'replacementId': target['replacementId'], 'revision': 0, 'active': None, 'pending': None, 'history': []}
        if row['pending'] is not None:
            raise HTTPException(409, 'Предыдущее сообщение об исходе ещё рассматривается')
        related = _physical_rows(data, target)
        if any(other['replacementId'] != target['replacementId'] and other['pending'] is not None for other in related):
            raise HTTPException(409, 'Исход того же физического урока уже рассматривается; сначала завершите его рассмотрение')
        if body.kind == 'retract' and not any(other['active'] is not None for other in related):
            raise HTTPException(409, 'Нет подтверждённого исхода для отзыва')
        if len(row['history']) >= 200:raise HTTPException(429, 'Достигнут предел истории исхода')
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:raise HTTPException(409, 'Текущее время сервера не подтверждено')
        stamp = now.astimezone(timezone.utc).isoformat()
        value = {'kind': body.kind, 'reason': body.reason, 'target': target, 'reportedBy': _binding(profile),
            'reportedAt': stamp, 'confirmedBy': None, 'confirmedAt': None, 'lessonMinutes': minutes}
        before = deepcopy(row['active'])
        ever_confirmed = any(_ever_confirmed(other) for other in related)
        direct = body.kind == 'conducted' and row['active'] is None and not ever_confirmed and not _manager(profile)
        if direct:
            value.update(confirmedBy=_binding(profile), confirmedAt=stamp); row['active'] = value
        else:
            row['pending'] = value
        row['revision'] += 1
        row['history'].append({'action': 'conducted_confirmed' if direct else 'reported_for_review',
            'at': stamp, 'kind': body.kind, 'reason': body.reason, 'actor': _binding(profile),
            'before': before, 'after': deepcopy(row['active']), 'reported': deepcopy(value)})
        data['records'][target['replacementId']] = row
        _actor(profile)
        return _save(state, data, body, fingerprint, profile, row)


def _review(connection, state, data, identifier, profile, now=None):
    row = data['records'].get(identifier)
    if row is None or row['pending'] is None:
        raise HTTPException(409, 'Нет сообщения об исходе для рассмотрения')
    pending = row['pending']; blockers = []
    try:
        target, event, _ = _target(connection, state, profile, identifier)
    except HTTPException as error:
        if error.status_code in {401, 403, 503}:raise
        target, event = pending['target'], None
        blockers.append({'code': 'publication_changed', 'message': str(error.detail)})
    if target != pending['target']:
        blockers.append({'code': 'publication_changed', 'message': 'Замена опубликована в другой версии; нужен новый точный исход'})
    owner = launch_identity.resolve_membership(profile['schoolId'], pending['reportedBy']['membershipId'])
    if not owner or not launch_identity.membership_current(owner) or _binding(owner) != pending['reportedBy']:
        blockers.append({'code': 'author_changed', 'message': 'Доступ или привязка автора изменились'})
    elif pending['kind'] == 'conducted' and (_manager(owner) or target['teacherRef'] not in owner.get('teacherRefs', [])):
        blockers.append({'code': 'conducted_author_invalid', 'message': 'Проведение должен сообщить назначенный педагог'})
    if event is not None:
        timing = _timing(state, profile, target, event, now)
    else:
        cal = calendar._calendar(state, profile['schoolId'])
        timing = {'calendarRevision': cal['revision'], 'calendarHash': digest(cal), 'lessonEnded': False, 'blockers': []}
    if pending['kind'] == 'conducted':blockers.extend(timing['blockers'])
    related = _physical_rows(data, pending['target'])
    others = [other for other in related if other['replacementId'] != identifier]
    if any(other['pending'] is not None for other in others):
        blockers.append({'code': 'physical_outcome_pending', 'message': 'Другой исход того же физического урока ещё рассматривается'})
    if any(other['active'] is not None and len(other['history']) >= 200 for other in others):
        blockers.append({'code': 'physical_history_limit', 'message': 'Достигнут предел истории прежнего исхода этого урока'})
    binding = {'schoolId': profile['schoolId'], 'revision': data['revision'], 'rowHash': digest(row),
        'target': target, 'calendarRevision': timing['calendarRevision'], 'calendarHash': timing['calendarHash'],
        'owner': _binding(owner) if owner else None, 'manager': _binding(profile), 'lessonEnded': timing['lessonEnded'],
        'physicalHistoryHash': digest(related), 'blockers': blockers}
    return {'replacementId': identifier, 'revision': data['revision'], 'previewHash': digest(binding),
        'ready': not blockers, 'blockers': blockers, 'record': _public(row), 'notice': NOTICE}


def preview(identifier, body, profile, *, storage=None):
    profile = _actor(profile, True); body = _body(RevisionInput, body)
    connection = _read_connection(storage)
    if connection is None:raise HTTPException(404, 'Исход не найден')
    try:
        connection.execute('BEGIN'); state = calendar._read_state(connection); data = _ledger(state, profile['schoolId'])
        _cas(data, body.expectedRevision)
        return _review(connection, state, data, identifier, profile)
    finally:connection.close()


def decide(identifier, body, profile, *, storage=None):
    profile = _actor(profile, True); body = _body(DecisionInput, body)
    fingerprint = digest({'operation': 'decision', 'id': identifier, 'body': body.model_dump()})
    with (_atomic_domain() if storage is None else storage.atomic_domain()) as (connection, state):
        _actor(profile, True); data = _ledger(state, profile['schoolId'])
        old = _replay(data, body, fingerprint, profile)
        if old is not None:return old
        _cas(data, body.expectedRevision)
        now = utc_now()
        checked = _review(connection, state, data, identifier, profile, now)
        if checked['previewHash'] != body.previewHash:
            raise HTTPException(409, 'Исход, публикация, календарь или участники изменились; повторите предпросмотр')
        if body.decision == 'approve' and not checked['ready']:
            raise HTTPException(409, 'Исход требует уточнения перед подтверждением')
        row = data['records'][identifier]; pending = row['pending']
        if len(row['history']) >= 200:raise HTTPException(429, 'Достигнут предел истории исхода')
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:raise HTTPException(409, 'Текущее время сервера не подтверждено')
        stamp = now.astimezone(timezone.utc).isoformat()
        before = deepcopy(row['active'])
        if body.decision == 'approve':
            current = launch_identity.resolve_membership(profile['schoolId'], pending['reportedBy']['membershipId'])
            if not current or not launch_identity.membership_current(current) or _binding(current) != pending['reportedBy']:
                raise HTTPException(409, 'Доступ или привязка автора изменились')
            # This manager decision replaces the physical fact, not merely an
            # independent replacement record. Preserve each prior fact/history
            # and every old receipt while removing its active contribution.
            for other in _physical_rows(data, pending['target']):
                if other['replacementId'] == identifier or other['active'] is None:
                    continue
                previous = deepcopy(other['active'])
                other['active'] = None; other['revision'] += 1
                other['history'].append({'action': 'replaced_by_review', 'at': stamp, 'kind': pending['kind'],
                    'reason': body.reason, 'actor': _binding(profile), 'before': previous, 'after': None,
                    'reported': deepcopy(pending)})
            if pending['kind'] == 'retract':row['active'] = None
            else:
                row['active'] = {**deepcopy(pending), 'confirmedBy': _binding(profile), 'confirmedAt': stamp}
        row['pending'] = None; row['revision'] += 1
        row['history'].append({'action': body.decision, 'at': stamp, 'kind': pending['kind'],
            'reason': body.reason, 'actor': _binding(profile), 'before': before,
            'after': deepcopy(row['active']), 'reported': deepcopy(pending)})
        _actor(profile, True)
        return _save(state, data, body, fingerprint, profile, row)


def router_for(storage=None):
    routes = APIRouter(tags=['replacement outcomes'], route_class=SafeRoute)

    @routes.get('/api/teacher/replacement-outcomes')
    def own_route(replacementId: str | None = Query(default=None, pattern=REPLACEMENT_ID), profile=Depends(session_profile)):
        if _manager(_actor(profile)):raise HTTPException(403, 'Личный журнал предназначен педагогу')
        return list_outcomes(profile, replacement_id=replacementId, storage=storage)

    @routes.post('/api/teacher/replacement-outcomes')
    def report_route(body: ReportInput, profile=Depends(session_profile)):
        if _manager(_actor(profile)):raise HTTPException(403, 'Личный журнал предназначен педагогу')
        return report(body, profile, storage=storage)

    @routes.get('/api/school/replacement-outcomes')
    def manager_route(replacementId: str | None = Query(default=None, pattern=REPLACEMENT_ID), profile=Depends(session_profile)):
        return list_outcomes(profile, manager=True, replacement_id=replacementId, storage=storage)

    @routes.post('/api/school/replacement-outcomes')
    def manager_report_route(body: ReportInput, profile=Depends(session_profile)):
        return report(body, _actor(profile, True), storage=storage)

    @routes.post('/api/school/replacement-outcomes/{identifier}/preview')
    def preview_route(body: RevisionInput, identifier: str, profile=Depends(session_profile)):
        if not re.fullmatch(REPLACEMENT_ID, identifier):raise HTTPException(422, 'Некорректная ссылка на замену')
        return preview(identifier, body, profile, storage=storage)

    @routes.post('/api/school/replacement-outcomes/{identifier}/decision')
    def decision_route(body: DecisionInput, identifier: str, profile=Depends(session_profile)):
        if not re.fullmatch(REPLACEMENT_ID, identifier):raise HTTPException(422, 'Некорректная ссылка на замену')
        return decide(identifier, body, profile, storage=storage)

    return routes


router = router_for()
