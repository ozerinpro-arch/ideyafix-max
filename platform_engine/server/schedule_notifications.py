"""Addressed in-app notices from immutable publications; no external sends.

The existing own-schedule readers remain the privacy authority. Availability
in the inbox, explicit acknowledgement and external delivery are distinct facts.
"""
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import re
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import Field

from . import school_publications as publications, staff_access
from .auth import session_profile
from .request_planning import canonical_manager, digest
from .school_requests import SafeRoute, _atomic_domain, require_bound_school
from .scheduling_models import StrictModel

KEY = '_scheduleNotifications'
LIMIT = 2000
NOTICE = 'Здесь показаны уведомления кабинета. Доставка во внешние каналы учитывается отдельно. Получатель подтверждает прочтение в кабинете.'
SCHEDULE_NOTICE_TEXT = 'Опубликована новая версия вашего расписания. Проверьте занятия в личном расписании.'


class PrepareInput(StrictModel):
    expectedRevision: int = Field(ge=0, le=2**31-1)
    expectedPublicationVersion: int = Field(ge=1, le=2**31-1)
    requestId: str = Field(pattern=r'^[A-Za-z0-9_-]{16,80}$')


class AcknowledgeInput(StrictModel):
    expectedRevision: int = Field(ge=1, le=2**31-1)
    requestId: str = Field(pattern=r'^[A-Za-z0-9_-]{16,80}$')


def _binding(actor):
    return {key: deepcopy(actor.get(key)) for key in ('schoolId', 'userId', 'membershipId',
        'membershipRevision', 'staffRole', 'teacherRefs', 'calendarTeacherIds')}


def _ledger(state, school):
    value = state.get(KEY, {'version': 1, 'schoolId': school, 'revision': 0,
                           'items': {}, 'receipts': {}})
    try:
        if (value['version'] != 1 or value['schoolId'] != school
                or type(value['revision']) is not int or not 0 <= value['revision'] < 2**31
                or not isinstance(value['items'], dict) or len(value['items']) > LIMIT
                or not isinstance(value['receipts'], dict) or len(value['receipts']) > LIMIT):
            raise ValueError()
        for key, item in value['items'].items():
            if (not isinstance(key, str) or not re.fullmatch(r'notice_[a-f0-9]{32}', key)
                    or item['id'] != key or item['schoolId'] != school
                    or item['recipient']['schoolId'] != school
                    or item['status'] not in {'available', 'acknowledged'}
                    or type(item['revision']) is not int or item['revision'] < 1
                    or type(item['publicationVersion']) is not int or item['publicationVersion'] < 1
                    or not isinstance(item['publicationId'], str) or not item['publicationId']
                    or not isinstance(item['text'], str) or len(item['text']) > 2000
                    or not isinstance(item['createdAt'], str)
                    or not isinstance(item['periodStart'], str)
                    or (item['acknowledgedAt'] is not None and not isinstance(item['acknowledgedAt'], str))):
                raise ValueError()
        for key, receipt in value['receipts'].items():
            if (not isinstance(key, str) or not isinstance(receipt['fingerprint'], str)
                    or not re.fullmatch(r'[a-f0-9]{64}', receipt['fingerprint'])
                    or not isinstance(receipt['actor'], dict) or receipt['actor']['schoolId'] != school
                    or not isinstance(receipt['response'], dict)):
                raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise HTTPException(409, 'История уведомлений требует проверки; записи не заменены') from None
    return deepcopy(value)


class ScheduleNotificationService:
    def __init__(self, own_reader: Callable, *, storage=None):
        self.own_reader = own_reader
        self.storage = storage

    def actor(self, profile, *, manager=False):
        actor = canonical_manager(profile)
        require_bound_school(actor['schoolId'])
        current = staff_access.resolve_membership(actor['schoolId'], actor['membershipId'])
        if not current or _binding(actor) != _binding(current):
            raise HTTPException(403, 'Привязка школьного доступа изменилась')
        roles = {'director', 'planner'} if manager else {'teacher', 'student'}
        if actor.get('staffRole') not in roles:
            raise HTTPException(403, 'Действие недоступно для этой школьной роли')
        return actor

    @contextmanager
    def _read_connection(self):
        if self.storage is None:
            yield None
            return
        with closing(self.storage.read_connection()) as connection:
            connection.execute('BEGIN')
            yield connection

    def _atomic(self):
        return _atomic_domain() if self.storage is None else self.storage.atomic_domain()

    def _read(self, connection):
        from .school_calendar import _read_state
        return _read_state(connection)

    def _own(self, profile, connection):
        if self.storage is None:
            return self.own_reader(profile)
        return self.own_reader(profile, connection=connection)

    def active_publication(self, school):
        with self._read_connection() as connection:
            return publications.read_active_snapshot(school, connection)

    def status(self, profile):
        actor = self.actor(profile, manager=True)
        with self._read_connection() as connection:
            data = _ledger(self._read(connection), actor['schoolId'])
            active = publications.read_active_snapshot(actor['schoolId'], connection)
        rows = list(data['items'].values())
        return {'revision': data['revision'], 'publicationVersion': active['version'] if active else None,
            'prepared': len(rows), 'acknowledged': sum(r['status'] == 'acknowledged' for r in rows),
            'externalDelivery': 'not_configured', 'notice': NOTICE}

    def current_teacher_notices(self, profile):
        actor = self.actor(profile, manager=True)
        school = actor['schoolId']
        with self._read_connection() as connection:
            active = publications.read_active_snapshot(school, connection)
            data = _ledger(self._read(connection), school) if active else None
        members = {row['membershipId']: row for row in staff_access.list_respondents(school)
                   if row['staffRole'] == 'teacher'} if active else {}
        rows = []
        if active:
            for row in data['items'].values():
                if row['publicationId'] != active['publicationId']:
                    continue
                member = members.get(row['recipient'].get('membershipId'))
                if member and _binding(member) == row['recipient']:
                    rows.append({'id': row['id'], 'membershipId': member['membershipId'],
                                 'name': member['name'], 'status': row['status']})
        self.actor(actor, manager=True)
        return active, rows

    def _receipt(self, data, actor, body, fingerprint):
        previous = data['receipts'].get(actor['membershipId'] + ':' + body.requestId)
        if previous:
            if previous['fingerprint'] != fingerprint or previous['actor'] != _binding(actor):
                raise HTTPException(409, 'Запрос уже использован другим действием или доступ изменился')
            self.actor(actor, manager=actor['staffRole'] in {'director', 'planner'})
            return deepcopy(previous['response'])
        return None

    def _save(self, state, data, actor, body, fingerprint, response):
        self.actor(actor, manager=actor['staffRole'] in {'director', 'planner'})
        if len(data['receipts']) >= LIMIT or len(data['items']) > LIMIT or data['revision'] >= 2**31-1:
            raise HTTPException(429, 'Достигнут предел уведомлений; требуется архивирование')
        data['revision'] += 1
        data['receipts'][actor['membershipId'] + ':' + body.requestId] = {
            'fingerprint': fingerprint, 'actor': _binding(actor), 'response': deepcopy(response)}
        if len(json.dumps(data, ensure_ascii=False).encode()) > 8 * 1024 * 1024:
            raise HTTPException(429, 'Достигнут предел размера уведомлений')
        state[KEY] = data

    def prepare(self, body, profile):
        actor = self.actor(profile, manager=True)
        body = PrepareInput.model_validate(body)
        fingerprint = digest({'operation': 'prepare', 'body': body.model_dump()})
        with self._atomic() as (connection, state):
            data = _ledger(state, actor['schoolId'])
            previous = self._receipt(data, actor, body, fingerprint)
            if previous is not None:
                return previous
            if data['revision'] != body.expectedRevision:
                raise HTTPException(409, 'Список уведомлений изменился; обновите его')
            active = publications.read_active_snapshot(actor['schoolId'], connection)
            if active is None or active['version'] != body.expectedPublicationVersion:
                raise HTTPException(409, 'Опубликованная версия изменилась')
            names = {name for row in active['lessons'] for name in row['teachers']}
            if publications._exists(connection, 'school_publications'):
                old = connection.execute('SELECT snapshot_json FROM school_publications WHERE school_id=? AND version=?',
                    (actor['schoolId'], active['version']-1)).fetchone()
                old = json.loads(old[0]) if old else None
                if old and old['periodStart'] == active['periodStart']:
                    names.update(name for row in old['lessons'] for name in row['teachers'])
            created, matched, blocked = 0, set(), []
            for recipient in staff_access.list_respondents(actor['schoolId']):
                role = recipient.get('staffRole')
                if role not in {'teacher', 'student'} or not staff_access.membership_current(recipient):
                    continue
                refs = set(recipient.get('teacherRefs', []))
                if role == 'teacher' and not refs.intersection(names):
                    continue
                try:
                    own = self._own(recipient, connection)
                except HTTPException as error:
                    if error.status_code == 503:
                        raise
                    blocked.append({'memberId': recipient['membershipId'], 'code': 'own_schedule_unavailable'})
                    continue
                if own.get('publicationVersion') != active['version']:
                    blocked.append({'memberId': recipient['membershipId'], 'code': 'binding_required'})
                    continue
                current = staff_access.resolve_membership(actor['schoolId'], recipient['membershipId'])
                if not current or _binding(recipient) != _binding(current):
                    blocked.append({'memberId': recipient['membershipId'], 'code': 'membership_changed'})
                    continue
                identity = 'notice_' + digest([active['publicationId'], _binding(recipient)])[:32]
                matched.update(refs.intersection(names))
                if identity in data['items']:
                    continue
                # Only a short notice is stored. Lesson content is always read
                # through the current own-schedule authority, never cached here.
                data['items'][identity] = {'id': identity, 'schoolId': actor['schoolId'],
                    'revision': 1, 'publicationId': active['publicationId'], 'publicationVersion': active['version'],
                    'periodStart': active['periodStart'], 'recipient': _binding(recipient),
                    'status': 'available', 'createdAt': datetime.now(timezone.utc).isoformat(),
                    'acknowledgedAt': None, 'text': SCHEDULE_NOTICE_TEXT}
                created += 1
            response = {'revision': data['revision']+1, 'publicationVersion': active['version'],
                'created': created, 'missingTeacherBindings': sorted(names-matched), 'blockedRecipients': blocked,
                'externalDelivery': 'not_configured', 'notice': NOTICE}
            # Check again after all per-recipient reads; no stale snapshot may
            # be labelled as the active publication of an inbox preparation.
            if publications.read_active_snapshot(actor['schoolId'], connection) != active:
                raise HTTPException(409, 'Публикация изменилась во время подготовки')
            self._save(state, data, actor, body, fingerprint, response)
            return response

    def own(self, profile):
        actor = self.actor(profile)
        with self._read_connection() as connection:
            active = publications.read_active_snapshot(actor['schoolId'], connection)
            own = self._own(actor, connection)
            data = _ledger(self._read(connection), actor['schoolId'])
        rows = []
        if active and own.get('publicationVersion') == active['version']:
            for row in data['items'].values():
                if row['recipient'] == _binding(actor) and row['publicationId'] == active['publicationId']:
                    rows.append({k: deepcopy(v) for k, v in row.items() if k not in {'recipient', 'schoolId'}})
        self.actor(actor)
        return {'items': rows, 'publicationVersion': active['version'] if active else None, 'notice': NOTICE}

    def acknowledge(self, identifier, body, profile):
        actor = self.actor(profile)
        body = AcknowledgeInput.model_validate(body)
        fingerprint = digest({'operation': 'acknowledge', 'id': identifier, 'body': body.model_dump()})
        with self._atomic() as (connection, state):
            data = _ledger(state, actor['schoolId'])
            previous = self._receipt(data, actor, body, fingerprint)
            if previous is not None:
                return previous
            row = data['items'].get(identifier)
            if not row or row['recipient'] != _binding(actor):
                raise HTTPException(404, 'Уведомление не найдено')
            active = publications.read_active_snapshot(actor['schoolId'], connection)
            if (not active or active['publicationId'] != row['publicationId']
                    or row['revision'] != body.expectedRevision or row['status'] != 'available'):
                raise HTTPException(409, 'Версия уведомления изменилась')
            own = self._own(actor, connection)
            if own.get('publicationVersion') != active['version']:
                raise HTTPException(409, 'Личное расписание требует обновления')
            row.update(status='acknowledged', revision=row['revision']+1,
                       acknowledgedAt=datetime.now(timezone.utc).isoformat())
            response = {k: deepcopy(v) for k, v in row.items() if k not in {'recipient', 'schoolId'}}
            self._save(state, data, actor, body, fingerprint, response)
            return response


def service_for(student_schedule, *, storage=None):
    def own_reader(profile, *, connection=None):
        reader = student_schedule.own if profile.get('staffRole') == 'student' else staff_access.own_timetable
        return reader(profile) if connection is None else reader(profile, connection=connection)
    return ScheduleNotificationService(own_reader, storage=storage)


def router_for(service, *, jobs=None):
    router = APIRouter(tags=['schedule notifications'], route_class=SafeRoute)

    @router.get('/api/school/schedule-notifications')
    def status(profile=Depends(session_profile)):
        result = service.status(profile)
        if jobs is not None:
            result['backgroundPreparation'] = True
        return result

    @router.post('/api/school/schedule-notifications/prepare')
    def prepare(body: PrepareInput, profile=Depends(session_profile)):
        if jobs is not None:
            actor = service.actor(profile, manager=True)
            if service.storage is not None:
                service.storage.queue_identity()
            code, result = jobs.submit(body.model_dump(), actor)
            return JSONResponse(result, status_code=code)
        return service.prepare(body, profile)

    @router.get('/api/staff/schedule-notifications')
    def own(profile=Depends(session_profile)):
        return service.own(profile)

    @router.post('/api/staff/schedule-notifications/{identifier}/acknowledge')
    def acknowledge(identifier: str, body: AcknowledgeInput, profile=Depends(session_profile)):
        return service.acknowledge(identifier, body, profile)

    return router
