"""Durable school requests: extraction is advisory, only exact manager approval writes a draft.

Optional extraction runs before domain transactions; no seed, calendar or publication side effects.
"""
from __future__ import annotations
from contextlib import closing, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timedelta, timezone, date
import hashlib
import json
import re
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from . import db, launch_runtime, timetable, request_extraction
from .auth import session_profile
from .workspace import empty_school_state, verify_school_state
from .school_publications import read_active_snapshot

ZONE = ZoneInfo('Europe/Moscow')
LIMIT = 2000
COMMAND_LIMIT = 10000
MAX_NAMESPACE_BYTES = 8 * 1024 * 1024
_school_context = ContextVar("school_requests_domain", default=None)


@contextmanager
def bound_school(school_id):
    """Trusted per-app/host context, not an identity taken from an HTTP/MAX body."""
    if not isinstance(school_id, str) or not 1 <= len(school_id) <= 120:
        raise ValueError('Explicit school binding required')
    token = _school_context.set(school_id)
    try:
        yield
    finally:
        _school_context.reset(token)


def require_bound_school(school_id):
    bound = _school_context.get()
    if bound is None:
        raise HTTPException(409, 'Не подтверждена привязка приложения к школе')
    if bound != school_id:
        raise HTTPException(403, 'Запрос относится к другой школе')


class DomainSchoolMiddleware:
    def __init__(self, app, school_id):
        self.app, self.school_id = app, school_id

    async def __call__(self, scope, receive, send):
        with bound_school(self.school_id):
            await self.app(scope, receive, send)


def configure_extraction_hook(hook=None, *, path=None):
    """Compatibility for explicit host/tests; scoped to the current context.

    New hosts should inject bound_service, never a process-global callable.
    This function performs no model request or schema write.
    """
    school = _school_context.get()
    if hook is not None and school is None:
        raise ValueError('Bind a trusted school before configuring extraction')
    service = request_extraction.ExtractionService(school,
        path or db.database_path().with_suffix('.extraction.sqlite3'), hook) if hook is not None else None
    request_extraction._service.set(service)


def utc_now():
    return datetime.now(timezone.utc)


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True)


class Fields(Input):
    kind: Literal['unavailability', 'add_lesson', 'change_lesson', 'question'] | None = None
    date: str | None = Field(default=None, pattern=r'^\d{4}-\d{2}-\d{2}$')
    slot: int | None = Field(default=None, ge=1, le=20)
    lessonIds: list[str] = Field(default_factory=list, max_length=30)
    lesson: timetable.EditableLessonInput | None = None


class CreateRequest(Fields):
    requestId: str = Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')
    text: str = Field(default='', max_length=4000)
    kind: Literal['unavailability', 'add_lesson', 'change_lesson', 'question'] | None = None


class Command(Input):
    requestId: str = Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')
    expectedRevision: int = Field(ge=0, le=2**31-1)


class ReplyInput(Command):
    text: str = Field(min_length=1, max_length=4000)
    fields: Fields | None = None


class QuestionInput(Command):
    text: str = Field(min_length=1, max_length=2000)
    respondentMembershipId: str = Field(min_length=1, max_length=120)


class ProposalInput(Command):
    expectedDraftRevision: int = Field(ge=0, le=2**31-1)
    action: Literal['add', 'change']
    lessonId: str | None = Field(default=None, min_length=1, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')
    lesson: timetable.EditableLessonInput
    targetWeekStart: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')


class DecisionInput(Command):
    decision: Literal['approve', 'reject']
    proposalHash: str = Field(pattern=r'^[a-f0-9]{64}$')


def _body(cls, value):
    if isinstance(value, cls):
        return value
    from pydantic import ValidationError
    try:
        return cls.model_validate(value)
    except ValidationError:
        raise HTTPException(422, 'Некорректные поля школьной заявки') from None


def _role(profile):
    return profile.get('staffRole', profile.get('role'))


def _actor(profile, manage=False):
    result = launch_runtime.actor(profile, 'launch.manage' if manage else 'launch.read')
    if _role(result) not in {'teacher', 'director', 'planner'}:
        raise HTTPException(403, 'Роль не работает со школьными заявками')
    require_bound_school(result['schoolId'])
    return result


def _domain(profile):
    require_bound_school(profile['schoolId'])


def _manager(profile):
    return _role(profile) in {'director', 'planner'} and 'launch.manage' in profile.get('permissions', [])


def _ns(state, school_id):
    return state.setdefault('_schoolRequests', {'formatVersion': 1, 'schools': {}})['schools'].setdefault(school_id, {'requests': {}, 'commands': {}})


def _read_ns(state, school_id):
    return state.get('_schoolRequests', {}).get('schools', {}).get(school_id, {'requests': {}, 'commands': {}})


def _binding(profile):
    return {k: deepcopy(profile[k]) for k in ('schoolId', 'userId', 'membershipId', 'membershipRevision')}


def _current_binding(binding, profile):
    member = launch_runtime.resolve_member(profile, binding['membershipId'])
    if any(member.get(k) != value for k, value in binding.items()):
        raise HTTPException(409, 'Состав или полномочия участника изменились; требуется новое согласование')
    return member


def _get(data, ident):
    item = data['requests'].get(ident)
    if item is None:
        raise HTTPException(404, 'Заявка не найдена')
    return item


def _allowed(item, profile):
    return _manager(profile) or item['owner']['membershipId'] == profile['membershipId'] or any(q['respondentMembershipId'] == profile['membershipId'] for q in item['questions'])


def _access(item, profile):
    if item['schoolId'] != profile['schoolId'] or not _allowed(item, profile):
        raise HTTPException(404, 'Заявка не найдена')


def _cas(item, revision):
    if item['revision'] != revision:
        raise HTTPException(409, 'Заявка изменена: обновите её перед ответом')
    if item['status'] in {'applied', 'rejected'}:
        raise HTTPException(409, 'Заявка уже завершена')


def _own(profile, connection=None):
    if _role(profile) != 'teacher':
        return [], []
    snapshot = read_active_snapshot(profile['schoolId'], connection)
    if snapshot is None:
        return [], ['publication_required']
    refs = profile.get('teacherRefs', [])
    known = {t for row in snapshot['lessons'] for t in row.get('teachers', [])}
    if not refs or not set(refs) <= known:
        return [], ['teacher_binding_required']
    try:
        start = date.fromisoformat(snapshot['periodStart'])
        if start.weekday() != 0: raise ValueError()
    except (ValueError, KeyError):
        return [], ['publication_period_required']
    return [{**{k: deepcopy(row[k]) for k in ('id', 'className', 'day', 'slot', 'start', 'end', 'subject', 'rooms')},
             'date': (start + timedelta(days=row['day'])).isoformat()} for row in snapshot['lessons'] if set(refs) & set(row.get('teachers', []))], []


def _public(item, profile, *, replacement=None, connection=None):
    value = deepcopy(item)
    for key in ('schoolId', 'ownerBinding', 'proposalActor', 'requestHash', 'replacement'):
        value.pop(key, None)
    value['canManage'] = _manager(profile)
    if not _manager(profile):
        own, _ = _own(profile, connection)
        ids = {x['id'] for x in own}
        value['lessonIds'] = [x for x in value['lessonIds'] if x in ids]
        value['questions'] = [q for q in value['questions'] if q['respondentMembershipId'] == profile['membershipId']]
        value['messages'] = [m for m in value['messages'] if m['membershipId'] == profile['membershipId'] or m.get('respondentMembershipId') == profile['membershipId']]
        value['proposal'] = None
        if item['owner']['membershipId'] != profile['membershipId']:
            value['text'] = ''
            value['lesson'] = None
        elif value.get('lesson'):
            value['lesson']['teachers'] = [t for t in value['lesson']['teachers'] if t in profile.get('teacherRefs', [])]
    if replacement is not None:
        value['replacement'] = replacement
    return value


def _response(item, profile, receipt=None, *, connection=None):
    result = {'request': _public(item, profile, connection=connection), 'serverReply': item['serverReply']}
    if receipt is not None: result['receipt'] = deepcopy(receipt)
    return result


def _replay(data, key, fingerprint, profile, *, connection=None):
    receipt = data['commands'].get(key)
    if receipt:
        if receipt['hash'] != fingerprint or receipt['actor'] != _binding(profile):
            raise HTTPException(409, 'Идентификатор команды уже использован иначе')
        _access(_get(data, receipt['requestId']), profile)
        return _response(receipt['result'], profile, receipt['receipt'], connection=connection)
    if len(data['commands']) >= COMMAND_LIMIT:
        raise HTTPException(429, 'Достигнут лимит команд заявок; нужен разбор оператором')
    return None


def _record(data, key, fingerprint, item, profile, draft_revision=None, *, connection=None):
    _actor(profile)
    receipt = {'commandId': key, 'requestId': item['id'], 'requestRevision': item['revision'], 'status': item['status'], 'draftRevision': draft_revision}
    data['commands'][key] = {'hash': fingerprint, 'actor': _binding(profile), 'requestId': item['id'], 'result': deepcopy(item), 'receipt': receipt}
    if len(json.dumps(data, ensure_ascii=False).encode()) > MAX_NAMESPACE_BYTES:
        raise HTTPException(429, 'Хранилище заявок заполнено; требуется разбор оператором')
    return _response(item, profile, receipt, connection=connection)


def _recognize(text, received_at, advisory=None):
    lower = text.casefold(); found = {}; ambiguities = []
    if re.search(r'не\s*(?:смогу|могу)|недоступ|отсутств|не\s*буду', lower): found['kind'] = 'unavailability'
    elif re.search(r'добав|нов[ыо].*урок', lower): found['kind'] = 'add_lesson'
    elif re.search(r'перенес|измен|помен', lower): found['kind'] = 'change_lesson'
    days = set(re.findall(r'\b\d{4}-\d{2}-\d{2}\b', text))
    local = received_at.astimezone(ZONE).date()
    if 'завтра' in lower: days.add((local + timedelta(days=1)).isoformat())
    if 'сегодня' in lower: days.add(local.isoformat())
    if len(days) == 1: found['date'] = next(iter(days))
    elif len(days) > 1: ambiguities.append('Уточните одну дату отсутствия.')
    slots = set(int(x) for x in re.findall(r'(?<!\d)(\d{1,2})\s*(?:-?[йы]й?\s*)?урок', lower))
    for pattern, number in ((r'перв(?:ый|ом|ого)',1),(r'втор(?:ой|ом|ого)',2),(r'третий|треть(?:ем|его)',3),(r'четв[её]рт(?:ый|ом|ого)',4),(r'пят(?:ый|ом|ого)',5),(r'шест(?:ой|ом|ого)',6),(r'седьм(?:ой|ом|ого)',7),(r'восьм(?:ой|ом|ого)',8),(r'девят(?:ый|ом|ого)',9),(r'десят(?:ый|ом|ого)',10)):
        if re.search(r'\b(?:' + pattern + r')\s+урок', lower): slots.add(number)
    if len(slots) == 1 and 1 <= next(iter(slots)) <= 20: found['slot'] = next(iter(slots))
    elif slots: ambiguities.append('Уточните номер одного урока.')
    # Only an already prepared durable receipt may add advisory fields. Never
    # invoke a model here: this function also runs inside domain transactions.
    for key, value in request_extraction.bounded_result(advisory or {}).items():
        if key not in found: found[key] = value
    return found, ambiguities


def _prepare_teacher_lesson(lesson, profile, *, connection=None):
    if lesson is None: return None
    value = deepcopy(lesson)
    if _role(profile) == 'teacher':
        refs = profile.get('teacherRefs', [])
        _, blockers = _own(profile, connection)
        if blockers: raise HTTPException(403, 'Нет подтверждённого опубликованного расписания педагога')
        if not value['teachers'] and len(refs) == 1: value['teachers'] = deepcopy(refs)
        if not set(value['teachers']) <= set(refs):
            raise HTTPException(403, 'Преподаватель заявки берётся только из подтверждённой привязки')
    return value


def _admit_teacher_fields(profile, lesson_ids, lesson, *, connection=None):
    """Authorize structured references BEFORE any optional extraction/model hook."""
    if len(lesson_ids) != len(set(lesson_ids)):
        raise HTTPException(422, 'Идентификаторы выбранных уроков не должны повторяться')
    if _role(profile) == 'teacher':
        own, _ = _own(profile, connection)
        if any(ident not in {row['id'] for row in own} for ident in lesson_ids):
            raise HTTPException(403, 'Можно выбрать только собственные опубликованные уроки')
    return _prepare_teacher_lesson(lesson, profile, connection=connection)


def _absence_source(item, before, after, owner, week_start, connection):
    """Bind a single own published lesson; ordinals never replace real intervals."""
    blockers = []
    def block(code, message): blockers.append({'code': code, 'message': message})
    own, unavailable = _own(owner, connection)
    snapshot = read_active_snapshot(owner['schoolId'], connection)
    selected = item.get('lessonIds') or []
    if len(selected) != 1:
        block('one_affected_lesson_required', 'Для точечной правки выберите один свой урок; несколько занятий требуют проверенного пересчёта')
    source = next((row for row in own if row['id'] == after['id'] and row['id'] in selected), None)
    if unavailable or source is None or snapshot is None:
        block('published_source_required', 'Нужен конкретный собственный урок из действующей опубликованной недели')
        return None, blockers
    fields = ('id', 'day', 'slot', 'start', 'end', 'className', 'subject')
    if (before is None or any(before.get(k) != source[k] for k in fields)
            or not set(before.get('teachers', [])) & set(owner.get('teacherRefs', []))):
        block('published_draft_mismatch', 'Исходный урок черновика отличается от выбранного собственного опубликованного занятия')
    if source['date'] != item['resolvedDate'] or (item['slot'] is not None and item['slot'] != source['slot']):
        block('published_request_mismatch', 'Дата или номер заявки отличается от опубликованного занятия')
    after_date = (date.fromisoformat(week_start) + timedelta(days=after['day'])).isoformat()
    if (after_date == source['date'] and set(after['teachers']) & set(owner.get('teacherRefs', []))
            and after['start'] < source['end'] and source['start'] < after['end']):
        block('unavailability_not_resolved', 'Педагог всё ещё занят в недоступный реальный интервал; смена номера или кабинета не устраняет отсутствие')
    binding = {'publicationVersion': snapshot['version'], 'publicationHash': _hash(snapshot),
               'sourceLesson': {**{k: source[k] for k in fields}, 'date': source['date']},
               'ownerTeacherRefs': sorted(owner.get('teacherRefs', []))}
    return binding, blockers


def _reconcile(item, profile, *, initial_ambiguities=(), connection=None):
    missing = list(initial_ambiguities)
    if _role(profile) == 'teacher':
        own, _ = _own(profile, connection)
        if any(x not in {row['id'] for row in own} for x in item['lessonIds']):
            raise HTTPException(403, 'Можно выбрать только собственные опубликованные уроки')
        if item.get('lesson') and not item['lesson']['teachers']:
            missing.append('Выберите одну подтверждённую привязку преподавателя в точных полях урока.')
    if item['kind'] == 'question': missing.append('Уточните: сообщить недоступность, добавить или изменить урок?')
    if item['resolvedDate'] is not None:
        try: date.fromisoformat(item['resolvedDate'])
        except ValueError: raise HTTPException(422, 'Некорректная календарная дата') from None
    if item['kind'] == 'unavailability':
        if item['resolvedDate'] is None: missing.append('На какую дату сообщить недоступность?')
        if item['slot'] is None and not item['lessonIds']: missing.append('Какой урок затронут: укажите номер или свой опубликованный урок.')
        if _role(profile) == 'teacher':
            own, blockers = _own(profile, connection); allowed = {x['id']: x for x in own}
            if any(x not in allowed for x in item['lessonIds']): raise HTTPException(403, 'Можно указать только собственные опубликованные уроки')
            if blockers: missing.append('Сначала нужна публикация расписания и подтверждённая привязка учителя.')
            if item['resolvedDate'] and item['slot']:
                candidates = [x for x in own if x['date'] == item['resolvedDate'] and x['slot'] == item['slot']]
                if not item['lessonIds'] and len(candidates) == 1: item['lessonIds'] = [candidates[0]['id']]
                elif not item['lessonIds'] and len(candidates) != 1: missing.append('Уточните свой опубликованный урок на эту дату: найдено ' + str(len(candidates)) + '.')
            if item['lessonIds'] and any(allowed[x]['date'] != item['resolvedDate'] or (item['slot'] is not None and allowed[x]['slot'] != item['slot']) for x in item['lessonIds']):
                raise HTTPException(422, 'Дата или номер не совпадают с выбранным собственным уроком')
    if item['kind'] in {'add_lesson', 'change_lesson'} and item['lesson'] is None:
        missing.append('Заполните точные поля предлагаемого урока.')
    item['status'] = 'needs_clarification' if missing else 'submitted'
    item['serverReply'] = ' '.join(dict.fromkeys(missing)) if missing else 'Заявка принята на рассмотрение. Расписание пока не изменено.'
    if missing:
        item['questions'].append({'id': 'q_' + uuid4().hex, 'text': item['serverReply'], 'state': 'open', 'respondentMembershipId': item['owner']['membershipId'], 'respondentRevision': item['ownerBinding']['membershipRevision'], 'kind': 'clarification'})


def request_status(profile, *, storage=None):
    profile = _actor(profile)
    if storage is None:
        own, blockers = _own(profile)
    else:
        with _request_read_snapshot(storage) as (_, connection):
            own, blockers = _own(profile, connection)
            _actor(profile)
    extraction_enabled = storage is None and request_extraction.current_service(profile['schoolId']) is not None
    return {'enabled': True, 'modelEnabled': extraction_enabled, 'extractionEnabled': extraction_enabled, 'extractionHookConfigured': extraction_enabled,
            'timezone': str(ZONE), 'canManage': _manager(profile), 'currentUserId': profile['userId'],
            'membershipId': profile['membershipId'], 'ownLessons': own, 'ownTeacherRefs': deepcopy(profile.get('teacherRefs', [])) if not blockers and _role(profile) == 'teacher' else [],
            'blockers': blockers}


@contextmanager
def _request_read_snapshot(storage=None):
    from .school_calendar import _read_state
    from .school_publications import _read_connection

    connection = _read_connection() if storage is None else storage.read_connection()
    if connection is None:
        yield empty_school_state(), None
    else:
        with closing(connection):
            connection.execute('BEGIN')
            yield _read_state(connection), connection


def _replacement_progress(record, version, outcome_row):
    summary = {'id': record['id'], 'stage': 'published' if version is not None else 'draft',
               'publishedVersion': version, 'outcome': None, 'pendingReview': False}
    if version is None or outcome_row is None:
        return summary

    try:
        record_hash = _hash(record)
    except (TypeError, ValueError):
        return None

    def matches(value):
        target = value.get('target') if isinstance(value, dict) else None
        return (isinstance(target, dict) and target.get('replacementId') == record['id']
                and target.get('recordHash') == record_hash and target.get('lessonId') == record['lessonId']
                and target.get('date') == record['date'] and type(target.get('publishedVersion')) is int
                and target['publishedVersion'] >= version)

    active, pending = outcome_row['active'], outcome_row['pending']
    if active is not None and not matches(active):
        return None
    summary['pendingReview'] = matches(pending)
    accepted = [entry for entry in outcome_row['history']
                if entry['action'] in {'conducted_confirmed', 'approve', 'replaced_by_review'}]
    if active is not None:
        summary['outcome'] = active['kind']
        summary['stage'] = 'conducted' if active['kind'] == 'conducted' and len(accepted) <= 1 else 'corrected'
    elif accepted:
        latest = accepted[-1]
        if not matches(latest['before']):
            return None
        if latest['kind'] == 'retract' and latest['action'] in {'approve', 'replaced_by_review'}:
            summary['stage'] = 'retracted'
        elif latest['action'] == 'replaced_by_review':
            summary['stage'] = 'corrected'
        else:
            return None
    return summary


def _request_replacements(state, items, profile):
    school = profile['schoolId']
    can_manage = _manager(profile)
    sources = {item['id']: item for item in items if item['schoolId'] == school and item['kind'] == 'unavailability'
               and (can_manage or item['owner']['membershipId'] == profile['membershipId'])}
    if not sources:
        return {}

    from . import replacement_outcomes, school_replacements

    try:
        records = school_replacements._ns(state, school)['records']
    except (HTTPException, KeyError, TypeError):
        return {}
    if not isinstance(records, dict) or not records:
        return {}
    try:
        outcomes = replacement_outcomes._ledger(state, school)['records']
    except HTTPException:
        outcomes = None

    summaries = {}
    for identifier, record in records.items():
        if not isinstance(record, dict) or record.get('id') != identifier or not re.fullmatch(replacement_outcomes.REPLACEMENT_ID, identifier):
            continue
        proof, approval = record.get('proof'), record.get('approval')
        if not isinstance(proof, dict) or not isinstance(approval, dict) or approval.get('schoolId') != school:
            continue
        source = proof.get('absenceSource')
        if not isinstance(source, dict) or not isinstance(source.get('schoolRequestId'), str):
            continue
        item = sources.get(source['schoolRequestId'])
        if (item is None or record.get('date') != item['resolvedDate'] or item['lessonIds'] != [record.get('lessonId')]
                or type(source.get('schoolRequestRevision')) is not int
                or not 1 <= source['schoolRequestRevision'] <= item['revision']):
            continue
        version = record.get('publishedVersion')
        if record.get('status') == 'applied' and version is None:
            summary = _replacement_progress(record, None, None)
        elif record.get('status') == 'published' and type(version) is int and version >= 1 and outcomes is not None:
            summary = _replacement_progress(record, version, outcomes.get(identifier))
        else:
            summary = None
        # Ambiguous source history must not suggest a replacement to open.
        summaries[item['id']] = summary if item['id'] not in summaries else None
    return summaries


def list_requests(profile, *, storage=None):
    profile = _actor(profile)
    with _request_read_snapshot(storage) as (state, connection):
        items = [item for item in _read_ns(state, profile['schoolId'])['requests'].values()
                 if item['schoolId'] == profile['schoolId'] and _allowed(item, profile)]
        replacements = _request_replacements(state, items, profile)
        rows = [_public(item, profile, replacement=replacements.get(item['id']), connection=connection)
                for item in sorted(items, key=lambda item: item['receivedAt'], reverse=True)]
        _actor(profile)
        return {'requests': rows}


def get_request(ident, profile, *, storage=None):
    profile = _actor(profile)
    with _request_read_snapshot(storage) as (state, connection):
        item = _get(_read_ns(state, profile['schoolId']), ident)
        _access(item, profile)
        replacement = _request_replacements(state, [item], profile).get(ident)
        result = {'request': _public(item, profile, replacement=replacement, connection=connection),
                  'serverReply': item['serverReply']}
        _actor(profile)
        return result


def get_request_in_state(state, ident, profile):
    profile = _actor(profile); item = _get(_read_ns(state, profile['schoolId']), ident); _access(item, profile)
    return _response(item, profile)


def _extraction_actor(profile):
    return {**_binding(profile), **{key: deepcopy(profile.get(key)) for key in
        ('staffRole', 'role', 'permissions', 'teacherRefs', 'calendarTeacherIds')}}


def _trusted_received_at(value):
    if value is None: return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(422, 'Нужно доверенное время получения с часовым поясом')
    return value.astimezone(timezone.utc).isoformat()


def _create_fingerprint(body, received_at=None):
    payload = {'operation': 'create', 'body': body.model_dump()}
    if received_at is not None: payload['trustedReceivedAt'] = received_at
    return _hash(payload)


def _extraction_payload(body, request_id=None, question_id=None, received_at=None):
    payload = {'body': body.model_dump(), 'requestId': request_id, 'questionId': question_id}
    if received_at is not None: payload['trustedReceivedAt'] = received_at
    return payload


def _admit_reply(item, body, profile, question_id=None):
    mine = [q for q in item['questions'] if q['state'] == 'open' and q['respondentMembershipId'] == profile['membershipId']]
    if question_id is not None:
        mine = [q for q in mine if q['id'] == question_id]
        if not mine: raise HTTPException(409, 'Этот вопрос закрыт, изменён или адресован другому сотруднику')
    if item['owner']['membershipId'] != profile['membershipId'] and not mine:
        raise HTTPException(403, 'Ответ доступен владельцу или назначенному адресату открытого вопроса')
    if any(q['respondentRevision'] != profile['membershipRevision'] for q in mine):
        raise HTTPException(409, 'Привязка адресата изменилась; нужен новый вопрос')
    if body.fields is not None and item['owner']['membershipId'] != profile['membershipId']:
        raise HTTPException(403, 'Адресат отвечает текстом, поля заявки изменяет её владелец')
    if item['owner']['membershipId'] == profile['membershipId']:
        supplied = body.fields.model_dump(exclude_unset=True) if body.fields is not None else {}
        _admit_teacher_fields(profile, supplied.get('lessonIds', item['lessonIds']), supplied.get('lesson', item['lesson']))
    return mine


def _preflight_extraction(operation, body, profile, request_id, question_id, received_at):
    profile = _actor(profile)
    state = db.read_state(); data = _read_ns(state, profile['schoolId'])
    fp = _create_fingerprint(body, received_at) if operation == 'create' else _hash(
        {'operation': 'reply', 'id': request_id, 'body': body.model_dump(), 'questionId': question_id})
    if _replay(data, body.requestId, fp, profile): return None
    if operation == 'create':
        if len(data['requests']) >= LIMIT: raise HTTPException(429, 'Очередь школьных заявок заполнена')
        _admit_teacher_fields(profile, body.lessonIds, body.lesson.model_dump() if body.lesson else None)
        return received_at or utc_now().isoformat()
    item = _get(data, request_id); _access(item, profile); _cas(item, body.expectedRevision)
    _admit_reply(item, body, profile, question_id)
    if item['owner']['membershipId'] != profile['membershipId']: return None
    return item['receivedAt']


def prepare_extraction(operation, body, profile, *, request_id=None, question_id=None, received_at=None):
    """Authorize, reserve and invoke outside domain transactions (HTTP or worker).

    received_at is a server-only aware timestamp for authenticated MAX creation;
    it is never accepted in a request body. Reply keeps its original anchor.
    """
    profile = _actor(profile)
    if operation not in {'create', 'reply'}: raise ValueError('Unsupported extraction operation')
    if operation == 'reply' and received_at is not None:
        raise HTTPException(422, 'Ответ использует исходное время получения заявки')
    received_at = _trusted_received_at(received_at)
    body = _body(CreateRequest if operation == 'create' else ReplyInput, body)
    received = _preflight_extraction(operation, body, profile, request_id, question_id, received_at)
    if received is None: return None
    service = request_extraction.current_service(profile['schoolId'])
    if service is None: return None
    binding_anchor = received_at if operation == 'create' else received
    def before_invoke():
        # The OS lock may have blocked since preflight. Recheck after waiting,
        # with no SQLite writer held, immediately before the first model call.
        current_anchor = _preflight_extraction(operation, body, profile, request_id, question_id, received_at)
        if current_anchor is None:
            raise HTTPException(409, 'Команда уже обработана; получите сохранённый результат')
        if operation == 'reply' and current_anchor != received:
            raise HTTPException(409, 'Исходная дата обращения изменилась')
        if request_extraction.current_service(profile['schoolId']) is not service:
            raise HTTPException(409, 'Настройка извлечения изменилась')
    return service.prepare(operation, body.requestId, _extraction_actor(profile),
        _extraction_payload(body, request_id, question_id, binding_anchor), body.text, received,
        before_invoke=before_invoke)


def _consume_extraction(operation, body, profile, receipt, *, request_id=None, question_id=None, received_at=None):
    service = request_extraction.current_service(profile['schoolId'])
    if service is None:
        if receipt is not None: raise HTTPException(409, 'Извлечение не разрешено в этом приложении')
        return datetime.fromisoformat(received_at) if received_at else utc_now(), {}, 'disabled'
    if receipt is None:
        raise HTTPException(409, 'Сначала подготовьте квитанцию извлечения вне транзакции')
    result = service.resolve(receipt, operation, body.requestId, _extraction_actor(profile),
        _extraction_payload(body, request_id, question_id, received_at))
    if received_at is not None and received_at != result.received_at:
        raise HTTPException(409, 'Исходная дата обращения изменилась')
    return datetime.fromisoformat(result.received_at), result.advisory, result.status


def create_request(body, profile, *, extraction_receipt=None, received_at=None, storage=None):
    _actor(profile)
    if storage is not None:
        if extraction_receipt is not None:
            raise HTTPException(409, 'Извлечение не разрешено на стенде замены')
        with request_extraction.bound_service(None), storage.atomic_domain() as (connection, state):
            result = create_request_in_state(state, body, profile, received_at=received_at, connection=connection)
            if result['request']['kind'] != 'unavailability':
                raise HTTPException(422, 'На стенде замены можно создать только обращение об отсутствии')
            return result
    if extraction_receipt is None:
        extraction_receipt = prepare_extraction('create', body, profile, received_at=received_at)
    with db.edit_state() as state:
        return create_request_in_state(state, body, profile, extraction_receipt=extraction_receipt, received_at=received_at)


def create_request_in_state(state, body, profile, *, extraction_receipt=None, received_at=None, connection=None):
    # Stage only our namespace; a transport may catch validation failures inside
    # its larger transaction. Failed commands must not leave partial state.
    staged = {**state, '_schoolRequests': deepcopy(state.get('_schoolRequests', {'formatVersion': 1, 'schools': {}}))}
    result = _create_request_in_state(staged, body, profile, extraction_receipt=extraction_receipt,
                                      received_at=received_at, connection=connection)
    state['_schoolRequests'] = staged['_schoolRequests']
    return result


def _create_request_in_state(state, body, profile, *, extraction_receipt=None, received_at=None, connection=None):
    profile = _actor(profile); body = _body(CreateRequest, body); received_at = _trusted_received_at(received_at); fp = _create_fingerprint(body, received_at)
    _actor(profile); data = _ns(state, profile['schoolId'])
    replay = _replay(data, body.requestId, fp, profile, connection=connection)
    if replay: return replay
    if len(data['requests']) >= LIMIT: raise HTTPException(429, 'Очередь школьных заявок заполнена')
    prepared_lesson = _admit_teacher_fields(profile, body.lessonIds, body.lesson.model_dump() if body.lesson else None,
                                             connection=connection)
    received, advisory, extraction_status = _consume_extraction('create', body, profile, extraction_receipt, received_at=received_at)
    found, ambiguous = _recognize(body.text, received, advisory)
    kind = body.kind or found.get('kind') or 'question'
    # Validate advisory values through the same strict models as form input.
    extracted = _body(Fields, {k: found[k] for k in ('date', 'slot') if k in found})
    if not isinstance(kind, str) or kind not in {'unavailability', 'add_lesson', 'change_lesson', 'question'}: kind = 'question'
    if body.date and extracted.date and body.date != extracted.date: ambiguous.append('Дата в форме отличается от текста: подтвердите нужную дату.')
    when = body.date or extracted.date; slot = body.slot or extracted.slot
    item = {'id': 'sr_' + uuid4().hex, 'revision': 1, 'schoolId': profile['schoolId'], 'kind': kind,
            'text': body.text, 'receivedAt': received.isoformat(), 'resolvedDate': when, 'slot': slot,
            'lessonIds': body.lessonIds, 'lesson': prepared_lesson,
            'owner': {'membershipId': profile['membershipId'], 'displayName': profile.get('name', 'Сотрудник школы')},
            'ownerBinding': _binding(profile), 'questions': [], 'messages': [], 'proposal': None,
            'serverReply': '', 'audit': [{'action': 'created', 'at': received.isoformat(), 'membershipId': profile['membershipId']}]}
    _reconcile(item, profile, initial_ambiguities=ambiguous, connection=connection)
    if extraction_status == 'unknown':
        item['extractionStatus'] = 'unknown'
        item['serverReply'] += ' Ответ модели не подтверждён; автоматический повтор не выполнялся.'
    data['requests'][item['id']] = item
    return _record(data, body.requestId, fp, item, profile, connection=connection)



def _command_in_state(state, ident, body, profile, operation, update, *, question_id=None):
    fp = _hash({'operation': operation, 'id': ident, 'body': body.model_dump(), 'questionId': question_id})
    profile = _actor(profile, manage=operation in {'question', 'proposal'})
    data = _ns(state, profile['schoolId']); item = _get(data, ident); _access(item, profile)
    replay = _replay(data, body.requestId, fp, profile)
    if replay: return replay
    _cas(item, body.expectedRevision)
    if len(item['messages']) >= 200 or len(item['questions']) >= 200: raise HTTPException(429, 'Слишком много сообщений в заявке')
    update(item)
    item['revision'] += 1
    item['audit'].append({'action': operation, 'membershipId': profile['membershipId'], 'at': utc_now().isoformat()})
    return _record(data, body.requestId, fp, item, profile)


def _command(ident, body, profile, operation, update, *, connection_aware=False):
    if connection_aware:
        with _atomic_domain() as (connection, state):
            return _command_in_state(state, ident, body, profile, operation, lambda item: update(item, connection))
    with db.edit_state() as state:
        return _command_in_state(state, ident, body, profile, operation, update)


def reply_request(ident, body, profile, *, question_id=None, extraction_receipt=None):
    _actor(profile)
    if extraction_receipt is None:
        extraction_receipt = prepare_extraction('reply', body, profile, request_id=ident, question_id=question_id)
    with db.edit_state() as state:
        return reply_request_in_state(state, ident, body, profile, question_id=question_id, extraction_receipt=extraction_receipt)


def reply_request_in_state(state, ident, body, profile, *, question_id=None, extraction_receipt=None):
    staged = {**state, '_schoolRequests': deepcopy(state.get('_schoolRequests', {'formatVersion': 1, 'schools': {}}))}
    result = _reply_request_in_state(staged, ident, body, profile, question_id=question_id, extraction_receipt=extraction_receipt)
    state['_schoolRequests'] = staged['_schoolRequests']
    return result


def _reply_request_in_state(state, ident, body, profile, *, question_id=None, extraction_receipt=None):
    profile = _actor(profile); body = _body(ReplyInput, body)
    def update(item):
        mine = _admit_reply(item, body, profile, question_id)
        # Chat and MAX may provide a free-text answer. Resolve only missing
        # fields against ORIGINAL trusted receive time; never move 'tomorrow'
        # merely because a later reply arrived on another day.
        ambiguities = []
        if item['owner']['membershipId'] == profile['membershipId']:
            _, advisory, extraction_status = _consume_extraction('reply', body, profile, extraction_receipt, request_id=ident, question_id=question_id, received_at=item['receivedAt'])
            found, ambiguities = _recognize(body.text, datetime.fromisoformat(item['receivedAt']), advisory)
            explicit = set(re.findall(r'\b\d{4}-\d{2}-\d{2}\b', body.text))
            if item['resolvedDate'] and explicit and explicit != {item['resolvedDate']} and not (body.fields and body.fields.date):
                raise HTTPException(422, 'Дата ответа отличается от сохранённой: подтвердите точную дату в поле заявки')
            if item['resolvedDate'] is None and 'date' in found:
                item['resolvedDate'] = _body(Fields, {'date': found['date']}).date
            if item['slot'] is None and 'slot' in found:
                item['slot'] = _body(Fields, {'slot': found['slot']}).slot
            if item['kind'] == 'question' and found.get('kind') in {'unavailability', 'add_lesson', 'change_lesson'}:
                item['kind'] = found['kind']
        for q in mine: q.update(state='answered', answer=body.text, answeredAt=utc_now().isoformat())
        item['messages'].append({'text': body.text, 'membershipId': profile['membershipId']})
        item['proposal'] = None
        if body.fields is not None:
            f = body.fields.model_dump(exclude_unset=True)
            if f.get('kind') is not None: item['kind'] = f['kind']
            if 'date' in f: item['resolvedDate'] = f['date']
            for k in ('slot', 'lessonIds'): 
                if k in f: item[k] = f[k]
            if 'lesson' in f: item['lesson'] = _prepare_teacher_lesson(f['lesson'], profile)
        if item['owner']['membershipId'] == profile['membershipId']:
            _reconcile(item, profile, initial_ambiguities=ambiguities)
            if extraction_status == 'unknown':
                item['extractionStatus'] = 'unknown'
                item['serverReply'] += ' Ответ модели не подтверждён; автоматический повтор не выполнялся.'
            if item['status'] == 'submitted' and any(q['state'] == 'open' for q in item['questions']):
                item['status'] = 'waiting_reply'; item['serverReply'] = 'Ответ сохранён. Ожидаются остальные ответы.'
        else:
            item['status'] = 'waiting_reply' if any(q['state'] == 'open' for q in item['questions']) else 'submitted'
            item['serverReply'] = 'Ответ сохранён. Завуч проверит сведения; расписание не изменено.'
    return _command_in_state(state, ident, body, profile, 'reply', update, question_id=question_id)


def question_request(ident, body, profile):
    profile = _actor(profile, True); body = _body(QuestionInput, body)
    member = launch_runtime.resolve_member(profile, body.respondentMembershipId)
    if _role(member) not in {'teacher', 'director', 'planner'}: raise HTTPException(403, 'Неподдерживаемый адресат')
    def update(item):
        current = launch_runtime.resolve_member(profile, body.respondentMembershipId)
        item['proposal'] = None
        item['questions'].append({'id': 'q_' + uuid4().hex, 'text': body.text, 'state': 'open', 'respondentMembershipId': current['membershipId'], 'respondentRevision': current['membershipRevision'], 'kind': 'manager'})
        item['messages'].append({'text': body.text, 'membershipId': profile['membershipId'], 'respondentMembershipId': current['membershipId']})
        item['status'] = 'waiting_reply'; item['serverReply'] = 'Вопрос сохранён для выбранного сотрудника. Изменений расписания нет.'
    return _command(ident, body, profile, 'question', update)


def _draft_hash(draft):
    return _hash({'revision': draft['revision'], 'lessons': draft['lessons']})


def proposal_request(ident, body, profile):
    profile = _actor(profile, True); _domain(profile); body = _body(ProposalInput, body)
    def update(item, connection):
        owner = _current_binding(item['ownerBinding'], profile)
        if any(q['state'] == 'open' for q in item['questions']): raise HTTPException(409, 'Сначала нужны ответы на открытые вопросы')
        if item['status'] == 'needs_clarification': raise HTTPException(409, 'Сначала уточните заявку')
        draft = timetable._public(timetable._load(connection)) if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='timetable_draft'").fetchone() else timetable._empty()
        timetable._cas(draft, body.expectedDraftRevision)
        lesson_id = body.lessonId if body.action == 'change' else 'srlesson_' + uuid4().hex
        if body.action == 'change' and not lesson_id: raise HTTPException(422, 'Выберите исходный урок')
        before = next((x for x in draft['lessons'] if x['id'] == lesson_id), None)
        if body.action == 'change' and before is None: raise HTTPException(404, 'Урок черновика не найден')
        after = {'id': lesson_id, **body.lesson.model_dump()}
        if not after['teachers'] or not after['rooms']: raise HTTPException(422, 'В точном предложении явно укажите преподавателя и кабинет')
        patch = {'kind': 'text', 'expectedRevision': draft['revision'], 'changes': [{'lessonId': lesson_id, 'before': before, 'after': after}], 'summary': 'Точное предложение по школьной заявке', 'uncheckedChecks': ['Квалификация, вместимость, подгруппы и школьные нормативы не доказаны свободной правкой.']}
        patch['hash'] = timetable.text_patch_hash(patch)
        timetable.validate_text_patch_context(patch, connection)
        proposed = [x for x in draft['lessons'] if x['id'] != lesson_id] + [{**after, 'raw': '', 'sourceCell': '', 'edited': True}]
        issues = [x for x in timetable.compute_issues(proposed) if lesson_id in x['lessonIds']]
        blockers = [x for x in issues if x['kind'] in {'class', 'teacher', 'room', 'ambiguous'}]
        from .school_calendar import effective_calendar_snapshot
        calendar = effective_calendar_snapshot(body.targetWeekStart, school_id=profile['schoolId'], connection=connection)
        blockers.extend(deepcopy(calendar['readiness']['blockers']))
        first = date.fromisoformat(body.targetWeekStart)
        after_date = (first + timedelta(days=after['day'])).isoformat()
        if not any(d['date'] == after_date and d['status'] == 'school' for d in calendar['days']):
            blockers.append({'code': 'target_day_closed', 'message': 'Конечная дата не подтверждена как учебная'})
        if item['resolvedDate']:
            if not body.targetWeekStart <= item['resolvedDate'] <= (first + timedelta(days=5)).isoformat():
                blockers.append({'code': 'request_week_mismatch', 'message': 'Дата заявки относится к другой неделе'})
        absence_binding = None
        if item['kind'] == 'unavailability':
            if body.action != 'change' or not item['lessonIds'] or lesson_id not in item['lessonIds']:
                blockers.append({'code': 'affected_lesson_required', 'message': 'Предложение должно изменить конкретный урок из заявки'})
            absence_binding, absence_blockers = _absence_source(item, before, after, owner, body.targetWeekStart, connection)
            blockers.extend(absence_blockers)
        item['proposal'] = {'action': body.action, 'lessonId': lesson_id, 'before': before, 'after': after,
            'expectedDraftRevision': draft['revision'], 'draftHash': _draft_hash(draft), 'issues': issues,
            'targetWeekStart': body.targetWeekStart, 'targetDate': after_date,
            'calendarBinding': {'schoolId': profile['schoolId'], 'weekStart': body.targetWeekStart,
                'calendarRevision': calendar['calendarRevision'], 'inputHash': calendar['inputHash']},
            'absenceSourceBinding': absence_binding, 'blockers': blockers, 'patch': patch}
        item['proposal']['hash'] = _hash(item['proposal'])
        item['proposal']['proposalHash'] = item['proposal']['hash']
        item['proposalActor'] = _binding(profile)
        item['status'] = 'proposed' if not blockers else 'needs_clarification'
        item['serverReply'] = 'Предложение подготовлено. Требуется отдельное утверждение черновика.' if not blockers else 'Нужно уточнение: ' + ' '.join(dict.fromkeys(b['message'] for b in blockers))[:700] + ' Расписание не изменено.'
    return _command(ident, body, profile, 'proposal', update, connection_aware=True)


@contextmanager
def _atomic_domain():
    # Same domain DB and state layout as db.edit_state; connection also carries
    # timetable changes so request checkpoint cannot diverge from draft receipt.
    connection = db.connect()
    try:
        connection.execute('BEGIN IMMEDIATE')
        state = json.loads(connection.execute('SELECT payload FROM demo_state WHERE id=1').fetchone()[0]); verify_school_state(state)
        yield connection, state
        connection.execute('UPDATE demo_state SET payload=? WHERE id=1', (json.dumps(state, ensure_ascii=False),))
        connection.commit()
    except BaseException:
        connection.rollback(); raise
    finally:
        connection.close()


def decision_request(ident, body, profile):
    profile = _actor(profile, True); _domain(profile); body = _body(DecisionInput, body)
    fp = _hash({'operation': 'decision', 'id': ident, 'body': body.model_dump()})
    with _atomic_domain() as (connection, state):
        _actor(profile, True); data = _ns(state, profile['schoolId']); item = _get(data, ident); _access(item, profile)
        replay = _replay(data, body.requestId, fp, profile)
        if replay: return replay
        _cas(item, body.expectedRevision)
        p = item['proposal']
        if not p or p['hash'] != body.proposalHash: raise HTTPException(409, 'Утвердите точное сохранённое предложение')
        if body.decision == 'reject':
            item['status'] = 'rejected'; item['serverReply'] = 'Предложение отклонено. Расписание не изменено.'
        else:
            owner = _current_binding(item['ownerBinding'], profile); _current_binding(item['proposalActor'], profile)
            if item['status'] != 'proposed' or p['blockers']: raise HTTPException(409, 'Предложение не готово: сначала уточните ограничения')
            from .school_calendar import assert_calendar_snapshot_current
            binding = p['calendarBinding']
            current_calendar = assert_calendar_snapshot_current(binding['calendarRevision'], binding['inputHash'], binding['weekStart'], school_id=profile['schoolId'], connection=connection)
            if not current_calendar['readiness']['ready']: raise HTTPException(409, 'Календарь требует подтверждения')
            draft = timetable._public(timetable._load(connection))
            if _draft_hash(draft) != p['draftHash']: raise HTTPException(409, 'Черновик изменён: подготовьте новое предложение')
            if item['kind'] == 'unavailability':
                source_binding, blockers = _absence_source(item, p['before'], p['after'], owner, p['targetWeekStart'], connection)
                if blockers or source_binding != p.get('absenceSourceBinding'):
                    raise HTTPException(409, 'Опубликованный исходный урок или устранение недоступности изменились; нужно новое предложение')
            adapter = {**profile, 'role': 'admin' if _role(profile) == 'planner' else 'director'}
            applied = timetable.apply_approved_request_patch(connection, p['patch'], adapter,
                idempotency_key='school_request_' + item['id'], guard=lambda conn: _actor(profile, True))
            item['status'] = 'applied'; item['draftReceipt'] = {'revision': applied['revision'], 'proposalHash': p['hash'], 'targetWeekStart': p['targetWeekStart']}
            item['serverReply'] = 'Правка сохранена в недельный черновик для недели ' + p['targetWeekStart'] + '. Датированная публикация и уведомления не выполнялись.'
        item['revision'] += 1
        item['audit'].append({'action': body.decision, 'at': utc_now().isoformat(), 'membershipId': profile['membershipId'], 'proposalHash': p['hash']})
        return _record(data, body.requestId, fp, item, profile, item.get('draftReceipt', {}).get('revision'))


class SafeRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handler(request):
            try: return await original(request)
            except RequestValidationError: raise HTTPException(422, 'Некорректные поля школьной заявки') from None
        return handler


def router_for(storage=None):
    routes = APIRouter(prefix='/api/school/requests', tags=['school requests'], route_class=SafeRoute)
    @routes.get('/status')
    def status_route(profile=Depends(session_profile)): return request_status(profile, storage=storage)
    @routes.get('')
    def list_route(profile=Depends(session_profile)): return list_requests(profile, storage=storage)
    @routes.post('')
    def create_route(body: CreateRequest, profile=Depends(session_profile)): return create_request(body, profile, storage=storage)
    @routes.get('/{ident}')
    def detail_route(ident: str, profile=Depends(session_profile)): return get_request(ident, profile, storage=storage)
    if storage is None:
        @routes.post('/{ident}/reply')
        def reply_route(ident: str, body: ReplyInput, profile=Depends(session_profile)): return reply_request(ident, body, profile)
        @routes.post('/{ident}/question')
        def question_route(ident: str, body: QuestionInput, profile=Depends(session_profile)): return question_request(ident, body, profile)
        @routes.post('/{ident}/proposal')
        def proposal_route(ident: str, body: ProposalInput, profile=Depends(session_profile)): return proposal_request(ident, body, profile)
        @routes.post('/{ident}/decision')
        def decision_route(ident: str, body: DecisionInput, profile=Depends(session_profile)): return decision_request(ident, body, profile)
    return routes


router = router_for()
